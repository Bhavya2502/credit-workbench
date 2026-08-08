"""Diagnostic for M2 (calculation linkbase) and instrument-level debt extraction.

The calculation linkbase is the filer's own declaration of which tags sum into which
subtotal. That makes it the authoritative test of our spread mapping: any child tag a
company says belongs in a subtotal we map, but which we do not map ourselves, is a
real gap rather than an inference from frequency counts.

Instrument-level debt sits in the dimensioned facts under a DebtInstrument axis, where
the member name identifies the security. Both need their shapes confirmed before use.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

PERIOD = "2026_06"

Q: list[tuple[str, str]] = [
    ("1. Calculation linkbase — structure", f"""
        SELECT adsh, grp, arc, negative, ptag, ctag
        FROM raw.fsn_cal WHERE period = '{PERIOD}' LIMIT 8"""),

    ("2. How the weight/sign is encoded", f"""
        SELECT negative, count(*) AS arcs FROM raw.fsn_cal
        WHERE period = '{PERIOD}' GROUP BY 1 ORDER BY 2 DESC"""),

    ("3. Volume and reach", f"""
        SELECT count(*) AS arcs, count(DISTINCT adsh) AS filings,
               count(DISTINCT ptag) AS parent_tags, count(DISTINCT ctag) AS child_tags
        FROM raw.fsn_cal WHERE period = '{PERIOD}'"""),

    ("4. What filers declare feeds the subtotals we map", f"""
        SELECT ptag, count(DISTINCT ctag) AS distinct_children,
               count(DISTINCT adsh) AS filings
        FROM raw.fsn_cal
        WHERE period = '{PERIOD}'
          AND ptag IN ('Assets', 'AssetsCurrent', 'Liabilities', 'LiabilitiesCurrent',
                       'StockholdersEquity', 'Revenues', 'OperatingIncomeLoss',
                       'NetCashProvidedByUsedInOperatingActivities')
        GROUP BY 1 ORDER BY 3 DESC"""),

    ("5. Children of current assets that our map may miss", f"""
        SELECT c.ctag, count(DISTINCT c.adsh) AS filings,
               (m.tag IS NOT NULL) AS already_mapped
        FROM raw.fsn_cal c
        LEFT JOIN staging.tag_map m ON m.tag = c.ctag
        WHERE c.period = '{PERIOD}' AND c.ptag = 'AssetsCurrent'
        GROUP BY 1, m.tag IS NOT NULL
        ORDER BY filings DESC LIMIT 15"""),

    ("6. Debt instruments — how the axis and member appear", f"""
        SELECT d.segments, count(*) AS facts
        FROM raw.fsn_num n JOIN raw.fsn_dim d
          ON d.dimhash = n.dimh AND d.period = n.period
        WHERE n.period = '{PERIOD}' AND d.segments LIKE '%DebtInstrument=%'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 10"""),

    ("7. Which instrument-level tags are actually populated", f"""
        SELECT n.tag, count(*) AS facts, count(DISTINCT n.adsh) AS filings
        FROM raw.fsn_num n JOIN raw.fsn_dim d
          ON d.dimhash = n.dimh AND d.period = n.period
        WHERE n.period = '{PERIOD}' AND d.segments LIKE '%DebtInstrument=%'
          AND (n.tag LIKE 'DebtInstrument%' OR n.tag LIKE 'LongTermDebt%'
               OR n.tag LIKE 'LineOfCredit%')
        GROUP BY 1 ORDER BY 2 DESC LIMIT 20"""),

    ("8. Can a maturity year be read from the member name?", f"""
        SELECT regexp_extract(d.segments, 'DebtInstrument=([^;]+)', 1) AS member,
               count(*) AS facts
        FROM raw.fsn_num n JOIN raw.fsn_dim d
          ON d.dimhash = n.dimh AND d.period = n.period
        WHERE n.period = '{PERIOD}' AND d.segments LIKE '%DebtInstrument=%'
          AND regexp_matches(d.segments, 'DebtInstrument=[^;]*20[2-5][0-9]')
        GROUP BY 1 ORDER BY 2 DESC LIMIT 12"""),

    ("9. Revolver capacity — undrawn liquidity", f"""
        SELECT n.tag, count(DISTINCT n.adsh) AS filings
        FROM raw.fsn_num n
        WHERE n.period = '{PERIOD}' AND n.tag LIKE 'LineOfCreditFacility%'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 8"""),
]


def show(con, query: str) -> None:
    cur = con.execute(query)
    headers = [d[0] for d in cur.description]
    rows = [[("" if v is None else str(v))[:95] for v in r] for r in cur.fetchall()]
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
