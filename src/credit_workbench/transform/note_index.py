"""Which note was this fact presented in?

Every fact is reachable, but until now a fact carried a statement and not a note, so
"everything in the Debt note for this company" was not a query anyone could write. The
filing already knows: the presentation linkbase assigns each tag to a numbered report,
and the rendering file gives those reports the filer's own heading - "Fair Value
Measurements", "Income Taxes", "Leases".

Three things the probe settled, and that shape what is built here.

The join is complete. Every presentation row resolves to a titled report, and every fact
in a filing resolves to at least one report, so nothing is left unassigned.

The relationship is many-to-many, not a lookup. A figure shown on the balance sheet and
again in the debt note belongs to both, and 57,977 tag-filings in a single month appear
in two reports, some in ten or more. So this is a bridge table; collapsing it to one
note per fact would be a decision to lose real information.

Filings already distinguish the note text from the schedule inside it. `menucat` marks
each report as a statement, a note, a detail block - which is where the dimensioned
schedules are presented - a table, or an accounting policy. That distinction is worth
keeping, because "the Debt note" and "the Debt maturity schedule" are different asks.

Titles are as the filer wrote them, so `FAIR VALUE MEASUREMENTS` and `Fair Value
Measurements` are the same note typed differently. A normalised title and a classified
note type make them one thing to query.
"""
from __future__ import annotations

import argparse

import duckdb

from credit_workbench.common.config import R2, motherduck_token

LAKE = "r2://credit-workbench-raw"
NOTE_INDEX = f"{LAKE}/parquet/derived/note_index"
TAG_NOTE = f"{LAKE}/parquet/derived/tag_note_map"

# The filer's heading, normalised, then classified. Order matters: the more specific
# pattern has to win, so debt is tested before the generic "financial instruments".
NOTE_TYPE_SQL = """
    CASE
      WHEN t LIKE '%significant accounting polic%' OR t LIKE '%basis of presentation%'
        OR t LIKE '%summary of accounting%'                      THEN 'accounting_policies'
      WHEN t LIKE '%fair value%'                                 THEN 'fair_value'
      WHEN t LIKE '%income tax%' OR t LIKE '%deferred tax%'      THEN 'income_taxes'
      WHEN t LIKE '%lease%'                                      THEN 'leases'
      WHEN t LIKE '%debt%' OR t LIKE '%borrowing%'
        OR t LIKE '%credit facilit%' OR t LIKE '%notes payable%'
        OR t LIKE '%long-term obligation%'                       THEN 'debt'
      WHEN t LIKE '%segment%'                                    THEN 'segments'
      WHEN t LIKE '%goodwill%' OR t LIKE '%intangible%'          THEN 'goodwill_intangibles'
      WHEN t LIKE '%pension%' OR t LIKE '%postretirement%'
        OR t LIKE '%benefit plan%' OR t LIKE '%retirement%'      THEN 'pension_opeb'
      WHEN t LIKE '%commitment%' OR t LIKE '%contingenc%'
        OR t LIKE '%litigation%' OR t LIKE '%legal proceeding%'  THEN 'commitments_contingencies'
      WHEN t LIKE '%related party%' OR t LIKE '%related-party%'  THEN 'related_party'
      WHEN t LIKE '%derivative%' OR t LIKE '%hedg%'              THEN 'derivatives_hedging'
      WHEN t LIKE '%share-based%' OR t LIKE '%stock-based%'
        OR t LIKE '%stock option%' OR t LIKE '%equity incentive%' THEN 'share_based_compensation'
      WHEN t LIKE '%revenue%'                                    THEN 'revenue'
      WHEN t LIKE '%acquisition%' OR t LIKE '%business combination%'
        OR t LIKE '%merger%'                                     THEN 'business_combinations'
      WHEN t LIKE '%subsequent event%'                           THEN 'subsequent_events'
      WHEN t LIKE '%going concern%'                              THEN 'going_concern'
      WHEN t LIKE '%restructur%' OR t LIKE '%impairment%'
        OR t LIKE '%exit cost%'                                  THEN 'restructuring_impairment'
      WHEN t LIKE '%inventor%'                                   THEN 'inventory'
      WHEN t LIKE '%property%' OR t LIKE '%equipment%'           THEN 'property_plant_equipment'
      WHEN t LIKE '%receivable%' OR t LIKE '%credit loss%'
        OR t LIKE '%allowance%'                                  THEN 'receivables_credit_losses'
      WHEN t LIKE '%equity%' OR t LIKE '%stockholder%'
        OR t LIKE '%shareholder%' OR t LIKE '%capital stock%'    THEN 'equity'
      WHEN t LIKE '%investment%' OR t LIKE '%securit%'           THEN 'investments'
      WHEN t LIKE '%variable interest%' OR t LIKE '%consolidat%' THEN 'consolidation_vie'
      WHEN t LIKE '%discontinued%' OR t LIKE '%held for sale%'   THEN 'discontinued_operations'
      WHEN t LIKE '%concentration%' OR t LIKE '%risks and uncertaint%' THEN 'concentrations_risks'
      WHEN t LIKE '%earnings per share%' OR t LIKE '%per share%' THEN 'earnings_per_share'
      WHEN t LIKE '%cash%' AND t LIKE '%equivalent%'             THEN 'cash'
      WHEN t LIKE '%cybersecurity%'                              THEN 'cybersecurity'
      WHEN t LIKE '%insider trading%' OR t LIKE '%pay vs performance%'
        OR t LIKE '%award timing%'                               THEN 'governance_disclosures'
      ELSE 'other'
    END"""

