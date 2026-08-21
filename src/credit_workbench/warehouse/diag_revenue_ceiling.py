"""How close to 100% revenue fill can we actually get? Every recovery path, measured.

The previous answers assumed a null is correct wherever the filer tagged no revenue
concept. That assumption is worth dropping, because it is not the only defensible
treatment: Compustat and Capital IQ both report 0 for a company with no sales rather than
a blank, and a company that reports operating expenses and an operating loss has told you
its revenue was zero as surely as if it had tagged it.

So this measures every path to a value, in priority order, and counts what each one
recovers. A company-year lands in the first bucket that can supply it:

  1 MAP      a revenue-shaped tag exists that `staging.tag_map` does not claim - banks,
             utilities, IFRS. Purely a mapping fix, no inference.
  2 IDENTITY revenue is computable from figures already held: gross profit + cost of
             sales, or operating income + total operating expenses. Arithmetic, not
             estimation.
  3 SEGMENT  no consolidated revenue, but segment or dimensioned revenue exists in
             `marts.segments`. Recoverable, with care - segments must be exhaustive and
             single-axis or the sum double-counts.
  4 ZERO     provably zero: the company reports operating expenses and an operating
             income equal to their negative, so revenue was nil. An imputation, but a
             checkable one.
  5 NONE     no income statement, or nothing from which revenue can be derived.

Bucket 5 is the real ceiling. Everything above it is reachable.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

# Deliberately narrow: concepts that ARE a revenue, not merely tags containing the word.
REV_LIKE = """(
    f.tag ILIKE 'Revenue%' OR f.tag ILIKE '%OperatingRevenue%'
 OR f.tag ILIKE 'Sales%' OR f.tag ILIKE '%SalesRevenue%'
 OR f.tag ILIKE 'InterestAndDividendIncome%' OR f.tag ILIKE 'InterestAndFeeIncome%'
 OR f.tag ILIKE '%RevenuesNetOfInterestExpense%'
 OR f.tag ILIKE 'RevenuesIncludingIntersegment%'
 OR f.tag ILIKE 'OilAndGasRevenue%' OR f.tag ILIKE 'RealEstateRevenue%'
 OR f.tag ILIKE 'BrokerageCommissions%' OR f.tag ILIKE 'PremiumsEarned%'
 OR f.tag ILIKE 'TotalRevenue%' OR f.tag ILIKE '%RevenueFromContract%')"""

TOL = "0.01"          # 1% - the tolerance the spread's own subtotal checks use

SETUP = f"""
CREATE OR REPLACE TEMP TABLE nulls AS
SELECT s.cik, s.period_end, s.fy, s.company_name, s.sic,
       s.gross_profit, s.cost_of_sales, s.operating_income,
       s.total_operating_expenses, s.total_assets, s.net_income
FROM marts.spreads_a s
WHERE s.basis = 'first_reported' AND s.is_primary_annual AND s.revenue IS NULL;

-- 1 MAP: an unclaimed revenue tag exists for that exact period
CREATE OR REPLACE TEMP TABLE has_tag AS
SELECT DISTINCT n.cik, n.period_end
FROM nulls n
JOIN staging.facts_pit f ON f.cik = n.cik AND f.period_end = n.period_end
LEFT JOIN staging.tag_map m ON m.tag = f.tag
WHERE f.qtrs = 4 AND m.tag IS NULL AND {REV_LIKE};

-- 3 SEGMENT: revenue disclosed on an axis but not at the consolidated level
CREATE OR REPLACE TEMP TABLE has_segment AS
SELECT DISTINCT n.cik, n.period_end
FROM nulls n
JOIN marts.segments g ON g.cik = CAST(n.cik AS VARCHAR) AND g.fy = n.fy
WHERE g.is_latest AND g.qtrs = 4 AND g.uom = 'USD'
  AND (g.tag ILIKE 'Revenue%' OR g.tag ILIKE '%SalesRevenue%'
       OR g.tag ILIKE '%RevenueFromContract%');

CREATE OR REPLACE TEMP TABLE classified AS
SELECT n.*,
       t.cik IS NOT NULL                                              AS can_map,
       (n.gross_profit IS NOT NULL AND n.cost_of_sales IS NOT NULL)   AS can_gp,
       (n.operating_income IS NOT NULL
        AND n.total_operating_expenses IS NOT NULL)                   AS can_opex,
       g.cik IS NOT NULL                                              AS can_segment,
       (n.operating_income IS NOT NULL AND n.total_operating_expenses IS NOT NULL
        AND abs(n.operating_income + n.total_operating_expenses)
            <= {TOL} * abs(nullif(n.total_operating_expenses, 0)))    AS provably_zero
