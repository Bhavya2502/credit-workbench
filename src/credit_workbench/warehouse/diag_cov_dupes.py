"""Did the sample run duplicate rows already in the covenant mart?"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    row = con.execute("""
        SELECT count(*) AS rows,
               count(DISTINCT (adsh, exhibit_number, covenant_type, direction,
                               level, level_index)) AS distinct_rows
        FROM marts.covenant_terms""").fetchone()
    print(f"  rows={row[0]:,}  distinct={row[1]:,}  duplicated={row[0] - row[1]:,}")


if __name__ == "__main__":
    main()
