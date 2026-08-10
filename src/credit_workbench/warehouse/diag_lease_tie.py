"""Why does the operating-lease ladder not sum to the total the filer reports?

Adding the missing rungs moved the tie rate from 49.5% to only 52.3%, so the rung
vocabulary was a real but minor cause and something larger is still wrong. Two
candidates, and they call for different fixes:

  tag semantics   the ladder genuinely does not sum to `LesseeOperatingLeaseLiabilityPaymentsDue`
                  for many filers, because that tag means something else to them
  the D1 pivot    `marts.adjustment_inputs` keys on (cik, basis, period_end) and the
                  dedupe behind it keys on (cik, basis, period_end, qtrs, col), never on
                  adsh - so rungs of one ladder could be drawn from different filings

Rebuilding the ladder from facts_pit keyed on adsh separates the two: if it ties within
a single filing, the pivot is at fault, not the data.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

RUNGS = {
    "y1": "NextTwelveMonths", "y2": "YearTwo", "y3": "YearThree",
    "y4": "YearFour", "y5": "YearFive", "after5": "AfterYearFive",
    "after4": "AfterYearFour",
}
PIV = ",\n                   ".join(
    f"max(value) FILTER (WHERE tag = 'LesseeOperatingLeaseLiabilityPaymentsDue{suffix}')"
    f" AS {col}" for col, suffix in RUNGS.items())

BASE = f"""
    WITH piv AS (
        SELECT adsh, period_end, any_value(fy) AS fy,
               {PIV},
               max(value) FILTER (
                   WHERE tag = 'LesseeOperatingLeaseLiabilityPaymentsDue') AS total,
               max(value) FILTER (
                   WHERE tag = 'OperatingLeaseLiability') AS discounted_liability
        FROM staging.facts_pit
        WHERE is_latest AND qtrs = 0 AND uom = 'USD'
          AND (tag LIKE 'LesseeOperatingLeaseLiabilityPaymentsDue%'
               OR tag = 'OperatingLeaseLiability')
        GROUP BY adsh, period_end),
    lad AS (
        SELECT *, coalesce(y1,0)+coalesce(y2,0)+coalesce(y3,0)+coalesce(y4,0)
                  +coalesce(y5,0)+coalesce(after5,0)+coalesce(after4,0) AS ladder
        FROM piv WHERE total > 0 AND y1 IS NOT NULL)
"""

Q: list[tuple[str, str]] = [
    ("1. THE TEST — does the ladder tie within a single filing?", BASE + """
        SELECT count(*) AS compared,
               round(100.0 * count(*) FILTER (
                   WHERE abs(ladder - total) <= 0.02 * total) / count(*), 1) AS pct_tie
        FROM lad"""),

    ("2. Shape of the failure — where does the ladder land against the total?", BASE + """
        SELECT CASE WHEN ladder > total * 1.5   THEN 'f. more than 1.5x total'
                    WHEN ladder > total * 1.02  THEN 'e. above total'
                    WHEN ladder >= total * 0.98 THEN 'd. TIES'
                    WHEN ladder >= total * 0.7  THEN 'c. 70-98% of total'
                    WHEN ladder >= total * 0.3  THEN 'b. 30-70% of total'
                    ELSE 'a. under 30% of total' END AS band,
               count(*) AS filings,
               round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
        FROM lad GROUP BY 1 ORDER BY 1"""),

    ("3. Is the shortfall explained by a missing tail? (rungs present vs tie)", BASE + """
        SELECT (y5 IS NOT NULL) AS has_y5, (after5 IS NOT NULL) AS has_after5,
               (after4 IS NOT NULL) AS has_after4, count(*) AS filings,
               round(100.0 * count(*) FILTER (
                   WHERE abs(ladder - total) <= 0.02 * total) / count(*), 1) AS pct_tie,
               round(median(ladder / total), 3) AS median_ratio
        FROM lad GROUP BY 1, 2, 3 ORDER BY filings DESC"""),

    ("4. Does the undiscounted total behave like a total? (must exceed discounted)", BASE + """
        SELECT count(*) AS compared,
               round(100.0 * count(*) FILTER (WHERE total >= discounted_liability)
                     / count(*), 1) AS pct_total_ge_liability,
               round(median(total / nullif(discounted_liability, 0)), 3) AS median_ratio
        FROM lad WHERE discounted_liability > 0"""),

    ("5. Same question for the D1 mart, to compare against the per-filing answer", """
        SELECT count(*) AS compared,
               round(100.0 * count(*) FILTER (
                   WHERE abs(ladder - total) <= 0.02 * total) / count(*), 1) AS pct_tie
        FROM (SELECT coalesce(op_lease_due_y1,0)+coalesce(op_lease_due_y2,0)
                     +coalesce(op_lease_due_y3,0)+coalesce(op_lease_due_y4,0)
                     +coalesce(op_lease_due_y5,0)+coalesce(op_lease_due_thereafter,0)
                     AS ladder, op_lease_undiscounted_total AS total
              FROM marts.adjustment_inputs
              WHERE basis = 'latest' AND op_lease_undiscounted_total > 0
                AND op_lease_due_y1 IS NOT NULL)"""),

    ("6. Do multiple filings report the same cik+period_end? (pivot mixing risk)", """
        SELECT count(*) AS cik_period_ends,
               round(100.0 * count(*) FILTER (WHERE filings > 1) / count(*), 1)
                   AS pct_with_multiple_filings
        FROM (SELECT cik, period_end, count(DISTINCT adsh) AS filings
              FROM staging.facts_pit
              WHERE is_latest AND qtrs = 0
                AND tag = 'LesseeOperatingLeaseLiabilityPaymentsDueNextTwelveMonths'
              GROUP BY 1, 2)"""),
]


def show(con, query: str) -> None:
    cur = con.execute(query)
    headers = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,.4g}" if isinstance(v, float) else
                                    f"{v:,}" if isinstance(v, int) else str(v)))[:64]
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
