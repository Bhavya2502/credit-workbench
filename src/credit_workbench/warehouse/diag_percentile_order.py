"""Why are 408 of 1,614,345 coverage cells' percentiles out of order?

`quantile_cont` cannot return a p10 above a p90 for well-behaved input, and the build
already filters `isfinite(value)`, so something about these particular cohorts breaks the
ordering. 408 cells is 0.025% and easy to wave away, but a percentile that is not monotone
means the interpolation was done over values it cannot represent, and any threshold cut
from those cells is meaningless rather than merely imprecise.

The likely cause is magnitude. A ratio with a near-zero denominator produces values in the
1e300 range, and interpolating between two of those overflows to infinity - which is
finite-checked as fine on input and compares as greater than everything on output. If that
is it, the fix is not a looser check but a bound on what counts as a usable ratio value.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

Q = [
    ("1. Which ratios and cohorts are affected?", """
        SELECT ratio, count(*) AS bad_cells,
               count(DISTINCT industry_code) AS industries,
               count(DISTINCT fy) AS years
        FROM marts.ratio_coverage
        WHERE p10 > p25 OR p25 > p50 OR p50 > p75 OR p75 > p90
        GROUP BY 1 ORDER BY bad_cells DESC LIMIT 15"""),

    ("2. What do the offending percentile sets look like?", """
        SELECT ratio, industry_scheme, industry_code, size_band, fy, basis,
               companies_with_value, p10, p25, p50, p75, p90, min_value, max_value
        FROM marts.ratio_coverage
        WHERE p10 > p25 OR p25 > p50 OR p50 > p75 OR p75 > p90
        ORDER BY companies_with_value DESC LIMIT 8"""),

    ("3. Is it magnitude? — how extreme do these cells get", """
        SELECT count(*) AS bad_cells,
               count(*) FILTER (WHERE abs(max_value) > 1e15) AS max_over_1e15,
               count(*) FILTER (WHERE abs(min_value) > 1e15) AS min_over_1e15,
               count(*) FILTER (WHERE isinf(p10) OR isinf(p25) OR isinf(p50)
                                   OR isinf(p75) OR isinf(p90)) AS any_infinite,
               count(*) FILTER (WHERE isnan(p10) OR isnan(p50) OR isnan(p90))
                   AS any_nan
        FROM marts.ratio_coverage
        WHERE p10 > p25 OR p25 > p50 OR p50 > p75 OR p75 > p90"""),

    ("4. And how extreme is marts.ratio_values in general?", """
        SELECT count(*) AS rows,
               count(*) FILTER (WHERE abs(value) > 1e6) AS over_1e6,
               count(*) FILTER (WHERE abs(value) > 1e12) AS over_1e12,
               count(*) FILTER (WHERE abs(value) > 1e15) AS over_1e15,
               max(abs(value)) AS biggest
        FROM marts.ratio_values WHERE value IS NOT NULL AND isfinite(value)"""),

    ("5. Which ratios carry the absurd magnitudes?", """
        SELECT ratio, count(*) AS rows_over_1e12,
               round(max(abs(value)), 0) AS biggest
        FROM marts.ratio_values
        WHERE value IS NOT NULL AND isfinite(value) AND abs(value) > 1e12
        GROUP BY 1 ORDER BY rows_over_1e12 DESC LIMIT 12"""),

    # If a bound on magnitude removes the disorder without removing meaningful data, that
    # is the fix. Measured before it is applied.
    ("6. Would bounding at 1e9 lose anything that matters?", """
        SELECT count(*) AS rows,
               count(*) FILTER (WHERE abs(value) > 1e9) AS would_drop,
               round(100.0 * count(*) FILTER (WHERE abs(value) > 1e9)
                     / count(*), 4) AS pct_dropped,
               count(DISTINCT ratio) FILTER (WHERE abs(value) > 1e9) AS ratios_affected
        FROM marts.ratio_values WHERE value IS NOT NULL AND isfinite(value)"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else
                                    (f"{v:.4g}" if isinstance(v, float) else str(v))))[:26]
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
