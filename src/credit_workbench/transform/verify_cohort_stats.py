"""Invariants for the cohort coverage and outcome marts (G-02, G-03).

Both tables are aggregates, and a wrong aggregate is the hardest kind of error to see: a
coverage percentage of 140% announces itself, but a coverage percentage of 61% that should
be 6% does not. So the checks here are mostly arithmetic that must hold by construction -
a numerator cannot exceed its denominator, a p10 cannot exceed a p90, an 'ALL' size band
must contain at least as many companies as any single band within it.

The one substantive check is that the two industry schemes disagree in the direction the
design decision predicted: `peer_group` must produce a materially higher share of cohorts
big enough to draw a distribution from than `sic2` does. That is the whole reason both are
published, and if it stopped being true the second scheme would be dead weight.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

CHECKS: list[tuple[str, str, str]] = [
    # ---- ratio_coverage
    ("coverage table is populated across ratios, industries and years",
     """SELECT count(*) AS rows, count(DISTINCT ratio) AS ratios,
               count(DISTINCT industry_code) AS industries,
               count(DISTINCT fy) AS years,
               count(DISTINCT industry_scheme) AS schemes
        FROM marts.ratio_coverage""",
     "rows > 100000 and ratios > 30 and years > 10 and schemes == 2"),

    ("a ratio never computes for more companies than the cohort holds",
     """SELECT count(*) AS rows,
               count(*) FILTER (WHERE companies_with_value > companies_total)
                   AS impossible,
               max(coverage_pct) AS max_coverage_pct
        FROM marts.ratio_coverage""",
     "impossible == 0 and max_coverage_pct <= 100.0"),

    ("percentiles are ordered",
     """SELECT count(*) AS rows,
               count(*) FILTER (WHERE p10 > p25 OR p25 > p50
                                   OR p50 > p75 OR p75 > p90) AS out_of_order,
               count(*) FILTER (WHERE p50 < min_value OR p50 > max_value)
                   AS median_outside_range
        FROM marts.ratio_coverage""",
     "out_of_order == 0 and median_outside_range == 0"),

    # The pooled band is the same companies without the size cut, so it cannot be smaller.
    ("the ALL size band is never smaller than a band inside it",
     """SELECT count(*) AS compared, count(*) FILTER (WHERE b.companies_with_value
                                                          > a.companies_with_value)
                   AS all_smaller_than_part
        FROM marts.ratio_coverage a
        JOIN marts.ratio_coverage b
          ON b.industry_scheme = a.industry_scheme
         AND b.industry_code = a.industry_code AND b.fy = a.fy
         AND b.basis = a.basis AND b.ratio = a.ratio
        WHERE a.size_band = 'ALL' AND b.size_band <> 'ALL'""",
     "compared > 1000 and all_smaller_than_part == 0"),

    # This is the design decision, stated as a property. The peer groups were rolled up
    # until they reached thirty companies; if they no longer beat sic2 on that measure the
    # bridge has regressed and publishing two schemes is pointless.
    ("peer groups yield more usable cohorts than sic2, which is why both exist",
     """SELECT round(100.0 * count(*) FILTER (WHERE is_sufficient AND
                                              industry_scheme = 'peer_group')
                     / nullif(count(*) FILTER (WHERE industry_scheme = 'peer_group'), 0),
                     1) AS pct_sufficient_peer,
               round(100.0 * count(*) FILTER (WHERE is_sufficient AND
                                              industry_scheme = 'sic2')
                     / nullif(count(*) FILTER (WHERE industry_scheme = 'sic2'), 0),
                     1) AS pct_sufficient_sic2
        FROM marts.ratio_coverage WHERE size_band = 'ALL'""",
     "pct_sufficient_peer > pct_sufficient_sic2"),

    # If almost every cell were sufficient the threshold would be doing nothing; if almost
    # none were, the table would have no usable content. Both would mean a broken grain.
    ("the sufficiency flag actually discriminates",
     """SELECT round(100.0 * count(*) FILTER (WHERE is_sufficient) / count(*), 1)
                   AS pct_sufficient,
               round(median(companies_with_value), 0) AS median_companies
        FROM marts.ratio_coverage""",
     "5 < pct_sufficient < 95"),

    ("every ratio in ratio_values appears in the coverage table",
     """SELECT (SELECT count(DISTINCT ratio) FROM marts.ratio_values) AS in_source,
               (SELECT count(DISTINCT ratio) FROM marts.ratio_coverage) AS in_coverage""",
     "in_coverage == in_source"),

    # ---- outcome_counts
    ("outcome counts are populated with both schemes and a pooled window",
     """SELECT count(*) AS rows, count(DISTINCT industry_code) AS industries,
               count(*) FILTER (WHERE fy IS NULL) AS pooled_rows,
               count(DISTINCT industry_scheme) AS schemes
        FROM marts.outcome_counts""",
     "rows > 1000 and schemes == 2 and pooled_rows > 100"),

    ("no outcome flag exceeds the company-years it is counted from",
     """SELECT count(*) AS rows,
               count(*) FILTER (WHERE distress_24m > company_years
                                   OR default_24m > company_years
                                   OR bankruptcy_24m > company_years) AS impossible,
               count(*) FILTER (WHERE companies > company_years) AS more_cos_than_years
        FROM marts.outcome_counts""",
     "impossible == 0 and more_cos_than_years == 0"),

    # Nesting: 12-month distress is a subset of 24-month, and bankruptcy of default.
    ("the outcome flags nest the way their definitions require",
     """SELECT count(*) FILTER (WHERE distress_12m > distress_24m) AS distress_12_over_24,
               count(*) FILTER (WHERE default_12m > default_24m) AS default_12_over_24,
               count(*) FILTER (WHERE bankruptcy_24m > default_24m)
                   AS bankruptcy_over_default
        FROM marts.outcome_counts""",
     "distress_12_over_24 == 0 and default_12_over_24 == 0"),

    # The join to size_band goes through ratio_values, which has ~90 rows per company-year.
    # Aggregating first is what stops that fanning out; this is the check that it did.
    ("the size-band join did not fan out the outcome population",
     """SELECT (SELECT count(*) FROM marts.credit_outcomes) AS source_rows,
               (SELECT max(company_years) FROM marts.outcome_counts
                WHERE fy IS NULL AND industry_scheme = 'sic2') AS biggest_pooled_cohort""",
     "biggest_pooled_cohort <= source_rows"),

    ("pooled company-years reconcile to the source, scheme by scheme",
     """SELECT (SELECT count(*) FROM marts.credit_outcomes
                WHERE sic2 IS NOT NULL AND sic2 <> '') AS source_with_sic2,
               (SELECT sum(company_years) FROM marts.outcome_counts
                WHERE fy IS NULL AND industry_scheme = 'sic2') AS summed_pooled""",
     "summed_pooled == source_with_sic2"),

    ("enough cohorts carry enough events to calibrate a scale at all",
     """SELECT count(*) FILTER (WHERE can_calibrate_default) AS can_default,
               count(*) FILTER (WHERE can_calibrate_distress) AS can_distress,
               count(*) AS rows
        FROM marts.outcome_counts""",
     "can_distress > 50 and can_default > 10"),

    ("default rates are rates, and not implausibly high",
     """SELECT max(default_24m_rate) AS max_default_rate,
               round(median(distress_24m_rate), 2) AS median_distress_rate,
               count(*) FILTER (WHERE default_24m_rate > 100) AS over_100
        FROM marts.outcome_counts WHERE company_years >= 30""",
     "over_100 == 0 and max_default_rate <= 100"),
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
