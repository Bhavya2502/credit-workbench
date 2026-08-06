"""Verification for C4/C5/C6 — does the spread hold up?

Reports coverage, reconciliation pass rates, the biggest unmapped tags (the honest
list of what the template does not yet capture), restatement examples, and a printed
spread for one company so the numbers can be eyeballed against its 10-K.
"""
from __future__ import annotations

import argparse

import duckdb

from credit_workbench.common.config import motherduck_token

HEADLINE = [
    ("revenue", "Revenue"), ("cost_of_sales", "Cost of sales"),
    ("gross_profit_calc", "Gross profit"), ("sgna", "SG&A"),
    ("research_development", "R&D"), ("ebit_calc", "Operating income (EBIT)"),
    ("ebitda", "EBITDA"), ("interest_expense", "Interest expense"),
    ("pretax_income", "Profit before tax"), ("income_tax", "Income tax"),
    ("net_income", "Net income"),
    ("cash", "Cash & equivalents"), ("accounts_receivable", "Receivables"),
    ("inventory", "Inventory"), ("total_current_assets", "Total current assets"),
    ("ppe_net", "PP&E net"), ("goodwill", "Goodwill"), ("total_assets", "Total assets"),
    ("accounts_payable", "Payables"),
    ("total_current_liabilities", "Total current liabilities"),
    ("short_term_debt", "Short-term borrowings"),
    ("current_portion_ltd", "Current portion of LTD"),
    ("long_term_debt", "Long-term debt"), ("total_liabilities", "Total liabilities"),
    ("total_equity", "Total equity"),
    ("total_debt", "Total debt"), ("total_debt_incl_leases", "Total debt incl. leases"),
    ("net_debt", "Net debt"), ("working_capital", "Working capital"),
    ("cfo", "Cash flow from operations"), ("capex", "Capital expenditure"),
    ("free_cash_flow", "Free cash flow"), ("ffo_simplified", "FFO (simplified)"),
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

    print("\n### Spread marts built")
    show(con, """
        SELECT 'spreads_a (annual)' AS mart, count(*) AS rows,
               count(DISTINCT cik) AS companies, min(fy) AS from_fy, max(fy) AS to_fy
        FROM marts.spreads_a WHERE basis = 'latest'
        UNION ALL
        SELECT 'spreads_q (quarterly)', count(*), count(DISTINCT cik),
               min(fy), max(fy) FROM marts.spreads_q WHERE basis = 'latest'""")

    print("\n### Template coverage of reported face-financial value")
    show(con, """
        SELECT round(median(fact_coverage) * 100, 1)  AS median_pct_facts_mapped,
               round(median(value_coverage) * 100, 1) AS median_pct_value_mapped,
               count(*) AS company_years
        FROM marts.spread_coverage WHERE face_facts >= 20""")

    print("\n### Reconciliation checks (annual, latest basis)")
    show(con, """
        SELECT verdict, count(*) AS company_years,
               round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
        FROM marts.spread_checks WHERE basis = 'latest'
        GROUP BY 1 ORDER BY 2 DESC""")

    print("\n### Balance sheet tie quality (where both totals present)")
    show(con, """
        SELECT count(*) AS company_years,
               round(100.0 * count(*) FILTER (WHERE balance_sheet_gap < 0.0001)
                     / count(*), 1) AS pct_tie_exactly,
               round(100.0 * count(*) FILTER (WHERE balance_sheet_gap < 0.01)
                     / count(*), 1) AS pct_within_1pct
        FROM marts.spread_checks
        WHERE basis = 'latest' AND balance_sheet_gap IS NOT NULL""")

    print("\n### Biggest tags the template does NOT capture (extend-the-map worklist)")
    show(con, """
        SELECT tag, statement, filings, round(abs_value_carried / 1e9, 1) AS usd_bn
        FROM staging.unmapped_tags ORDER BY filings DESC LIMIT 20""")

    print("\n### Restatements found (first reported vs latest)")
    show(con, """
        SELECT count(*) AS restated_figures, count(DISTINCT cik) AS companies
        FROM marts.restatements""")
    show(con, """
        SELECT company_name, tag, period_end,
               round(first_reported / 1e6, 0) AS first_usd_mm,
               round(latest_value / 1e6, 0)   AS latest_usd_mm,
               round(restatement_pct * 100, 1) AS pct_change
        FROM marts.restatements
        WHERE tag IN ('Revenues', 'NetIncomeLoss', 'Assets')
          AND abs(first_reported) > 1e9 AND abs(restatement_pct) BETWEEN 0.02 AND 5
        ORDER BY abs(restatement_amount) DESC LIMIT 10""")

    print("\n### Pilot industry coverage in the annual spread")
    show(con, """
        SELECT CASE WHEN sic BETWEEN '2833' AND '2836' THEN 'Pharma / biotech'
                    WHEN sic BETWEEN '3500' AND '3599' THEN 'Capital goods'
                    WHEN sic BETWEEN '5200' AND '5999' THEN 'Retail' END AS industry,
               count(DISTINCT cik) AS companies, count(*) AS company_years
        FROM marts.spreads_a
        WHERE basis = 'latest' AND industry IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC""")

    # ------------------------------------------------------------ printed spread
    row = con.execute("""
        SELECT c.cik, c.company_name FROM ref.dim_company c
        JOIN ref.company_tickers t USING (cik) WHERE t.ticker = ? LIMIT 1""",
        [args.ticker.upper()]).fetchone()
    if not row:
        return
    cik, name = row
    years = [r[0] for r in con.execute(f"""
        SELECT DISTINCT fy FROM marts.spreads_a
        WHERE cik = {cik} AND basis = 'latest' AND fy IS NOT NULL
        ORDER BY fy DESC LIMIT 4""").fetchall()]
    if not years:
        return
    cols = ", ".join(f"max(CASE WHEN fy = {y} THEN {{c}} END) AS fy{y}" for y in years)
    print(f"\n### Credit spread: {name} (USD millions, latest basis)")
    parts = []
    for code, label in HEADLINE:
        sel = cols.replace("{c}", code)
        parts.append(f"SELECT '{label}' AS line, {sel} "
                     f"FROM marts.spreads_a WHERE cik = {cik} AND basis = 'latest'")
    query = "\nUNION ALL\n".join(parts)
    cur = con.execute(f"SELECT * FROM ({query})")
    headers = ["Line"] + [f"FY{y}" for y in years]
    out = []
    for r in cur.fetchall():
        vals = [r[0]] + [("" if v is None else f"{v / 1e6:,.0f}") for v in r[1:]]
        out.append(vals)
    widths = [max(len(h), *(len(r[i]) for r in out)) for i, h in enumerate(headers)]
    print("  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for r in out:
        print("  " + r[0].ljust(widths[0]) + "  "
              + "  ".join(v.rjust(w) for v, w in zip(r[1:], widths[1:])))


if __name__ == "__main__":
    main()
