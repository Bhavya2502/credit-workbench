"""G-13 — a read-only catalogue of the schemas belonging to the other workstream.

The request is for "a catalogue of what those schemas actually contain - grain, coverage,
years, entity identifiers - so it can be assessed rather than guessed at". That was first
answered with "it has to come from their owner", which was over-cautious: describing a table
does not require owning it, and reading `information_schema` changes nothing.

This reads metadata and row counts only. It writes nothing, creates nothing and touches no
data in `gold`, `silver` or `catalog`. What it cannot supply is *meaning* - whether a column
called `pd` is a probability of default or a product code is the owner's to confirm, and the
catalogue says so rather than guessing.

**These schemas are not in `credit_workbench`.** MotherDuck's `information_schema` spans
every database the token can reach, so the first version listed them as though they were
local and then failed to count a single row: an unqualified `silver.fdic_bank_financials`
resolves inside `credit_workbench` only, where it does not exist. Every name here is
resolved to its database first and quoted, which is also the honest way to report the
finding - this is another database in the same account, not a corner of ours.

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

    print("### 1. Which databases and schemas can this token reach?")
    try:
        cur = con.execute("""
            SELECT table_catalog, table_schema, count(*) AS objects,
                   count(*) FILTER (WHERE table_type = 'BASE TABLE') AS tables,
                   count(*) FILTER (WHERE table_type = 'VIEW') AS views
            FROM information_schema.tables
            GROUP BY 1, 2 ORDER BY objects DESC""")
        for r in cur.fetchall():
            print(f"  {r[0]:<22}.{r[1]:<20} {r[2]:>5} objects  "
                  f"({r[3]} tables, {r[4]} views)")
    except Exception as exc:
        print(f"  (failed: {str(exc)[:190]})")

    quoted = ", ".join(f"'{s}'" for s in SIBLING)

    print("\n### 2. Objects in the sibling schemas, with column counts")
    try:
        cur = con.execute(f"""
            SELECT t.table_catalog, t.table_schema, t.table_name, t.table_type,
                   count(c.column_name) AS columns
            FROM information_schema.tables t
            LEFT JOIN information_schema.columns c
              ON c.table_schema = t.table_schema AND c.table_name = t.table_name
            WHERE t.table_schema IN ({quoted})
            GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3""")
        rows = cur.fetchall()
        if not rows:
            print("  (no objects found in any sibling schema)")
        for r in rows:
            print(f"  {r[0]}.{r[1]:<10} {r[2]:<42} {r[3]:<12} {r[4]:>4} cols")
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

    # G-13 asked for grain, coverage and years, not just names. Restricted to the tables
    # that answer G-10 and G-13, because the Kaggle training sets drowned the identifier
    # query in NAME_CONTRACT_TYPE columns.
    print("\n### 4b. Grain, coverage and years for the relevant tables")
    relevant = [
        ("silver", "fdic_bank_financials"), ("gold", "us_bank_credit_quarterly"),
        ("gold", "us_bank_credit_by_size"), ("gold", "india_corporate_lgd_panel"),
        ("gold", "india_corporate_lgd_summary"), ("gold", "india_retail_pd_panel"),
        ("gold", "india_retail_pd_summary"), ("silver", "ibbi_cirp_cases"),
        ("silver", "ibbi_liquidation_cases"), ("silver", "ibbi_liquidation_waterfall"),
        ("silver", "ibbi_voluntary_liquidations"), ("catalog", "source_registry"),
    ]
    for schema, table in relevant:
        try:
            db = con.execute("""
                SELECT DISTINCT table_catalog FROM information_schema.tables
                WHERE table_schema = ? AND table_name = ?""",
                             [schema, table]).fetchone()
            if not db:
                print(f"  {schema}.{table:<32} (not present in any database)")
                continue
            fq = f'"{db[0]}".{schema}.{table}'
            n = con.execute(f"SELECT count(*) FROM {fq}").fetchone()[0]
            cols = [r[0] for r in con.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = ? AND table_name = ?
                ORDER BY ordinal_position""", [schema, table]).fetchall()]
            dated = [c for c in cols
                     if any(k in c.lower() for k in ("year", "date", "quarter", "period"))]
            span = ""
            if dated:
                try:
                    lo, hi = con.execute(
                        f"SELECT min({dated[0]}), max({dated[0]}) FROM {fq}"
                    ).fetchone()
                    span = f"   {dated[0]}: {str(lo)[:10]} to {str(hi)[:10]}"
                except Exception:  # noqa: BLE001  unorderable column type
                    span = ""
            print(f"  {db[0]}.{schema}.{table:<30} {n:>10,} rows{span}")
            print(f"      {', '.join(cols[:12])}"
                  + (f" ... (+{len(cols) - 12} more)" if len(cols) > 12 else ""))
        except Exception as exc:  # noqa: BLE001  not ours; report and continue
            print(f"  {schema}.{table:<32} (unreadable: {str(exc)[:70]})")

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
