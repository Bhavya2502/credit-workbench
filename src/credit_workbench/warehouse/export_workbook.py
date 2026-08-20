"""The company / line-item / industry map of this warehouse, as one Excel workbook.

    uv run python -m credit_workbench.warehouse.export_workbook

Answers "what do we actually hold" without anyone writing SQL: every company we have
financials for, the industry it maps to under all three schemes with names attached,
every line item in the spread with its XBRL tag alternatives and fill rate, how many
years of each line item exist per company, and the values themselves.

Sheets, and the grain of each:

  README                one row per sheet - what it is, its grain, and its caveats
  Companies             one company
  Industry_map          one SIC code, mapped up to peer group and division
  Line_items            one line item of the spread
  Coverage_by_company   one company x one line item (years populated)
  Company_years         one company-year - the spine, with outcome labels
  Values                one company-year, one column per line item - the numbers
  Ratios                one ratio
  Warehouse_tables      one table in the warehouse

Three things are deliberately true of this file and are stated on the README sheet
rather than left for a reader to discover:

**The universe is companies with a usable annual spread**, 15,550 of roughly 18,200 with
any XBRL fact. The rest are mostly pre-revenue or non-operating filers - they have a
balance sheet but no income statement, so no spread is built. It is not every SEC filer.

**Values are `first_reported`**, the figures as originally published. `marts.spreads_a`
also holds a `latest` basis with restatements folded in; mixing them is how a model ends
up training on knowledge that did not exist at the filing date.

**A blank cell means the filer did not tag that concept**, not that the value is zero.
The fill rate on the Line_items sheet says how often each one is present, which is the
only honest way to read the wide sheets.

Two implementation notes. The workbook is written in `constant_memory` mode and every
sheet is streamed from its cursor in batches, because the Values sheet alone is 16.8m
cells and materialising it would need several gigabytes. And the outcome join is keyed on
`(cik, fy, period_end)` with the outcome side pre-deduplicated: `marts.credit_outcomes`
is grained finer than `(cik, fy)`, so joining on the pair alone can fan out. The row
count is asserted against the source before a single sheet is written.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import xlsxwriter

from credit_workbench.common.config import motherduck_token

OUT = Path("export")
BASIS = "first_reported"
BATCH = 5_000

# Columns of marts.spreads_a that identify the row rather than carry a figure.
KEY_COLS = ("cik", "company_name", "sic", "basis", "period_end", "fy", "last_filed",
            "ebit_source")

# Codes that name a legal form rather than a line of business. Flagged, never dropped.
FORM_CODES = ("6770", "9995", "8880", "6199")

SHEET_NOTES = [
    ("Companies", "One row per company (CIK)",
     "Universe is companies with a usable annual spread. line_items_ever_populated "
     "counts how many of the spread's value columns the company ever reported."),
    ("Industry_map", "One row per SIC4 code in use",
     "sic4 -> sic3 -> sic2 -> division is SIC's own hierarchy. peer_group is this "
     "project's 140-group scheme, rolled up only as far as needed to reach 30 "
     "companies. house_industry is reserved for a house scheme and is empty."),
    ("Line_items", "One row per line item of the spread",
     "xbrl_tags lists the tag alternatives the mapper accepts, in priority order. "
     "Items with statement DERIVED are computed by this platform, not tagged by "
     "filers. pct_populated is the share of company-years carrying a value."),
    ("Coverage_by_company", "One row per company, one column per line item",
     "The cell is the number of years that line item is populated for that company. "
     "0 means the company never reported it. This is the map of what exists."),
    ("Company_years", "One row per company-year",
     "The spine. Carries the industry mapping, the observation date the outcome "
     "labels are measured from, and those labels."),
    ("Values", "One row per company-year, one column per line item",
     f"basis = {BASIS}: as originally published. Blank means not tagged, not zero. "
     "Money is in the filer's reporting currency as filed, unscaled."),
    ("Ratios", "One row per ratio",
     "The ratio library and how much of the population each one computes for. "
     "Values live in marts.ratio_values, not in this workbook."),
    ("Warehouse_tables", "One row per table",
     "Everything in the warehouse, including what this workbook does not cover - "
     "covenants, debt instruments, segments, governance, notes and events."),
]

CAVEATS = [
    ("Universe",
     "15,550 companies with a usable annual spread, of roughly 18,200 with any XBRL "
     "fact. The excluded ones are mostly pre-revenue or non-operating filers: 885 of "
     "909 measured in FY2024 have Assets but only 224 have Revenues."),
    ("Basis",
     f"Values are {BASIS} - the figures as first published. marts.spreads_a also "
     "holds a 'latest' basis with restatements folded in. Do not mix them."),
    ("Blank cells",
     "A blank is 'the filer did not tag this concept', never zero. Check the "
     "pct_populated column on Line_items before treating any column as complete."),
    ("Industry codes",
     "SIC is self-assigned by the filer on its own filings and audited by nobody. "
     "9.8% of companies carry more than one SIC across their history, and 1,327 of "
     "the 1,468 that moved crossed a major group - is_form_code flags the buckets "
     "(Blank Checks, Non-Operating) that are a legal form, not an industry."),
    ("Fiscal years",
     "fy is the filer's own fiscal-year label and ranges 2004-2028; values beyond "
     "the current year are the filer's forward labelling, not our error."),
    ("Currency",
     "Values are as filed and unscaled, in the filer's reporting currency. Foreign "
     "private issuers filing in EUR or JPY are not converted."),
    ("Outcome labels",
     "default_24m is a severity-5 8-K within 24 months of the filing date - "
     "bankruptcy, debt acceleration or non-reliance. It is an observed event, not an "
     "agency default, and the last two years of the window are censored."),
    ("Not in this workbook",
     "Covenants, debt instruments, segments, concentration, governance, note text, "
     "8-K events and adjusted metrics. See the Warehouse_tables sheet."),
]

FMT: dict[str, object] = {}


def value_columns(con) -> list[str]:
    """Every column of spreads_a that carries a figure, in the statement's own order."""
    cols = [r[0] for r in con.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'marts' AND table_name = 'spreads_a'
        ORDER BY ordinal_position""").fetchall()]
    return [c for c in cols if c not in KEY_COLS]


def stream_sheet(wb, con, name, query, widths=None, money_from=None):
    """Run a query and stream it into a sheet in batches, never holding it all."""
    cur = con.execute(query)
    heads = [d[0] for d in cur.description]
    ws = wb.add_worksheet(name)
    ws.freeze_panes(1, 1)
    for i, h in enumerate(heads):
        w = (widths or {}).get(h, min(max(len(str(h)) + 2, 10), 42))
        fmt = FMT["money"] if (money_from is not None and i >= money_from) else None
        ws.set_column(i, i, w, fmt)
    ws.write_row(0, 0, heads, FMT["header"])
    n = 0
    while True:
        batch = cur.fetchmany(BATCH)
        if not batch:
            break
        for row in batch:
            n += 1
            ws.write_row(n, 0, row)          # None writes a blank cell, not a zero
    if n:
        ws.autofilter(0, 0, n, len(heads) - 1)
    print(f"  sheet {name:<20} {n:>9,} rows x {len(heads):>3} cols")
    return n


def sheet_readme(wb, counts):
    ws = wb.add_worksheet("README")
    ws.set_column(0, 0, 24)
    ws.set_column(1, 1, 34)
    ws.set_column(2, 2, 96)
    r = 0
    ws.write(r, 0, "Credit Workbench - company, industry and line-item map",
             FMT["title"])
    r += 2
    for label, value in (("Built", date.today().isoformat()),
                         ("Source", "MotherDuck credit_workbench (SEC XBRL)"),
                         ("Basis", BASIS)):
        ws.write(r, 0, label, FMT["bold"])
        ws.write(r, 1, value)
        r += 1
    r += 1

    ws.write(r, 0, "SHEETS", FMT["title"])
    r += 1
    ws.write_row(r, 0, ["Sheet", "Grain", "What to know"], FMT["header"])
    r += 1
    for nm, grain, note in SHEET_NOTES:
        ws.write(r, 0, nm, FMT["bold"])
        ws.write(r, 1, grain)
        ws.write(r, 2, note, FMT["wrap"])
        ws.set_row(r, 32)
        r += 1
    r += 1

    ws.write(r, 0, "READ THIS BEFORE USING THE NUMBERS", FMT["title"])
    r += 1
    ws.write_row(r, 0, ["Topic", "", "Caveat"], FMT["header"])
    r += 1
    for topic, text in CAVEATS:
        ws.write(r, 0, topic, FMT["bold"])
        ws.write(r, 2, text, FMT["wrap"])
        ws.set_row(r, 44)
        r += 1
    r += 1

    ws.write(r, 0, "ROW COUNTS", FMT["title"])
    r += 1
    for k, v in counts.items():
        ws.write(r, 0, k)
        ws.write_number(r, 1, v)
        r += 1


def guard_keys(con) -> None:
    """Every join in this workbook is on a key that must be unique. Assert it first.

    Fan-out on a non-unique key has damaged three tables in this project already, and it
    is invisible in the output: each row still looks correct while the totals inflate.
    Cheaper to check here than to explain a wrong workbook later.
    """
    checks = [
        ("marts.spreads_a (cik, fy, period_end) within basis",
         f"""SELECT count(*), count(DISTINCT (cik, fy, period_end))
             FROM marts.spreads_a WHERE basis = '{BASIS}'"""),
        ("ref.sic_hierarchy.sic4",
         "SELECT count(*), count(DISTINCT sic4) FROM ref.sic_hierarchy"),
        ("ref.industry_group.sic4",
         "SELECT count(*), count(DISTINCT sic4) FROM ref.industry_group"),
        ("ref.dim_company.cik",
         "SELECT count(*), count(DISTINCT cik) FROM ref.dim_company"),
    ]
    for label, q in checks:
        rows, keys = con.execute(q).fetchone()
        state = "ok" if rows == keys else "NOT UNIQUE"
        print(f"  guard {label:<48} {rows:>9,} rows {keys:>9,} keys  {state}")
        if rows != keys:
            raise SystemExit(
                f"{label} is not unique ({rows:,} rows, {keys:,} keys): joining on it "
                "would inflate the workbook. Fix the key before exporting.")


def build_spine(con) -> int:
    """One row per company-year on the chosen basis, with industry and outcomes joined.

    `marts.credit_outcomes` is grained on (cik, fy, period_end, observation_date), so
    joining it on (cik, fy) alone can duplicate a company-year that changed its fiscal
    period. It is deduplicated to one row per (cik, fy, period_end) first, and the join
    is keyed on all three. Fan-out here would inflate every count in the workbook while
    each individual row still looked correct, so the total is asserted afterwards.
    """
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE outcomes_one AS
        SELECT TRY_CAST(cik AS BIGINT) AS cik, fy, period_end,
               max(observation_date) AS observation_date,
               max(events_24m) AS events_24m,
               arg_max(first_event_category, observation_date) AS first_event_category,
               max(worst_severity_24m) AS worst_severity_24m,
               bool_or(distress_12m) AS distress_12m,
               bool_or(distress_24m) AS distress_24m,
               bool_or(default_12m) AS default_12m,
               bool_or(default_24m) AS default_24m,
               bool_or(bankruptcy_24m) AS bankruptcy_24m,
               bool_or(debt_acceleration_24m) AS debt_acceleration_24m,
               bool_or(non_reliance_24m) AS non_reliance_24m,
               bool_or(late_filing_24m) AS late_filing_24m
        FROM marts.credit_outcomes GROUP BY 1, 2, 3""")

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE spine AS
        SELECT s.cik, s.company_name, s.fy, s.period_end, s.last_filed,
               s.sic AS sic4, h.sic4_description, h.sic3, h.sic2,
               h.division_code, h.division_name,
               g.industry_code AS peer_group_code, g.industry_label AS peer_group_label,
               g.industry_level AS peer_group_level, g.peers AS peers_in_group,
               s.sic IN {FORM_CODES} AS is_form_code,
               o.observation_date, o.events_24m, o.first_event_category,
               o.worst_severity_24m, o.distress_12m, o.distress_24m,
               o.default_12m, o.default_24m, o.bankruptcy_24m,
               o.debt_acceleration_24m, o.non_reliance_24m, o.late_filing_24m
        FROM marts.spreads_a s
        LEFT JOIN ref.sic_hierarchy h ON h.sic4 = s.sic
        LEFT JOIN ref.industry_group g ON g.sic4 = s.sic
        LEFT JOIN outcomes_one o
               ON o.cik = s.cik AND o.fy = s.fy AND o.period_end = s.period_end
        WHERE s.basis = '{BASIS}'""")

    spine_rows, source_rows = con.execute(f"""
        SELECT (SELECT count(*) FROM spine),
               (SELECT count(*) FROM marts.spreads_a WHERE basis = '{BASIS}')""").fetchone()
    print(f"  spine {spine_rows:,} rows against {source_rows:,} source rows")
    if spine_rows != source_rows:
        raise SystemExit(
            f"a join fanned out ({spine_rows:,} vs {source_rows:,}): every count in "
            "this workbook would be inflated. Fix the key before exporting.")
    return spine_rows


def main() -> None:
    OUT.mkdir(exist_ok=True)
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    con.execute("SET temp_directory = '/tmp/duckdb_spill'")
    con.execute("SET preserve_insertion_order = false")

    vcols = value_columns(con)
    print(f"spreads_a carries {len(vcols)} value columns")
    print("guarding join keys ...")
    guard_keys(con)
    print("building spine ...")
    spine_rows = build_spine(con)

    # Years populated per company per line item. One pass, read by two sheets.
    counted = ", ".join(f"count({c}) AS {c}" for c in vcols)
    populated = " + ".join(f"CASE WHEN {c} > 0 THEN 1 ELSE 0 END" for c in vcols)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE per_company AS
        SELECT cik, {counted} FROM marts.spreads_a
        WHERE basis = '{BASIS}' GROUP BY cik""")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE per_company_totals AS
        SELECT cik, ({populated}) AS line_items_ever_populated FROM per_company""")

    wb = xlsxwriter.Workbook(
        OUT / "credit_workbench_map.xlsx",
        {"constant_memory": True, "default_date_format": "yyyy-mm-dd"})
    FMT["header"] = wb.add_format({"bold": True, "bg_color": "#1F3864",
                                   "font_color": "white", "border": 1,
                                   "text_wrap": True, "valign": "vcenter"})
    FMT["title"] = wb.add_format({"bold": True, "font_size": 13})
    FMT["bold"] = wb.add_format({"bold": True})
    FMT["wrap"] = wb.add_format({"text_wrap": True, "valign": "top"})
    FMT["money"] = wb.add_format({"num_format": "#,##0"})

    counts: dict[str, int] = {}

    def dump(sheet, query, widths=None, money_from=None):
        try:
            counts[sheet] = stream_sheet(wb, con, sheet, query, widths, money_from)
        except Exception as exc:                     # one bad sheet must not lose the file
            print(f"  sheet {sheet:<20} FAILED: {str(exc)[:200]}")

    # ---------------------------------------------------------------- Companies
    dump("Companies", f"""
        SELECT s.cik,
               any_value(s.company_name) AS company_name,
               t.tickers, t.exchanges,
               d.entity_type, d.filer_category, d.state_of_incorporation,
               any_value(s.sic4) AS sic,
               any_value(s.sic4_description) AS sic_description,
               any_value(s.sic3) AS sic3, any_value(s.sic2) AS sic2,
               any_value(s.division_name) AS division,
               any_value(s.peer_group_code) AS peer_group_code,
               any_value(s.peer_group_label) AS peer_group_label,
               count(DISTINCT s.sic4) AS distinct_sic_codes,
               any_value(s.is_form_code) AS is_form_code,
               min(s.fy) AS first_fy, max(s.fy) AS last_fy,
               count(*) AS company_years,
               count(*) FILTER (WHERE s.observation_date IS NOT NULL) AS years_with_outcome,
               count(*) FILTER (WHERE s.default_24m) AS defaults_24m,
               count(*) FILTER (WHERE s.distress_24m) AS distress_24m,
               any_value(pt.line_items_ever_populated) AS line_items_ever_populated,
               round(100.0 * any_value(pt.line_items_ever_populated) / {len(vcols)}, 1)
                   AS pct_line_items
        FROM spine s
        LEFT JOIN per_company_totals pt ON pt.cik = s.cik
        LEFT JOIN ref.dim_company d ON d.cik = s.cik
        LEFT JOIN (SELECT cik, string_agg(DISTINCT ticker, ' ') AS tickers,
                          string_agg(DISTINCT exchange, ' ') AS exchanges
                   FROM ref.company_tickers GROUP BY cik) t ON t.cik = s.cik
        GROUP BY s.cik, t.tickers, t.exchanges, d.entity_type, d.filer_category,
                 d.state_of_incorporation
        ORDER BY company_name""",
         widths={"company_name": 46, "sic_description": 40, "peer_group_label": 34,
                 "division": 34, "filer_category": 30})

    # ---------------------------------------------------------------- Industry_map
    dump("Industry_map", """
        SELECT h.sic4, h.sic4_description, h.sic3, h.sic2,
               h.division_code, h.division_name,
               g.industry_code AS peer_group_code,
               g.industry_label AS peer_group_label,
               g.industry_level AS peer_group_level,
               g.peers AS companies_in_peer_group,
               g.custom_industry AS house_industry,
               coalesce(c.companies, 0) AS companies_here,
               coalesce(c.company_years, 0) AS company_years,
               coalesce(c.defaults_24m, 0) AS defaults_24m,
               c.default_rate_pct
        FROM ref.sic_hierarchy h
        LEFT JOIN ref.industry_group g ON g.sic4 = h.sic4
        LEFT JOIN (SELECT sic4, count(DISTINCT cik) AS companies,
                          count(*) AS company_years,
                          count(*) FILTER (WHERE default_24m) AS defaults_24m,
                          round(100.0 * count(*) FILTER (WHERE default_24m)
                                / nullif(count(*), 0), 2) AS default_rate_pct
                   FROM spine GROUP BY sic4) c ON c.sic4 = h.sic4
        ORDER BY h.sic4""",
         widths={"sic4_description": 46, "peer_group_label": 34, "division_name": 34})

    # ---------------------------------------------------------------- Line_items
    # Two aggregates over one pass each, unpivoted, rather than one scan per column.
    cols_list = ", ".join(vcols)
    years_sel = ", ".join(f"count({c}) AS {c}" for c in vcols)
    firms_sel = ", ".join(
        f"count(DISTINCT cik) FILTER (WHERE {c} IS NOT NULL) AS {c}" for c in vcols)
    order = " ".join(f"WHEN '{c}' THEN {i}" for i, c in enumerate(vcols))
    dump("Line_items", f"""
        WITH years AS (
            SELECT * FROM (SELECT {years_sel} FROM marts.spreads_a
                           WHERE basis = '{BASIS}')
            UNPIVOT (company_years_populated FOR column_name IN ({cols_list}))),
        firms AS (
            SELECT * FROM (SELECT {firms_sel} FROM marts.spreads_a
                           WHERE basis = '{BASIS}')
            UNPIVOT (companies_populated FOR column_name IN ({cols_list}))),
        tpl AS (
            SELECT line_code, any_value(line_no) AS line_no,
                   any_value(label) AS label, any_value(statement) AS statement,
                   string_agg(tag, ', ' ORDER BY priority) AS xbrl_tags
            FROM staging.tag_map GROUP BY line_code)
        SELECT CASE y.column_name {order} END AS column_order,
               y.column_name,
               coalesce(t.label, y.column_name) AS label,
               coalesce(t.statement, 'DERIVED') AS statement,
               t.line_no AS template_line_no,
               y.company_years_populated, f.companies_populated,
               round(100.0 * y.company_years_populated / {spine_rows}, 1) AS pct_populated,
               coalesce(t.xbrl_tags, '(computed by this platform)') AS xbrl_tags
        FROM years y
        JOIN firms f ON f.column_name = y.column_name
        LEFT JOIN tpl t ON t.line_code = y.column_name
        ORDER BY column_order""",
         widths={"label": 38, "xbrl_tags": 92, "column_name": 30})

    # ---------------------------------------------------------------- Coverage
    dump("Coverage_by_company", f"""
        SELECT p.cik, n.company_name, n.industry, n.years,
               {cols_list}
        FROM per_company p
        JOIN (SELECT cik, any_value(company_name) AS company_name,
                     any_value(sic4_description) AS industry, count(*) AS years
              FROM spine GROUP BY cik) n ON n.cik = p.cik
        ORDER BY n.company_name""",
         widths={"company_name": 46, "industry": 40})

    # ---------------------------------------------------------------- Company_years
    dump("Company_years", """
        SELECT cik, company_name, fy, period_end, last_filed AS filed,
               sic4 AS sic, sic4_description AS industry, sic2, division_name AS division,
               peer_group_code, peer_group_label, is_form_code,
               observation_date, events_24m, first_event_category, worst_severity_24m,
               distress_12m, distress_24m, default_12m, default_24m, bankruptcy_24m,
               debt_acceleration_24m, non_reliance_24m, late_filing_24m
        FROM spine ORDER BY company_name, fy""",
         widths={"company_name": 46, "industry": 40, "peer_group_label": 34,
                 "division": 34})

    # ---------------------------------------------------------------- Values
    dump("Values", f"""
        SELECT s.cik, s.company_name, s.fy, s.period_end, s.last_filed AS filed,
               s.sic4 AS sic, s.sic4_description AS industry,
               s.peer_group_label AS peer_group, a.ebit_source,
               {', '.join('a.' + c for c in vcols)}
        FROM spine s
        JOIN marts.spreads_a a
          ON a.cik = s.cik AND a.fy = s.fy AND a.period_end = s.period_end
         AND a.basis = '{BASIS}'
        ORDER BY s.company_name, s.fy""",
         widths={"company_name": 46, "industry": 40, "peer_group": 34},
         money_from=9)

    # ---------------------------------------------------------------- Ratios
    dump("Ratios", """
        SELECT ratio,
               count(*) AS values_held,
               count(DISTINCT cik) AS companies,
               min(fy) AS first_fy, max(fy) AS last_fy,
               round(quantile_cont(value, 0.25), 3) AS p25,
               round(quantile_cont(value, 0.50), 3) AS median,
               round(quantile_cont(value, 0.75), 3) AS p75
        FROM marts.ratio_values
        WHERE basis = 'first_reported' AND value IS NOT NULL AND isfinite(value)
        GROUP BY ratio ORDER BY ratio""")

    # ---------------------------------------------------------------- Inventory
    dump("Warehouse_tables", """
        SELECT table_schema AS schema, table_name,
               (SELECT count(*) FROM information_schema.columns c
                WHERE c.table_schema = t.table_schema
                  AND c.table_name = t.table_name) AS columns
        FROM information_schema.tables t
        WHERE table_schema NOT IN ('information_schema', 'pg_catalog', 'main')
        ORDER BY table_schema, table_name""",
         widths={"table_name": 40})

    sheet_readme(wb, counts)
    wb.close()

    xlsx = OUT / "credit_workbench_map.xlsx"
    print(f"\nwrote {xlsx}  {xlsx.stat().st_size / 1e6:.1f} MB")

    # The Values sheet is the one people will want in pandas rather than in Excel.
    csv = OUT / "values_first_reported.csv"
    con.execute(f"""
        COPY (SELECT s.cik, s.company_name, s.fy, s.period_end, s.sic4 AS sic,
                     s.sic4_description AS industry, s.peer_group_label AS peer_group,
                     {', '.join('a.' + c for c in vcols)}
              FROM spine s
              JOIN marts.spreads_a a
                ON a.cik = s.cik AND a.fy = s.fy AND a.period_end = s.period_end
               AND a.basis = '{BASIS}'
              ORDER BY s.company_name, s.fy)
        TO '{csv.as_posix()}' (HEADER, DELIMITER ',')""")
    print(f"wrote {csv}  {csv.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
