"""Invariants for the cohort marts (G-18).

The failure these guard against is the one that produced G-19 in the first place: a cohort
built from a list of current constituents drops every company that failed, and a default rate
computed on it is meaningless in a way nothing downstream can detect. So the check that
matters most here asserts the opposite of what a naive build would produce - that cohorts
*do* contain companies which stopped filing, and *do* contain companies with bankruptcy
outcomes.

The rest are structural: every member satisfies the definition it was matched by, the
headline counts reconcile to the membership they summarise, and ambiguous names are flagged
rather than quietly removed.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

CHECKS: list[tuple[str, str, str]] = [
    ("every defined cohort resolved to members",
     """SELECT (SELECT count(*) FROM ref.cohort_definition) AS defined,
               (SELECT count(*) FROM marts.cohorts) AS resolved,
               (SELECT count(*) FROM marts.cohorts WHERE companies = 0) AS empty""",
     "resolved == defined and empty == 0"),

    ("one row per cohort, company and year",
     """SELECT count(*) AS rows,
               count(DISTINCT (cohort_id, cik, fy)) AS distinct_keys
        FROM marts.cohort_members""",
     "rows == distinct_keys"),

    ("the headline counts reconcile to the membership they summarise",
     """SELECT count(*) AS cohorts, count(*) FILTER (WHERE c.companies <> m.companies)
                   AS company_mismatch,
               count(*) FILTER (WHERE c.company_years <> m.rows) AS row_mismatch
        FROM marts.cohorts c
        JOIN (SELECT cohort_id, count(DISTINCT cik) AS companies, count(*) AS rows
              FROM marts.cohort_members GROUP BY cohort_id) m
          ON m.cohort_id = c.cohort_id""",
     "cohorts >= 10 and company_mismatch == 0 and row_mismatch == 0"),

    # Every criterion in the definition must actually hold of every member.
    ("every member satisfies the industry its cohort declares",
     """SELECT count(*) AS rows,
               count(*) FILTER (WHERE d.industry_codes IS NOT NULL
                   AND NOT list_contains(str_split(d.industry_codes, ','), m.sic2))
                   AS wrong_industry
        FROM marts.cohort_members m
        JOIN ref.cohort_definition d ON d.cohort_id = m.cohort_id""",
     "rows > 10000 and wrong_industry == 0"),

    ("every member satisfies the year range and revenue floor",
     """SELECT count(*) AS rows,
               count(*) FILTER (WHERE m.fy < d.fy_from OR m.fy > d.fy_to)
                   AS outside_years,
               count(*) FILTER (WHERE d.min_revenue_usd IS NOT NULL
                   AND (m.revenue IS NULL OR m.revenue < d.min_revenue_usd))
                   AS below_revenue_floor,
               count(*) FILTER (WHERE d.size_bands IS NOT NULL
                   AND NOT list_contains(str_split(d.size_bands, ','), m.size_band))
                   AS wrong_size_band
        FROM marts.cohort_members m
        JOIN ref.cohort_definition d ON d.cohort_id = m.cohort_id""",
     "outside_years == 0 and below_revenue_floor == 0 and wrong_size_band == 0"),

    # The reason G-19 exists. A cohort with no dead companies in it cannot calibrate a
    # default rate, and a live-ticker build produces exactly that while looking complete.
    ("cohorts retain companies that stopped filing",
     """SELECT count(*) AS cohorts,
               count(*) FILTER (WHERE companies_stopped_filing = 0) AS survivor_only,
               round(100.0 * sum(companies_stopped_filing) / sum(companies), 1)
                   AS pct_stopped_overall
        FROM marts.cohorts""",
     "cohorts >= 10 and survivor_only == 0 and pct_stopped_overall > 10"),

    ("cohorts retain companies that went bankrupt",
     """SELECT count(DISTINCT cohort_id) AS cohorts_with_a_bankruptcy,
               count(DISTINCT cik) FILTER (WHERE bankruptcy_24m) AS bankrupt_members
        FROM marts.cohort_members""",
     "cohorts_with_a_bankruptcy >= 5 and bankrupt_members > 50"),

    # Flagged, not filtered. Dropping them would remove exactly the observations that
    # matter - 40 of the companies in a name collision have a bankruptcy outcome.
    ("ambiguous names are flagged and still present",
     """SELECT count(*) FILTER (WHERE name_is_ambiguous) AS ambiguous_rows,
               count(DISTINCT cik) FILTER (WHERE name_is_ambiguous) AS ambiguous_companies
        FROM marts.cohort_members""",
     "ambiguous_rows > 0 and ambiguous_companies > 0"),

    ("the calibration flags agree with the counts behind them",
     """SELECT count(*) AS cohorts,
               count(*) FILTER (WHERE can_calibrate_default <> (default_24m >= 30))
                   AS default_flag_wrong,
               count(*) FILTER (WHERE can_calibrate_distress <> (distress_24m >= 30))
                   AS distress_flag_wrong
        FROM marts.cohorts""",
     "default_flag_wrong == 0 and distress_flag_wrong == 0"),

    ("the worked example from the request resolves to something usable",
     """SELECT companies, company_years, default_24m, distress_24m
        FROM marts.cohorts WHERE cohort_id = 'us_retail_large'""",
     "companies > 20 and company_years > 100"),

    ("a revenue floor actually narrows its cohort",
     """SELECT (SELECT companies FROM marts.cohorts WHERE cohort_id = 'us_retail_large')
                   AS large,
               (SELECT companies FROM marts.cohorts WHERE cohort_id = 'us_retail_all')
                   AS all_sizes""",
     "large < all_sizes"),
]


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    failures = 0
    for i, (name, query, assertion) in enumerate(CHECKS, 1):
        try:
            cur = con.execute(query)
            row = dict(zip([d[0] for d in cur.description], cur.fetchone()))
        except Exception as exc:  # noqa: BLE001
            print(f"{i:2}. ERROR  {name}\n      {str(exc)[:150]}")
            failures += 1
            continue
        detail = ", ".join(
            f"{k}={v:,}" if isinstance(v, int)
            else f"{k}={v:,.2f}" if isinstance(v, float) else f"{k}={v}"
            for k, v in row.items())
        ok = eval(assertion, {}, {k: (v if v is not None else 0)  # noqa: S307
                                  for k, v in row.items()})
        print(f"{i:2}. {'PASS' if ok else 'FAIL'}  {name}\n      {detail}")
        failures += not ok
    print(f"\n{len(CHECKS) - failures}/{len(CHECKS)} checks passed")
    if failures:
        raise SystemExit(f"{failures} invariant(s) failed")


if __name__ == "__main__":
    main()
