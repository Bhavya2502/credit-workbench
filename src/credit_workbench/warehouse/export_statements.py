"""Financial statements for every company, arranged the way an analyst reads them.

    uv run python -m credit_workbench.warehouse.export_statements

Line items down the rows in statement order, fiscal years across the columns, one block
per company. This is the transpose of the Values sheet in `export_workbook`: same
figures, laid out as a spread rather than as a database table.

  Income_statement    36 line items x every company
  Balance_sheet       42
  Cash_flow           20
  Memo_and_derived    20  - nine memo lines the filer tags, eleven this platform derives
  Companies           the index: who is in the file, with industry names
  Line_items          the template in order, with its XBRL tags and fill rate
  README              grain and caveats

Four decisions worth stating, because each one changes what a reader sees.

**Every company gets every line of the template**, including lines it never reported.
Blocks are then identical across companies, so a row means the same thing everywhere and
a bank's missing inventory line is visible as missing. `years_populated` on each row says
how many years actually carry a figure - filter on it to collapse to what exists.

**Split by statement, not by company.** 118 line items x 15,550 companies is 1.83m rows,
past Excel's 1,048,576 cap for one sheet. Split by statement the largest sheet is the
balance sheet at 653,100 rows, which fits with room to spare.

**One row per fiscal year.** `marts.spreads_a` is unique on `(cik, fy, period_end)`, not
on `(cik, fy)` - a company that changed its year-end can carry two period ends inside one
fiscal-year label. Only the latest period end per fiscal year is kept, and the number
dropped is printed, because a year column can hold one figure and picking silently is how
a spread ends up mixing two period lengths in one column.

**`is_empty_spread` and `is_primary_annual` are excluded.** They are boolean row flags,
not line items, and they sat in the previous workbook's Values sheet as though they were
figures. They belong to the company-year, not to the statement.
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

# Identify the row rather than carry a figure.
KEY_COLS = ("cik", "company_name", "sic", "basis", "period_end", "fy", "last_filed",
            "ebit_source")

# Boolean flags of the company-year that are not line items of any statement.
NOT_LINE_ITEMS = ("is_empty_spread", "is_primary_annual")

# Columns this platform computes rather than lifting from a filer's tag. staging.tag_map
# does not describe them, so they carry their own labels and sort after the template.
DERIVED = {
    "gross_profit_calc": "Gross profit (derived)",
    "ebit_calc": "EBIT (derived)",
    "ebitda": "EBITDA (derived)",
    "total_debt": "Total debt (derived)",
    "total_debt_incl_leases": "Total debt including leases (derived)",
    "net_debt": "Net debt (derived)",
    "working_capital": "Working capital (derived)",
    "capital_employed": "Capital employed (derived)",
    "tangible_net_worth": "Tangible net worth (derived)",
    "free_cash_flow": "Free cash flow (derived)",
    "ffo_simplified": "Funds from operations, simplified (derived)",
}

# Per-share lines need decimals. Under the money format an EPS of 6.08 renders as "6",
# which is wrong on the face of a statement even though the stored value is intact.
PER_SHARE = ("eps_basic", "eps_diluted", "dividends_declared_ps")

SHEETS = [
    ("Income_statement", "IS"),
    ("Balance_sheet", "BS"),
    ("Cash_flow", "CF"),
    ("Memo_and_derived", "MEMO"),
]

CAVEATS = [
    ("Layout",
     "Line items down the rows in statement order, fiscal years across the columns, "
     "one block per company sorted by company name. Every company carries every line "
     "of the template, so rows mean the same thing in every block."),
    ("Empty rows",
     "years_populated says how many years that line actually holds a figure. 0 means "
     "the company never reported it - filter years_populated > 0 to see only what "
     "exists."),
    ("Blank cells",
     "A blank is 'the filer did not tag this concept in that year', never zero."),
    ("Basis",
     f"Values are {BASIS} - the figures as first published, not restated. The "
     "warehouse also holds a 'latest' basis with restatements folded in; this file "
     "does not mix them."),
    ("One row per fiscal year",
     "Where a company reported two period ends under one fiscal-year label, the "
     "latest period end is kept. A year column can only hold one figure."),
    ("Currency and scale",
     "As filed and unscaled, in the filer's own reporting currency. Foreign private "
     "issuers filing in EUR or JPY are not converted."),
    ("Derived lines",
     "The eleven lines marked (derived) on the Memo_and_derived sheet are computed by "
     "this platform - EBITDA, total debt, net debt and so on - not tagged by the "
     "filer. Everything else comes from the filer's own XBRL tag."),
    ("Universe",
     "15,550 companies with a usable annual spread, of roughly 18,200 with any XBRL "
     "fact. The excluded ones are mostly pre-revenue or non-operating filers."),
    ("Fiscal years",
     "fy is the filer's own label. FY2026-FY2028 hold 21 company-years between them "
     "and are the filers' forward labelling, not our error."),
]

FMT: dict[str, object] = {}


def line_items(con) -> list[tuple[str, str, int, str]]:
    """(line_code, statement, line_no, label) for every line item, in reading order."""
    cols = [r[0] for r in con.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'marts' AND table_name = 'spreads_a'
        ORDER BY ordinal_position""").fetchall()]
    cols = [c for c in cols if c not in KEY_COLS and c not in NOT_LINE_ITEMS]

    tpl = {r[0]: (r[1], r[2], r[3]) for r in con.execute("""
        SELECT line_code, any_value(statement), any_value(line_no), any_value(label)
        FROM staging.tag_map GROUP BY line_code""").fetchall()}

    out = []
    for c in cols:
        if c in tpl:
            stmt, line_no, label = tpl[c]
            out.append((c, stmt, line_no, label or c))
        elif c in DERIVED:
            out.append((c, "MEMO", 900 + list(DERIVED).index(c), DERIVED[c]))
        else:                                    # a column the template forgot to describe
            out.append((c, "MEMO", 990, c))
    return sorted(out, key=lambda r: (r[1], r[2]))


