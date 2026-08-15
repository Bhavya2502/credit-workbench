"""Tracker F1 + F2 — segment breakdowns and concentration disclosures.

Both live in the *dimensioned* facts, which the spread deliberately excludes: a fact
carrying `BusinessSegments=RetailMember` is one slice of revenue, not consolidated
revenue, and adding it to the face figures would double-count. The dimension text
sits in `raw.fsn_dim.segments` as `Axis=Member;` pairs, joined to facts by a hash.

F1 marts.segments      revenue, profit and assets by business segment, product and
                       geography — the concentration of earnings a credit analyst
                       needs before believing a consolidated margin
F2 marts.concentration customer, supplier and geographic concentration percentages.
                       The tag is `ConcentrationRiskPercentage1`, not the more obvious
                       `ConcentrationRiskPercentage`, which carries only a twentieth
                       of the disclosures.

Batched by source archive so each is read once.
"""
from __future__ import annotations

import argparse

import duckdb

from credit_workbench.common.config import R2, motherduck_token

LAKE = "r2://credit-workbench-raw"
SEG_OUT = f"{LAKE}/parquet/derived/segments"
CONC_OUT = f"{LAKE}/parquet/derived/concentration"

# Axis names appear in `segments` with the "Axis" suffix stripped.
SEGMENT_AXES = ("BusinessSegments", "ProductOrService", "StatementGeographical",
                "Geographical", "ConsolidationItems", "StatementBusinessSegments")
MEASURE_TAGS = (
    "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
    "OperatingIncomeLoss", "GrossProfit", "Assets", "NetIncomeLoss",
    "DepreciationDepletionAndAmortization", "CapitalExpenditures",
    "PaymentsToAcquirePropertyPlantAndEquipment", "ProfitLoss")
CONC_TAGS = ("ConcentrationRiskPercentage1", "ConcentrationRiskPercentage",
             "GrossProfitConcentrationRiskPercentage",
             "ConcentrationRiskThresholdPercentage")


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


def sources(lo: int, hi: int) -> str:
    """Facts joined to their dimension text, for archives in [lo, hi]."""
    quoted = ", ".join(f"'{t}'" for t in MEASURE_TAGS + CONC_TAGS)
    return f"""
        WITH sub AS (
            SELECT adsh, period, TRY_CAST(cik AS BIGINT) AS cik, name, sic, form,
                   TRY_CAST(strptime(filed, '%Y%m%d') AS DATE) AS filed,
                   TRY_CAST(fy AS INTEGER) AS fy, fp
            FROM read_parquet('{LAKE}/parquet/sec/fsn/sub/*/data.parquet',
                              hive_partitioning = true, union_by_name = true)
            WHERE TRY_CAST(substr(period, 1, 4) AS INTEGER) BETWEEN {lo} AND {hi}),
        facts AS (
            SELECT n.adsh, n.period, n.tag, n.dimh, n.uom,
                   TRY_CAST(strptime(n.ddate, '%Y%m%d') AS DATE) AS period_end,
                   TRY_CAST(n.qtrs AS INTEGER) AS qtrs,
                   TRY_CAST(n.value AS DOUBLE) AS value
            FROM read_parquet('{LAKE}/parquet/sec/fsn/num/*/data.parquet',
                              hive_partitioning = true, union_by_name = true) n
            WHERE TRY_CAST(substr(n.period, 1, 4) AS INTEGER) BETWEEN {lo} AND {hi}
              AND n.dimn <> '0' AND n.iprx = '0'
              AND n.value IS NOT NULL AND n.value <> ''
              AND n.tag IN ({quoted})),
        dims AS (
            SELECT dimhash, period, segments FROM read_parquet(
                '{LAKE}/parquet/sec/fsn/dim/*/data.parquet',
                hive_partitioning = true, union_by_name = true)
            WHERE TRY_CAST(substr(period, 1, 4) AS INTEGER) BETWEEN {lo} AND {hi}
              AND segments IS NOT NULL AND segments <> ''),
        joined AS (
            SELECT s.cik, s.name AS company_name, s.sic, s.form, s.fy, s.filed,
                   f.adsh, f.tag, f.uom, f.period_end, f.qtrs, f.value, d.segments,
                   year(f.period_end) AS period_year
            FROM facts f
            JOIN sub s  ON s.adsh = f.adsh AND s.period = f.period
            JOIN dims d ON d.dimhash = f.dimh AND d.period = f.period)
    """


