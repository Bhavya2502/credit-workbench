"""G-02 and G-03 — coverage, distribution and outcome counts per cohort.

The scorecard-design workstream asked for two tables and named them the most valuable
things that could be built: one saying which ratios compute for what share of a cohort and
how they are distributed, and one saying how many credit events fall in that cohort. Their
reasoning is that a user must not weight a factor at 20% when the ratio computes for 30% of
the relevant companies, and must not calibrate a grade scale on five events.

**Why this is not built at the grain they asked for, alone.** They asked for
`ratio x sic2 x size_band x fy`. Measured, FY2024 at that grain gives 12,127 cells of which
1,409 hold thirty companies or more and 7,663 hold fewer than ten - a median cell of six.
A p10 and a p90 over six companies are not a distribution, and a table that publishes them
as though they were would defeat the exact purpose it was requested for: it would hand a
user a precise-looking threshold drawn from noise.

So every cohort is emitted under two industry schemes, in one table, distinguished by
`industry_scheme`:

  `sic2`        the 73 two-digit major groups, as asked for, for compatibility
  `peer_group`  the 140 groups in `ref.industry_group`, which exist precisely because
                four-digit SIC is too thin and were rolled up only as far as needed to
                reach thirty comparable companies

and `companies_with_value` and `is_sufficient` travel with every row, so a thin cell can be
refused rather than trusted. Nothing is hidden and nothing is silently substituted; the
consumer chooses, which is what the design tool wants to expose to its own users anyway.

`size_band = 'ALL'` rows carry the same cohort without the size cut, because an industry
distribution over all sizes is often the only one with enough companies to mean anything.

Both tables are materialised rather than left as views. G-24 in the same document is a
complaint that free-plan compute is exhausted by exploratory scans, and a pre-aggregated
table is the answer to it - this is the one place where computing once and storing is
strictly better than letting every consumer re-derive.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

SUFFICIENT = 30      # the threshold ref.industry_group itself was built to reach

# One row per (company, cohort keying, ratio). Built once as a temp table because both the
# numerator and the denominator read it, and the peer-group join is the expensive part.
TAGGED = f"""
CREATE OR REPLACE TEMP TABLE tagged AS
WITH base AS (
    SELECT r.cik, r.fy, r.basis, r.ratio, r.value,
           coalesce(nullif(r.size_band, ''), 'Z unknown') AS size_band,
           r.sic2,
           g.industry_code, g.industry_label
    FROM marts.ratio_values r
    LEFT JOIN ref.industry_group g ON g.sic4 = r.sic
)
SELECT 'sic2' AS industry_scheme, sic2 AS industry_code,
       sic2 AS industry_label, cik, fy, basis, size_band, ratio, value
FROM base WHERE sic2 IS NOT NULL AND sic2 <> ''
UNION ALL
SELECT 'peer_group', industry_code, industry_label,
       cik, fy, basis, size_band, ratio, value
FROM base WHERE industry_code IS NOT NULL AND industry_code <> ''
"""

# companies_total is every company in the cohort that has any ratio at all, which is the
# denominator the request implies: "of the companies here, how many have this one".
DENOM = """
CREATE OR REPLACE TEMP TABLE denom AS
SELECT industry_scheme, industry_code, fy, basis, size_band,
       count(DISTINCT cik) AS companies_total
FROM tagged GROUP BY 1, 2, 3, 4, 5
UNION ALL
SELECT industry_scheme, industry_code, fy, basis, 'ALL' AS size_band,
       count(DISTINCT cik) AS companies_total
FROM tagged GROUP BY 1, 2, 3, 4, 5
"""

# All five percentiles come from one `quantile_cont` call taking a list, not five calls.
# Five separate aggregates sort the same group five times; over 13.2m tagged rows that was
# enough to spill to disk, and spilling is what broke the first run.
NUMER = """
CREATE OR REPLACE TEMP TABLE numer AS
SELECT industry_scheme, industry_code, any_value(industry_label) AS industry_label,
       fy, basis, size_band, ratio,
       count(DISTINCT cik) AS companies_with_value,
       quantile_cont(value, [0.10, 0.25, 0.50, 0.75, 0.90]) AS q,
       min(value) AS min_value, max(value) AS max_value
FROM tagged WHERE value IS NOT NULL AND isfinite(value)
GROUP BY 1, 2, 4, 5, 6, 7
UNION ALL
SELECT industry_scheme, industry_code, any_value(industry_label),
       fy, basis, 'ALL' AS size_band, ratio,
       count(DISTINCT cik),
       quantile_cont(value, [0.10, 0.25, 0.50, 0.75, 0.90]),
       min(value), max(value)
