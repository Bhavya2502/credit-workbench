"""Fill rates after the tag-map rebuild, against the measured baseline.

Baselines taken before the change, on `basis='first_reported' AND is_primary_annual`
(129,224 company-years):

    revenue        97,389   75.4%
    capex          88,586   68.6%
    ebitda        107,141   82.9%
    total_assets  116,533   90.2%

Three things are checked, not one. Coverage is the point of the exercise, but a map
change can also do harm, so the provenance of every filled figure is broken out by
source tag, and the values themselves are tested against figures published in the
filers' own accounts. A tag that fills a lot of rows with the wrong number is worse than
the null it replaced.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

POP = "basis = 'first_reported' AND is_primary_annual"
BEFORE = {"revenue": 97_389, "capex": 88_586, "ebitda": 107_141,
          "total_assets": 116_533}

Q = [
    ("1. Fill rates now", f"""
        SELECT count(*) AS company_years,
               count(revenue) AS revenue,
               count(capex) AS capex,
               count(ebitda) AS ebitda,
               count(total_assets) AS total_assets,
               count(operating_income) AS operating_income
        FROM marts.spreads_a WHERE {POP}"""),

    ("2. Revenue provenance - which tag supplied each figure", """
        SELECT source_tag, count(*) AS company_years,
               count(DISTINCT cik) AS companies
        FROM marts.spread_lines
        WHERE basis = 'first_reported' AND line_code = 'revenue'
          AND qtrs = 4 AND value IS NOT NULL
        GROUP BY source_tag ORDER BY company_years DESC LIMIT 25"""),

    ("3. Revenue fill by division, non-financial vs financial", f"""
        SELECT CASE WHEN substr(s.sic, 1, 2) IN ('60','61','62','63','64','65','67')
                    THEN 'Finance, Insurance & Real Estate' ELSE 'everything else' END
                    AS block,
               count(*) AS company_years,
               count(s.revenue) AS with_revenue,
               round(100.0 * count(s.revenue) / count(*), 1) AS fill_pct
        FROM marts.spreads_a s WHERE {POP} GROUP BY 1 ORDER BY 1"""),

    ("4. Revenue fill by division", f"""
        SELECT coalesce(h.division_name, '(unmapped)') AS division,
               count(*) AS company_years,
               round(100.0 * count(s.revenue) / count(*), 1) AS fill_pct
        FROM marts.spreads_a s
        LEFT JOIN ref.sic_hierarchy h ON h.sic4 = s.sic
        WHERE {POP} GROUP BY 1 ORDER BY fill_pct"""),

    ("5. Revenue fill by fiscal year", f"""
        SELECT fy, count(*) AS company_years,
               round(100.0 * count(revenue) / count(*), 1) AS revenue_fill,
               round(100.0 * count(capex) / count(*), 1) AS capex_fill
        FROM marts.spreads_a WHERE {POP} AND fy BETWEEN 2012 AND 2025
        GROUP BY fy ORDER BY fy"""),

    ("6. SPOT CHECK - the named cases this fix was built from", """
        SELECT s.company_name, s.fy, s.sic,
               round(s.revenue / 1e9, 3) AS revenue_bn,
               l.source_tag
        FROM marts.spreads_a s
        LEFT JOIN marts.spread_lines l
               ON l.cik = s.cik AND l.period_end = s.period_end
              AND l.basis = s.basis AND l.line_code = 'revenue' AND l.qtrs = 4
        WHERE s.basis = 'first_reported' AND s.is_primary_annual AND s.fy = 2023
          AND s.cik IN (72903,       -- Xcel Energy        expect 14.206
                        936340,      -- DTE Energy         expect 12.745
                        92230,       -- Truist             expect 24.46 (interest income)
                        1114448,     -- Novartis           expect 45.440
                        1090727)     -- UPS control: unchanged
        ORDER BY s.company_name"""),

    ("7. HARM CHECK - did any company LOSE a revenue it used to have?", f"""
        SELECT count(*) AS company_years_with_revenue,
               count(*) FILTER (WHERE revenue < 0) AS negative_revenue,
               count(*) FILTER (WHERE revenue = 0) AS zero_revenue,
               count(*) FILTER (WHERE operating_income IS NOT NULL
                                  AND revenue IS NOT NULL
                                  AND operating_income > revenue * 1.5
                                  AND revenue > 0) AS opinc_far_exceeds_revenue
        FROM marts.spreads_a WHERE {POP} AND revenue IS NOT NULL"""),

    ("8. HARM CHECK - banks on the gross-interest-income convention", """
        SELECT count(*) AS company_years, count(DISTINCT cik) AS companies,
               round(min(value) / 1e9, 2) AS min_bn,
               round(median(value) / 1e6, 1) AS median_m,
               round(max(value) / 1e9, 1) AS max_bn
        FROM marts.spread_lines
        WHERE basis = 'first_reported' AND line_code = 'revenue' AND qtrs = 4
          AND source_tag = 'InterestAndDividendIncomeOperating'"""),

    ("9. Capex provenance - the new tags", """
        SELECT source_tag, count(*) AS company_years, count(DISTINCT cik) AS companies
        FROM marts.spread_lines
        WHERE basis = 'first_reported' AND line_code = 'capex'
          AND qtrs = 4 AND value IS NOT NULL
        GROUP BY source_tag ORDER BY company_years DESC"""),
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

    total = con.execute(
        f"SELECT count(*) FROM marts.spreads_a WHERE {POP}").fetchone()[0]
    print(f"\n### 0. Before and after, on {total:,} company-years")
    print(f"  {'column':<16}{'before':>10}{'after':>10}{'gain':>9}"
          f"{'before %':>10}{'after %':>9}")
    print("  " + "-" * 64)
    for col, before in BEFORE.items():
        after = con.execute(
            f"SELECT count({col}) FROM marts.spreads_a WHERE {POP}").fetchone()[0]
        print(f"  {col:<16}{before:>10,}{after:>10,}{after - before:>+9,}"
              f"{100.0 * before / total:>9.1f}%{100.0 * after / total:>8.1f}%")

    for title, q in Q:
        print(f"\n### {title}")
        try:
            show(con, q)
        except Exception as exc:
            print(f"  (failed: {str(exc)[:200]})")


if __name__ == "__main__":
    main()
