"""Tracker A1/A6 — expose the R2 data lake through MotherDuck.

Design: heavy fact tables (num, txt, pre, cal, ren, dim, the filing index) stay as
parquet in R2 and are queried in place through views — storage stays pennies and the
warehouse's free tier is never filled. Small, heavily joined tables (filing headers,
the tag dictionary, the entity master) are materialised for speed.

Idempotent — safe to re-run after every ingest.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import R2, motherduck_token

BUCKET = "credit-workbench-raw"
LAKE = f"r2://{BUCKET}"

# dataset -> tables kept as views over parquet
VIEW_TABLES = {
    "fsds": ["num", "pre", "tag"],
    "fsn": ["num", "txt", "dim", "pre", "cal", "ren", "tag"],
}


def sql(con, statement: str) -> None:
    con.execute(statement)


def main() -> None:
    cfg = R2.from_env()
    con = duckdb.connect(f"md:?motherduck_token={motherduck_token()}")
    sql(con, "CREATE DATABASE IF NOT EXISTS credit_workbench")
    sql(con, "USE credit_workbench")

    secret_sql = f"""
        CREATE OR REPLACE PERSISTENT SECRET r2_lake (
            TYPE R2,
            KEY_ID '{cfg.access_key_id}',
            SECRET '{cfg.secret_access_key}',
            ACCOUNT_ID '{cfg.account_id}',
            REGION 'auto'
        )"""
    try:
        sql(con, secret_sql)
        print("R2 secret stored (persistent)")
    except Exception as exc:  # noqa: BLE001
        print(f"Persistent secret unavailable ({exc}); using session secret")
        sql(con, secret_sql.replace("PERSISTENT ", ""))

    # Placeholder tables from the initial schema would block same-named views.
    for name in ("raw.fsn_sub", "raw.fsn_num", "raw.fsn_tag"):
        sql(con, f"DROP TABLE IF EXISTS {name}")

    # ---------------------------------------------------------------- views
    for dataset, tables in VIEW_TABLES.items():
        for table in tables:
            path = f"{LAKE}/parquet/sec/{dataset}/{table}/*/data.parquet"
            sql(con, f"DROP VIEW IF EXISTS raw.{dataset}_{table}")
            sql(con, f"""
                CREATE VIEW raw.{dataset}_{table} AS
                SELECT * FROM read_parquet('{path}', hive_partitioning = true,
                                           union_by_name = true)""")
            print(f"view  raw.{dataset}_{table}")

    sql(con, "DROP VIEW IF EXISTS ref.filing_index")
    sql(con, f"""
        CREATE VIEW ref.filing_index AS
        SELECT * FROM read_parquet('{LAKE}/parquet/sec/entity/filings/data.parquet')""")
    print("view  ref.filing_index")

    # ------------------------------------------------------- materialised
    for dataset in ("fsds", "fsn"):
        path = f"{LAKE}/parquet/sec/{dataset}/sub/*/data.parquet"
        sql(con, f"""
            CREATE OR REPLACE TABLE raw.{dataset}_sub AS
            SELECT * FROM read_parquet('{path}', hive_partitioning = true,
                                       union_by_name = true)""")
        n = con.execute(f"SELECT count(*) FROM raw.{dataset}_sub").fetchone()[0]
        print(f"table raw.{dataset}_sub  {n:,} rows")

    sql(con, """
        CREATE OR REPLACE TABLE ref.xbrl_tag AS
        SELECT tag, version, any_value(custom) AS custom, any_value(abstract) AS abstract,
               any_value(datatype) AS datatype, any_value(iord) AS iord,
               any_value(crdr) AS crdr, any_value(tlabel) AS tlabel, any_value(doc) AS doc
        FROM (SELECT * FROM raw.fsn_tag UNION ALL BY NAME SELECT * FROM raw.fsds_tag)
        GROUP BY tag, version""")
    print(f"table ref.xbrl_tag  "
          f"{con.execute('SELECT count(*) FROM ref.xbrl_tag').fetchone()[0]:,} rows")

    sql(con, f"""
        CREATE OR REPLACE TABLE ref.dim_company AS
        SELECT TRY_CAST(cik AS BIGINT)      AS cik,
               name                          AS company_name,
               entity_type, sic, sic_description, ein, description,
               category                      AS filer_category,
               fiscal_year_end, state_of_incorporation,
               state_of_incorporation_description, phone, website, investor_website,
               owner_org, flags,
               business_street1, business_street2, business_city, business_state,
               business_zip, business_country,
               mailing_street1, mailing_street2, mailing_city, mailing_state,
               mailing_zip, mailing_country,
               current_localtimestamp()      AS loaded_at
        FROM read_parquet('{LAKE}/parquet/sec/entity/companies/data.parquet')""")
    print(f"table ref.dim_company  "
          f"{con.execute('SELECT count(*) FROM ref.dim_company').fetchone()[0]:,} rows")

    sql(con, f"""
        CREATE OR REPLACE TABLE ref.company_tickers AS
        SELECT TRY_CAST(cik AS BIGINT) AS cik, ticker, exchange
        FROM read_parquet('{LAKE}/parquet/sec/entity/tickers/data.parquet')""")
    sql(con, f"""
        CREATE OR REPLACE TABLE ref.former_names AS
        SELECT TRY_CAST(cik AS BIGINT) AS cik, former_name,
               TRY_CAST(name_from AS TIMESTAMP) AS valid_from,
               TRY_CAST(name_to AS TIMESTAMP)   AS valid_to
        FROM read_parquet('{LAKE}/parquet/sec/entity/former_names/data.parquet')""")
    for table in ("ref.company_tickers", "ref.former_names"):
        print(f"table {table}  "
              f"{con.execute(f'SELECT count(*) FROM {table}').fetchone()[0]:,} rows")

    # ------------------------------------------- typed staging convenience
    for dataset in ("fsds", "fsn"):
        sql(con, f"DROP VIEW IF EXISTS staging.{dataset}_num_typed")
        sql(con, f"""
            CREATE VIEW staging.{dataset}_num_typed AS
            SELECT adsh, tag, version,
                   TRY_CAST(strptime(ddate, '%Y%m%d') AS DATE) AS ddate,
                   TRY_CAST(qtrs AS INTEGER)  AS qtrs,
                   uom, TRY_CAST(value AS DOUBLE) AS value,
                   coreg, footnote, period
            FROM raw.{dataset}_num""")
        print(f"view  staging.{dataset}_num_typed")

    print("\nWarehouse objects:")
    for schema, name, kind in con.execute("""
            SELECT table_schema, table_name, table_type
            FROM information_schema.tables
            WHERE table_catalog = 'credit_workbench'
            ORDER BY 1, 2""").fetchall():
        print(f"  {schema}.{name}  ({kind})")


if __name__ == "__main__":
    main()
