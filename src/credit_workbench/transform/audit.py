"""Systematic quality audit of the spread marts.

Spot-checking finds the errors you happen to look at. This tests the spreads against
things that must be true of any real set of accounts — the balance sheet balances,
subtotals equal their parts, expenses do not have the sign of income, EBIT does not
exceed gross profit, a company does not report two different figures for the same
year — and ranks what is missing by how much money it represents rather than how
often it appears.

Each check prints a headline count and, where useful, the worst offenders so the
cause can be traced rather than guessed.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

# (title, sql). Each returns a small result set.
CHECKS: list[tuple[str, str]] = [
    ("1. Fill rate per spread line (companies with revenue, last 10 years)", """
        WITH pop AS (
            SELECT * FROM marts.spreads_a
            WHERE basis = 'latest' AND fy >= 2015 AND revenue IS NOT NULL),
        n AS (SELECT count(*) AS total FROM pop)
        SELECT line, filled, total, round(100.0 * filled / total, 1) AS pct
        FROM (
            SELECT 'revenue' AS line, count(revenue) AS filled FROM pop UNION ALL
            SELECT 'cost_of_sales', count(cost_of_sales) FROM pop UNION ALL
            SELECT 'operating_income (as tagged)', count(operating_income) FROM pop UNION ALL
            SELECT 'ebit_calc', count(ebit_calc) FROM pop UNION ALL
            SELECT 'dep_amort (either)', count(coalesce(dep_amort_is, dep_amort_cf)) FROM pop UNION ALL
            SELECT 'ebitda', count(ebitda) FROM pop UNION ALL
            SELECT 'interest_expense', count(interest_expense) FROM pop UNION ALL
            SELECT 'net_income', count(net_income) FROM pop UNION ALL
            SELECT 'total_assets', count(total_assets) FROM pop UNION ALL
            SELECT 'total_liabilities', count(total_liabilities) FROM pop UNION ALL
            SELECT 'total_equity', count(total_equity) FROM pop UNION ALL
            SELECT 'cash', count(cash) FROM pop UNION ALL
            SELECT 'total_debt', count(total_debt) FROM pop UNION ALL
            SELECT 'long_term_debt', count(long_term_debt) FROM pop UNION ALL
            SELECT 'current_portion_ltd', count(current_portion_ltd) FROM pop UNION ALL
            SELECT 'cfo', count(cfo) FROM pop UNION ALL
            SELECT 'capex', count(capex) FROM pop UNION ALL
            SELECT 'inventory', count(inventory) FROM pop UNION ALL
            SELECT 'accounts_receivable', count(accounts_receivable) FROM pop UNION ALL
            SELECT 'accounts_payable', count(accounts_payable) FROM pop
        ) CROSS JOIN n ORDER BY pct"""),

    ("2. Unmapped face tags ranked by VALUE carried (not frequency)", """
        SELECT tag, statement, filings,
               round(abs_value_carried / 1e12, 1) AS usd_tn
        FROM staging.unmapped_tags
        WHERE statement IN ('IS', 'CF')
        ORDER BY abs_value_carried DESC LIMIT 15"""),

    ("3. Sign anomalies — share of values that are negative", """
        WITH pop AS (SELECT * FROM marts.spreads_a WHERE basis = 'latest' AND fy >= 2015)
        SELECT line, neg, n, round(100.0 * neg / nullif(n, 0), 1) AS pct_negative
        FROM (
            SELECT 'revenue' AS line, count(*) FILTER (WHERE revenue < 0) AS neg,
                   count(revenue) AS n FROM pop UNION ALL
            SELECT 'cost_of_sales', count(*) FILTER (WHERE cost_of_sales < 0), count(cost_of_sales) FROM pop UNION ALL
            SELECT 'sgna', count(*) FILTER (WHERE sgna < 0), count(sgna) FROM pop UNION ALL
            SELECT 'interest_expense', count(*) FILTER (WHERE interest_expense < 0), count(interest_expense) FROM pop UNION ALL
            SELECT 'income_tax', count(*) FILTER (WHERE income_tax < 0), count(income_tax) FROM pop UNION ALL
            SELECT 'capex', count(*) FILTER (WHERE capex < 0), count(capex) FROM pop UNION ALL
            SELECT 'total_assets', count(*) FILTER (WHERE total_assets < 0), count(total_assets) FROM pop UNION ALL
            SELECT 'inventory', count(*) FILTER (WHERE inventory < 0), count(inventory) FROM pop UNION ALL
            SELECT 'cash', count(*) FILTER (WHERE cash < 0), count(cash) FROM pop UNION ALL
            SELECT 'total_debt', count(*) FILTER (WHERE total_debt < 0), count(total_debt) FROM pop
        ) ORDER BY pct_negative DESC"""),

    ("4. EBIT exceeding gross profit (suggests an opex subtotal that excludes COGS)", """
        SELECT count(*) AS company_years,
               count(DISTINCT cik) AS companies,
               round(100.0 * count(*) / (SELECT count(*) FROM marts.spreads_a
                   WHERE basis = 'latest' AND ebit_calc IS NOT NULL
                     AND gross_profit_calc IS NOT NULL), 1) AS pct_of_comparable
        FROM marts.spreads_a
        WHERE basis = 'latest' AND ebit_calc IS NOT NULL AND gross_profit_calc IS NOT NULL
          AND ebit_calc > gross_profit_calc * 1.01 AND revenue > 0"""),

    ("4b. Worst EBIT-over-gross-profit offenders", """
        SELECT company_name, fy, round(revenue/1e6) AS revenue_mm,
               round(gross_profit_calc/1e6) AS gross_profit_mm,
               round(ebit_calc/1e6) AS ebit_mm,
               round(total_operating_expenses/1e6) AS total_opex_mm
        FROM marts.spreads_a
        WHERE basis = 'latest' AND ebit_calc > gross_profit_calc * 1.01
          AND revenue > 1e9 AND gross_profit_calc > 0
        ORDER BY (ebit_calc - gross_profit_calc) DESC LIMIT 8"""),

    ("5. Long-term debt double-count risk (LongTermDebt is a TOTAL incl. current)", """
        SELECT l.source_tag, count(*) AS company_years,
               count(*) FILTER (WHERE s.current_portion_ltd IS NOT NULL)
                   AS also_has_current_portion
        FROM marts.spread_lines l
        JOIN marts.spreads_a s ON s.cik = l.cik AND s.basis = l.basis
                              AND s.period_end = l.period_end
        WHERE l.basis = 'latest' AND l.line_code = 'long_term_debt'
        GROUP BY 1 ORDER BY 2 DESC"""),

    ("6. Balance sheet subtotal ties (assets = liabilities + equity)", """
        SELECT verdict, count(*) AS company_years
        FROM marts.spread_checks WHERE basis = 'latest' GROUP BY 1 ORDER BY 2 DESC"""),

    ("7. Current assets vs the sum of their parts", """
        WITH p AS (
            SELECT *, coalesce(cash,0) + coalesce(short_term_investments,0)
                    + coalesce(accounts_receivable,0) + coalesce(inventory,0)
                    + coalesce(prepaid_other_current,0) AS parts
            FROM marts.spreads_a
            WHERE basis = 'latest' AND total_current_assets > 0 AND fy >= 2015)
        SELECT count(*) AS company_years,
               round(100.0 * count(*) FILTER (WHERE parts <= total_current_assets * 1.02), 1)
                   AS pct_parts_within_total,
               round(100.0 * count(*) FILTER (WHERE parts > total_current_assets * 1.02), 1)
                   AS pct_parts_exceed_total
        FROM p"""),

    ("8. Duplicate fiscal years (same company + fy, two period ends)", """
        SELECT count(*) AS duplicate_groups FROM (
            SELECT cik, basis, fy FROM marts.spreads_a
            WHERE basis = 'latest' AND fy IS NOT NULL
            GROUP BY 1, 2, 3 HAVING count(*) > 1)"""),

    ("9. Scale outliers (implausible magnitudes)", """
        SELECT 'revenue > $1tn' AS check, count(*) AS rows FROM marts.spreads_a
            WHERE basis='latest' AND revenue > 1e12
        UNION ALL SELECT 'total_assets > $10tn', count(*) FROM marts.spreads_a
            WHERE basis='latest' AND total_assets > 1e13
        UNION ALL SELECT 'negative total_assets', count(*) FROM marts.spreads_a
            WHERE basis='latest' AND total_assets < 0
        UNION ALL SELECT 'negative inventory', count(*) FROM marts.spreads_a
            WHERE basis='latest' AND inventory < 0
        UNION ALL SELECT 'EBITDA > 5x revenue', count(*) FROM marts.spreads_a
            WHERE basis='latest' AND revenue > 1e6 AND ebitda > revenue * 5"""),

    ("10. Cash flow statement ties (CFO + CFI + CFF + FX = net change in cash)", """
        WITH p AS (
            SELECT *, coalesce(cfo,0) + coalesce(cfi,0) + coalesce(cff,0)
                    + coalesce(fx_effect_cash,0) AS computed
            FROM marts.spreads_a
            WHERE basis = 'latest' AND fy >= 2015
              AND cfo IS NOT NULL AND cfi IS NOT NULL AND cff IS NOT NULL
              AND net_change_cash IS NOT NULL)
        SELECT count(*) AS company_years,
               round(100.0 * count(*) FILTER (
                   WHERE abs(computed - net_change_cash)
                         <= greatest(abs(net_change_cash) * 0.01, 1e5)), 1) AS pct_tie"""),

    ("11. first_reported basis coverage vs latest", """
        SELECT basis, count(*) AS rows, count(DISTINCT cik) AS companies,
               count(revenue) AS with_revenue
        FROM marts.spreads_a GROUP BY 1"""),

    ("12. Companies whose spread is essentially empty (mapped nothing useful)", """
        SELECT count(*) AS company_years
        FROM marts.spreads_a
        WHERE basis = 'latest' AND revenue IS NULL AND total_assets IS NULL
          AND net_income IS NULL AND cfo IS NULL"""),
]


def show(con, query: str) -> None:
    cur = con.execute(query)
    headers = [d[0] for d in cur.description]
    rows = [["" if v is None else (f"{v:,.4g}" if isinstance(v, float) else str(v))
             for v in r] for r in cur.fetchall()]
    if not rows:
        print("  (no rows — check passes)")
        return
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    print("  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        print("  " + "  ".join(v.ljust(w) for v, w in zip(r, widths)))


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    for title, query in CHECKS:
        print(f"\n### {title}")
        try:
            show(con, query)
        except Exception as exc:  # noqa: BLE001
            print(f"  (check failed: {exc})")


if __name__ == "__main__":
    main()
