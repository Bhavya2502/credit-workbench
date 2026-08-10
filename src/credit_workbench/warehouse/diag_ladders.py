"""Do maturity-ladder rungs share one period, or scatter across future years?

The tag probe showed intangible-amortisation ladder facts carrying period years out to
2028 — a "year five" bucket dated at the year it falls in rather than at the balance
sheet date. That matters: `marts.adjustment_inputs` pivots on (cik, basis, period_end),
so rungs dated differently land on different rows and the ladder never sums. The
operating-lease and debt ladders are already in D1, so they can answer whether this is
a real problem or an artefact of a minority of filers.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

LADDERS = {
    "intangible amortisation": "FiniteLivedIntangibleAssetsAmortizationExpense%",
    "finance lease":           "FinanceLeaseLiabilityPaymentsDue%",
    "operating lease":         "LesseeOperatingLeaseLiabilityPaymentsDue%",
    "debt maturities":         "LongTermDebtMaturitiesRepaymentsOfPrincipal%",
}

Q: list[tuple[str, str]] = [
    ("1. Within one filing, do the rungs of a ladder share a single period_end?", " UNION ALL ".join(f"""
        SELECT '{name}' AS ladder, round(avg(dates), 2) AS avg_distinct_dates,
               round(100.0 * count(*) FILTER (WHERE dates = 1) / count(*), 1) AS pct_single_date,
               count(*) AS filings
        FROM (SELECT adsh, count(DISTINCT period_end) AS dates
              FROM staging.facts_pit
              WHERE is_latest AND tag LIKE '{pat}' AND period_year >= 2020
              GROUP BY adsh HAVING count(DISTINCT tag) >= 3)""" for name, pat in LADDERS.items())),

    ("2. Precedent: does the operating-lease ladder already in D1 sum to its total?", """
        SELECT count(*) AS compared,
               round(100.0 * count(*) FILTER (
                   WHERE abs(ladder - op_lease_undiscounted_total)
                         <= 0.02 * op_lease_undiscounted_total) / count(*), 1) AS pct_tie
        FROM (SELECT coalesce(op_lease_due_y1, 0) + coalesce(op_lease_due_y2, 0)
                     + coalesce(op_lease_due_y3, 0) + coalesce(op_lease_due_y4, 0)
                     + coalesce(op_lease_due_y5, 0)
                     + coalesce(op_lease_due_thereafter, 0) AS ladder,
                     op_lease_undiscounted_total
              FROM marts.adjustment_inputs
              WHERE basis = 'latest' AND op_lease_undiscounted_total > 0
                AND op_lease_due_y1 IS NOT NULL)"""),

    ("3. Precedent: how complete is the debt ladder on a single row?", """
        SELECT count(*) AS rows_with_y1,
               round(100.0 * count(*) FILTER (WHERE debt_due_y5 IS NOT NULL)
                     / count(*), 1) AS pct_also_y5
        FROM marts.adjustment_inputs
        WHERE basis = 'latest' AND debt_due_y1 IS NOT NULL"""),

    ("4. If rungs do scatter, how far ahead of the filing do they sit?", """
        SELECT tag, count(*) AS facts,
               round(avg(period_year - fy), 2) AS avg_years_ahead_of_fiscal_year
        FROM staging.facts_pit
        WHERE is_latest AND period_year >= 2020 AND fy IS NOT NULL
          AND tag LIKE 'FiniteLivedIntangibleAssetsAmortizationExpense%'
        GROUP BY 1 ORDER BY 1"""),

    # Did adding the AfterYearFour and Rolling rungs actually help? Both lease ladders
    # publish their own undiscounted total, so they can be checked against themselves.
    ("6. Post-fix: do the ladders tie to the totals filers report?", """
        SELECT 'operating lease' AS ladder, count(*) AS compared,
               round(100.0 * count(*) FILTER (
                   WHERE abs(ladder - total) <= 0.02 * total) / count(*), 1) AS pct_tie
        FROM (SELECT coalesce(op_lease_due_y1, 0) + coalesce(op_lease_due_y2, 0)
                     + coalesce(op_lease_due_y3, 0) + coalesce(op_lease_due_y4, 0)
                     + coalesce(op_lease_due_y5, 0) + coalesce(op_lease_due_thereafter, 0)
                     + coalesce(op_lease_due_remainder_fy, 0) AS ladder, op_lease_undiscounted_total AS total
              FROM marts.adjustment_inputs
              WHERE basis = 'latest' AND op_lease_undiscounted_total > 0
                AND op_lease_due_y1 IS NOT NULL)
        UNION ALL
        SELECT 'finance lease', count(*),
               round(100.0 * count(*) FILTER (
                   WHERE abs(ladder - total) <= 0.02 * total) / count(*), 1)
        FROM (SELECT coalesce(fin_lease_due_y1, 0) + coalesce(fin_lease_due_y2, 0)
                     + coalesce(fin_lease_due_y3, 0) + coalesce(fin_lease_due_y4, 0)
                     + coalesce(fin_lease_due_y5, 0) + coalesce(fin_lease_due_thereafter, 0)
                     + coalesce(fin_lease_due_remainder_fy, 0) AS ladder, fin_lease_undiscounted_total AS total
              FROM marts.adjustment_inputs
              WHERE basis = 'latest' AND fin_lease_undiscounted_total > 0
                AND fin_lease_due_y1 IS NOT NULL)"""),

    ("7. Post-fix: intangible ladder against net carrying value (an identity)", """
        SELECT count(*) AS compared,
               round(100.0 * count(*) FILTER (
                   WHERE abs(ladder - net_carrying) <= 0.05 * net_carrying) / count(*), 1)
                   AS pct_tie,
               round(100.0 * count(*) FILTER (WHERE ladder > net_carrying * 1.05)
                     / count(*), 1) AS pct_ladder_too_high
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
                AND intangible_amort_thereafter IS NOT NULL)"""),

    ("8. Coverage gained: company-periods now carrying each ladder", """
        SELECT count(*) FILTER (WHERE op_lease_due_y1 IS NOT NULL)   AS op_lease,
               count(*) FILTER (WHERE fin_lease_due_y1 IS NOT NULL)  AS fin_lease,
               count(*) FILTER (WHERE debt_due_y1 IS NOT NULL)       AS debt,
               count(*) FILTER (WHERE intangible_amort_y1 IS NOT NULL) AS intangible,
               count(*) FILTER (WHERE ppe_gross IS NOT NULL)         AS ppe_gross
        FROM marts.adjustment_inputs WHERE basis = 'latest'"""),

    # The op-lease ladder tied only half the time, so the rung vocabulary is wider than
    # what D1 mapped. List the rungs filers actually use, most-used first.
    ("5. The real rung vocabulary of each ladder", " UNION ALL ".join(f"""
        (SELECT '{name}' AS ladder, tag, count(DISTINCT adsh) AS filings,
                (m.tag IS NOT NULL) AS mapped_in_d1
         FROM staging.facts_pit f
         LEFT JOIN (SELECT DISTINCT source_tag AS tag FROM staging.note_inputs) m USING (tag)
         WHERE f.is_latest AND f.tag LIKE '{pat}' AND f.period_year >= 2021
         GROUP BY tag, m.tag
         ORDER BY filings DESC LIMIT 14)""" for name, pat in LADDERS.items())),
]


def show(con, query: str) -> None:
    cur = con.execute(query)
    headers = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,.4g}" if isinstance(v, float) else str(v)))[:70]
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
    for title, query in Q:
        print(f"\n### {title}")
        try:
            show(con, query)
        except Exception as exc:  # noqa: BLE001
            print(f"  (failed: {exc})")


if __name__ == "__main__":
    main()