MENU_CATEGORY_SQL = """
    CASE menucat
      WHEN 'S' THEN 'statement'
      WHEN 'N' THEN 'note'
      WHEN 'D' THEN 'note_detail'      -- where the dimensioned schedules are presented
      WHEN 'T' THEN 'note_table'
      WHEN 'P' THEN 'accounting_policy'
      WHEN 'C' THEN 'cover'
      ELSE 'other'
    END"""


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

    print(f"Building the note index from the rendering file, {lo}-{hi} ...")
    con.execute(f"""
        COPY (
            WITH r AS (
                SELECT adsh, period,
                       TRY_CAST(report AS INTEGER) AS report,
                       shortname AS note_title, longname, menucat, roleuri,
                       TRY_CAST(parentreport AS INTEGER) AS parent_report,
                       TRY_CAST(ultparentrpt AS INTEGER) AS root_report,
                       TRY_CAST(substr(period, 1, 4) AS INTEGER) AS archive_year,
                       -- Titles are typed by the filer, so FAIR VALUE MEASUREMENTS and
                       -- Fair Value Measurements are one note written two ways.
                       lower(regexp_replace(trim(shortname), '\\s+', ' ', 'g')) AS t
                FROM read_parquet('{LAKE}/parquet/sec/fsn/ren/*/data.parquet',
                                  hive_partitioning = true, union_by_name = true)
                WHERE TRY_CAST(substr(period, 1, 4) AS INTEGER) BETWEEN {lo} AND {hi}
                  AND shortname IS NOT NULL AND shortname <> '')
            SELECT adsh, period, report, note_title, longname, roleuri,
                   parent_report, root_report, archive_year,
                   t AS note_title_normalised,
                   {MENU_CATEGORY_SQL} AS note_category,
                   {NOTE_TYPE_SQL} AS note_type
            FROM r
        ) TO '{NOTE_INDEX}' (FORMAT PARQUET, COMPRESSION ZSTD,
                             PARTITION_BY (archive_year), OVERWRITE_OR_IGNORE,
                             FILENAME_PATTERN 'ni_{tag}_{{i}}')""")

    print(f"Building the tag-to-note bridge from the presentation linkbase, {lo}-{hi} ...")
    con.execute(f"""
        COPY (
            SELECT DISTINCT adsh, period, tag,
                   TRY_CAST(report AS INTEGER) AS report,
                   TRY_CAST(line AS INTEGER) AS line,
                   nullif(stmt, '') AS statement,
                   plabel AS presented_label,
                   TRY_CAST(substr(period, 1, 4) AS INTEGER) AS archive_year
            FROM read_parquet('{LAKE}/parquet/sec/fsn/pre/*/data.parquet',
                              hive_partitioning = true, union_by_name = true)
            WHERE TRY_CAST(substr(period, 1, 4) AS INTEGER) BETWEEN {lo} AND {hi}
              AND tag IS NOT NULL AND tag <> ''
        ) TO '{TAG_NOTE}' (FORMAT PARQUET, COMPRESSION ZSTD,
                           PARTITION_BY (archive_year), OVERWRITE_OR_IGNORE,
                           FILENAME_PATTERN 'tn_{tag}_{{i}}')""")

    for label, path in (("note_index", f"{NOTE_INDEX}/*/ni_{tag}_*.parquet"),
                        ("tag_note_map", f"{TAG_NOTE}/*/tn_{tag}_*.parquet")):
        try:
            n = con.execute(f"SELECT count(*) FROM read_parquet('{path}')").fetchone()[0]
            print(f"DONE {label} {lo}-{hi}: {n:,} rows")
        except duckdb.IOException:
            print(f"DONE {label} {lo}-{hi}: no rows")