def build(lo: int, hi: int) -> None:
    con = connect()
    tag = f"a{lo}_{hi}"

    print(f"F1 segments, archives {lo}-{hi} ...")
    axes = ", ".join(f"'{a}'" for a in SEGMENT_AXES)
    measures = ", ".join(f"'{t}'" for t in MEASURE_TAGS)
    con.execute(f"""
        COPY (
            {sources(lo, hi)}
            SELECT j.* EXCLUDE (segments),
                   split_part(pair, '=', 1) AS axis,
                   split_part(pair, '=', 2) AS member,
                   j.segments AS full_dimension
            FROM joined j,
                 UNNEST(str_split(rtrim(j.segments, ';'), ';')) AS t(pair)
            WHERE j.tag IN ({measures})
              AND split_part(pair, '=', 1) IN ({axes})
              AND split_part(pair, '=', 2) <> ''
        ) TO '{SEG_OUT}' (FORMAT PARQUET, COMPRESSION ZSTD,
                          PARTITION_BY (period_year), OVERWRITE_OR_IGNORE,
                          FILENAME_PATTERN 'seg_{tag}_{{i}}')""")

    print(f"F2 concentration, archives {lo}-{hi} ...")
    conc = ", ".join(f"'{t}'" for t in CONC_TAGS)
    con.execute(f"""
        COPY (
            {sources(lo, hi)}
            SELECT j.* EXCLUDE (segments),
                   j.segments AS full_dimension,
                   -- the benchmark says what the percentage is a share OF (revenue,
                   -- receivables); the type says which risk it describes
                   regexp_extract(j.segments, 'ConcentrationRiskByBenchmark=([^;]+)', 1)
                       AS benchmark,
                   regexp_extract(j.segments, 'ConcentrationRiskByType=([^;]+)', 1)
                       AS risk_type,
                   coalesce(
                       nullif(regexp_extract(j.segments, 'MajorCustomers=([^;]+)', 1), ''),
                       nullif(regexp_extract(j.segments, 'Customer=([^;]+)', 1), ''),
                       nullif(regexp_extract(j.segments, 'CounterpartyName=([^;]+)', 1), ''))
                       AS counterparty
            FROM joined j
            WHERE j.tag IN ({conc})
        ) TO '{CONC_OUT}' (FORMAT PARQUET, COMPRESSION ZSTD,
                           PARTITION_BY (period_year), OVERWRITE_OR_IGNORE,
                           FILENAME_PATTERN 'conc_{tag}_{{i}}')""")

    for label, path, pattern in (("segments", SEG_OUT, f"seg_{tag}"),
                                 ("concentration", CONC_OUT, f"conc_{tag}")):
        try:
            n = con.execute(
                f"SELECT count(*) FROM read_parquet('{path}/*/{pattern}_*.parquet')"
            ).fetchone()[0]
            print(f"DONE {label} {lo}-{hi}: {n:,} rows")
        except duckdb.IOException:
            print(f"DONE {label} {lo}-{hi}: no rows")


