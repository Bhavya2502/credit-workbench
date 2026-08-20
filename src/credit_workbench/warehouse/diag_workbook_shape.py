"""Shape probe for the company/line-item workbook - metadata only, no data scans.

Excel takes 1,048,576 rows and 16,384 columns per sheet, so the workbook's design is
decided by counts this asks for rather than by guesses: how wide `marts.spreads_a` is,
how many line codes the template defines, and how many rows each sheet would carry.
Everything here reads `information_schema` or a single count, so it costs almost nothing
against the daily compute limit.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

TABLES = [
    ("marts", "spreads_a"), ("marts", "spread_coverage"), ("marts", "credit_outcomes"),
    ("marts", "ratio_values"), ("ref", "dim_company"), ("ref", "industry_group"),
    ("ref", "sic_hierarchy"), ("ref", "company_tickers"), ("staging", "tag_map"),
]

Q = [
    ("1. Column count per table", """
        SELECT table_schema, table_name, count(*) AS columns
        FROM information_schema.columns
        WHERE (table_schema, table_name) IN (
            ('marts','spreads_a'), ('marts','spread_coverage'), ('marts','credit_outcomes'),
            ('marts','ratio_values'), ('ref','dim_company'), ('ref','industry_group'),
            ('ref','sic_hierarchy'), ('ref','company_tickers'), ('staging','tag_map'))
        GROUP BY 1, 2 ORDER BY 1, 2"""),

    ("2. marts.spreads_a - every column, in order", """
        SELECT ordinal_position AS pos, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'marts' AND table_name = 'spreads_a'
        ORDER BY ordinal_position"""),

    ("3. The spread template - how many line items, by statement", """
        SELECT statement, count(DISTINCT line_code) AS line_codes,
               count(*) AS tag_alternatives
        FROM staging.tag_map GROUP BY 1 ORDER BY line_codes DESC"""),

    ("4. Sheet sizes - rows each candidate sheet would carry", """
        SELECT 'companies' AS sheet,
               (SELECT count(DISTINCT cik) FROM marts.credit_outcomes) AS rows
        UNION ALL SELECT 'company_years (outcomes)',
               (SELECT count(*) FROM marts.credit_outcomes)
        UNION ALL SELECT 'spreads_a first_reported',
               (SELECT count(*) FROM marts.spreads_a WHERE basis = 'first_reported')
        UNION ALL SELECT 'spreads_a latest',
               (SELECT count(*) FROM marts.spreads_a WHERE basis = 'latest')
        UNION ALL SELECT 'template lines',
               (SELECT count(DISTINCT line_code) FROM staging.tag_map)
        UNION ALL SELECT 'ratio names',
               (SELECT count(DISTINCT ratio) FROM marts.ratio_values)
        UNION ALL SELECT 'industry_group rows',
               (SELECT count(*) FROM ref.industry_group)"""),

    ("5. What `basis` values does spreads_a actually carry?", """
        SELECT basis, count(*) AS rows, count(DISTINCT cik) AS companies,
               min(fy) AS first_fy, max(fy) AS last_fy
        FROM marts.spreads_a GROUP BY 1 ORDER BY rows DESC"""),

    ("6. Tickers - is the join one-to-one or does a company carry several?", """
        SELECT tickers_per_company, count(*) AS companies FROM (
            SELECT cik, count(DISTINCT ticker) AS tickers_per_company
            FROM ref.company_tickers GROUP BY cik)
        GROUP BY 1 ORDER BY 1 LIMIT 6"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)))[:60]
             for v in r] for r in cur.fetchall()]
    if not rows:
        print("  (no rows)")
        return
    w = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(heads)]
    print("  " + "  ".join(h.ljust(x) for h, x in zip(heads, w)))
    print("  " + "  ".join("-" * x for x in w))
    for r in rows:
        print("  " + "  ".join(v.ljust(x) for v, x in zip(r, w)))


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    for title, q in Q:
        print(f"\n### {title}")
        try:
            show(con, q)
        except Exception as exc:
            print(f"  (failed: {str(exc)[:200]})")


if __name__ == "__main__":
    main()
