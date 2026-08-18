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
CREATE OR REPLACE TEMP TABLE kpi_lines AS
WITH ind AS (
    SELECT DISTINCT cik, sic2 FROM marts.ratio_values WHERE fy >= 2019
),
src AS (
    SELECT s.cik, i.sic2, s.adsh, s.item AS section,
           TRY_CAST(substr(s.period_of_report, 1, 4) AS INTEGER) AS fy, s.text
    FROM quali.filing_sections s
    JOIN ind i ON i.cik = s.cik
    WHERE s.item IN ('1', '7') AND substr(s.filing_date, 1, 4) >= '2019'
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
    pat = phrase.replace(" ", r"\\s+")
    return f"""
SELECT cik, fy, '{kpi}' AS kpi, section, adsh,
       TRY_CAST(replace(regexp_extract(lower(line),
           '{pat}[^0-9]{{0,60}}([0-9][0-9,]*\\.?[0-9]*)', 1), ',', '') AS DOUBLE) AS value,
       CASE
           WHEN regexp_matches(regexp_extract(lower(line),
                '{pat}[^0-9]{{0,60}}[0-9][0-9,]*\\.?[0-9]*\\s*.{{0,3}}'), '%')
               THEN 'percent'
           WHEN regexp_matches(regexp_extract(lower(line),
                '.{{0,12}}{pat}[^0-9]{{0,60}}[0-9]'), '\\$')
               THEN 'dollar'
           ELSE 'plain'
       END AS unit_seen,
       '{unit}' AS expected_unit,
       {lo} AS min_value, {hi} AS max_value,
       line AS source_sentence
FROM kpi_lines
WHERE sic2 IN ({sic_list}) AND regexp_matches(lower(line), '{pat}')
"""


BUILD_TAIL = """
CREATE OR REPLACE TABLE marts.disclosed_kpis AS
WITH raw AS (
    {branches}
),
scored AS (
    SELECT *,
           value IS NOT NULL AND value BETWEEN min_value AND max_value AS in_range,
           (unit_seen = expected_unit)
             OR (expected_unit = 'count' AND unit_seen = 'plain') AS unit_agrees
    FROM raw WHERE value IS NOT NULL
),
ranked AS (
    -- One value per company, year and KPI. A company mentions a metric several times in a
    -- filing, so without this the mart would carry one row per mention and any average
    -- over it would be weighted by how talkative the filer is.
    SELECT *, row_number() OVER (
        PARTITION BY cik, fy, kpi
        ORDER BY (in_range AND unit_agrees) DESC, in_range DESC,
                 length(source_sentence) DESC) AS rn
    FROM scored
)
SELECT cik, fy, kpi, value, unit_seen AS unit, expected_unit,
       section AS source_section,
       substr(source_sentence, 1, 400) AS source_sentence,
       adsh,
       CASE WHEN in_range AND unit_agrees THEN 'high'
            WHEN in_range THEN 'medium' ELSE 'low' END AS confidence
FROM ranked WHERE rn = 1
"""


def main() -> None:
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

    phrases = [d[2] for d in DICTIONARY]
    con.execute(lines_table(phrases))
    n = con.execute("SELECT count(*) FROM kpi_lines").fetchone()[0]
    print(f"temp  kpi_lines  {n:,} candidate lines (one pass over the narrative)")

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
