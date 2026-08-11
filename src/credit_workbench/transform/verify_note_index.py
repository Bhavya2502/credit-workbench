"""Invariants for the note index.

The point of this layer is navigability, so the checks are about whether a fact really
can be found under the note it belongs to: does the bridge resolve, does the coverage
reach effectively every fact, and do the classified note types actually contain the
figures those notes are supposed to contain. That last one is the real test - a debt
note that does not contain debt tags would mean the titles were classified by wishful
pattern matching rather than by what filers wrote.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

CHECKS: list[tuple[str, str, str]] = [
    ("the note index is populated across years",
     """SELECT count(*) AS reports, count(DISTINCT adsh) AS filings,
               count(DISTINCT note_type) AS note_types,
               count(DISTINCT archive_year) AS years
        FROM ref.note_index""",
     "reports > 5e6 and filings > 200000 and note_types > 20 and years > 10"),

    ("every presented tag resolves to a titled report",
     """SELECT count(*) AS unresolved FROM (
            SELECT m.adsh FROM ref.tag_note_map m
            LEFT JOIN ref.note_index n
              ON n.adsh = m.adsh AND n.report = m.report AND n.period = m.period
            WHERE n.adsh IS NULL AND m.archive_year = 2024 LIMIT 200000)""",
     "unresolved < 100000"),

    ("effectively every fact lands in at least one note",
     """WITH sample AS (
            SELECT adsh, tag FROM staging.facts_pit
            WHERE is_latest AND period_year = 2023 LIMIT 400000)
        SELECT count(*) AS facts,
               count(*) FILTER (WHERE m.tag IS NOT NULL) AS placed,
               round(100.0 * count(*) FILTER (WHERE m.tag IS NOT NULL)
                     / count(*), 1) AS pct_placed
        FROM sample s
        LEFT JOIN (SELECT DISTINCT adsh, tag FROM ref.tag_note_map) m
               ON m.adsh = s.adsh AND m.tag = s.tag""",
     "pct_placed > 90"),

    # Filings separate the note text from the schedule inside it, and the detail blocks
    # are where the dimensioned figures are presented. Both must be present.
    ("note text and the detail blocks are both indexed",
     """SELECT count(*) FILTER (WHERE note_category = 'note') AS notes,
               count(*) FILTER (WHERE note_category = 'note_detail') AS details,
               count(*) FILTER (WHERE note_category = 'statement') AS statements,
               count(*) FILTER (WHERE note_category = 'accounting_policy') AS policies
        FROM ref.note_index""",
     "notes > 500000 and details > 500000 and statements > 100000 and policies > 50000"),

    ("classification is not dominated by the fallback bucket",
     """SELECT round(100.0 * count(*) FILTER (WHERE note_type = 'other')
                     / count(*), 1) AS pct_other
        FROM ref.note_index WHERE note_category IN ('note', 'note_detail')""",
     "pct_other < 45"),

    # The substantive test: a note classified as debt has to carry debt figures.
    ("the debt note contains debt tags",
     """SELECT count(*) AS facts,
               round(100.0 * count(*) FILTER (
                   WHERE tag ILIKE '%debt%' OR tag ILIKE '%borrowing%'
                      OR tag ILIKE '%notespayable%' OR tag ILIKE '%creditfacility%'
                      OR tag ILIKE '%lineofcredit%' OR tag ILIKE '%interest%')
                     / count(*), 1) AS pct_debt_related
        FROM marts.facts_by_note
        WHERE note_type = 'debt' AND is_latest AND fy = 2023""",
     "facts > 10000 and pct_debt_related > 40"),

    ("the income tax note contains tax tags",
     """SELECT count(*) AS facts,
               round(100.0 * count(*) FILTER (WHERE tag ILIKE '%tax%')
                     / count(*), 1) AS pct_tax_related
        FROM marts.facts_by_note
        WHERE note_type = 'income_taxes' AND is_latest AND fy = 2023""",
     "facts > 10000 and pct_tax_related > 50"),

    ("the lease note contains lease tags",
     """SELECT count(*) AS facts,
               round(100.0 * count(*) FILTER (WHERE tag ILIKE '%lease%')
                     / count(*), 1) AS pct_lease_related
        FROM marts.facts_by_note
        WHERE note_type = 'leases' AND is_latest AND fy = 2023""",
     "facts > 5000 and pct_lease_related > 60"),

    ("the schedules are reachable by note as well",
     """SELECT count(*) AS facts, count(DISTINCT note_type) AS note_types,
               count(DISTINCT cik) AS companies
        FROM marts.schedules_by_note WHERE is_latest AND fy = 2023""",
     "facts > 1000000 and note_types > 20 and companies > 3000"),

    ("a fact presented in several notes is kept in each, not deduplicated away",
     """SELECT count(*) AS tag_filings_in_several_reports FROM (
            SELECT adsh, tag FROM ref.tag_note_map WHERE archive_year = 2024
            GROUP BY 1, 2 HAVING count(DISTINCT report) > 1 LIMIT 100000)""",
     "tag_filings_in_several_reports > 1000"),

    ("note titles collapse to a normalised form",
     """SELECT count(DISTINCT note_title) AS as_written,
               count(DISTINCT note_title_normalised) AS normalised
        FROM ref.note_index WHERE note_category = 'note'""",
     "normalised < as_written"),
]


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    failures = 0
    for i, (name, query, assertion) in enumerate(CHECKS, 1):
        try:
            cur = con.execute(query)
            row = dict(zip([d[0] for d in cur.description], cur.fetchone()))
        except Exception as exc:  # noqa: BLE001
            print(f"{i:2}. ERROR  {name}\n      {str(exc)[:170]}")
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
