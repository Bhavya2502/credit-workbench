"""Industry-wise default rate and count, at three levels of grouping.

`marts.credit_outcomes` is one row per company-year, dated when the accounts were filed,
carrying `default_24m` - a severity-5 8-K within 24 months of that filing date, meaning
bankruptcy (item 1.03), debt acceleration (2.04) or a non-reliance declaration (4.02).
It is an observed-event label, not an agency default.

Two things decide whether the rate printed here is the right number.

**The window is right-censored.** A company-year observed less than 24 months before the
event feed ends cannot have had its full window observed, so its zero means "not yet"
rather than "no". Those years drag every pooled rate down. Both cuts are printed - all
years, and full-window years only - so the difference is visible rather than assumed.

**The denominator is company-years, not companies.** A company filing for twelve years
contributes twelve observations, and one default is counted in up to two of them because
consecutive 24-month windows overlap. That is the right denominator for a per-annum rate
but it is not "share of companies that defaulted"; the distinct-company count is printed
alongside so neither is mistaken for the other.

Q7 checks this aggregation against the pre-built `marts.outcome_counts`, which a
different module built from the same source. The two must agree exactly on the sic2 cut.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

# A company-year whose 24-month window has fully elapsed inside the observed event feed.
FULL = ("observation_date <= (SELECT max(observation_date) FROM marts.credit_outcomes)"
        " - INTERVAL 730 DAY")

Q = [
    ("1. Grain and totals - what the denominator actually is", f"""
        SELECT count(*) AS company_years,
               count(DISTINCT cik) AS companies,
               count(DISTINCT (cik, fy)) AS distinct_cik_fy,
               min(observation_date) AS first_obs,
               max(observation_date) AS last_obs,
               count(*) FILTER (WHERE default_24m) AS defaults,
               count(DISTINCT cik) FILTER (WHERE default_24m) AS companies_defaulting,
               round(100.0 * count(*) FILTER (WHERE default_24m) / count(*), 2) AS rate_pct,
               count(*) FILTER (WHERE {FULL}) AS full_window_years
        FROM marts.credit_outcomes"""),

    ("2. Is the tail censored? Rate by observation year", """
        SELECT year(observation_date) AS obs_year,
               count(*) AS company_years,
               count(*) FILTER (WHERE default_24m) AS defaults,
               round(100.0 * count(*) FILTER (WHERE default_24m) / count(*), 2) AS rate_pct
        FROM marts.credit_outcomes
        WHERE observation_date >= DATE '2010-01-01'
        GROUP BY 1 ORDER BY 1"""),

    ("3. How much of the population can be tagged to an industry at all?", """
        SELECT count(*) AS company_years,
               count(*) FILTER (WHERE co.sic IS NOT NULL AND co.sic <> '') AS with_sic4,
               count(*) FILTER (WHERE co.sic2 IS NOT NULL AND co.sic2 <> '') AS with_sic2,
               count(*) FILTER (WHERE g.industry_code IS NOT NULL) AS with_peer_group
        FROM marts.credit_outcomes co
        LEFT JOIN ref.industry_group g ON g.sic4 = co.sic"""),

    ("4. Division level - the ten-way cut, full-window years only", f"""
        SELECT coalesce(h.division_name, '(unmapped)') AS division,
               count(*) AS company_years,
               count(DISTINCT co.cik) AS companies,
               count(*) FILTER (WHERE co.default_24m) AS defaults,
               round(100.0 * count(*) FILTER (WHERE co.default_24m) / count(*), 2)
                   AS default_rate_pct,
               count(*) FILTER (WHERE co.distress_24m) AS distress,
               round(100.0 * count(*) FILTER (WHERE co.distress_24m) / count(*), 2)
                   AS distress_rate_pct
        FROM marts.credit_outcomes co
        LEFT JOIN ref.sic_hierarchy h ON h.sic4 = co.sic
        WHERE {FULL}
        GROUP BY 1 ORDER BY default_rate_pct DESC"""),

    ("5. SIC2 major group - full-window years, worst rate first", f"""
        SELECT co.sic2,
               mode(h.sic4_description) AS example_industry,
               count(*) AS company_years,
               count(DISTINCT co.cik) AS companies,
               count(*) FILTER (WHERE co.default_24m) AS defaults,
               round(100.0 * count(*) FILTER (WHERE co.default_24m) / count(*), 2)
                   AS default_rate_pct,
               count(*) FILTER (WHERE co.default_24m) >= 30 AS can_calibrate
        FROM marts.credit_outcomes co
        LEFT JOIN ref.sic_hierarchy h ON h.sic4 = co.sic
        WHERE {FULL} AND co.sic2 IS NOT NULL AND co.sic2 <> ''
        GROUP BY 1 HAVING count(*) >= 100 ORDER BY default_rate_pct DESC"""),

    ("6a. Peer group (140) - highest rate, 30+ defaults so it is calibratable", f"""
        SELECT g.industry_code, g.industry_label,
               count(*) AS company_years,
               count(DISTINCT co.cik) AS companies,
               count(*) FILTER (WHERE co.default_24m) AS defaults,
               round(100.0 * count(*) FILTER (WHERE co.default_24m) / count(*), 2)
                   AS default_rate_pct
        FROM marts.credit_outcomes co
        JOIN ref.industry_group g ON g.sic4 = co.sic
        WHERE {FULL}
        GROUP BY 1, 2 HAVING count(*) FILTER (WHERE co.default_24m) >= 30
        ORDER BY default_rate_pct DESC LIMIT 25"""),

    ("6b. Peer group - lowest rate, same 30+ event gate", f"""
        SELECT g.industry_code, g.industry_label,
               count(*) AS company_years,
               count(DISTINCT co.cik) AS companies,
               count(*) FILTER (WHERE co.default_24m) AS defaults,
               round(100.0 * count(*) FILTER (WHERE co.default_24m) / count(*), 2)
                   AS default_rate_pct
        FROM marts.credit_outcomes co
        JOIN ref.industry_group g ON g.sic4 = co.sic
        WHERE {FULL}
        GROUP BY 1, 2 HAVING count(*) FILTER (WHERE co.default_24m) >= 30
        ORDER BY default_rate_pct ASC LIMIT 15"""),

    ("7. CHECK: does this agree with the pre-built marts.outcome_counts?", """
        WITH mine AS (
            SELECT sic2 AS industry_code, count(*) AS company_years,
                   count(*) FILTER (WHERE default_24m) AS defaults
            FROM marts.credit_outcomes
            WHERE sic2 IS NOT NULL AND sic2 <> '' GROUP BY 1),
        theirs AS (
            SELECT industry_code, company_years, default_24m AS defaults
            FROM marts.outcome_counts
            WHERE industry_scheme = 'sic2' AND fy IS NULL AND size_band = 'ALL')
        SELECT count(*) AS groups_compared,
               count(*) FILTER (WHERE m.company_years = t.company_years) AS denom_agrees,
               count(*) FILTER (WHERE m.defaults = t.defaults) AS numer_agrees,
               count(*) FILTER (WHERE m.company_years <> t.company_years
                                   OR m.defaults <> t.defaults) AS disagreements
        FROM mine m JOIN theirs t USING (industry_code)"""),

    ("8. CHECK: no fan-out - the industry joins must not multiply rows", """
        SELECT count(*) AS joined_rows,
               (SELECT count(*) FROM marts.credit_outcomes) AS source_rows
        FROM marts.credit_outcomes co
        LEFT JOIN ref.sic_hierarchy h ON h.sic4 = co.sic
        LEFT JOIN ref.industry_group g ON g.sic4 = co.sic"""),
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
