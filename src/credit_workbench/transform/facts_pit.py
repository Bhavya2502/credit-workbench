"""Tracker C4 — de-duplicated, point-in-time fact base.

Three problems solved here, each confirmed against the data rather than assumed:

1. **Duplicate copies inside one filing.** The SEC stores the same figure more than
   once at different precision. Pfizer's FY2025 total assets appear as both
   208,160,000,000 (decimals -6) and 208,000,000,000 (decimals -9). The copies are
   distinguished by `iprx`, and `iprx = 0` is the most precise — that is the one kept.

2. **Consolidated vs dimensioned facts.** Roughly two thirds of facts carry a
   dimension (a segment, a subsidiary, a plan). Spreads need the consolidated figure,
   so only `dimn = 0` rows are taken here; dimensioned facts stay in `raw.fsn_num`
   for the segment work (F1).

3. **Restatement across filings.** The same period gets reported repeatedly. Pfizer
   first reported FY2023 revenue as $58,496m in its Feb-2024 10-K, then $59,553m in
   the Feb-2025 10-K — a $1.06bn restatement. Every fact therefore carries both
   `is_first_report` (what was knowable at the time — what a model must train on) and
   `is_latest` (today's best view — what an analyst reads).

Batching note: work is split by the year the fact *describes*, never by the year it
was filed, so every report of a given period lands in the same batch and the vintage
flags are computed against the complete set.
"""
from __future__ import annotations

import argparse

import duckdb

from credit_workbench.common.config import R2

BUCKET = "credit-workbench-raw"
LAKE = f"r2://{BUCKET}"
OUT = f"{LAKE}/parquet/derived/facts_pit"


def build_sql(lo: int, hi: int) -> str:
    return f"""
COPY (
    WITH sub AS (
        SELECT adsh, period, TRY_CAST(cik AS BIGINT) AS cik, name, sic, form,
               TRY_CAST(strptime(filed, '%Y%m%d') AS DATE) AS filed,
               TRY_CAST(fy AS INTEGER) AS fy, fp, prevrpt
        FROM read_parquet('{LAKE}/parquet/sec/fsn/sub/*/data.parquet',
                          hive_partitioning = true, union_by_name = true)
    ),
    tagstmt AS (
        SELECT tag, statement AS stmt FROM read_parquet(
            '{LAKE}/parquet/derived/tag_statement/data.parquet') WHERE rank = 1
    ),
    facts AS (
        SELECT n.adsh, n.tag, n.version, n.uom,
               TRY_CAST(strptime(n.ddate, '%Y%m%d') AS DATE) AS period_end,
               TRY_CAST(n.qtrs AS INTEGER)  AS qtrs,
               TRY_CAST(n.value AS DOUBLE)  AS value,
               TRY_CAST(n.dcml AS INTEGER)  AS decimals,
               TRY_CAST(n.durp AS DOUBLE)   AS duration_fit,
               TRY_CAST(n.datp AS DOUBLE)   AS date_fit,
               n.period
        FROM read_parquet('{LAKE}/parquet/sec/fsn/num/*/data.parquet',
                          hive_partitioning = true, union_by_name = true) n
        WHERE n.dimn = '0'          -- consolidated only; segments handled in F1
          AND n.iprx = '0'          -- most precise copy of a duplicated figure
          AND n.value IS NOT NULL AND n.value <> ''
          AND TRY_CAST(substr(n.ddate, 1, 4) AS INTEGER) BETWEEN {lo} AND {hi}
    ),
    joined AS (
        SELECT s.cik, s.name AS company_name, s.sic, s.form, s.fy, s.fp, s.filed,
               s.prevrpt, f.adsh, f.tag, f.version, f.uom, f.period_end, f.qtrs,
               f.value, f.decimals, f.duration_fit, f.date_fit, t.stmt,
               year(f.period_end) AS period_year,
               row_number() OVER (
                   PARTITION BY s.cik, f.tag, f.uom, f.period_end, f.qtrs, f.adsh
                   ORDER BY abs(coalesce(f.duration_fit, 0)),
                            abs(coalesce(f.date_fit, 0)),
                            f.decimals) AS pick
        FROM facts f
        JOIN sub s ON s.adsh = f.adsh AND s.period = f.period
        LEFT JOIN tagstmt t ON t.tag = f.tag
    ),
    deduped AS (SELECT * EXCLUDE (pick) FROM joined WHERE pick = 1),
    vintage AS (
        SELECT cik, tag, uom, period_end, qtrs,
               min(filed) AS first_filed, max(filed) AS last_filed,
               count(DISTINCT adsh) AS times_reported
        FROM deduped GROUP BY 1, 2, 3, 4, 5
    )
    SELECT d.*,
           (d.filed = v.first_filed) AS is_first_report,
           (d.filed = v.last_filed)  AS is_latest,
           v.times_reported
    FROM deduped d
    JOIN vintage v USING (cik, tag, uom, period_end, qtrs)
) TO '{OUT}' (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (period_year),
              OVERWRITE_OR_IGNORE, FILENAME_PATTERN 'facts_{lo}_{hi}_{{i}}')
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", required=True, help="period-end years, e.g. 2015-2017")
    args = ap.parse_args()
    lo, _, hi = args.years.partition("-")
    lo, hi = int(lo), int(hi or lo)

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

    print(f"Building point-in-time facts for period-end years {lo}-{hi} ...")
    con.execute(build_sql(lo, hi))
    n = con.execute(f"""
        SELECT count(*) FROM read_parquet('{OUT}/*/facts_{lo}_{hi}_*.parquet')""").fetchone()[0]
    print(f"DONE {lo}-{hi}: {n:,} facts written")


if __name__ == "__main__":
    main()
