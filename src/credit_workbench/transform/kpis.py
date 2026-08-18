"""G-08 — disclosed operating KPIs, with the dictionary held as data.

The requesting workstream calls this the single biggest upgrade available to business-risk
scorecards, and they are right that same-store sales, RASM, load factor, occupancy and
production volumes are what make a sector scorecard sector-specific rather than a generic
ratio set wearing an industry label. None of it is in XBRL; all of it is in narrative this
warehouse already holds.

**The dictionary is a table, not code.** `ref.kpi_dictionary` holds one row per KPI: the
phrase, the SIC major groups it belongs to, the unit to expect and the range a real value
falls in. Adding an industry is an INSERT. That matters more than any single extractor here,
because the request is explicitly to grow this industry by industry, and an analyst who
knows airlines should be able to add a metric without touching a parser.

**Industry scope is the precision mechanism, not a convenience.** Measured over 2022-2025
MD&A, "backlog" appears for 239 companies inside SIC 35/36/37 and 477 outside it; "average
selling price" 149 inside and 274 outside; "admissions" 31 inside and 62 outside. These are
ordinary English as often as they are metrics. Scoping each phrase to its own industry makes
those hits not happen at all, which is the only reason a phrase that loose can be used
safely.

**What the coverage really is.** Within its own industry a KPI is mentioned by 5% to 68% of
companies, median about 21%: proved reserves 56.5% of 191 energy filers, load factor 46.7%
of 30 airlines, FFO 22.9% of 899 REITs, megawatt hours 5.4% of 168 utilities. The mart
publishes `companies_disclosing` per KPI so a scorecard can refuse a thin factor rather than
weight it blindly, the same discipline `marts.ratio_coverage.is_sufficient` enforces.

**What cannot be read.** Of KPI-bearing lines, 52.4% are prose carrying the value on the
same line, 25.3% are prose discussing the metric with no number, and 19.2% are bare table
labels whose value the cell-per-line conversion at ingest put on another line. The third kind
is out of reach without re-deriving 1.79m sections from source HTML - a re-fetch on the scale
of the original ingest - and is recorded as a known limitation rather than papered over.

**Cost.** `quali.filing_sections` is an unpartitioned view over parquet, so every query
against it reads the whole narrative dataset and the number of passes is the only lever that
matters. One pass explodes sections into the few thousand lines that mention any KPI at all;
every per-KPI extraction then runs over that small table. An earlier probe that asked each
phrase in its own SELECT timed out after an hour without producing a row.
"""
from __future__ import annotations

import argparse
import hashlib

import duckdb

from credit_workbench.common.config import motherduck_token

# kpi, label, phrase, sic2 groups, unit, min, max, and the coverage measured on 2022-2025
# MD&A so a reader can see what each entry is worth before using it.
DICTIONARY = [
    ("energy_reserves", "Proved reserves", "proved reserves", "13,29",
     "count", 0.0, 1e7, 56.5),
    ("airline_load_factor", "Passenger load factor", "load factor", "45",
     "percent", 0.0, 100.0, 46.7),
    ("airline_rasm", "Revenue per available seat mile", "available seat mile", "45",
     "count", 0.0, 100.0, 43.3),
    ("mfg_backlog", "Order backlog", "backlog", "35,36,37",
     "dollar", 0.0, 1e9, 38.0),
    ("restaurant_auv", "Average unit volume", "average unit volume", "58",
     "dollar", 0.0, 1e8, 35.0),
    ("hotel_adr", "Average daily rate", "average daily rate", "70",
     "dollar", 0.0, 5000.0, 67.9),
    ("hotel_revpar", "Revenue per available room", "revpar", "70",
     "dollar", 0.0, 5000.0, 50.0),
    ("health_admissions", "Patient admissions", "admissions", "80",
     "count", 0.0, 1e8, 23.8),
    ("reit_ffo", "Funds from operations", "funds from operations", "65,67",
     "dollar", 0.0, 1e10, 22.9),
    ("semi_asp", "Average selling price", "average selling price", "36,38",
     "dollar", 0.0, 1e6, 21.9),
    ("retail_store_count", "Store count", "store count", "53,54,56,57,59",
     "count", 0.0, 100000.0, 20.7),
    ("retail_comp_sales", "Comparable store sales", "comparable store sales",
     "53,54,56,57,59", "percent", -100.0, 100.0, 20.1),
    ("reit_occupancy", "Occupancy rate", "occupancy rate", "65,67",
     "percent", 0.0, 100.0, 17.0),
    ("telecom_arpu", "Average revenue per user", "average revenue per user", "48",
     "dollar", 0.0, 1000.0, 14.0),
    ("saas_arr", "Annual recurring revenue", "annual recurring revenue", "73",
     "dollar", 0.0, 1e10, 11.3),
    ("energy_boe", "Production, barrels of oil equivalent", "barrels of oil equivalent",
     "13,29", "count", 0.0, 1e9, 9.9),
    ("mining_aisc", "All-in sustaining cost", "all-in sustaining cost", "10,12,14",
     "dollar", 0.0, 20000.0, 7.7),
    ("utility_mwh", "Megawatt hours", "megawatt hour", "49",
     "count", 0.0, 1e10, 5.4),
]