FROM tagged WHERE value IS NOT NULL AND isfinite(value)
GROUP BY 1, 2, 4, 5, 6, 7
"""

COVERAGE = f"""
CREATE OR REPLACE TABLE marts.ratio_coverage AS
SELECT n.industry_scheme, n.industry_code, n.industry_label, n.size_band, n.fy, n.basis,
       n.ratio,
       d.companies_total,
       n.companies_with_value,
       round(100.0 * n.companies_with_value / nullif(d.companies_total, 0), 1)
           AS coverage_pct,
       n.companies_with_value >= {SUFFICIENT} AS is_sufficient,
       n.q[1] AS p10, n.q[2] AS p25, n.q[3] AS p50, n.q[4] AS p75, n.q[5] AS p90,
       n.min_value, n.max_value
FROM numer n
JOIN denom d
  ON d.industry_scheme = n.industry_scheme AND d.industry_code = n.industry_code
 AND d.fy = n.fy AND d.basis = n.basis AND d.size_band = n.size_band
"""

# G-03. credit_outcomes carries sic2 but no size_band, so the band is joined from
# ratio_values - aggregated to one row per (cik, fy) FIRST. Joining it raw would fan out
# once per ratio and per basis, inflating every count about ninety-fold. Three separate
# tables in this project have been damaged by exactly that, so it is done deliberately.
OUTCOMES = f"""
CREATE OR REPLACE TABLE marts.outcome_counts AS
WITH band AS (
    SELECT cik, fy, any_value(size_band) AS size_band
    FROM marts.ratio_values
    GROUP BY cik, fy
),
o AS (
    SELECT co.cik, co.fy, co.sic2,
           coalesce(nullif(b.size_band, ''), 'Z unknown') AS size_band,
           g.industry_code, g.industry_label,
           co.distress_12m, co.distress_24m, co.default_12m, co.default_24m,
           co.bankruptcy_24m, co.debt_acceleration_24m, co.non_reliance_24m,
           co.adverse_delisting_24m
    FROM marts.credit_outcomes co
    LEFT JOIN band b ON b.cik = co.cik AND b.fy = co.fy
    LEFT JOIN ref.industry_group g ON g.sic4 = co.sic
),
tagged_o AS (
    SELECT 'sic2' AS industry_scheme, sic2 AS industry_code, sic2 AS industry_label,
           cik, fy, size_band, distress_12m, distress_24m, default_12m, default_24m,
           bankruptcy_24m, debt_acceleration_24m, non_reliance_24m, adverse_delisting_24m
    FROM o WHERE sic2 IS NOT NULL AND sic2 <> ''
    UNION ALL
    SELECT 'peer_group', industry_code, industry_label,
           cik, fy, size_band, distress_12m, distress_24m, default_12m, default_24m,
           bankruptcy_24m, debt_acceleration_24m, non_reliance_24m, adverse_delisting_24m
    FROM o WHERE industry_code IS NOT NULL AND industry_code <> ''
),
counted AS (
    SELECT industry_scheme, industry_code, any_value(industry_label) AS industry_label,
           size_band, fy,
           count(*) AS company_years, count(DISTINCT cik) AS companies,
           count(*) FILTER (WHERE distress_12m) AS distress_12m,
           count(*) FILTER (WHERE distress_24m) AS distress_24m,
           count(*) FILTER (WHERE default_12m) AS default_12m,
           count(*) FILTER (WHERE default_24m) AS default_24m,
           count(*) FILTER (WHERE bankruptcy_24m) AS bankruptcy_24m,
           count(*) FILTER (WHERE debt_acceleration_24m) AS debt_acceleration_24m,
           count(*) FILTER (WHERE non_reliance_24m) AS non_reliance_24m,
           count(*) FILTER (WHERE adverse_delisting_24m) AS adverse_delisting_24m
    FROM tagged_o GROUP BY 1, 2, 4, 5
    UNION ALL
    SELECT industry_scheme, industry_code, any_value(industry_label), 'ALL', fy,
           count(*), count(DISTINCT cik),
           count(*) FILTER (WHERE distress_12m), count(*) FILTER (WHERE distress_24m),
           count(*) FILTER (WHERE default_12m), count(*) FILTER (WHERE default_24m),
           count(*) FILTER (WHERE bankruptcy_24m),
           count(*) FILTER (WHERE debt_acceleration_24m),
           count(*) FILTER (WHERE non_reliance_24m),
           count(*) FILTER (WHERE adverse_delisting_24m)
    FROM tagged_o GROUP BY 1, 2, 4, 5
    UNION ALL
    -- fy = NULL is the whole window pooled, which is the only cell with enough events to
    -- calibrate a scale for most cohorts. The request asks for exactly this as a
    -- "cohort-level total across the window".
    SELECT industry_scheme, industry_code, any_value(industry_label), 'ALL', NULL,
           count(*), count(DISTINCT cik),
           count(*) FILTER (WHERE distress_12m), count(*) FILTER (WHERE distress_24m),
           count(*) FILTER (WHERE default_12m), count(*) FILTER (WHERE default_24m),
           count(*) FILTER (WHERE bankruptcy_24m),
           count(*) FILTER (WHERE debt_acceleration_24m),
           count(*) FILTER (WHERE non_reliance_24m),
           count(*) FILTER (WHERE adverse_delisting_24m)
    FROM tagged_o GROUP BY 1, 2, 4
)
SELECT *,
       round(100.0 * distress_24m / nullif(company_years, 0), 2) AS distress_24m_rate,
       round(100.0 * default_24m / nullif(company_years, 0), 2) AS default_24m_rate,
       -- A scale cannot be cut on a handful of events. The threshold is stated in the
       -- data rather than left to each consumer to invent.
       default_24m >= {SUFFICIENT} AS can_calibrate_default,
       distress_24m >= {SUFFICIENT} AS can_calibrate_distress
