"""Is a null revenue ever our fault? Five hypotheses, tested against named companies.

The claim under test is that revenue should be close to 100% populated and that 24.6%
nulls indicates a defect rather than a disclosure fact. The previous decomposition
concluded most nulls were genuine, but it classified them using `facts_pit.stmt` - the
fact's own statement label - while the spread builder resolves lines using the statement
from `staging.tag_map`. If those disagree the earlier conclusion is unsafe, so this
starts again from hypotheses that can each be falsified:

  H1  zeros are being stored as null. A pre-revenue filer tagging Revenues = 0 would
      then look identical to one that disclosed nothing.
  H2  the first_reported basis is the cause. A figure absent from the first filing and
      added in a later one is null on this basis and present on `latest`.
  H3  the revenue line's tag list is too narrow, so a real revenue tag goes unclaimed.
  H4  the period shape is wrong - annual revenue tagged with qtrs other than 4, or a
      52/53-week year falling outside the annual bucket.
  H5  the concept genuinely is not disclosed.

Q6 is the decisive one. It takes the largest companies by total assets that have a null
revenue and prints what they actually filed, tag by tag. If a real operating company
with real revenue appears there, the fault is ours and the rest of this is noise.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

POP = "is_primary_annual"

Q = [
    ("H2. Null rate by basis - does 'latest' fill what 'first_reported' does not?", f"""
        SELECT basis, count(*) AS company_years,
               round(100.0 * count(*) FILTER (WHERE revenue IS NULL) / count(*), 1)
                   AS revenue_null_pct,
               round(100.0 * count(*) FILTER (WHERE total_assets IS NULL) / count(*), 1)
                   AS assets_null_pct,
               round(100.0 * count(*) FILTER (WHERE capex IS NULL) / count(*), 1)
                   AS capex_null_pct
        FROM marts.spreads_a WHERE {POP} GROUP BY basis"""),

    ("H1. Are zeros preserved, or dropped to null?", f"""
        SELECT count(*) AS company_years,
               count(*) FILTER (WHERE revenue = 0) AS revenue_exactly_zero,
               count(*) FILTER (WHERE revenue IS NULL) AS revenue_null,
               count(*) FILTER (WHERE capex = 0) AS capex_exactly_zero,
               count(*) FILTER (WHERE total_assets = 0) AS assets_exactly_zero
        FROM marts.spreads_a WHERE {POP} AND basis = 'first_reported'"""),

    ("H3. Which tags does the template accept for revenue?", """
        SELECT priority, tag, statement
        FROM staging.tag_map WHERE line_code = 'revenue' ORDER BY priority"""),

    ("H4/H5. FY2023 revenue nulls - what annual IS facts do they actually carry?", f"""
        WITH nulls AS (
            SELECT cik, period_end FROM marts.spreads_a
            WHERE {POP} AND basis = 'first_reported' AND fy = 2023
              AND revenue IS NULL AND NOT is_empty_spread)
        SELECT f.tag,
               count(DISTINCT f.cik) AS companies,
               count(*) FILTER (WHERE m.tag IS NOT NULL) AS facts_we_map,
               any_value(f.qtrs) AS example_qtrs
        FROM nulls n
        JOIN staging.facts_pit f ON f.cik = n.cik AND f.period_end = n.period_end
        LEFT JOIN staging.tag_map m ON m.tag = f.tag
        WHERE f.stmt = 'IS'
        GROUP BY f.tag ORDER BY companies DESC LIMIT 20"""),

    ("H4. Do the null companies tag revenue with a non-annual qtrs?", f"""
        WITH nulls AS (
            SELECT cik, period_end FROM marts.spreads_a
            WHERE {POP} AND basis = 'first_reported' AND fy = 2023
              AND revenue IS NULL AND NOT is_empty_spread)
        SELECT f.qtrs, count(DISTINCT f.cik) AS companies, count(*) AS facts
        FROM nulls n
        JOIN staging.facts_pit f ON f.cik = n.cik AND f.period_end = n.period_end
        JOIN staging.tag_map m ON m.tag = f.tag AND m.line_code = 'revenue'
        GROUP BY f.qtrs ORDER BY companies DESC"""),

    ("H-DECISIVE. The biggest null-revenue companies, and what they filed", f"""
        WITH nulls AS (
            SELECT cik, company_name, total_assets, sic FROM marts.spreads_a
            WHERE {POP} AND basis = 'first_reported' AND fy = 2023
              AND revenue IS NULL AND total_assets IS NOT NULL
            ORDER BY total_assets DESC LIMIT 15)
        SELECT n.company_name, n.sic,
               round(n.total_assets / 1e9, 1) AS assets_bn,
               count(DISTINCT f.tag) FILTER (WHERE f.stmt = 'IS' AND f.qtrs = 4)
                   AS annual_is_tags,
               max(CASE WHEN f.tag IN ('Revenues',
                        'RevenueFromContractWithCustomerExcludingAssessedTax',
                        'SalesRevenueNet') AND f.qtrs = 4
                        THEN round(f.value / 1e9, 2) END) AS revenue_tag_value_bn,
               max(CASE WHEN f.tag = 'InterestAndDividendIncomeOperating' AND f.qtrs = 4
                        THEN round(f.value / 1e9, 2) END) AS interest_income_bn
        FROM nulls n
        LEFT JOIN staging.facts_pit f ON f.cik = n.cik AND f.fy = 2023
        GROUP BY 1, 2, 3 ORDER BY assets_bn DESC"""),

    ("H-DECISIVE 2. Of FY2023 null-revenue companies, how many filed a mapped "
     "revenue tag at ANY qtrs for that period?", f"""
        WITH nulls AS (
            SELECT cik, period_end FROM marts.spreads_a
            WHERE {POP} AND basis = 'first_reported' AND fy = 2023
              AND revenue IS NULL AND NOT is_empty_spread)
        SELECT count(DISTINCT n.cik) AS null_companies,
               count(DISTINCT f.cik) AS have_a_mapped_revenue_fact_somewhere,
               count(DISTINCT f.cik) FILTER (WHERE f.qtrs = 4)
                   AS have_it_at_annual_qtrs
        FROM nulls n
        LEFT JOIN staging.facts_pit f
               ON f.cik = n.cik AND f.period_end = n.period_end
        LEFT JOIN staging.tag_map m ON m.tag = f.tag AND m.line_code = 'revenue'
        WHERE m.tag IS NOT NULL OR f.cik IS NULL"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)))[:54]
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
