"""Invariants for the adjusted metrics (G-04, G-05).

An adjusted figure is the easiest kind of number to get quietly wrong, because every
intermediate step is plausible and the answer is never checkable against a published
total the way an audit fee is. So these checks lean on relationships that hold by
definition: adjusted debt cannot be less than reported debt, EBITDAR cannot be less than
EBITDA, and the `reported` policy must produce exactly the unadjusted figures or the
baseline is not a baseline.

The one that matters most is that the policies actually differ from each other in the
direction stated. If `lease_8x` and `lease_6x` produced the same leverage, the multiple
would not be reaching the arithmetic and the whole parameterisation would be decorative.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

CHECKS: list[tuple[str, str, str]] = [
    ("the mart is populated across policies, companies and years",
     """SELECT count(*) AS rows, count(DISTINCT cik) AS companies,
               count(DISTINCT policy) AS policies, count(DISTINCT fy) AS years
        FROM marts.adjusted_metrics""",
     "rows > 100000 and companies > 3000 and policies == 5 and years > 10"),

    ("one row per company, year, basis and policy — no fan-out from the pivot",
     """SELECT count(*) AS rows,
               count(DISTINCT (cik, fy, basis, policy)) AS distinct_keys
        FROM marts.adjusted_metrics""",
     "rows == distinct_keys"),

    # `with_debt` is counted, not just the violations. Both of the next two checks passed a
    # build in which adjusted_debt was NULL on every one of 726,690 rows, because a NULL
    # comparison is neither true nor false and so counts as no violation. A check that
    # cannot distinguish "nothing is wrong" from "there is nothing here" is not a check, so
    # each now asserts that it had something to look at.
    ("the reported policy adds nothing",
     """SELECT count(*) AS rows,
               count(adjusted_debt) AS with_debt,
               count(*) FILTER (WHERE capitalised_leases <> 0) AS leases_added,
               count(*) FILTER (WHERE pension_deficit <> 0) AS pension_added,
               count(*) FILTER (WHERE abs(adjusted_debt - reported_debt
                                          - finance_lease_debt) > 1) AS debt_differs
        FROM marts.adjusted_metrics WHERE policy = 'reported'""",
     "with_debt > 50000 and leases_added == 0 and pension_added == 0 "
     "and debt_differs == 0"),

    ("adjusted debt is never below reported debt",
     """SELECT count(*) AS rows, count(adjusted_debt) AS with_debt,
               count(*) FILTER (WHERE adjusted_debt < reported_debt - 1) AS impossible,
               count(*) FILTER (WHERE capitalised_leases < 0
                                   OR pension_deficit < 0) AS negative_adjustment
        FROM marts.adjusted_metrics""",
     "with_debt > 250000 and impossible == 0 and negative_adjustment == 0"),

    ("EBITDAR is never below EBITDA",
     """SELECT count(*) AS rows,
               count(*) FILTER (WHERE ebitdar < ebitda - 1) AS impossible
        FROM marts.adjusted_metrics WHERE ebitda IS NOT NULL AND ebitdar IS NOT NULL""",
     "rows > 10000 and impossible == 0"),

    # If the multiple did not reach the arithmetic, parameterising it is decorative.
    ("the lease multiple actually changes the answer",
     """SELECT count(*) AS compared,
               count(*) FILTER (WHERE a.capitalised_leases > b.capitalised_leases)
                   AS eight_exceeds_six,
               count(*) FILTER (WHERE a.capitalised_leases <> b.capitalised_leases)
                   AS differ_at_all
        FROM marts.adjusted_metrics a
        JOIN marts.adjusted_metrics b
          ON b.cik = a.cik AND b.fy = a.fy AND b.basis = a.basis
        WHERE a.policy = 'lease_8x' AND b.policy = 'lease_6x'
          AND a.lease_source = 'rent_capitalised'""",
     "compared > 100 and eight_exceeds_six == differ_at_all and differ_at_all > 0"),

    # Capitalising at a multiple of rent raises leverage only below a crossover, and the
    # crossover is the multiple itself — but only when the lease is the *sole* difference
    # between the two policies being compared.
    #
    # This check has been wrong twice, both times because the algebra was incomplete.
    # First it asserted leverage always rises, which is false: comparing
    # (debt + 8r)/(ebitda + r) against debt/ebitda reduces to 8·ebitda against debt, so
    # above 8x the adjustment lowers leverage. Then it compared lease_8x against `reported`,
    # which also differs by the pension deficit P, giving 8·E + P·E/r against debt - a
    # crossover that moves with P and is not 8 at all. That left 18 rows failing.
    #
    # `pension_only` is the correct baseline: it carries the same pension deficit and the
    # same finance leases, so the operating lease is the only difference and the crossover
    # is exactly the multiple. Rows within a whisker of 8x are skipped, since either
    # direction is correct there.
    ("capitalising leases raises leverage below the multiple and lowers it above",
     """SELECT count(*) AS compared,
               count(*) FILTER (WHERE b.adjusted_leverage < 7.9
                                  AND a.adjusted_leverage
                                      < b.adjusted_leverage - 0.001) AS rose_when_it_should,
               count(*) FILTER (WHERE b.adjusted_leverage > 8.1
                                  AND a.adjusted_leverage
                                      > b.adjusted_leverage + 0.001) AS fell_when_it_should
        FROM marts.adjusted_metrics a
        JOIN marts.adjusted_metrics b
          ON b.cik = a.cik AND b.fy = a.fy AND b.basis = a.basis
        WHERE a.policy = 'lease_8x' AND b.policy = 'pension_only'
          AND a.lease_source = 'rent_capitalised'
          AND a.adjusted_leverage IS NOT NULL AND b.adjusted_leverage IS NOT NULL""",
     "compared > 1000 and rose_when_it_should == 0 and fell_when_it_should == 0"),

    # G-05. The splice must follow the stated rule, and the eras must land where the
    # accounting standard says: ASC 842 applies to fiscal years beginning after 15 Dec 2018.
    ("the lease source follows the accounting eras",
     """SELECT count(*) FILTER (WHERE fy >= 2020
                                  AND lease_source = 'asc842_reported_liability')
                   AS asc842_after_2020,
               count(*) FILTER (WHERE fy <= 2017
                                  AND lease_source = 'asc842_reported_liability')
                   AS asc842_before_2018,
               count(*) FILTER (WHERE fy <= 2017
                                  AND lease_source = 'rent_capitalised')
                   AS asc840_before_2018
        FROM marts.adjusted_metrics WHERE policy = 'lease_8x' AND basis = 'first_reported'""",
     "asc842_after_2020 > 5000 and asc840_before_2018 > 1000 "
     "and asc842_before_2018 < asc842_after_2020 / 50"),

    ("adjusted leverage is a plausible multiple, not an artefact",
     """SELECT count(*) AS n, round(median(adjusted_leverage), 2) AS median_leverage,
               count(*) FILTER (WHERE adjusted_leverage < 0) AS negative,
               round(100.0 * count(*) FILTER (WHERE adjusted_leverage > 100)
                     / count(*), 2) AS pct_over_100x
        FROM marts.adjusted_metrics
        WHERE policy = 'lease_8x' AND basis = 'first_reported'
          AND adjusted_leverage IS NOT NULL""",
     "n > 10000 and 0 < median_leverage < 15 and negative == 0 and pct_over_100x < 5"),

    ("fixed charge cover is a plausible cover ratio",
     """SELECT count(*) AS n, round(median(fixed_charge_cover), 2) AS median_fcc,
               count(*) FILTER (WHERE fixed_charge_cover < 0) AS negative
        FROM marts.adjusted_metrics
        WHERE policy = 'lease_8x' AND basis = 'first_reported'
          AND fixed_charge_cover IS NOT NULL""",
     "n > 10000 and 0 < median_fcc < 50"),

    ("the policy table documents every policy the mart uses",
     """SELECT (SELECT count(DISTINCT policy) FROM marts.adjusted_metrics) AS in_mart,
               (SELECT count(*) FROM ref.adjustment_policy) AS documented""",
     "in_mart == documented"),

    ("the published splice view agrees with the mart on lease source",
     """SELECT count(*) AS compared,
               count(*) FILTER (WHERE v.lease_source <> m.lease_source
                                  AND v.lease_source <> 'asc840_ladder_only') AS disagree
        FROM marts.lease_adjustment v
        JOIN marts.adjusted_metrics m
          ON m.cik = v.cik AND m.fy = v.fy AND m.basis = v.basis
        WHERE m.policy = 'lease_8x'""",
     "compared > 10000 and disagree == 0"),
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
