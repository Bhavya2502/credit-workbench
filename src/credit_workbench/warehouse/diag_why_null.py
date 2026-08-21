"""Why is revenue only 75% filled, and capex only 69%?

A blank cell has four possible causes and they demand opposite responses, so counting
them together is useless:

  A  the spread is empty - the company-year carries essentially nothing
  B  the statement is absent - no income statement or cash flow resolved for that period
  C  the concept was tagged under a name `staging.tag_map` does not claim - a mapping
     gap on our side, and fixable
  D  the filer never disclosed that concept - a bank has no "revenue" line and a
     software company has no capex. Correct, and not fixable

The cause split is done on FY2023 alone. `staging.facts_pit` is 373m rows against a real
compute limit, and one recent year is enough to establish the shape - the whole-window
null rates in Q1-Q3 are exact.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

POP = "basis = 'first_reported' AND is_primary_annual"

# Concepts a filer would use for these ideas. Deliberately broad: the point is to find
# out whether a revenue-shaped tag exists at all, not to map it correctly.
# Qualified as f.tag: both facts_pit and tag_map carry a `tag` column, and an
# unqualified reference is an ambiguity error, not a silent wrong answer - it failed
# loudly on the first run.
REVENUE_LIKE = ("(f.tag ILIKE '%revenue%' OR f.tag ILIKE '%sales%' "
                "OR f.tag ILIKE '%premium%' OR f.tag ILIKE '%interestand%income%')")
CAPEX_LIKE = ("(f.tag ILIKE '%paymentstoacquire%' OR f.tag ILIKE '%capitalexpenditure%' "
              "OR f.tag ILIKE '%purchaseofproperty%' OR f.tag ILIKE '%paymentsforcapital%')")

Q = [
    ("1. The nulls, and how many are simply empty spreads", f"""
        SELECT count(*) AS company_years,
               count(*) FILTER (WHERE is_empty_spread) AS empty_spreads,
               count(*) FILTER (WHERE revenue IS NULL) AS revenue_null,
               count(*) FILTER (WHERE revenue IS NULL AND NOT is_empty_spread)
                   AS revenue_null_non_empty,
               count(*) FILTER (WHERE capex IS NULL) AS capex_null,
               count(*) FILTER (WHERE capex IS NULL AND NOT is_empty_spread)
                   AS capex_null_non_empty,
               count(*) FILTER (WHERE total_assets IS NULL) AS assets_null,
               count(*) FILTER (WHERE ebitda IS NULL) AS ebitda_null
        FROM marts.spreads_a WHERE {POP}"""),

    ("2. Null rate by division - is this a sector effect?", f"""
        SELECT coalesce(h.division_name, '(unmapped)') AS division,
               count(*) AS company_years,
               round(100.0 * count(*) FILTER (WHERE s.revenue IS NULL) / count(*), 1)
                   AS revenue_null_pct,
               round(100.0 * count(*) FILTER (WHERE s.ebitda IS NULL) / count(*), 1)
                   AS ebitda_null_pct,
               round(100.0 * count(*) FILTER (WHERE s.total_assets IS NULL) / count(*), 1)
                   AS assets_null_pct,
               round(100.0 * count(*) FILTER (WHERE s.capex IS NULL) / count(*), 1)
                   AS capex_null_pct
        FROM marts.spreads_a s
        LEFT JOIN ref.sic_hierarchy h ON h.sic4 = s.sic
        WHERE {POP} GROUP BY 1 ORDER BY revenue_null_pct DESC"""),

    ("3. Null rate by fiscal year - is it a coverage problem in the early years?", f"""
        SELECT fy, count(*) AS company_years,
               round(100.0 * count(*) FILTER (WHERE revenue IS NULL) / count(*), 1)
                   AS revenue_null_pct,
               round(100.0 * count(*) FILTER (WHERE capex IS NULL) / count(*), 1)
                   AS capex_null_pct
        FROM marts.spreads_a WHERE {POP} AND fy BETWEEN 2009 AND 2025
        GROUP BY fy ORDER BY fy"""),

    ("4. FY2023 revenue nulls - do they even have an income statement?", f"""
        WITH nulls AS (
            SELECT cik, period_end FROM marts.spreads_a
            WHERE {POP} AND fy = 2023 AND revenue IS NULL AND NOT is_empty_spread),
        lines AS (
            SELECT n.cik, n.period_end,
                   count(*) FILTER (WHERE l.statement = 'IS') AS is_lines
            FROM nulls n
            LEFT JOIN marts.spread_lines l
              ON l.cik = n.cik AND l.period_end = n.period_end
             AND l.basis = 'first_reported' AND l.qtrs = 4
            GROUP BY 1, 2)
        SELECT count(*) AS revenue_nulls,
               count(*) FILTER (WHERE is_lines = 0) AS B_no_income_statement,
               count(*) FILTER (WHERE is_lines > 0) AS has_income_statement
        FROM lines"""),

    ("5. FY2023 revenue nulls WITH an income statement - is a revenue tag present?", f"""
        WITH nulls AS (
            SELECT cik, period_end FROM marts.spreads_a
            WHERE {POP} AND fy = 2023 AND revenue IS NULL AND NOT is_empty_spread),
        tagged AS (
            SELECT n.cik,
                   bool_or({REVENUE_LIKE}) AS has_revenue_like_tag,
                   bool_or({REVENUE_LIKE} AND m.tag IS NULL) AS has_UNMAPPED_revenue_tag
            FROM nulls n
            JOIN staging.facts_pit f ON f.cik = n.cik AND f.period_end = n.period_end
            LEFT JOIN staging.tag_map m ON m.tag = f.tag
            WHERE f.qtrs = 4 AND f.stmt = 'IS'
            GROUP BY n.cik)
        SELECT count(*) AS companies,
               count(*) FILTER (WHERE has_UNMAPPED_revenue_tag)
                   AS C_mapping_gap_on_our_side,
               count(*) FILTER (WHERE NOT has_revenue_like_tag)
                   AS D_no_revenue_concept_disclosed
        FROM tagged"""),

    ("6. The revenue-shaped tags we do not claim, ranked", f"""
        WITH nulls AS (
            SELECT cik, period_end FROM marts.spreads_a
            WHERE {POP} AND fy = 2023 AND revenue IS NULL AND NOT is_empty_spread)
        SELECT f.tag, count(DISTINCT f.cik) AS companies
        FROM nulls n
        JOIN staging.facts_pit f ON f.cik = n.cik AND f.period_end = n.period_end
        LEFT JOIN staging.tag_map m ON m.tag = f.tag
        WHERE f.qtrs = 4 AND f.stmt = 'IS' AND m.tag IS NULL AND {REVENUE_LIKE}
        GROUP BY f.tag ORDER BY companies DESC LIMIT 15"""),

    ("7. FY2023 capex nulls - cash flow present, and a capex-shaped tag?", f"""
        WITH nulls AS (
            SELECT cik, period_end FROM marts.spreads_a
            WHERE {POP} AND fy = 2023 AND capex IS NULL AND NOT is_empty_spread),
        tagged AS (
            SELECT n.cik,
                   bool_or(f.stmt = 'CF') AS has_cash_flow,
                   bool_or(f.stmt = 'CF' AND {CAPEX_LIKE}) AS has_capex_like_tag,
                   bool_or(f.stmt = 'CF' AND {CAPEX_LIKE} AND m.tag IS NULL)
                       AS has_UNMAPPED_capex_tag
            FROM nulls n
            JOIN staging.facts_pit f ON f.cik = n.cik AND f.period_end = n.period_end
            LEFT JOIN staging.tag_map m ON m.tag = f.tag
            WHERE f.qtrs = 4
            GROUP BY n.cik)
        SELECT count(*) AS companies,
               count(*) FILTER (WHERE NOT has_cash_flow) AS B_no_cash_flow_statement,
               count(*) FILTER (WHERE has_UNMAPPED_capex_tag) AS C_mapping_gap,
               count(*) FILTER (WHERE has_cash_flow AND NOT has_capex_like_tag)
                   AS D_no_capex_disclosed
        FROM tagged"""),

    ("8. The global unmapped-tag worklist, by filings", """
        SELECT tag, statement, filings, last_seen_fy
        FROM staging.unmapped_tags ORDER BY filings DESC LIMIT 15"""),
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
