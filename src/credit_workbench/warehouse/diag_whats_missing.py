"""What the workbook holds against what the warehouse holds - and is spreads_a stale?

Two questions, both raised by the exported workbook.

**How much of the warehouse is in it.** The workbook carries the face financial
statements, the industry bridge and the outcome labels. The notes to accounts, the
segments, the covenants, the debt instruments, the governance extracts and the 8-K
events are not in it. Q1 sizes every table so the omission can be measured rather than
described.

**Was it built on the wrong table?** `DATA_GUIDE.md` flags `marts.spreads_a` as
superseded by `marts.spread_lines`, and the workbook's Values sheet came from
`spreads_a`. Q2-Q5 test whether that costs anything real: whether the long table covers
more company-years, more line items or more values, and whether the two disagree on a
figure that can be checked against a published account.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

# The columns of spreads_a that carry a figure - the workbook's 120 line items.
EXCLUDE = "('cik','company_name','sic','basis','period_end','fy','last_filed','ebit_source')"

Q = [
    ("1. Every table in the warehouse, by size - metadata only", """
        SELECT schema_name AS schema, table_name, estimated_size AS approx_rows,
               column_count AS columns
        FROM duckdb_tables()
        WHERE database_name = 'credit_workbench'
          AND schema_name NOT IN ('information_schema', 'pg_catalog')
        ORDER BY approx_rows DESC NULLS LAST LIMIT 45"""),

    ("2. spread_lines vs spreads_a - the same population?", """
        SELECT 'spread_lines annual first_reported' AS source,
               count(*) AS values_held,
               count(DISTINCT cik) AS companies,
               count(DISTINCT (cik, period_end)) AS company_periods,
               count(DISTINCT line_code) AS line_items,
               min(fy) AS first_fy, max(fy) AS last_fy
        FROM marts.spread_lines
        WHERE basis = 'first_reported' AND qtrs IN (0, 4) AND value IS NOT NULL"""),

    ("3. spread_lines - all bases and frequencies, for scale", """
        SELECT basis, qtrs, count(*) AS rows, count(DISTINCT cik) AS companies
        FROM marts.spread_lines GROUP BY 1, 2 ORDER BY rows DESC LIMIT 10"""),

    ("4. Line codes in spread_lines that are NOT a column of spreads_a", f"""
        WITH cols AS (
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'marts' AND table_name = 'spreads_a'
              AND column_name NOT IN {EXCLUDE}),
        lines AS (
            SELECT line_code, any_value(label) AS label, any_value(statement) AS stmt,
                   count(*) AS values_held
            FROM marts.spread_lines
            WHERE basis = 'first_reported' AND value IS NOT NULL
            GROUP BY line_code)
        SELECT l.line_code, l.label, l.stmt, l.values_held
        FROM lines l LEFT JOIN cols c ON c.column_name = l.line_code
        WHERE c.column_name IS NULL
        ORDER BY l.values_held DESC LIMIT 20"""),

    ("5. Columns of spreads_a that are NOT a line code in spread_lines", f"""
        WITH cols AS (
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'marts' AND table_name = 'spreads_a'
              AND column_name NOT IN {EXCLUDE}),
        lines AS (SELECT DISTINCT line_code FROM marts.spread_lines)
        SELECT c.column_name
        FROM cols c LEFT JOIN lines l ON l.line_code = c.column_name
        WHERE l.line_code IS NULL ORDER BY 1"""),

    ("6. Do they agree? Apple revenue, both tables, last five years", """
        SELECT a.fy,
               a.revenue AS spreads_a_revenue,
               l.value   AS spread_lines_revenue,
               a.revenue = l.value AS agrees
        FROM marts.spreads_a a
        LEFT JOIN marts.spread_lines l
               ON l.cik = a.cik AND l.period_end = a.period_end
              AND l.basis = a.basis AND l.line_code = 'revenue'
        WHERE a.cik = 320193 AND a.basis = 'first_reported' AND a.fy >= 2021
        ORDER BY a.fy DESC"""),

    ("7. The notes to accounts - what is actually held", """
        SELECT 'facts_by_note' AS table, count(*) AS rows,
               count(DISTINCT cik) AS companies, count(DISTINCT note_title_normalised) AS notes
        FROM marts.facts_by_note"""),

    ("8. Note text and narrative sections", """
        SELECT 'quali.filing_sections' AS source, count(*) AS rows,
               count(DISTINCT cik) AS companies FROM quali.filing_sections
        UNION ALL SELECT 'quali.risk_factors', count(*), count(DISTINCT cik)
        FROM quali.risk_factors
        UNION ALL SELECT 'quali.proxy_sections', count(*), count(DISTINCT cik)
        FROM quali.proxy_sections
        UNION ALL SELECT 'marts.credit_events', count(*), count(DISTINCT cik)
        FROM marts.credit_events
        UNION ALL SELECT 'marts.covenant_terms', count(*), count(DISTINCT cik)
        FROM marts.covenant_terms
        UNION ALL SELECT 'marts.debt_instruments', count(*), count(DISTINCT cik)
        FROM marts.debt_instruments"""),
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
    con.execute("SET temp_directory = '/tmp/duckdb_spill'")
    for title, q in Q:
        print(f"\n### {title}")
        try:
            show(con, q)
        except Exception as exc:
            print(f"  (failed: {str(exc)[:200]})")


if __name__ == "__main__":
    main()
