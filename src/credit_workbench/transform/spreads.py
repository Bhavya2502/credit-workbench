"""Tracker C5 + C6 — apply the standardization map and build the spread marts.

C5 resolves every mapped tag to a spread line, choosing the highest-priority tag a
filer actually used, and publishes two safety nets so nothing disappears quietly:

  staging.unmapped_tags   every face-financial tag no line claims, ranked by how many
                          filings use it and how much value it carries — the worklist
                          for extending the map
  marts.spread_coverage   per company-year: how much of the reported face-financial
                          value the template captured

C6 builds the marts:

  marts.spread_lines      long form, one row per company / period / line, with the
                          source tag kept so any number is traceable to its filing
  marts.spreads_a         annual spreads, one row per company-year, wide
  marts.spreads_q         quarterly spreads incl. trailing-twelve-month flows
  marts.spread_checks     reconciliation tests (balance sheet balances, gross profit
                          ties) that catch a mis-mapping

Both a `latest` and a `first_reported` basis are produced: analysts read the former,
models must be trained on the latter.

Runs on a cloud runner against the R2 lake, writing results back as parquet; only the
final, small marts are materialised in the warehouse. The fact base is 151m rows, far
too much to pull through the warehouse repeatedly.

  --part lines --years 2015-2017   resolve tags to lines for those period years
  --part marts                     build wide marts, checks, coverage, unmapped list
"""
from __future__ import annotations

import argparse

import duckdb

from credit_workbench.common.config import R2, motherduck_token
from credit_workbench.transform.tag_map import DERIVED, TEMPLATE, rows

BUCKET = "credit-workbench-raw"
LAKE = f"r2://{BUCKET}"
PIT = f"{LAKE}/parquet/derived/facts_pit"
LINES = f"{LAKE}/parquet/derived/spread_lines"
MARTS = f"{LAKE}/parquet/derived/marts"

LINE_CODES = [code for _, code, _, _, _ in TEMPLATE]
USABLE_UOM = "('USD', 'USD/shares', 'shares', 'pure')"


def period_filter(flow_qtrs: int) -> str:
    """Which facts belong in a spread period.

    Income statement and cash flow are duration facts (4 quarters for an annual
    column, 1 for a quarterly one); the balance sheet is an instant. Memo items are
    mixed — a pension obligation is an instant, interest paid is a duration — so both
    shapes are admitted for those.
    """
    return (f"(statement IN ('IS', 'CF') AND qtrs = {flow_qtrs})"
            f" OR (statement = 'BS' AND qtrs = 0)"
            f" OR (statement = 'MEMO' AND qtrs IN (0, {flow_qtrs}))")


def pivot_columns() -> str:
    return ",\n           ".join(
        f"max(CASE WHEN line_code = '{code}' THEN value END) AS {code}"
        for code in LINE_CODES)


# Derived lines are deliberately NULL-safe: a metric is only produced when its
# defining input exists. Summing coalesce(x, 0) terms would quietly turn a missing
# operating income into an "EBITDA" that is really just depreciation — a plausible
# looking number that is wrong, which is worse than a blank.
DERIVED_SQL = """
    CASE WHEN gross_profit IS NOT NULL THEN gross_profit
         WHEN revenue IS NOT NULL AND cost_of_sales IS NOT NULL
         THEN revenue - cost_of_sales END                               AS gross_profit_calc,
    CASE WHEN operating_income IS NOT NULL THEN operating_income
         WHEN revenue IS NOT NULL AND total_operating_expenses IS NOT NULL
         THEN revenue - total_operating_expenses
         WHEN revenue IS NOT NULL AND cost_of_sales IS NOT NULL
         THEN revenue - cost_of_sales - coalesce(sgna, 0)
              - coalesce(selling_marketing, 0) - coalesce(general_admin, 0)
              - coalesce(research_development, 0) END                   AS ebit_calc,
    CASE WHEN operating_income IS NOT NULL
         THEN operating_income + coalesce(dep_amort_is, dep_amort_cf, 0) END AS ebitda,
    CASE WHEN coalesce(short_term_debt, current_portion_ltd, long_term_debt) IS NOT NULL
         THEN coalesce(short_term_debt, 0) + coalesce(current_portion_ltd, 0)
              + coalesce(long_term_debt, 0) END                         AS total_debt,
    CASE WHEN coalesce(short_term_debt, current_portion_ltd, long_term_debt,
                       operating_lease_current, operating_lease_noncurrent,
                       finance_lease_current, finance_lease_noncurrent) IS NOT NULL
         THEN coalesce(short_term_debt, 0) + coalesce(current_portion_ltd, 0)
              + coalesce(long_term_debt, 0) + coalesce(operating_lease_current, 0)
              + coalesce(operating_lease_noncurrent, 0)
              + coalesce(finance_lease_current, 0)
              + coalesce(finance_lease_noncurrent, 0) END               AS total_debt_incl_leases,
    CASE WHEN coalesce(short_term_debt, current_portion_ltd, long_term_debt) IS NOT NULL
         THEN coalesce(short_term_debt, 0) + coalesce(current_portion_ltd, 0)
              + coalesce(long_term_debt, 0) - coalesce(cash, 0)
              - coalesce(short_term_investments, 0) END                 AS net_debt,
    total_current_assets - total_current_liabilities                    AS working_capital,
    CASE WHEN cfo IS NOT NULL THEN cfo - coalesce(capex, 0) END         AS free_cash_flow,
    CASE WHEN net_income IS NOT NULL
         THEN net_income + coalesce(dep_amort_cf, dep_amort_is, 0)
              + coalesce(deferred_tax_cf, 0)
              + coalesce(share_based_comp_cf, 0) END                    AS ffo_simplified,
    CASE WHEN total_equity IS NOT NULL
         THEN total_equity - coalesce(goodwill, 0)
              - coalesce(intangibles, 0) END                            AS tangible_net_worth,
    total_assets - coalesce(total_current_liabilities, 0)               AS capital_employed
"""