def register() -> None:
    md = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    md.execute("DROP VIEW IF EXISTS marts.segments")
    md.execute(f"""
        CREATE VIEW marts.segments_all_vintages AS SELECT * FROM read_parquet(
            '{SEG_OUT}/*/*.parquet', hive_partitioning = true, union_by_name = true)""")
    md.execute("DROP VIEW IF EXISTS marts.concentration")
    md.execute(f"""
        CREATE VIEW marts.concentration_all_vintages AS SELECT * FROM read_parquet(
            '{CONC_OUT}/*/*.parquet', hive_partitioning = true, union_by_name = true)""")

    # Segments carried no vintage flag, unlike the fact tables, so a naive sum counted
    # every restatement: Intel's FY2024 Client Computing revenue came out at $338bn
    # against a reported $30.29bn - an elevenfold overstatement that looks like a real
    # number. Both marts now carry the same is_latest / is_first_report contract the
    # rest of the warehouse uses. Materialised rather than computed in a view, so a
    # query does not pay for the window sort every time.
    # The two marts are shaped differently - segments is sliced by axis and member,
    # concentration by counterparty and benchmark - so each needs its own key. Applying
    # one key to both is what broke the first attempt.
    for target, source, slice_cols in (
            ("marts.segments", "marts.segments_all_vintages", ("member", "axis")),
            ("marts.concentration", "marts.concentration_all_vintages",
             ("counterparty", "benchmark"))):
        slices = ", ".join(f"coalesce({c}, '')" for c in slice_cols)
        md.execute(f"DROP VIEW IF EXISTS {target}")
        md.execute(f"DROP TABLE IF EXISTS {target}")
        md.execute(f"""
            CREATE TABLE {target} AS
            SELECT *,
                   filed = max(filed) OVER w AS is_latest,
                   filed = min(filed) OVER w AS is_first_report,
                   count(*) OVER w AS filings_reporting
            FROM {source}
            WINDOW w AS (PARTITION BY cik, period_end, qtrs, tag, uom, {slices})""")
        n, latest = md.execute(
            f"SELECT count(*), count(*) FILTER (WHERE is_latest) FROM {target}"
        ).fetchone()
        print(f"table {target}  {n:,} rows, {latest:,} latest vintage "
              f"({100 * latest / max(n, 1):.0f}%)")

    for name in ("marts.segments_all_vintages", "marts.concentration_all_vintages"):
        rows, companies = md.execute(
            f"SELECT count(*), count(DISTINCT cik) FROM {name}").fetchone()
        print(f"view  {name}  {rows:,} rows, {companies:,} companies")

    # A compact per-company-year view of segment concentration: how much of revenue
    # the largest business segment represents.
    md.execute("""
        CREATE OR REPLACE TABLE marts.segment_concentration AS
        WITH seg AS (
            SELECT cik, any_value(company_name) AS company_name, fy, period_end,
                   member, sum(value) AS segment_revenue
            FROM marts.segments
            WHERE axis IN ('BusinessSegments', 'StatementBusinessSegments')
              AND tag LIKE 'Revenue%' AND qtrs = 4 AND uom = 'USD' AND value > 0
              -- One vintage per figure. The share would partly self-cancel without
              -- this, but only if every segment were restated the same number of
              -- times, which is not something to rely on.
              AND is_latest
            GROUP BY cik, fy, period_end, member),
        tot AS (
            SELECT cik, fy, period_end, sum(segment_revenue) AS total_segment_revenue,
                   count(*) AS n_segments,
                   max(segment_revenue) AS largest_segment_revenue
            FROM seg GROUP BY 1, 2, 3)
        SELECT t.*, any_value(s.company_name) AS company_name,
               t.largest_segment_revenue / nullif(t.total_segment_revenue, 0)
                   AS largest_segment_share
        FROM tot t JOIN seg s USING (cik, fy, period_end)
        GROUP BY t.cik, t.fy, t.period_end, t.total_segment_revenue, t.n_segments,
                 t.largest_segment_revenue""")
    print(f"table marts.segment_concentration  "
          f"{md.execute('SELECT count(*) FROM marts.segment_concentration').fetchone()[0]:,} rows")


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
