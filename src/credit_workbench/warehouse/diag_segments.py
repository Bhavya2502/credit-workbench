"""Is segment data patchy, or was it queried with the wrong cik format?

The other session reports Intel returning no segment rows. Intel is CIK 50863, and
several tables store cik zero-padded to ten characters, so a query using the bare
integer silently matches nothing. Test the claim before treating it as a data gap.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

Q = [
    ("1. Intel by every plausible cik format", """
        SELECT 'padded string 0000050863' AS tried, count(*) AS rows FROM marts.segments
          WHERE cik = '0000050863'
        UNION ALL SELECT 'bare string 50863', count(*) FROM marts.segments
          WHERE cik = '50863'
        UNION ALL SELECT 'integer cast', count(*) FROM marts.segments
          WHERE TRY_CAST(cik AS BIGINT) = 50863
        UNION ALL SELECT 'name match', count(*) FROM marts.segments
          WHERE company_name ILIKE '%INTEL CORP%'"""),

    ("2. How cik is actually stored, per table", """
        SELECT 'marts.segments' AS tbl, any_value(cik) AS example, max(length(cik)) AS len
          FROM marts.segments
        UNION ALL SELECT 'staging.facts_pit', any_value(cik), max(length(cik))
          FROM staging.facts_pit
        UNION ALL SELECT 'marts.ratio_values', any_value(cik), max(length(cik))
          FROM marts.ratio_values
        UNION ALL SELECT 'marts.covenant_terms', any_value(cik), max(length(cik))
          FROM marts.covenant_terms
        UNION ALL SELECT 'quali.filing_sections', any_value(cik), max(length(cik))
          FROM quali.filing_sections"""),

    ("3. Intel segment rows, by axis and year", """
        SELECT axis, count(*) AS rows, count(DISTINCT member) AS members,
               min(fy) AS first_fy, max(fy) AS last_fy
        FROM marts.segments WHERE TRY_CAST(cik AS BIGINT) = 50863
        GROUP BY 1 ORDER BY rows DESC"""),

    ("4. Intel's actual reported segments, latest year", """
        SELECT member, tag, uom, round(sum(value)/1e9, 2) AS usd_bn
        FROM marts.segments
        WHERE TRY_CAST(cik AS BIGINT) = 50863 AND fy = 2024
          AND axis IN ('BusinessSegments', 'StatementBusinessSegments')
          AND tag IN ('Revenues', 'RevenueFromContractWithCustomerExcludingAssessedTax')
        GROUP BY 1, 2, 3 ORDER BY usd_bn DESC LIMIT 15"""),

    ("5. Segment coverage overall - is it patchy?", """
        SELECT count(DISTINCT cik) AS companies_with_segments,
               count(DISTINCT fy) AS years,
               count(*) AS rows
        FROM marts.segments"""),

    ("6. Coverage by year, against companies filing at all", """
        SELECT s.fy,
               count(DISTINCT s.cik) AS with_segments,
               (SELECT count(DISTINCT cik) FROM marts.ratio_values r
                 WHERE r.fy = s.fy) AS with_ratios,
               round(100.0 * count(DISTINCT s.cik)
                     / nullif((SELECT count(DISTINCT cik) FROM marts.ratio_values r
                                WHERE r.fy = s.fy), 0), 1) AS pct
        FROM marts.segments s
        WHERE s.fy BETWEEN 2019 AND 2024
        GROUP BY s.fy ORDER BY s.fy"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)))[:52]
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
