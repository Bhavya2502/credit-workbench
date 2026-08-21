"""Can industry revenue and EBITDA be summed? Currency and country, measured first.

An industry-year aggregate adds one company's revenue to another's. Two things make that
arithmetic wrong rather than merely imprecise, and neither is visible in the output:

**Currency.** `marts.spreads_a` carries no unit column - it is "as filed, unscaled". If
some filers report in JPY or EUR, summing them with USD filers produces a number that
looks plausible and means nothing: one yen-reporting telecom would swamp an entire
industry. `marts.spread_lines` does carry `uom`, so the currency mix is measurable there.

**Country.** The requested format has a country column. This warehouse is SEC filers, so
the question is not "which country" but "do we hold one at all, and for how many". A
column filled for 40% of companies is worse than no column.

Also sizes the output: an industry-year grid is only useful if its cells hold enough
companies to mean something.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

Q = [
    ("1. CURRENCY - what units does annual revenue actually arrive in?", """
        SELECT uom, count(*) AS values_held, count(DISTINCT cik) AS companies
        FROM marts.spread_lines
        WHERE basis = 'first_reported' AND qtrs = 4 AND line_code = 'revenue'
          AND value IS NOT NULL
        GROUP BY uom ORDER BY companies DESC LIMIT 12"""),

    ("2. CURRENCY - companies reporting in anything other than USD, named", """
        SELECT l.uom, any_value(l.company_name) AS example_company,
               count(DISTINCT l.cik) AS companies, max(l.value) AS largest_revenue
        FROM marts.spread_lines l
        WHERE l.basis = 'first_reported' AND l.qtrs = 4 AND l.line_code = 'revenue'
          AND l.value IS NOT NULL AND l.uom <> 'USD'
        GROUP BY l.uom ORDER BY companies DESC LIMIT 15"""),

    ("3. CURRENCY - does a company ever switch unit between years?", """
        SELECT units_per_company, count(*) AS companies FROM (
            SELECT cik, count(DISTINCT uom) AS units_per_company
            FROM marts.spread_lines
            WHERE basis = 'first_reported' AND qtrs = 4 AND line_code = 'revenue'
              AND value IS NOT NULL
            GROUP BY cik)
        GROUP BY 1 ORDER BY 1"""),

    ("4. COUNTRY - what does ref.dim_company hold for our companies?", """
        SELECT count(*) AS companies,
               count(d.business_country) AS with_business_country,
               count(d.state_of_incorporation) AS with_state_of_inc,
               count(d.business_state) AS with_business_state
        FROM (SELECT DISTINCT cik FROM marts.spreads_a) s
        LEFT JOIN ref.dim_company d ON d.cik = s.cik"""),

    ("5. COUNTRY - the actual distribution", """
        SELECT coalesce(d.business_country, '(blank)') AS business_country,
               count(*) AS companies
        FROM (SELECT DISTINCT cik FROM marts.spreads_a) s
        LEFT JOIN ref.dim_company d ON d.cik = s.cik
        GROUP BY 1 ORDER BY companies DESC LIMIT 15"""),

    ("6. SIZE - how big would the industry-year grid be, per scheme?", """
        SELECT 'sic2' AS scheme, count(*) AS cells FROM (
            SELECT DISTINCT sic2, fy FROM marts.spreads_a a
            JOIN ref.sic_hierarchy h ON h.sic4 = a.sic WHERE a.is_primary_annual)
        UNION ALL
        SELECT 'peer_group', count(*) FROM (
            SELECT DISTINCT g.industry_code, a.fy FROM marts.spreads_a a
            JOIN ref.industry_group g ON g.sic4 = a.sic WHERE a.is_primary_annual)
        UNION ALL
        SELECT 'sic4', count(*) FROM (
            SELECT DISTINCT sic, fy FROM marts.spreads_a WHERE is_primary_annual)
        UNION ALL
        SELECT 'division', count(*) FROM (
            SELECT DISTINCT h.division_code, a.fy FROM marts.spreads_a a
            JOIN ref.sic_hierarchy h ON h.sic4 = a.sic WHERE a.is_primary_annual)"""),

    ("7. COVERAGE - how many companies in a cell actually report revenue AND ebitda?", """
        SELECT fy,
               count(DISTINCT cik) AS companies,
               count(DISTINCT cik) FILTER (WHERE revenue IS NOT NULL) AS with_revenue,
               count(DISTINCT cik) FILTER (WHERE ebitda IS NOT NULL) AS with_ebitda,
               count(DISTINCT cik) FILTER (WHERE revenue IS NOT NULL
                                             AND ebitda IS NOT NULL) AS with_both
        FROM marts.spreads_a
        WHERE basis = 'first_reported' AND is_primary_annual AND fy BETWEEN 2015 AND 2025
        GROUP BY fy ORDER BY fy"""),

    ("8. SANITY - would non-USD filers distort a sum? Largest revenues by unit", """
        SELECT l.uom, count(DISTINCT l.cik) AS companies, sum(l.value) AS total_revenue
        FROM marts.spread_lines l
        WHERE l.basis = 'first_reported' AND l.qtrs = 4 AND l.line_code = 'revenue'
          AND l.value IS NOT NULL AND l.fy = 2024
        GROUP BY l.uom ORDER BY total_revenue DESC LIMIT 8"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else
                                    (f"{v:,.0f}" if isinstance(v, float) else str(v))))[:56]
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
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    con.execute("SET temp_directory = '/tmp/duckdb_spill'")
    for title, q in Q:
        print(f"\n### {title}")
        try:
            show(con, q)
        except Exception as exc:
            print(f"  (failed: {str(exc)[:200]})")


if __name__ == "__main__":
    main()
