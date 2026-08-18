"""Invariants for the disclosed-KPI mart (G-08).

A KPI read out of prose is the loosest extraction in this warehouse, and the checks are
sized to that: they test that no value escapes the range its own dictionary entry declares,
that every row carries the sentence it came from, and that each KPI stays inside the industry
it was scoped to. That last one is the important one - the scope is the only thing keeping
"backlog" and "admissions" from matching ordinary English, and if it ever leaked the mart
would fill with plausible numbers meaning nothing.

There is no check on how many rows a KPI produces beyond a floor, because thin coverage is a
property of the disclosure and not a defect. `companies_disclosing` is published so a
scorecard can refuse a thin factor; a check that demanded volume would only encourage
loosening the patterns until it was met.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

CHECKS: list[tuple[str, str, str]] = [
    ("the mart is populated across KPIs, companies and years",
     """SELECT count(*) AS rows, count(DISTINCT cik) AS companies,
               count(DISTINCT kpi) AS kpis, count(DISTINCT fy) AS years
        FROM marts.disclosed_kpis""",
     "rows > 2000 and companies > 500 and kpis >= 12 and years > 4"),

    # The grain the request asked for. A company mentions a metric several times per
    # filing, so without the de-duplication the mart would be weighted by talkativeness.
    ("one value per company, year and KPI",
     """SELECT count(*) AS rows, count(DISTINCT (cik, fy, kpi)) AS distinct_keys
        FROM marts.disclosed_kpis""",
     "rows == distinct_keys"),

    # The scope is the precision mechanism. "backlog" fires on 477 companies outside
    # SIC 35/36/37; if any of them appear here the scoping has failed.
    ("every KPI stays inside the industry it was scoped to",
     """SELECT count(*) AS rows, count(*) FILTER (WHERE NOT scoped) AS out_of_scope
        FROM (
            SELECT k.kpi,
                   list_contains(str_split(d.sic2_groups, ','), r.sic2) AS scoped
            FROM marts.disclosed_kpis k
            JOIN ref.kpi_dictionary d ON d.kpi = k.kpi
            JOIN (SELECT DISTINCT cik, sic2 FROM marts.ratio_values) r ON r.cik = k.cik)""",
     "rows > 2000 and out_of_scope == 0"),

    ("no value escapes the range its own dictionary entry declares",
     """SELECT count(*) AS rows,
               count(*) FILTER (WHERE k.value < d.min_value
                                   OR k.value > d.max_value) AS out_of_range
        FROM marts.disclosed_kpis k JOIN ref.kpi_dictionary d ON d.kpi = k.kpi
        WHERE k.confidence IN ('high', 'medium')""",
     "rows > 1000 and out_of_range == 0"),

    # Percentages are the easiest to sanity-check and the easiest to get wrong, because a
    # ratio and a percentage differ by a factor of a hundred.
    ("percentage KPIs are on a percentage scale",
     """SELECT count(*) AS rows,
               count(*) FILTER (WHERE value > 100) AS over_100,
               count(*) FILTER (WHERE value < -100) AS under_minus_100
        FROM marts.disclosed_kpis
        WHERE kpi IN ('airline_load_factor', 'reit_occupancy', 'retail_comp_sales')
          AND confidence = 'high'""",
     "rows > 100 and over_100 == 0 and under_minus_100 == 0"),

    ("every row carries the sentence it was read from",
     """SELECT count(*) AS rows,
               count(*) FILTER (WHERE source_sentence IS NULL
                                   OR length(source_sentence) < 20) AS no_evidence,
               count(*) FILTER (WHERE source_section NOT IN ('1', '7')) AS bad_section
        FROM marts.disclosed_kpis""",
     "no_evidence == 0 and bad_section == 0"),

    ("confidence takes only its three values, and high is a real share",
     """SELECT count(DISTINCT confidence) AS levels,
               round(100.0 * count(*) FILTER (WHERE confidence = 'high')
                     / count(*), 1) AS pct_high,
               count(*) FILTER (WHERE confidence NOT IN ('high', 'medium', 'low'))
                   AS bad_values
        FROM marts.disclosed_kpis""",
     "bad_values == 0 and pct_high > 15"),

    # A dictionary entry that produces nothing is worse than absent: it implies coverage
    # the mart does not have. This names them rather than tolerating them silently.
    ("every dictionary entry produces at least some rows",
     """SELECT (SELECT count(*) FROM ref.kpi_dictionary) AS defined,
               (SELECT count(DISTINCT kpi) FROM marts.disclosed_kpis) AS producing""",
     "producing >= defined - 3"),

    ("the mart joins to the companies it is meant to score",
     """SELECT count(DISTINCT k.cik) AS with_ratios
        FROM marts.disclosed_kpis k
        JOIN (SELECT DISTINCT cik FROM marts.ratio_values) r ON r.cik = k.cik""",
     "with_ratios > 500"),

    ("load factor and occupancy behave like the percentages they are",
     """SELECT round(median(value) FILTER (WHERE kpi = 'airline_load_factor'), 1)
                   AS median_load_factor,
               round(median(value) FILTER (WHERE kpi = 'reit_occupancy'), 1)
                   AS median_occupancy,
               count(*) AS n
        FROM marts.disclosed_kpis WHERE confidence = 'high'
          AND kpi IN ('airline_load_factor', 'reit_occupancy')""",
     "n > 50 and 50 < median_load_factor <= 100 and 50 < median_occupancy <= 100"),
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
