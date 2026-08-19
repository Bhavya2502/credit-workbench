"""G-13 — a read-only catalogue of the schemas belonging to the other workstream.

The request is for "a catalogue of what those schemas actually contain - grain, coverage,
years, entity identifiers - so it can be assessed rather than guessed at". That was first
answered with "it has to come from their owner", which was over-cautious: describing a table
does not require owning it, and reading `information_schema` changes nothing.

This reads metadata and row counts only. It writes nothing, creates nothing and touches no
data in `gold`, `silver` or `catalog`. What it cannot supply is *meaning* - whether a column
called `pd` is a probability of default or a product code is the owner's to confirm, and the
catalogue says so rather than guessing.

It also serves G-10 in passing. The gap document notes that FDIC data may already sit in
these schemas, in which case exposing it is most of the work of the banks segment rather than
a new ingest of FFIEC Call Reports. That is worth knowing before anyone commissions the
larger job.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

SIBLING = ("gold", "silver", "catalog", "bronze", "raw_india", "india")


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")

    print("### 1. Which schemas exist in this database at all?")
    try:
        cur = con.execute("""
            SELECT table_schema, count(*) AS objects,
                   count(*) FILTER (WHERE table_type = 'BASE TABLE') AS tables,
                   count(*) FILTER (WHERE table_type = 'VIEW') AS views
            FROM information_schema.tables
            GROUP BY table_schema ORDER BY objects DESC""")
        for r in cur.fetchall():
            print(f"  {r[0]:<20} {r[1]:>5} objects  ({r[2]} tables, {r[3]} views)")
    except Exception as exc:
        print(f"  (failed: {str(exc)[:190]})")

    quoted = ", ".join(f"'{s}'" for s in SIBLING)

    print("\n### 2. Objects in the sibling schemas, with column counts")
    try:
        cur = con.execute(f"""
            SELECT t.table_schema, t.table_name, t.table_type,
                   count(c.column_name) AS columns
            FROM information_schema.tables t
            LEFT JOIN information_schema.columns c
              ON c.table_schema = t.table_schema AND c.table_name = t.table_name
            WHERE t.table_schema IN ({quoted})
            GROUP BY 1, 2, 3 ORDER BY 1, 2""")
        rows = cur.fetchall()
        if not rows:
            print("  (no objects found in any sibling schema)")
        for r in rows:
            print(f"  {r[0]:<12} {r[1]:<44} {r[2]:<12} {r[3]:>4} cols")
    except Exception as exc:
        print(f"  (failed: {str(exc)[:190]})")

    # Entity identifiers are the join that decides whether any of this is usable alongside
    # the SEC data, so they are looked for by name rather than left to be discovered.
    print("\n### 3. Entity identifiers — what could join to anything here?")
    try:
        cur = con.execute(f"""
            SELECT table_schema, table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema IN ({quoted})
              AND (lower(column_name) LIKE '%cik%' OR lower(column_name) LIKE '%rssd%'
                OR lower(column_name) LIKE '%cert%' OR lower(column_name) LIKE '%lei%'
                OR lower(column_name) LIKE '%isin%' OR lower(column_name) LIKE '%cin%'
                OR lower(column_name) LIKE '%pan%' OR lower(column_name) LIKE '%ticker%'
                OR lower(column_name) LIKE '%company%' OR lower(column_name) LIKE '%entity%'
                OR lower(column_name) LIKE '%borrower%' OR lower(column_name) LIKE '%name%')
            ORDER BY 1, 2, 3""")
        rows = cur.fetchall()
        if not rows:
            print("  (no identifier-shaped columns found)")
        for r in rows[:60]:
            print(f"  {r[0]:<10} {r[1]:<38} {r[2]:<28} {r[3]}")
        if len(rows) > 60:
            print(f"  ... and {len(rows) - 60} more")
    except Exception as exc:
        print(f"  (failed: {str(exc)[:190]})")

    print("\n### 4. Anything that looks like FDIC or bank regulatory data (G-10)")
    try:
        cur = con.execute(f"""
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE (table_schema IN ({quoted})
                   AND (lower(table_name) LIKE '%fdic%' OR lower(table_name) LIKE '%bank%'
                     OR lower(table_name) LIKE '%call%' OR lower(table_name) LIKE '%ffiec%'
                     OR lower(table_name) LIKE '%rssd%'))
            ORDER BY 1, 2""")
        rows = cur.fetchall()
        print("  " + ("(nothing bank-shaped found)" if not rows else ""))
        for r in rows:
            print(f"  {r[0]:<12} {r[1]}")
    except Exception as exc:
        print(f"  (failed: {str(exc)[:190]})")

    print("\n### 5. Anything India-shaped (G-13's actual subject)")
    try:
        cur = con.execute(f"""
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema IN ({quoted})
              AND (lower(table_name) LIKE '%india%' OR lower(table_name) LIKE '%ibbi%'
                OR lower(table_name) LIKE '%lgd%' OR lower(table_name) LIKE '%pd%'
                OR lower(table_name) LIKE '%insolven%' OR lower(table_name) LIKE '%nclt%')
            ORDER BY 1, 2""")
        rows = cur.fetchall()
        print("  " + ("(nothing India-shaped found)" if not rows else ""))
        for r in rows:
            print(f"  {r[0]:<12} {r[1]}")
    except Exception as exc:
        print(f"  (failed: {str(exc)[:190]})")


if __name__ == "__main__":
    main()