def stream_sheet(wb, con, name, query, widths=None, money_from=None):
    cur = con.execute(query)
    heads = [d[0] for d in cur.description]
    ws = wb.add_worksheet(name)
    ws.freeze_panes(1, 5)                        # keys stay put, years scroll
    for i, h in enumerate(heads):
        w = (widths or {}).get(h, min(max(len(str(h)) + 2, 10), 44))
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
            per_share = (money_from is not None and len(row) > 5
                         and row[5] in PER_SHARE)
            if per_share:                        # override the integer money format
                ws.write_row(n, 0, row[:money_from])
                for j, v in enumerate(row[money_from:]):
                    ws.write(n, money_from + j, v, FMT["per_share"])
            else:
                ws.write_row(n, 0, row)
    if n:
        ws.autofilter(0, 0, n, len(heads) - 1)
    print(f"  sheet {name:<20} {n:>9,} rows x {len(heads):>3} cols")
    return n


def sheet_readme(wb, counts, years):
    ws = wb.add_worksheet("README")
    ws.set_column(0, 0, 26)
    ws.set_column(1, 1, 30)
    ws.set_column(2, 2, 98)
    r = 0
    ws.write(r, 0, "Credit Workbench - financial statements by company and year",
             FMT["title"])
    r += 2
    for label, value in (("Built", date.today().isoformat()),
                         ("Source", "MotherDuck credit_workbench (SEC XBRL)"),
                         ("Basis", BASIS),
                         ("Years", f"FY{min(years)} to FY{max(years)}"),
                         ("Layout", "line items = rows, fiscal years = columns")):
        ws.write(r, 0, label, FMT["bold"])
        ws.write(r, 1, value)
        r += 1
    r += 1

    ws.write(r, 0, "SHEETS", FMT["title"])
    r += 1
    ws.write_row(r, 0, ["Sheet", "Rows", "What it is"], FMT["header"])
    r += 1
    descr = {
        "Income_statement": "Revenue through to net income and EPS, per company per year.",
        "Balance_sheet": "Assets, liabilities and equity, per company per year.",
        "Cash_flow": "Operating, investing and financing flows, per company per year.",
        "Memo_and_derived": "Memo lines the filer tags, plus the eleven this platform "
                            "derives (EBITDA, total debt, net debt, FCF and so on).",
        "Companies": "The index: every company with tickers, SIC, industry names, "
                     "peer group and the span of years held.",
        "Line_items": "The template in statement order, with the XBRL tag alternatives "
                      "behind each line and how often it is populated.",
    }
    for nm, note in descr.items():
        ws.write(r, 0, nm, FMT["bold"])
        ws.write_number(r, 1, counts.get(nm, 0))
        ws.write(r, 2, note, FMT["wrap"])
        ws.set_row(r, 28)
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


