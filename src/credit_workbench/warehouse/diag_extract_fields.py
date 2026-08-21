"""Can the requested extract be built, field by field?

Nine columns were asked for. Seven sit on `marts.spreads_a` and need nothing. Two do not,
and both need checking before a row is written rather than after.

**form_type.** `spreads_a` carries no form. `marts.spread_lines` does, but one per
*line*: the spread resolves each line independently and takes the best available filing
for it, so a single company-year can draw lines from a 10-K and its 10-K/A. Collapsing
that to one form per row is a choice, and if it is done with a join rather than an
aggregate the row count multiplies. Q1-Q3 measure how often forms disagree inside a
company-year, so the collapse rule is chosen on evidence.

**gics_sub_industry.** GICS is licensed from S&P and MSCI. Nothing in this warehouse
ingests it and `ref.sic_naics` was never populated. Q4 searches every column name in the
database for anything GICS- or NAICS-shaped, so the answer is "searched and absent"
rather than "I do not recall seeing one".
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

Q = [
    ("1. What forms does spread_lines carry for annual periods?", """
        SELECT form, count(*) AS lines, count(DISTINCT cik) AS companies
        FROM marts.spread_lines
        WHERE basis = 'first_reported' AND qtrs IN (0, 4)
        GROUP BY form ORDER BY lines DESC LIMIT 12"""),

    ("2. Does a company-period ever draw lines from more than one form?", """
        SELECT forms_per_period, count(*) AS company_periods
        FROM (SELECT cik, period_end, count(DISTINCT form) AS forms_per_period
              FROM marts.spread_lines
              WHERE basis = 'first_reported' AND qtrs IN (0, 4)
              GROUP BY cik, period_end)
        GROUP BY 1 ORDER BY 1"""),

    ("3. Picking the form that supplied the most lines - does it cover everything?", """
        WITH picked AS (
            SELECT cik, period_end,
                   arg_max(form, n) AS form_type,
                   sum(n) AS lines_total
            FROM (SELECT cik, period_end, form, count(*) AS n
                  FROM marts.spread_lines
                  WHERE basis = 'first_reported' AND qtrs IN (0, 4)
                  GROUP BY cik, period_end, form)
            GROUP BY cik, period_end)
        SELECT (SELECT count(*) FROM marts.spreads_a
                WHERE basis = 'first_reported' AND is_primary_annual) AS spread_rows,
               count(*) AS picked_rows,
               (SELECT count(*) FROM marts.spreads_a s
                JOIN picked p ON p.cik = s.cik AND p.period_end = s.period_end
                WHERE s.basis = 'first_reported' AND s.is_primary_annual)
                   AS rows_after_join
        FROM picked"""),

    ("4. GICS or NAICS - does any column anywhere hold one?", """
        SELECT table_schema, table_name, column_name
        FROM information_schema.columns
        WHERE lower(column_name) LIKE '%gics%'
           OR lower(column_name) LIKE '%naics%'
           OR lower(column_name) LIKE '%gsector%'
           OR lower(column_name) LIKE '%sub_industry%'
           OR lower(column_name) LIKE '%sector%'
        ORDER BY 1, 2, 3 LIMIT 25"""),

    ("5. And is ref.sic_naics still empty?", """
        SELECT count(*) AS rows FROM ref.sic_naics"""),

    ("6. Coverage of the requested measures, on the extract population", """
        SELECT count(*) AS company_years,
               count(revenue) AS revenue,
               count(ebitda) AS ebitda,
               count(operating_income) AS operating_income,
               count(ebit_calc) AS ebit_calc,
               count(dep_amort_is) AS dep_amort_is,
               count(dep_amort_cf) AS dep_amort_cf,
               count(total_assets) AS total_assets,
               count(capex) AS capex
        FROM marts.spreads_a
        WHERE basis = 'first_reported' AND is_primary_annual"""),

    ("7. Does ebitda reconcile to operating_income + D&A where both exist?", """
        WITH b AS (
            SELECT ebitda, ebit_calc,
                   coalesce(dep_amort_cf, dep_amort_is) AS da
            FROM marts.spreads_a
            WHERE basis = 'first_reported' AND is_primary_annual
              AND ebitda IS NOT NULL AND ebit_calc IS NOT NULL
              AND coalesce(dep_amort_cf, dep_amort_is) IS NOT NULL)
        SELECT count(*) AS comparable,
               count(*) FILTER (WHERE abs(ebitda - (ebit_calc + da))
                                      <= 0.01 * abs(nullif(ebitda, 0))) AS within_1_pct,
               round(100.0 * count(*) FILTER (WHERE abs(ebitda - (ebit_calc + da))
                                      <= 0.01 * abs(nullif(ebitda, 0))) / count(*), 1)
                   AS pct_agreeing
        FROM b"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)))[:48]
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
