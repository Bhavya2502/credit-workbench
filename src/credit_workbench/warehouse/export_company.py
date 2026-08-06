"""Export everything the warehouse holds for a few companies, as CSV.

    uv run python -m credit_workbench.warehouse.export_company --tickers PFE,CAT,WMT

Writes to ./export/:

  01_entity.csv              the master record for each company
  02_spread_annual.csv       full annual spread, every line, every year, both bases
  03_spread_quarterly.csv    quarterly spread incl. trailing-twelve-month flows
  04_spread_lines_long.csv   one row per line per period, carrying the XBRL tag and
                             accession number each figure came from — the audit trail
  05_reconciliation.csv      balance-sheet and gross-profit checks per company-year
  06_coverage.csv            share of reported face-financial value the template caught
  07_restatements.csv        figures whose value changed after first publication
  08_template.csv            the spread template itself: every line and its tag
                             alternatives, so the mapping is inspectable
  09_unmapped_tags.csv       face-financial tags these filers used that no line claims
"""
from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

from credit_workbench.common.config import motherduck_token

OUT = Path("export")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="PFE,CAT,WMT")
    args = ap.parse_args()
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    OUT.mkdir(exist_ok=True)

    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    ticker_list = ", ".join(f"'{t}'" for t in tickers)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE picked AS
        SELECT DISTINCT c.cik, t.ticker, c.company_name, c.sic, c.sic_description
        FROM ref.dim_company c JOIN ref.company_tickers t USING (cik)
        WHERE t.ticker IN ({ticker_list})""")
    picked = con.execute("SELECT ticker, cik, company_name, sic_description "
                         "FROM picked ORDER BY ticker").fetchall()
    for t, cik, name, sic in picked:
        print(f"  {t:5} CIK {cik:<8} {name} — {sic}")
    if not picked:
        raise SystemExit("None of those tickers matched.")

    exports: list[tuple[str, str]] = [
        ("01_entity", """
            SELECT p.ticker, c.* FROM ref.dim_company c JOIN picked p USING (cik)"""),
        ("02_spread_annual", """
            SELECT p.ticker, s.* FROM marts.spreads_a s JOIN picked p USING (cik)
            ORDER BY p.ticker, s.basis, s.period_end"""),
        ("03_spread_quarterly", """
            SELECT p.ticker, s.* FROM marts.spreads_q s JOIN picked p USING (cik)
            ORDER BY p.ticker, s.basis, s.period_end"""),
        ("04_spread_lines_long", """
            SELECT p.ticker, l.basis, l.period_end, l.fy, l.qtrs, l.line_no, l.line_code,
                   l.label, l.statement, l.value, l.uom,
                   l.source_tag, l.adsh AS source_filing, l.form, l.filed
            FROM marts.spread_lines l JOIN picked p USING (cik)
            ORDER BY p.ticker, l.basis, l.period_end DESC, l.line_no"""),
        ("05_reconciliation", """
            SELECT p.ticker, s.* FROM marts.spread_checks s JOIN picked p USING (cik)
            ORDER BY p.ticker, s.basis, s.period_end"""),
        ("06_coverage", """
            SELECT p.ticker, v.* FROM marts.spread_coverage v JOIN picked p USING (cik)
            ORDER BY p.ticker, v.fy"""),
        ("07_restatements", """
            SELECT p.ticker, r.* FROM marts.restatements r JOIN picked p USING (cik)
            ORDER BY p.ticker, abs(r.restatement_amount) DESC"""),
        ("08_template", """
            SELECT m.line_no, m.line_code, m.label, m.statement,
                   m.priority AS tag_priority, m.tag AS xbrl_tag
            FROM staging.tag_map m ORDER BY m.line_no, m.priority"""),
        ("09_unmapped_tags", f"""
            SELECT f.tag, any_value(f.stmt) AS statement,
                   count(DISTINCT f.adsh) AS filings, sum(abs(f.value)) AS abs_value
            FROM staging.facts_pit f
            JOIN picked p USING (cik)
            LEFT JOIN staging.tag_map m ON m.tag = f.tag
            WHERE m.tag IS NULL AND f.stmt IN ('IS', 'BS', 'CF') AND f.is_latest
            GROUP BY 1 ORDER BY filings DESC"""),
    ]

    for name, query in exports:
        path = OUT / f"{name}.csv"
        con.execute(f"COPY ({query}) TO '{path.as_posix()}' (HEADER, DELIMITER ',')")
        n = con.execute(f"SELECT count(*) FROM ({query})").fetchone()[0]
        print(f"  {path}  {n:,} rows")


if __name__ == "__main__":
    main()