def register() -> None:
    md = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")

    md.execute("DROP VIEW IF EXISTS ref.note_index")
    md.execute(f"""
        CREATE VIEW ref.note_index AS SELECT * FROM read_parquet(
            '{NOTE_INDEX}/*/*.parquet', hive_partitioning = true, union_by_name = true)""")
    md.execute("DROP VIEW IF EXISTS ref.tag_note_map")
    md.execute(f"""
        CREATE VIEW ref.tag_note_map AS SELECT * FROM read_parquet(
            '{TAG_NOTE}/*/*.parquet', hive_partitioning = true, union_by_name = true)""")
    for name in ("ref.note_index", "ref.tag_note_map"):
        print(f"view  {name}  "
              f"{md.execute(f'SELECT count(*) FROM {name}').fetchone()[0]:,} rows")

    # Consolidated facts under the note they were presented in. A fact shown in two
    # places appears twice by design - that is the disclosure, not a duplicate - so
    # anything counting money must pick a note_category rather than sum across them.
    md.execute("DROP VIEW IF EXISTS marts.facts_by_note")
    md.execute("""
        CREATE VIEW marts.facts_by_note AS
        SELECT f.cik, f.company_name, f.sic, f.adsh, f.period_end, f.qtrs, f.fy,
               f.tag, f.uom, f.value, f.is_latest, f.is_first_report,
               n.note_title, n.note_title_normalised, n.note_type, n.note_category,
               m.presented_label, m.line, n.report
        FROM staging.facts_pit f
        JOIN ref.tag_note_map m ON m.adsh = f.adsh AND m.tag = f.tag
        JOIN ref.note_index  n ON n.adsh = m.adsh AND n.report = m.report
                              AND n.period = m.period""")
    print("view  marts.facts_by_note")

    # The same for the schedules, which is where the detail actually lives.
    md.execute("DROP VIEW IF EXISTS marts.schedules_by_note")
    md.execute("""
        CREATE VIEW marts.schedules_by_note AS
        SELECT f.cik, f.company_name, f.sic, f.adsh, f.period_end, f.qtrs, f.fy,
               f.tag, f.uom, f.value, f.dimh, f.dimension_count, f.is_latest,
               n.note_title, n.note_title_normalised, n.note_type, n.note_category,
               m.presented_label, n.report
        FROM marts.facts_dimensioned f
        JOIN ref.tag_note_map m ON m.adsh = f.adsh AND m.tag = f.tag
        JOIN ref.note_index  n ON n.adsh = m.adsh AND n.report = m.report
                              AND n.period = m.period""")
    print("view  marts.schedules_by_note")

    md.execute("DROP TABLE IF EXISTS ref.note_catalog")
    md.execute("""
        CREATE TABLE ref.note_catalog AS
        SELECT note_type, note_category,
               count(*) AS reports,
               count(DISTINCT adsh) AS filings,
               count(DISTINCT note_title_normalised) AS distinct_titles,
               any_value(note_title) AS example_title
        FROM ref.note_index
        GROUP BY 1, 2 ORDER BY reports DESC""")
    rows, types = md.execute("""
        SELECT count(*), count(DISTINCT note_type) FROM ref.note_catalog""").fetchone()
    print(f"table ref.note_catalog  {rows:,} rows across {types} note types")


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
