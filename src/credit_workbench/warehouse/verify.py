"""Post-load verification (tracker M2 seed).

Proves the warehouse holds what we think it does, and demonstrates the credit-relevant
content that is now queryable: note-level facts, segment dimensions, concentration
disclosures, 8-K event codes, and coverage of the pilot industries.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

# DEC-5 pilot universe: pharma, capital goods, retail
PILOT_SIC = {
    "Pharma / biotech": "sic BETWEEN '2833' AND '2836'",
    "Capital goods": "sic BETWEEN '3500' AND '3599'",
    "Retail": "sic BETWEEN '5200' AND '5999'",
}

QUERIES: list[tuple[str, str]] = [
    ("Entity master", """
        SELECT count(*) AS companies,
               count(*) FILTER (WHERE sic IS NOT NULL) AS with_sic,
               count(*) FILTER (WHERE filer_category LIKE '%Large accelerated%') AS large_accel
        FROM ref.dim_company"""),
    ("Listed companies by exchange", """
        SELECT exchange, count(*) AS listings
        FROM ref.company_tickers WHERE exchange IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT 6"""),
    ("Filing index coverage", """
        SELECT min(filing_date) AS earliest, max(filing_date) AS latest,
               count(*) AS filings
        FROM ref.filing_index"""),
    ("Credit-relevant forms available", """
        SELECT form, count(*) AS n
        FROM ref.filing_index
        WHERE form IN ('10-K','10-Q','8-K','DEF 14A','NT 10-K','NT 10-Q','25','20-F')
        GROUP BY 1 ORDER BY 2 DESC"""),
    ("8-K credit events (item codes -> EWS feed, H1)", """
        SELECT CASE
                 WHEN items LIKE '%1.03%' THEN '1.03 bankruptcy'
                 WHEN items LIKE '%2.04%' THEN '2.04 debt acceleration / covenant'
                 WHEN items LIKE '%4.02%' THEN '4.02 non-reliance / restatement'
                 WHEN items LIKE '%4.01%' THEN '4.01 auditor change'
                 WHEN items LIKE '%2.06%' THEN '2.06 material impairment'
                 WHEN items LIKE '%3.01%' THEN '3.01 listing / covenant notice'
               END AS credit_event,
               count(*) AS n
        FROM ref.filing_index
        WHERE form LIKE '8-K%' AND credit_event IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC"""),
    ("Financial statement archives loaded", """
        SELECT 'fsds' AS dataset, count(DISTINCT period) AS periods,
               min(period) AS first_period, max(period) AS last_period
        FROM raw.fsds_sub
        UNION ALL
        SELECT 'fsn', count(DISTINCT period), min(period), max(period) FROM raw.fsn_sub"""),
    ("Note-level depth: facts per filing type", """
        SELECT s.form, count(*) AS facts, count(DISTINCT n.tag) AS distinct_tags
        FROM raw.fsn_num n JOIN raw.fsn_sub s USING (adsh)
        WHERE s.form IN ('10-K','10-Q') AND n.period = (SELECT max(period) FROM raw.fsn_sub)
        GROUP BY 1"""),
    ("Footnote text blocks available (G corpus)", """
        SELECT count(*) AS text_blocks, count(DISTINCT adsh) AS filings,
               round(avg(TRY_CAST(txtlen AS BIGINT))) AS avg_chars
        FROM raw.fsn_txt
        WHERE period = (SELECT max(period) FROM raw.fsn_sub)"""),
    ("Customer / supplier concentration disclosures (F2)", """
        SELECT count(*) AS facts, count(DISTINCT adsh) AS filings
        FROM raw.fsn_num
        WHERE tag LIKE 'ConcentrationRisk%'
          AND period = (SELECT max(period) FROM raw.fsn_sub)"""),
    ("Lease / pension / debt inputs for adjustments (D1)", """
        SELECT CASE
                 WHEN tag LIKE '%OperatingLeaseLiability%' THEN 'operating lease liability'
                 WHEN tag LIKE '%DefinedBenefit%' THEN 'pension / defined benefit'
                 WHEN tag LIKE '%DebtInstrument%' THEN 'debt instrument schedule'
               END AS adjustment_input,
               count(*) AS facts, count(DISTINCT adsh) AS filings
        FROM raw.fsn_num
        WHERE period = (SELECT max(period) FROM raw.fsn_sub)
          AND adjustment_input IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC"""),
]


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    for title, query in QUERIES:
        print(f"\n### {title}")
        try:
            print(con.sql(query))
        except Exception as exc:  # noqa: BLE001
            print(f"  (skipped: {exc})")

    print("\n### Pilot universe coverage (DEC-5)")
    for label, predicate in PILOT_SIC.items():
        row = con.execute(f"""
            SELECT count(*) AS companies,
                   count(*) FILTER (WHERE cik IN (
                       SELECT TRY_CAST(cik AS BIGINT) FROM raw.fsn_sub)) AS with_xbrl_filings
            FROM ref.dim_company WHERE {predicate}""").fetchone()
        print(f"  {label:20} {row[0]:>6,} companies   {row[1]:>6,} with XBRL filings")


if __name__ == "__main__":
    main()
