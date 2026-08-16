"""Expose the proxy layer in the warehouse.

The section text stays as parquet in the lake and is read through a view, the same
arrangement as the 10-K sections and the note text: it is far too big to materialise and
nothing queries all of it at once. `marts.governance_metrics` is the opposite - one
narrow row per filing - and is a table, because every scorecard query touches all of it.

Named views sit on the sections a Management Risk read starts from, so none of them needs
a filter written by hand each time.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

LAKE = "r2://credit-workbench-raw"
PROXY = f"{LAKE}/parquet/sec/narrative/proxy_sections"

NAMED = (("proxy_governance", "governance"), ("proxy_independence", "independence"),
         ("proxy_related_party", "related_party"), ("proxy_audit_fees", "audit_fees"),
         ("proxy_compensation", "cda"), ("proxy_committees", "committees"))


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    con.execute("CREATE SCHEMA IF NOT EXISTS quali")

    con.execute("DROP VIEW IF EXISTS quali.proxy_sections")
    con.execute(f"""
        CREATE VIEW quali.proxy_sections AS
        SELECT * FROM read_parquet('{PROXY}/*/*.parquet',
                                   hive_partitioning = true, union_by_name = true)""")
    rows, filings, companies = con.execute("""
        SELECT count(*), count(DISTINCT adsh), count(DISTINCT cik)
        FROM quali.proxy_sections""").fetchone()
    print(f"view  quali.proxy_sections  {rows:,} sections, {filings:,} proxies, "
          f"{companies:,} companies")

    for name, section in NAMED:
        con.execute(f"DROP VIEW IF EXISTS quali.{name}")
        con.execute(f"""
            CREATE VIEW quali.{name} AS
            SELECT cik, adsh, form, filing_date, period_of_report, char_len, text
            FROM quali.proxy_sections WHERE section = '{section}'""")
    print("view  " + ", ".join(f"quali.{n}" for n, _ in NAMED))


if __name__ == "__main__":
    main()
