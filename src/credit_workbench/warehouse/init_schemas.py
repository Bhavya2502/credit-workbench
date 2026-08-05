"""Tracker item A1 — create the warehouse database and schemas in MotherDuck.

Idempotent: every statement in warehouse/schema.sql uses IF NOT EXISTS, so this can
be re-run safely after schema additions. Runs on a GitHub Actions runner.
"""
from pathlib import Path

import duckdb

from credit_workbench.common.config import motherduck_token


def run() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    statements = [s.strip() for s in Path("warehouse/schema.sql").read_text().split(";")]
    executed = 0
    for stmt in statements:
        if stmt and not all(line.strip().startswith("--") or not line.strip()
                            for line in stmt.splitlines()):
            con.execute(stmt)
            executed += 1
    schemas = [r[0] for r in con.execute(
        "SELECT schema_name FROM information_schema.schemata "
        "WHERE catalog_name = 'credit_workbench' ORDER BY 1").fetchall()]
    tables = [f"{r[0]}.{r[1]}" for r in con.execute(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_catalog = 'credit_workbench' ORDER BY 1, 2").fetchall()]
    print(f"Executed {executed} statements against md:credit_workbench")
    print(f"Schemas: {', '.join(schemas)}")
    print(f"Tables:  {', '.join(tables) if tables else '(none yet)'}")


if __name__ == "__main__":
    run()
