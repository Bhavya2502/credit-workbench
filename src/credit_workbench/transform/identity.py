"""G-19 — resolving a company name to a CIK without picking up a survivor.

The reported failure is worth stating exactly, because it is not a coverage problem and no
amount of extra data fixes it: a ticker resolved to the company that later acquired a failed
retailer's *brand*, so a survivor's financials were scored against a bankruptcy outcome. The
cohort looked complete and every row in it was real.

Two of the three things asked for turned out to exist already. Delisted issuers are retained
- 780 companies carry a bankruptcy outcome and all 780 are in `ref.dim_company` - and
`ref.former_names` has held name-change history with effective dates since B1, covering 531
of those 780. What was missing is the thing that makes them useful together.

**Measured, 1,237 names are one company's former name and another company's current name.**
That is the trap, counted. `ref.company_names` puts every name a CIK has ever filed under
into one place with its validity window, and flags the ones that resolve ambiguously, so a
name lookup can refuse rather than guess. A cohort builder that joins on name without
checking `is_ambiguous` will hit one of those 1,237 eventually.

**On `status`, which does not exist and is not being invented.** There is no delisting flag,
and last-filing-date is a weaker substitute than it looks: 63.7% of companies with a
bankruptcy outcome stopped filing before 2024, but so did 43.6% of those without one -
acquisitions, going private and deregistration all look the same from here. So
`last_filing_year` is published as what it is, a date, and no `is_active` is derived from it.
Naming it `status` would imply a judgement the data cannot support.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

# Every name a CIK has filed under, current and former, with its window and whether the
# name resolves to more than one company.
COMPANY_NAMES = """
CREATE OR REPLACE TABLE ref.company_names AS
WITH all_names AS (
    SELECT cik, company_name AS name, 'current' AS name_role,
           CAST(NULL AS TIMESTAMP) AS valid_from, CAST(NULL AS TIMESTAMP) AS valid_to
    FROM ref.dim_company
    WHERE company_name IS NOT NULL AND trim(company_name) <> ''
    UNION ALL
    SELECT cik, former_name AS name, 'former' AS name_role, valid_from, valid_to
    FROM ref.former_names
    WHERE former_name IS NOT NULL AND trim(former_name) <> ''
),
keyed AS (
    -- Normalised only for matching. The name as filed is kept, because a lookup that
    -- silently rewrote the name would be as hard to audit as one that guessed the CIK.
    SELECT *, lower(regexp_replace(trim(name), '[^a-zA-Z0-9 ]', '', 'g')) AS name_key
    FROM all_names
)
SELECT k.cik, k.name, k.name_key, k.name_role, k.valid_from, k.valid_to,
       a.ciks_with_this_name,
       a.ciks_with_this_name > 1 AS is_ambiguous
FROM keyed k
JOIN (SELECT name_key, count(DISTINCT cik) AS ciks_with_this_name
      FROM keyed GROUP BY name_key) a USING (name_key)
"""

# The subset that reproduces the reported failure: a name that one company has moved on
# from and another company now files under.
COLLISIONS = """
CREATE OR REPLACE TABLE ref.name_collisions AS
SELECT f.name_key,
       f.name AS name_as_filed,
       f.cik AS former_holder_cik,
       f.valid_to AS former_holder_stopped_using,
       c.cik AS current_holder_cik,
       d.company_name AS current_holder_name
FROM ref.company_names f
JOIN ref.company_names c
  ON c.name_key = f.name_key AND c.name_role = 'current' AND c.cik <> f.cik
JOIN ref.dim_company d ON d.cik = c.cik
WHERE f.name_role = 'former'
"""

# Last filing date per company, published as a date and nothing more. See the module
# docstring for why no is_active flag is derived from it.
LAST_FILING = """
CREATE OR REPLACE TABLE ref.company_filing_span AS
SELECT TRY_CAST(cik AS BIGINT) AS cik,
       min(TRY_CAST(filing_date AS DATE)) AS first_filing,
       max(TRY_CAST(filing_date AS DATE)) AS last_filing,
       CAST(max(substr(filing_date, 1, 4)) AS INTEGER) AS last_filing_year,
       count(*) AS filings
FROM ref.filing_index
WHERE cik IS NOT NULL
GROUP BY 1
"""


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    con.execute("SET temp_directory = '/tmp/duckdb_spill'")
    con.execute("CREATE SCHEMA IF NOT EXISTS ref")

    con.execute(COMPANY_NAMES)
    rows, ciks, amb = con.execute("""
        SELECT count(*), count(DISTINCT cik), count(*) FILTER (WHERE is_ambiguous)
        FROM ref.company_names""").fetchone()
    print(f"table ref.company_names  {rows:,} names, {ciks:,} companies, "
          f"{amb:,} ambiguous")

    con.execute(COLLISIONS)
    n = con.execute("SELECT count(*) FROM ref.name_collisions").fetchone()[0]
    print(f"table ref.name_collisions  {n:,} rows")

    con.execute(LAST_FILING)
    n = con.execute("SELECT count(*) FROM ref.company_filing_span").fetchone()[0]
    print(f"table ref.company_filing_span  {n:,} companies")

    print("\nCollisions involving a company that went bankrupt — the reported failure:")
    cur = con.execute("""
        SELECT nc.name_as_filed, nc.former_holder_cik, nc.current_holder_cik,
               nc.current_holder_name
        FROM ref.name_collisions nc
        WHERE nc.former_holder_cik IN (SELECT DISTINCT cik FROM marts.credit_outcomes
                                       WHERE bankruptcy_24m)
           OR nc.current_holder_cik IN (SELECT DISTINCT cik FROM marts.credit_outcomes
                                        WHERE bankruptcy_24m)
        LIMIT 10""")
    for r in cur.fetchall():
        print("  " + " | ".join(str(v)[:34] for v in r))

    print("\nHow many bankrupt companies are exposed to a name collision?")
    cur = con.execute("""
        SELECT count(DISTINCT o.cik) AS bankrupt_ciks,
               count(DISTINCT nc.former_holder_cik) AS also_in_a_collision
        FROM marts.credit_outcomes o
        LEFT JOIN ref.name_collisions nc ON nc.former_holder_cik = o.cik
        WHERE o.bankruptcy_24m""")
    for r in cur.fetchall():
        print("  bankrupt_ciks=%s  also_in_a_collision=%s" % r)


if __name__ == "__main__":
    main()
