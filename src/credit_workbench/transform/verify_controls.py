"""Invariants for the ICFR control signals.

A text-derived flag fails quietly, and this one is worse than most because a wrong ICFR
conclusion is indistinguishable from a right one without reading the filing. So the checks
test properties the extraction must satisfy: the adverse rate has to sit where the
literature and the earlier probe both put it, and - the one that actually matters - an
adverse conclusion has to travel with distress. If that lift ever disappears, the signal has
stopped working and no amount of coverage compensates.

The comparison against `material_weakness_identified` is kept as a check rather than deleted,
because it is the documented counter-example: the phrase discriminates the wrong way and the
polarity does not. Keeping both measurable in one table is how that stays visible instead of
becoming folklore.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

CHECKS: list[tuple[str, str, str]] = [
    ("the table covers Item 9A across companies and years",
     """SELECT count(*) AS rows, count(DISTINCT cik) AS companies,
               count(DISTINCT substr(filing_date, 1, 4)) AS years,
               count(icfr_conclusion) AS with_conclusion
        FROM marts.control_signals""",
     "rows > 100000 and companies > 15000 and years > 10 and with_conclusion > 60000"),

    ("one row per company and filing",
     """SELECT count(*) AS rows, count(DISTINCT (cik, adsh)) AS distinct_pairs
        FROM marts.control_signals""",
     "rows == distinct_pairs"),

    ("the conclusion only ever takes its two values or null",
     """SELECT count(*) AS rows,
               count(*) FILTER (WHERE icfr_conclusion NOT IN
                                      ('effective', 'not_effective')) AS other_values
        FROM marts.control_signals WHERE icfr_conclusion IS NOT NULL""",
     "other_values == 0"),

    # Both patterns must contribute. If the second stopped firing, the adverse
    # under-reporting it was added to fix would silently return.
    ("both phrasings carry conclusions",
     """SELECT count(*) FILTER (WHERE icfr_pattern = 'adjective_after_subject') AS after_form,
               count(*) FILTER (WHERE icfr_pattern = 'adjective_before_subject')
                   AS before_form
        FROM marts.control_signals WHERE icfr_conclusion IS NOT NULL""",
     "after_form > 20000 and before_form > 1000"),

    # 11-15% was measured across filing years. A population of all SEC filers runs higher
    # than the ~5% quoted for accelerated filers, because management-only assessments by
    # smaller companies fail far more often.
    ("the adverse rate stays in its measured band",
     """SELECT round(100.0 * count(*) FILTER (WHERE icfr_conclusion = 'not_effective')
                     / count(icfr_conclusion), 1) AS pct_adverse,
               count(icfr_conclusion) AS n
        FROM marts.control_signals
        WHERE substr(filing_date, 1, 4) BETWEEN '2019' AND '2025'""",
     "n > 30000 and 8 < pct_adverse < 25"),

    # The reason the table exists. A 2.3x lift was measured; anything at or below 1.3x means
    # the extraction has stopped tracking reality.
    ("an adverse conclusion still travels with distress",
     """WITH j AS (
            SELECT c.icfr_conclusion, o.distress_24m
            FROM marts.control_signals c
            JOIN marts.credit_outcomes o ON o.cik = c.cik AND o.fy = c.fy
            WHERE c.icfr_conclusion IS NOT NULL)
        SELECT count(*) AS company_years,
               round(100.0 * avg(CASE WHEN distress_24m THEN 1.0 ELSE 0.0 END)
                     FILTER (WHERE icfr_conclusion = 'not_effective'), 2) AS adverse_rate,
               round(100.0 * avg(CASE WHEN distress_24m THEN 1.0 ELSE 0.0 END)
                     FILTER (WHERE icfr_conclusion = 'effective'), 2) AS clean_rate
        FROM j""",
     "company_years > 10000 and adverse_rate > clean_rate * 1.3"),

    # The documented counter-example, kept measurable. If the phrase ever started
    # discriminating correctly, that would be worth knowing too.
    ("the phrase still discriminates worse than the polarity",
     """WITH j AS (
            SELECT c.material_weakness_identified AS flagged, c.icfr_conclusion,
                   o.distress_24m
            FROM marts.control_signals c
            JOIN marts.credit_outcomes o ON o.cik = c.cik AND o.fy = c.fy)
        SELECT round(100.0 * avg(CASE WHEN distress_24m THEN 1.0 ELSE 0.0 END)
                     FILTER (WHERE flagged), 2) AS phrase_flagged_rate,
               round(100.0 * avg(CASE WHEN distress_24m THEN 1.0 ELSE 0.0 END)
                     FILTER (WHERE NOT flagged), 2) AS phrase_clean_rate,
               round(100.0 * avg(CASE WHEN distress_24m THEN 1.0 ELSE 0.0 END)
                     FILTER (WHERE icfr_conclusion = 'not_effective'), 2)
                   AS polarity_adverse_rate
        FROM j""",
     "polarity_adverse_rate > phrase_flagged_rate"),

    # The boilerplate trap, quantified in the table itself so it cannot be forgotten.
    ("the weakness definition really is boilerplate, and not the finding",
     """SELECT round(100.0 * count(*) FILTER (WHERE carries_weakness_definition)
                     / count(*), 1) AS pct_with_definition,
               round(100.0 * count(*) FILTER (WHERE material_weakness_identified)
                     / count(*), 1) AS pct_with_finding,
               round(100.0 * count(*) FILTER (WHERE icfr_conclusion = 'not_effective')
                     / count(*), 1) AS pct_adverse_conclusion
        FROM marts.control_signals""",
     "pct_with_finding > pct_adverse_conclusion"),

    ("a conclusion always comes with the sentence it was read from",
     """SELECT count(*) AS with_conclusion,
               count(*) FILTER (WHERE conclusion_sentence IS NULL
                                   OR length(conclusion_sentence) < 20) AS no_evidence
        FROM marts.control_signals WHERE icfr_conclusion IS NOT NULL""",
     "with_conclusion > 60000 and no_evidence == 0"),

    ("conflicting conclusions are rare and flagged rather than hidden",
     """SELECT count(*) FILTER (WHERE conflicting_conclusions) AS conflicting,
               round(100.0 * count(*) FILTER (WHERE conflicting_conclusions)
                     / count(icfr_conclusion), 1) AS pct_of_conclusions
        FROM marts.control_signals""",
     "pct_of_conclusions < 20"),

    ("the mart joins to the outcome labels it is meant to be scored against",
     """SELECT count(DISTINCT c.cik) AS scored_companies
        FROM marts.control_signals c
        JOIN marts.credit_outcomes o ON o.cik = c.cik AND o.fy = c.fy""",
     "scored_companies > 5000"),
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
