"""Why do fair-value levels not always sum to the reported total?

Restricting to facts where the hierarchy is the only axis lifted the tie from 43% to
62%, so the cross-tabulation was a real cause but not the whole one. Before relaxing
the threshold, establish the direction and the concentration of the residual: a sum
that overshoots the total points at netting and collateral offsets, which are a genuine
feature of derivative disclosure rather than a defect; a sum that falls short points at
a level we are not capturing.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

BASE = """
    WITH lv AS (
        SELECT cik, period_end, tag,
               sum(value) FILTER (
                   WHERE hierarchy_level IN ('Level 1', 'Level 2', 'Level 3')) AS levels_sum
        FROM marts.fair_value_hierarchy
        WHERE uom = 'USD' AND qtrs = 0 AND dimension_count = 1
        GROUP BY 1, 2, 3),
    tot AS (
        SELECT cik, period_end, tag, value AS total
        FROM staging.facts_pit WHERE is_latest AND uom = 'USD' AND qtrs = 0),
    cmp AS (
        SELECT l.cik, l.period_end, l.tag, l.levels_sum, t.total
        FROM lv l JOIN tot t USING (cik, period_end, tag)
        WHERE l.levels_sum IS NOT NULL AND abs(t.total) > 1e6)
"""

Q: list[tuple[str, str]] = [
    ("1. Direction of the miss", BASE + """
        SELECT CASE WHEN levels_sum > total * 1.02 THEN 'levels exceed the total'
                    WHEN levels_sum < total * 0.98 THEN 'levels fall short'
                    ELSE 'ties' END AS direction,
               count(*) AS rows,
               round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct,
               round(median(levels_sum / nullif(total, 0)), 3) AS median_ratio
        FROM cmp GROUP BY 1 ORDER BY rows DESC"""),

    ("2. Which tags tie, and which do not", BASE + """
        SELECT tag, count(*) AS rows,
               round(100.0 * count(*) FILTER (
                   WHERE abs(levels_sum - total) <= 0.02 * abs(total)) / count(*), 1)
                   AS pct_tie,
               round(median(levels_sum / nullif(total, 0)), 3) AS median_ratio
        FROM cmp GROUP BY 1 HAVING count(*) >= 2000
        ORDER BY rows DESC LIMIT 20"""),

    ("3. Do the non-tying filings carry a netting or NAV member?", BASE + """
        SELECT c.tag,
               count(*) FILTER (WHERE h.has_other) AS with_other_member,
               count(*) AS rows
        FROM cmp c
        LEFT JOIN (
            SELECT cik, period_end, tag, true AS has_other
            FROM marts.fair_value_hierarchy
            WHERE hierarchy_level IN ('NAV practical expedient', 'Combined or other')
            GROUP BY 1, 2, 3) h
          ON h.cik = c.cik AND h.period_end = c.period_end AND h.tag = c.tag
        WHERE abs(c.levels_sum - c.total) > 0.02 * abs(c.total)
        GROUP BY 1 ORDER BY rows DESC LIMIT 12"""),
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
