"""Industry tagging, default flags and KPI coverage — measured, per company.

Three direct questions, answered from the warehouse rather than from the documentation.

**Is industry tagged for every company?** SIC arrives on the filing itself, assigned by SEC,
so coverage should be near-total - but "tagged" and "tagged correctly" are different claims
and only the first is measurable here. What can be checked is how often a company's SIC
changes between years, since a code that moves is a code someone re-assigned.

**Is there a default flag, and how is it derived?** `marts.credit_outcomes` builds it from
the 8-K event feed at severity 5, measured from the *filing date* rather than the period end.
The counts below are what that produces.

**Are there company KPIs?** `marts.disclosed_kpis` holds them where a filer stated one in
prose. Airlines are the worked example: a small industry, high within-industry coverage, and
metrics whose plausible range is well known, so a reader can judge the values rather than
take them on trust.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token


def show(con, title, q):
    print(f"\n### {title}")
    try:
        cur = con.execute(q)
        heads = [d[0] for d in cur.description]
        rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)))[:58]
                 for v in r] for r in cur.fetchall()]
        if not rows:
            print("  (no rows)")
            return
        w = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(heads)]
        print("  " + "  ".join(h.ljust(x) for h, x in zip(heads, w)))
        print("  " + "  ".join("-" * x for x in w))
        for r in rows:
            print("  " + "  ".join(v.ljust(x) for v, x in zip(r, w)))
    except Exception as exc:
        print(f"  (failed: {str(exc)[:190]})")


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")

    # ---- (a) industry
    show(con, "a1. Industry tagging across companies that have financials", """
        SELECT count(DISTINCT cik) AS companies,
               count(DISTINCT cik) FILTER (WHERE sic IS NOT NULL AND sic <> '')
                   AS with_sic,
               count(DISTINCT cik) FILTER (WHERE sic2 IS NOT NULL AND sic2 <> '')
                   AS with_sic2,
               count(DISTINCT cik) FILTER (WHERE sic IN
                   (SELECT sic4 FROM ref.industry_group)) AS with_peer_group
        FROM marts.ratio_values""")

    # A code that moves between years is a code somebody re-assigned. It is the only
    # measurable proxy for correctness available without an external reference.
    show(con, "a2. Does a company's SIC stay put? (stability, not accuracy)", """
        SELECT codes_per_company, count(*) AS companies
        FROM (SELECT cik, count(DISTINCT sic) AS codes_per_company
              FROM marts.ratio_values GROUP BY cik)
        GROUP BY 1 ORDER BY 1 LIMIT 5""")

    show(con, "a3. Companies with no peer-group mapping — which SIC codes are they?", """
        SELECT r.sic, any_value(c.sic_description) AS description,
               count(DISTINCT r.cik) AS companies
        FROM marts.ratio_values r
        LEFT JOIN ref.dim_company c ON c.cik = r.cik
        WHERE r.sic NOT IN (SELECT sic4 FROM ref.industry_group)
        GROUP BY r.sic ORDER BY companies DESC LIMIT 8""")

    # ---- (b) default flag
    show(con, "b1. What the default and distress flags actually contain", """
        SELECT count(*) AS company_years, count(DISTINCT cik) AS companies,
               count(*) FILTER (WHERE default_12m) AS default_12m,
               count(*) FILTER (WHERE default_24m) AS default_24m,
               count(*) FILTER (WHERE bankruptcy_24m) AS bankruptcy_24m,
               count(*) FILTER (WHERE debt_acceleration_24m) AS debt_accel_24m,
               count(*) FILTER (WHERE non_reliance_24m) AS non_reliance_24m,
               count(*) FILTER (WHERE distress_24m) AS distress_24m
        FROM marts.credit_outcomes""")

    show(con, "b2. Which 8-K events produce a default, and how often", """
        SELECT first_event_category AS category, count(*) AS company_years,
               round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
        FROM marts.credit_outcomes
        WHERE default_24m GROUP BY 1 ORDER BY company_years DESC LIMIT 8""")

    # The check that the label is not contaminated: default must separate by leverage.
    show(con, "b3. Does the default flag separate by leverage? (label sanity)", """
        WITH lev AS (
            SELECT o.cik, o.fy, o.default_24m,
                   ntile(5) OVER (ORDER BY r.value) AS leverage_quintile
            FROM marts.credit_outcomes o
            JOIN marts.ratio_values r
              ON r.cik = o.cik AND r.fy = o.fy AND r.basis = 'first_reported'
             AND r.ratio = 'debt_to_ebitda' AND r.value IS NOT NULL
             AND r.value BETWEEN 0 AND 50)
        SELECT leverage_quintile, count(*) AS company_years,
               round(100.0 * count(*) FILTER (WHERE default_24m) / count(*), 2)
                   AS pct_default_24m
        FROM lev GROUP BY 1 ORDER BY 1""")

    # ---- (c) KPIs, airlines
    show(con, "c1. Airline KPI values, with the company and the sentence", """
        SELECT c.company_name, k.fy, k.kpi, round(k.value, 2) AS value, k.unit,
               k.confidence, substr(k.source_sentence, 1, 88) AS sentence
        FROM marts.disclosed_kpis k
        LEFT JOIN ref.dim_company c ON c.cik = k.cik
        WHERE k.kpi IN ('airline_load_factor', 'airline_rasm')
        ORDER BY c.company_name, k.fy LIMIT 14""")

    show(con, "c2. KPI coverage for airlines — how many of them have anything", """
        SELECT (SELECT count(DISTINCT cik) FROM marts.ratio_values
                WHERE sic2 = '45' AND fy >= 2019) AS airlines_with_financials,
               (SELECT count(DISTINCT k.cik) FROM marts.disclosed_kpis k
                WHERE k.kpi LIKE 'airline%') AS airlines_with_a_kpi""")

    show(con, "c3. And the same question across every KPI in the dictionary", """
        SELECT d.kpi, d.measured_industry_coverage_pct AS mentioned_pct,
               count(DISTINCT k.cik) AS companies_with_a_value
        FROM ref.kpi_dictionary d
        LEFT JOIN marts.disclosed_kpis k ON k.kpi = d.kpi
        GROUP BY 1, 2 ORDER BY companies_with_a_value DESC LIMIT 20""")


if __name__ == "__main__":
    main()