DICT_TABLE = """
CREATE OR REPLACE TABLE ref.kpi_dictionary (
    kpi VARCHAR, label VARCHAR, phrase VARCHAR, sic2_groups VARCHAR,
    expected_unit VARCHAR, min_value DOUBLE, max_value DOUBLE,
    measured_industry_coverage_pct DOUBLE)
"""


def lines_table(phrases: list[str]) -> str:
    """One pass over the narrative, keeping only lines that mention any KPI and a digit.

    This is the whole cost of the build. Everything downstream reads the few thousand rows
    it produces rather than the 1.79m-section view.
    """
    alt = "|".join(p.replace(" ", r"\s+") for p in phrases)
    return f"""
CREATE OR REPLACE TABLE staging.kpi_lines AS
WITH ind AS (
    SELECT DISTINCT cik, sic2 FROM marts.ratio_values WHERE fy >= 2019
),
src AS (
    SELECT s.cik, i.sic2, s.adsh, s.item AS section,
           TRY_CAST(substr(s.period_of_report, 1, 4) AS INTEGER) AS fy, s.text
    FROM quali.filing_sections s
    JOIN ind i ON i.cik = s.cik
    WHERE s.item IN ('1', '7') AND substr(s.filing_date, 1, 4) >= '2019'
      -- Discard whole sections before exploding them. Unnesting first turns 1.79m
      -- sections into hundreds of millions of lines and then throws away all but a few
      -- thousand; MotherDuck failed to commit the intermediate outright. Testing the
      -- section for the same alternation costs one regex per section and removes the
      -- explosion, because only a few per cent of sections mention any KPI at all.
      AND regexp_matches(lower(s.text), '{alt}')
),
exploded AS (
    SELECT cik, sic2, adsh, section, fy,
           trim(unnest(str_split(text, chr(10)))) AS line
    FROM src
)
SELECT cik, sic2, adsh, section, fy, line
FROM exploded
WHERE fy IS NOT NULL
  AND length(line) BETWEEN 20 AND 600
  AND regexp_matches(line, '[0-9]')
  AND regexp_matches(lower(line), '{alt}')
"""


