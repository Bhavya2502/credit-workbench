"""Structure diagnostic for D1, F1, F2 and H1 — inspect before building.

Confirms how segment dimensions are encoded, what concentration disclosures look
like, which note-level tags carry the adjustment inputs, and how 8-K item codes are
stored, so those four builds are written against the real shapes.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

Q: list[tuple[str, str]] = [
    ("F1a. dim table columns — what actually holds the segment text", """
        SELECT dimhash, segments, segt FROM raw.fsn_dim
        WHERE period = '2026_06' AND segments IS NOT NULL AND segments <> ''
        LIMIT 8"""),
    ("F1b. Most common segment axes", """
        SELECT regexp_extract(segments, '^([A-Za-z]+)=', 1) AS axis, count(*) AS n
        FROM raw.fsn_dim WHERE period = '2026_06' AND segments IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT 15"""),
    ("F1c. Revenue facts carrying a business-segment dimension", """
        SELECT count(*) AS facts, count(DISTINCT n.adsh) AS filings
        FROM raw.fsn_num n JOIN raw.fsn_dim d
          ON d.dimhash = n.dimh AND d.period = n.period
        WHERE n.period = '2026_06' AND n.tag LIKE 'Revenue%' AND n.uom = 'USD'
          AND d.segments LIKE '%BusinessSegments%'"""),
    ("F2a. Concentration tags in use", """
        SELECT tag, count(*) AS facts, count(DISTINCT adsh) AS filings
        FROM raw.fsn_num WHERE period = '2026_06' AND tag LIKE '%Concentration%'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 10"""),
    ("F2b. What dimensions concentration facts carry", """
        SELECT d.segments, count(*) AS facts
        FROM raw.fsn_num n JOIN raw.fsn_dim d
          ON d.dimhash = n.dimh AND d.period = n.period
        WHERE n.period = '2026_06' AND n.tag LIKE 'ConcentrationRisk%'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 12"""),
    ("D1a. Lease inputs available (latest month, consolidated)", """
        SELECT tag, count(DISTINCT adsh) AS filings
        FROM staging.facts_pit
        WHERE is_latest AND (tag LIKE '%OperatingLease%' OR tag LIKE '%FinanceLease%')
        GROUP BY 1 ORDER BY 2 DESC LIMIT 12"""),
    ("D1b. Pension and debt-schedule inputs", """
        SELECT tag, count(DISTINCT adsh) AS filings
        FROM staging.facts_pit
        WHERE is_latest AND (tag LIKE 'DefinedBenefit%' OR tag LIKE 'DebtInstrument%'
                             OR tag LIKE 'LongTermDebtMaturities%')
        GROUP BY 1 ORDER BY 2 DESC LIMIT 15"""),
    ("H1a. How 8-K item codes are stored", """
        SELECT items, count(*) AS filings FROM ref.filing_index
        WHERE form = '8-K' AND items IS NOT NULL AND items <> ''
        GROUP BY 1 ORDER BY 2 DESC LIMIT 10"""),
    ("H1b. 8-K volume by year", """
        SELECT substr(filing_date, 1, 4) AS yr, count(*) AS filings
        FROM ref.filing_index WHERE form LIKE '8-K%'
        GROUP BY 1 ORDER BY 1 DESC LIMIT 6"""),
]


def show(con, query: str) -> None:
    cur = con.execute(query)
    headers = [d[0] for d in cur.description]
    rows = [[("" if v is None else str(v))[:70] for v in r] for r in cur.fetchall()]
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
