"""Feasibility check for the adjustments engine (D2/D3/D4).

Two questions decide how this gets built:

1. Can each agency-style adjustment actually be computed from what we hold, and for
   how many companies? An adjustment we can only apply to 5% of the book is a
   footnote, not a feature.
2. Is there anything to validate against? Agency criteria documents sit behind
   registration walls, but companies tag their own non-GAAP measures in XBRL. If
   `AdjustedEBITDA` and friends are widely tagged, D4 has an external reference for
   thousands of companies instead of a handful.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

Q: list[tuple[str, str]] = [
    ("A. Input coverage for each adjustment (share of company-years with the input)", """
        WITH n AS (SELECT count(*) AS total FROM marts.adjustment_inputs
                   WHERE basis = 'latest' AND fy >= 2019)
        SELECT adjustment, have, total, round(100.0 * have / total, 1) AS pct
        FROM (
            SELECT 'Operating lease liability' AS adjustment,
                   count(op_lease_liability) AS have FROM marts.adjustment_inputs
                   WHERE basis='latest' AND fy>=2019
            UNION ALL SELECT 'Operating lease maturity ladder',
                   count(op_lease_due_y1) FROM marts.adjustment_inputs WHERE basis='latest' AND fy>=2019
            UNION ALL SELECT 'Operating lease discount rate',
                   count(op_lease_discount_rate) FROM marts.adjustment_inputs WHERE basis='latest' AND fy>=2019
            UNION ALL SELECT 'Operating lease cost (P&L)',
                   count(op_lease_cost) FROM marts.adjustment_inputs WHERE basis='latest' AND fy>=2019
            UNION ALL SELECT 'Finance lease liability',
                   count(fin_lease_liability) FROM marts.adjustment_inputs WHERE basis='latest' AND fy>=2019
            UNION ALL SELECT 'Pension obligation (PBO)',
                   count(pension_obligation) FROM marts.adjustment_inputs WHERE basis='latest' AND fy>=2019
            UNION ALL SELECT 'Pension plan assets',
                   count(pension_plan_assets) FROM marts.adjustment_inputs WHERE basis='latest' AND fy>=2019
            UNION ALL SELECT 'Pension service cost',
                   count(pension_service_cost) FROM marts.adjustment_inputs WHERE basis='latest' AND fy>=2019
            UNION ALL SELECT 'Pension interest cost',
                   count(pension_interest_cost) FROM marts.adjustment_inputs WHERE basis='latest' AND fy>=2019
            UNION ALL SELECT 'Debt maturity ladder',
                   count(debt_due_y1) FROM marts.adjustment_inputs WHERE basis='latest' AND fy>=2019
            UNION ALL SELECT 'Capitalised interest',
                   count(capitalised_interest) FROM marts.adjustment_inputs WHERE basis='latest' AND fy>=2019
            UNION ALL SELECT 'Guarantees (max exposure)',
                   count(guarantee_max_exposure) FROM marts.adjustment_inputs WHERE basis='latest' AND fy>=2019
            UNION ALL SELECT 'Loss contingency accrual',
                   count(loss_contingency_accrual) FROM marts.adjustment_inputs WHERE basis='latest' AND fy>=2019
            UNION ALL SELECT 'Purchase obligations',
                   count(purchase_obligation) FROM marts.adjustment_inputs WHERE basis='latest' AND fy>=2019
            UNION ALL SELECT 'Restructuring charge',
                   count(restructuring_charge) FROM marts.adjustment_inputs WHERE basis='latest' AND fy>=2019
            UNION ALL SELECT 'Goodwill impairment',
                   count(impairment_goodwill) FROM marts.adjustment_inputs WHERE basis='latest' AND fy>=2019
        ) CROSS JOIN n ORDER BY pct DESC"""),

    ("B. VALIDATION SOURCE — do companies tag their own non-GAAP measures?", """
        SELECT tag, count(DISTINCT cik) AS companies, count(*) AS facts,
               max(period_end) AS latest
        FROM staging.facts_pit
        WHERE is_latest AND uom = 'USD'
          AND (tag ILIKE '%AdjustedEbitda%' OR tag ILIKE '%AdjustedOperatingIncome%'
               OR tag ILIKE '%NetDebt%' OR tag ILIKE '%AdjustedNetIncome%'
               OR tag ILIKE '%FundsFromOperations%')
        GROUP BY 1 ORDER BY companies DESC LIMIT 15"""),

    ("C. Preferred stock and hybrids — how often equity credit is even a question", """
        SELECT count(*) AS company_years,
               count(*) FILTER (WHERE preferred_equity > 0) AS with_preferred,
               round(100.0 * count(*) FILTER (WHERE preferred_equity > 0) / count(*), 1)
                   AS pct_with_preferred,
               count(*) FILTER (WHERE minority_interest_bs > 0) AS with_minority
        FROM marts.spreads_a WHERE basis = 'latest' AND fy >= 2019 AND is_primary_annual"""),

    ("D. Materiality — how much would lease capitalisation move leverage?", """
        SELECT round(median(total_debt_incl_leases / nullif(total_debt, 0)), 3)
                   AS median_debt_uplift_multiple,
               round(quantile_cont(total_debt_incl_leases / nullif(total_debt, 0), 0.9), 3)
                   AS p90_uplift,
               count(*) AS company_years
        FROM marts.spreads_a
        WHERE basis = 'latest' AND fy >= 2019 AND total_debt > 0
          AND total_debt_incl_leases IS NOT NULL"""),
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
    for title, query in Q:
        print(f"\n### {title}")
        try:
            show(con, query)
        except Exception as exc:  # noqa: BLE001
            print(f"  (failed: {exc})")


if __name__ == "__main__":
    main()
