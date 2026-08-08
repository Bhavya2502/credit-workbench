"""Tracker M2 — validate the spread mapping against the filer's own arithmetic.

Every XBRL filing carries a calculation linkbase: the company's explicit statement of
which tags sum into which subtotal, and with what sign. That makes it the one
authoritative test available of whether our template is complete.

Two outputs, and the second is the more valuable:

  marts.calc_check   does the filer's own arithmetic hold? Declared children summed
                     against the reported parent. A gap means either the filing is
                     internally inconsistent or we have mis-selected a fact.
  staging.map_gaps   for every subtotal our template maps, the components the filer
                     says belong in it that we do NOT map. This replaces guessing from
                     frequency counts with the filer's own declaration — the first
                     sample already surfaced OtherReceivablesNetCurrent,
                     RestrictedCashCurrent, IncomeTaxesReceivable and
                     ContractWithCustomerAssetNetCurrent sitting inside current assets
                     with no home in our map.
"""
from __future__ import annotations

import argparse

import duckdb

from credit_workbench.common.config import R2, motherduck_token
from credit_workbench.transform.tag_map import rows as tag_map_rows

LAKE = "r2://credit-workbench-raw"
PIT = f"{LAKE}/parquet/derived/facts_pit"
OUT = f"{LAKE}/parquet/derived/calc_check"
GAPS = f"{LAKE}/parquet/derived/map_gaps"

# Relative gap beyond which a subtotal is treated as not tying.
TOLERANCE = 0.005


def connect() -> duckdb.DuckDBPyConnection:
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
    return con


