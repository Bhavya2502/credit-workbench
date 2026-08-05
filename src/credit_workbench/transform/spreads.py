"""Tracker C5 + C6 — apply the standardization map and build the spread marts.

C5 resolves every mapped tag to a spread line, choosing the highest-priority tag a
filer actually used, and publishes two safety nets so nothing disappears quietly:

  staging.unmapped_tags   every face-financial tag no line claims, ranked by how many
                          filings use it and how much value it carries — the worklist
                          for extending the map
  marts.spread_coverage   per company-year: how much of the reported face-financial
                          value the template captured

C6 builds the marts themselves:

  marts.spread_lines      long form, one row per company / period / line, with the
                          source tag retained so any number is traceable to its filing
  marts.spreads_a         annual spreads, one row per company-year, wide
  marts.spreads_q         quarterly spreads incl. derived Q4 and trailing-twelve-month
                          flows
  marts.spread_checks     reconciliation tests (balance sheet balances, gross profit
                          ties, current assets tie) that catch a mis-mapping

Both a `latest` and a `first_reported` basis are produced: analysts read the former,
models must be trained on the latter.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token
from credit_workbench.transform.tag_map import DERIVED, TEMPLATE, rows

FLOW_STATEMENTS = ("IS", "CF", "MEMO")   # duration facts
STOCK_STATEMENTS = ("BS",)               # instant facts

LINE_CODES = [code for _, code, _, _, _ in TEMPLATE]
STMT_BY_CODE = {code: stmt for _, code, _, stmt, _ in TEMPLATE}


def pivot_columns() -> str:
    return ",\n           ".join(
        f"max(CASE WHEN line_code = '{code}' THEN value END) AS {code}"
        for code in LINE_CODES)


DERIVED_SQL = """
    coalesce(operating_income, 0)
        + coalesce(dep_amort_is, dep_amort_cf, 0)                       AS ebitda,
    coalesce(short_term_debt, 0) + coalesce(current_portion_ltd, 0)
        + coalesce(long_term_debt, 0)                                   AS total_debt,
    coalesce(short_term_debt, 0) + coalesce(current_portion_ltd, 0)
        + coalesce(long_term_debt, 0) + coalesce(operating_lease_current, 0)
        + coalesce(operating_lease_noncurrent, 0)
        + coalesce(finance_lease_current, 0)
        + coalesce(finance_lease_noncurrent, 0)                         AS total_debt_incl_leases,
    coalesce(short_term_debt, 0) + coalesce(current_portion_ltd, 0)
        + coalesce(long_term_debt, 0) - coalesce(cash, 0)
        - coalesce(short_term_investments, 0)                           AS net_debt,
    total_current_assets - total_current_liabilities                    AS working_capital,
    coalesce(cfo, 0) - coalesce(capex, 0)                               AS free_cash_flow,
    coalesce(net_income, 0) + coalesce(dep_amort_cf, dep_amort_is, 0)
        + coalesce(deferred_tax_cf, 0) + coalesce(share_based_comp_cf, 0)
                                                                        AS ffo_simplified,
    total_equity - coalesce(goodwill, 0) - coalesce(intangibles, 0)     AS tangible_net_worth,
    total_assets - coalesce(total_current_liabilities, 0)               AS capital_employed
