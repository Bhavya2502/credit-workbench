"""Investigate how SEC facts duplicate and which tags carry the statements.

Answers the questions C4 and C5 depend on, from the data rather than assumption:
how the same figure appears more than once, which column identifies the best copy,
and which tags actually populate the face of the financial statements.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

Q: list[tuple[str, str]] = [
    ("A. Pfizer 'Assets' in its latest 10-K — every stored copy", """
        WITH latest AS (
            SELECT adsh, period FROM raw.fsn_sub
            WHERE cik = '78003' AND form = '10-K' ORDER BY filed DESC LIMIT 1)
        SELECT n.ddate, n.qtrs, n.uom, n.dimh, n.iprx, n.coreg,
               n.durp, n.datp, n.dcml, n.value
        FROM raw.fsn_num n JOIN latest l ON n.adsh = l.adsh AND n.period = l.period
        WHERE n.tag = 'Assets'
        ORDER BY n.ddate DESC, n.iprx"""),
    ("B. How often is iprx non-zero (duplicate copies)?", """
        SELECT iprx, count(*) AS facts
        FROM raw.fsn_num WHERE period = '2026_06'
        GROUP BY 1 ORDER BY 1 LIMIT 8"""),
    ("C. durp / datp spread (period-fit indicators)", """
        SELECT round(TRY_CAST(durp AS DOUBLE), 2) AS durp, count(*) AS facts
        FROM raw.fsn_num WHERE period = '2026_06'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 8"""),
    ("D. Dimensioned vs undimensioned facts", """
        SELECT CASE WHEN dimn = '0' THEN 'no dimensions (consolidated)'
                    ELSE 'dimensioned (segments/axes)' END AS kind,
               count(*) AS facts
        FROM raw.fsn_num WHERE period = '2026_06' GROUP BY 1"""),
    ("E. Amended filings: prevrpt flag", """
        SELECT prevrpt, count(*) AS filings FROM raw.fsn_sub GROUP BY 1"""),
    ("F. Same figure across filings (point-in-time need)", """
        SELECT n.ddate, s.form, s.filed, n.value
        FROM raw.fsn_num n JOIN raw.fsn_sub s ON s.adsh = n.adsh AND s.period = n.period
        WHERE s.cik = '78003' AND n.tag = 'Revenues' AND n.qtrs = '4'
          AND n.dimn = '0' AND n.ddate = '20231231'
        ORDER BY s.filed"""),
    ("G. Statement types available in the presentation table", """
        SELECT stmt, count(*) AS lines FROM raw.fsn_pre
        WHERE period = '2026_06' GROUP BY 1 ORDER BY 2 DESC"""),
]

TOP_TAGS = """
    SELECT p.stmt, n.tag, count(DISTINCT n.adsh) AS filings
    FROM raw.fsn_num n
    JOIN raw.fsn_pre p ON p.adsh = n.adsh AND p.tag = n.tag
                      AND p.version = n.version AND p.period = n.period
    JOIN raw.fsn_sub s ON s.adsh = n.adsh AND s.period = n.period
    WHERE n.period = '2026_06' AND s.form = '10-K'
      AND n.dimn = '0' AND p.stmt = ?
    GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 60"""


def show(con, query: str, params: list | None = None) -> None:
    cur = con.execute(query, params) if params else con.execute(query)
    headers = [d[0] for d in cur.description]
    rows = [["" if v is None else str(v) for v in r] for r in cur.fetchall()]
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

    for stmt, label in (("IS", "income statement"), ("BS", "balance sheet"),
                        ("CF", "cash flow statement")):
        print(f"\n### Most-used {label} tags (10-K, latest month)")
        try:
            show(con, TOP_TAGS, [stmt])
        except Exception as exc:  # noqa: BLE001
            print(f"  (failed: {exc})")


if __name__ == "__main__":
    main()
