"""Tracker C4 stage 2 — reporting vintages, restatements, and the registered view.

The same period is reported repeatedly: Pfizer first reported FY2023 revenue as
$58,496m in its Feb-2024 10-K, then $59,553m in the Feb-2025 10-K — a $1.06bn
restatement. Every fact therefore carries `is_first_report` (what was knowable at the
time, which a model must train on) and `is_latest` (today's best view, which an
analyst reads).

Reads the de-duplicated output of stage 1 — a fraction of the raw fact volume — so
the vintage pass, which must see every copy of a figure at once, stays cheap.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common import r2 as r2util
from credit_workbench.common.config import R2, motherduck_token

BUCKET = "credit-workbench-raw"
LAKE = f"r2://{BUCKET}"
DEDUP = f"{LAKE}/parquet/derived/facts_dedup"
PREFIX = "parquet/derived/facts_pit/"
OUT = f"{LAKE}/{PREFIX.rstrip('/')}"

BUILD = f"""
COPY (
    WITH d AS (
        SELECT * FROM read_parquet('{DEDUP}/*/*.parquet', hive_partitioning = true)
    ),
    vintage AS (
        SELECT cik, tag, uom, coreg, period_end, qtrs,
               min(filed) AS first_filed, max(filed) AS last_filed,
               count(DISTINCT adsh) AS times_reported
        FROM d GROUP BY 1, 2, 3, 4, 5, 6
    )
    SELECT d.*,
           (d.filed = v.first_filed) AS is_first_report,
           (d.filed = v.last_filed)  AS is_latest,
           v.times_reported
    FROM d JOIN vintage v USING (cik, tag, uom, coreg, period_end, qtrs)
) TO '{OUT}' (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (period_year),
              OVERWRITE_OR_IGNORE, FILENAME_PATTERN 'pit_{{i}}')
"""


def wipe(prefix: str) -> None:
    """Clear the output prefix entirely before a rebuild.

    This dataset is always written whole, and earlier aborted runs left files with a
    different column set under the same partition names — a selective clean would not
    catch those, and the view would then union mismatched schemas.
    """
    cfg = R2.from_env()
    s3 = r2util.client(cfg)
    doomed = []
    for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=cfg.bucket, Prefix=prefix):
        doomed += [{"Key": o["Key"]} for o in page.get("Contents", [])]
    for i in range(0, len(doomed), 1000):
        s3.delete_objects(Bucket=cfg.bucket, Delete={"Objects": doomed[i:i + 1000]})
    print(f"Cleared {len(doomed)} object(s) under {prefix}")


def main() -> None:
    wipe(PREFIX)

    cfg = R2.from_env()
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"""
        CREATE OR REPLACE SECRET r2_lake (
            TYPE R2, KEY_ID '{cfg.access_key_id}', SECRET '{cfg.secret_access_key}',
            ACCOUNT_ID '{cfg.account_id}', REGION 'auto')""")
    con.execute("SET memory_limit = '9GB'")
    con.execute("SET preserve_insertion_order = false")
    con.execute("SET temp_directory = '/tmp/duckdb'")

    print("Computing reporting vintages ...")
    con.execute(BUILD)
    n = con.execute(
        f"SELECT count(*) FROM read_parquet('{OUT}/*/*.parquet')").fetchone()[0]
    print(f"  {n:,} facts written with vintage flags")

    md = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    md.execute("DROP VIEW IF EXISTS staging.facts_pit")
    md.execute(f"""
        CREATE VIEW staging.facts_pit AS
        SELECT * FROM read_parquet('{OUT}/*/*.parquet', hive_partitioning = true)""")
    rows, companies, lo, hi = md.execute("""
        SELECT count(*), count(DISTINCT cik), min(period_end), max(period_end)
        FROM staging.facts_pit""").fetchone()
    print(f"view  staging.facts_pit  {rows:,} facts, {companies:,} companies, {lo} .. {hi}")

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