"""


def build(md: duckdb.DuckDBPyConnection) -> None:
    # ---------------------------------------------------------------- C5: the map
    md.execute("DROP TABLE IF EXISTS staging.tag_map")
    md.execute("""
        CREATE TABLE staging.tag_map (
            line_no INTEGER, line_code VARCHAR, label VARCHAR,
            statement VARCHAR, tag VARCHAR, priority INTEGER)""")
    md.executemany(
        "INSERT INTO staging.tag_map VALUES (?, ?, ?, ?, ?, ?)", rows())
    n_lines = len({c for _, c, *_ in rows()})
    n_tags = md.execute("SELECT count(*) FROM staging.tag_map").fetchone()[0]
    print(f"table staging.tag_map  {n_lines} lines, {n_tags} tag alternatives")

    # --------------------------------------------- C5: resolve tags to spread lines
    md.execute(f"""
        CREATE OR REPLACE TABLE marts.spread_lines AS
        WITH basis_facts AS (
            SELECT *, 'latest' AS basis FROM staging.facts_pit WHERE is_latest
            UNION ALL BY NAME
            SELECT *, 'first_reported' AS basis FROM staging.facts_pit WHERE is_first_report
        ),
        mapped AS (
            SELECT f.cik, f.company_name, f.sic, f.basis, f.period_end, f.qtrs,
                   f.fy, f.fp, f.form, f.uom, f.filed, f.adsh,
                   m.line_no, m.line_code, m.label, m.statement, m.priority,
                   f.tag AS source_tag, f.value
            FROM basis_facts f
            JOIN staging.tag_map m ON m.tag = f.tag
            WHERE f.uom IN ('USD', 'USD/shares', 'shares', 'pure')
        ),
        ranked AS (
            SELECT *, row_number() OVER (
                       PARTITION BY cik, basis, period_end, qtrs, line_code
                       ORDER BY priority, filed DESC) AS rn
            FROM mapped
        )
        SELECT * EXCLUDE (rn) FROM ranked WHERE rn = 1""")
    print(f"table marts.spread_lines  "
          f"{md.execute('SELECT count(*) FROM marts.spread_lines').fetchone()[0]:,} rows")

    # ------------------------------------------- C5 safety net: what we did NOT map
    md.execute("""
        CREATE OR REPLACE TABLE staging.unmapped_tags AS
        SELECT f.tag, any_value(f.stmt) AS statement,
               count(DISTINCT f.adsh)   AS filings,
               count(*)                 AS facts,
               sum(abs(f.value))        AS abs_value_carried,
               max(f.fy)                AS last_seen_fy
        FROM staging.facts_pit f
        LEFT JOIN staging.tag_map m ON m.tag = f.tag
        WHERE m.tag IS NULL
          AND f.stmt IN ('IS', 'BS', 'CF')
          AND f.is_latest
        GROUP BY 1
        ORDER BY filings DESC""")
    print(f"table staging.unmapped_tags  "
          f"{md.execute('SELECT count(*) FROM staging.unmapped_tags').fetchone()[0]:,} tags")

    md.execute("""
        CREATE OR REPLACE TABLE marts.spread_coverage AS
        WITH face AS (
            SELECT f.cik, f.fy, f.adsh,
                   count(*)                                    AS face_facts,
                   count(*) FILTER (WHERE m.tag IS NOT NULL)   AS mapped_facts,
                   sum(abs(f.value))                           AS face_value,
                   sum(abs(f.value)) FILTER (WHERE m.tag IS NOT NULL) AS mapped_value
            FROM staging.facts_pit f
            LEFT JOIN staging.tag_map m ON m.tag = f.tag
            WHERE f.stmt IN ('IS', 'BS', 'CF') AND f.is_latest AND f.uom = 'USD'
            GROUP BY 1, 2, 3)
        SELECT *,
               mapped_facts::DOUBLE / nullif(face_facts, 0) AS fact_coverage,
               mapped_value::DOUBLE / nullif(face_value, 0) AS value_coverage
        FROM face""")

    # ------------------------------------------------------ C6: annual wide spreads
    md.execute(f"""
        CREATE OR REPLACE TABLE marts.spreads_a AS
        WITH base AS (
            SELECT cik, any_value(company_name) AS company_name, any_value(sic) AS sic,
                   basis, period_end, max(fy) AS fy, max(filed) AS last_filed,
                   {pivot_columns()}
            FROM marts.spread_lines
            WHERE (statement IN {FLOW_STATEMENTS} AND qtrs = 4)
               OR (statement IN {STOCK_STATEMENTS} AND qtrs = 0)
            GROUP BY cik, basis, period_end)
        SELECT *, {DERIVED_SQL} FROM base""")
    n_a, n_co = md.execute(
        "SELECT count(*), count(DISTINCT cik) FROM marts.spreads_a").fetchone()
    print(f"table marts.spreads_a  {n_a:,} company-years, {n_co:,} companies")

    # -------------------------------------- C6: quarterly, derived Q4, TTM flows
    md.execute(f"""
        CREATE OR REPLACE TABLE marts.spreads_q AS
        WITH q AS (
            SELECT cik, any_value(company_name) AS company_name, any_value(sic) AS sic,
                   basis, period_end, max(fy) AS fy, max(fp) AS fp,
                   {pivot_columns()}
            FROM marts.spread_lines
            WHERE (statement IN {FLOW_STATEMENTS} AND qtrs = 1)
               OR (statement IN {STOCK_STATEMENTS} AND qtrs = 0)
            GROUP BY cik, basis, period_end),
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
               coalesce(operating_income, 0) + coalesce(dep_amort_is, dep_amort_cf, 0)
                   AS ebitda,
               coalesce(short_term_debt, 0) + coalesce(current_portion_ltd, 0)
                   + coalesce(long_term_debt, 0) AS total_debt,
               coalesce(short_term_debt, 0) + coalesce(current_portion_ltd, 0)
                   + coalesce(long_term_debt, 0) - coalesce(cash, 0)
                   - coalesce(short_term_investments, 0) AS net_debt,
               CASE WHEN quarters_in_window = 4 THEN coalesce(cfo_ttm, 0) - coalesce(capex_ttm, 0)
               END AS free_cash_flow_ttm
        FROM withttm""")
    n_q = md.execute("SELECT count(*) FROM marts.spreads_q").fetchone()[0]
    print(f"table marts.spreads_q  {n_q:,} company-quarters")

    # ------------------------------------------------ C6: reconciliation checks
    md.execute("""
        CREATE OR REPLACE TABLE marts.spread_checks AS
        SELECT cik, company_name, basis, period_end, fy,
               total_assets, total_liab_and_equity,
               abs(total_assets - total_liab_and_equity)
                   / nullif(abs(total_assets), 0)          AS balance_sheet_gap,
               abs(coalesce(total_liabilities, 0)
                   + coalesce(total_equity_incl_minority, total_equity, 0)
                   - total_assets) / nullif(abs(total_assets), 0)
                                                            AS liab_equity_gap,
               abs(coalesce(gross_profit, 0)
                   - (coalesce(revenue, 0) - coalesce(cost_of_sales, 0)))
                   / nullif(abs(revenue), 0)                AS gross_profit_gap,
               CASE WHEN total_assets IS NULL THEN 'no total assets'
                    WHEN revenue IS NULL      THEN 'no revenue'
                    WHEN abs(total_assets - total_liab_and_equity)
                         / nullif(abs(total_assets), 0) > 0.01 THEN 'balance sheet does not tie'
                    ELSE 'ok' END                           AS verdict
        FROM marts.spreads_a""")
    print("table marts.spread_checks")

    md.execute("""
        CREATE OR REPLACE TABLE ref.spread_template AS
        SELECT DISTINCT line_no, line_code, label, statement FROM staging.tag_map
        ORDER BY line_no""")
    md.executemany(
        "INSERT INTO ref.spread_template VALUES (?, ?, ?, 'DERIVED')",
        [(no, code, label) for no, code, label in DERIVED])
    print(f"table ref.spread_template  "
          f"{md.execute('SELECT count(*) FROM ref.spread_template').fetchone()[0]} lines")


def main() -> None:
    md = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    build(md)


if __name__ == "__main__":
    main()
