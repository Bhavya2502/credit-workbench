"""Tracker C4 stage 2 — reporting vintages.

The same period is reported repeatedly: Pfizer first reported FY2023 revenue as
$58,496m in its Feb-2024 10-K, then $59,553m in the Feb-2025 10-K — a $1.06bn
restatement. Every fact therefore carries `is_first_report` (what was knowable at the
time, which a model must train on) and `is_latest` (today's best view, which an
analyst reads).

Every copy of a figure describes the same period, and stage 1 partitions its output by
that period — so a partition holds all copies of every figure it contains and none of
anyone else's. Vintages can therefore be computed one partition at a time with no
cross-partition dependency, which is what keeps this affordable: a single pass over
all 151m facts exhausted the runner's 12.6 GB of spill space.
"""
from __future__ import annotations

import argparse

import duckdb

from credit_workbench.common.config import R2

BUCKET = "credit-workbench-raw"
LAKE = f"r2://{BUCKET}"
DEDUP = f"{LAKE}/parquet/derived/facts_dedup"
OUT = f"{LAKE}/parquet/derived/facts_pit"


def build_sql(years: list[int], tag: str) -> str:
    globs = ", ".join(f"'{DEDUP}/period_year={y}/*.parquet'" for y in years)
    return f"""
COPY (
    WITH d AS (
        -- coreg (co-registrant) is empty for almost every fact. It is part of the
        -- identity of a figure, but joining on a NULL never matches, so the key is
        -- built from a coalesced copy and the original column is preserved.
        SELECT *, coalesce(coreg, '') AS coreg_key
        FROM read_parquet([{globs}], hive_partitioning = true, union_by_name = true)
        WHERE period_end IS NOT NULL AND cik IS NOT NULL AND qtrs IS NOT NULL
    ),
    vintage AS (
        SELECT cik, tag, uom, coreg_key, period_end, qtrs,
               min(filed) AS first_filed, max(filed) AS last_filed,
               count(DISTINCT adsh) AS times_reported
        FROM d GROUP BY 1, 2, 3, 4, 5, 6
    )
    SELECT d.* EXCLUDE (coreg_key),
           (d.filed = v.first_filed) AS is_first_report,
           (d.filed = v.last_filed)  AS is_latest,
           v.times_reported
    FROM d JOIN vintage v USING (cik, tag, uom, coreg_key, period_end, qtrs)
) TO '{OUT}' (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (period_year),
              OVERWRITE_OR_IGNORE, FILENAME_PATTERN '{tag}_{{i}}')
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", required=True, help="period years, e.g. 2015-2017")
    args = ap.parse_args()
    lo, _, hi = args.years.partition("-")
    years = list(range(int(lo), int(hi or lo) + 1))

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

    tag = f"pit{years[0]}_{years[-1]}"
    print(f"Computing vintages for period years {years[0]}-{years[-1]} ...")
    try:
        con.execute(build_sql(years, tag))
    except duckdb.IOException as exc:
        if "No files found" not in str(exc):
            raise
        print("  no input for these years, nothing to do")
        return

    try:
        n = con.execute(
            f"SELECT count(*) FROM read_parquet('{OUT}/*/{tag}_*.parquet')").fetchone()[0]
    except duckdb.IOException:
        # No output files means the query returned no rows. Say so plainly: a silent
        # empty result is nearly always a join that dropped everything.
        raise SystemExit(
            f"FAILED {years[0]}-{years[-1]}: the transform produced no rows, so no "
            f"files were written. Check the join keys for nullable columns.")
    print(f"DONE {years[0]}-{years[-1]}: {n:,} facts with vintage flags")


if __name__ == "__main__":
    main()
