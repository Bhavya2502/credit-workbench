"""Which period years fell outside the flag pass?

The point-in-time pass is batched over period years 2008-2030. The flagged dataset came
out 496,846 rows short of the source, so some facts carry a period year outside that
window - or none at all. Name them before deciding whether to widen the batches or to
exclude them deliberately.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import R2

LAKE = "r2://credit-workbench-raw"
FACTS_DIM = f"{LAKE}/parquet/derived/facts_dimensioned"


def main() -> None:
    cfg = R2.from_env()
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"""
        CREATE OR REPLACE SECRET r2_lake (
            TYPE R2, KEY_ID '{cfg.access_key_id}', SECRET '{cfg.secret_access_key}',
            ACCOUNT_ID '{cfg.account_id}', REGION 'auto')""")
    con.execute("SET memory_limit = '9GB'")

    print("### Period years outside the 2008-2030 flag window")
    rows = con.execute(f"""
        SELECT period_year, count(*) AS facts
        FROM read_parquet('{FACTS_DIM}/*/*.parquet', hive_partitioning = true,
                          union_by_name = true)
        WHERE period_year IS NULL OR period_year < 2008 OR period_year > 2030
        GROUP BY 1 ORDER BY facts DESC LIMIT 40""").fetchall()
    total = 0
    for year, facts in rows:
        total += facts
        print(f"  {str(year):>12}  {facts:>12,}")
    print(f"  {'TOTAL':>12}  {total:>12,}")


if __name__ == "__main__":
    main()
