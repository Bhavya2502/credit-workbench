"""Tracker C5 — the standardization map: XBRL tags -> bank credit spread lines.

Each spread line lists candidate tags in priority order. For a given filing the first
tag that carries a value wins, which is how one template absorbs the fact that
companies tag the same economic item differently (a pharma company reporting
`RevenueFromContractWithCustomerExcludingAssessedTax` and an older filer reporting
`SalesRevenueNet` both land on `revenue`).

Priority order is derived from observed usage across 10-K filings, most specific and
most current tag first, older or broader fallbacks after.

`statement`: IS income statement, BS balance sheet, CF cash flow, MEMO supplementary.
`sign`: +1 stored as reported; -1 flips a tag reported with the opposite natural sign
        to the spread's convention (costs positive, inflows positive).

Nothing here is silently dropped: any face-financial tag that no line claims is
reported by `staging.unmapped_tags` so the map can be extended deliberately.
"""
from __future__ import annotations

# (line_no, line_code, label, statement, [tags in priority order])
TEMPLATE: list[tuple[int, str, str, str, list[str]]] = [
    # ---------------------------------------------------------------- income statement
    (10, "revenue", "Revenue", "IS", [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues", "SalesRevenueNet", "SalesRevenueGoodsNet",
        "SalesRevenueServicesNet", "RevenuesNetOfInterestExpense",
        "RevenueFromContractWithCustomerExcludingAssessedTaxMember"]),
    (20, "cost_of_sales", "Cost of sales", "IS", [
        "CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold",
        "CostOfServices", "CostOfSales"]),
    (30, "gross_profit", "Gross profit", "IS", ["GrossProfit"]),
    (40, "selling_marketing", "Selling & marketing", "IS", [
        "SellingAndMarketingExpense", "MarketingAndAdvertisingExpense",
        "AdvertisingExpense", "SellingExpense"]),
    (50, "general_admin", "General & administrative", "IS", [
        "GeneralAndAdministrativeExpense"]),
    (60, "sgna", "Selling, general & administrative", "IS", [
        "SellingGeneralAndAdministrativeExpense"]),
    (70, "research_development", "Research & development", "IS", [
        "ResearchAndDevelopmentExpense",
        "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"]),
    (80, "labor_expense", "Labor & related expense", "IS", ["LaborAndRelatedExpense"]),
    (90, "professional_fees", "Professional fees", "IS", ["ProfessionalFees"]),
    (100, "dep_amort_is", "Depreciation & amortisation (income statement)", "IS", [
        "DepreciationAndAmortization", "DepreciationDepletionAndAmortization",
        "Depreciation", "AmortizationOfIntangibleAssets"]),
    (110, "impairment", "Impairment charges", "IS", [
        "GoodwillImpairmentLoss", "AssetImpairmentCharges",
        "ImpairmentOfIntangibleAssetsExcludingGoodwill",
        "TangibleAssetImpairmentCharges"]),
    (120, "restructuring", "Restructuring charges", "IS", [
        "RestructuringCharges", "RestructuringSettlementAndImpairmentProvisions",
        "RestructuringCostsAndAssetImpairmentCharges"]),
    (130, "share_based_comp_is", "Share-based compensation", "IS", [
        "ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"]),
    (140, "other_operating", "Other operating income/(expense)", "IS", [
        "OtherOperatingIncomeExpenseNet", "OtherCostAndExpenseOperating",
        "OtherGeneralExpense"]),
    (150, "total_operating_expenses", "Total operating expenses", "IS", [
        "OperatingExpenses", "CostsAndExpenses", "BenefitsLossesAndExpenses"]),
    (160, "operating_income", "Operating income (EBIT)", "IS", ["OperatingIncomeLoss"]),
    (170, "interest_expense", "Interest expense", "IS", [
        "InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt",
        "InterestAndDebtExpense", "InterestExpenseBorrowings"]),
    (180, "interest_income", "Interest & investment income", "IS", [
        "InvestmentIncomeInterest", "InterestIncomeExpenseNonoperatingNet",
        "InterestIncomeExpenseNet", "InvestmentIncomeInterestAndDividend"]),
    (190, "fx_gain_loss", "FX gain/(loss)", "IS", [
        "ForeignCurrencyTransactionGainLossBeforeTax",
        "ForeignCurrencyTransactionGainLossRealized"]),
    (200, "debt_extinguishment", "Gain/(loss) on debt extinguishment", "IS", [
        "GainsLossesOnExtinguishmentOfDebt", "EarlyRepaymentOfSeniorDebt"]),
    (210, "investment_gains", "Gain/(loss) on investments", "IS", [
        "GainLossOnInvestments", "DerivativeGainLossOnDerivativeNet",
        "FairValueAdjustmentOfWarrants"]),
    (220, "other_nonoperating", "Other non-operating income/(expense)", "IS", [
        "OtherNonoperatingIncomeExpense", "NonoperatingIncomeExpense",
        "OtherNonoperatingIncome", "OtherNonoperatingExpense", "OtherIncome"]),
    (230, "equity_method_income", "Share of equity-method results", "IS", [
        "IncomeLossFromEquityMethodInvestments"]),
    (240, "pretax_income", "Profit before tax", "IS", [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic"]),
    (250, "income_tax", "Income tax expense", "IS", ["IncomeTaxExpenseBenefit"]),
    (260, "discontinued_operations", "Discontinued operations, net of tax", "IS", [
        "IncomeLossFromDiscontinuedOperationsNetOfTax",
        "IncomeLossFromDiscontinuedOperationsNetOfTaxAttributableToReportingEntity"]),
    (270, "net_income_incl_minority", "Net income incl. minority interest", "IS", [
        "ProfitLoss"]),
    (280, "minority_interest_is", "Less: minority interest", "IS", [
        "NetIncomeLossAttributableToNoncontrollingInterest"]),
    (290, "net_income", "Net income", "IS", ["NetIncomeLoss"]),
    (300, "preferred_dividends", "Preferred dividends", "IS", [
        "PreferredStockDividendsIncomeStatementImpact",
        "PreferredStockDividendsAndOtherAdjustments"]),
    (310, "net_income_common", "Net income to common", "IS", [
        "NetIncomeLossAvailableToCommonStockholdersBasic"]),
    (320, "comprehensive_income", "Comprehensive income", "IS", [
        "ComprehensiveIncomeNetOfTax",
        "ComprehensiveIncomeNetOfTaxIncludingPortionAttributableToNoncontrollingInterest"]),
    (330, "eps_basic", "EPS - basic", "IS", ["EarningsPerShareBasic"]),
    (340, "eps_diluted", "EPS - diluted", "IS", ["EarningsPerShareDiluted"]),
    (350, "shares_basic", "Weighted average shares - basic", "IS", [
        "WeightedAverageNumberOfSharesOutstandingBasic"]),
    (360, "shares_diluted", "Weighted average shares - diluted", "IS", [
        "WeightedAverageNumberOfDilutedSharesOutstanding"]),

    # ---------------------------------------------------------------- balance sheet
    (400, "cash", "Cash & cash equivalents", "BS", [
        "CashAndCashEquivalentsAtCarryingValue", "Cash",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashAndDueFromBanks"]),
    (410, "short_term_investments", "Short-term investments", "BS", [
        "ShortTermInvestments", "MarketableSecuritiesCurrent",
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
        "OtherShortTermInvestments"]),
    (420, "accounts_receivable", "Accounts receivable, net", "BS", [
        "AccountsReceivableNetCurrent", "ReceivablesNetCurrent",
        "AccountsAndOtherReceivablesNetCurrent",
        "AccountsReceivableGrossCurrent"]),
    (430, "inventory", "Inventory", "BS", ["InventoryNet", "InventoryGross"]),
    (440, "prepaid_other_current", "Prepaid & other current assets", "BS", [
        "PrepaidExpenseAndOtherAssetsCurrent", "PrepaidExpenseCurrent",
        "OtherAssetsCurrent"]),
    (450, "total_current_assets", "Total current assets", "BS", ["AssetsCurrent"]),
    (460, "ppe_net", "Property, plant & equipment, net", "BS", [
        "PropertyPlantAndEquipmentNet"]),
    (470, "operating_lease_rou_asset", "Operating lease right-of-use asset", "BS", [
        "OperatingLeaseRightOfUseAsset"]),
    (480, "goodwill", "Goodwill", "BS", ["Goodwill"]),
    (490, "intangibles", "Intangible assets, net", "BS", [
        "IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet",
        "IndefiniteLivedIntangibleAssetsExcludingGoodwill"]),
    (500, "long_term_investments", "Long-term investments", "BS", [
        "LongTermInvestments", "EquityMethodInvestments",
        "MarketableSecuritiesNoncurrent"]),
    (510, "deferred_tax_asset", "Deferred tax assets", "BS", [
        "DeferredIncomeTaxAssetsNet", "DeferredTaxAssetsNetNoncurrent"]),
    (520, "other_noncurrent_assets", "Other non-current assets", "BS", [
        "OtherAssetsNoncurrent"]),
    (530, "total_assets", "Total assets", "BS", ["Assets"]),
    (540, "accounts_payable", "Accounts payable", "BS", [
        "AccountsPayableCurrent", "AccountsPayableAndAccruedLiabilitiesCurrent",
        "AccountsPayableTradeCurrent"]),
    (550, "accrued_liabilities", "Accrued liabilities", "BS", [
        "AccruedLiabilitiesCurrent", "EmployeeRelatedLiabilitiesCurrent",
        "AccruedIncomeTaxesCurrent"]),
    (560, "short_term_debt", "Short-term borrowings", "BS", [
        "ShortTermBorrowings", "NotesPayableCurrent", "OtherShortTermBorrowings",
        "CommercialPaper", "LinesOfCreditCurrent"]),
    (570, "current_portion_ltd", "Current portion of long-term debt", "BS", [
        "LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"]),
    (580, "operating_lease_current", "Operating lease liability - current", "BS", [
        "OperatingLeaseLiabilityCurrent"]),
    (590, "finance_lease_current", "Finance lease liability - current", "BS", [
        "FinanceLeaseLiabilityCurrent", "CapitalLeaseObligationsCurrent"]),
    (600, "deferred_revenue_current", "Deferred revenue - current", "BS", [
        "ContractWithCustomerLiabilityCurrent", "DeferredRevenueCurrent"]),
    (610, "other_current_liabilities", "Other current liabilities", "BS", [
        "OtherLiabilitiesCurrent"]),
    (620, "total_current_liabilities", "Total current liabilities", "BS", [
        "LiabilitiesCurrent"]),
    (630, "long_term_debt", "Long-term debt", "BS", [
        "LongTermDebtNoncurrent", "LongTermDebt",
        "LongTermDebtAndCapitalLeaseObligations", "LongTermNotesPayable"]),
    (640, "operating_lease_noncurrent", "Operating lease liability - non-current", "BS", [
        "OperatingLeaseLiabilityNoncurrent"]),
    (650, "finance_lease_noncurrent", "Finance lease liability - non-current", "BS", [
        "FinanceLeaseLiabilityNoncurrent", "CapitalLeaseObligationsNoncurrent"]),
    (660, "deferred_tax_liability", "Deferred tax liabilities", "BS", [
        "DeferredIncomeTaxLiabilitiesNet", "DeferredTaxLiabilitiesNoncurrent"]),
    (670, "pension_liability", "Pension & post-retirement liability", "BS", [
        "LiabilityDefinedBenefitPlanNoncurrent",
        "DefinedBenefitPensionPlanLiabilitiesNoncurrent",
        "PensionAndOtherPostretirementDefinedBenefitPlansLiabilitiesNoncurrent"]),
    (680, "deferred_revenue_noncurrent", "Deferred revenue - non-current", "BS", [
        "ContractWithCustomerLiabilityNoncurrent", "DeferredRevenueNoncurrent"]),
    (690, "other_noncurrent_liabilities", "Other non-current liabilities", "BS", [
        "OtherLiabilitiesNoncurrent"]),
    (700, "total_liabilities", "Total liabilities", "BS", ["Liabilities"]),
    (710, "preferred_equity", "Preferred stock", "BS", ["PreferredStockValue"]),
    (720, "common_stock", "Common stock", "BS", [
        "CommonStockValue", "CommonStockNoParValue"]),
    (730, "additional_paid_in_capital", "Additional paid-in capital", "BS", [
        "AdditionalPaidInCapital", "AdditionalPaidInCapitalCommonStock"]),
    (740, "retained_earnings", "Retained earnings / (accumulated deficit)", "BS", [
        "RetainedEarningsAccumulatedDeficit"]),
    (750, "treasury_stock", "Treasury stock", "BS", [
        "TreasuryStockValue", "TreasuryStockCommonValue"]),
    (760, "aoci", "Accumulated other comprehensive income", "BS", [
        "AccumulatedOtherComprehensiveIncomeLossNetOfTax"]),
    (770, "total_equity", "Total shareholders' equity", "BS", ["StockholdersEquity"]),
    (780, "minority_interest_bs", "Minority interest", "BS", ["MinorityInterest"]),
    (790, "total_equity_incl_minority", "Total equity incl. minority interest", "BS", [
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]),
    (800, "total_liab_and_equity", "Total liabilities & equity", "BS", [
        "LiabilitiesAndStockholdersEquity"]),

    # ---------------------------------------------------------------- cash flow
    (900, "cfo", "Cash flow from operations", "CF", [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]),
    (910, "dep_amort_cf", "Depreciation & amortisation (cash flow)", "CF", [
        "DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
        "Depreciation"]),
    (920, "share_based_comp_cf", "Share-based compensation (cash flow)", "CF", [
        "ShareBasedCompensation"]),
    (930, "deferred_tax_cf", "Deferred income taxes", "CF", [
        "DeferredIncomeTaxExpenseBenefit"]),
    (940, "wc_receivables", "Change in receivables", "CF", [
        "IncreaseDecreaseInAccountsReceivable"]),
    (950, "wc_inventory", "Change in inventory", "CF", [
        "IncreaseDecreaseInInventories"]),
    (960, "wc_payables", "Change in payables & accruals", "CF", [
        "IncreaseDecreaseInAccountsPayableAndAccruedLiabilities",
        "IncreaseDecreaseInAccountsPayable", "IncreaseDecreaseInAccruedLiabilities"]),
    (970, "wc_other", "Change in other working capital", "CF", [
        "IncreaseDecreaseInOtherOperatingCapitalNet",
        "IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets",
        "IncreaseDecreaseInOtherOperatingAssets"]),
    (980, "capex", "Capital expenditure", "CF", [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForCapitalImprovements",
        "PaymentsToAcquireProductiveAssets"]),
    (990, "acquisitions", "Acquisitions, net of cash", "CF", [
        "PaymentsToAcquireBusinessesNetOfCashAcquired",
        "PaymentsToAcquireBusinessesAndInterestInAffiliates"]),
    (1000, "asset_sales", "Proceeds from asset sales", "CF", [
        "ProceedsFromSaleOfPropertyPlantAndEquipment",
        "ProceedsFromDivestitureOfBusinesses"]),
    (1010, "cfi", "Cash flow from investing", "CF", [
        "NetCashProvidedByUsedInInvestingActivities",
        "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations"]),
    (1020, "debt_issued", "Debt issued", "CF", [
        "ProceedsFromIssuanceOfLongTermDebt", "ProceedsFromNotesPayable",
        "ProceedsFromIssuanceOfSeniorLongTermDebt", "ProceedsFromLinesOfCredit",
        "ProceedsFromRelatedPartyDebt"]),
    (1030, "debt_repaid", "Debt repaid", "CF", [
        "RepaymentsOfLongTermDebt", "RepaymentsOfDebt", "RepaymentsOfNotesPayable",
        "RepaymentsOfLinesOfCredit"]),
    (1040, "dividends_paid", "Dividends paid", "CF", [
        "PaymentsOfDividends", "PaymentsOfDividendsCommonStock",
        "PaymentsOfDividendsMinorityInterest"]),
    (1050, "buybacks", "Share buybacks", "CF", [
        "PaymentsForRepurchaseOfCommonStock"]),
    (1060, "equity_issued", "Equity issued", "CF", [
        "ProceedsFromIssuanceOfCommonStock",
        "ProceedsFromIssuanceOrSaleOfEquity"]),
    (1070, "cff", "Cash flow from financing", "CF", [
        "NetCashProvidedByUsedInFinancingActivities",
        "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations"]),
    (1080, "fx_effect_cash", "FX effect on cash", "CF", [
        "EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "EffectOfExchangeRateOnCashAndCashEquivalents"]),
    (1090, "net_change_cash", "Net change in cash", "CF", [
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect",
        "CashAndCashEquivalentsPeriodIncreaseDecrease"]),

    # ---------------------------------------------------------------- memo / credit
    (1100, "interest_paid", "Interest paid (cash)", "MEMO", [
        "InterestPaidNet", "InterestPaid", "InterestPaidCapitalized"]),
    (1110, "taxes_paid", "Income taxes paid (cash)", "MEMO", [
        "IncomeTaxesPaidNet", "IncomeTaxesPaid"]),
    (1120, "operating_lease_cost", "Operating lease cost", "MEMO", [
        "OperatingLeaseCost", "LeaseCost"]),
    (1130, "operating_lease_payments", "Operating lease payments (cash)", "MEMO", [
        "OperatingLeasePayments"]),
    (1140, "pension_benefit_obligation", "Projected benefit obligation", "MEMO", [
        "DefinedBenefitPlanBenefitObligation"]),
    (1150, "pension_plan_assets", "Pension plan assets at fair value", "MEMO", [
        "DefinedBenefitPlanFairValueOfPlanAssets"]),
    (1160, "capitalized_interest", "Capitalised interest", "MEMO", [
        "InterestCostsCapitalized", "InterestCostsCapitalizedAdjustment"]),
    (1170, "dividends_declared_ps", "Dividends declared per share", "MEMO", [
        "CommonStockDividendsPerShareDeclared"]),
    (1180, "shares_outstanding", "Shares outstanding", "MEMO", [
        "CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding"]),
]

# Derived lines computed in C6 from the mapped values above.
DERIVED = [
    (1200, "ebitda", "EBITDA"),
    (1210, "total_debt", "Total debt"),
    (1220, "total_debt_incl_leases", "Total debt incl. leases"),
    (1230, "net_debt", "Net debt"),
    (1240, "working_capital", "Working capital"),
    (1250, "free_cash_flow", "Free cash flow"),
    (1260, "funds_from_operations", "Funds from operations (FFO)"),
    (1270, "tangible_net_worth", "Tangible net worth"),
    (1280, "capital_employed", "Capital employed"),
]


def rows() -> list[tuple]:
    """Flatten to (line_no, line_code, label, statement, tag, priority)."""
    out = []
    for line_no, code, label, stmt, tags in TEMPLATE:
        for priority, tag in enumerate(tags):
            out.append((line_no, code, label, stmt, tag, priority))
    return out


ALL_MAPPED_TAGS = {tag for _, _, _, _, tags in TEMPLATE for tag in tags}
