"""Non-financial operating-company panel: one row per company-fiscal-year, long format.

    uv run python -m credit_workbench.warehouse.export_panel

Built to a supplied specification, so the column list, order and types are fixed and no
convenience columns are added - a loader expecting 19 columns gets 19 columns. Where the
warehouse holds something better than what the spec asks for, that is reported in the
run output rather than substituted silently.

Decisions the spec left to us, and how each was taken:

**Basis is as-reported.** `first_reported` is the figure as originally published, not
restated. That is the point-in-time basis, and for downturn work it is the correct one:
a restated 2008 balance sheet is knowledge the market did not have in 2008.

**Duplicate periods are kept.** The spec says keep them and de-duplicate downstream, so
the primary-annual filter is NOT applied. A company that changed its year-end can carry
two period ends under one fiscal-year label; `fiscal_period_end_date` tells them apart.

**Fiscal year is labelled from the period, not the filing.** A period ending January
through May is labelled the previous year, so a January-2009 year-end reads as fiscal
2009 covering mostly calendar 2008 - which is the alignment the spec asks for. The exact
period end travels alongside so the caller can re-cut it.

**Depreciation prefers the cash flow statement**, falling back to the income statement,
as specified. Which source supplied it is reported in the run output.

**No screening on size, profitability, listing status or completeness.** The only
exclusions are structural: SIC 6000-6799, and trusts that are pass-through vehicles
rather than operating companies.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

from credit_workbench.common.config import motherduck_token

OUT = Path("export")
BASIS = "first_reported"

# Structural exclusions only. 6000-6799 covers banks, insurers, brokers, REITs, funds
# and blank checks (6770) in one range.
FINANCIAL = "TRY_CAST(s.sic AS INTEGER) BETWEEN 6000 AND 6799"
# Pass-through vehicles that file like companies but do not operate. Matched on name
# because their SIC is usually that of the underlying asset (1311 for an oil royalty).
TRUSTS = """(
    upper(s.company_name) LIKE '%ROYALTY TRUST%'
 OR upper(s.company_name) LIKE '%LIQUIDATING TRUST%'
 OR upper(s.company_name) LIKE '%LIQUIDATION TRUST%'
 OR upper(s.company_name) LIKE '%LIQUIDATING%TRUST%'
 OR upper(s.company_name) LIKE '%NET PROFITS INTEREST%'
 OR upper(s.company_name) LIKE '%ROYALTY PARTNERS%')"""

PANEL = f"""
WITH forms AS (
    SELECT cik, period_end, arg_max(form, n) AS form_type
    FROM (SELECT cik, period_end, form, count(*) AS n
          FROM marts.spread_lines
          WHERE basis = '{BASIS}' AND qtrs IN (0, 4) AND form IS NOT NULL
          GROUP BY cik, period_end, form)
    GROUP BY cik, period_end),
units AS (
    SELECT cik, period_end, arg_max(uom, n) AS currency
    FROM (SELECT cik, period_end, uom, count(*) AS n
          FROM marts.spread_lines
          WHERE basis = '{BASIS}' AND qtrs IN (0, 4) AND uom IS NOT NULL
            AND uom NOT IN ('shares', 'pure')
          GROUP BY cik, period_end, uom)
    GROUP BY cik, period_end)
SELECT lpad(CAST(s.cik AS VARCHAR), 10, '0')      AS cik,
       s.company_name                              AS company_name,
       s.fy                                        AS fiscal_year,
       s.period_end                                AS fiscal_period_end_date,
       f.form_type                                 AS form_type,
       s.sic                                       AS sic,
       CAST(NULL AS VARCHAR)                       AS gics_sub_industry,
       s.revenue                                   AS revenue,
       s.cost_of_sales                             AS cost_of_revenue,
       s.gross_profit                              AS gross_profit,
       s.operating_income                          AS operating_income,
       coalesce(s.dep_amort_cf, s.dep_amort_is)    AS depreciation_amortization,
       s.ebitda                                    AS ebitda,
       s.research_development                      AS rd_expense,
       s.sgna                                      AS sga_expense,
       s.total_assets                              AS total_assets,
       s.capex                                     AS capex,
       s.total_debt                                AS total_debt,
       u.currency                                  AS currency
