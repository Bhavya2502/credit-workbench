"""Can every fact be told which note it was presented in?

Reachability is done: all 373m filed facts sit in a mart. But a fact carries a statement
(IS/BS/CF) and not a note, so "everything in the Debt note for this company" is not yet
a query anyone can write. That is the difference between all schedules being reachable
and all notes being covered.

The filing itself knows the answer. The presentation linkbase assigns each tag to a
numbered report within the filing, and the rendering file titles those reports - "Debt",
"Fair Value Measurements", "Income Taxes". Joining the two would give every fact the
note it appeared in, under the filer's own heading.

Before building on that, confirm on real data: what the two tables contain, whether the
report numbers actually join, whether titles are usable as-is, and what share of facts
would end up assigned.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

PERIOD = "2026_06"

Q: list[tuple[str, str]] = [
    ("1. What is in the rendering file?", f"""
        SELECT * FROM raw.fsn_ren WHERE period = '{PERIOD}' LIMIT 4"""),

    ("2. What is in the presentation linkbase?", f"""
        SELECT * FROM raw.fsn_pre WHERE period = '{PERIOD}' LIMIT 4"""),

    ("3. Do report numbers join between them?", f"""
        SELECT count(*) AS pre_rows,
               count(*) FILTER (WHERE r.adsh IS NOT NULL) AS joined_to_a_title,
               round(100.0 * count(*) FILTER (WHERE r.adsh IS NOT NULL)
                     / count(*), 1) AS pct
        FROM raw.fsn_pre p
        LEFT JOIN raw.fsn_ren r
          ON r.adsh = p.adsh AND r.report = p.report AND r.period = p.period
        WHERE p.period = '{PERIOD}'"""),

    ("4. Menu categories - how the filing groups its own reports", f"""
        SELECT menucat, count(*) AS reports, count(DISTINCT adsh) AS filings
        FROM raw.fsn_ren WHERE period = '{PERIOD}'
        GROUP BY 1 ORDER BY reports DESC"""),

    ("5. The note titles filers actually use", f"""
        SELECT shortname, count(*) AS filings
        FROM raw.fsn_ren WHERE period = '{PERIOD}' AND menucat = 'N'
        GROUP BY 1 ORDER BY filings DESC LIMIT 30"""),

    ("6. What share of facts would get a note, via tag+filing?", f"""
        WITH pre AS (
            SELECT DISTINCT adsh, period, tag, report FROM raw.fsn_pre
            WHERE period = '{PERIOD}')
        SELECT count(*) AS facts,
               count(*) FILTER (WHERE p.report IS NOT NULL) AS with_a_report,
               round(100.0 * count(*) FILTER (WHERE p.report IS NOT NULL)
                     / count(*), 1) AS pct_assigned
        FROM raw.fsn_num n
        LEFT JOIN pre p ON p.adsh = n.adsh AND p.period = n.period AND p.tag = n.tag
        WHERE n.period = '{PERIOD}' AND n.iprx = '0'"""),

    ("7. Does a tag land in more than one report within a filing?", f"""
        SELECT reports_per_tag, count(*) AS tag_filings
        FROM (SELECT adsh, tag, count(DISTINCT report) AS reports_per_tag
              FROM raw.fsn_pre WHERE period = '{PERIOD}'
              GROUP BY 1, 2)
        GROUP BY 1 ORDER BY 1 LIMIT 10"""),

    ("8. Are the detailed schedules separated from the note text?", f"""
        SELECT CASE WHEN lower(shortname) LIKE '%(details)%' THEN 'Details (the schedule)'
                    WHEN lower(shortname) LIKE '%(tables)%'  THEN 'Tables'
                    WHEN lower(shortname) LIKE '%(polic%'    THEN 'Policies'
                    WHEN menucat = 'N' THEN 'Note text'
                    ELSE 'Statement or other' END AS kind,
               count(*) AS reports
        FROM raw.fsn_ren WHERE period = '{PERIOD}'
        GROUP BY 1 ORDER BY reports DESC"""),
]


def show(con, query: str) -> None:
    cur = con.execute(query)
    headers = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,.4g}" if isinstance(v, float) else
                                    f"{v:,}" if isinstance(v, int) else str(v)))[:58]
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