def main() -> None:
    OUT.mkdir(exist_ok=True)
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    con.execute("SET temp_directory = '/tmp/duckdb_spill'")
    con.execute("SET preserve_insertion_order = false")

    items = line_items(con)
    codes = [i[0] for i in items]
    print(f"{len(items)} line items across "
          f"{len(set(i[1] for i in items))} statements")

    # A year column holds one figure, so a fiscal year must resolve to one period end.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE annual AS
        SELECT * FROM marts.spreads_a
        WHERE basis = '{BASIS}'
        QUALIFY row_number() OVER (PARTITION BY cik, fy ORDER BY period_end DESC) = 1""")
    kept, total = con.execute(f"""
        SELECT (SELECT count(*) FROM annual),
               (SELECT count(*) FROM marts.spreads_a WHERE basis = '{BASIS}')""").fetchone()
    print(f"  kept {kept:,} of {total:,} rows - {total - kept:,} extra period ends "
          f"inside a fiscal-year label dropped")

    years = [r[0] for r in con.execute(
        "SELECT DISTINCT fy FROM annual WHERE fy IS NOT NULL ORDER BY fy").fetchall()]
    print(f"  years FY{min(years)}-FY{max(years)} ({len(years)} columns)")

    # Long form, NULLs excluded by UNPIVOT, then one row per (company, line item) with
    # the years spread across columns.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE long AS
        SELECT * FROM (SELECT cik, fy, {', '.join(codes)} FROM annual)
        UNPIVOT (value FOR line_code IN ({', '.join(codes)}))""")
    year_cols = ", ".join(
        f'max(CASE WHEN fy = {y} THEN value END) AS "FY{y}"' for y in years)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE pivoted AS
        SELECT cik, line_code, count(value) AS years_populated, {year_cols}
        FROM long GROUP BY cik, line_code""")
    n_long, n_piv = con.execute(
        "SELECT (SELECT count(*) FROM long), (SELECT count(*) FROM pivoted)").fetchone()
    print(f"  {n_long:,} values -> {n_piv:,} company/line-item rows")

    # The template as a table, so the cross join can build identical blocks.
    con.execute("CREATE OR REPLACE TEMP TABLE template (line_code VARCHAR, "
                "statement VARCHAR, line_no INTEGER, line_item VARCHAR)")
    con.executemany("INSERT INTO template VALUES (?, ?, ?, ?)", items)

    con.execute("""
        CREATE OR REPLACE TEMP TABLE co AS
        SELECT a.cik, any_value(a.company_name) AS company_name, any_value(a.sic) AS sic,
               any_value(h.sic4_description) AS industry,
               any_value(g.industry_label) AS peer_group,
               min(a.fy) AS first_fy, max(a.fy) AS last_fy, count(*) AS years_held
        FROM annual a
        LEFT JOIN ref.sic_hierarchy h ON h.sic4 = a.sic
        LEFT JOIN ref.industry_group g ON g.sic4 = a.sic
        GROUP BY a.cik""")

    wb = xlsxwriter.Workbook(OUT / "credit_workbench_statements.xlsx",
                             {"constant_memory": True})
    FMT["header"] = wb.add_format({"bold": True, "bg_color": "#1F3864",
                                   "font_color": "white", "border": 1,
                                   "text_wrap": True, "valign": "vcenter"})
    FMT["title"] = wb.add_format({"bold": True, "font_size": 13})
    FMT["bold"] = wb.add_format({"bold": True})
    FMT["wrap"] = wb.add_format({"text_wrap": True, "valign": "top"})
    FMT["money"] = wb.add_format({"num_format": "#,##0"})
    FMT["per_share"] = wb.add_format({"num_format": "#,##0.00"})

    counts: dict[str, int] = {}
    year_sel = ", ".join(f'p."FY{y}"' for y in years)
    widths = {"company_name": 44, "line_item": 40, "industry": 38}

    for sheet, stmt in SHEETS:
        counts[sheet] = stream_sheet(wb, con, sheet, f"""
            SELECT c.cik, c.company_name, c.sic, c.industry,
                   t.line_no, t.line_code, t.line_item,
                   coalesce(p.years_populated, 0) AS years_populated,
                   {year_sel}
            FROM co c
            CROSS JOIN (SELECT * FROM template WHERE statement = '{stmt}') t
            LEFT JOIN pivoted p ON p.cik = c.cik AND p.line_code = t.line_code
            ORDER BY c.company_name, t.line_no""",
            widths, money_from=8)

    counts["Companies"] = stream_sheet(wb, con, "Companies", """
        SELECT c.cik, c.company_name, t.tickers, c.sic, c.industry, c.peer_group,
               d.entity_type, d.filer_category,
               c.first_fy, c.last_fy, c.years_held
        FROM co c
        LEFT JOIN ref.dim_company d ON d.cik = c.cik
        LEFT JOIN (SELECT cik, string_agg(DISTINCT ticker, ' ') AS tickers
                   FROM ref.company_tickers GROUP BY cik) t ON t.cik = c.cik
        ORDER BY c.company_name""", widths)

    counts["Line_items"] = stream_sheet(wb, con, "Line_items", f"""
        WITH tags AS (
            SELECT line_code, string_agg(tag, ', ' ORDER BY priority) AS xbrl_tags
            FROM staging.tag_map GROUP BY line_code),
        fill AS (
            SELECT line_code, count(*) AS company_years_populated,
                   count(DISTINCT cik) AS companies_populated
            FROM long GROUP BY line_code)
        SELECT t.statement, t.line_no, t.line_code, t.line_item,
               f.company_years_populated, f.companies_populated,
               round(100.0 * f.company_years_populated / {kept}, 1) AS pct_populated,
               coalesce(g.xbrl_tags, '(computed by this platform)') AS xbrl_tags
        FROM template t
        LEFT JOIN fill f ON f.line_code = t.line_code
        LEFT JOIN tags g ON g.line_code = t.line_code
        ORDER BY t.statement, t.line_no""",
        {"line_item": 40, "xbrl_tags": 92})

    sheet_readme(wb, counts, years)
    wb.close()

    xlsx = OUT / "credit_workbench_statements.xlsx"
    print(f"\nwrote {xlsx}  {xlsx.stat().st_size / 1e6:.1f} MB")

    # Long form for anyone who would rather pivot it themselves. Gzipped: the plain
    # CSV is 971 MB of mostly repeated company and label text.
    csv = OUT / "statements_long.csv.gz"
    con.execute(f"""
        COPY (SELECT c.cik, c.company_name, c.sic, c.industry,
                     t.statement, t.line_no, t.line_code, t.line_item, l.fy, l.value
              FROM long l
              JOIN template t ON t.line_code = l.line_code
              JOIN co c ON c.cik = l.cik
              ORDER BY c.company_name, t.statement, t.line_no, l.fy)
        TO '{csv.as_posix()}' (HEADER, DELIMITER ',', COMPRESSION GZIP)""")
    print(f"wrote {csv}  {csv.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
