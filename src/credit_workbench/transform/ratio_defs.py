"""Tracker E1 — the ratio library, defined as data so it can be inspected and exported.

Each entry is (name, category, SQL expression over a row of marts.spreads_a).

Two conventions run through every definition:

*Guarded denominators.* Every divisor is wrapped in `nullif(..., 0)`, so a missing or
zero denominator yields a blank rather than an error or an infinity.

*Meaningless ratios are blank, not negative.* Debt/EBITDA is undefined when EBITDA is
negative — the arithmetic returns a negative number that screens as if it were
conservative leverage, which is the opposite of the truth. Those cases return NULL and
are picked up instead by the `ebitda_negative` flag, so a loss-making borrower is
surfaced as a loss-making borrower rather than hidden among the safe ones.
"""
from __future__ import annotations

RATIOS: list[tuple[str, str, str]] = [
    # ---------------------------------------------------------------- leverage
    ("debt_to_ebitda", "leverage",
     "CASE WHEN ebitda > 0 THEN total_debt / ebitda END"),
    ("debt_incl_leases_to_ebitda", "leverage",
     "CASE WHEN ebitda > 0 THEN total_debt_incl_leases / ebitda END"),
    ("net_debt_to_ebitda", "leverage",
     "CASE WHEN ebitda > 0 THEN net_debt / ebitda END"),
    ("debt_to_capital", "leverage",
     "total_debt / nullif(total_debt + total_equity, 0)"),
    ("debt_to_assets", "leverage", "total_debt / nullif(total_assets, 0)"),
    ("liabilities_to_assets", "leverage",
     "total_liabilities / nullif(total_assets, 0)"),
    ("debt_to_tangible_net_worth", "leverage",
     "CASE WHEN tangible_net_worth > 0 THEN total_debt / tangible_net_worth END"),
    ("net_debt_to_capital_employed", "leverage",
     "net_debt / nullif(capital_employed, 0)"),
    ("ffo_to_debt", "leverage",
     "CASE WHEN total_debt > 0 THEN ffo_simplified / total_debt END"),
    ("cfo_to_debt", "leverage",
     "CASE WHEN total_debt > 0 THEN cfo / total_debt END"),
    ("fcf_to_debt", "leverage",
     "CASE WHEN total_debt > 0 THEN free_cash_flow / total_debt END"),
    ("equity_to_assets", "leverage", "total_equity / nullif(total_assets, 0)"),

    # ---------------------------------------------------------------- coverage
    ("ebitda_interest_cover", "coverage",
     "CASE WHEN interest_expense > 0 THEN ebitda / interest_expense END"),
    ("ebit_interest_cover", "coverage",
     "CASE WHEN interest_expense > 0 THEN ebit_calc / interest_expense END"),
    ("ebitda_less_capex_interest_cover", "coverage",
     "CASE WHEN interest_expense > 0 "
     "THEN (ebitda - coalesce(capex, 0)) / interest_expense END"),
    ("ffo_interest_cover", "coverage",
     "CASE WHEN interest_expense > 0 "
     "THEN (ffo_simplified + interest_expense) / interest_expense END"),
    ("cfo_interest_cover", "coverage",
     "CASE WHEN interest_expense > 0 THEN cfo / interest_expense END"),
    ("debt_service_cover", "coverage",
     "CASE WHEN coalesce(interest_expense, 0) + coalesce(current_portion_ltd, 0) > 0 "
     "THEN ebitda / (coalesce(interest_expense, 0) "
     "+ coalesce(current_portion_ltd, 0)) END"),

    # ---------------------------------------------------------------- liquidity
    ("current_ratio", "liquidity",
     "total_current_assets / nullif(total_current_liabilities, 0)"),
    ("quick_ratio", "liquidity",
     "(coalesce(cash, 0) + coalesce(short_term_investments, 0) "
     "+ coalesce(accounts_receivable, 0)) / nullif(total_current_liabilities, 0)"),
    ("cash_ratio", "liquidity",
     "(coalesce(cash, 0) + coalesce(short_term_investments, 0)) "
     "/ nullif(total_current_liabilities, 0)"),
    ("working_capital_to_revenue", "liquidity",
     "CASE WHEN revenue > 0 THEN working_capital / revenue END"),
    ("cash_to_debt", "liquidity",
     "CASE WHEN total_debt > 0 THEN coalesce(cash, 0) / total_debt END"),

    # ------------------------------------------------------------ profitability
    ("gross_margin", "profitability",
     "CASE WHEN revenue > 0 THEN gross_profit_calc / revenue END"),
    ("ebitda_margin", "profitability",
     "CASE WHEN revenue > 0 THEN ebitda / revenue END"),
    ("ebit_margin", "profitability",
     "CASE WHEN revenue > 0 THEN ebit_calc / revenue END"),
    ("net_margin", "profitability",
     "CASE WHEN revenue > 0 THEN net_income / revenue END"),
    ("return_on_assets", "profitability",
     "CASE WHEN total_assets > 0 THEN net_income / total_assets END"),
    ("return_on_equity", "profitability",
     "CASE WHEN total_equity > 0 THEN net_income / total_equity END"),
    ("return_on_capital_employed", "profitability",
     "CASE WHEN capital_employed > 0 THEN ebit_calc / capital_employed END"),
    ("effective_tax_rate", "profitability",
     "CASE WHEN pretax_income > 0 THEN income_tax / pretax_income END"),

    # ---------------------------------------------------------------- activity
    ("receivable_days", "activity",
     "CASE WHEN revenue > 0 THEN accounts_receivable / revenue * 365 END"),
    ("inventory_days", "activity",
     "CASE WHEN cost_of_sales > 0 THEN inventory / cost_of_sales * 365 END"),
    ("payable_days", "activity",
     "CASE WHEN cost_of_sales > 0 THEN accounts_payable / cost_of_sales * 365 END"),
    ("cash_conversion_cycle", "activity",
     "CASE WHEN revenue > 0 AND cost_of_sales > 0 "
     "THEN accounts_receivable / revenue * 365 "
     "+ inventory / cost_of_sales * 365 "
     "- accounts_payable / cost_of_sales * 365 END"),
    ("asset_turnover", "activity",
     "CASE WHEN total_assets > 0 THEN revenue / total_assets END"),
    ("fixed_asset_turnover", "activity",
     "CASE WHEN ppe_net > 0 THEN revenue / ppe_net END"),
    ("capex_to_revenue", "activity",
     "CASE WHEN revenue > 0 THEN capex / revenue END"),
    ("capex_to_depreciation", "activity",
     "CASE WHEN coalesce(dep_amort_cf, dep_amort_is) > 0 "
     "THEN capex / coalesce(dep_amort_cf, dep_amort_is) END"),

    # -------------------------------------------------------- cash flow quality
    ("cfo_to_net_income", "cash_flow_quality",
     "CASE WHEN net_income > 0 THEN cfo / net_income END"),
    ("cfo_to_ebitda", "cash_flow_quality",
     "CASE WHEN ebitda > 0 THEN cfo / ebitda END"),
    ("fcf_margin", "cash_flow_quality",
     "CASE WHEN revenue > 0 THEN free_cash_flow / revenue END"),
    ("fcf_to_ebitda", "cash_flow_quality",
     "CASE WHEN ebitda > 0 THEN free_cash_flow / ebitda END"),

    # ---------------------------------------------------------------- growth
    ("revenue_growth", "growth",
     "CASE WHEN revenue_prior > 0 THEN revenue / revenue_prior - 1 END"),
    ("ebitda_growth", "growth",
     "CASE WHEN ebitda_prior > 0 THEN ebitda / ebitda_prior - 1 END"),
    ("debt_growth", "growth",
     "CASE WHEN total_debt_prior > 0 THEN total_debt / total_debt_prior - 1 END"),
    ("asset_growth", "growth",
     "CASE WHEN total_assets_prior > 0 THEN total_assets / total_assets_prior - 1 END"),
]

