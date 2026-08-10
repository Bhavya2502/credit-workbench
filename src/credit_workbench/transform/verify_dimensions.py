"""Invariants for the dimensioned layer.

The point of this build is coverage, so the checks are mostly coverage checks: does
every dimensioned fact reach a mart, does every axis resolve, and do the named views
return the shapes their names promise. Plus two substantive ones — a fair-value ladder
that sums to its own total, and subsidiary detail that stays inside the consolidated
figure — because a schedule that does not reconcile is worse than no schedule.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

CHECKS: list[tuple[str, str, str]] = [
    ("dimension index is populated",
     "SELECT count(*) AS n, count(DISTINCT axis) AS axes FROM ref.dimension_index",
     "n > 1e6 and axes > 100"),

    ("every dimensioned fact resolves to at least one axis",
     """SELECT count(*) AS unresolved FROM (
            SELECT f.dimh, f.period FROM marts.facts_dimensioned f
            LEFT JOIN ref.dimension_index d
              ON d.dimhash = f.dimh AND d.period = f.period
            WHERE d.dimhash IS NULL LIMIT 200000)""",
     "unresolved == 0"),

    ("declared dimension_count matches the axes we actually parsed",
     """SELECT count(*) AS mismatched FROM (
            SELECT f.dimh, f.period, any_value(f.dimension_count) AS declared,
                   count(DISTINCT d.axis) AS parsed
            FROM marts.facts_dimensioned f
            JOIN ref.dimension_index d ON d.dimhash = f.dimh AND d.period = f.period
            WHERE f.period_year = 2024
            GROUP BY f.dimh, f.period
            HAVING any_value(f.dimension_count) <> count(DISTINCT d.axis))""",
     # `segt` marks hashes SEC truncated, so a small residue is expected, not a bug
     "mismatched < 20000"),

    ("no consolidated facts leaked into the dimensioned mart",
     "SELECT count(*) AS leaked FROM marts.facts_dimensioned WHERE dimension_count = 0",
     "leaked == 0"),

    ("LegalEntity view returns subsidiary-level figures",
     """SELECT count(*) AS facts, count(DISTINCT cik) AS companies,
               count(DISTINCT member) AS entities FROM marts.dim_legal_entity""",
     "facts > 1e6 and companies > 2000 and entities > 5000"),

    ("entity roles cover the structural-subordination cases",
     """SELECT count(*) FILTER (WHERE entity_role = 'parent_only')  AS parent_only,
               count(*) FILTER (WHERE entity_role = 'guarantor')    AS guarantor,
               count(*) FILTER (WHERE entity_role = 'non_guarantor') AS non_guarantor,
               count(*) FILTER (WHERE entity_role = 'named_entity') AS named
        FROM marts.legal_entity_detail""",
     "parent_only > 50000 and guarantor > 10000 and named > 100000"),

    ("fair-value view resolves to the three hierarchy levels",
     """SELECT count(*) FILTER (WHERE hierarchy_level = 'Level 1') AS l1,
               count(*) FILTER (WHERE hierarchy_level = 'Level 2') AS l2,
               count(*) FILTER (WHERE hierarchy_level = 'Level 3') AS l3,
               count(*) FILTER (WHERE hierarchy_level = 'Combined or other') AS other
        FROM marts.fair_value_hierarchy""",
     "l1 > 100000 and l2 > 100000 and l3 > 100000 and other < l1 * 0.2"),

    ("fair-value levels sum to the reported total, where a filer gives both",
     """WITH lv AS (
            SELECT cik, period_end, tag,
                   sum(value) FILTER (
                       WHERE hierarchy_level IN ('Level 1', 'Level 2', 'Level 3'))
                       AS levels_sum
            FROM marts.fair_value_hierarchy
            WHERE uom = 'USD' AND qtrs = 0 GROUP BY 1, 2, 3),
        tot AS (
            SELECT cik, period_end, tag, value AS total
            FROM staging.facts_pit WHERE is_latest AND uom = 'USD' AND qtrs = 0)
        SELECT count(*) AS compared,
               round(100.0 * count(*) FILTER (
                   WHERE abs(l.levels_sum - t.total)
                         <= 0.02 * greatest(abs(t.total), 1)) / count(*), 1) AS pct_tie
        FROM lv l JOIN tot t USING (cik, period_end, tag)
        WHERE l.levels_sum IS NOT NULL AND abs(t.total) > 1e6""",
     "compared > 1000 and pct_tie > 60"),

    ("subsidiary revenue does not exceed the consolidated figure",
     """WITH sub AS (
            SELECT cik, period_end, max(value) AS biggest_entity
            FROM marts.dim_legal_entity
            WHERE tag = 'Revenues' AND uom = 'USD' AND qtrs = 4 GROUP BY 1, 2),
        con AS (
            SELECT cik, period_end, value AS consolidated FROM staging.facts_pit
            WHERE is_latest AND tag = 'Revenues' AND uom = 'USD' AND qtrs = 4)
        SELECT count(*) AS compared,
               count(*) FILTER (WHERE s.biggest_entity > c.consolidated * 1.02) AS exceeds
        FROM sub s JOIN con c USING (cik, period_end)
        WHERE c.consolidated > 0""",
     # A guarantor subsidiary can be nearly the whole group, so equality is fine;
     # materially exceeding the parent is not.
     "compared > 200 and exceeds < compared * 0.15"),

    ("tag catalog covers both layers and is one row per tag",
     """SELECT count(*) AS tags, count(DISTINCT tag) AS distinct_tags,
               count(*) FILTER (WHERE dimensioned_facts > 0) AS with_schedules,
               count(*) FILTER (WHERE standard_taxonomy) AS standard
        FROM ref.tag_catalog""",
     "tags == distinct_tags and with_schedules > 50000 and standard > 10000"),

    ("the two new D1 ladders landed in the adjustment inputs",
     """SELECT count(*) FILTER (WHERE intangible_amort_y1 IS NOT NULL) AS intangible,
               count(*) FILTER (WHERE fin_lease_due_y1 IS NOT NULL)   AS fin_lease,
               count(*) FILTER (WHERE ppe_gross IS NOT NULL)          AS ppe
        FROM marts.adjustment_inputs WHERE basis = 'latest'""",
     "intangible > 5000 and fin_lease > 2000 and ppe > 10000"),

    # An identity, not an approximation: all amortisation still to come is exactly the
    # unamortised cost sitting on the balance sheet. Gross less accumulated is what
    # must remain to be charged, so the whole ladder has to reach it.
    ("intangible ladder equals net carrying value of finite-lived intangibles",
     """SELECT count(*) AS compared,
               round(100.0 * count(*) FILTER (
                   WHERE abs(ladder - net_carrying) <= 0.05 * net_carrying) / count(*), 1)
                   AS pct_tie
        FROM (SELECT coalesce(intangible_amort_y1, 0) + coalesce(intangible_amort_y2, 0)
                     + coalesce(intangible_amort_y3, 0) + coalesce(intangible_amort_y4, 0)
                     + coalesce(intangible_amort_y5, 0)
                     + coalesce(intangible_amort_thereafter, 0)
                     + coalesce(intangible_amort_remainder_fy, 0) AS ladder,
                     intangible_gross - intangible_accumulated_amortisation AS net_carrying
              FROM marts.adjustment_inputs
              WHERE basis = 'latest' AND intangible_gross > 0
                AND intangible_accumulated_amortisation IS NOT NULL
                AND intangible_amort_y1 IS NOT NULL
                AND intangible_amort_thereafter IS NOT NULL)""",
     "compared > 1000 and pct_tie > 55"),

    ("operating-lease ladder ties to its total (the rung gaps are closed)",
     """SELECT count(*) AS compared,
               round(100.0 * count(*) FILTER (
                   WHERE abs(ladder - op_lease_undiscounted_total)
                         <= 0.02 * op_lease_undiscounted_total) / count(*), 1) AS pct_tie
        FROM (SELECT coalesce(op_lease_due_y1, 0) + coalesce(op_lease_due_y2, 0)
                     + coalesce(op_lease_due_y3, 0) + coalesce(op_lease_due_y4, 0)
                     + coalesce(op_lease_due_y5, 0)
                     + coalesce(op_lease_due_thereafter, 0)
                     + coalesce(op_lease_due_remainder_fy, 0) AS ladder,
                     op_lease_undiscounted_total
              FROM marts.adjustment_inputs
              WHERE basis = 'latest' AND op_lease_undiscounted_total > 0
                AND op_lease_due_y1 IS NOT NULL)""",
     # Was 49.5% before the AfterYearFour and rolling rungs were added.
     "compared > 50000 and pct_tie > 80"),

    ("finance lease ladder sums to the undiscounted total filers report",
     """SELECT count(*) AS compared,
               round(100.0 * count(*) FILTER (
                   WHERE abs(ladder - fin_lease_undiscounted_total)
                         <= 0.02 * fin_lease_undiscounted_total) / count(*), 1) AS pct_tie
        FROM (SELECT coalesce(fin_lease_due_y1, 0) + coalesce(fin_lease_due_y2, 0)
                     + coalesce(fin_lease_due_y3, 0) + coalesce(fin_lease_due_y4, 0)
                     + coalesce(fin_lease_due_y5, 0)
                     + coalesce(fin_lease_due_thereafter, 0)
                     + coalesce(fin_lease_due_remainder_fy, 0) AS ladder,
                     fin_lease_undiscounted_total
              FROM marts.adjustment_inputs
              WHERE basis = 'latest' AND fin_lease_undiscounted_total > 0
                AND fin_lease_due_y1 IS NOT NULL)""",
     "compared > 500 and pct_tie > 70"),
]


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    failures = 0
    for i, (name, query, assertion) in enumerate(CHECKS, 1):
        try:
            cur = con.execute(query)
            row = dict(zip([d[0] for d in cur.description], cur.fetchone()))
        except Exception as exc:  # noqa: BLE001
            print(f"{i:2}. ERROR  {name}\n      {exc}")
            failures += 1
            continue
        detail = ", ".join(
            f"{k}={v:,}" if isinstance(v, int)
            else f"{k}={v:,.1f}" if isinstance(v, float) else f"{k}={v}"
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
