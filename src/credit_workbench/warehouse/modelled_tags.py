"""One registry of every tag any mart claims, and which mart claims it.

The frontier analysis asked which tags are still unmodelled by excluding those in the
spread template and in the note inputs. That was wrong, and flattering: it counted work
already done as outstanding. All six of the debt tags it put near the top -
`DebtInstrumentInterestRateStatedPercentage`, `DebtInstrumentFaceAmount`,
`DebtInstrumentBasisSpreadOnVariableRate1`, `DebtInstrumentCarryingAmount`,
`LineOfCreditFacilityMaximumBorrowingCapacity`, `LineOfCredit` - are modelled in the
debt-instrument mart and have been all along.

The lists live in the transform modules, so this reads them from the code rather than
restating them, and a mart that gains a tag is registered here the moment it is added.
Anything not in this registry really is unclaimed.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token
from credit_workbench.transform import debt_instruments, note_inputs, segments, tag_map


def claimed() -> list[tuple[str, str, str]]:
    """(tag, mart, role) for every tag any mart claims."""
    rows: list[tuple[str, str, str]] = []

    for line_no, line_code, _label, _stmt, tags in tag_map.TEMPLATE:
        for tag in tags:
            rows.append((tag, "marts.spread_lines", line_code))

    for category, cols in note_inputs.CATEGORIES.items():
        for col, tags in cols.items():
            for tag in tags:
                rows.append((tag, "marts.adjustment_inputs", col))

    for group, mart in ((debt_instruments.INSTRUMENT_TAGS, "marts.debt_instruments"),
                        (debt_instruments.FACILITY_TAGS, "marts.revolver_capacity")):
        for col, tags in group.items():
            for tag in tags:
                rows.append((tag, mart, col))

    for tag in segments.MEASURE_TAGS:
        rows.append((tag, "marts.segments", "segment_measure"))
    for tag in segments.CONC_TAGS:
        rows.append((tag, "marts.concentration", "concentration"))

    return rows


def main() -> None:
    rows = claimed()
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    con.execute("""CREATE OR REPLACE TABLE ref.modelled_tags (
                       tag VARCHAR, mart VARCHAR, role VARCHAR)""")
    con.executemany("INSERT INTO ref.modelled_tags VALUES (?, ?, ?)", rows)

    total, tags = con.execute(
        "SELECT count(*), count(DISTINCT tag) FROM ref.modelled_tags").fetchone()
    print(f"table ref.modelled_tags  {total:,} claims over {tags:,} distinct tags")
    for mart, n in con.execute("""
            SELECT mart, count(DISTINCT tag) FROM ref.modelled_tags
            GROUP BY 1 ORDER BY 2 DESC""").fetchall():
        print(f"  {mart:<28} {n:>5} tags")

    # Restate the frontier against the full registry rather than two marts.
    con.execute("""
        CREATE OR REPLACE TABLE staging.frontier_tags AS
        SELECT c.tag, c.label,
               greatest(c.consolidated_filings, c.dimensioned_filings) AS filings,
               c.companies, c.consolidated_facts, c.dimensioned_facts,
               CASE WHEN c.dimensioned_facts > c.consolidated_facts THEN 'schedule'
                    ELSE 'company' END AS grain
        FROM ref.tag_catalog c
        LEFT JOIN (SELECT DISTINCT tag FROM ref.modelled_tags) m ON m.tag = c.tag
        WHERE c.standard_taxonomy AND m.tag IS NULL
        ORDER BY filings DESC LIMIT 300""")
    n, top = con.execute("""
        SELECT count(*), max(filings) FROM staging.frontier_tags""").fetchone()
    print(f"\ntable staging.frontier_tags  {n:,} genuinely unclaimed tags, "
          f"largest reaches {top:,} filings")

    print("\nHow much of the filed data does the modelled set now carry?")
    row = con.execute("""
        SELECT count(*) AS tags_modelled,
               round(100.0 * sum(c.total_facts) FILTER (WHERE m.tag IS NOT NULL)
                     / sum(c.total_facts), 1) AS pct_facts_modelled
        FROM ref.tag_catalog c
        LEFT JOIN (SELECT DISTINCT tag FROM ref.modelled_tags) m ON m.tag = c.tag
        """).fetchone()
    print(f"  distinct tags in the registry: {tags:,}")
    print(f"  share of all facts they carry: {row[1]}%")


if __name__ == "__main__":
    main()
