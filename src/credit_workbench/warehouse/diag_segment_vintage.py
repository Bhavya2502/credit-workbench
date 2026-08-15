"""marts.segments has no point-in-time flags. Confirm what that costs a naive sum."""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

Q = [
    ("Does marts.segments carry vintage flags?", """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'marts' AND table_name = 'segments'
          AND column_name IN ('is_latest', 'is_first_report', 'filed', 'adsh')
        ORDER BY column_name"""),

    ("Intel FY2024 Client Computing revenue - naive sum vs one vintage", """
        WITH raw AS (
            SELECT adsh, filed, period_end, qtrs, value
            FROM marts.segments
            WHERE TRY_CAST(cik AS BIGINT) = 50863 AND fy = 2024
              AND member = 'ClientComputingGroup' AND uom = 'USD' AND qtrs = 4
              AND tag = 'RevenueFromContractWithCustomerExcludingAssessedTax')
        SELECT count(*) AS rows_returned,
               count(DISTINCT adsh) AS filings,
               count(DISTINCT period_end) AS periods,
               round(sum(value) / 1e9, 2) AS naive_sum_bn,
               round(max(value) / 1e9, 2) AS single_figure_bn"""),

    ("The same figure, deduplicated to one filing per period", """
        SELECT period_end, round(value / 1e9, 2) AS usd_bn, filed, adsh
        FROM (
            SELECT period_end, value, filed, adsh,
                   row_number() OVER (PARTITION BY period_end
                                      ORDER BY filed DESC) AS rn
            FROM marts.segments
            WHERE TRY_CAST(cik AS BIGINT) = 50863 AND fy = 2024
              AND member = 'ClientComputingGroup' AND uom = 'USD' AND qtrs = 4
              AND tag = 'RevenueFromContractWithCustomerExcludingAssessedTax')
        WHERE rn = 1 ORDER BY period_end"""),

    ("How many companies genuinely report segments, by size", """
        SELECT r.size_band,
               count(DISTINCT r.cik) AS companies,
               count(DISTINCT s.cik) AS with_segments,
               round(100.0 * count(DISTINCT s.cik) / count(DISTINCT r.cik), 1) AS pct
        FROM marts.ratio_values r
        LEFT JOIN (SELECT DISTINCT cik, fy FROM marts.segments) s
               ON s.cik = r.cik AND s.fy = r.fy
        WHERE r.fy = 2024 AND r.basis = 'latest' AND r.size_band IS NOT NULL
        GROUP BY 1 ORDER BY 1"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)))[:46]
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
            print(f"  (failed: {str(exc)[:180]})")


if __name__ == "__main__":
    main()
