"""Which of the 24 requested gaps are real? — DATA_GAPS_FOR_WAREHOUSE.md, 18 Aug 2026

The requesting workstream states plainly that it had no database access and read every gap
off `DATA_GUIDE.md`. That makes the list a set of hypotheses about this warehouse rather
than findings, and at least two look answerable already: the industry bridge
(`ref.industry_group`, 140 peer groups) and the SIC hierarchy were built on 15 August,
which is most of G-17, and `marts.concentration` gained vintage flags the same day, which
would make G-22 a stale caveat in section 7 of the guide rather than a defect in the data.

Neither is documented in the guide. That is the actual failure, and it is worth separating
from the rest: a gap that exists because a table is undiscoverable is closed by writing a
paragraph, and a gap that exists because a table is absent is not.

So this checks each cheap claim against the warehouse before anything gets built, and
collects the shape the two decisive marts need - G-02 wants `ratio x sic2 x size_band x fy`
and G-03 wants outcomes on the same cohort keys, so whether those columns are populated
and how thin the resulting cells are decides whether either table is worth having.

Deliberately avoids `marts.facts_dimensioned` (222m) and `ref.tag_note_map` (279m): the
free plan's daily compute has been exhausted on this project before, and G-24 is itself a
complaint about that.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

Q = [
    # ---- what exists at all
    ("1. Does anything resembling the requested marts already exist?", """
        SELECT table_schema, table_name, table_type
        FROM information_schema.tables
        WHERE table_name IN ('ratio_coverage', 'outcome_counts', 'adjusted_metrics',
                             'macro_series', 'disclosed_kpis', 'risk_themes',
                             'cohorts', 'cohort_members', 'issuer_ratings',
                             'industry_group', 'sic_hierarchy', 'sic_naics')
        ORDER BY table_schema, table_name"""),

    # ---- G-17: the industry bridge exists but is undocumented
    ("2. G-17 — is the industry bridge populated?", """
        SELECT 'ref.industry_group' AS obj, count(*) AS rows,
               count(DISTINCT peer_group) AS peer_groups,
               count(custom_industry) AS with_custom_industry
        FROM ref.industry_group"""),

    ("3. G-17 — and the SIC hierarchy above it?", """
        SELECT count(*) AS sic_codes,
               count(DISTINCT sic3) AS three_digit_groups,
               count(DISTINCT major_group) AS major_groups,
               count(DISTINCT division) AS divisions
        FROM ref.sic_hierarchy"""),

    # ---- G-22: claimed missing, believed fixed on 15 Aug
    ("4. G-22 — does marts.concentration carry vintage flags now?", """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'marts' AND table_name = 'concentration'
          AND column_name IN ('is_latest', 'is_first_report', 'filings_reporting')
        ORDER BY column_name"""),

    ("5. G-22 — and are they actually populated?", """
        SELECT count(*) AS rows, count(*) FILTER (WHERE is_latest) AS latest,
               round(100.0 * count(*) FILTER (WHERE is_latest) / count(*), 1) AS pct_latest
        FROM marts.concentration"""),

    # ---- G-16
    ("6. G-16 — is ref.sic_naics still empty?", "SELECT count(*) AS rows FROM ref.sic_naics"),

    # ---- G-02 groundwork
    ("7. G-02 — the cohort keys the coverage table needs", """
        SELECT count(*) AS rows, count(DISTINCT ratio) AS ratios,
               count(DISTINCT sic2) AS sic2, count(DISTINCT size_band) AS size_bands,
               count(DISTINCT fy) AS years, min(fy) AS first_fy, max(fy) AS last_fy,
               count(DISTINCT basis) AS bases
        FROM marts.ratio_values"""),

    ("8. G-02 — what are the size bands called?", """
        SELECT size_band, count(DISTINCT cik) AS companies
        FROM marts.ratio_values WHERE fy = 2024
        GROUP BY 1 ORDER BY companies DESC"""),

    # If most cells hold a handful of companies the table is mostly noise, which the
    # design tool needs to know before it weights a factor on one.
    ("9. G-02 — how thin would the cells be? (ratio x sic2 x size_band, fy 2024)", """
        SELECT count(*) AS cells,
               count(*) FILTER (WHERE companies >= 30) AS cells_over_30,
               count(*) FILTER (WHERE companies < 10) AS cells_under_10,
               round(median(companies), 0) AS median_companies
        FROM (SELECT ratio, sic2, size_band, count(DISTINCT cik) AS companies
              FROM marts.ratio_values
              WHERE fy = 2024 AND basis = 'first_reported' AND value IS NOT NULL
              GROUP BY 1, 2, 3)"""),

    # ---- G-03 groundwork
    ("10. G-03 — outcome columns available", """
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_schema = 'marts' AND table_name = 'credit_outcomes'
        ORDER BY ordinal_position"""),

    ("11. G-03 — how many events are there in total to spread across cohorts?", """
        SELECT count(*) AS company_years,
               count(*) FILTER (WHERE distress_12m) AS distress_12m,
               count(*) FILTER (WHERE distress_24m) AS distress_24m,
               count(*) FILTER (WHERE default_24m) AS default_24m,
               count(*) FILTER (WHERE bankruptcy_24m) AS bankruptcy_24m
        FROM marts.credit_outcomes"""),

    # ---- G-19: survivorship, the one that silently corrupts a backtest
    ("12. G-19 — what does ref.dim_company carry about status and former names?", """
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_schema = 'ref' AND table_name = 'dim_company'
        ORDER BY ordinal_position"""),

    # A delisted issuer is exactly the company with a credit event. If the master only
    # holds live filers the whole outcome set is survivorship-biased.
    ("13. G-19 — do companies with a bankruptcy outcome still appear in the master?", """
        SELECT count(DISTINCT o.cik) AS bankrupt_ciks,
               count(DISTINCT c.cik) AS present_in_dim_company
        FROM marts.credit_outcomes o
        LEFT JOIN ref.dim_company c
          ON lpad(CAST(c.cik AS VARCHAR), 10, '0') = lpad(CAST(o.cik AS VARCHAR), 10, '0')
        WHERE o.bankruptcy_24m"""),

    ("14. G-19 — and do they stop filing? (evidence the master is not survivor-only)", """
        SELECT max(substr(CAST(observation_date AS VARCHAR), 1, 4)) AS last_obs_year,
               count(DISTINCT cik) AS ciks
        FROM marts.credit_outcomes WHERE bankruptcy_24m"""),

    # ---- G-20
    ("15. G-20 — the superseded tables, with row counts to show which is which", """
        SELECT 'marts.ratios' AS t, count(*) AS rows FROM marts.ratios
        UNION ALL SELECT 'marts.ratio_values', count(*) FROM marts.ratio_values
        UNION ALL SELECT 'marts.spreads_a', count(*) FROM marts.spreads_a
        UNION ALL SELECT 'marts.spreads_q', count(*) FROM marts.spreads_q
        UNION ALL SELECT 'marts.spread_lines', count(*) FROM marts.spread_lines"""),

    # ---- G-21
    ("16. G-21 — how inconsistent is the cik type across marts?", """
        SELECT data_type, count(*) AS tables,
               string_agg(table_schema || '.' || table_name, ', ') AS examples
        FROM information_schema.columns
        WHERE column_name = 'cik' AND table_schema IN ('marts', 'ref', 'quali', 'staging')
        GROUP BY 1 ORDER BY tables DESC"""),

    # ---- G-04 groundwork
    ("17. G-04/G-05 — are the lease and pension inputs actually populated?", """
        SELECT count(*) AS rows,
               count(op_lease_y1) AS op_lease_842,
               count(op_lease_840_y1) AS op_lease_840,
               count(fin_lease_y1) AS fin_lease,
               count(pension_benefit_obligation) AS pension_pbo,
               count(*) FILTER (WHERE basis = 'first_reported') AS first_reported
        FROM marts.adjustment_inputs"""),

    # ---- G-07 feasibility: do issuers state their ratings in text we already hold?
    ("18. G-07 — do MD&A sections mention the agencies by name?", """
        SELECT count(*) AS sections,
               count(*) FILTER (WHERE lower(text) LIKE '%moody%') AS mentions_moodys,
               count(*) FILTER (WHERE lower(text) LIKE '%standard & poor%'
                                   OR lower(text) LIKE '%s&p global%') AS mentions_sp,
               count(*) FILTER (WHERE lower(text) LIKE '%fitch%') AS mentions_fitch
        FROM quali.mdna
        WHERE substr(filing_date, 1, 4) = '2024'"""),

    ("19. G-23 — the net_worth outliers said to number about 30", """
        SELECT count(*) AS rows FROM marts.covenant_headline
        WHERE covenant_type = 'net_worth'"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)))[:96]
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
            print(f"  (failed: {str(exc)[:170]})")


if __name__ == "__main__":
    main()
