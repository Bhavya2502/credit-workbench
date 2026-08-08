"""Instrument-level debt detail and the maturity profile it supports.

Aggregate debt tells a credit analyst how much; instrument detail tells them when it
comes due, at what coupon, and on what terms. That sits in the dimensioned facts under
a DebtInstrument axis, where the member name identifies the security — and usefully,
names it in a way that often carries the maturity: `ConvertibleSeniorNotesDue2030`,
`Zero625PercentConvertibleSeniorNotesDue2026`.

  marts.debt_instruments      one row per company / period / instrument
  marts.debt_maturity_profile face value falling due by year, per company
  marts.revolver_capacity     committed facilities and how much is undrawn

The maturity year is parsed from the member name where the filer put it there, and
flagged as `maturity_source` so nobody mistakes an inference for a disclosure. The
aggregate maturity ladder captured in D1 remains the authoritative total; this adds
the instrument breakdown beneath it.
"""
from __future__ import annotations

import argparse

import duckdb

from credit_workbench.common.config import R2, motherduck_token

LAKE = "r2://credit-workbench-raw"
OUT = f"{LAKE}/parquet/derived/debt_instruments"

# tag -> output column. Amounts are USD; rates are 'pure' fractions.
INSTRUMENT_TAGS = {
    "face_amount": ["DebtInstrumentFaceAmount", "DebtInstrumentIssuedPrincipal"],
    "carrying_amount": ["DebtInstrumentCarryingAmount", "LongTermDebt",
                        "LongTermDebtNoncurrent", "LineOfCredit"],
    "stated_rate": ["DebtInstrumentInterestRateStatedPercentage"],
    "effective_rate": ["DebtInstrumentInterestRateEffectivePercentage",
                       "DebtInstrumentInterestRateDuringPeriod"],
    "basis_spread": ["DebtInstrumentBasisSpreadOnVariableRate1"],
    "unamortised_discount": ["DebtInstrumentUnamortizedDiscount"],
    "fair_value": ["DebtInstrumentFairValue", "LongTermDebtFairValue"],
    "periodic_payment": ["DebtInstrumentPeriodicPayment"],
    "conversion_price": ["DebtInstrumentConvertibleConversionPrice1"],
}

FACILITY_TAGS = {
    "facility_maximum": ["LineOfCreditFacilityMaximumBorrowingCapacity"],
    "facility_remaining": ["LineOfCreditFacilityRemainingBorrowingCapacity"],
    "facility_drawn": ["LineOfCreditFacilityAmountOutstanding", "LineOfCredit"],
}

# Instrument type from the member name, most specific first.
TYPE_SQL = """
    CASE WHEN member ILIKE '%convertible%' THEN 'convertible'
         WHEN member ILIKE '%revolv%' OR member ILIKE '%creditfacility%'
              OR member ILIKE '%creditagreement%' THEN 'revolver'
         WHEN member ILIKE '%termloan%' OR member ILIKE '%term_loan%' THEN 'term_loan'
         WHEN member ILIKE '%mortgage%' THEN 'mortgage'
         WHEN member ILIKE '%subordinat%' THEN 'subordinated'
         WHEN member ILIKE '%seniorsecured%' THEN 'senior_secured'
         WHEN member ILIKE '%seniorunsecured%' OR member ILIKE '%seniornote%'
              THEN 'senior_unsecured'
         WHEN member ILIKE '%promissory%' THEN 'promissory_note'
         WHEN member ILIKE '%debenture%' THEN 'debenture'
         WHEN member ILIKE '%capitallease%' OR member ILIKE '%financelease%'
              THEN 'finance_lease'
         WHEN member ILIKE '%note%' THEN 'notes'
         ELSE 'other' END"""

# Prefer an explicit "Due2030"; fall back to any plausible year in the name.
MATURITY_SQL = """
    coalesce(
        TRY_CAST(regexp_extract(member, '[Dd]ue.?(20[2-9][0-9])', 1) AS INTEGER),
        TRY_CAST(regexp_extract(member, 'Maturing.?(20[2-9][0-9])', 1) AS INTEGER))"""
