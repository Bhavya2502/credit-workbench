"""Industry x country x year revenue, EBITDA and margin, aggregated from the companies.

    uv run python -m credit_workbench.warehouse.export_industry_year

One row per industry, country and fiscal year, under four industry schemes stacked in one
sheet and told apart by `industry_scheme`. There is no single right level - SIC4 is
precise and thin, division is broad and always populated - so all four are emitted and
the reader picks.

Three things were measured before this was built, because each would have produced a
plausible wrong number rather than an error.

**Currency.** Summing revenue across companies is only valid in one currency.
`marts.spread_lines` records the unit, and annual revenue is 100% USD across all 97,861
first-reported lines - the IFRS filers who would report in EUR or JPY are the same ones
missing from the spread entirely. So the sums are safe, and they are USD.

**Country.** `business_country` is not a country field: it holds CA, NY and TX beside
"China". The pair explains it - `business_state` carries EDGAR's stateOrCountry *code*
and `business_country` its *description*, so the two are identical for a US state and
differ for a foreign one (code F4, description "China"). That resolves the dangerous
case: CA with CA beside it is California, and Canada appears as "Ontario, Canada". The
province prefix is stripped so all Canadian provinces roll into Canada.

**The margin's denominator.** 96.5% of revenue filers also carry EBITDA, which sounds
safe and is not: EBITDA is derived and exists for companies that never tagged revenue, so
the two populations differ in both directions. Summing them independently gives SIC 6021
banks a 1.0% margin from 44 revenue filers over 6 EBITDA filers, where the same companies
compared against themselves give 76.0%. Every figure here therefore comes from the
**matched** set - companies reporting both in that year - and `n_companies_revenue`,
`n_companies_ebitda` and `revenue_all_reporters` sit beside it so the gap is visible.
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

# (scheme name, code expression, name expression)
SCHEMES = [
    ("sic4", "b.sic4", "b.sic4_description"),
    ("sic2", "b.sic2", "b.sic2_label"),
    ("peer_group", "b.peer_group_code", "b.peer_group_label"),
    ("division", "b.division_code", "b.division_name"),
]

CAVEATS = [
    ("Grain",
     "One row per industry_scheme x industry_code x country x year. Four schemes are "
     "stacked in one sheet - filter industry_scheme first or you will count a company "
     "four times."),
    ("country = (all)",
     "Every industry-year also appears with country '(all)', which is the whole "
     "industry pooled. Do not add the country rows to the (all) row."),
    ("Matched population",
     "revenue, ebitda and ebitda_margin_pct all come from companies reporting BOTH in "
     "that year, so the three are internally consistent. n_companies is that matched "
     "count. revenue_all_reporters and n_companies_revenue show the fuller population "
     "the matched figure is drawn from."),
    ("Why matched matters",
     "EBITDA is derived and exists for companies that never tagged revenue, so summing "
     "the two independently mixes populations. SIC 6021 in FY2023: 1.0% margin "
     "unmatched, 76.0% matched. The unmatched number is the wrong one."),
    ("Currency",
     "USD. Annual revenue is 100% USD across all first-reported lines - the IFRS "
     "filers who report in other currencies are absent from the spread entirely. "
     "Figures are as filed and unscaled."),
    ("Country derivation",
     "business_state holds EDGAR's stateOrCountry code and business_country its "
     "description. Identical means a US state, so CA beside CA is California, not "
     "Canada. Canadian provinces ('Ontario, Canada') roll into Canada. Country names, "
     "not ISO-2 codes - the warehouse holds no ISO crosswalk."),
    ("Basis",
     f"{BASIS} - figures as first published, not restated, one row per fiscal year "
     "chosen by the spread builder's is_primary_annual flag."),
    ("Industry names",
     "sic4, peer_group and division names are real labels. The sic2 name is derived - "
     "the most common SIC4 description inside that major group - because SEC's major "
     "group titles are not held in this warehouse. It is a representative example, "
     "not an official name."),
    ("Coverage",
     "15,550 companies with a usable annual spread. 2,018 companies with XBRL facts "
     "have no spread - 1,504 never tagged an annual flow, and ~511 are IFRS 20-F "
     "filers the us-gaap template does not claim, so foreign totals understate."),
]

FMT: dict[str, object] = {}


def stream_sheet(wb, con, name, query, widths=None, money_cols=()):
    cur = con.execute(query)
    heads = [d[0] for d in cur.description]
    ws = wb.add_worksheet(name)
    ws.freeze_panes(1, 5)
    for i, h in enumerate(heads):
        w = (widths or {}).get(h, min(max(len(str(h)) + 2, 10), 44))
        ws.set_column(i, i, w, FMT["money"] if h in money_cols else None)
    ws.write_row(0, 0, heads, FMT["header"])
    n = 0
    while True:
        batch = cur.fetchmany(BATCH)
        if not batch:
            break
        for row in batch:
            n += 1
            ws.write_row(n, 0, row)
    if n:
        ws.autofilter(0, 0, n, len(heads) - 1)
    print(f"  sheet {name:<20} {n:>9,} rows x {len(heads):>3} cols")
    return n


def build_base(con) -> int:
    """One row per company-year, carrying every industry key and a resolved country."""
    con.execute("""
        CREATE OR REPLACE TEMP TABLE country AS
        SELECT cik,
               CASE
                 WHEN business_country IS NULL OR business_country = '' THEN 'Unknown'
                 -- code == description means the code is a US state, not a country
                 WHEN business_country = business_state THEN 'United States'
                 WHEN business_country = 'United States' THEN 'United States'
                 -- "Ontario, Canada" -> Canada; the province is not the country
                 ELSE trim(regexp_extract(business_country, '([^,]+)$', 1))
               END AS country
        FROM ref.dim_company""")

    # SEC publishes no major-group titles, so a sic2 label is borrowed from the most
    # common SIC4 description inside it. Representative, not official - said so on README.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE sic2_label AS
        SELECT sic2, mode(sic4_description) AS sic2_label
        FROM ref.sic_hierarchy WHERE sic4_description IS NOT NULL GROUP BY sic2""")

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE base AS
        SELECT s.cik, s.fy, s.revenue, s.ebitda,
               s.sic AS sic4, h.sic4_description, h.sic2,
               'SIC ' || h.sic2 || ' (e.g. ' || coalesce(l.sic2_label, 'unlabelled') || ')'
                   AS sic2_label,
               h.division_code, h.division_name,
               g.industry_code AS peer_group_code, g.industry_label AS peer_group_label,
               coalesce(c.country, 'Unknown') AS country
        FROM marts.spreads_a s
        LEFT JOIN ref.sic_hierarchy h ON h.sic4 = s.sic
        LEFT JOIN sic2_label l ON l.sic2 = h.sic2
        LEFT JOIN ref.industry_group g ON g.sic4 = s.sic
        LEFT JOIN country c ON c.cik = s.cik
        WHERE s.basis = '{BASIS}' AND s.is_primary_annual AND s.fy IS NOT NULL""")

    rows, src = con.execute(f"""
        SELECT (SELECT count(*) FROM base),
               (SELECT count(*) FROM marts.spreads_a
                WHERE basis = '{BASIS}' AND is_primary_annual AND fy IS NOT NULL)
        """).fetchone()
    print(f"  base {rows:,} company-years against {src:,} source rows")
    if rows != src:
        raise SystemExit(f"a lookup join fanned out ({rows:,} vs {src:,}); every total "
                         "in this sheet would be inflated.")
    return rows


def aggregate_sql() -> str:
    """One SELECT per scheme, each emitted per country and pooled across countries."""
    blocks = []
    for scheme, code, label in SCHEMES:
        for country in ("b.country", "'(all)'"):
            blocks.append(f"""
        SELECT '{scheme}' AS industry_scheme,
               {code} AS industry_code,
               {label} AS industry_name,
               {country} AS country,
               b.fy AS year,
               -- every figure below is the MATCHED population: both reported
               sum(CASE WHEN b.revenue IS NOT NULL AND b.ebitda IS NOT NULL
                        THEN b.revenue END) AS revenue,
               sum(CASE WHEN b.revenue IS NOT NULL AND b.ebitda IS NOT NULL
                        THEN b.ebitda END) AS ebitda,
               round(100.0 * sum(CASE WHEN b.revenue IS NOT NULL AND b.ebitda IS NOT NULL
                                      THEN b.ebitda END)
                     / nullif(sum(CASE WHEN b.revenue IS NOT NULL AND b.ebitda IS NOT NULL
                                       THEN b.revenue END), 0), 1) AS ebitda_margin_pct,
               count(DISTINCT b.cik) FILTER (WHERE b.revenue IS NOT NULL
                                               AND b.ebitda IS NOT NULL) AS n_companies,
               sum(b.revenue) AS revenue_all_reporters,
               count(DISTINCT b.cik) AS n_companies_any,
               count(DISTINCT b.cik) FILTER (WHERE b.revenue IS NOT NULL)
                   AS n_companies_revenue,
               count(DISTINCT b.cik) FILTER (WHERE b.ebitda IS NOT NULL)
                   AS n_companies_ebitda
        FROM base b
        WHERE {code} IS NOT NULL AND {code} <> ''
        GROUP BY 1, 2, 3, 4, 5""")
    return "\nUNION ALL".join(blocks)


def sheet_readme(wb, counts):
    ws = wb.add_worksheet("README")
    ws.set_column(0, 0, 24)
    ws.set_column(1, 1, 26)
    ws.set_column(2, 2, 100)
    r = 0
    ws.write(r, 0, "Credit Workbench - industry x country x year aggregates", FMT["title"])
    r += 2
    for k, v in (("Built", date.today().isoformat()),
                 ("Source", "MotherDuck credit_workbench (SEC XBRL)"),
                 ("Basis", BASIS), ("Currency", "USD")):
        ws.write(r, 0, k, FMT["bold"])
        ws.write(r, 1, v)
        r += 1
    r += 1
    ws.write(r, 0, "SHEETS", FMT["title"])
    r += 1
    for k, v in counts.items():
        ws.write(r, 0, k, FMT["bold"])
        ws.write_number(r, 1, v)
        r += 1
    r += 1
    ws.write(r, 0, "READ THIS BEFORE USING THE NUMBERS", FMT["title"])
    r += 1
    ws.write_row(r, 0, ["Topic", "", "Caveat"], FMT["header"])
    r += 1
    for topic, text in CAVEATS:
        ws.write(r, 0, topic, FMT["bold"])
        ws.write(r, 2, text, FMT["wrap"])
        ws.set_row(r, 46)
        r += 1


def main() -> None:
    OUT.mkdir(exist_ok=True)
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    con.execute("SET temp_directory = '/tmp/duckdb_spill'")
    print("building base ...")
    build_base(con)

    wb = xlsxwriter.Workbook(OUT / "credit_workbench_industry_year.xlsx",
                             {"constant_memory": True})
    FMT["header"] = wb.add_format({"bold": True, "bg_color": "#1F3864",
                                   "font_color": "white", "border": 1,
                                   "text_wrap": True, "valign": "vcenter"})
    FMT["title"] = wb.add_format({"bold": True, "font_size": 13})
    FMT["bold"] = wb.add_format({"bold": True})
    FMT["wrap"] = wb.add_format({"text_wrap": True, "valign": "top"})
    FMT["money"] = wb.add_format({"num_format": "#,##0"})

    counts: dict[str, int] = {}
    money = ("revenue", "ebitda", "revenue_all_reporters")
    widths = {"industry_name": 44, "country": 24, "industry_scheme": 15}

    counts["Industry_year"] = stream_sheet(
        wb, con, "Industry_year", f"""
        SELECT * FROM ({aggregate_sql()})
        ORDER BY industry_scheme, industry_code, country, year""",
        widths, money)

    counts["Industry_index"] = stream_sheet(
        wb, con, "Industry_index", f"""
        SELECT industry_scheme, industry_code, industry_name,
               count(DISTINCT year) AS years,
               min(year) AS first_year, max(year) AS last_year,
               max(n_companies_any) AS peak_companies
        FROM ({aggregate_sql()})
        WHERE country = '(all)'
        GROUP BY 1, 2, 3 ORDER BY 1, 2""",
        widths)

    counts["Country_index"] = stream_sheet(
        wb, con, "Country_index", """
        SELECT country, count(DISTINCT cik) AS companies,
               count(*) AS company_years,
               min(fy) AS first_year, max(fy) AS last_year
        FROM base GROUP BY country ORDER BY companies DESC""",
        widths)

    sheet_readme(wb, counts)
    wb.close()
    path = OUT / "credit_workbench_industry_year.xlsx"
    print(f"\nwrote {path}  {path.stat().st_size / 1e6:.1f} MB")

    csv = OUT / "industry_year.csv"
    con.execute(f"""
        COPY (SELECT * FROM ({aggregate_sql()})
              ORDER BY industry_scheme, industry_code, country, year)
        TO '{csv.as_posix()}' (HEADER, DELIMITER ',')""")
    print(f"wrote {csv}  {csv.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
