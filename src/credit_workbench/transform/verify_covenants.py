"""Invariants for the covenant levels.

A wrong covenant level looks exactly like a right one - a number, a direction, a
plausible ratio - so these checks are about whether the values are credible as covenants
rather than whether rows exist. Leverage covenants cluster in a known band and coverage
covenants in another; a maximum leverage covenant of 0.5x or a minimum coverage covenant
of 40x would mean the parser had picked up something else entirely.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

CHECKS: list[tuple[str, str, str]] = [
    ("covenant levels extracted across companies",
     """SELECT count(*) AS levels, count(DISTINCT adsh) AS agreements,
               count(DISTINCT cik) AS companies,
               count(DISTINCT covenant_type) AS covenant_types
        FROM marts.covenant_terms""",
     "levels > 300 and companies > 100 and covenant_types >= 5"),

    ("every level carries the sentence it came from",
     """SELECT count(*) AS levels,
               count(*) FILTER (WHERE sentence IS NULL OR length(sentence) < 40)
                   AS without_evidence
        FROM marts.covenant_terms""",
     "without_evidence == 0"),

    # Leverage covenants sit in a recognisable band. Anything far outside it means the
    # sentence was not a leverage covenant.
    ("maximum leverage covenants fall in a credible band",
     """SELECT count(*) AS n, round(median(level), 2) AS median_level,
               round(100.0 * count(*) FILTER (WHERE level BETWEEN 1.5 AND 9)
                     / count(*), 1) AS pct_in_band
        FROM marts.covenant_terms
        WHERE direction = 'max' AND unit = 'ratio' AND near_covenant_heading
          AND covenant_type LIKE '%leverage%'""",
     "n > 50 and pct_in_band > 80 and 2.5 <= median_level <= 6"),

    ("minimum coverage covenants fall in a credible band",
     """SELECT count(*) AS n, round(median(level), 2) AS median_level,
               round(100.0 * count(*) FILTER (WHERE level BETWEEN 1 AND 5)
                     / count(*), 1) AS pct_in_band
        FROM marts.covenant_terms
        WHERE direction = 'min' AND unit = 'ratio' AND near_covenant_heading
          AND covenant_type LIKE '%coverage%'""",
     "n > 20 and pct_in_band > 70 and 1 <= median_level <= 4"),

    # The direction is the half of a covenant that inverts its meaning, so it must
    # follow the covenant rather than be spread evenly across both.
    ("leverage covenants are maxima and coverage covenants are minima",
     """SELECT round(100.0 * count(*) FILTER (
                   WHERE covenant_type LIKE '%leverage%' AND direction = 'max')
                 / nullif(count(*) FILTER (WHERE covenant_type LIKE '%leverage%'), 0), 1)
                   AS pct_leverage_max,
               round(100.0 * count(*) FILTER (
                   WHERE covenant_type LIKE '%coverage%' AND direction = 'min')
                 / nullif(count(*) FILTER (WHERE covenant_type LIKE '%coverage%'), 0), 1)
                   AS pct_coverage_min
        FROM marts.covenant_terms WHERE near_covenant_heading""",
     "pct_leverage_max > 92 and pct_coverage_min > 90"),

    # A leverage covenant is a ceiling. Recording one as a floor inverts what the
    # borrower promised, and compound sentences - "the Leverage Ratio to exceed 4.00
    # and the Fixed Charge Coverage Ratio to be less than 1.15" - produced 416 of them
    # before each covenant was given its own clause.
    ("leverage covenants are not recorded as floors",
     """SELECT count(*) FILTER (WHERE covenant_type LIKE '%leverage%') AS leverage_levels,
               count(*) FILTER (WHERE covenant_type LIKE '%leverage%'
                                  AND direction = 'min') AS recorded_as_floor
        FROM marts.covenant_terms""",
     "leverage_levels > 500 and recorded_as_floor < leverage_levels * 0.04"),

    ("incurrence language was excluded, not merely hoped against",
     """SELECT count(*) AS levels,
               count(*) FILTER (
                   WHERE lower(sentence) LIKE '%after giving effect%'
                      OR lower(sentence) LIKE '%at the election of%'
                      OR lower(sentence) LIKE '%in connection with%'
                      OR lower(sentence) LIKE '%pro forma basis%') AS incurrence_leaked
        FROM marts.covenant_terms""",
     "incurrence_leaked == 0"),

    ("step-down schedules are kept as several levels",
     """SELECT count(*) FILTER (WHERE is_schedule) AS levels_in_schedules,
               count(DISTINCT adsh) FILTER (WHERE is_schedule) AS agreements
        FROM marts.covenant_terms""",
     "levels_in_schedules > 20"),

    ("the headline view gives one binding level per company and covenant",
     """SELECT count(*) AS rows, count(DISTINCT (cik, covenant_type)) AS pairs
        FROM marts.covenant_headline""",
     "rows == pairs and rows > 50"),

    ("covenant levels join back to a real agreement",
     """SELECT count(*) AS orphans FROM (
            SELECT t.adsh FROM marts.covenant_terms t
            LEFT JOIN quali.debt_agreements a ON a.adsh = t.adsh
            WHERE a.adsh IS NULL LIMIT 10000)""",
     "orphans == 0"),
]


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    failures = 0
    for i, (name, query, assertion) in enumerate(CHECKS, 1):
        try:
            cur = con.execute(query)
            row = dict(zip([d[0] for d in cur.description], cur.fetchone()))
        except Exception as exc:  # noqa: BLE001
            print(f"{i:2}. ERROR  {name}\n      {str(exc)[:170]}")
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
