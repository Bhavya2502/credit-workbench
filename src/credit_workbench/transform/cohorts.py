"""G-18 — named, reusable cohort definitions with membership recorded.

The request: save a cohort ("US listed retailers, revenue > $500m, FY2013-25") and reuse it
across threshold-setting, scale calibration and testing, with a stable definition and
membership recorded at a point in time.

**A cohort is two things and both are stored.** `ref.cohort_definition` holds the criteria,
so a cohort can be re-resolved as the warehouse grows. `marts.cohort_members` holds who
matched when it was last resolved, so a threshold cut last month can still be reproduced
after new filings land. Keeping only the definition makes old results unreproducible;
keeping only the membership makes the cohort unextendable. Definitions are rows, so adding
one is an INSERT - the same pattern as `ref.kpi_dictionary` and `ref.risk_theme_dictionary`.

**Delisted issuers stay in, and that is the point.** G-19 was filed because a cohort built
from a live ticker file dropped every bankrupt retailer and, worse, resolved one ticker to
the company that had acquired the failed retailer's brand - scoring a survivor's financials
against a bankruptcy. So membership here is drawn from the warehouse rather than from any
list of current constituents, `last_filing_year` travels with every member so a consumer can
see the cohort contains companies that stopped filing, and an invariant asserts it does.
A cohort with no dead companies in it cannot calibrate a default rate.

**Ambiguous names are flagged, never dropped.** 2,219 names belong to one company now and
another before, and 40 companies with a bankruptcy outcome sit in one. Dropping them would
lose exactly the observations that matter, so `name_is_ambiguous` marks them and the
consumer decides. Resolution is by CIK throughout - the flag is a warning about joining to
anything external by name, not a filter applied here.

**Calibration honesty carries through.** Each resolved cohort records its own company count,
default count and whether it clears the thirty-event threshold `marts.outcome_counts` uses,
so the design tool can refuse to cut a grade scale on a cohort that cannot support one
rather than discovering it afterwards.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

# cohort_id, name, description, industry_scheme, industry_codes, size_bands,
# fy_from, fy_to, min_revenue_usd
#
# Seeded to cover the segments the design tool starts from. NULL means "no constraint on
# this axis", which is why every field is nullable rather than defaulted.
DEFINITIONS = [
    ("us_retail_large", "US listed retailers, revenue over $500m",
     "Retail trade, large enough to have a credit story. The worked example in the gap "
     "document.", "sic2", "53,54,56,57,59", None, 2013, 2025, 5e8),
    ("us_retail_all", "US listed retailers, all sizes",
     "The same industries without the revenue floor, for comparison against the large cut.",
     "sic2", "53,54,56,57,59", None, 2013, 2025, None),
    ("reits_all", "Listed REITs and real estate",
     "SIC 65 and 67. The largest single industry block with financials.",
     "sic2", "65,67", None, 2013, 2025, None),
    ("airlines_all", "Scheduled air transportation",
     "Small industry, high KPI coverage - load factor and RASM are disclosed by most.",
     "sic2", "45", None, 2013, 2025, None),
    ("semiconductors", "Semiconductors and related devices",
     "SIC 36 and 38.", "sic2", "36,38", None, 2013, 2025, None),
    ("energy_e_and_p", "Crude petroleum, natural gas and refining",
     "SIC 13 and 29. Proved reserves disclosed by 56.5% of them.",
     "sic2", "13,29", None, 2013, 2025, None),
    ("software_services", "Business services and software",
     "SIC 73, the largest industry by company count.",
     "sic2", "73", None, 2013, 2025, None),
    ("utilities", "Electric, gas and sanitary services",
     "SIC 49.", "sic2", "49", None, 2013, 2025, None),
    ("large_cap_all", "Any industry, over $10bn revenue",
     "A cross-industry control: whatever a factor does here is not industry-specific.",
     None, None, "D over $10bn", 2013, 2025, 1e10),
    ("small_cap_all", "Any industry, under $100m revenue",
     "The other end. Coverage of most metrics is materially worse here, which is itself "
     "worth being able to demonstrate.", None, None, "A under $100m", 2013, 2025, None),
]

DEF_TABLE = """
CREATE OR REPLACE TABLE ref.cohort_definition (
    cohort_id VARCHAR, cohort_name VARCHAR, description VARCHAR,
    industry_scheme VARCHAR, industry_codes VARCHAR, size_bands VARCHAR,
    fy_from INTEGER, fy_to INTEGER, min_revenue_usd DOUBLE)
"""

# Membership is resolved per company-year, because every criterion except the industry can
# change from one year to the next: a company crosses a revenue threshold, moves size band,
# or reports for the first time.
MEMBERS = """
CREATE OR REPLACE TABLE marts.cohort_members AS
WITH base AS (
    SELECT DISTINCT r.cik, r.fy, r.sic, r.sic2, r.size_band, r.company_name
    FROM marts.ratio_values r
    WHERE r.basis = 'first_reported'
),
revenue AS (
    SELECT cik, fy, max(revenue) AS revenue
    FROM marts.adjusted_metrics
    WHERE policy = 'reported' AND basis = 'first_reported' AND revenue IS NOT NULL
    GROUP BY cik, fy
),
ambiguous AS (
    SELECT DISTINCT cik FROM ref.company_names WHERE is_ambiguous
),
enriched AS (
    SELECT b.cik, b.fy, b.sic2, b.size_band, b.company_name,
           rev.revenue,
           g.industry_code, g.industry_label,
           s.last_filing_year,
           a.cik IS NOT NULL AS name_is_ambiguous,
           o.distress_24m, o.default_24m, o.bankruptcy_24m
    FROM base b
    LEFT JOIN revenue rev ON rev.cik = b.cik AND rev.fy = b.fy
    LEFT JOIN ref.industry_group g ON g.sic4 = b.sic
    LEFT JOIN ref.company_filing_span s ON s.cik = b.cik
    LEFT JOIN ambiguous a ON a.cik = b.cik
    LEFT JOIN marts.credit_outcomes o ON o.cik = b.cik AND o.fy = b.fy
)
SELECT d.cohort_id, e.cik, e.fy, e.company_name, e.sic2, e.industry_code,
       e.industry_label, e.size_band, e.revenue, e.last_filing_year,
       e.name_is_ambiguous,
       e.distress_24m, e.default_24m, e.bankruptcy_24m,
       e.distress_24m IS NOT NULL AS has_outcome
