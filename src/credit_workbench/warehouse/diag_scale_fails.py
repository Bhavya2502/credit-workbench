"""The three invariants that failed once all eight years were scored.

All three passed on filing year 2024 alone and fail on 36,642 filings, which is the
expected way for a threshold calibrated on one year to be wrong. But "calibrated too
tightly" and "genuinely broken on a few filings" look identical from the summary line, so
each is inspected here before any threshold is touched. Relaxing a check to make a build
pass is how a suite stops being worth running.

  6. table rows survived the conversion   pct_with_rows=79.9, threshold >80
 12. footnote markers not read as figures  looks_like_footnote=1 of 28,775
 15. boards are a plausible size           over_30=2, median 7.0 on 17,876

For check 6 the question is whether the 20% of fee sections without a cell separator are
filings that describe fees in prose - which the coverage table already documents as a
third of them - or whether the converter is failing on an older era of markup. Those
imply different actions: the first is a threshold set from one year, the second is a bug.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

Q = [
    ("1. The one filing whose audit fee looks like a footnote marker", """
        SELECT cik, adsh, filing_date, audit_fees, audit_related_fees, tax_fees,
               total_fees_stated, fee_units, fee_units_overridden, fee_source_section
        FROM marts.governance_metrics
        WHERE audit_fees IN (1, 2, 3, 4)"""),

    ("2. Audit fees under 100 — the wider tail of the same problem", """
        SELECT audit_fees, count(*) AS filings,
               count(*) FILTER (WHERE fee_units = 'dollars') AS as_dollars,
               count(*) FILTER (WHERE total_fees_stated IS NOT NULL) AS with_total
        FROM marts.governance_metrics
        WHERE audit_fees > 0 AND audit_fees < 100
        GROUP BY 1 ORDER BY 2 DESC LIMIT 12"""),

    ("3. The two boards over 30 — real, or a table that is not a board?", """
        SELECT cik, adsh, filing_date, directors_listed,
               directors_marked_independent, substr(independence_statement, 1, 90) AS stmt
        FROM marts.governance_metrics
        WHERE directors_listed > 30
        ORDER BY directors_listed DESC"""),

    ("4. Board size distribution — is 30 a sensible ceiling at all?", """
        SELECT directors_listed, count(*) AS filings
        FROM marts.governance_metrics
        WHERE directors_listed >= 18
        GROUP BY 1 ORDER BY 1"""),

    # Check 6: prose fee sections are expected and documented. A converter failure would
    # show as a whole era with no separators, so split it by year.
    ("5. Fee sections carrying a cell separator, by filing year", """
        SELECT substr(filing_date, 1, 4) AS filing_year,
               count(*) AS fee_sections,
               round(100.0 * count(*) FILTER (WHERE text LIKE '%|%')
                     / count(*), 1) AS pct_with_rows
        FROM quali.proxy_sections
        WHERE section = 'audit_fees'
        GROUP BY 1 ORDER BY 1"""),

    ("6. And across every section, not just the fee one", """
        SELECT round(100.0 * count(*) FILTER (WHERE text LIKE '%|%')
                     / count(*), 1) AS pct_any_section_with_rows,
               count(*) AS sections
        FROM quali.proxy_sections"""),

    # If the sections without separators still yield fee figures, the separator is not
    # what decides extraction and the check is measuring the wrong thing.
    ("7. Do filings whose fee section has no rows still get a fee figure?", """
        SELECT s.text LIKE '%|%' AS section_has_rows,
               count(*) AS filings,
               count(g.audit_fees) AS with_audit_fee,
               round(100.0 * count(g.audit_fees) / count(*), 1) AS pct_extracted
        FROM quali.proxy_sections s
        JOIN marts.governance_metrics g ON g.adsh = s.adsh AND g.cik = s.cik
        WHERE s.section = 'audit_fees'
        GROUP BY 1 ORDER BY 1"""),

    ("8. Overall fee coverage across the full eight years", """
        SELECT count(*) AS filings,
               count(audit_fees) AS with_audit_fee,
               round(100.0 * count(audit_fees) / count(*), 1) AS pct,
               count(directors_listed) AS with_board,
               round(100.0 * count(directors_listed) / count(*), 1) AS pct_board,
               count(ceo_pay_ratio) AS with_pay_ratio
        FROM marts.governance_metrics"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)))[:92]
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