# Distress flags: conditions a screen should surface rather than silently drop.
FLAGS: list[tuple[str, str]] = [
    ("ebitda_negative", "ebitda IS NOT NULL AND ebitda <= 0"),
    ("equity_negative", "total_equity IS NOT NULL AND total_equity < 0"),
    ("fcf_negative", "free_cash_flow IS NOT NULL AND free_cash_flow < 0"),
    ("net_loss", "net_income IS NOT NULL AND net_income < 0"),
    ("interest_uncovered",
     "interest_expense > 0 AND ebitda IS NOT NULL AND ebitda < interest_expense"),
    ("current_ratio_below_1",
     "total_current_liabilities > 0 AND total_current_assets < total_current_liabilities"),
    ("tangible_net_worth_negative",
     "tangible_net_worth IS NOT NULL AND tangible_net_worth < 0"),
]

RATIO_NAMES = [r[0] for r in RATIOS]
FLAG_NAMES = [f[0] for f in FLAGS]

# Ratios where a HIGHER value is worse for credit. Used to orient percentile ranks so
# that "worst quartile" means the same thing whichever ratio you are looking at.
HIGHER_IS_WORSE = {
    "debt_to_ebitda", "debt_incl_leases_to_ebitda", "net_debt_to_ebitda",
    "debt_to_capital", "debt_to_assets", "liabilities_to_assets",
    "debt_to_tangible_net_worth", "net_debt_to_capital_employed",
    "receivable_days", "inventory_days", "cash_conversion_cycle",
    "capex_to_revenue", "debt_growth", "effective_tax_rate",
}
