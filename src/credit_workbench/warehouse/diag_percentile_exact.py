"""The percentile disorder at full precision — is it float noise or wrong indexing?

Magnitude was the first hypothesis and it was wrong: no infinities, no NaN, nothing above
1e15, the largest ratio anywhere is 2.46e10. So the 408 disordered cells are not overflow.

Two candidates remain and they need opposite fixes. Either the differences are at the
sixteenth decimal place, in which case the check is wrong to test float equality exactly
after an interpolation, or `quantile_cont` with a list of probabilities does not return them
in the order given, in which case `q[1]..q[5]` are mislabelled and every percentile column
in the mart is wrong - which would matter enormously and be almost invisible, since the
values would all be plausible members of the right distribution.

So: the exact size of each violation, and a direct comparison between the list form and
five separate calls on the same cohort. The second settles the indexing question outright.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

Q = [
    ("1. How big are the violations, exactly?", """
        SELECT count(*) AS bad_cells,
               max(greatest(p10 - p25, p25 - p50, p50 - p75, p75 - p90)) AS worst_gap,
               min(greatest(p10 - p25, p25 - p50, p50 - p75, p75 - p90)) AS smallest_gap,
               count(*) FILTER (WHERE greatest(p10 - p25, p25 - p50,
                                              p50 - p75, p75 - p90) > 1e-9)
                   AS gap_over_1e_minus_9,
               count(*) FILTER (WHERE greatest(p10 - p25, p25 - p50,
                                              p50 - p75, p75 - p90) > 0.001)
                   AS gap_over_thousandth
        FROM marts.ratio_coverage
        WHERE p10 > p25 OR p25 > p50 OR p50 > p75 OR p75 > p90"""),

    ("2. Relative to the value itself, how big is the worst violation?", """
        SELECT ratio, industry_code, fy, companies_with_value,
               p10, p25, p10 - p25 AS gap,
               abs(p10 - p25) / nullif(greatest(abs(p10), abs(p25)), 0) AS relative_gap
        FROM marts.ratio_coverage
        WHERE p10 > p25
        ORDER BY abs(p10 - p25) DESC LIMIT 6"""),

    # The decisive test. If the list form and five separate calls agree, the indexing is
    # right and the disorder is float noise. If they disagree, every percentile column in
    # the mart is mislabelled.
    ("3. Does the list form agree with five separate calls?", """
        WITH one_cell AS (
            SELECT r.value
            FROM marts.ratio_values r
            LEFT JOIN ref.industry_group g ON g.sic4 = r.sic
            WHERE g.industry_code = '7389' AND r.fy = 2013
              AND r.basis = 'first_reported' AND r.ratio = 'working_capital_to_revenue'
              AND r.size_band = 'D over $10bn' AND r.value IS NOT NULL
        )
        SELECT quantile_cont(value, [0.10, 0.25, 0.50, 0.75, 0.90]) AS as_list,
               quantile_cont(value, 0.10) AS single_p10,
               quantile_cont(value, 0.25) AS single_p25,
               quantile_cont(value, 0.50) AS single_p50,
               quantile_cont(value, 0.75) AS single_p75,
               quantile_cont(value, 0.90) AS single_p90,
               count(*) AS rows_in_cell
        FROM one_cell"""),

    # If a company appears more than once per (cohort, ratio), the quantile is computed
    # over more rows than companies_with_value reports - worth knowing either way.
    ("4. Is marts.ratio_values unique per (cik, fy, basis, ratio)?", """
        SELECT count(*) AS rows,
               count(DISTINCT (cik, fy, basis, ratio)) AS distinct_keys,
               count(*) - count(DISTINCT (cik, fy, basis, ratio)) AS extra_rows
        FROM marts.ratio_values"""),

    ("5. Where a company repeats, why? (period_end within one fiscal year)", """
        SELECT cik, fy, basis, ratio, count(*) AS rows,
               string_agg(DISTINCT CAST(period_end AS VARCHAR), ', ') AS period_ends
        FROM marts.ratio_values
        GROUP BY 1, 2, 3, 4 HAVING count(*) > 1
        ORDER BY rows DESC LIMIT 6"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else
                                    (repr(v) if isinstance(v, float) else str(v))))[:46]
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
