"""Which revenue tags do real non-financial operators use that we do not claim?

Q7 of diag_revenue_nonfin found Xcel Energy, DTE, Apache and Meritage Homes carrying a
null revenue while reporting operating income in the billions. Those are not pre-revenue
shells, so the tag they used is the fix list. Restricted to null-revenue company-years
that DO report operating income, which is what separates a real operator from a
clinical-stage biotech with no sales.
"""
from __future__ import annotations
import duckdb
from credit_workbench.common.config import motherduck_token

Q = """
WITH nulls AS (
    SELECT s.cik, s.period_end FROM marts.spreads_a s
    WHERE s.basis = 'first_reported' AND s.is_primary_annual AND s.fy = 2023
      AND s.revenue IS NULL AND s.operating_income IS NOT NULL
      AND substr(s.sic, 1, 2) NOT IN ('60','61','62','63','64','65','67'))
SELECT f.tag, count(DISTINCT f.cik) AS companies,
       round(max(abs(f.value)) / 1e9, 1) AS largest_bn
FROM nulls n
JOIN staging.facts_pit f ON f.cik = n.cik AND f.period_end = n.period_end
LEFT JOIN staging.tag_map m ON m.tag = f.tag
WHERE f.qtrs = 4 AND m.tag IS NULL
  AND (f.tag ILIKE '%revenue%' OR f.tag ILIKE '%sales%')
GROUP BY f.tag ORDER BY companies DESC LIMIT 20"""

con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
n = con.execute("""
    SELECT count(*) FROM marts.spreads_a s
    WHERE s.basis = 'first_reported' AND s.is_primary_annual AND s.fy = 2023
      AND s.revenue IS NULL AND s.operating_income IS NOT NULL
      AND substr(s.sic, 1, 2) NOT IN ('60','61','62','63','64','65','67')""").fetchone()[0]
print(f"\n  FY2023 non-financial, null revenue but WITH operating income: {n:,} companies\n")
print(f"  {'unmapped tag':<58}{'companies':>10}{'largest $bn':>13}")
print("  " + "-" * 81)
for tag, c, v in con.execute(Q).fetchall():
    print(f"  {tag[:56]:<58}{c:>10,}{(v if v is not None else 0):>13,.1f}")
