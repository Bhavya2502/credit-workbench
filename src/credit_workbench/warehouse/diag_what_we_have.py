"""What is actually in `credit_workbench`, and what merely sits next to it.

Two questions this answers, because the gap work blurred them.

**What do we have?** A measured inventory of `credit_workbench` by schema, with the row
count of every substantial object. This warehouse is corporate credit from SEC filings and
nothing else.

**What is `credit_data`?** A separate database reachable by the same token, belonging to
another workstream. It came into this conversation only because gap G-13 asked for a
catalogue of it. Nothing in it feeds anything here, and the naming warrants care: one of its
tables is presented as an "India retail PD panel" while sitting in a schema otherwise full of
Kaggle competition downloads, at a row count and date window that match a public vehicle-loan
competition set. This checks the columns rather than asserting the resemblance, because a
public competition file relabelled as a PD panel would mislead anyone who built on it.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")

    print("### 1. credit_workbench — what this project actually holds")
    try:
        cur = con.execute("""
            SELECT table_schema, count(*) AS objects
            FROM information_schema.tables
            WHERE table_catalog = 'credit_workbench'
            GROUP BY 1 ORDER BY 2 DESC""")
        for r in cur.fetchall():
            print(f"  {r[0]:<12} {r[1]:>4} objects")
    except Exception as exc:
        print(f"  (failed: {str(exc)[:180]})")

    print("\n### 2. The substantial objects, by row count")
    tables = [
        "staging.facts_pit", "marts.facts_dimensioned", "marts.spread_lines",
        "marts.ratio_values", "marts.ratio_coverage", "marts.segments",
        "marts.concentration", "marts.adjusted_metrics", "marts.risk_themes",
        "marts.control_signals", "marts.governance_metrics", "marts.credit_outcomes",
        "marts.credit_events", "marts.debt_instruments", "marts.covenant_terms",
        "marts.cohort_members", "marts.disclosed_kpis", "marts.outcome_counts",
        "quali.filing_sections", "quali.proxy_sections", "ref.filing_index",
        "ref.dim_company", "ref.company_names", "ref.industry_group",
    ]
    for t in tables:
        try:
            n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            print(f"  {t:<34} {n:>15,}")
        except Exception:  # noqa: BLE001  absent is a finding
            print(f"  {t:<34} {'ABSENT':>15}")

    print("\n### 3. Coverage of the SEC universe this warehouse is built on")
    try:
        cur = con.execute("""
            SELECT (SELECT count(DISTINCT cik) FROM ref.dim_company) AS companies_known,
                   (SELECT count(DISTINCT cik) FROM marts.ratio_values) AS with_financials,
                   (SELECT count(*) FROM ref.filing_index) AS filings_indexed,
                   (SELECT min(fy) FROM marts.ratio_values) AS first_fy,
                   (SELECT max(fy) FROM marts.ratio_values) AS last_fy""")
        for h, v in zip([d[0] for d in cur.description], cur.fetchone()):
            print(f"  {h:<20} {v:,}")
    except Exception as exc:
        print(f"  (failed: {str(exc)[:180]})")

    # The naming question. A public competition file relabelled as a PD panel would
    # mislead anyone who built a methodology on it, so the columns are read rather than
    # the resemblance assumed.
    print("\n### 4. Is credit_data.gold.india_retail_pd_panel a Kaggle download?")
    try:
        cols = [r[0] for r in con.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_catalog = 'credit_data' AND table_schema = 'gold'
              AND table_name = 'india_retail_pd_panel'
            ORDER BY ordinal_position""").fetchall()]
        print(f"  {len(cols)} columns: {', '.join(cols)}")
    except Exception as exc:
        print(f"  (failed: {str(exc)[:180]})")

    print("\n### 5. What else is in that schema, for context")
    try:
        cur = con.execute("""
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_catalog = 'credit_data'
              AND (lower(table_name) LIKE '%kaggle%' OR lower(table_name) LIKE '%india%'
                OR lower(table_name) LIKE '%bondora%')
            ORDER BY 1, 2""")
        rows = cur.fetchall()
        print(f"  {len(rows)} objects that are downloads or India-labelled:")
        for r in rows[:24]:
            print(f"    {r[0]}.{r[1]}")
        if len(rows) > 24:
            print(f"    ... and {len(rows) - 24} more")
    except Exception as exc:
        print(f"  (failed: {str(exc)[:180]})")

    print("\n### 6. Does anything in credit_workbench read from credit_data?")
    try:
        cur = con.execute("""
            SELECT count(*) AS views_referencing_credit_data
            FROM duckdb_views()
            WHERE database_name = 'credit_workbench'
              AND lower(sql) LIKE '%credit_data%'""")
        for h, v in zip([d[0] for d in cur.description], cur.fetchone()):
            print(f"  {h:<38} {v}")
    except Exception as exc:
        print(f"  (failed: {str(exc)[:180]})")


if __name__ == "__main__":
    main()