def extract_for(kpi: str, phrase: str, sics: str, unit: str,
                lo: float, hi: float) -> str:
    """One KPI, scoped to its own industries.

    The value taken is the first number within 60 characters after the phrase, which is
    where a prose disclosure puts it: "occupancy rate to 98.2% in Fiscal 2022". The unit is
    read from the same window rather than assumed, and disagreement with the dictionary's
    expectation lowers the confidence instead of discarding the row - a reader can then
    decide, which they cannot do with a row that was silently dropped.
    """
    sic_list = ", ".join(f"'{s}'" for s in sics.split(","))
    # One backslash, not two. `lines_table` uses r"\s+" and this used r"\\s+", and DuckDB
    # does not process backslash escapes in string literals - so the regex engine saw a
    # literal backslash followed by "s" and every multi-word phrase matched nothing. The
    # tell was unmissable once it ran: exactly three KPIs produced rows, and they were
    # exactly the three single-word phrases in the dictionary.
    pat = phrase.replace(" ", r"\s+")

    # The unit is demanded of the text rather than inferred afterwards, and the window is
    # 25 characters rather than 60.
    #
    # Taking the first number within 60 characters produced values that passed every range
    # check and were still wrong: a load factor of 2.00 read out of "Passenger revenue
    # increased $1.3 billion, or 3.3%", and a RevPAR of 99 read out of "higher RevPAR, unit
    # growth ($99 million)". Both sat inside their declared bounds, because 2% is a
    # possible load factor and $99 is a possible RevPAR - which is this warehouse's
    # standing failure: a wrong value looks exactly like a right one.
    #
    # Requiring the percent sign for a percentage and the currency mark for a money figure
    # removes most of it, because the intervening quantities that were being caught are
    # rarely marked the same way as the metric itself.
    trailing_re = None
    if unit == "percent":
        value_re = rf"{pat}[^0-9%]{{0,25}}([0-9][0-9,]*\.?[0-9]*)\s*%"
        unit_seen = "'percent'"
    elif unit == "dollar":
        value_re = rf"{pat}[^0-9$]{{0,25}}\$\s*([0-9][0-9,]*\.?[0-9]*)"
        unit_seen = "'dollar'"
        # RevPAR survived the currency guard because the quantity stealing the match was
        # also a dollar amount: "higher RevPAR, unit growth ($99 million)". Scale is what
        # separates them - a per-unit metric is never followed by "million". RE2 has no
        # lookahead, so the trailing words are captured and filtered in SQL below.
        trailing_re = rf"{pat}[^0-9$]{{0,25}}\$\s*[0-9][0-9,]*\.?[0-9]*\s*[a-z]{{0,8}}"
    else:
        value_re = rf"{pat}[^0-9]{{0,25}}([0-9][0-9,]*\.?[0-9]*)"
        unit_seen = "'plain'"
    # Only the per-unit metrics need it; an aggregate like backlog or ARR legitimately
    # runs into millions.
    per_unit = kpi in ('hotel_revpar', 'hotel_adr', 'telecom_arpu', 'semi_asp',
                       'mining_aisc', 'restaurant_auv')
    scale_guard = (
        f"AND NOT regexp_matches(regexp_extract(lower(line), '{trailing_re}'), "
        f"'million|billion|thousand')" if per_unit and trailing_re else "")
    return f"""
SELECT cik, fy, '{kpi}' AS kpi, section, adsh,
       TRY_CAST(replace(regexp_extract(lower(line), '{value_re}', 1), ',', '')
                AS DOUBLE) AS value,
       {unit_seen} AS unit_seen,
       '{unit}' AS expected_unit,
       {lo} AS min_value, {hi} AS max_value,
       line AS source_sentence
FROM staging.kpi_lines
WHERE sic2 IN ({sic_list})
  AND regexp_matches(lower(line), '{value_re}')
  {scale_guard}
"""