FROM counted
"""


def guard_join_key(con) -> None:
    """`ref.industry_group.sic4` must be unique, or every count here is inflated.

    Both tables reach the peer groups by joining on it, and a non-unique key would
    multiply 6.66m ratio rows silently while every individual row still looked correct.
    That failure has damaged three tables in this project already - on (tag, version), on
    multi-vintage facts, and on exhibit number - so it is asserted before the build runs
    rather than discovered in the output.
    """
    rows, distinct = con.execute("""
        SELECT count(*), count(DISTINCT sic4) FROM ref.industry_group""").fetchone()
    print(f"guard ref.industry_group  {rows:,} rows, {distinct:,} distinct sic4")
    if rows != distinct:
        raise SystemExit(
            f"ref.industry_group.sic4 is not unique ({rows:,} rows, {distinct:,} keys): "
            "joining on it would fan out every cohort count. Fix the bridge first.")


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    # A MotherDuck connection has no usable default spill location: when the first run
    # needed to spill it tried to create a directory named after the whole connection
    # string, token and all, and failed with "File name too long". Aggregating 13.2m rows
    # will spill, so point it somewhere real before starting.
    con.execute("SET temp_directory = '/tmp/duckdb_spill'")
    con.execute("SET preserve_insertion_order = false")
    con.execute("CREATE SCHEMA IF NOT EXISTS marts")
    guard_join_key(con)

    for label, sql in (("tagged", TAGGED), ("denom", DENOM), ("numer", NUMER)):
        con.execute(sql)
        n = con.execute(f"SELECT count(*) FROM {label}").fetchone()[0]
        print(f"temp  {label:<8} {n:,} rows")

    con.execute(COVERAGE)
    rows, cells_ok = con.execute("""
        SELECT count(*), count(*) FILTER (WHERE is_sufficient)
        FROM marts.ratio_coverage""").fetchone()
    print(f"table marts.ratio_coverage  {rows:,} rows, "
          f"{cells_ok:,} with {SUFFICIENT}+ companies "
          f"({100 * cells_ok / max(rows, 1):.0f}%)")

    con.execute(OUTCOMES)
    rows, cal = con.execute("""
        SELECT count(*), count(*) FILTER (WHERE can_calibrate_default)
        FROM marts.outcome_counts""").fetchone()
    print(f"table marts.outcome_counts  {rows:,} rows, "
          f"{cal:,} cohorts with {SUFFICIENT}+ defaults")

    # What the two industry schemes cost and buy, side by side. This is the number the
    # requesting workstream needs in order to choose, so it is printed rather than left
    # for them to derive.
    print("\nSufficient cells by industry scheme (fy 2024, first_reported):")
    cur = con.execute(f"""
        SELECT industry_scheme, size_band, count(*) AS cells,
               count(*) FILTER (WHERE is_sufficient) AS sufficient,
               round(100.0 * count(*) FILTER (WHERE is_sufficient) / count(*), 1) AS pct,
               round(median(companies_with_value), 0) AS median_companies
        FROM marts.ratio_coverage
        WHERE fy = 2024 AND basis = 'first_reported'
        GROUP BY 1, 2 ORDER BY 1, 2""")
    heads = [d[0] for d in cur.description]
    print("  " + "  ".join(f"{h:<18}" for h in heads))
    for r in cur.fetchall():
        print("  " + "  ".join(f"{('' if v is None else v)!s:<18}" for v in r))


if __name__ == "__main__":
    main()