FROM nulls n
LEFT JOIN has_tag t ON t.cik = n.cik AND t.period_end = n.period_end
LEFT JOIN has_segment g ON g.cik = n.cik AND g.period_end = n.period_end
"""

Q = [
    ("1. The waterfall - what each recovery path adds, in priority order", """
        WITH b AS (
            SELECT *,
                   CASE WHEN can_map THEN '1 MAP - unclaimed revenue tag exists'
                        WHEN can_gp OR can_opex THEN '2 IDENTITY - computable from held figures'
                        WHEN can_segment THEN '3 SEGMENT - disclosed on an axis only'
                        WHEN provably_zero THEN '4 ZERO - provably nil'
                        ELSE '5 NONE - nothing to derive from' END AS bucket
            FROM classified)
        SELECT bucket, count(*) AS company_years,
               count(DISTINCT cik) AS companies
        FROM b GROUP BY 1 ORDER BY 1"""),

    ("2. What fill rate each cumulative step would reach", """
        WITH tot AS (SELECT count(*) AS n FROM marts.spreads_a
                     WHERE basis = 'first_reported' AND is_primary_annual),
        filled AS (SELECT count(revenue) AS n FROM marts.spreads_a
                   WHERE basis = 'first_reported' AND is_primary_annual),
        b AS (SELECT count(*) FILTER (WHERE can_map) AS m,
                     count(*) FILTER (WHERE NOT can_map AND (can_gp OR can_opex)) AS i,
                     count(*) FILTER (WHERE NOT can_map AND NOT (can_gp OR can_opex)
                                        AND can_segment) AS s,
                     count(*) FILTER (WHERE NOT can_map AND NOT (can_gp OR can_opex)
                                        AND NOT can_segment AND provably_zero) AS z
              FROM classified)
        SELECT 'today' AS step, filled.n AS revenue_rows,
               round(100.0 * filled.n / tot.n, 1) AS fill_pct FROM filled, tot
        UNION ALL SELECT '+ map unclaimed tags', filled.n + b.m,
               round(100.0 * (filled.n + b.m) / tot.n, 1) FROM filled, tot, b
        UNION ALL SELECT '+ arithmetic identity', filled.n + b.m + b.i,
               round(100.0 * (filled.n + b.m + b.i) / tot.n, 1) FROM filled, tot, b
        UNION ALL SELECT '+ segment aggregation', filled.n + b.m + b.i + b.s,
               round(100.0 * (filled.n + b.m + b.i + b.s) / tot.n, 1) FROM filled, tot, b
        UNION ALL SELECT '+ impute provable zero', filled.n + b.m + b.i + b.s + b.z,
               round(100.0 * (filled.n + b.m + b.i + b.s + b.z) / tot.n, 1)
        FROM filled, tot, b"""),

    ("3. Bucket 5 - the irreducible residue. What do they have at all?", """
        SELECT count(*) AS company_years,
               count(*) FILTER (WHERE total_assets IS NOT NULL) AS has_total_assets,
               count(*) FILTER (WHERE net_income IS NOT NULL) AS has_net_income,
               count(*) FILTER (WHERE operating_income IS NOT NULL) AS has_operating_income,
               count(*) FILTER (WHERE total_assets IS NULL AND net_income IS NULL)
                   AS has_neither
        FROM classified
        WHERE NOT can_map AND NOT can_gp AND NOT can_opex AND NOT can_segment
          AND NOT provably_zero"""),

    ("4. Bucket 5 by industry - is the residue concentrated?", """
        SELECT h.division_name AS division, substr(c.sic, 1, 2) AS sic2,
               mode(h.sic4_description) AS example_industry,
               count(*) AS company_years
        FROM classified c
        LEFT JOIN ref.sic_hierarchy h ON h.sic4 = c.sic
        WHERE NOT c.can_map AND NOT c.can_gp AND NOT c.can_opex AND NOT c.can_segment
          AND NOT c.provably_zero
        GROUP BY 1, 2 ORDER BY company_years DESC LIMIT 12"""),

    ("5. Bucket 5 by fiscal year - is it an old-data problem?", """
        SELECT fy, count(*) AS company_years
        FROM classified
        WHERE NOT can_map AND NOT can_gp AND NOT can_opex AND NOT can_segment
          AND NOT provably_zero
        GROUP BY fy ORDER BY fy DESC LIMIT 18"""),

    ("6. Sanity - does the identity actually reproduce revenue where BOTH are known?", f"""
        SELECT count(*) AS testable,
               count(*) FILTER (WHERE abs(revenue - (gross_profit + cost_of_sales))
                                      <= {TOL} * abs(nullif(revenue, 0)))
                   AS gp_identity_holds,
               round(100.0 * count(*) FILTER (WHERE abs(revenue - (gross_profit + cost_of_sales))
                                      <= {TOL} * abs(nullif(revenue, 0))) / count(*), 1)
                   AS pct
        FROM marts.spreads_a
        WHERE basis = 'first_reported' AND is_primary_annual
          AND revenue IS NOT NULL AND gross_profit IS NOT NULL
          AND cost_of_sales IS NOT NULL"""),

    ("7. Sanity - and the operating-expense identity?", f"""
        SELECT count(*) AS testable,
               count(*) FILTER (WHERE abs(revenue - (operating_income + total_operating_expenses))
                                      <= {TOL} * abs(nullif(revenue, 0)))
                   AS opex_identity_holds,
               round(100.0 * count(*) FILTER (WHERE abs(revenue - (operating_income + total_operating_expenses))
                                      <= {TOL} * abs(nullif(revenue, 0))) / count(*), 1)
                   AS pct
        FROM marts.spreads_a
        WHERE basis = 'first_reported' AND is_primary_annual
          AND revenue IS NOT NULL AND operating_income IS NOT NULL
          AND total_operating_expenses IS NOT NULL"""),
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
    print("building classification ...")
    for stmt in SETUP.strip().split(";"):
        if stmt.strip():
            con.execute(stmt)
    n = con.execute("SELECT count(*) FROM classified").fetchone()[0]
    print(f"  {n:,} null-revenue company-years classified")
    for title, q in Q:
        print(f"\n### {title}")
        try:
            show(con, q)
        except Exception as exc:
            print(f"  (failed: {str(exc)[:200]})")


if __name__ == "__main__":
    main()