BUILD_TAIL = """
CREATE OR REPLACE TABLE marts.disclosed_kpis AS
WITH raw AS (
    {branches}
),
scored AS (
    -- The unit is no longer part of confidence, because the pattern now demands it: a
    -- percentage is only read where a percent sign follows and a money figure only where
    -- a currency mark precedes. Scoring on a condition the extraction guarantees would be
    -- a check that cannot fail, which is worse than no check.
    SELECT *,
           value BETWEEN min_value AND max_value AS in_range
    FROM raw WHERE value IS NOT NULL
),
ranked AS (
    -- One value per company, year and KPI. A company mentions a metric several times in a
    -- filing, so without this the mart would carry one row per mention and any average
    -- over it would be weighted by how talkative the filer is.
    SELECT *, row_number() OVER (
        PARTITION BY cik, fy, kpi
        ORDER BY in_range DESC, length(source_sentence) DESC) AS rn
    FROM scored
)
SELECT cik, fy, kpi, value, unit_seen AS unit, expected_unit,
       section AS source_section,
       substr(source_sentence, 1, 400) AS source_sentence,
       adsh,
       CASE WHEN in_range THEN 'high' ELSE 'low' END AS confidence
FROM ranked WHERE rn = 1
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-lines", action="store_true",
                    help="rebuild the candidate-line table even if the phrases are unchanged")
    args = ap.parse_args()
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    con.execute("SET temp_directory = '/tmp/duckdb_spill'")
    con.execute("SET preserve_insertion_order = false")
    con.execute("CREATE SCHEMA IF NOT EXISTS ref")
    con.execute("CREATE SCHEMA IF NOT EXISTS marts")

    con.execute(DICT_TABLE)
    con.executemany(
        "INSERT INTO ref.kpi_dictionary VALUES (?, ?, ?, ?, ?, ?, ?, ?)", DICTIONARY)
    print(f"table ref.kpi_dictionary  {len(DICTIONARY)} KPIs across "
          f"{len({d[3] for d in DICTIONARY})} industry groups")

    # The narrative scan is the whole cost of this build and it does not depend on any of
    # the extraction logic below, which has now been revised four times. So it is a
    # permanent table, rebuilt only when the phrase list it was derived from changes.
    #
    # The stored fingerprint is what makes that safe. Adding a KPI to the dictionary
    # without refreshing the lines would leave the new entry silently producing nothing -
    # a table that looks built and is quietly incomplete, which is the failure this
    # warehouse keeps meeting.
    phrases = [d[2] for d in DICTIONARY]
    fingerprint = hashlib.sha256("|".join(sorted(phrases)).encode()).hexdigest()[:16]
    con.execute("CREATE SCHEMA IF NOT EXISTS staging")
    con.execute("""CREATE TABLE IF NOT EXISTS staging.kpi_lines_build
                   (fingerprint VARCHAR, built_at TIMESTAMP, lines BIGINT)""")
    have = con.execute(
        "SELECT fingerprint FROM staging.kpi_lines_build ORDER BY built_at DESC LIMIT 1"
    ).fetchone()
    exists = con.execute("""SELECT count(*) FROM information_schema.tables
                            WHERE table_schema = 'staging'
                              AND table_name = 'kpi_lines'""").fetchone()[0]

    if args.refresh_lines or not exists or not have or have[0] != fingerprint:
        why = ("asked for" if args.refresh_lines else
               "absent" if not exists else "phrase list changed")
        print(f"building staging.kpi_lines ({why}) - one pass over the narrative")
        con.execute(lines_table(phrases))
        n = con.execute("SELECT count(*) FROM staging.kpi_lines").fetchone()[0]
        con.execute("INSERT INTO staging.kpi_lines_build VALUES (?, now(), ?)",
                    [fingerprint, n])
    else:
        n = con.execute("SELECT count(*) FROM staging.kpi_lines").fetchone()[0]
        print("reusing staging.kpi_lines - phrase list unchanged")
    print(f"table staging.kpi_lines  {n:,} candidate lines")

    branches = "\n    UNION ALL\n".join(
        extract_for(d[0], d[2], d[3], d[4], d[5], d[6]) for d in DICTIONARY)
    con.execute(BUILD_TAIL.format(branches=branches))

    rows, cos, kpis = con.execute("""
        SELECT count(*), count(DISTINCT cik), count(DISTINCT kpi)
        FROM marts.disclosed_kpis""").fetchone()
    print(f"table marts.disclosed_kpis  {rows:,} rows, {cos:,} companies, {kpis} KPIs")

    print("\nPer KPI: how many companies, and how much is trustworthy?")
    cur = con.execute("""
        SELECT k.kpi, count(DISTINCT k.cik) AS companies, count(*) AS company_years,
               count(*) FILTER (WHERE confidence = 'high') AS high,
               round(100.0 * count(*) FILTER (WHERE confidence = 'high')
                     / count(*), 1) AS pct_high,
               round(median(value), 2) AS median_value
        FROM marts.disclosed_kpis k
        GROUP BY 1 ORDER BY companies DESC""")
    print(f"  {'kpi':<22} {'cos':>6} {'rows':>7} {'high':>7} {'pct':>7} {'median':>14}")
    for r in cur.fetchall():
        print(f"  {r[0]:<22} {r[1]:>6,} {r[2]:>7,} {r[3]:>7,} {r[4]:>6.1f}% "
              f"{r[5]:>14,.2f}")

    print("\nA few high-confidence values with the sentence they came from:")
    cur = con.execute("""
        SELECT kpi, cik, fy, value, unit, substr(source_sentence, 1, 105)
        FROM marts.disclosed_kpis
        WHERE confidence = 'high'
          AND kpi IN ('airline_load_factor', 'reit_occupancy', 'hotel_revpar',
                      'retail_comp_sales', 'mining_aisc')
        ORDER BY kpi LIMIT 12""")
    for r in cur.fetchall():
        print(f"  {r[0]:<20} {r[2]} {r[3]:>12,.2f} {r[4]:<8} {r[5]}")


if __name__ == "__main__":
    main()
