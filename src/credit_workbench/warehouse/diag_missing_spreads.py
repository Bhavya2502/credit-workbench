"""The 814 companies with annual facts and no spread - why does the builder skip them?

`transform/spreads.py` emits an annual row only for a `(cik, period_end)` where the
company tagged a **full-year income-statement or cash-flow flow**:

    annual_periods AS (SELECT DISTINCT cik, basis, period_end FROM lines
                       WHERE statement IN ('IS', 'CF') AND qtrs = 4)

and `lines` is itself the product of `staging.tag_map` - a fact whose tag no line claims
never becomes a line at all. So there are three distinct ways to have annual facts and no
spread, and they need different fixes:

  A  no annual IS/CF fact at all - only a balance sheet, or only quarterly flows.
     Correct behaviour: there is no income statement to spread.
  B  annual IS/CF facts exist, but every tag used is outside `staging.tag_map`.
     A mapping gap, and fixable by extending the map.
  C  the facts arrived after the mart was last built. Staleness, fixable by a re-run.

This separates them and counts each, because "814 companies missing" is not actionable
until it is known which of the three each one is.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

# Companies holding XBRL facts but no row at all in the annual spread mart.
NOSPREAD = """
    SELECT DISTINCT cik FROM staging.facts_pit
    WHERE cik NOT IN (SELECT cik FROM marts.spreads_a)
"""

Q = [
    ("1. Split the gap by cause - the builder's own gate", f"""
        WITH nospread AS ({NOSPREAD}),
        per_company AS (
            SELECT n.cik,
                   bool_or(f.qtrs = 4 AND f.stmt IN ('IS', 'CF')) AS has_annual_flow_fact,
                   bool_or(f.qtrs = 4 AND f.stmt IN ('IS', 'CF')
                           AND m.tag IS NOT NULL)                 AS has_mapped_annual_flow,
                   bool_or(f.stmt = 'BS')                         AS has_balance_sheet,
                   max(f.fy)                                      AS last_fy
            FROM nospread n
            JOIN staging.facts_pit f ON f.cik = n.cik
            LEFT JOIN staging.tag_map m ON m.tag = f.tag
            GROUP BY n.cik)
        SELECT count(*) AS companies,
               count(*) FILTER (WHERE NOT has_annual_flow_fact)
                   AS a_no_annual_flow_at_all,
               count(*) FILTER (WHERE has_annual_flow_fact AND NOT has_mapped_annual_flow)
                   AS b_annual_flow_but_no_mapped_tag,
               count(*) FILTER (WHERE has_mapped_annual_flow)
                   AS c_mapped_annual_flow_should_have_spread,
               count(*) FILTER (WHERE has_balance_sheet) AS have_a_balance_sheet
        FROM per_company"""),

    ("2. Group C - mapped annual flows but no spread. Which years?", f"""
        WITH nospread AS ({NOSPREAD}),
        c AS (
            SELECT n.cik, max(f.fy) AS last_fy, count(*) AS mapped_annual_facts
            FROM nospread n
            JOIN staging.facts_pit f ON f.cik = n.cik
            JOIN staging.tag_map m ON m.tag = f.tag
            WHERE f.qtrs = 4 AND f.stmt IN ('IS', 'CF')
            GROUP BY n.cik)
        SELECT last_fy, count(*) AS companies, sum(mapped_annual_facts) AS facts
        FROM c GROUP BY 1 ORDER BY 1 DESC LIMIT 15"""),

    ("3. Group C - name them, so they can be looked up", f"""
        WITH nospread AS ({NOSPREAD}),
        c AS (
            SELECT n.cik, any_value(f.company_name) AS company_name,
                   any_value(f.sic) AS sic, min(f.fy) AS first_fy, max(f.fy) AS last_fy,
                   count(*) AS mapped_annual_facts,
                   count(DISTINCT f.period_end) AS periods
            FROM nospread n
            JOIN staging.facts_pit f ON f.cik = n.cik
            JOIN staging.tag_map m ON m.tag = f.tag
            WHERE f.qtrs = 4 AND f.stmt IN ('IS', 'CF')
            GROUP BY n.cik)
        SELECT * FROM c ORDER BY mapped_annual_facts DESC LIMIT 20"""),

    ("4. Group B - annual flows whose tags no line claims. Which tags?", f"""
        WITH nospread AS ({NOSPREAD})
        SELECT f.tag, count(DISTINCT f.cik) AS companies, count(*) AS facts
        FROM nospread n
        JOIN staging.facts_pit f ON f.cik = n.cik
        LEFT JOIN staging.tag_map m ON m.tag = f.tag
        WHERE f.qtrs = 4 AND f.stmt IN ('IS', 'CF') AND m.tag IS NULL
        GROUP BY f.tag ORDER BY companies DESC LIMIT 20"""),

    ("5. Is the mart stale? Latest period in the facts vs in the spread", """
        SELECT (SELECT max(period_end) FROM staging.facts_pit
                WHERE qtrs = 4 AND stmt = 'IS') AS latest_annual_is_fact,
               (SELECT max(period_end) FROM marts.spreads_a) AS latest_spread_period,
               (SELECT max(filed) FROM staging.facts_pit) AS latest_filing_in_facts,
               (SELECT max(last_filed) FROM marts.spreads_a) AS latest_filing_in_spread"""),

    ("6. CHECK for the export: is_primary_annual vs picking the latest period_end", """
        SELECT count(*) AS first_reported_rows,
               count(*) FILTER (WHERE is_primary_annual) AS primary_annual_rows,
               count(*) FILTER (WHERE is_primary_annual AND NOT is_empty_spread)
                   AS primary_and_not_empty,
               count(DISTINCT (cik, fy)) AS distinct_cik_fy
        FROM marts.spreads_a WHERE basis = 'first_reported'"""),

    ("7. Where the two selections disagree - does it change the figures?", """
        WITH mine AS (
            SELECT cik, fy, period_end, revenue, total_assets FROM marts.spreads_a
            WHERE basis = 'first_reported'
            QUALIFY row_number() OVER (PARTITION BY cik, fy ORDER BY period_end DESC) = 1),
        theirs AS (
            SELECT cik, fy, period_end, revenue, total_assets FROM marts.spreads_a
            WHERE basis = 'first_reported' AND is_primary_annual)
        SELECT count(*) AS company_years_compared,
               count(*) FILTER (WHERE m.period_end <> t.period_end) AS different_period,
               count(*) FILTER (WHERE m.revenue IS DISTINCT FROM t.revenue)
                   AS different_revenue,
               count(*) FILTER (WHERE m.revenue IS NULL AND t.revenue IS NOT NULL)
                   AS mine_null_theirs_not
        FROM mine m JOIN theirs t USING (cik, fy)"""),
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
