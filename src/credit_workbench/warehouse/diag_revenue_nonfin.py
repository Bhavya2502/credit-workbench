"""Revenue fill rate with financials removed - and what is still missing after that.

"Financial institution" has two defensible boundaries and they give different answers, so
both are measured rather than one being chosen silently:

  narrow   SIC 60-64: depository institutions, non-depository credit, brokers, insurance
           carriers and agents. The businesses whose income statement is interest and fee
           income rather than revenue.
  broad    SIC division H, 60-67: the above plus real estate (65) and holding and
           investment offices (67). That sweeps in REITs, which do report rental revenue,
           and blank-check shells, which report nothing because there is nothing.

The point of the question is to isolate how much of the 24.6% gap is the banking-tag
defect and how much is everything else, so the exclusions are applied one at a time and
the fill rate after each is shown.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

POP = "s.basis = 'first_reported' AND s.is_primary_annual"
NARROW = "s.sic2 IN ('60', '61', '62', '63', '64')"
BROAD = "s.sic2 IN ('60', '61', '62', '63', '64', '65', '67')"
SHELL = "s.sic IN ('6770', '9995', '8880', '6199')"

Q = [
    ("1. Fill rate, exclusions applied one at a time", f"""
        WITH b AS (SELECT s.*, substr(s.sic, 1, 2) AS sic2 FROM marts.spreads_a s
                   WHERE s.basis = 'first_reported' AND s.is_primary_annual)
        SELECT 'all company-years' AS cut, count(*) AS company_years,
               count(revenue) AS with_revenue,
               round(100.0 * count(revenue) / count(*), 1) AS fill_pct
        FROM b s
        UNION ALL SELECT 'minus empty spreads', count(*), count(revenue),
               round(100.0 * count(revenue) / count(*), 1)
        FROM b s WHERE NOT s.is_empty_spread
        UNION ALL SELECT 'minus empty, minus banks (SIC 60-64)', count(*), count(revenue),
               round(100.0 * count(revenue) / count(*), 1)
        FROM b s WHERE NOT s.is_empty_spread AND NOT {NARROW}
        UNION ALL SELECT 'minus empty, minus all FIRE (SIC 60-67)', count(*), count(revenue),
               round(100.0 * count(revenue) / count(*), 1)
        FROM b s WHERE NOT s.is_empty_spread AND NOT {BROAD}
        UNION ALL SELECT 'minus empty, FIRE and shell/blank-check codes', count(*),
               count(revenue), round(100.0 * count(revenue) / count(*), 1)
        FROM b s WHERE NOT s.is_empty_spread AND NOT {BROAD} AND NOT {SHELL}"""),

    ("2. Non-financial only (ex SIC 60-67, ex empty) - fill rate by division", f"""
        SELECT h.division_name AS division, count(*) AS company_years,
               count(s.revenue) AS with_revenue,
               round(100.0 * count(s.revenue) / count(*), 1) AS fill_pct
        FROM marts.spreads_a s
        JOIN ref.sic_hierarchy h ON h.sic4 = s.sic
        WHERE {POP} AND NOT s.is_empty_spread
          AND substr(s.sic, 1, 2) NOT IN ('60','61','62','63','64','65','67')
        GROUP BY 1 ORDER BY fill_pct"""),

    ("3. Non-financial - fill rate by fiscal year", f"""
        SELECT s.fy, count(*) AS company_years,
               round(100.0 * count(s.revenue) / count(*), 1) AS fill_pct
        FROM marts.spreads_a s
        WHERE {POP} AND NOT s.is_empty_spread AND s.fy BETWEEN 2010 AND 2025
          AND substr(s.sic, 1, 2) NOT IN ('60','61','62','63','64','65','67')
        GROUP BY 1 ORDER BY 1"""),

    ("4. Non-financial - the worst major groups", f"""
        SELECT substr(s.sic, 1, 2) AS sic2,
               mode(h.sic4_description) AS example_industry,
               count(*) AS company_years, count(DISTINCT s.cik) AS companies,
               round(100.0 * count(s.revenue) / count(*), 1) AS fill_pct
        FROM marts.spreads_a s
        LEFT JOIN ref.sic_hierarchy h ON h.sic4 = s.sic
        WHERE {POP} AND NOT s.is_empty_spread
          AND substr(s.sic, 1, 2) NOT IN ('60','61','62','63','64','65','67')
        GROUP BY 1 HAVING count(*) >= 300 ORDER BY fill_pct LIMIT 12"""),

    ("5. Non-financial nulls - explicit zero, or nothing at all?", f"""
        SELECT count(*) AS company_years,
               count(*) FILTER (WHERE s.revenue IS NULL) AS revenue_null,
               count(*) FILTER (WHERE s.revenue = 0) AS revenue_zero,
               count(*) FILTER (WHERE s.revenue IS NULL AND s.total_assets IS NOT NULL)
                   AS null_but_has_assets,
               count(*) FILTER (WHERE s.revenue IS NULL AND s.net_income IS NOT NULL)
                   AS null_but_has_net_income
        FROM marts.spreads_a s
        WHERE {POP} AND NOT s.is_empty_spread
          AND substr(s.sic, 1, 2) NOT IN ('60','61','62','63','64','65','67')"""),

    ("6. FY2023 non-financial nulls - what do they report instead?", f"""
        WITH nulls AS (
            SELECT s.cik, s.period_end FROM marts.spreads_a s
            WHERE {POP} AND s.fy = 2023 AND s.revenue IS NULL AND NOT s.is_empty_spread
              AND substr(s.sic, 1, 2) NOT IN ('60','61','62','63','64','65','67'))
        SELECT f.tag, count(DISTINCT f.cik) AS companies
        FROM nulls n
        JOIN staging.facts_pit f ON f.cik = n.cik AND f.period_end = n.period_end
        WHERE f.stmt = 'IS' AND f.qtrs = 4
        GROUP BY f.tag ORDER BY companies DESC LIMIT 15"""),

    ("7. FY2023 non-financial nulls - the biggest by assets, are any real operators?", f"""
        SELECT s.company_name, s.sic, round(s.total_assets / 1e9, 2) AS assets_bn,
               round(s.net_income / 1e6, 1) AS net_income_m,
               round(s.operating_income / 1e6, 1) AS operating_income_m
        FROM marts.spreads_a s
        WHERE {POP} AND s.fy = 2023 AND s.revenue IS NULL AND NOT s.is_empty_spread
          AND substr(s.sic, 1, 2) NOT IN ('60','61','62','63','64','65','67')
          AND s.total_assets IS NOT NULL
        ORDER BY s.total_assets DESC LIMIT 15"""),
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