FROM ref.cohort_definition d
JOIN enriched e
  ON (d.industry_codes IS NULL
      OR list_contains(str_split(d.industry_codes, ','), e.sic2))
 AND (d.size_bands IS NULL
      OR list_contains(str_split(d.size_bands, ','), e.size_band))
 AND (d.fy_from IS NULL OR e.fy >= d.fy_from)
 AND (d.fy_to IS NULL OR e.fy <= d.fy_to)
 AND (d.min_revenue_usd IS NULL
      OR (e.revenue IS NOT NULL AND e.revenue >= d.min_revenue_usd))
"""

# The resolved cohort, with the counts that decide whether it can carry a grade scale.
COHORTS = """
CREATE OR REPLACE TABLE marts.cohorts AS
SELECT d.cohort_id, d.cohort_name, d.description,
       d.industry_scheme, d.industry_codes, d.size_bands,
       d.fy_from, d.fy_to, d.min_revenue_usd,
       count(DISTINCT m.cik) AS companies,
       count(*) AS company_years,
       count(DISTINCT m.fy) AS years,
       count(*) FILTER (WHERE m.has_outcome) AS company_years_with_outcome,
       count(*) FILTER (WHERE m.distress_24m) AS distress_24m,
       count(*) FILTER (WHERE m.default_24m) AS default_24m,
       count(*) FILTER (WHERE m.bankruptcy_24m) AS bankruptcy_24m,
       count(DISTINCT m.cik) FILTER (WHERE m.name_is_ambiguous) AS companies_ambiguous_name,
       -- Retaining delisted issuers is what makes a default rate meaningful; this is the
       -- evidence that they are here rather than an assurance that they should be.
       count(DISTINCT m.cik) FILTER (WHERE m.last_filing_year < 2025) AS companies_stopped_filing,
       count(*) FILTER (WHERE m.default_24m) >= 30 AS can_calibrate_default,
       count(*) FILTER (WHERE m.distress_24m) >= 30 AS can_calibrate_distress
FROM ref.cohort_definition d
LEFT JOIN marts.cohort_members m ON m.cohort_id = d.cohort_id
GROUP BY ALL
"""

# now() cannot be a grouping key, so the resolution stamp is applied after the counts.
STAMP = ["ALTER TABLE marts.cohorts ADD COLUMN resolved_at TIMESTAMP",
         "UPDATE marts.cohorts SET resolved_at = now()"]


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    con.execute("SET temp_directory = '/tmp/duckdb_spill'")
    con.execute("SET preserve_insertion_order = false")
    for schema in ("ref", "marts"):
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

    con.execute(DEF_TABLE)
    con.executemany(
        "INSERT INTO ref.cohort_definition VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        DEFINITIONS)
    print(f"table ref.cohort_definition  {len(DEFINITIONS)} cohorts")

    con.execute(MEMBERS)
    n = con.execute("SELECT count(*) FROM marts.cohort_members").fetchone()[0]
    print(f"table marts.cohort_members  {n:,} rows")

    con.execute(COHORTS)
    for stmt in STAMP:
        con.execute(stmt)
    n = con.execute("SELECT count(*) FROM marts.cohorts").fetchone()[0]
    print(f"table marts.cohorts  {n} resolved cohorts")

    print("\nResolved cohorts:")
    cur = con.execute("""
        SELECT cohort_id, companies, company_years, default_24m, distress_24m,
               can_calibrate_default, companies_stopped_filing, companies_ambiguous_name
        FROM marts.cohorts ORDER BY companies DESC""")
    print(f"  {'cohort':<20} {'cos':>6} {'co-yrs':>8} {'dflt':>6} {'distr':>7} "
          f"{'calib':>7} {'dead':>6} {'ambig':>6}")
    for r in cur.fetchall():
        print(f"  {r[0]:<20} {r[1]:>6,} {r[2]:>8,} {r[3]:>6,} {r[4]:>7,} "
              f"{r[5]!s:>7} {r[6]:>6,} {r[7]:>6,}")

    print("\nThe survivorship test — do cohorts contain companies that stopped filing?")
    cur = con.execute("""
        SELECT cohort_id,
               round(100.0 * count(DISTINCT cik) FILTER (WHERE last_filing_year < 2025)
                     / nullif(count(DISTINCT cik), 0), 1) AS pct_stopped,
               count(DISTINCT cik) FILTER (WHERE bankruptcy_24m) AS bankrupt_members
        FROM marts.cohort_members GROUP BY 1 ORDER BY pct_stopped DESC""")
    for r in cur.fetchall():
        print(f"  {r[0]:<20} {r[1]:>6.1f}% stopped filing, "
              f"{r[2]:>4,} with a bankruptcy outcome")


if __name__ == "__main__":
    main()
