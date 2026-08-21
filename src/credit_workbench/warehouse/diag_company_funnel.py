"""Reconcile the company count, exactly, at every stage from filer to spread.

The docs headline "21,671 companies", an earlier probe in this workstream reported about
18,200 from `approx_count_distinct`, and the statements workbook contains 15,550. Those
are three different numbers for three different populations, but only one of them was
measured exactly, and an approximate count carries no right to disagree with a documented
one - so this counts each stage exactly and states what each stage means.

The expensive query is the exact `count(DISTINCT cik)` over `staging.facts_pit`, 373m
rows. It is run once, on one column, because the whole question turns on it.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

Q = [
    ("1. The funnel, counted exactly", """
        SELECT
          (SELECT count(*) FROM ref.dim_company) AS sec_entities_known,
          (SELECT count(DISTINCT cik) FROM quali.filing_sections) AS companies_with_filings,
          (SELECT count(DISTINCT cik) FROM staging.facts_pit) AS companies_with_xbrl_facts,
          (SELECT count(DISTINCT cik) FROM marts.spreads_a) AS companies_with_any_spread,
          (SELECT count(DISTINCT cik) FROM marts.spreads_a
           WHERE basis = 'first_reported') AS companies_in_workbook"""),

    ("2. Of companies with facts, how many ever filed ANNUAL statement facts?", """
        SELECT count(DISTINCT cik) AS with_any_fact,
               count(DISTINCT cik) FILTER (WHERE qtrs = 4) AS with_annual_flows,
               count(DISTINCT cik) FILTER (WHERE stmt IN ('IS','BS','CF'))
                   AS with_face_statement_facts,
               count(DISTINCT cik) FILTER (WHERE stmt = 'IS') AS with_income_statement
        FROM staging.facts_pit"""),

    ("3. Companies with facts but no spread - what is missing for them?", """
        WITH nospread AS (
            SELECT DISTINCT cik FROM staging.facts_pit
            WHERE cik NOT IN (SELECT cik FROM marts.spreads_a))
        SELECT count(*) AS companies,
               count(*) FILTER (WHERE has_is) AS have_income_statement_facts,
               count(*) FILTER (WHERE has_annual) AS have_annual_flows
        FROM (SELECT n.cik,
                     bool_or(f.stmt = 'IS') AS has_is,
                     bool_or(f.qtrs = 4) AS has_annual
              FROM nospread n JOIN staging.facts_pit f ON f.cik = n.cik
              GROUP BY n.cik)"""),

    ("4. Those companies, by entity type and filer category", """
        WITH nospread AS (
            SELECT DISTINCT cik FROM staging.facts_pit
            WHERE cik NOT IN (SELECT cik FROM marts.spreads_a))
        SELECT coalesce(d.entity_type, '(none)') AS entity_type,
               coalesce(d.sic_description, '(no sic)') AS industry,
               count(*) AS companies
        FROM nospread n
        LEFT JOIN ref.dim_company d ON d.cik = TRY_CAST(n.cik AS BIGINT)
        GROUP BY 1, 2 ORDER BY companies DESC LIMIT 15"""),

    ("5. And how many of them are recent, versus long-dead registrants?", """
        WITH nospread AS (
            SELECT DISTINCT cik FROM staging.facts_pit
            WHERE cik NOT IN (SELECT cik FROM marts.spreads_a))
        SELECT max_fy, count(*) AS companies FROM (
            SELECT n.cik, max(f.fy) AS max_fy
            FROM nospread n JOIN staging.facts_pit f ON f.cik = n.cik
            GROUP BY n.cik)
        GROUP BY 1 ORDER BY 1 DESC LIMIT 12"""),
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
