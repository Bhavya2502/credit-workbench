"""Tracker E1 + E2 — the ratio library and industry benchmarks.

E1 computes 46 credit ratios and 7 distress flags for every company-year.
E2 turns the full filer population into peer distributions, then places each company
against them.

The benchmark is computed from the whole population rather than bought in, which is
the advantage of holding every filer: percentiles are the real distribution of the
industry in that year, not a vendor's sample.

Outputs
  marts.ratios              wide, one row per company-year
  marts.ratio_values        long, one row per company-year-ratio (for benchmarking)
  marts.benchmarks          industry x year x size band x ratio: n, p10, p25, median,
                            p75, p90
  marts.ratio_percentiles   each company's percentile within its industry-year, and a
                            credit_percentile oriented so 0 is always the worst
  ref.ratio_definitions     the library itself, so any number can be traced to its
                            formula
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import R2, motherduck_token
from credit_workbench.transform.ratio_defs import (
    FLAGS, HIGHER_IS_WORSE, RATIO_NAMES, RATIOS)

LAKE = "r2://credit-workbench-raw"
DERIVED = f"{LAKE}/parquet/derived"

MIN_PEERS = 5          # below this a percentile is noise, so none is published
SIZE_BANDS = """
    CASE WHEN revenue IS NULL          THEN 'Z unknown'
         WHEN revenue < 1e8            THEN 'A under $100m'
         WHEN revenue < 1e9            THEN 'B $100m-$1bn'
         WHEN revenue < 1e10           THEN 'C $1bn-$10bn'
         ELSE                               'D over $10bn' END"""


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


def main() -> None:
    md = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")

    ratio_sql = ",\n           ".join(f"{expr} AS {name}" for name, _, expr in RATIOS)
    flag_sql = ",\n           ".join(f"({expr}) AS {name}" for name, expr in FLAGS)

    # ------------------------------------------------------------------- E1
    print("Computing ratios ...")
    md.execute(f"""
        CREATE OR REPLACE TABLE marts.ratios AS
        WITH lagged AS (
            SELECT *,
                   lag(revenue)      OVER w AS revenue_prior,
                   lag(ebitda)       OVER w AS ebitda_prior,
                   lag(total_debt)   OVER w AS total_debt_prior,
                   lag(total_assets) OVER w AS total_assets_prior
            FROM marts.spreads_a
            WHERE is_primary_annual AND NOT is_empty_spread
            WINDOW w AS (PARTITION BY cik, basis ORDER BY period_end))
        SELECT cik, company_name, sic, substr(sic, 1, 2) AS sic2, basis,
               period_end, fy, revenue, ebitda, total_debt, total_assets,
               {SIZE_BANDS} AS size_band,
               {ratio_sql},
               {flag_sql}
        FROM lagged""")
    rows, companies = md.execute(
        "SELECT count(*), count(DISTINCT cik) FROM marts.ratios").fetchone()
    print(f"table marts.ratios  {rows:,} company-years, {companies:,} companies, "
          f"{len(RATIOS)} ratios + {len(FLAGS)} flags")

    # long form, for benchmarking and percentile work
    unpivot_cols = ", ".join(RATIO_NAMES)
    md.execute(f"""
        CREATE OR REPLACE TABLE marts.ratio_values AS
        SELECT cik, company_name, sic, sic2, basis, period_end, fy, size_band,
               ratio, value
        FROM (SELECT cik, company_name, sic, sic2, basis, period_end, fy, size_band,
                     {unpivot_cols} FROM marts.ratios)
        UNPIVOT (value FOR ratio IN ({unpivot_cols}))
        WHERE value IS NOT NULL AND isfinite(value)""")
    print(f"table marts.ratio_values  "
          f"{md.execute('SELECT count(*) FROM marts.ratio_values').fetchone()[0]:,} rows")

    # ------------------------------------------------------------------- E2
    print("Building peer benchmarks ...")
    md.execute(f"""
        CREATE OR REPLACE TABLE marts.benchmarks AS
        WITH v AS (
            SELECT r.sic, r.sic2, r.fy, r.size_band, r.ratio, r.value,
                   c.sic_description
            FROM marts.ratio_values r LEFT JOIN ref.dim_company c USING (cik)
            WHERE r.basis = 'latest'),
        grains AS (
            -- three grains: 2-digit industry, 4-digit industry, and 2-digit crossed
            -- with size, because a $50m borrower is not peer to a $50bn one
            SELECT 'sic2' AS level, sic2 AS industry_code, fy, 'ALL' AS size_band,
                   ratio, value, sic_description
            FROM v WHERE sic2 IS NOT NULL
            UNION ALL
            SELECT 'sic4', sic, fy, 'ALL', ratio, value, sic_description
            FROM v WHERE sic IS NOT NULL
            UNION ALL
            SELECT 'sic2_size', sic2, fy, size_band, ratio, value, sic_description
            FROM v WHERE sic2 IS NOT NULL)
        SELECT level, industry_code, fy, size_band, ratio,
               -- the label must not be part of the grouping key: a 2-digit industry
               -- spans many 4-digit descriptions, and grouping by it would split one
               -- industry into a dozen tiny peer groups
               mode(sic_description)                      AS industry_name,
               count(*)                                   AS n_companies,
               quantile_cont(value, 0.10)                 AS p10,
               quantile_cont(value, 0.25)                 AS p25,
               median(value)                              AS p50,
               quantile_cont(value, 0.75)                 AS p75,
               quantile_cont(value, 0.90)                 AS p90,
               avg(value)                                 AS mean
        FROM grains
        GROUP BY level, industry_code, fy, size_band, ratio
        HAVING count(*) >= {MIN_PEERS}""")
    print(f"table marts.benchmarks  "
          f"{md.execute('SELECT count(*) FROM marts.benchmarks').fetchone()[0]:,} rows")

    higher_worse = ", ".join(f"'{r}'" for r in sorted(HIGHER_IS_WORSE))
    md.execute(f"""
        CREATE OR REPLACE TABLE marts.ratio_percentiles AS
        SELECT cik, company_name, sic, sic2, fy, period_end, size_band, ratio, value,
               peer_count, peer_count_size,
               percentile_in_industry, percentile_in_size_peers,
               -- orient every ratio the same way: 0 is the weakest credit in the peer
               -- group, 1 the strongest, whichever direction the raw ratio runs
               CASE WHEN ratio IN ({higher_worse}) THEN 1 - percentile_in_industry
                    ELSE percentile_in_industry END      AS credit_percentile,
               -- Prefer this one for analysis. An industry alone is not a peer group:
               -- "Pharmaceutical Preparations" holds both Pfizer and hundreds of
               -- pre-revenue biotechs, whose median EBITDA margin is about -43%.
               -- Ranking a large profitable issuer against them flatters it for
               -- reasons that have nothing to do with its credit.
               CASE WHEN peer_count_size < {MIN_PEERS} THEN NULL
                    WHEN ratio IN ({higher_worse}) THEN 1 - percentile_in_size_peers
                    ELSE percentile_in_size_peers END    AS credit_percentile_size
        FROM (
            SELECT r.*,
                   count(*) OVER (PARTITION BY r.sic2, r.fy, r.ratio) AS peer_count,
                   count(*) OVER (PARTITION BY r.sic2, r.fy, r.size_band, r.ratio)
                       AS peer_count_size,
                   percent_rank() OVER (PARTITION BY r.sic2, r.fy, r.ratio
                                        ORDER BY r.value) AS percentile_in_industry,
                   percent_rank() OVER (PARTITION BY r.sic2, r.fy, r.size_band, r.ratio
                                        ORDER BY r.value) AS percentile_in_size_peers
            FROM marts.ratio_values r
            WHERE r.basis = 'latest' AND r.sic2 IS NOT NULL)
        WHERE peer_count >= {MIN_PEERS}""")
    print(f"table marts.ratio_percentiles  "
          f"{md.execute('SELECT count(*) FROM marts.ratio_percentiles').fetchone()[0]:,} rows")

    # ------------------------------------------------------------- definitions
    md.execute("DROP TABLE IF EXISTS ref.ratio_definitions")
    md.execute("""CREATE TABLE ref.ratio_definitions (
                      ratio VARCHAR, category VARCHAR, formula VARCHAR,
                      higher_is_worse BOOLEAN)""")
    md.executemany("INSERT INTO ref.ratio_definitions VALUES (?, ?, ?, ?)",
                   [(n, c, e, n in HIGHER_IS_WORSE) for n, c, e in RATIOS])
    print(f"table ref.ratio_definitions  {len(RATIOS)} ratios")


if __name__ == "__main__":
    main()
