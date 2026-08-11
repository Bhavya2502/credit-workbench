"""Invariants for the narrative layer.

Text extraction fails quietly: a splitter that returns the table of contents, or one
section that has swallowed the rest of the document, still produces plausible-looking
rows. So the checks are about whether each section is the thing it claims to be - the
right length, containing the language that section must contain, and joining back to a
real filing.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

CHECKS: list[tuple[str, str, str]] = [
    ("sections extracted across companies and years",
     """SELECT count(*) AS sections, count(DISTINCT adsh) AS filings,
               count(DISTINCT cik) AS companies,
               count(DISTINCT substr(filing_date, 1, 4)) AS years
        FROM quali.filing_sections""",
     "sections > 100000 and companies > 3000 and years >= 5"),

    ("every section joins back to a real filing",
     """SELECT count(*) AS orphans FROM (
            SELECT s.adsh FROM quali.filing_sections s
            LEFT JOIN ref.filing_index f ON f.accession_number = s.adsh
            WHERE f.accession_number IS NULL LIMIT 50000)""",
     "orphans == 0"),

    ("risk factors read like risk factors",
     """SELECT count(*) AS n,
               round(100.0 * count(*) FILTER (WHERE lower(text) LIKE '%risk%')
                     / count(*), 1) AS pct_mentioning_risk,
               median(char_len) AS median_chars
        FROM quali.risk_factors""",
     "n > 20000 and pct_mentioning_risk > 95 and median_chars > 5000"),

    ("MD&A reads like MD&A",
     """SELECT count(*) AS n,
               round(100.0 * count(*) FILTER (
                   WHERE lower(text) LIKE '%results of operations%'
                      OR lower(text) LIKE '%liquidity%') / count(*), 1) AS pct_expected,
               median(char_len) AS median_chars
        FROM quali.mdna""",
     "n > 30000 and pct_expected > 80 and median_chars > 5000"),

    ("controls discussion mentions internal control",
     """SELECT count(*) AS n,
               round(100.0 * count(*) FILTER (
                   WHERE lower(text) LIKE '%internal control%') / count(*), 1) AS pct
        FROM quali.controls_and_procedures""",
     "n > 30000 and pct > 90"),

    # A section that runs to the end of the document is the classic failure: Item 15
    # is an exhibit list, and if the financial statements follow it in the filing the
    # split can swallow them whole.
    ("no section has swallowed the rest of the document",
     """SELECT count(*) AS sections,
               round(100.0 * count(*) FILTER (WHERE char_len > 400000)
                     / count(*), 3) AS pct_over_400k,
               max(char_len) AS longest
        FROM quali.filing_sections""",
     "pct_over_400k < 1.0"),

    ("item 15 stays an exhibit list rather than the financial statements",
     "SELECT median(char_len) AS median_chars FROM quali.filing_sections WHERE item = '15'",
     "median_chars < 30000"),

    ("sections are distinct per filing and item",
     """SELECT count(*) AS rows, count(DISTINCT (adsh, item)) AS distinct_pairs
        FROM quali.filing_sections""",
     "rows == distinct_pairs"),

    ("cybersecurity disclosures appear once the rule took effect",
     """SELECT count(*) AS n FROM quali.filing_sections
        WHERE item = '1C' AND substr(filing_date, 1, 4) >= '2024'""",
     "n > 500"),

    # Exhibits ----------------------------------------------------------------
    ("debt agreements captured",
     """SELECT count(*) AS docs, count(DISTINCT cik) AS companies,
               count(*) FILTER (WHERE doc_kind = 'credit_agreement') AS credit_agreements,
               count(*) FILTER (WHERE doc_kind = 'indenture') AS indentures
        FROM quali.debt_agreements""",
     "docs > 5000 and companies > 1000 and credit_agreements > 1000"),

    ("the agreements contain the clauses that make them agreements",
     """SELECT count(*) AS n,
               round(100.0 * count(*) FILTER (
                   WHERE lower(text) LIKE '%events of default%') / count(*), 1)
                   AS pct_events_of_default,
               round(100.0 * count(*) FILTER (
                   WHERE lower(text) LIKE '%covenant%') / count(*), 1) AS pct_covenant
        FROM quali.debt_agreements
        WHERE doc_kind IN ('credit_agreement', 'indenture')""",
     "n > 1000 and pct_events_of_default > 70 and pct_covenant > 70"),

    ("exhibits are long enough to be agreements, not cover letters",
     "SELECT median(char_len) AS median_chars FROM quali.debt_agreements",
     "median_chars > 50000"),
]


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    failures = 0
    for i, (name, query, assertion) in enumerate(CHECKS, 1):
        try:
            cur = con.execute(query)
            row = dict(zip([d[0] for d in cur.description], cur.fetchone()))
        except Exception as exc:  # noqa: BLE001
            print(f"{i:2}. ERROR  {name}\n      {str(exc)[:150]}")
            failures += 1
            continue
        detail = ", ".join(
            f"{k}={v:,}" if isinstance(v, int)
            else f"{k}={v:,.1f}" if isinstance(v, float) else f"{k}={v}"
            for k, v in row.items())
        ok = eval(assertion, {}, {k: (v if v is not None else 0)  # noqa: S307
                                  for k, v in row.items()})
        print(f"{i:2}. {'PASS' if ok else 'FAIL'}  {name}\n      {detail}")
        failures += not ok
    print(f"\n{len(CHECKS) - failures}/{len(CHECKS)} checks passed")
    if failures:
        raise SystemExit(f"{failures} invariant(s) failed")


if __name__ == "__main__":
    main()
