"""Compact tag -> statement lookup.

Which financial statement a tag belongs to is a property of the tag, not of each
individual filing, so it is resolved once here into a small table instead of joining
55 million presentation rows every time facts are processed. A tag that genuinely
appears on two statements (depreciation, share-based compensation) keeps a row for
each, with the filing count that lets the resolver prefer the usual one.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import R2, motherduck_token

BUCKET = "credit-workbench-raw"
LAKE = f"r2://{BUCKET}"
OUT = f"{LAKE}/parquet/derived/tag_statement/data.parquet"


def main() -> None:
    cfg = R2.from_env()
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"""
        CREATE OR REPLACE SECRET r2_lake (
            TYPE R2, KEY_ID '{cfg.access_key_id}', SECRET '{cfg.secret_access_key}',
            ACCOUNT_ID '{cfg.account_id}', REGION 'auto')""")
    con.execute("SET memory_limit = '10GB'")
    con.execute("SET preserve_insertion_order = false")

    print("Aggregating presentation rows to a tag -> statement lookup ...")
    con.execute(f"""
        COPY (
            SELECT tag, stmt, count(*) AS lines,
                   row_number() OVER (PARTITION BY tag ORDER BY count(*) DESC) AS rank
            FROM read_parquet('{LAKE}/parquet/sec/fsn/pre/*/data.parquet',
                              hive_partitioning = true, union_by_name = true)
            WHERE stmt IN ('IS', 'BS', 'CF', 'EQ', 'CI')
            GROUP BY tag, stmt
        ) TO '{OUT}' (FORMAT PARQUET, COMPRESSION ZSTD)""")

    n, tags = con.execute(
        f"SELECT count(*), count(DISTINCT tag) FROM read_parquet('{OUT}')").fetchone()
    print(f"  {n:,} tag/statement pairs across {tags:,} distinct tags")

    md = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    md.execute(f"""
        CREATE OR REPLACE TABLE ref.tag_statement AS
        SELECT tag, stmt AS statement, lines, rank FROM read_parquet('{OUT}')""")
    print(f"table ref.tag_statement  "
          f"{md.execute('SELECT count(*) FROM ref.tag_statement').fetchone()[0]:,} rows")


if __name__ == "__main__":
    main()