FROM marts.spreads_a s
LEFT JOIN forms f ON f.cik = s.cik AND f.period_end = s.period_end
LEFT JOIN units u ON u.cik = s.cik AND u.period_end = s.period_end
WHERE s.basis = '{BASIS}'
  AND NOT ({FINANCIAL})
  AND NOT {TRUSTS}
ORDER BY s.company_name, s.fy, s.period_end
"""

REPORT = [
    ("Universe - what the structural exclusions removed", f"""
        SELECT count(*) AS all_first_reported_rows,
               count(*) FILTER (WHERE {FINANCIAL}) AS excluded_sic_6000_6799,
               count(*) FILTER (WHERE NOT ({FINANCIAL}) AND {TRUSTS})
                   AS excluded_trusts,
               count(*) FILTER (WHERE NOT ({FINANCIAL}) AND NOT {TRUSTS})
                   AS kept,
               count(*) FILTER (WHERE s.sic IS NULL OR s.sic = '')
                   AS kept_with_no_sic
        FROM marts.spreads_a s WHERE s.basis = '{BASIS}'"""),

    ("Q1 - does it include companies that stopped filing?", f"""
        WITH last_year AS (
            SELECT s.cik, max(s.fy) AS last_fy, min(s.fy) AS first_fy
            FROM marts.spreads_a s
            WHERE s.basis = '{BASIS}' AND NOT ({FINANCIAL}) AND NOT {TRUSTS}
            GROUP BY s.cik)
        SELECT count(*) AS companies,
               count(*) FILTER (WHERE last_fy <= 2012) AS stopped_by_2012,
               count(*) FILTER (WHERE last_fy BETWEEN 2013 AND 2019) AS stopped_2013_2019,
               count(*) FILTER (WHERE last_fy BETWEEN 2020 AND 2022) AS stopped_2020_2022,
               count(*) FILTER (WHERE last_fy >= 2023) AS still_filing
        FROM last_year"""),

    ("Q1b - dead companies with a recorded bankruptcy or default", f"""
        SELECT count(DISTINCT o.cik) AS companies_with_default_event,
               count(DISTINCT o.cik) FILTER (WHERE o.bankruptcy_24m)
                   AS with_bankruptcy_event
        FROM marts.credit_outcomes o
        JOIN (SELECT DISTINCT s.cik FROM marts.spreads_a s
              WHERE s.basis = '{BASIS}' AND NOT ({FINANCIAL}) AND NOT {TRUSTS}) k
          ON TRY_CAST(o.cik AS BIGINT) = k.cik
        WHERE o.default_24m"""),

    ("Q3 - coverage by fiscal year, and where the seam is", f"""
        SELECT s.fy AS fiscal_year, count(*) AS rows,
               count(DISTINCT s.cik) AS companies,
               round(100.0 * count(s.revenue) / count(*), 1) AS revenue_fill_pct,
               round(100.0 * count(s.total_assets) / count(*), 1) AS assets_fill_pct
        FROM marts.spreads_a s
        WHERE s.basis = '{BASIS}' AND NOT ({FINANCIAL}) AND NOT {TRUSTS}
          AND s.fy IS NOT NULL
        GROUP BY 1 ORDER BY 1"""),

    ("Duplicates - company-fiscal-years carrying more than one period end", f"""
        SELECT count(*) AS company_fiscal_years,
               count(*) FILTER (WHERE n > 1) AS with_more_than_one_row,
               sum(n) FILTER (WHERE n > 1) AS rows_involved
        FROM (SELECT s.cik, s.fy, count(*) AS n
              FROM marts.spreads_a s
              WHERE s.basis = '{BASIS}' AND NOT ({FINANCIAL}) AND NOT {TRUSTS}
              GROUP BY s.cik, s.fy)"""),

    ("form_type spread - 20-F and other foreign filers are included", f"""
        SELECT form_type, count(*) AS rows, count(DISTINCT cik) AS companies
        FROM ({PANEL}) GROUP BY form_type ORDER BY rows DESC LIMIT 12"""),

    ("currency - is everything USD?", f"""
        SELECT coalesce(currency, '(unknown)') AS currency, count(*) AS rows
        FROM ({PANEL}) GROUP BY 1 ORDER BY rows DESC LIMIT 8"""),

    ("depreciation_amortization - which statement supplied it", f"""
        SELECT count(*) AS rows,
               count(*) FILTER (WHERE s.dep_amort_cf IS NOT NULL) AS from_cash_flow,
               count(*) FILTER (WHERE s.dep_amort_cf IS NULL
                                  AND s.dep_amort_is IS NOT NULL) AS fell_back_to_income,
               count(*) FILTER (WHERE s.dep_amort_cf IS NULL
                                  AND s.dep_amort_is IS NULL) AS neither
        FROM marts.spreads_a s
        WHERE s.basis = '{BASIS}' AND NOT ({FINANCIAL}) AND NOT {TRUSTS}"""),

    ("Negative values are preserved, not absolute", f"""
        SELECT count(*) FILTER (WHERE s.operating_income < 0) AS negative_operating_income,
               count(*) FILTER (WHERE s.revenue < 0) AS negative_revenue,
               count(*) FILTER (WHERE s.total_debt < 0) AS negative_total_debt
        FROM marts.spreads_a s
        WHERE s.basis = '{BASIS}' AND NOT ({FINANCIAL}) AND NOT {TRUSTS}"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)))[:52]
             for v in r] for r in cur.fetchall()]
    if not rows:
        print("  (no rows)")
        return
    w = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(heads)]
    print("  " + "  ".join(h.ljust(x) for h, x in zip(heads, w)))
    print("  " + "  ".join("-" * x for x in w))
    for r in rows:
        print("  " + "  ".join(v.ljust(x) for v, x in zip(r, w)))


