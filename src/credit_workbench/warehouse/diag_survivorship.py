"""G-19 — can the warehouse actually catch the failure that was described?

The request asked for confirmation that delisted and renamed entities survive in the
company master, plus a `former_names` or `status` field if they do not. Two of the three
parts are already answered and were simply invisible: 780 companies carry a bankruptcy
outcome and all 780 are present in `ref.dim_company`, and `ref.former_names` has held
name-change history with effective dates since B1 - it is in the generated data dictionary
but nowhere in `DATA_GUIDE.md`, which is what anyone actually reads.

So the question worth asking is not whether the columns exist but whether they catch the
specific failure reported: a ticker resolved to the company that later acquired a failed
retailer's *brand*, substituting a survivor's financials against a bankruptcy outcome. That
is a name-collision problem, and it is caught only if the same name appears as a former name
of one CIK and the current name of another - or if one CIK's name changed around the date
the outcome was recorded.

The third part, `status`, has no column. Whether that matters depends on whether "last
filing date" already separates a company that stopped filing from one that did not, so that
is measured too rather than assumed.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

Q = [
    ("1. Is ref.former_names populated, and over what period?", """
        SELECT count(*) AS rows, count(DISTINCT cik) AS companies,
               count(name_from) AS with_from_date, count(name_to) AS with_to_date,
               min(substr(name_from, 1, 4)) AS earliest,
               max(substr(name_from, 1, 4)) AS latest
        FROM ref.former_names"""),

    # The reported failure in its exact shape: a name that is one company's former name and
    # another company's current name. If these exist, the collision is detectable.
    ("2. Names that belong to one CIK now and another CIK before — the reported trap", """
        SELECT count(*) AS colliding_names
        FROM (SELECT DISTINCT lower(trim(f.former_name)) AS nm, f.cik AS former_cik,
                     c.cik AS current_cik
              FROM ref.former_names f
              JOIN ref.dim_company c
                ON lower(trim(c.company_name)) = lower(trim(f.former_name))
              WHERE c.cik <> TRY_CAST(f.cik AS BIGINT))"""),

    ("3. A few of them, to see whether they are real collisions", """
        SELECT f.former_name, f.cik AS held_by_cik, f.name_to AS renamed_on,
               c.cik AS now_held_by_cik, c.company_name AS current_name_of_that_cik
        FROM ref.former_names f
        JOIN ref.dim_company c
          ON lower(trim(c.company_name)) = lower(trim(f.former_name))
        WHERE c.cik <> TRY_CAST(f.cik AS BIGINT)
        LIMIT 8"""),

    # Do the companies with credit events actually have name history? If a bankrupt filer
    # renamed itself, matching on current name alone would miss its own earlier filings.
    ("4. Do companies with a bankruptcy outcome have former names?", """
        SELECT count(DISTINCT o.cik) AS bankrupt_ciks,
               count(DISTINCT f.cik) AS with_former_names
        FROM marts.credit_outcomes o
        LEFT JOIN ref.former_names f
          ON TRY_CAST(f.cik AS BIGINT) = o.cik
        WHERE o.bankruptcy_24m"""),

    # G-19's third part. No status column exists; does last-filing-date substitute?
    ("5. Does a bankrupt company stop filing? (last filing vs a live company)", """
        WITH last_filing AS (
            SELECT TRY_CAST(cik AS BIGINT) AS cik,
                   max(substr(filing_date, 1, 4)) AS last_year
            FROM ref.filing_index GROUP BY 1
        ),
        flagged AS (
            SELECT DISTINCT cik, bool_or(bankruptcy_24m) AS went_bankrupt
            FROM marts.credit_outcomes GROUP BY cik
        )
        SELECT f.went_bankrupt,
               count(*) AS companies,
               count(*) FILTER (WHERE l.last_year < '2024') AS stopped_before_2024,
               round(100.0 * count(*) FILTER (WHERE l.last_year < '2024')
                     / count(*), 1) AS pct_stopped
        FROM flagged f JOIN last_filing l ON l.cik = f.cik
        GROUP BY 1 ORDER BY 1"""),

    ("6. How many companies in the master have stopped filing altogether?", """
        WITH last_filing AS (
            SELECT TRY_CAST(cik AS BIGINT) AS cik,
                   max(substr(filing_date, 1, 4)) AS last_year
            FROM ref.filing_index GROUP BY 1
        )
        SELECT last_year, count(*) AS companies
        FROM last_filing WHERE last_year >= '2015'
        GROUP BY 1 ORDER BY 1"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)))[:44]
             for v in r] for r in cur.fetchall()]
    if not rows:
        print("  (no rows)")
        return
    w = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(heads)]
    print("  " + "  ".join(h.ljust(x) for h, x in zip(heads, w)))
    print("  " + "  ".join("-" * x for x in w))
    for r in rows:
        print("  " + "  ".join(v.ljust(x) for v, x in zip(r, w)))


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    for title, q in Q:
        print(f"\n### {title}")
        try:
            show(con, q)
        except Exception as exc:
            print(f"  (failed: {str(exc)[:170]})")


if __name__ == "__main__":
    main()
