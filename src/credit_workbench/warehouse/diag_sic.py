"""What industry information does the warehouse already hold?

A crosswalk is only worth building on top of what is actually populated. SIC arrives on
every filing and `ref.dim_company` carries a description, so the questions are how many
distinct codes are in use, how complete the descriptions are, whether the code is stable
for a company over time, and how many companies sit in each bucket - a grouping with
fifty companies in one bucket and three in another will not support peer comparison
however elegant its taxonomy.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

Q = [
    ("1. How much SIC do we have, and is it described?", """
        SELECT count(*) AS companies,
               count(sic) AS with_sic,
               count(sic_description) AS with_description,
               count(DISTINCT sic) AS distinct_codes
        FROM ref.dim_company"""),

    ("2. Same, for companies that actually have financials", """
        SELECT count(DISTINCT r.cik) AS companies_with_ratios,
               count(DISTINCT r.sic) AS distinct_codes,
               count(DISTINCT r.sic2) AS distinct_major_groups
        FROM marts.ratio_values r WHERE r.fy >= 2020"""),

    ("3. Is a company's SIC stable over time?", """
        SELECT codes_per_company, count(*) AS companies
        FROM (SELECT cik, count(DISTINCT sic) AS codes_per_company
              FROM marts.ratio_values WHERE fy >= 2015 GROUP BY cik)
        GROUP BY 1 ORDER BY 1 LIMIT 6"""),

    ("4. Do the two-digit major groups give usable peer sizes?", """
        SELECT count(*) AS major_groups,
               count(*) FILTER (WHERE companies >= 30) AS groups_over_30,
               count(*) FILTER (WHERE companies < 10) AS groups_under_10,
               round(median(companies), 0) AS median_companies
        FROM (SELECT sic2, count(DISTINCT cik) AS companies
              FROM marts.ratio_values WHERE fy = 2024 GROUP BY sic2)"""),

    ("5. And the four-digit codes?", """
        SELECT count(*) AS sic4_codes,
               count(*) FILTER (WHERE companies >= 30) AS codes_over_30,
               count(*) FILTER (WHERE companies < 10) AS codes_under_10,
               round(median(companies), 0) AS median_companies
        FROM (SELECT sic, count(DISTINCT cik) AS companies
              FROM marts.ratio_values WHERE fy = 2024 GROUP BY sic)"""),

    ("6. The biggest major groups, with SEC's own description", """
        SELECT r.sic2,
               any_value(c.sic_description) AS example_description,
               count(DISTINCT r.cik) AS companies
        FROM marts.ratio_values r
        LEFT JOIN ref.dim_company c ON c.cik = r.cik
        WHERE r.fy = 2024
        GROUP BY r.sic2 ORDER BY companies DESC LIMIT 15"""),

    ("7. Does ref.sic_naics exist and is it empty?", """
        SELECT count(*) AS rows FROM ref.sic_naics"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)))[:56]
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
    for title, q in Q:
        print(f"\n### {title}")
        try:
            show(con, q)
        except Exception as exc:
            print(f"  (failed: {str(exc)[:170]})")


if __name__ == "__main__":
    main()