def main() -> None:
    OUT.mkdir(exist_ok=True)
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    con.execute("SET temp_directory = '/tmp/duckdb_spill'")

    n = con.execute(f"SELECT count(*) FROM ({PANEL})").fetchone()[0]
    src = con.execute(f"""
        SELECT count(*) FROM marts.spreads_a s WHERE s.basis = '{BASIS}'
          AND NOT ({FINANCIAL}) AND NOT {TRUSTS}""").fetchone()[0]
    print(f"panel rows {n:,} against {src:,} source rows")
    if n != src:
        raise SystemExit(f"the form or currency join fanned out ({n:,} vs {src:,}).")

    path = OUT / "nonfinancial_panel.csv"
    con.execute(f"COPY ({PANEL}) TO '{path.as_posix()}' (HEADER, DELIMITER ',')")
    print(f"wrote {path}  {path.stat().st_size / 1e6:.1f} MB\n")

    cols = [d[0] for d in con.execute(f"SELECT * FROM ({PANEL}) LIMIT 0").description]
    sel = ", ".join(f"count({c}) AS {c}" for c in cols)
    got = con.execute(f"SELECT {sel} FROM ({PANEL})").fetchone()
    print("### Column fill")
    print(f"  {'#':>3}  {'column':<28}{'filled':>10}{'pct':>8}   spec")
    spec = {"cik": "Yes", "company_name": "Yes", "fiscal_year": "Yes",
            "fiscal_period_end_date": "Yes", "form_type": "Yes", "sic": "Yes",
            "gics_sub_industry": "if available", "revenue": "Yes",
            "cost_of_revenue": "if available", "gross_profit": "if available",
            "operating_income": "Yes", "depreciation_amortization": "Yes",
            "ebitda": "if available", "rd_expense": "if available",
            "sga_expense": "if available", "total_assets": "Yes", "capex": "Yes",
            "total_debt": "if available", "currency": "if not all USD"}
    for i, (c, v) in enumerate(zip(cols, got), 1):
        print(f"  {i:>3}  {c:<28}{v:>10,}{100.0 * v / n:>7.1f}%   {spec.get(c, '')}")

    for title, q in REPORT:
        print(f"\n### {title}")
        try:
            show(con, q)
        except Exception as exc:
            print(f"  (failed: {str(exc)[:200]})")


if __name__ == "__main__":
    main()
