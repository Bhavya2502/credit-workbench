"""Tracker item A1 — create the warehouse database and schemas in MotherDuck.

Idempotent: every statement in warehouse/schema.sql uses IF NOT EXISTS, so this can
be re-run safely after schema additions. Runs on a GitHub Actions runner.
"""
from pathlib import Path

import duckdb

from credit_workbench.common.config import motherduck_token


def run() -> None:
    con = duckdb.connect(f"md:?motherduck_token={motherduck_token()}")
    con.execute("CREATE DATABASE IF NOT EXISTS credit_workbench")
    con.execute("USE credit_workbench")
    sql_text = "\n".join(
        line for line in Path("warehouse/schema.sql").read_text().splitlines()
        if not line.lstrip().startswith("--")
    )
    executed = 0
    for stmt in (s.strip() for s in sql_text.split(";")):
        if stmt:
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