def build(lo: int, hi: int) -> None:
    con = connect()
    con.execute("""CREATE OR REPLACE TABLE tag_map (
                       line_no INTEGER, line_code VARCHAR, label VARCHAR,
                       statement VARCHAR, tag VARCHAR, priority INTEGER)""")
    con.executemany("INSERT INTO tag_map VALUES (?, ?, ?, ?, ?, ?)", tag_map_rows())
    tag = f"a{lo}_{hi}"

    common = f"""
        WITH arcs AS (
            -- `negative` carries the calculation weight, +1 or -1
            SELECT DISTINCT adsh, period, ptag, ctag,
                   TRY_CAST(negative AS INTEGER) AS weight
            FROM read_parquet('{LAKE}/parquet/sec/fsn/cal/*/data.parquet',
                              hive_partitioning = true, union_by_name = true)
            WHERE TRY_CAST(substr(period, 1, 4) AS INTEGER) BETWEEN {lo} AND {hi}
              AND ptag IS NOT NULL AND ctag IS NOT NULL),
        facts AS (
            -- Restrict the fact side to the periods these archives can describe.
            -- Without this every batch scans all 150m facts regardless of its year
            -- range, which is what exhausted memory on the two largest batches.
            -- The window reaches back three years because an annual report carries
            -- comparatives, and forward one for early-filed periods.
            SELECT cik, company_name, adsh, tag, period_end, qtrs, uom, value, fy,
                   period_year
            FROM read_parquet('{PIT}/*/*.parquet', hive_partitioning = true)
            WHERE is_latest AND uom = 'USD' AND value IS NOT NULL
              AND period_year BETWEEN {lo} - 3 AND {hi} + 1)
    """

    print(f"Checking filer arithmetic, archives {lo}-{hi} ...")
    con.execute(f"""
        COPY (
            {common},
            parent AS (
                SELECT DISTINCT a.adsh, a.ptag, f.cik, f.company_name, f.period_end,
                       f.qtrs, f.uom, f.fy, f.value AS parent_value, f.period_year
                FROM arcs a JOIN facts f ON f.adsh = a.adsh AND f.tag = a.ptag),
            child AS (
                SELECT a.adsh, a.ptag, f.period_end, f.qtrs, f.uom,
                       sum(a.weight * f.value) AS children_sum,
                       count(*) AS children_found,
                       count(*) FILTER (WHERE m.tag IS NULL) AS children_unmapped
                FROM arcs a
                JOIN facts f ON f.adsh = a.adsh AND f.tag = a.ctag
                LEFT JOIN tag_map m ON m.tag = a.ctag
                GROUP BY 1, 2, 3, 4, 5)
            SELECT p.cik, p.company_name, p.adsh, p.fy, p.period_end, p.qtrs, p.uom,
                   p.ptag AS subtotal_tag, p.parent_value, c.children_sum,
                   c.children_found, c.children_unmapped,
                   p.parent_value - c.children_sum AS gap,
                   abs(p.parent_value - c.children_sum)
                       / nullif(abs(p.parent_value), 0) AS relative_gap,
                   (abs(p.parent_value - c.children_sum)
                       / nullif(abs(p.parent_value), 0) <= {TOLERANCE}) AS ties,
                   p.period_year
            FROM parent p
            JOIN child c ON c.adsh = p.adsh AND c.ptag = p.ptag
                        AND c.period_end = p.period_end AND c.qtrs = p.qtrs
                        AND c.uom = p.uom
        ) TO '{OUT}' (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (period_year),
                      OVERWRITE_OR_IGNORE, FILENAME_PATTERN 'cc_{tag}_{{i}}')""")

    print(f"Finding declared components our map misses, archives {lo}-{hi} ...")
    con.execute(f"""
        COPY (
            {common}
            SELECT a.ptag AS subtotal_tag, a.ctag AS missing_component,
                   any_value(pm.line_code) AS our_line_for_subtotal,
                   count(DISTINCT a.adsh)  AS filings,
                   sum(abs(f.value))       AS abs_value_carried,
                   max(f.fy)               AS last_seen_fy
            FROM arcs a
            -- only subtotals our template actually claims: a component missing from a
            -- subtotal we do not use is not our problem
            JOIN tag_map pm ON pm.tag = a.ptag
            LEFT JOIN tag_map cm ON cm.tag = a.ctag
            LEFT JOIN facts f ON f.adsh = a.adsh AND f.tag = a.ctag
            WHERE cm.tag IS NULL
            GROUP BY 1, 2
        ) TO '{GAPS}/gaps_{tag}.parquet' (FORMAT PARQUET, COMPRESSION ZSTD)""")

    for label, path in (("calc_check", f"{OUT}/*/cc_{tag}_*.parquet"),
                        ("map_gaps", f"{GAPS}/gaps_{tag}.parquet")):
        try:
            n = con.execute(f"SELECT count(*) FROM read_parquet('{path}')").fetchone()[0]
            print(f"DONE {label} {lo}-{hi}: {n:,} rows")
        except duckdb.IOException:
            print(f"DONE {label} {lo}-{hi}: no rows")


def register() -> None:
    md = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    md.execute("DROP VIEW IF EXISTS marts.calc_check")
    md.execute(f"""
        CREATE VIEW marts.calc_check AS SELECT * FROM read_parquet(
            '{OUT}/*/*.parquet', hive_partitioning = true, union_by_name = true)""")
    rows, filings = md.execute(
        "SELECT count(*), count(DISTINCT adsh) FROM marts.calc_check").fetchone()
    print(f"view  marts.calc_check  {rows:,} subtotal checks over {filings:,} filings")

    md.execute(f"""
        CREATE OR REPLACE TABLE staging.map_gaps AS
        SELECT subtotal_tag, missing_component,
               any_value(our_line_for_subtotal) AS our_line_for_subtotal,
               sum(filings) AS filings, sum(abs_value_carried) AS abs_value_carried,
               max(last_seen_fy) AS last_seen_fy
        FROM read_parquet('{GAPS}/*.parquet')
        GROUP BY 1, 2 ORDER BY filings DESC""")
    print(f"table staging.map_gaps  "
          f"{md.execute('SELECT count(*) FROM staging.map_gaps').fetchone()[0]:,} gaps")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archives", default="")
    ap.add_argument("--register", action="store_true")
    args = ap.parse_args()
    if args.register:
        register()
        return
    lo, _, hi = args.archives.partition("-")
    build(int(lo), int(hi or lo))


if __name__ == "__main__":
    main()
