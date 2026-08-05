"""Register the point-in-time fact base and build the restatement report (C4, final step).

Runs after all year batches have written their parquet.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common import r2 as r2util
from credit_workbench.common.config import R2, motherduck_token

LAKE = "r2://credit-workbench-raw"
PREFIX = "parquet/derived/facts_pit/"
OUT = f"{LAKE}/{PREFIX.rstrip('/')}"


def clean_stale() -> None:
    """Remove output from earlier runs that used a different partition column.

    The first build of this dataset partitioned by filing year and was killed
    part-way; any leftovers would be picked up by the view's glob and collide with
    the current `period_year=` layout.
    """
    cfg = R2.from_env()
    s3 = r2util.client(cfg)
    stale = []
    for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=cfg.bucket, Prefix=PREFIX):
        for obj in page.get("Contents", []):
            leaf = obj["Key"][len(PREFIX):]
            if not leaf.startswith("period_year="):
                stale.append({"Key": obj["Key"]})
    for i in range(0, len(stale), 1000):
        s3.delete_objects(Bucket=cfg.bucket, Delete={"Objects": stale[i:i + 1000]})
    print(f"Removed {len(stale)} stale object(s) from an earlier partition scheme")


def main() -> None:
    clean_stale()
    md = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    md.execute("DROP VIEW IF EXISTS staging.facts_pit")
    md.execute(f"""
        CREATE VIEW staging.facts_pit AS
        SELECT * FROM read_parquet('{OUT}/*/*.parquet', hive_partitioning = true)""")
    n, companies, lo, hi = md.execute("""
        SELECT count(*), count(DISTINCT cik), min(period_end), max(period_end)
        FROM staging.facts_pit""").fetchone()
    print(f"view  staging.facts_pit  {n:,} facts, {companies:,} companies, {lo} .. {hi}")

    md.execute("""
        CREATE OR REPLACE TABLE marts.restatements AS
        WITH f AS (
            SELECT cik, any_value(company_name) AS company_name, tag, uom,
                   period_end, qtrs,
                   max(CASE WHEN is_first_report THEN value END) AS first_reported,
                   max(CASE WHEN is_latest       THEN value END) AS latest_value,
                   min(CASE WHEN is_first_report THEN filed END) AS first_filed,
                   max(CASE WHEN is_latest       THEN filed END) AS latest_filed,
                   any_value(times_reported) AS times_reported
            FROM staging.facts_pit
            WHERE times_reported > 1 AND qtrs IN (0, 4) AND uom = 'USD'
            GROUP BY cik, tag, uom, period_end, qtrs)
        SELECT *, latest_value - first_reported AS restatement_amount,
               CASE WHEN first_reported <> 0
                    THEN (latest_value - first_reported) / abs(first_reported) END
                    AS restatement_pct
        FROM f
        WHERE first_reported IS NOT NULL AND latest_value IS NOT NULL
          AND first_reported <> latest_value""")
    print(f"table marts.restatements  "
          f"{md.execute('SELECT count(*) FROM marts.restatements').fetchone()[0]:,} rows")


if __name__ == "__main__":
    main()
