"""Can an industry x year revenue and EBITDA aggregate be built honestly?

The requested shape - industry_code, industry_name, country, year, revenue, ebitda,
ebitda_margin_pct, n_companies - is three assumptions wearing a table, and each one
silently produces a plausible wrong number if it does not hold.

**Country.** `marts.spreads_a` has no country. `ref.dim_company` carries
`business_country` and `state_of_incorporation`, and SEC commonly leaves the country
blank for domestic filers. If it is blank for most, "country" has to be derived, and how
it is derived has to be stated.

**Currency.** `spreads_a` carries no unit either - the unit lives on
`marts.spread_lines.uom`. Summing revenue across an industry mixes USD, EUR and JPY into
one number that looks like money and is not. This measures how much of the population is
non-USD.

**Denominator mismatch.** A margin is only meaningful if the numerator and denominator
come from the same companies. If 28 companies report revenue and 12 report EBITDA, then
sum(ebitda)/sum(revenue) is not that industry's margin - it is a ratio of two different
populations. This measures the overlap.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

Q = [
    ("1. COUNTRY - what does dim_company actually carry?", """
        SELECT count(*) AS companies,
               count(*) FILTER (WHERE business_country IS NOT NULL
                                  AND business_country <> '') AS with_business_country,
               count(*) FILTER (WHERE state_of_incorporation IS NOT NULL
                                  AND state_of_incorporation <> '') AS with_state_incorp,
               count(DISTINCT business_country) AS distinct_countries
        FROM ref.dim_company
        WHERE cik IN (SELECT DISTINCT cik FROM marts.spreads_a)"""),

    ("2. COUNTRY - the actual values, for companies we hold financials for", """
        SELECT coalesce(nullif(d.business_country, ''), '(blank)') AS business_country,
               count(DISTINCT d.cik) AS companies
        FROM ref.dim_company d
        WHERE d.cik IN (SELECT DISTINCT cik FROM marts.spreads_a)
        GROUP BY 1 ORDER BY companies DESC LIMIT 20"""),

    ("3. CURRENCY - what unit is revenue actually reported in?", """
        SELECT uom, count(*) AS lines, count(DISTINCT cik) AS companies
        FROM marts.spread_lines
        WHERE basis = 'first_reported' AND line_code = 'revenue' AND qtrs = 4
          AND value IS NOT NULL
        GROUP BY 1 ORDER BY lines DESC LIMIT 12"""),

    ("4. DENOMINATOR - do revenue and EBITDA cover the same companies?", """
        SELECT count(*) AS company_years,
               count(*) FILTER (WHERE revenue IS NOT NULL) AS with_revenue,
               count(*) FILTER (WHERE ebitda IS NOT NULL) AS with_ebitda,
               count(*) FILTER (WHERE revenue IS NOT NULL AND ebitda IS NOT NULL)
                   AS with_both,
               round(100.0 * count(*) FILTER (WHERE revenue IS NOT NULL
                                                AND ebitda IS NOT NULL)
                     / nullif(count(*) FILTER (WHERE revenue IS NOT NULL), 0), 1)
                   AS pct_of_revenue_filers_with_ebitda
        FROM marts.spreads_a
        WHERE basis = 'first_reported' AND is_primary_annual"""),

    ("5. How much would the mismatch move a margin? One industry-year, both ways", """
        WITH base AS (
            SELECT s.sic, s.fy, s.cik, s.revenue, s.ebitda
            FROM marts.spreads_a s
            WHERE s.basis = 'first_reported' AND s.is_primary_annual AND s.fy = 2023)
        SELECT sic,
               count(*) FILTER (WHERE revenue IS NOT NULL) AS n_rev,
               count(*) FILTER (WHERE ebitda IS NOT NULL) AS n_ebitda,
               round(100.0 * sum(ebitda) / nullif(sum(revenue), 0), 1) AS margin_unmatched,
               round(100.0 * sum(CASE WHEN revenue IS NOT NULL AND ebitda IS NOT NULL
                                      THEN ebitda END)
                     / nullif(sum(CASE WHEN revenue IS NOT NULL AND ebitda IS NOT NULL
                                       THEN revenue END), 0), 1) AS margin_matched
        FROM base GROUP BY sic
        HAVING count(*) FILTER (WHERE revenue IS NOT NULL) >= 25
        ORDER BY abs(coalesce(round(100.0 * sum(ebitda) / nullif(sum(revenue), 0), 1), 0)
                   - coalesce(round(100.0 * sum(CASE WHEN revenue IS NOT NULL
                                                      AND ebitda IS NOT NULL
                                                 THEN ebitda END)
                              / nullif(sum(CASE WHEN revenue IS NOT NULL
                                                 AND ebitda IS NOT NULL
                                            THEN revenue END), 0), 1), 0)) DESC
        LIMIT 10"""),

    ("6. SIZE - how many rows would the sheet have, per scheme?", """
        SELECT 'sic4' AS scheme, count(DISTINCT (sic, fy)) AS cells
        FROM marts.spreads_a WHERE basis = 'first_reported' AND is_primary_annual
        UNION ALL
        SELECT 'sic2', count(DISTINCT (substr(sic, 1, 2), fy))
        FROM marts.spreads_a WHERE basis = 'first_reported' AND is_primary_annual
        UNION ALL
        SELECT 'peer_group', count(DISTINCT (g.industry_code, s.fy))
        FROM marts.spreads_a s JOIN ref.industry_group g ON g.sic4 = s.sic
        WHERE s.basis = 'first_reported' AND s.is_primary_annual
        UNION ALL
        SELECT 'division', count(DISTINCT (h.division_code, s.fy))
        FROM marts.spreads_a s JOIN ref.sic_hierarchy h ON h.sic4 = s.sic
        WHERE s.basis = 'first_reported' AND s.is_primary_annual"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)))[:58]
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
