"""Which capex-shaped tags does the template fail to claim? The fix list."""
from __future__ import annotations
import duckdb
from credit_workbench.common.config import motherduck_token

Q = """
WITH nulls AS (
    SELECT cik, period_end FROM marts.spreads_a
    WHERE basis = 'first_reported' AND is_primary_annual AND fy = 2023
      AND capex IS NULL AND NOT is_empty_spread)
SELECT f.tag, count(DISTINCT f.cik) AS companies, sum(abs(f.value)) AS abs_value
FROM nulls n
JOIN staging.facts_pit f ON f.cik = n.cik AND f.period_end = n.period_end
LEFT JOIN staging.tag_map m ON m.tag = f.tag
WHERE f.qtrs = 4 AND f.stmt = 'CF' AND m.tag IS NULL
  AND (f.tag ILIKE '%paymentstoacquire%' OR f.tag ILIKE '%capitalexpenditure%'
       OR f.tag ILIKE '%purchaseofproperty%' OR f.tag ILIKE '%paymentsforcapital%')
GROUP BY f.tag ORDER BY companies DESC LIMIT 18"""

con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
cur = con.execute(Q)
print(f"\n  {'tag':<56}{'companies':>10}  {'$bn':>8}")
print("  " + "-" * 76)
for tag, n, v in cur.fetchall():
    print(f"  {tag[:54]:<56}{n:>10,}  {(v or 0)/1e9:>8,.1f}")
