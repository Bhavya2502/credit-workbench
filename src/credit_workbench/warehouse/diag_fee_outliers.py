"""Why do 21 filings report an audit fee over $500m?

The invariant that audit fees are a plausible size for an audited registrant failed on
the 2024 trial: 21 of 3,739 filings above $500m, against a median of $1.39m. No audit fee
anywhere is that large - the biggest in the world are around $120m - so those figures are
wrong, and they are wrong in a way that looked like a number.

Two candidates, and the fix differs. A units note reading "in thousands" applied to a
table already stated in dollars multiplies by a thousand, which would put a $2m fee at
$2bn; 516 tables were marked as thousands, so a false positive there is the first
suspicion. Or the block matched is not the fee table at all, in which case the figure is
someone else's number entirely.

This prints each outlier with its units, its source section, and the actual rows the
extractor read, which distinguishes the two immediately.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token
from credit_workbench.transform.governance import best_fee_block

OUTLIERS = """
    SELECT g.cik, g.adsh, g.audit_fees, g.total_fees_stated, g.fee_units,
           g.fee_source_section
    FROM marts.governance_metrics g
    WHERE g.audit_fees > 500000000
    ORDER BY g.audit_fees DESC"""

SMALL = """
    SELECT g.cik, g.adsh, g.audit_fees, g.fee_units, g.fee_source_section
    FROM marts.governance_metrics g
    WHERE g.audit_fees > 0 AND g.audit_fees < 10000
    ORDER BY g.audit_fees
    LIMIT 8"""


def show_rows(con, adsh: str, section: str) -> None:
    got = con.execute("""
        SELECT text FROM quali.proxy_sections
        WHERE adsh = ? AND section = ? LIMIT 1""", [adsh, section]).fetchone()
    if not got:
        print("      (section not found)")
        return
    block = best_fee_block(got[0])
    if not block:
        print("      (no block found on re-read)")
        return
    lines = got[0].split("\n")
    print(f"      block units={block['units']} labels={block['labels']} "
          f"found={ {k: round(v) for k, v in block['found'].items()} }")
    # The units note is read from the twelve lines above the block; print them, since
    # that is where a false "in thousands" would be coming from.
    for ln in lines[max(0, block["start"] - 12):block["end"] + 1]:
        if ln.strip():
            print(f"      | {ln.strip()[:104]}")


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")

    print("### Audit fees over $500m — impossible, so these are extraction errors")
    rows = con.execute(OUTLIERS).fetchall()
    print(f"  {len(rows)} filings\n")
    for cik, adsh, audit, total, units, section in rows:
        print(f"  --- {adsh}  cik={cik}  audit={audit:,.0f}  "
              f"total={total if total is None else f'{total:,.0f}'}  "
              f"units={units}  from={section}")
        show_rows(con, adsh, section)
        print()

    print("\n### And the other tail: audit fees under $10,000")
    for cik, adsh, audit, units, section in con.execute(SMALL).fetchall():
        print(f"  --- {adsh}  audit={audit:,.0f}  units={units}  from={section}")
        show_rows(con, adsh, section)
        print()

    print("\n### How are units distributed against fee size?")
    cur = con.execute("""
        SELECT fee_units,
               count(*) AS n,
               round(median(audit_fees), 0) AS median_audit,
               count(*) FILTER (WHERE audit_fees > 500000000) AS over_500m,
               count(*) FILTER (WHERE audit_fees < 10000) AS under_10k
        FROM marts.governance_metrics
        WHERE audit_fees IS NOT NULL
        GROUP BY 1 ORDER BY n DESC""")
    heads = [d[0] for d in cur.description]
    print("  " + "  ".join(f"{h:<14}" for h in heads))
    for r in cur.fetchall():
        print("  " + "  ".join(f"{('' if v is None else v):<14}" for v in r))


if __name__ == "__main__":
    main()
