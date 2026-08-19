"""Measured inventory of everything built against DATA_GAPS_FOR_WAREHOUSE.md.

A coverage report written from memory is worth nothing here. Four gaps in that document were
filed against tables that already existed, and two of our own answers were later overturned
by a query - so this counts every object the gap work produced, from the warehouse, and
reports what is actually there rather than what was intended.

Objects that are missing show as missing. That is the point: a coverage report whose only
possible outcome is "all present" would be the same failure in a different costume.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

# gap, object, what it answers
BUILT = [
    ("G-02", "marts.ratio_coverage", "ratio coverage and distribution per cohort"),
    ("G-03", "marts.outcome_counts", "credit events per cohort"),
    ("G-04", "marts.adjusted_metrics", "agency-style adjusted figures"),
    ("G-04", "ref.adjustment_policy", "what each adjustment policy assumes"),
    ("G-05", "marts.lease_adjustment", "the ASC 840 to 842 splice"),
    ("G-08", "marts.disclosed_kpis", "sector operating KPIs from narrative"),
    ("G-08", "ref.kpi_dictionary", "KPI phrases, scope, units, coverage"),
    ("G-08", "staging.kpi_lines", "candidate lines, fingerprinted"),
    ("G-09", "marts.risk_themes", "risk factors classified at heading grain"),
    ("G-09", "marts.risk_theme_prevalence", "theme by industry and year"),
    ("G-09", "ref.risk_theme_dictionary", "theme patterns"),
    ("G-09", "staging.risk_headings", "risk headings, fingerprinted"),
    ("G-18", "marts.cohorts", "named cohorts, resolved"),
    ("G-18", "marts.cohort_members", "cohort membership snapshot"),
    ("G-18", "ref.cohort_definition", "cohort criteria"),
    ("G-19", "ref.company_names", "every name a CIK has filed under"),
    ("G-19", "ref.name_collisions", "names one company left and another uses"),
    ("G-19", "ref.company_filing_span", "first and last filing per company"),
    ("G-23", "marts.control_signals", "ICFR conclusion (the controls pillar)"),
    ("G3", "quali.proxy_sections", "DEF 14A governance sections"),
    ("G3", "marts.governance_metrics", "Management Risk inputs"),
    # Pre-existing, and the reason four gaps were filed in error.
    ("G-17", "ref.industry_group", "140 peer groups above SIC2 (already existed)"),
    ("G-17", "ref.sic_hierarchy", "SIC hierarchy (already existed)"),
    ("G-19", "ref.former_names", "name history (already existed)"),
    ("G-22", "marts.concentration", "vintage flags (already fixed)"),
    ("G-16", "ref.sic_naics", "SIC to NAICS (confirmed empty)"),
]


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    print(f"{'gap':<6} {'object':<34} {'rows':>13}  answers")
    print(f"{'-'*6} {'-'*34} {'-'*13}  {'-'*44}")
    missing = 0
    for gap, obj, what in BUILT:
        try:
            n = con.execute(f"SELECT count(*) FROM {obj}").fetchone()[0]
            print(f"{gap:<6} {obj:<34} {n:>13,}  {what}")
        except Exception:  # noqa: BLE001  absent is a finding, not an error
            missing += 1
            print(f"{gap:<6} {obj:<34} {'MISSING':>13}  {what}")
    print(f"\n{len(BUILT) - missing} of {len(BUILT)} objects present")

    print("\nInvariant suites guarding this work:")
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "transform"
    for f in sorted(root.glob("verify_*.py")):
        checks = f.read_text(encoding="utf-8").count('"""SELECT')
        print(f"  {f.name:<32} ~{checks} checks")


if __name__ == "__main__":
    main()
