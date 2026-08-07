"""Tracker G — the qualitative corpus and its credit signals.

Every disclosure note a company tags in XBRL carries its full narrative text, and that
text is already in the lake: no fetching, no scraping, complete coverage of 10-K
filers. This classifies it into note types and extracts the phrases a credit analyst
reads for.

  quali.note_text     a view, not a copy — the corpus classified by note type, with a
                      truncation flag so any analysis knows what it is reading
  quali.note_signals  one row per company-period: going concern, covenant breach,
                      material weakness, cross-default, liquidity warnings and the
                      rest, with the matched note type recorded

The signals are deterministic phrase matching, not a language model. That is a
deliberate choice: it costs nothing to run over the whole market, it is reproducible,
and every hit can be traced to the sentence that caused it. A model layer can sit on
top of this later (G5) — but the cheap layer should exist first, because it turns out
to carry most of the signal.
"""
from __future__ import annotations

import argparse

import duckdb

from credit_workbench.common.config import R2, motherduck_token

LAKE = "r2://credit-workbench-raw"
SIGNALS_OUT = f"{LAKE}/parquet/derived/note_signals"

# Note type from the tag name. Ordered: the first match wins, so the specific
# patterns must precede the general ones.
NOTE_TYPE_SQL = """
    CASE WHEN tag ILIKE '%GoingConcern%' THEN 'going_concern'
         WHEN tag ILIKE '%Liquidity%' THEN 'liquidity'
         WHEN tag ILIKE '%Debt%' OR tag ILIKE '%Borrowing%' THEN 'debt'
         WHEN tag ILIKE '%Lease%' THEN 'leases'
         WHEN tag ILIKE '%CommitmentsAndContingencies%'
           OR tag ILIKE '%LossContingency%' THEN 'commitments_contingencies'
         WHEN tag ILIKE '%RelatedParty%' THEN 'related_party'
         WHEN tag ILIKE '%SegmentReporting%' THEN 'segments'
         WHEN tag ILIKE '%Pension%' OR tag ILIKE '%Postretirement%' THEN 'pension_opeb'
         WHEN tag ILIKE '%IncomeTax%' THEN 'income_taxes'
         WHEN tag ILIKE '%SubsequentEvent%' THEN 'subsequent_events'
         WHEN tag ILIKE '%Concentration%' OR tag ILIKE '%RisksAndUncertainties%'
              THEN 'risks_concentrations'
         WHEN tag ILIKE '%Derivative%' OR tag ILIKE '%Hedging%' THEN 'derivatives'
         WHEN tag ILIKE '%Goodwill%' OR tag ILIKE '%IntangibleAssets%' THEN 'goodwill_intangibles'
         WHEN tag ILIKE '%Restructuring%' THEN 'restructuring'
         WHEN tag ILIKE '%FairValue%' THEN 'fair_value'
         WHEN tag ILIKE '%BusinessCombination%' OR tag ILIKE '%Acquisition%' THEN 'acquisitions'
         WHEN tag ILIKE '%StockholdersEquity%' OR tag ILIKE '%EquityMethod%' THEN 'equity'
         WHEN tag ILIKE '%AccountingPolic%' OR tag ILIKE '%BasisOfPresentation%'
              OR tag ILIKE '%SignificantAccounting%' THEN 'accounting_policies'
         WHEN tag ILIKE '%EmployeeBenefitPlan%' THEN 'benefit_plan_filing'
         ELSE 'other' END"""

