"""Why are 143 leverage covenants still recorded as floors?

A leverage covenant is a ceiling, so a floor is either a parse error or a genuinely
unusual term. Look at the sentences before deciding which, rather than moving the
threshold until the check passes.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")

    print("### Are the floors concentrated away from a covenant heading?")
    for row in con.execute("""
        SELECT near_covenant_heading, count(*) AS levels,
               count(*) FILTER (WHERE direction = 'min') AS floors,
               round(100.0 * count(*) FILTER (WHERE direction = 'min')
                     / count(*), 1) AS pct_floor
        FROM marts.covenant_terms WHERE covenant_type LIKE '%leverage%'
        GROUP BY 1 ORDER BY 1 DESC""").fetchall():
        print(f"  near_heading={row[0]}  levels={row[1]:,}  floors={row[2]:,} "
              f"({row[3]}%)")

    print("\n### What the floor sentences actually say")
    for cik, ctype, level, sentence in con.execute("""
        SELECT cik, covenant_type, level, sentence
        FROM marts.covenant_terms
        WHERE covenant_type LIKE '%leverage%' AND direction = 'min'
        ORDER BY hash(sentence) LIMIT 10""").fetchall():
        print(f"\n  [{cik}] {ctype} = {level}")
        print(f"    {sentence[:300]}")


if __name__ == "__main__":
    main()
