"""Find the missing tail of the operating-lease ladder by matching the arithmetic gap.

A complete ladder ties 90.6% of the time, so the tags and the pivot are both fine. The
failure is concentrated in 25,236 filings that report no year-five and no thereafter
rung, and whose ladder recovers a median 71.7% of the total they themselves report.
Roughly 28% of the obligation is therefore sitting in a bucket tagged with something we
do not capture.

Rather than guess at spellings: for each of those filings compute gap = total - ladder,
then look for a fact in the same filing whose value equals the gap. Whatever tag keeps
turning up IS the missing rung.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

# Filings that report an ASC 842 total and a first rung, but no tail at all.
FAILING = """
    WITH piv AS (
        SELECT adsh, period_end,
               max(value) FILTER (WHERE tag = 'LesseeOperatingLeaseLiabilityPaymentsDueNextTwelveMonths') AS y1,
               max(value) FILTER (WHERE tag = 'LesseeOperatingLeaseLiabilityPaymentsDueYearTwo')   AS y2,
               max(value) FILTER (WHERE tag = 'LesseeOperatingLeaseLiabilityPaymentsDueYearThree') AS y3,
               max(value) FILTER (WHERE tag = 'LesseeOperatingLeaseLiabilityPaymentsDueYearFour')  AS y4,
               max(value) FILTER (WHERE tag = 'LesseeOperatingLeaseLiabilityPaymentsDueYearFive')  AS y5,
               max(value) FILTER (WHERE tag = 'LesseeOperatingLeaseLiabilityPaymentsDueAfterYearFive') AS a5,
               max(value) FILTER (WHERE tag = 'LesseeOperatingLeaseLiabilityPaymentsDueAfterYearFour')  AS a4,
               max(value) FILTER (WHERE tag = 'LesseeOperatingLeaseLiabilityPaymentsDue') AS total
        FROM staging.facts_pit
        WHERE is_latest AND qtrs = 0 AND uom = 'USD'
          AND tag LIKE 'LesseeOperatingLeaseLiabilityPaymentsDue%'
        GROUP BY adsh, period_end),
    failing AS (
        SELECT adsh, period_end, total,
               total - (coalesce(y1,0)+coalesce(y2,0)+coalesce(y3,0)+coalesce(y4,0)) AS gap
        FROM piv
        WHERE total > 0 AND y1 IS NOT NULL
          AND y5 IS NULL AND a5 IS NULL AND a4 IS NULL)
"""

Q: list[tuple[str, str]] = [
    ("1. Which tag holds the missing amount? (value equals the gap, same filing)",
     FAILING + """
        SELECT c.tag, count(*) AS filings_where_value_equals_gap
        FROM failing f
        JOIN staging.facts_pit c
          ON c.adsh = f.adsh AND c.period_end = f.period_end
        WHERE c.is_latest AND c.qtrs = 0 AND c.uom = 'USD'
          AND f.gap > 0 AND abs(c.value - f.gap) <= 0.01 * f.gap
        GROUP BY 1 ORDER BY 2 DESC LIMIT 20"""),

    ("2. What lease tags do these filings carry at all?", FAILING + """
        SELECT c.tag, count(DISTINCT c.adsh) AS filings
        FROM failing f
        JOIN staging.facts_pit c
          ON c.adsh = f.adsh AND c.period_end = f.period_end
        WHERE c.is_latest AND c.qtrs = 0
          AND (c.tag ILIKE '%lease%' OR c.tag ILIKE '%rent%')
        GROUP BY 1 ORDER BY 2 DESC LIMIT 30"""),

    ("3. How big is the gap relative to the total?", FAILING + """
        SELECT count(*) AS filings,
               round(median(gap / total), 3) AS median_gap_share,
               count(*) FILTER (WHERE gap <= 0) AS no_gap
        FROM failing"""),

    ("4. Are these mostly interim filings using a remainder-of-year first rung?",
     FAILING + """
        SELECT s.form, count(DISTINCT f.adsh) AS filings
        FROM failing f JOIN raw.fsn_sub s ON s.adsh = f.adsh
        GROUP BY 1 ORDER BY 2 DESC LIMIT 10"""),
]


def show(con, query: str) -> None:
    cur = con.execute(query)
    headers = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,.4g}" if isinstance(v, float) else
                                    f"{v:,}" if isinstance(v, int) else str(v)))[:70]
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
