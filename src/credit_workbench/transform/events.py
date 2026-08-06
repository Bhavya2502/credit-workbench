"""Tracker H1 (+H3) — corporate events as an early-warning feed.

An 8-K carries standardised item codes saying what happened, so the whole US market's
event history is already machine-readable: 2.04 means a debt obligation was
accelerated, 4.02 means previously issued accounts can no longer be relied upon, 1.03
means bankruptcy. This turns 27 million filing records into one row per event, scored
for credit severity.

Also captures the filing-behaviour signals (H3): NT 10-K and NT 10-Q are notifications
that a company cannot file on time, and Form 25 is a delisting — both classic
early warnings that involve no financial analysis at all.

Item codes changed in August 2004. Before that they were single digits with different
meanings, so those are recorded as `legacy` rather than mapped to today's taxonomy and
silently mis-scored.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import R2, motherduck_token

LAKE = "r2://credit-workbench-raw"
FILINGS = f"{LAKE}/parquet/sec/entity/filings/data.parquet"
OUT = f"{LAKE}/parquet/derived/corp_events"

# item code -> (category, severity 1-5, description). Severity is credit-relevance,
# not newsworthiness: an earnings release is routine, a covenant trigger is not.
ITEMS: dict[str, tuple[str, int, str]] = {
    "1.01": ("material_agreement", 2, "Entry into a material definitive agreement"),
    "1.02": ("material_agreement", 3, "Termination of a material definitive agreement"),
    "1.03": ("distress", 5, "Bankruptcy or receivership"),
    "1.04": ("operational", 2, "Mine safety reporting"),
    "1.05": ("operational", 4, "Material cybersecurity incident"),
    "2.01": ("m_and_a", 2, "Completion of acquisition or disposition of assets"),
    "2.02": ("earnings", 1, "Results of operations and financial condition"),
    "2.03": ("leverage", 3, "Creation of a direct financial obligation"),
    "2.04": ("distress", 5, "Triggering event accelerating a financial obligation"),
    "2.05": ("restructuring", 3, "Costs associated with exit or disposal activities"),
    "2.06": ("impairment", 4, "Material impairments"),
    "3.01": ("listing", 4, "Notice of delisting or failure to satisfy a listing rule"),
    "3.02": ("equity", 2, "Unregistered sales of equity securities"),
    "3.03": ("equity", 3, "Material modification to rights of security holders"),
    "4.01": ("audit", 4, "Change in the registrant's certifying accountant"),
    "4.02": ("audit", 5, "Non-reliance on previously issued financial statements"),
    "5.01": ("governance", 3, "Changes in control of registrant"),
    "5.02": ("governance", 2, "Departure or election of directors or officers"),
    "5.03": ("governance", 1, "Amendments to articles, bylaws, or fiscal year change"),
    "5.04": ("governance", 2, "Trading suspension under employee benefit plans"),
    "5.05": ("governance", 2, "Amendment to the code of ethics"),
    "5.06": ("governance", 3, "Change in shell company status"),
    "5.07": ("governance", 1, "Submission of matters to a vote of security holders"),
    "5.08": ("governance", 1, "Shareholder director nominations"),
    "7.01": ("disclosure", 1, "Regulation FD disclosure"),
    "8.01": ("disclosure", 1, "Other events"),
    "9.01": ("disclosure", 1, "Financial statements and exhibits"),
}

# Whole-form signals that need no item code.
FORM_SIGNALS: dict[str, tuple[str, int, str]] = {
    "NT 10-K": ("late_filing", 4, "Unable to file annual report on time"),
    "NT 10-Q": ("late_filing", 3, "Unable to file quarterly report on time"),
    "25": ("listing", 4, "Notification of delisting"),
    "25-NSE": ("listing", 4, "Delisting by the exchange"),
    "15-12B": ("listing", 3, "Deregistration of a class of securities"),
    "15-12G": ("listing", 3, "Deregistration of a class of securities"),
}


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
    con = connect()
    con.execute("""CREATE OR REPLACE TABLE item_map (
                       code VARCHAR, category VARCHAR, severity INTEGER,
                       description VARCHAR)""")
    con.executemany("INSERT INTO item_map VALUES (?, ?, ?, ?)",
                    [(c, *v) for c, v in ITEMS.items()])
    con.execute("""CREATE OR REPLACE TABLE form_map (
                       form VARCHAR, category VARCHAR, severity INTEGER,
                       description VARCHAR)""")
    con.executemany("INSERT INTO form_map VALUES (?, ?, ?, ?)",
                    [(f, *v) for f, v in FORM_SIGNALS.items()])
    print(f"{len(ITEMS)} 8-K item codes, {len(FORM_SIGNALS)} form-level signals")

    print("Building the event feed ...")
    con.execute(f"""
        COPY (
            WITH f AS (
                SELECT TRY_CAST(cik AS BIGINT) AS cik, accession_number, form,
                       TRY_CAST(filing_date AS DATE) AS filing_date,
                       TRY_CAST(report_date AS DATE) AS report_date,
                       items, primary_doc_description
                FROM read_parquet('{FILINGS}')
                WHERE cik IS NOT NULL),
            eight_k AS (
                SELECT f.cik, f.accession_number, f.form, f.filing_date, f.report_date,
                       trim(code) AS item_code, f.primary_doc_description
                FROM f, UNNEST(str_split(f.items, ',')) AS t(code)
                WHERE f.form LIKE '8-K%' AND f.items IS NOT NULL AND f.items <> ''),
            coded AS (
                SELECT e.cik, e.accession_number, e.form, e.filing_date, e.report_date,
                       e.item_code,
                       -- codes carrying no dot pre-date the August 2004 renumbering
                       coalesce(m.category,
                                CASE WHEN e.item_code NOT LIKE '%.%'
                                     THEN 'legacy' ELSE 'other' END) AS category,
                       coalesce(m.severity,
                                CASE WHEN e.item_code NOT LIKE '%.%' THEN 0 ELSE 1 END)
                           AS severity,
                       coalesce(m.description,
                                CASE WHEN e.item_code NOT LIKE '%.%'
                                     THEN 'Pre-2004 item numbering'
                                     ELSE 'Unmapped item code' END) AS description
                FROM eight_k e LEFT JOIN item_map m ON m.code = e.item_code),
            form_events AS (
                SELECT f.cik, f.accession_number, f.form, f.filing_date, f.report_date,
                       NULL AS item_code, fm.category, fm.severity, fm.description
                FROM f JOIN form_map fm ON fm.form = f.form)
            SELECT *, year(filing_date) AS event_year FROM (
                SELECT * FROM coded UNION ALL BY NAME SELECT * FROM form_events)
            WHERE filing_date IS NOT NULL
        ) TO '{OUT}' (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (event_year),
                      OVERWRITE_OR_IGNORE, FILENAME_PATTERN 'ev_{{i}}')""")
    n = con.execute(f"SELECT count(*) FROM read_parquet('{OUT}/*/*.parquet')").fetchone()[0]
    print(f"  {n:,} events")

    md = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    md.execute("DROP VIEW IF EXISTS events.corp_events")
    md.execute(f"""
        CREATE VIEW events.corp_events AS
        SELECT * FROM read_parquet('{OUT}/*/*.parquet', hive_partitioning = true)""")
    rows, companies = md.execute(
        "SELECT count(*), count(DISTINCT cik) FROM events.corp_events").fetchone()
    print(f"view  events.corp_events  {rows:,} events, {companies:,} companies")

    # Severity 4+ only: the feed a credit officer would actually watch.
    md.execute("""
        CREATE OR REPLACE TABLE marts.credit_events AS
        SELECT e.cik, c.company_name, c.sic, c.sic_description,
               e.filing_date, e.form, e.item_code, e.category, e.severity,
               e.description, e.accession_number
        FROM events.corp_events e
        LEFT JOIN ref.dim_company c USING (cik)
        WHERE e.severity >= 4""")
    print(f"table marts.credit_events  "
          f"{md.execute('SELECT count(*) FROM marts.credit_events').fetchone()[0]:,} rows")


if __name__ == "__main__":
    main()
