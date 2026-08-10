"""Every dimensioned fact — the detailed note schedules — in one place.

Two thirds of all filed facts carry a dimension: by subsidiary, by plan, by instrument,
by fair-value level, by counterparty, by share class. Those *are* the schedules inside
the notes, and until now only six axes had been claimed by bespoke extractors
(segments, concentration, debt instruments). Writing a new extractor per axis does not
reach "all schedules" — there are dozens of axes and the list grows with the taxonomy.

So this covers them generically:

  ref.dimension_index      every axis=member pair, exploded from the dimension table
                           rather than from the facts. The dimension table is small and
                           each hash is shared by many facts, so exploding there costs
                           almost nothing while exploding the facts would multiply
                           tens of millions of rows.
  marts.facts_dimensioned  every dimensioned fact, one row each, keyed to its dimension
                           hash. Join to the index to slice by any axis.
  named views              the axes worth a front door of their own — subsidiary
                           detail, fair-value hierarchy, related parties, acquisitions,
                           share-based awards, investments, equity components.

Together with `staging.facts_pit` (the consolidated half) this means every numeric fact
in every filing is now reachable through a mart, not just the ones a bespoke extractor
happened to claim.
"""
from __future__ import annotations

import argparse

import duckdb

from credit_workbench.common.config import R2, motherduck_token

LAKE = "r2://credit-workbench-raw"
DIM_INDEX = f"{LAKE}/parquet/derived/dimension_index"
FACTS_DIM = f"{LAKE}/parquet/derived/facts_dimensioned"
FACTS_PIT = f"{LAKE}/parquet/derived/facts_dimensioned_pit"

