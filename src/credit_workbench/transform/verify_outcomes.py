"""Verification for the credit-outcome target.

The decisive test of a target variable is whether weak credits actually experience
more of the outcome than strong ones. If the event rate does not rise as leverage
rises and coverage falls, either the target or the ratios are wrong, and no amount of
modelling will rescue it.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

CHECKS: list[tuple[str, str]] = [
    ("Base rates — how often the outcome actually occurs", """
        SELECT count(*) AS company_years,
               round(100.0 * count(*) FILTER (WHERE distress_12m) / count(*), 2) AS pct_distress_12m,
               round(100.0 * count(*) FILTER (WHERE distress_24m) / count(*), 2) AS pct_distress_24m,
               round(100.0 * count(*) FILTER (WHERE default_12m)  / count(*), 2) AS pct_default_12m,
               round(100.0 * count(*) FILTER (WHERE default_24m)  / count(*), 2) AS pct_default_24m,
               round(100.0 * count(*) FILTER (WHERE bankruptcy_24m) / count(*), 2) AS pct_bankruptcy_24m,
               round(100.0 * count(*) FILTER (WHERE delisting_24m) / count(*), 2) AS pct_delisting_24m,
               round(100.0 * count(*) FILTER (WHERE adverse_delisting_24m) / count(*), 2)
                   AS pct_adverse_delisting_24m
        FROM marts.credit_outcomes"""),

    ("Event mix — what actually happens first", """
        SELECT first_event_category, count(*) AS company_years,
               round(median(days_to_first_event)) AS median_days_after_filing
        FROM marts.credit_outcomes WHERE first_event_category IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC"""),

    ("Base rate by year — does it move with the cycle?", """
        SELECT fy, count(*) AS company_years,
               round(100.0 * count(*) FILTER (WHERE distress_12m) / count(*), 1) AS pct_distress_12m,
               round(100.0 * count(*) FILTER (WHERE default_24m) / count(*), 2) AS pct_default_24m
        FROM marts.credit_outcomes WHERE fy BETWEEN 2007 AND 2024
        GROUP BY 1 ORDER BY 1"""),

    ("DISCRIMINATION TEST — distress rate by leverage quintile", """
        SELECT quintile,
               count(*) AS company_years,
               round(avg(debt_to_ebitda), 2) AS avg_debt_to_ebitda,
               round(100.0 * count(*) FILTER (WHERE distress_12m) / count(*), 1) AS pct_distress_12m,
               round(100.0 * count(*) FILTER (WHERE default_24m) / count(*), 2) AS pct_default_24m
        FROM (SELECT *, ntile(5) OVER (ORDER BY debt_to_ebitda) AS quintile
              FROM marts.model_dataset WHERE debt_to_ebitda IS NOT NULL)
        GROUP BY 1 ORDER BY 1"""),

    ("DISCRIMINATION TEST — distress rate by interest cover quintile", """
        SELECT quintile,
               count(*) AS company_years,
               round(avg(ebitda_interest_cover), 2) AS avg_interest_cover,
               round(100.0 * count(*) FILTER (WHERE distress_12m) / count(*), 1) AS pct_distress_12m,
               round(100.0 * count(*) FILTER (WHERE default_24m) / count(*), 2) AS pct_default_24m
        FROM (SELECT *, ntile(5) OVER (ORDER BY ebitda_interest_cover) AS quintile
              FROM marts.model_dataset WHERE ebitda_interest_cover IS NOT NULL)
        GROUP BY 1 ORDER BY 1"""),

    ("DISCRIMINATION TEST — distress rate by distress flag", """
        SELECT 'ebitda_negative' AS flag,
               round(100.0 * count(*) FILTER (WHERE distress_12m AND ebitda_negative)
                     / nullif(count(*) FILTER (WHERE ebitda_negative), 0), 1) AS pct_when_flagged,
               round(100.0 * count(*) FILTER (WHERE distress_12m AND NOT ebitda_negative)
                     / nullif(count(*) FILTER (WHERE NOT ebitda_negative), 0), 1) AS pct_when_not
        FROM marts.model_dataset
        UNION ALL SELECT 'equity_negative',
               round(100.0 * count(*) FILTER (WHERE distress_12m AND equity_negative)
                     / nullif(count(*) FILTER (WHERE equity_negative), 0), 1),
               round(100.0 * count(*) FILTER (WHERE distress_12m AND NOT equity_negative)
                     / nullif(count(*) FILTER (WHERE NOT equity_negative), 0), 1)
        FROM marts.model_dataset
        UNION ALL SELECT 'interest_uncovered',
               round(100.0 * count(*) FILTER (WHERE distress_12m AND interest_uncovered)
                     / nullif(count(*) FILTER (WHERE interest_uncovered), 0), 1),
               round(100.0 * count(*) FILTER (WHERE distress_12m AND NOT interest_uncovered)
                     / nullif(count(*) FILTER (WHERE NOT interest_uncovered), 0), 1)
        FROM marts.model_dataset"""),

    ("Model dataset readiness", """
        SELECT count(*) AS rows, count(DISTINCT cik) AS companies,
               min(fy) AS from_fy, max(fy) AS to_fy,
               count(*) FILTER (WHERE distress_12m) AS positive_cases_12m,
               count(*) FILTER (WHERE default_24m) AS default_cases_24m
        FROM marts.model_dataset"""),
]


def show(con, query: str) -> None:
    cur = con.execute(query)
    headers = [d[0] for d in cur.description]
    rows = [["" if v is None else (f"{v:,.4g}" if isinstance(v, float) else str(v))
             for v in r] for r in cur.fetchall()]
    if not rows:
        print("  (no rows)")
        return
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    print("  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        print("  " + "  ".join(v.ljust(w) for v, w in zip(r, widths)))


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    for title, query in CHECKS:
        print(f"\n### {title}")
        try:
            show(con, query)
        except Exception as exc:  # noqa: BLE001
            print(f"  (check failed: {exc})")


if __name__ == "__main__":
    main()
