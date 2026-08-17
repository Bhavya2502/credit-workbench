"""What can G-04 actually compute? — inputs for agency-style adjusted metrics

The request is for adjusted debt, EBITDAR, adjusted leverage, FFO and adjusted coverage,
with the adjustment policy exposed as parameters rather than baked in. `marts.adjustment_inputs`
holds the lease and pension ingredients, but the *base* figures - debt, EBITDA, interest,
rent - come from `marts.spread_lines`, and its `line_code` vocabulary has never been read in
this workstream. Writing the arithmetic against guessed line codes would fail at the end of
a long build, and worse, a wrongly-named code that happens to exist would compute a
plausible number from the wrong input.

So: the line codes actually present, how well each is populated, and whether the specific
inputs each adjustment needs are there often enough to be worth publishing. An adjusted
leverage that computes for 8% of companies is not a metric, it is a footnote.

The rent question decides the whole lease adjustment. Under ASC 842 the operating lease
liability is on the balance sheet and adjusted debt barely needs a lease estimate at all;
under ASC 840 it was off balance sheet and the multiple-of-rent convention was the only
route. Both eras are in this warehouse, which is what G-05 is about - so the coverage of
`op_lease_liability` against `op_lease_840_rent_expense` by year tells us where the splice
has to sit.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

Q = [
    ("1. The spread line vocabulary — what codes exist, by statement", """
        SELECT statement, count(DISTINCT line_code) AS codes,
               count(*) AS rows
        FROM marts.spread_lines GROUP BY 1 ORDER BY rows DESC"""),

    ("2. Codes that look like the ones the adjustments need", """
        SELECT line_code, any_value(label) AS example_label, statement,
               count(*) AS rows, count(DISTINCT cik) AS companies
        FROM marts.spread_lines
        WHERE lower(line_code) LIKE '%ebitda%' OR lower(line_code) LIKE '%ebit%'
           OR lower(line_code) LIKE '%debt%' OR lower(line_code) LIKE '%interest%'
           OR lower(line_code) LIKE '%rent%' OR lower(line_code) LIKE '%lease%'
           OR lower(line_code) LIKE '%operating_income%'
           OR lower(line_code) LIKE '%depreciation%' OR lower(line_code) LIKE '%cfo%'
           OR lower(line_code) LIKE '%cash_from%' OR lower(line_code) LIKE '%tax%'
        GROUP BY line_code, statement ORDER BY companies DESC LIMIT 40"""),

    ("3. How well populated are they for a recent year?", """
        SELECT line_code, count(DISTINCT cik) AS companies_2024
        FROM marts.spread_lines
        WHERE fy = 2024 AND basis = 'first_reported' AND value IS NOT NULL
        GROUP BY 1 ORDER BY companies_2024 DESC LIMIT 30"""),

    # The splice question. If op_lease_liability is well populated post-2019 and
    # op_lease_840_rent_expense pre-2019, the boundary is clean and G-05 is a documented
    # coalesce rather than an estimation problem.
    ("4. G-05 — where does the 840/842 boundary actually sit?", """
        SELECT fy,
               count(*) AS rows,
               count(op_lease_liability) AS has_842_liability,
               count(op_lease_840_rent_expense) AS has_840_rent,
               count(op_lease_840_total) AS has_840_ladder_total,
               count(op_lease_discount_rate) AS has_discount_rate
        FROM marts.adjustment_inputs
        WHERE basis = 'first_reported' AND fy BETWEEN 2014 AND 2025
        GROUP BY 1 ORDER BY 1"""),

    ("5. Do the two eras overlap, or is the handover clean?", """
        SELECT count(*) AS rows,
               count(*) FILTER (WHERE op_lease_liability IS NOT NULL
                                  AND op_lease_840_rent_expense IS NOT NULL) AS both,
               count(*) FILTER (WHERE op_lease_liability IS NOT NULL
                                  AND op_lease_840_rent_expense IS NULL) AS only_842,
               count(*) FILTER (WHERE op_lease_liability IS NULL
                                  AND op_lease_840_rent_expense IS NOT NULL) AS only_840,
               count(*) FILTER (WHERE op_lease_liability IS NULL
                                  AND op_lease_840_rent_expense IS NULL) AS neither
        FROM marts.adjustment_inputs WHERE basis = 'first_reported'"""),

    ("6. Pension — is there enough to adjust for?", """
        SELECT count(*) AS rows,
               count(pension_benefit_obligation) AS has_pbo,
               count(pension_plan_assets) AS has_assets,
               count(pension_service_cost) AS has_service_cost,
               count(pension_interest_cost) AS has_interest_cost
        FROM marts.adjustment_inputs WHERE basis = 'first_reported'"""),

    ("7. The pension columns that actually exist", """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'marts' AND table_name = 'adjustment_inputs'
          AND column_name LIKE '%pension%' ORDER BY column_name"""),

    # An adjusted metric is only worth publishing if the base and the adjustment are both
    # present for the same company-year. This is the joint coverage.
    ("8. Joint coverage — spread_lines and adjustment_inputs on the same company-year", """
        SELECT count(DISTINCT s.cik) AS in_spreads,
               count(DISTINCT a.cik) AS also_in_adjustments
        FROM (SELECT DISTINCT cik, fy FROM marts.spread_lines
              WHERE fy = 2024 AND basis = 'first_reported') s
        LEFT JOIN (SELECT DISTINCT cik, fy FROM marts.adjustment_inputs
                   WHERE fy = 2024 AND basis = 'first_reported') a
          ON a.cik = s.cik AND a.fy = s.fy"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else
                                    (f"{v:.4g}" if isinstance(v, float) else str(v))))[:44]
             for v in r] for r in cur.fetchall()]
    if not rows:
        print("  (no rows)")
        return
    w = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(heads)]
    print("  " + "  ".join(h.ljust(x) for h, x in zip(heads, w)))
    print("  " + "  ".join("-" * x for x in w))
    for r in rows:
        print("  " + "  ".join(v.ljust(x) for v, x in zip(r, w)))


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    for title, q in Q:
        print(f"\n### {title}")
        try:
            show(con, q)
        except Exception as exc:
            print(f"  (failed: {str(exc)[:170]})")


if __name__ == "__main__":
    main()
