"""One-off diagnostic: which tags does a company use for a given concept?

    uv run python -m credit_workbench.warehouse.diag_tags --cik 104169 --like %epreciat%
"""
from __future__ import annotations

import argparse

import duckdb

from credit_workbench.common.config import motherduck_token


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cik", default="104169")
    ap.add_argument("--like", default="%epreciat%")
    args = ap.parse_args()

    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    cur = con.execute(f"""
        SELECT f.tag, f.stmt, f.qtrs, count(*) AS facts,
               max(f.period_end) AS latest_period,
               round(max(abs(f.value)) / 1e6) AS max_usd_mm,
               (m.tag IS NOT NULL) AS is_mapped
        FROM staging.facts_pit f
        LEFT JOIN staging.tag_map m ON m.tag = f.tag
        WHERE f.cik = {int(args.cik)} AND f.is_latest AND f.uom = 'USD'
          AND (f.tag LIKE '{args.like}' OR f.tag LIKE '%mortizat%')
          AND f.qtrs IN (0, 4)
        GROUP BY 1, 2, 3, m.tag IS NOT NULL
        ORDER BY facts DESC LIMIT 25""")
    headers = [d[0] for d in cur.description]
    rows = [[str(v) for v in r] for r in cur.fetchall()]
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    print("  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        print("  " + "  ".join(v.ljust(w) for v, w in zip(r, widths)))


if __name__ == "__main__":
    main()