# (signal, SQL predicate over `txt`, why a credit analyst cares).
# Phrases are chosen to be specific: "covenant" alone appears in every debt note ever
# written, so it is paired with words that indicate the covenant was actually breached.
SIGNALS: list[tuple[str, str, str]] = [
    ("going_concern",
     "txt LIKE '%substantial doubt%' AND txt LIKE '%going concern%'",
     "Auditor or management doubts the company can continue operating"),
    ("material_weakness",
     "txt LIKE '%material weakness%'",
     "Internal controls over financial reporting are deficient"),
    ("restatement",
     "(txt LIKE '%restatement%' OR txt LIKE '%restated%') "
     "AND (txt LIKE '%previously issued%' OR txt LIKE '%non-reliance%')",
     "Previously published figures were wrong"),
    ("covenant_breach",
     "txt LIKE '%covenant%' AND (txt LIKE '%violation%' OR txt LIKE '%breach%' "
     "OR txt LIKE '%not in compliance%' OR txt LIKE '%failed to comply%')",
     "A borrowing covenant was breached"),
    ("covenant_waiver",
     "txt LIKE '%covenant%' AND (txt LIKE '%waiver%' OR txt LIKE '%waived%' "
     "OR txt LIKE '%amend%')",
     "Lenders waived or renegotiated a covenant - often precedes trouble"),
    ("event_of_default",
     "txt LIKE '%event of default%' OR txt LIKE '%in default under%'",
     "Default provisions discussed as live rather than hypothetical"),
    ("cross_default",
     "txt LIKE '%cross-default%' OR txt LIKE '%cross default%'",
     "One default can trigger others across the debt structure"),
    ("liquidity_warning",
     "txt LIKE '%sufficient liquidity%' OR txt LIKE '%additional financing%' "
     "OR txt LIKE '%may not be able to continue%' OR txt LIKE '%need to raise%'",
     "Management flags a funding need"),
    ("debt_acceleration",
     "txt LIKE '%accelerat%' AND txt LIKE '%indebtedness%'",
     "Debt could be or has been called early"),
    ("impairment_discussion",
     "txt LIKE '%impairment charge%' OR txt LIKE '%goodwill impairment%'",
     "Assets written down"),
    ("class_action",
     "txt LIKE '%class action%'",
     "Material litigation exposure"),
    ("regulatory_investigation",
     "txt LIKE '%subpoena%' OR txt LIKE '%sec investigation%' "
     "OR txt LIKE '%department of justice%'",
     "Regulatory or criminal exposure"),
    ("refinancing",
     "txt LIKE '%refinanc%'",
     "Debt structure being reworked"),
    ("dividend_restriction",
     "txt LIKE '%restrict%' AND txt LIKE '%dividend%'",
     "Lenders limit distributions - a sign of tight terms"),
]


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