def connect_lake() -> duckdb.DuckDBPyConnection:
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


def load_tag_map(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""CREATE OR REPLACE TABLE tag_map (
                       line_no INTEGER, line_code VARCHAR, label VARCHAR,
                       statement VARCHAR, tag VARCHAR, priority INTEGER)""")
    con.executemany("INSERT INTO tag_map VALUES (?, ?, ?, ?, ?, ?)", rows())


# --------------------------------------------------------------------- C5: lines
def build_lines(years: list[int]) -> None:
    con = connect_lake()
    load_tag_map(con)
    globs = ", ".join(f"'{PIT}/period_year={y}/*.parquet'" for y in years)
    tag = f"lines{years[0]}_{years[-1]}"

    try:
        con.execute(f"""
            COPY (
                WITH base AS (
                    -- Reduce before expanding: join the map first so only mapped
                    -- facts are ever duplicated across the two bases.
                    SELECT f.cik, f.company_name, f.sic, f.period_end, f.qtrs, f.fy,
                           f.fp, f.form, f.uom, f.filed, f.adsh, f.stmt, f.period_year,
                           f.is_latest, f.is_first_report,
                           m.line_no, m.line_code, m.label, m.statement, m.priority,
                           f.tag AS source_tag, f.value
                    FROM read_parquet([{globs}], hive_partitioning = true,
                                      union_by_name = true) f
                    JOIN tag_map m ON m.tag = f.tag
                    WHERE f.uom IN {USABLE_UOM}
                      AND (f.is_latest OR f.is_first_report)
                ),
                expanded AS (
                    SELECT * EXCLUDE (is_latest, is_first_report), 'latest' AS basis
                    FROM base WHERE is_latest
                    UNION ALL BY NAME
                    SELECT * EXCLUDE (is_latest, is_first_report),
                           'first_reported' AS basis
                    FROM base WHERE is_first_report
                )
                SELECT * FROM expanded
                -- A few tags belong to two lines (depreciation sits on both the
                -- income statement and the cash flow statement). Prefer the copy
                -- whose statement matches the line, then the tag priority.
                QUALIFY row_number() OVER (
                    PARTITION BY cik, basis, period_end, qtrs, line_code
                    ORDER BY CASE WHEN stmt = statement THEN 0 ELSE 1 END,
                             priority, filed DESC) = 1
            ) TO '{LINES}' (FORMAT PARQUET, COMPRESSION ZSTD,
                            PARTITION_BY (period_year), OVERWRITE_OR_IGNORE,
                            FILENAME_PATTERN '{tag}_{{i}}')""")
    except duckdb.IOException as exc:
        if "No files found" not in str(exc):
            raise
        print("  no input for these years, nothing to do")
        return

    try:
        n = con.execute(
            f"SELECT count(*) FROM read_parquet('{LINES}/*/{tag}_*.parquet')").fetchone()[0]
    except duckdb.IOException:
        raise SystemExit(f"FAILED {years[0]}-{years[-1]}: no rows produced.")
    print(f"DONE lines {years[0]}-{years[-1]}: {n:,} resolved spread lines")


# ------------------------------------------------------------- C5/C6: marts
def build_marts() -> None:
    con = connect_lake()
    load_tag_map(con)

    print("Building wide annual spreads ...")
    con.execute(f"""
        COPY (
            WITH lines AS (
                SELECT * FROM read_parquet('{LINES}/*/*.parquet',
                                           hive_partitioning = true,
                                           union_by_name = true)),
            -- An annual column exists only where the company actually reported a
            -- full-year flow. Balance sheets are tagged at every quarter end, so
            -- keying on period alone would invent an "annual" row for each quarter
            -- with no income statement attached.
            annual_periods AS (
                SELECT DISTINCT cik, basis, period_end FROM lines
                WHERE statement IN ('IS', 'CF') AND qtrs = 4),
            base AS (
                SELECT l.cik, any_value(l.company_name) AS company_name,
                       any_value(l.sic) AS sic, l.basis, l.period_end,
                       -- Label the year from the period itself. The filing's own fy
                       -- refers to the year of the report, so a 2024 column read from
                       -- a 2026 annual report would be labelled 2026.
                       CASE WHEN month(l.period_end) <= 5 THEN year(l.period_end) - 1
                            ELSE year(l.period_end) END AS fy,
                       max(l.filed) AS last_filed,
                       {pivot_columns()}
                FROM lines l JOIN annual_periods p
                  ON p.cik = l.cik AND p.basis = l.basis AND p.period_end = l.period_end
                WHERE {period_filter(4)}
                GROUP BY l.cik, l.basis, l.period_end)
            SELECT *, {DERIVED_SQL} FROM base
        ) TO '{MARTS}/spreads_a.parquet' (FORMAT PARQUET, COMPRESSION ZSTD)""")

    print("Building wide quarterly spreads ...")
    con.execute(f"""
        COPY (
            WITH lines AS (
                SELECT * FROM read_parquet('{LINES}/*/*.parquet',
                                           hive_partitioning = true,
                                           union_by_name = true)),
            quarter_periods AS (
                SELECT DISTINCT cik, basis, period_end FROM lines
                WHERE statement IN ('IS', 'CF') AND qtrs = 1),
            q AS (
                SELECT l.cik, any_value(l.company_name) AS company_name,
                       any_value(l.sic) AS sic, l.basis, l.period_end,
                       CASE WHEN month(l.period_end) <= 5 THEN year(l.period_end) - 1
                            ELSE year(l.period_end) END AS fy,
                       max(l.fp) AS fp,
                       {pivot_columns()}
                FROM lines l JOIN quarter_periods p
                  ON p.cik = l.cik AND p.basis = l.basis AND p.period_end = l.period_end
                WHERE {period_filter(1)}
                GROUP BY l.cik, l.basis, l.period_end),
            withttm AS (
                SELECT *,
                       sum(revenue) OVER w4          AS revenue_ttm,
                       sum(operating_income) OVER w4 AS operating_income_ttm,
                       sum(net_income) OVER w4       AS net_income_ttm,
                       sum(cfo) OVER w4              AS cfo_ttm,
                       sum(capex) OVER w4            AS capex_ttm,
                       count(*) OVER w4              AS quarters_in_window
                FROM q
                WINDOW w4 AS (PARTITION BY cik, basis ORDER BY period_end
                              ROWS BETWEEN 3 PRECEDING AND CURRENT ROW))
            SELECT *,
                   CASE WHEN operating_income IS NOT NULL
                        THEN operating_income
                             + coalesce(dep_amort_is, dep_amort_cf, 0) END AS ebitda,
                   CASE WHEN coalesce(short_term_debt, current_portion_ltd,
                                      long_term_debt) IS NOT NULL
                        THEN coalesce(short_term_debt, 0)
                             + coalesce(current_portion_ltd, 0)
                             + coalesce(long_term_debt, 0) END              AS total_debt,
                   CASE WHEN coalesce(short_term_debt, current_portion_ltd,
                                      long_term_debt) IS NOT NULL
                        THEN coalesce(short_term_debt, 0)
                             + coalesce(current_portion_ltd, 0)
                             + coalesce(long_term_debt, 0) - coalesce(cash, 0)
                             - coalesce(short_term_investments, 0) END      AS net_debt,
                   CASE WHEN quarters_in_window = 4
                        THEN coalesce(cfo_ttm, 0) - coalesce(capex_ttm, 0) END
                       AS free_cash_flow_ttm
            FROM withttm
        ) TO '{MARTS}/spreads_q.parquet' (FORMAT PARQUET, COMPRESSION ZSTD)""")

    print("Building coverage and the unmapped-tag worklist ...")
    con.execute(f"""
        COPY (
            SELECT f.tag, any_value(f.stmt) AS statement,
                   count(DISTINCT f.adsh)   AS filings,
                   count(*)                 AS facts,
                   sum(abs(f.value))        AS abs_value_carried,
                   max(f.fy)                AS last_seen_fy
            FROM read_parquet('{PIT}/*/*.parquet', hive_partitioning = true,
                              union_by_name = true) f
            LEFT JOIN tag_map m ON m.tag = f.tag
            WHERE m.tag IS NULL AND f.stmt IN ('IS', 'BS', 'CF') AND f.is_latest
            GROUP BY 1 ORDER BY filings DESC
        ) TO '{MARTS}/unmapped_tags.parquet' (FORMAT PARQUET, COMPRESSION ZSTD)""")

    con.execute(f"""
        COPY (
            WITH face AS (
                SELECT f.cik, f.fy, f.adsh,
                       count(*)                                   AS face_facts,
                       count(*) FILTER (WHERE m.tag IS NOT NULL)  AS mapped_facts,
                       sum(abs(f.value))                          AS face_value,
                       sum(abs(f.value)) FILTER (WHERE m.tag IS NOT NULL)
                                                                  AS mapped_value
                FROM read_parquet('{PIT}/*/*.parquet', hive_partitioning = true,
                                  union_by_name = true) f
                LEFT JOIN tag_map m ON m.tag = f.tag
                WHERE f.stmt IN ('IS', 'BS', 'CF') AND f.is_latest AND f.uom = 'USD'
                GROUP BY 1, 2, 3)
            SELECT *, mapped_facts::DOUBLE / nullif(face_facts, 0) AS fact_coverage,
                   mapped_value::DOUBLE / nullif(face_value, 0)    AS value_coverage
            FROM face
        ) TO '{MARTS}/spread_coverage.parquet' (FORMAT PARQUET, COMPRESSION ZSTD)""")

    register(con)


def register(con: duckdb.DuckDBPyConnection) -> None:
    md = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")

    md.execute("DROP TABLE IF EXISTS staging.tag_map")
    md.execute("""CREATE TABLE staging.tag_map (
                      line_no INTEGER, line_code VARCHAR, label VARCHAR,
                      statement VARCHAR, tag VARCHAR, priority INTEGER)""")
    md.executemany("INSERT INTO staging.tag_map VALUES (?, ?, ?, ?, ?, ?)", rows())
    print(f"table staging.tag_map  {len(LINE_CODES)} lines, {len(rows())} alternatives")

    md.execute("DROP VIEW IF EXISTS marts.spread_lines")
    md.execute(f"""
        CREATE VIEW marts.spread_lines AS
        SELECT * FROM read_parquet('{LINES}/*/*.parquet', hive_partitioning = true,
                                   union_by_name = true)""")
    print("view  marts.spread_lines")

    for name, path in (("marts.spreads_a", f"{MARTS}/spreads_a.parquet"),
                       ("marts.spreads_q", f"{MARTS}/spreads_q.parquet"),
                       ("staging.unmapped_tags", f"{MARTS}/unmapped_tags.parquet"),
                       ("marts.spread_coverage", f"{MARTS}/spread_coverage.parquet")):
        md.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM read_parquet('{path}')")
        print(f"table {name}  "
              f"{md.execute(f'SELECT count(*) FROM {name}').fetchone()[0]:,} rows")

    md.execute("""
        CREATE OR REPLACE TABLE marts.spread_checks AS
        SELECT cik, company_name, basis, period_end, fy,
               total_assets, total_liab_and_equity,
               abs(total_assets - total_liab_and_equity)
                   / nullif(abs(total_assets), 0)          AS balance_sheet_gap,
               abs(coalesce(total_liabilities, 0)
                   + coalesce(total_equity_incl_minority, total_equity, 0)
                   - total_assets) / nullif(abs(total_assets), 0) AS liab_equity_gap,
               abs(coalesce(gross_profit, 0)
                   - (coalesce(revenue, 0) - coalesce(cost_of_sales, 0)))
                   / nullif(abs(revenue), 0)                AS gross_profit_gap,
               CASE WHEN total_assets IS NULL THEN 'no total assets'
                    WHEN revenue IS NULL      THEN 'no revenue'
                    WHEN abs(total_assets - total_liab_and_equity)
                         / nullif(abs(total_assets), 0) > 0.01
                         THEN 'balance sheet does not tie'
                    ELSE 'ok' END                           AS verdict
        FROM marts.spreads_a""")
    print("table marts.spread_checks")

    md.execute("""
        CREATE OR REPLACE TABLE ref.spread_template AS
        SELECT DISTINCT line_no, line_code, label, statement FROM staging.tag_map""")
    md.executemany("INSERT INTO ref.spread_template VALUES (?, ?, ?, 'DERIVED')",
                   [(no, code, label) for no, code, label in DERIVED])
    print(f"table ref.spread_template  "
          f"{md.execute('SELECT count(*) FROM ref.spread_template').fetchone()[0]} lines")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", choices=["lines", "marts"], required=True)
    ap.add_argument("--years", default="")
    args = ap.parse_args()
    if args.part == "lines":
        lo, _, hi = args.years.partition("-")
        build_lines(list(range(int(lo), int(hi or lo) + 1)))
    else:
        build_marts()


if __name__ == "__main__":
    main()
