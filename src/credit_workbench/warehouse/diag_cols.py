"""Column names for the tables the gap work builds on.

Three guesses were wrong in the gap probe - `peer_group` for `peers`, `major_group` for
`division_code`, `op_lease_y1` for `op_lease_due_y1` - and a mart built on a guessed column
name fails at the end of a long run rather than the start. Cheap to ask.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

TABLES = [("ref", "industry_group"), ("ref", "sic_hierarchy"),
          ("ref", "dim_company"), ("marts", "ratio_values")]


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    for schema, table in TABLES:
        cols = con.execute("""
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_schema = ? AND table_name = ?
            ORDER BY ordinal_position""", [schema, table]).fetchall()
        print(f"\n### {schema}.{table}")
        for name, dtype in cols:
            print(f"  {name:<40} {dtype}")

    # The lease and pension prefixes, which decide what G-04 can compute.
    print("\n### marts.adjustment_inputs — lease, pension and debt columns")
    cols = con.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'marts' AND table_name = 'adjustment_inputs'
          AND (column_name LIKE '%lease%' OR column_name LIKE '%pension%'
               OR column_name LIKE 'debt_due%' OR column_name LIKE '%rent%')
        ORDER BY column_name""").fetchall()
    for (name,) in cols:
        print(f"  {name}")

    print("\n### ref.industry_group — a few rows")
    cur = con.execute("SELECT * FROM ref.industry_group LIMIT 5")
    heads = [d[0] for d in cur.description]
    print("  " + " | ".join(heads))
    for r in cur.fetchall():
        print("  " + " | ".join(str(v)[:28] for v in r))

    print("\n### ref.industry_group — how many companies per peer group?")
    cur = con.execute("""
        SELECT count(*) AS groups,
               count(*) FILTER (WHERE companies >= 30) AS groups_over_30,
               count(*) FILTER (WHERE companies < 10) AS groups_under_10,
               round(median(companies), 0) AS median_companies
        FROM (SELECT g.peers, count(DISTINCT r.cik) AS companies
              FROM ref.industry_group g
              JOIN marts.ratio_values r ON r.sic = g.sic
              WHERE r.fy = 2024
              GROUP BY g.peers)""")
    heads = [d[0] for d in cur.description]
    print("  " + " | ".join(heads))
    for r in cur.fetchall():
        print("  " + " | ".join(str(v) for v in r))


if __name__ == "__main__":
    main()