def build(lo: int, hi: int) -> None:
    con = connect()
    tag = f"a{lo}_{hi}"
    signal_cols = ",\n               ".join(
        f"coalesce(bool_or({pred}), FALSE) AS {name}" for name, pred, _ in SIGNALS)
    evidence_cols = ",\n               ".join(
        f"arg_max(note_type, CASE WHEN {pred} THEN 1 ELSE 0 END) "
        f"FILTER (WHERE {pred}) AS {name}_note" for name, pred, _ in SIGNALS)

    print(f"Extracting note signals, archives {lo}-{hi} ...")
    con.execute(f"""
        COPY (
            WITH sub AS (
                SELECT adsh, period, TRY_CAST(cik AS BIGINT) AS cik, name, sic, form,
                       TRY_CAST(strptime(filed, '%Y%m%d') AS DATE) AS filed,
                       TRY_CAST(fy AS INTEGER) AS fy
                FROM read_parquet('{LAKE}/parquet/sec/fsn/sub/*/data.parquet',
                                  hive_partitioning = true, union_by_name = true)
                WHERE TRY_CAST(substr(period, 1, 4) AS INTEGER) BETWEEN {lo} AND {hi}
                  AND form IN ('10-K', '10-K/A', '10-Q', '10-Q/A', '20-F', '40-F')),
            txt AS (
                SELECT adsh, period, tag,
                       TRY_CAST(strptime(ddate, '%Y%m%d') AS DATE) AS period_end,
                       TRY_CAST(txtlen AS BIGINT) AS chars,
                       {NOTE_TYPE_SQL} AS note_type,
                       lower(value) AS txt
                FROM read_parquet('{LAKE}/parquet/sec/fsn/txt/*/data.parquet',
                                  hive_partitioning = true, union_by_name = true)
                WHERE TRY_CAST(substr(period, 1, 4) AS INTEGER) BETWEEN {lo} AND {hi}
                  AND tag LIKE '%TextBlock' AND value IS NOT NULL AND length(value) > 40),
            joined AS (
                SELECT s.cik, s.name AS company_name, s.sic, s.form, s.fy, s.filed,
                       s.adsh, t.period_end, t.note_type, t.chars, t.txt,
                       year(t.period_end) AS period_year
                FROM txt t JOIN sub s ON s.adsh = t.adsh AND s.period = t.period)
            SELECT cik, any_value(company_name) AS company_name, any_value(sic) AS sic,
                   adsh, any_value(form) AS form, any_value(fy) AS fy,
                   any_value(filed) AS filed, max(period_end) AS period_end,
                   period_year,
                   count(*) AS note_blocks,
                   sum(chars) AS total_chars,
                   count(DISTINCT note_type) AS note_types,
                   {signal_cols},
                   {evidence_cols}
            FROM joined
            GROUP BY cik, adsh, period_year
        ) TO '{SIGNALS_OUT}' (FORMAT PARQUET, COMPRESSION ZSTD,
                              PARTITION_BY (period_year), OVERWRITE_OR_IGNORE,
                              FILENAME_PATTERN 'sig_{tag}_{{i}}')""")
    try:
        n = con.execute(
            f"SELECT count(*) FROM read_parquet('{SIGNALS_OUT}/*/sig_{tag}_*.parquet')"
        ).fetchone()[0]
        print(f"DONE {lo}-{hi}: {n:,} filings scanned")
    except duckdb.IOException:
        print(f"DONE {lo}-{hi}: no rows")


def register() -> None:
    md = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")

    # A view, not a copy: the text is already 8.9 GB in the lake and duplicating it
    # buys nothing.
    md.execute("DROP VIEW IF EXISTS quali.note_text")
    md.execute(f"""
        CREATE VIEW quali.note_text AS
        SELECT t.adsh, t.tag,
               TRY_CAST(strptime(t.ddate, '%Y%m%d') AS DATE) AS period_end,
               {NOTE_TYPE_SQL} AS note_type,
               TRY_CAST(t.txtlen AS BIGINT) AS chars,
               TRY_CAST(t.srclen AS BIGINT) AS source_chars,
               (TRY_CAST(t.txtlen AS BIGINT) < TRY_CAST(t.srclen AS BIGINT))
                   AS possibly_truncated,
               t.value AS note_text, t.period
        FROM raw.fsn_txt t
        WHERE t.tag LIKE '%TextBlock' AND t.value IS NOT NULL""")
    print("view  quali.note_text")

    md.execute(f"""
        CREATE OR REPLACE TABLE quali.note_signals AS
        SELECT * FROM read_parquet('{SIGNALS_OUT}/*/*.parquet',
                                   hive_partitioning = true, union_by_name = true)""")
    rows, companies = md.execute(
        "SELECT count(*), count(DISTINCT cik) FROM quali.note_signals").fetchone()
    print(f"table quali.note_signals  {rows:,} filings, {companies:,} companies")

    md.execute("DROP TABLE IF EXISTS ref.signal_definitions")
    md.execute("""CREATE TABLE ref.signal_definitions (
                      signal VARCHAR, pattern VARCHAR, why_it_matters VARCHAR)""")
    md.executemany("INSERT INTO ref.signal_definitions VALUES (?, ?, ?)",
                   [(n, p, w) for n, p, w in SIGNALS])
    print(f"table ref.signal_definitions  {len(SIGNALS)} signals")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archives", default="")
    ap.add_argument("--register", action="store_true")
    args = ap.parse_args()
    if args.register:
        register()
        return
    lo, _, hi = args.archives.partition("-")
    build(int(lo), int(hi or lo))


if __name__ == "__main__":
    main()
