"""Show what the warehouse now holds for a single company — a readable proof of depth.

    uv run python -m credit_workbench.warehouse.demo_company --ticker PFE

Prints the entity record, filing history, headline financials, a note-level extract
(leases, debt, pensions), segment disclosures and recent 8-K credit events: the raw
material a credit analyst spreads, adjusts and benchmarks.
"""
from __future__ import annotations

import argparse

import duckdb

from credit_workbench.common.config import motherduck_token


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="PFE")
    args = ap.parse_args()
    tk = args.ticker.upper()

    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    row = con.execute("""
        SELECT c.cik, c.company_name, c.sic, c.sic_description, c.filer_category,
               c.state_of_incorporation, c.fiscal_year_end
        FROM ref.dim_company c JOIN ref.company_tickers t USING (cik)
        WHERE t.ticker = ? LIMIT 1""", [tk]).fetchone()
    if not row:
        print(f"No company found for ticker {tk}")
        return
    cik, name, sic, sic_desc, cat, state, fye = row

    section(f"{name}  (ticker {tk}, CIK {cik})")
    print(f"  Industry (SIC)      {sic} — {sic_desc}")
    print(f"  Filer category      {cat}")
    print(f"  Incorporated in     {state}        Fiscal year end: {fye}")

    section("Filing history on record")
    print(con.sql(f"""
        SELECT form, count(*) AS filings,
               min(filing_date) AS earliest, max(filing_date) AS latest
        FROM ref.filing_index
        WHERE TRY_CAST(cik AS BIGINT) = {cik}
          AND form IN ('10-K','10-Q','8-K','DEF 14A','S-3','424B2')
        GROUP BY 1 ORDER BY 2 DESC"""))

    section("Headline financials as filed (latest annual figures)")
    print(con.sql(f"""
        WITH latest AS (
            SELECT adsh, period FROM raw.fsn_sub
            WHERE TRY_CAST(cik AS BIGINT) = {cik} AND form = '10-K'
            ORDER BY filed DESC LIMIT 1)
        SELECT n.tag,
               TRY_CAST(strptime(n.ddate, '%Y%m%d') AS DATE) AS as_of,
               round(TRY_CAST(n.value AS DOUBLE) / 1e6, 1) AS usd_millions
        FROM raw.fsn_num n JOIN latest l ON n.adsh = l.adsh AND n.period = l.period
        WHERE n.tag IN ('Revenues','RevenueFromContractWithCustomerExcludingAssessedTax',
                        'OperatingIncomeLoss','NetIncomeLoss','Assets','Liabilities',
                        'StockholdersEquity','CashAndCashEquivalentsAtCarryingValue',
                        'LongTermDebtNoncurrent','LongTermDebtCurrent')
          AND n.dimh IN ('0x00000000','') AND n.uom = 'USD'
        ORDER BY n.tag, as_of DESC"""))

    section("Note-level detail — the inputs to rating-agency adjustments (D1)")
    print(con.sql(f"""
        WITH latest AS (
            SELECT adsh, period FROM raw.fsn_sub
            WHERE TRY_CAST(cik AS BIGINT) = {cik} AND form = '10-K'
            ORDER BY filed DESC LIMIT 1)
        SELECT CASE
                 WHEN n.tag LIKE '%OperatingLeaseLiability%' THEN 'Operating leases'
                 WHEN n.tag LIKE '%FinanceLease%' THEN 'Finance leases'
                 WHEN n.tag LIKE '%DefinedBenefit%' THEN 'Pension / OPEB'
                 WHEN n.tag LIKE '%DebtInstrument%' THEN 'Debt instrument schedule'
                 WHEN n.tag LIKE '%LossContingency%' THEN 'Loss contingencies'
                 WHEN n.tag LIKE '%Restructuring%' THEN 'Restructuring charges'
               END AS adjustment_input,
               count(*) AS tagged_facts, count(DISTINCT n.tag) AS distinct_tags
        FROM raw.fsn_num n JOIN latest l ON n.adsh = l.adsh AND n.period = l.period
        WHERE adjustment_input IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC"""))

    section("Segment & geographic disclosures (F1)")
    print(con.sql(f"""
        WITH latest AS (
            SELECT adsh, period FROM raw.fsn_sub
            WHERE TRY_CAST(cik AS BIGINT) = {cik} AND form = '10-K'
            ORDER BY filed DESC LIMIT 1)
        SELECT d.segt AS segment_dimension,
               count(*) AS facts,
               round(sum(TRY_CAST(n.value AS DOUBLE)) / 1e6, 0) AS total_usd_mm
        FROM raw.fsn_num n
        JOIN latest l ON n.adsh = l.adsh AND n.period = l.period
        JOIN raw.fsn_dim d ON d.dimhash = n.dimh AND d.period = n.period
        WHERE n.tag LIKE 'Revenue%' AND n.uom = 'USD' AND d.segt IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT 12"""))

    section("Footnote narrative available for qualitative review (G corpus)")
    print(con.sql(f"""
        WITH latest AS (
            SELECT adsh, period FROM raw.fsn_sub
            WHERE TRY_CAST(cik AS BIGINT) = {cik} AND form = '10-K'
            ORDER BY filed DESC LIMIT 1)
        SELECT t.tag, TRY_CAST(t.txtlen AS BIGINT) AS characters
        FROM raw.fsn_txt t JOIN latest l ON t.adsh = l.adsh AND t.period = l.period
        WHERE TRY_CAST(t.txtlen AS BIGINT) > 4000
        ORDER BY characters DESC LIMIT 12"""))

    section("Recent 8-K events (early-warning feed, H1)")
    print(con.sql(f"""
        SELECT filing_date, items, primary_doc_description
        FROM ref.filing_index
        WHERE TRY_CAST(cik AS BIGINT) = {cik} AND form LIKE '8-K%' AND items IS NOT NULL
        ORDER BY filing_date DESC LIMIT 10"""))


if __name__ == "__main__":
    main()
