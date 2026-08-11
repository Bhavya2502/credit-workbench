"""What are the biggest unmodelled tags, and where does each one belong?

227 tags carry a quarter of all facts through named spread lines. The rest are reachable
and labelled but unnamed, and the question is not simply "map more" - it is where each
one belongs, because these are not all the same kind of thing. A share count is not a
spread line. An interest rate on one tranche is an attribute of a debt instrument, not a
company-year figure. A revolver commitment is a note input.

The note index makes that routing answerable rather than a matter of taste: it says
which note each tag is actually presented in, which is a far better guide than the tag's
name. Units settle the rest - a tag reported in shares or in pure percentages cannot
join a monetary spread whatever its name suggests.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

FRONTIER = """
    WITH frontier AS (
        SELECT tag, label,
               greatest(consolidated_filings, dimensioned_filings) AS filings,
               companies, consolidated_facts, dimensioned_facts
        FROM ref.tag_catalog
        WHERE NOT in_spread_template AND standard_taxonomy
          AND tag NOT IN (SELECT DISTINCT source_tag FROM staging.note_inputs)
        ORDER BY filings DESC LIMIT 150),
    units AS (
        SELECT tag, any_value(uom) AS uom FROM (
            SELECT tag, uom, count(*) AS n FROM staging.facts_pit
            WHERE is_latest AND period_year >= 2022
              AND tag IN (SELECT tag FROM frontier)
            GROUP BY 1, 2
            QUALIFY row_number() OVER (PARTITION BY tag ORDER BY count(*) DESC) = 1)
        GROUP BY tag),
    home AS (
        SELECT tag, note_type, note_category FROM (
            SELECT m.tag, n.note_type, n.note_category, count(*) AS n
            FROM ref.tag_note_map m
            JOIN ref.note_index n
              ON n.adsh = m.adsh AND n.report = m.report AND n.period = m.period
            WHERE m.archive_year = 2024
              AND m.tag IN (SELECT tag FROM frontier)
              AND n.note_category IN ('note', 'note_detail', 'statement')
            GROUP BY 1, 2, 3
            QUALIFY row_number() OVER (PARTITION BY m.tag ORDER BY count(*) DESC) = 1))
"""

Q: list[tuple[str, str]] = [
    ("1. The frontier, with the note each tag actually sits in", FRONTIER + """
        SELECT f.tag, coalesce(u.uom, '?') AS uom,
               coalesce(h.note_type, '?') AS note_type,
               coalesce(h.note_category, '?') AS presented_as,
               f.filings,
               CASE WHEN f.dimensioned_facts > f.consolidated_facts THEN 'schedule'
                    ELSE 'company' END AS grain
        FROM frontier f
        LEFT JOIN units u ON u.tag = f.tag
        LEFT JOIN home  h ON h.tag = f.tag
        ORDER BY f.filings DESC LIMIT 55"""),

    ("2. Clusters — what kind of thing is unmodelled, by unit", FRONTIER + """
        SELECT coalesce(u.uom, '?') AS uom, count(*) AS tags,
               sum(f.filings) AS total_filings
        FROM frontier f LEFT JOIN units u ON u.tag = f.tag
        GROUP BY 1 ORDER BY tags DESC"""),

    ("3. Clusters — which notes the unmodelled tags belong to", FRONTIER + """
        SELECT coalesce(h.note_type, '?') AS note_type, count(*) AS tags,
               sum(f.filings) AS total_filings
        FROM frontier f LEFT JOIN home h ON h.tag = f.tag
        GROUP BY 1 ORDER BY tags DESC LIMIT 20"""),

    ("4. Monetary tags on the face of a statement — candidate spread lines", FRONTIER + """
        SELECT f.tag, f.filings, coalesce(h.note_type, '?') AS note_type
        FROM frontier f
        LEFT JOIN units u ON u.tag = f.tag
        LEFT JOIN home  h ON h.tag = f.tag
        WHERE u.uom = 'USD' AND h.note_category = 'statement'
        ORDER BY f.filings DESC LIMIT 25"""),
]


def show(con, query: str) -> None:
    cur = con.execute(query)
    headers = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)))[:62]
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
            print(f"  (failed: {str(exc)[:200]})")


if __name__ == "__main__":
    main()