# Axes that earn a named view. The generic mart still carries every other axis.
NAMED_AXES: dict[str, tuple[str, str]] = {
    "legal_entity": ("LegalEntity",
                     "Subsidiary and guarantor-level figures - the basis of structural "
                     "subordination analysis"),
    "fair_value_hierarchy": ("FairValueByFairValueHierarchyLevel",
                             "Level 1/2/3 split; heavy Level 3 is a valuation-risk signal"),
    "related_party": ("RelatedPartyTransactionsByRelatedParty",
                      "Exposure by counterparty"),
    "acquisitions": ("BusinessAcquisition", "Purchase price allocation by target"),
    "share_awards": ("AwardType", "Share-based compensation by award type"),
    "investments": ("InvestmentType", "Investment portfolio composition"),
    "equity_components": ("EquityComponents", "Equity rollforward by component"),
    "consolidated_entities": ("ConsolidatedEntities", "Consolidation scope"),
    "class_of_stock": ("ClassOfStock", "Figures by share class"),
    "financing_receivables": ("FinancingReceivablePortfolioSegment",
                              "Loan book by portfolio segment"),
    "derivative_risk": ("DerivativeInstrumentRisk", "Derivatives by risk type"),
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


def build(lo: int, hi: int) -> None:
    con = connect()
    tag = f"a{lo}_{hi}"

    print(f"Exploding dimension hashes into axis/member pairs, {lo}-{hi} ...")
    con.execute(f"""
        COPY (
            SELECT period, dimhash,
                   split_part(pair, '=', 1) AS axis,
                   split_part(pair, '=', 2) AS member,
                   full_dimension, dimension_truncated, archive_year
            FROM (
                SELECT d.period, d.dimhash, d.segments AS full_dimension,
                       TRY_CAST(d.segt AS INTEGER) AS dimension_truncated,
                       TRY_CAST(substr(d.period, 1, 4) AS INTEGER) AS archive_year,
                       -- `segments` is 'Axis=Member;Axis=Member;' with the redundant
                       -- 'Axis' suffix already stripped by SEC
                       unnest(str_split(rtrim(d.segments, ';'), ';')) AS pair
                FROM read_parquet('{LAKE}/parquet/sec/fsn/dim/*/data.parquet',
                                  hive_partitioning = true, union_by_name = true) d
                WHERE TRY_CAST(substr(d.period, 1, 4) AS INTEGER) BETWEEN {lo} AND {hi}
                  AND d.segments IS NOT NULL AND d.segments <> '')
            WHERE split_part(pair, '=', 1) <> '' AND split_part(pair, '=', 2) <> ''
        ) TO '{DIM_INDEX}' (FORMAT PARQUET, COMPRESSION ZSTD,
                            PARTITION_BY (archive_year), OVERWRITE_OR_IGNORE,
                            FILENAME_PATTERN 'dx_{tag}_{{i}}')""")

    print(f"Writing every dimensioned fact, {lo}-{hi} ...")
    con.execute(f"""
        COPY (
            WITH sub AS (
                SELECT adsh, period, TRY_CAST(cik AS BIGINT) AS cik, name, sic, form,
                       TRY_CAST(strptime(filed, '%Y%m%d') AS DATE) AS filed,
                       TRY_CAST(fy AS INTEGER) AS fy, fp
                FROM read_parquet('{LAKE}/parquet/sec/fsn/sub/*/data.parquet',
                                  hive_partitioning = true, union_by_name = true)
                WHERE TRY_CAST(substr(period, 1, 4) AS INTEGER) BETWEEN {lo} AND {hi}),
            facts AS (
                SELECT n.adsh, n.period, n.tag, n.version, n.dimh, n.uom, n.coreg,
                       TRY_CAST(strptime(n.ddate, '%Y%m%d') AS DATE) AS period_end,
                       TRY_CAST(n.qtrs AS INTEGER)  AS qtrs,
                       TRY_CAST(n.value AS DOUBLE)  AS value,
                       TRY_CAST(n.dimn AS INTEGER)  AS dimension_count,
                       n.footnote
                FROM read_parquet('{LAKE}/parquet/sec/fsn/num/*/data.parquet',
                                  hive_partitioning = true, union_by_name = true) n
                WHERE TRY_CAST(substr(n.period, 1, 4) AS INTEGER) BETWEEN {lo} AND {hi}
                  AND n.dimn <> '0'      -- the schedules; consolidated lives in facts_pit
                  AND n.iprx = '0'       -- most precise copy of a duplicated figure
                  AND n.value IS NOT NULL AND n.value <> '')
            SELECT s.cik, s.name AS company_name, s.sic, s.form, s.fy, s.fp, s.filed,
                   f.adsh, f.tag, f.version, f.period_end, f.qtrs, f.uom, f.coreg,
                   f.value, f.dimh, f.dimension_count, f.footnote, f.period,
                   year(f.period_end) AS period_year
            FROM facts f JOIN sub s ON s.adsh = f.adsh AND s.period = f.period
        ) TO '{FACTS_DIM}' (FORMAT PARQUET, COMPRESSION ZSTD,
                            PARTITION_BY (period_year), OVERWRITE_OR_IGNORE,
                            FILENAME_PATTERN 'fd_{tag}_{{i}}')""")

    for label, path in (("dimension_index", f"{DIM_INDEX}/*/dx_{tag}_*.parquet"),
                        ("facts_dimensioned", f"{FACTS_DIM}/*/fd_{tag}_*.parquet")):
        try:
            n = con.execute(f"SELECT count(*) FROM read_parquet('{path}')").fetchone()[0]
            print(f"DONE {label} {lo}-{hi}: {n:,} rows")
        except duckdb.IOException:
            print(f"DONE {label} {lo}-{hi}: no rows")


def flag(lo: int, hi: int) -> None:
    """Mark which filing a dimensioned fact should be read from.

    A balance-sheet date is re-reported by the 10-K and by the comparatives in the
    10-Qs that follow, so the lake holds roughly four copies of every figure. Summing
    without choosing one gives a total about four times too large - measured at a median
    ratio of exactly 4.0 against filers' own fair-value totals, which is what exposed
    this. `staging.facts_pit` has carried these flags on the consolidated side all
    along; the schedules need them for the same reason.

    Batched by period year rather than by archive: every copy of a figure shares its
    period end, so a period-year partition holds the whole set to rank. The first pass
    already wrote the data partitioned that way, so this reads it back partition by
    partition instead of rescanning the source archives.
    """
    con = connect()
    if lo == 0:
        # The tail: comparatives reaching back before 2008 and maturity schedules
        # running past 2030, plus a handful of filer typos. Only ~497k facts, but
        # dropping them silently would be worse than carrying the typos. One glob over
        # the whole dataset with a filter on the partition column, which DuckDB prunes
        # to just these partitions rather than reading all 222m rows.
        source = (f"read_parquet('{FACTS_DIM}/*/*.parquet', hive_partitioning = true, "
                  f"union_by_name = true)")
        where = "WHERE period_year IS NULL OR period_year < 2008 OR period_year > 2030"
        tag, label = "ptail", "outside 2008-2030"
    else:
        globs = ", ".join(f"'{FACTS_DIM}/period_year={y}/*.parquet'"
                          for y in range(lo, hi + 1))
        source = f"read_parquet([{globs}], hive_partitioning = true, union_by_name = true)"
        where = ""
        tag, label = f"p{lo}_{hi}", f"{lo}-{hi}"
    print(f"Flagging point-in-time vintage for period years {label} ...")
    con.execute(f"""
        COPY (
            SELECT *,
                   filed = max(filed) OVER w AS is_latest,
                   filed = min(filed) OVER w AS is_first_report,
                   count(*) OVER w           AS filings_reporting
            FROM {source} {where}
            WINDOW w AS (PARTITION BY cik, period_end, qtrs, tag, dimh,
                                      coalesce(coreg, ''), uom)
        ) TO '{FACTS_PIT}' (FORMAT PARQUET, COMPRESSION ZSTD,
                            PARTITION_BY (period_year), OVERWRITE_OR_IGNORE,
                            FILENAME_PATTERN 'fp_{tag}_{{i}}')""")
    n, latest = con.execute(
        f"SELECT count(*), count(*) FILTER (WHERE is_latest) "
        f"FROM read_parquet('{FACTS_PIT}/*/fp_{tag}_*.parquet')").fetchone()
    print(f"DONE {lo}-{hi}: {n:,} rows, {latest:,} flagged is_latest "
          f"({100 * latest / n:.1f}%)")


def register() -> None:
    md = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")

    md.execute("DROP VIEW IF EXISTS ref.dimension_index")
    md.execute(f"""
        CREATE VIEW ref.dimension_index AS SELECT * FROM read_parquet(
            '{DIM_INDEX}/*/*.parquet', hive_partitioning = true, union_by_name = true)""")
    md.execute("DROP VIEW IF EXISTS marts.facts_dimensioned")
    md.execute(f"""
        CREATE VIEW marts.facts_dimensioned AS SELECT * FROM read_parquet(
            '{FACTS_PIT}/*/*.parquet', hive_partitioning = true, union_by_name = true)""")

    for name in ("ref.dimension_index", "marts.facts_dimensioned"):
        print(f"view  {name}  "
              f"{md.execute(f'SELECT count(*) FROM {name}').fetchone()[0]:,} rows")

    # A front door per high-value axis. The generic mart still holds every other axis,
    # so a new one needs a view, not a new pipeline.
    for view, (axis, purpose) in NAMED_AXES.items():
        md.execute(f"DROP VIEW IF EXISTS marts.dim_{view}")
        md.execute(f"""
            CREATE VIEW marts.dim_{view} AS
            SELECT f.cik, f.company_name, f.sic, f.form, f.fy, f.period_end, f.qtrs,
                   f.tag, f.uom, f.value, d.member, d.full_dimension,
                   -- 1 means this axis is the only one on the fact. Anything higher and
                   -- the figure is a cell of a cross-tabulation, so summing over one
                   -- axis alone double counts across the others.
                   f.dimension_count, f.adsh
            FROM marts.facts_dimensioned f
            JOIN ref.dimension_index d
              ON d.dimhash = f.dimh AND d.period = f.period
            WHERE d.axis = '{axis}' AND f.is_latest""")
    md.execute("DROP TABLE IF EXISTS ref.named_axis_view")
    md.execute("""CREATE TABLE ref.named_axis_view (
                      axis VARCHAR, view_name VARCHAR, purpose VARCHAR)""")
    md.executemany("INSERT INTO ref.named_axis_view VALUES (?, ?, ?)",
                   [(axis, f"marts.dim_{view}", purpose)
                    for view, (axis, purpose) in NAMED_AXES.items()])
    print(f"      {len(NAMED_AXES)} named axis views created, "
          f"mapping published to ref.named_axis_view")

    # The two axes asked for by name get a classified column on top of the raw member,
    # because the member string alone is not what an analyst wants to group by.
    #
    # Fair value: match the three levels exactly. Members like
    # `FairValueInputsLevel1AndLevel2` and `FairValueInputsLevel12And3` exist and a
    # LIKE '%Level1%' test would count them into more than one level, so a combined
    # member is labelled as such and kept out of any level total.
    md.execute("DROP VIEW IF EXISTS marts.fair_value_hierarchy")
    md.execute("""
        CREATE VIEW marts.fair_value_hierarchy AS
        SELECT *, CASE member
                      WHEN 'FairValueInputsLevel1' THEN 'Level 1'
                      WHEN 'FairValueInputsLevel2' THEN 'Level 2'
                      WHEN 'FairValueInputsLevel3' THEN 'Level 3'
                      WHEN 'FairValueMeasuredAtNetAssetValuePerShare' THEN 'NAV practical expedient'
                      WHEN 'EstimateOfFairValueFairValueDisclosure' THEN 'Total fair value'
                      WHEN 'CarryingReportedAmountFairValueDisclosure' THEN 'Carrying amount'
                      ELSE 'Combined or other' END AS hierarchy_level
        FROM marts.dim_fair_value_hierarchy""")

    # Legal entity: the credit question is which entity the lender's claim sits at.
    # `ParentCompany` is the SEC Schedule I parent-only presentation; guarantor and
    # non-guarantor members are the Rule 3-10 disclosure. Those three, contrasted with
    # the consolidated figure, are what structural subordination is read from.
    md.execute("DROP VIEW IF EXISTS marts.legal_entity_detail")
    md.execute("""
        CREATE VIEW marts.legal_entity_detail AS
        SELECT *,
               CASE WHEN member = 'ParentCompany' THEN 'parent_only'
                    WHEN member ILIKE 'NonGuarantor%' THEN 'non_guarantor'
                    WHEN member ILIKE '%NonGuarantor%' THEN 'non_guarantor'
                    WHEN member ILIKE '%Guarantor%' THEN 'guarantor'
                    WHEN member ILIKE 'VariableInterestEntity%' THEN 'vie'
                    WHEN member IN ('Subsidiaries', 'SubsidiaryIssuer', 'ConsolidatedFunds',
                                    'AllOtherSubsidiaries') THEN 'subsidiaries_aggregate'
                    WHEN member ILIKE '%Eliminat%' THEN 'eliminations'
                    ELSE 'named_entity' END AS entity_role
        FROM marts.dim_legal_entity""")
    print("view  marts.fair_value_hierarchy  (level-classified)")
    print("view  marts.legal_entity_detail   (entity role classified)")

    md.execute("DROP TABLE IF EXISTS ref.dimension_catalog")
    md.execute("""
        CREATE TABLE ref.dimension_catalog AS
        SELECT axis, count(*) AS member_rows, count(DISTINCT member) AS distinct_members,
               count(DISTINCT dimhash) AS dimension_hashes
        FROM ref.dimension_index GROUP BY 1 ORDER BY 2 DESC""")
    print(f"table ref.dimension_catalog  "
          f"{md.execute('SELECT count(*) FROM ref.dimension_catalog').fetchone()[0]:,} axes")

    # The index of every tag: label, documentation, usage, and whether any mart claims
    # it. This is the map of what exists, so nothing is invisible even when unmapped.
    # Both lookups must be one row per tag before joining. ref.xbrl_tag is keyed by
    # (tag, version) and staging.tag_map can carry a tag at several priorities, so
    # joining either on tag alone would multiply the usage rows and inflate the counts.
    md.execute("""
        CREATE OR REPLACE TABLE ref.tag_catalog AS
        WITH consolidated AS (
            SELECT tag, count(*) AS facts, count(DISTINCT adsh) AS filings,
                   count(DISTINCT cik) AS companies, max(fy) AS last_fy,
                   any_value(stmt) AS statement
            FROM staging.facts_pit WHERE is_latest GROUP BY 1),
        dimensioned AS (
            -- is_latest on both sides, so the two fact counts mean the same thing.
            -- Without it the schedule side counts every re-report of a figure and
            -- would look several times busier than the consolidated side.
            SELECT tag, count(*) AS facts, count(DISTINCT adsh) AS filings,
                   count(DISTINCT cik) AS companies, max(fy) AS last_fy
            FROM marts.facts_dimensioned WHERE is_latest GROUP BY 1),
        -- The universe is every tag that ever carried a fact, superseded vintages
        -- included: a tag a filer used once and then abandoned still needs a catalog
        -- entry. Only the counts above are held to one vintage.
        tags AS (SELECT DISTINCT tag FROM staging.facts_pit
                 UNION SELECT DISTINCT tag FROM marts.facts_dimensioned),
        labels AS (
            SELECT tag, any_value(tlabel) AS label, any_value(doc) AS documentation,
                   any_value(datatype) AS datatype, any_value(crdr) AS debit_credit
            FROM ref.xbrl_tag GROUP BY tag),
        mapped AS (
            SELECT tag, any_value(line_code) AS spread_line FROM staging.tag_map GROUP BY tag)
        SELECT g.tag, l.label, l.documentation, l.datatype, l.debit_credit,
               c.statement,
               coalesce(c.facts, 0)    AS consolidated_facts,
               coalesce(d.facts, 0)    AS dimensioned_facts,
               coalesce(c.facts, 0) + coalesce(d.facts, 0) AS total_facts,
               coalesce(c.filings, 0)  AS consolidated_filings,
               coalesce(d.filings, 0)  AS dimensioned_filings,
               greatest(coalesce(c.companies, 0), coalesce(d.companies, 0)) AS companies,
               greatest(coalesce(c.last_fy, 0), coalesce(d.last_fy, 0))     AS last_seen_fy,
               (l.label IS NOT NULL)   AS standard_taxonomy,
               m.spread_line,
               (m.tag IS NOT NULL)     AS in_spread_template
        FROM tags g
        LEFT JOIN consolidated c ON c.tag = g.tag
        LEFT JOIN dimensioned  d ON d.tag = g.tag
        LEFT JOIN labels       l ON l.tag = g.tag
        LEFT JOIN mapped       m ON m.tag = g.tag""")
    total, labelled, mapped, dim_only = md.execute("""
        SELECT count(*), count(label), count(*) FILTER (WHERE in_spread_template),
               count(*) FILTER (WHERE consolidated_facts = 0 AND dimensioned_facts > 0)
        FROM ref.tag_catalog""").fetchone()
    print(f"table ref.tag_catalog  {total:,} tags, {labelled:,} with taxonomy labels, "
          f"{mapped:,} claimed by the spread template, "
          f"{dim_only:,} appear only in the schedules")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archives", default="")
    ap.add_argument("--period-years", default="")
    ap.add_argument("--register", action="store_true")
    args = ap.parse_args()
    if args.register:
        register()
        return
    if args.period_years:
        if args.period_years == "tail":
            flag(0, 0)
            return
        lo, _, hi = args.period_years.partition("-")
        flag(int(lo), int(hi or lo))
        return
    lo, _, hi = args.archives.partition("-")
    build(int(lo), int(hi or lo))


if __name__ == "__main__":
    main()