MATURITY_LOOSE_SQL = """
    TRY_CAST(regexp_extract(member, '(20[2-9][0-9])', 1) AS INTEGER)"""


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
    all_tags = {t for tags in INSTRUMENT_TAGS.values() for t in tags}
    all_tags |= {t for tags in FACILITY_TAGS.values() for t in tags}
    quoted = ", ".join(f"'{t}'" for t in sorted(all_tags))

    con.execute("CREATE OR REPLACE TABLE colmap (tag VARCHAR, col VARCHAR, priority INTEGER)")
    con.executemany("INSERT INTO colmap VALUES (?, ?, ?)",
                    [(t, col, i)
                     for group in (INSTRUMENT_TAGS, FACILITY_TAGS)
                     for col, tags in group.items()
                     for i, t in enumerate(tags)])

    cols = list(INSTRUMENT_TAGS) + list(FACILITY_TAGS)
    pivot = ",\n                   ".join(
        f"max(CASE WHEN col = '{c}' THEN value END) AS {c}" for c in cols)
    tag = f"a{lo}_{hi}"

    print(f"Extracting instrument-level debt, archives {lo}-{hi} ...")
    con.execute(f"""
        COPY (
            WITH sub AS (
                SELECT adsh, period, TRY_CAST(cik AS BIGINT) AS cik, name, sic, form,
                       TRY_CAST(strptime(filed, '%Y%m%d') AS DATE) AS filed,
                       TRY_CAST(fy AS INTEGER) AS fy
                FROM read_parquet('{LAKE}/parquet/sec/fsn/sub/*/data.parquet',
                                  hive_partitioning = true, union_by_name = true)
                WHERE TRY_CAST(substr(period, 1, 4) AS INTEGER) BETWEEN {lo} AND {hi}),
            dims AS (
                SELECT dimhash, period, segments,
                       regexp_extract(segments, 'DebtInstrument=([^;]+)', 1) AS member
                FROM read_parquet('{LAKE}/parquet/sec/fsn/dim/*/data.parquet',
                                  hive_partitioning = true, union_by_name = true)
                WHERE TRY_CAST(substr(period, 1, 4) AS INTEGER) BETWEEN {lo} AND {hi}
                  AND segments LIKE '%DebtInstrument=%'),
            facts AS (
                SELECT n.adsh, n.period, n.tag, n.dimh, n.uom,
                       TRY_CAST(strptime(n.ddate, '%Y%m%d') AS DATE) AS period_end,
                       TRY_CAST(n.value AS DOUBLE) AS value
                FROM read_parquet('{LAKE}/parquet/sec/fsn/num/*/data.parquet',
                                  hive_partitioning = true, union_by_name = true) n
                WHERE TRY_CAST(substr(n.period, 1, 4) AS INTEGER) BETWEEN {lo} AND {hi}
                  AND n.dimn <> '0' AND n.iprx = '0' AND n.tag IN ({quoted})
                  AND n.value IS NOT NULL AND n.value <> ''),
            resolved AS (
                SELECT s.cik, s.name AS company_name, s.sic, s.form, s.fy, s.filed,
                       f.adsh, f.period_end, d.member, m.col, f.value, f.uom,
                       row_number() OVER (
                           PARTITION BY s.cik, f.adsh, f.period_end, d.member, m.col
                           ORDER BY m.priority) AS pick
                FROM facts f
                JOIN sub s  ON s.adsh = f.adsh AND s.period = f.period
                JOIN dims d ON d.dimhash = f.dimh AND d.period = f.period
                JOIN colmap m ON m.tag = f.tag
                WHERE d.member <> '')
            SELECT cik, any_value(company_name) AS company_name, any_value(sic) AS sic,
                   adsh, any_value(form) AS form, any_value(fy) AS fy,
                   any_value(filed) AS filed, period_end, member,
                   {TYPE_SQL} AS instrument_type,
                   coalesce({MATURITY_SQL}, {MATURITY_LOOSE_SQL}) AS maturity_year,
                   CASE WHEN {MATURITY_SQL} IS NOT NULL THEN 'named_due_year'
                        WHEN {MATURITY_LOOSE_SQL} IS NOT NULL THEN 'inferred_from_name'
                        END AS maturity_source,
                   {pivot},
                   year(period_end) AS period_year
            FROM resolved WHERE pick = 1
            GROUP BY cik, adsh, period_end, member
        ) TO '{OUT}' (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (period_year),
                      OVERWRITE_OR_IGNORE, FILENAME_PATTERN 'di_{tag}_{{i}}')""")
    try:
        n = con.execute(
            f"SELECT count(*) FROM read_parquet('{OUT}/*/di_{tag}_*.parquet')").fetchone()[0]
        print(f"DONE {lo}-{hi}: {n:,} instrument-periods")
    except duckdb.IOException:
        print(f"DONE {lo}-{hi}: no rows")


def register() -> None:
    md = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    md.execute("DROP VIEW IF EXISTS marts.debt_instruments")
    md.execute(f"""
        CREATE VIEW marts.debt_instruments AS SELECT * FROM read_parquet(
            '{OUT}/*/*.parquet', hive_partitioning = true, union_by_name = true)""")
    rows, companies = md.execute(
        "SELECT count(*), count(DISTINCT cik) FROM marts.debt_instruments").fetchone()
    print(f"view  marts.debt_instruments  {rows:,} instrument-periods, "
          f"{companies:,} companies")

    md.execute("""
        CREATE OR REPLACE TABLE marts.debt_maturity_profile AS
        SELECT cik, any_value(company_name) AS company_name, fy, period_end,
               maturity_year,
               maturity_year - year(period_end)        AS years_to_maturity,
               count(*)                                AS instruments,
               sum(coalesce(face_amount, carrying_amount)) AS amount_due,
               avg(stated_rate)                        AS avg_stated_rate,
               list_distinct(list(instrument_type))    AS instrument_types
        FROM marts.debt_instruments
        WHERE maturity_year IS NOT NULL
          AND maturity_year BETWEEN year(period_end) AND year(period_end) + 40
          AND coalesce(face_amount, carrying_amount) > 0
        GROUP BY cik, fy, period_end, maturity_year""")
    print(f"table marts.debt_maturity_profile  "
          f"{md.execute('SELECT count(*) FROM marts.debt_maturity_profile').fetchone()[0]:,} rows")

    md.execute("""
        CREATE OR REPLACE TABLE marts.revolver_capacity AS
        SELECT cik, any_value(company_name) AS company_name, fy, period_end,
               sum(facility_maximum)   AS committed_facilities,
               sum(facility_remaining) AS undrawn,
               sum(facility_drawn)     AS drawn,
               sum(facility_remaining) / nullif(sum(facility_maximum), 0) AS pct_undrawn,
               count(*) AS facilities
        FROM marts.debt_instruments
        WHERE facility_maximum > 0
        GROUP BY cik, fy, period_end""")
    print(f"table marts.revolver_capacity  "
          f"{md.execute('SELECT count(*) FROM marts.revolver_capacity').fetchone()[0]:,} rows")


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
