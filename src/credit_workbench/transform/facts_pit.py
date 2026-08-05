"""Tracker C4 — de-duplicated, point-in-time fact base.

Three problems solved here, each confirmed against the data rather than assumed:

1. **Duplicate copies inside one filing.** The SEC stores the same figure more than
   once at different precision. Pfizer's FY2025 total assets appear as both
   208,160,000,000 (dcml -6) and 208,000,000,000 (dcml -9). The copies are
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

Output: parquet in R2, partitioned by fiscal year, exposed as `staging.facts_pit`,
plus `marts.restatements` listing every figure whose value changed after first
publication.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import R2, motherduck_token

BUCKET = "credit-workbench-raw"
LAKE = f"r2://{BUCKET}"
OUT = f"{LAKE}/parquet/derived/facts_pit"

BUILD = f"""
COPY (
    WITH pre_stmt AS (           -- which statement each tag sits on, one row per tag
        SELECT adsh, tag, version, period, min(stmt) AS stmt
        FROM read_parquet('{LAKE}/parquet/sec/fsn/pre/*/data.parquet',
                          hive_partitioning = true, union_by_name = true)
        WHERE stmt IS NOT NULL AND stmt <> ''
        GROUP BY 1, 2, 3, 4
    ),
    sub AS (
        SELECT adsh, period, TRY_CAST(cik AS BIGINT) AS cik, name, sic, form,
               TRY_CAST(strptime(filed, '%Y%m%d') AS DATE) AS filed,
               TRY_CAST(fy AS INTEGER) AS fy, fp, prevrpt,
               TRY_CAST(strptime(period, '%Y%m%d') AS DATE) AS period_reported
        FROM read_parquet('{LAKE}/parquet/sec/fsn/sub/*/data.parquet',
                          hive_partitioning = true, union_by_name = true)
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
    ),
    joined AS (
        SELECT s.cik, s.name AS company_name, s.sic, s.form, s.fy, s.fp, s.filed,
               s.prevrpt, f.adsh, f.tag, f.version, f.uom, f.period_end, f.qtrs,
               f.value, f.decimals, f.duration_fit, f.date_fit,
               p.stmt,
               row_number() OVER (
                   PARTITION BY s.cik, f.tag, f.uom, f.period_end, f.qtrs, f.adsh
                   ORDER BY abs(coalesce(f.duration_fit, 0)),
                            abs(coalesce(f.date_fit, 0)),
                            f.decimals) AS pick
        FROM facts f
        JOIN sub s   ON s.adsh = f.adsh AND s.period = f.period
        LEFT JOIN pre_stmt p ON p.adsh = f.adsh AND p.tag = f.tag
                            AND p.version = f.version AND p.period = f.period
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
) TO '{OUT}' (FORMAT PARQUET, COMPRESSION ZSTD,
              PARTITION_BY (fy), OVERWRITE_OR_IGNORE, FILENAME_PATTERN 'facts_{{i}}')
"""


def main() -> None:
    cfg = R2.from_env()
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"""
        CREATE OR REPLACE SECRET r2_lake (
            TYPE R2, KEY_ID '{cfg.access_key_id}', SECRET '{cfg.secret_access_key}',
            ACCOUNT_ID '{cfg.account_id}', REGION 'auto')""")
    con.execute("SET memory_limit = '12GB'")
    con.execute("SET preserve_insertion_order = false")
    con.execute("SET temp_directory = '/tmp/duckdb'")

    print("Building point-in-time fact base ...")
    con.execute(BUILD)
    print(f"Written to {OUT}")

    n, companies, first_year, last_year = con.execute(f"""
        SELECT count(*), count(DISTINCT cik), min(fy), max(fy)
        FROM read_parquet('{OUT}/*/*.parquet', hive_partitioning = true)""").fetchone()
    print(f"  {n:,} facts, {companies:,} companies, fiscal years {first_year}-{last_year}")

    # Register in the warehouse and build the restatement report.
    md = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    md.execute("DROP VIEW IF EXISTS staging.facts_pit")
    md.execute(f"""
        CREATE VIEW staging.facts_pit AS
        SELECT * FROM read_parquet('{OUT}/*/*.parquet', hive_partitioning = true)""")
    print("view  staging.facts_pit")

    md.execute("""
        CREATE OR REPLACE TABLE marts.restatements AS
        WITH f AS (
            SELECT cik, company_name, tag, uom, period_end, qtrs,
                   max(CASE WHEN is_first_report THEN value END)  AS first_reported,
                   max(CASE WHEN is_latest       THEN value END)  AS latest_value,
                   min(CASE WHEN is_first_report THEN filed END)  AS first_filed,
                   max(CASE WHEN is_latest       THEN filed END)  AS latest_filed,
                   any_value(times_reported) AS times_reported
            FROM staging.facts_pit
            WHERE times_reported > 1 AND qtrs IN (0, 4)
            GROUP BY 1, 2, 3, 4, 5, 6)
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
