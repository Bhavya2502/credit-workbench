"""Expose the narrative layer in the warehouse.

The section and exhibit text stays as parquet in the lake and is read through views, the
same arrangement used for the note text: it is far too big to materialise and nothing
queries all of it at once.

Named views sit on top of the two sections that carry the most analytical weight - risk
factors and MD&A - so neither needs a filter written by hand each time.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

LAKE = "r2://credit-workbench-raw"
SECTIONS = f"{LAKE}/parquet/sec/narrative/sections"
EXHIBITS = f"{LAKE}/parquet/sec/narrative/exhibits"


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    con.execute("CREATE SCHEMA IF NOT EXISTS quali")

    con.execute("DROP VIEW IF EXISTS quali.filing_sections")
    con.execute(f"""
        CREATE VIEW quali.filing_sections AS
        SELECT * FROM read_parquet('{SECTIONS}/*/*.parquet',
                                   hive_partitioning = true, union_by_name = true)""")
    rows, filings, companies = con.execute("""
        SELECT count(*), count(DISTINCT adsh), count(DISTINCT cik)
        FROM quali.filing_sections""").fetchone()
    print(f"view  quali.filing_sections  {rows:,} sections, {filings:,} filings, "
          f"{companies:,} companies")

    for name, item in (("risk_factors", "1A"), ("mdna", "7"),
                       ("business_description", "1"), ("legal_proceedings", "3"),
                       ("controls_and_procedures", "9A")):
        con.execute(f"DROP VIEW IF EXISTS quali.{name}")
        con.execute(f"""
            CREATE VIEW quali.{name} AS
            SELECT cik, adsh, form, filing_date, period_of_report, char_len, text
            FROM quali.filing_sections WHERE item = '{item}'""")
    print("view  quali.risk_factors, quali.mdna, quali.business_description, "
          "quali.legal_proceedings, quali.controls_and_procedures")

    try:
        con.execute("DROP VIEW IF EXISTS quali.exhibits")
        con.execute(f"""
            CREATE VIEW quali.exhibits AS
            SELECT * FROM read_parquet('{EXHIBITS}/*/*.parquet',
                                       hive_partitioning = true, union_by_name = true)""")
        rows, filings = con.execute(
            "SELECT count(*), count(DISTINCT adsh) FROM quali.exhibits").fetchone()
        print(f"view  quali.exhibits  {rows:,} documents from {filings:,} filings")

        con.execute("DROP VIEW IF EXISTS quali.debt_agreements")
        con.execute("""
            CREATE VIEW quali.debt_agreements AS
            SELECT * FROM quali.exhibits
            WHERE doc_kind IN ('credit_agreement', 'indenture', 'amendment',
                               'note_purchase', 'security_agreement', 'guarantee')""")
        n = con.execute("SELECT count(*) FROM quali.debt_agreements").fetchone()[0]
        print(f"view  quali.debt_agreements  {n:,} documents")
    except duckdb.IOException:
        print("view  quali.exhibits  (no exhibit parquet in the lake yet)")


if __name__ == "__main__":
    main()
