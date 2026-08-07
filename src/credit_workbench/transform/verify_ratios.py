"""Verification for E1/E2 — do the ratios and benchmarks hold up?

Checks fill rates, sanity-bounds each ratio, confirms the benchmark distributions are
ordered and plausible, and prints a worked peer comparison so the output can be read
against a company whose financials are publicly known.
"""
from __future__ import annotations

import argparse

import duckdb

from credit_workbench.common.config import motherduck_token
from credit_workbench.transform.ratio_defs import RATIOS

CHECKS: list[tuple[str, str]] = [
    ("Ratio coverage by category (share of company-years with a value)", """
        WITH n AS (SELECT count(*) AS total FROM marts.ratios
                   WHERE basis = 'latest' AND fy >= 2015)
        SELECT d.category, count(DISTINCT v.ratio) AS ratios,
               round(100.0 * count(*) / (count(DISTINCT v.ratio) * any_value(n.total)), 1)
                   AS avg_pct_populated
        FROM marts.ratio_values v
        JOIN ref.ratio_definitions d USING (ratio) CROSS JOIN n
        WHERE v.basis = 'latest' AND v.fy >= 2015
        GROUP BY 1 ORDER BY 3 DESC"""),

    ("Sanity bounds — ratios outside a plausible range", """
        SELECT ratio, count(*) AS extreme_values,
               round(100.0 * count(*) / any_value(total), 2) AS pct
        FROM (SELECT v.*, count(*) OVER (PARTITION BY ratio) AS total
              FROM marts.ratio_values v WHERE basis = 'latest')
        WHERE (ratio LIKE '%margin%' AND (value < -50 OR value > 1.01))
           OR (ratio LIKE '%_days' AND (value < 0 OR value > 3650))
           OR (ratio = 'current_ratio' AND value > 1000)
           OR (ratio LIKE 'debt_to_%' AND value < 0)
        GROUP BY ratio ORDER BY 2 DESC LIMIT 10"""),

    ("Benchmark distributions are correctly ordered (p10 <= p50 <= p90)", """
        SELECT count(*) AS benchmark_rows,
               count(*) FILTER (WHERE p10 <= p50 AND p50 <= p90) AS correctly_ordered,
               count(*) FILTER (WHERE NOT (p10 <= p50 AND p50 <= p90)) AS broken
        FROM marts.benchmarks"""),

    ("Benchmark depth by grain", """
        SELECT level, count(*) AS rows, count(DISTINCT industry_code) AS industries,
               round(avg(n_companies)) AS avg_peers, max(n_companies) AS max_peers
        FROM marts.benchmarks GROUP BY 1 ORDER BY 2 DESC"""),

    ("Leverage by industry, latest full year (median debt/EBITDA)", """
        SELECT industry_code, any_value(industry_name) AS industry, n_companies,
               round(p25, 2) AS p25, round(p50, 2) AS median, round(p75, 2) AS p75
        FROM marts.benchmarks
        WHERE level = 'sic2' AND ratio = 'debt_to_ebitda' AND fy = 2024
          AND n_companies >= 25
        GROUP BY industry_code, n_companies, p25, p50, p75
        ORDER BY p50 DESC LIMIT 10"""),

    ("Distress flags across the population (latest year)", """
        SELECT count(*) AS company_years,
               round(100.0 * count(*) FILTER (WHERE ebitda_negative) / count(*), 1)
                   AS pct_ebitda_negative,
               round(100.0 * count(*) FILTER (WHERE equity_negative) / count(*), 1)
                   AS pct_equity_negative,
               round(100.0 * count(*) FILTER (WHERE interest_uncovered) / count(*), 1)
                   AS pct_interest_uncovered,
               round(100.0 * count(*) FILTER (WHERE net_loss) / count(*), 1)
                   AS pct_net_loss
        FROM marts.ratios WHERE basis = 'latest' AND fy = 2024"""),
]

TEARSHEET = [
    "debt_to_ebitda", "net_debt_to_ebitda", "debt_to_capital",
    "ebitda_interest_cover", "ffo_to_debt", "current_ratio",
    "ebitda_margin", "net_margin", "return_on_capital_employed",
    "receivable_days", "inventory_days", "cfo_to_ebitda", "revenue_growth",
]


def show(con, query: str, params: list | None = None) -> None:
    cur = con.execute(query, params) if params else con.execute(query)
    headers = [d[0] for d in cur.description]
    rows = [["" if v is None else (f"{v:,.4g}" if isinstance(v, float) else str(v))
             for v in r] for r in cur.fetchall()]
    if not rows:
        print("  (no rows)")
        return
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    print("  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        print("  " + "  ".join(v.ljust(w) for v, w in zip(r, widths)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="PFE")
    args = ap.parse_args()
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")

    print(f"\n### Library: {len(RATIOS)} ratios")
    for title, query in CHECKS:
        print(f"\n### {title}")
        try:
            show(con, query)
        except Exception as exc:  # noqa: BLE001
            print(f"  (check failed: {exc})")

    row = con.execute("""
        SELECT c.cik, c.company_name, c.sic_description FROM ref.dim_company c
        JOIN ref.company_tickers t USING (cik) WHERE t.ticker = ? LIMIT 1""",
        [args.ticker.upper()]).fetchone()
    if not row:
        return
    cik, name, industry = row
    fy = con.execute(f"""
        SELECT max(fy) FROM marts.ratio_percentiles WHERE cik = {cik}""").fetchone()[0]
    print(f"\n### Peer comparison: {name} — {industry}, FY{fy}")
    tear = ", ".join(f"'{r}'" for r in TEARSHEET)
    show(con, f"""
        SELECT p.ratio, round(p.value, 2) AS company,
               round(b.p25, 2) AS peer_p25, round(b.p50, 2) AS peer_median,
               round(b.p75, 2) AS peer_p75, b.n_companies AS peers,
               round(p.credit_percentile * 100) AS credit_pctile
        FROM marts.ratio_percentiles p
        LEFT JOIN marts.benchmarks b
          ON b.level = 'sic2' AND b.industry_code = p.sic2 AND b.fy = p.fy
         AND b.ratio = p.ratio AND b.size_band = 'ALL'
        WHERE p.cik = {cik} AND p.fy = {fy} AND p.ratio IN ({tear})
        ORDER BY p.ratio""")


if __name__ == "__main__":
    main()
