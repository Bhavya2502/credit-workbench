"""Who has data in this warehouse but never reaches an outcome row?

`diag_industry_mapping` showed the industry join is near-total *for companies that reach
`marts.credit_outcomes`* - 15,460 of 15,550 with a spread. But the warehouse holds facts
for far more companies than that, and a company absent from the denominator biases an
industry default rate exactly as much as one tagged to the wrong industry, while being
much harder to notice.

Measured on FY2024 alone rather than the whole window. `staging.facts_pit` is 373m rows
and the free-plan compute limit is real; one filtered year answers the question about the
shape of the gap without a full scan, and the shape is what is in doubt.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

Q = [
    ("1. FY2024 - the funnel from facts to an outcome row", """
        SELECT (SELECT count(DISTINCT cik) FROM staging.facts_pit WHERE fy = 2024)
                   AS with_any_fact,
               (SELECT count(DISTINCT cik) FROM marts.spreads_a
                WHERE fy = 2024 AND basis = 'latest') AS with_a_spread,
               (SELECT count(DISTINCT cik) FROM marts.spreads_a
                WHERE fy = 2024 AND basis = 'latest' AND is_primary_annual
                  AND NOT is_empty_spread) AS with_usable_annual,
               (SELECT count(DISTINCT cik) FROM marts.credit_outcomes WHERE fy = 2024)
                   AS with_outcome_row"""),

    ("2. FY2024 - what are the companies with facts but no outcome row?", """
        WITH missing AS (
            SELECT DISTINCT f.cik
            FROM staging.facts_pit f
            WHERE f.fy = 2024
              AND f.cik NOT IN (SELECT cik FROM marts.credit_outcomes WHERE fy = 2024))
        SELECT coalesce(d.entity_type, '(none)') AS entity_type,
               coalesce(d.sic_description, '(no sic)') AS industry,
               count(*) AS companies
        FROM missing m
        LEFT JOIN ref.dim_company d ON d.cik = TRY_CAST(m.cik AS BIGINT)
        GROUP BY 1, 2 ORDER BY companies DESC LIMIT 20"""),

    ("3. FY2024 - do the missing ones have real financial-statement facts?", """
        WITH missing AS (
            SELECT DISTINCT cik FROM staging.facts_pit f
            WHERE f.fy = 2024
              AND f.cik NOT IN (SELECT cik FROM marts.credit_outcomes WHERE fy = 2024))
        SELECT count(DISTINCT f.cik) AS companies,
               count(*) AS facts,
               count(DISTINCT f.cik) FILTER (WHERE f.tag = 'Assets') AS have_assets,
               count(DISTINCT f.cik) FILTER (WHERE f.tag = 'Revenues') AS have_revenues,
               round(median(facts_per_company), 0) AS median_facts_per_company
        FROM staging.facts_pit f
        JOIN missing m ON m.cik = f.cik
        JOIN (SELECT cik, count(*) AS facts_per_company FROM staging.facts_pit
              WHERE fy = 2024 GROUP BY cik) pc ON pc.cik = f.cik
        WHERE f.fy = 2024"""),

    ("4. FY2024 - and how many facts does a company WITH an outcome row have?", """
        SELECT count(DISTINCT f.cik) AS companies,
               round(median(pc.facts_per_company), 0) AS median_facts_per_company
        FROM staging.facts_pit f
        JOIN (SELECT cik, count(*) AS facts_per_company FROM staging.facts_pit
              WHERE fy = 2024 GROUP BY cik) pc ON pc.cik = f.cik
        WHERE f.fy = 2024
          AND f.cik IN (SELECT cik FROM marts.credit_outcomes WHERE fy = 2024)"""),

    ("5. Whole window - how many companies does the fact base actually hold?", """
        SELECT approx_count_distinct(cik) AS companies_with_facts_approx
        FROM staging.facts_pit"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)))[:60]
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
