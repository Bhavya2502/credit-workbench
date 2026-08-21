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
    # `Revenues` leads deliberately. Where a filer tags both, `Revenues` is the
    # statutory total and the contract-revenue tag is only the part arising from
    # customer contracts: Pfizer FY2023 reports $58.5bn of Revenues against $50.9bn
    # of contract revenue, the difference being alliance and royalty income. Filers
    # whose revenue is entirely from contracts tag only the latter, so it still wins
    # by fallback.
    #
    # Everything after the IFRS pair below was added on 21 Aug 2026, measured against
    # SEC's own companyfacts rather than against our derived layer. Xcel Energy's
    # FY2023 revenue of $14.206bn was in our fact base under
    # `RegulatedAndUnregulatedOperatingRevenue` and no line claimed it; the same was
    # true of Novartis at $45.4bn under IFRS. The map held nine tags, all of them
    # commercial-company tags, which is why 24.6% of company-years carried no revenue.
    #
    # ONE RULE decides what is admitted: a tag must be a TOTAL revenue measure. A
    # component understates the line while looking entirely plausible, which is the
    # worst failure this map can have. So these are deliberately refused, with the
    # count of company-years they would have "fixed" in a 200-row sample:
    #     RevenueFromRelatedParties (7)          revenue from related parties only
    #     RegulatedOperatingRevenueGas (2)       the gas half of a utility
    #     UnregulatedOperatingRevenue (2)        the unregulated half
    #     InterestAndFeeIncomeLoansAndLeases     one component of interest income
    #     RevenuesIncludingIntersegmentRevenues  includes what consolidation removes
    #     RevenueFromSaleOfCrudeOil / ...NaturalGas   one product each
    #     OtherHotelOperatingRevenue             the "other" bucket
    # A null is recoverable later; a wrong number is not detectable at all.
    #
    # BANKS: `InterestAndDividendIncomeOperating` is total interest and dividend
    # income - a lender's top line. It sits LAST so `Revenues` and
    # `RevenuesNetOfInterestExpense` win wherever a filer tags them. Note what this
    # means: for a bank falling through to it, revenue is GROSS interest income and
    # excludes non-interest income. That is a convention, not a fact, and it is
    # auditable - `marts.spread_lines.source_tag` records which tag supplied every
    # figure, so `WHERE source_tag = 'InterestAndDividendIncomeOperating'` isolates
    # every company-year on this basis.
    (10, "revenue", "Revenue", "IS", [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet", "SalesRevenueGoodsNet",
        "SalesRevenueServicesNet", "RevenuesNetOfInterestExpense",
        # IFRS taxonomy, used by foreign private issuers filing 20-F
        "Revenue", "RevenueFromContractsWithCustomers",
        # regulated utilities - the combined total first, then pure-play totals
        "RegulatedAndUnregulatedOperatingRevenue",
        "RegulatedOperatingRevenue", "ElectricUtilityRevenue",
        # oil and gas
        "OilAndGasRevenue", "OilAndGasSalesRevenue",
        "RevenueFromSaleOfOilAndGasProducts",
        # health care and construction
        "HealthCareOrganizationRevenue",
        "HealthCareOrganizationRevenueNetOfPatientServiceRevenueProvisions",
        "ContractsRevenue", "RevenueFromLeasedAndOwnedHotels",
        # IFRS industry totals, used by 20-F filers
        "RevenueFromSaleOfGoods", "RevenueFromRenderingOfServices",
        "RevenueFromRenderingOfTelecommunicationServices",
        "RevenueFromRenderingOfTransportServices", "RevenueFromSaleOfGold",
        # banks last: gross interest and dividend income. See the note above.
        "InterestAndDividendIncomeOperating"]),
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
    # `DepreciationAmortizationAndAccretionNet` matters more than its name suggests:
    # Walmart moved to it in 2019 and without it their EBITDA collapses to EBIT,
    # understating it by about $13bn a year. Broad measures lead; `Depreciation`
    # alone trails because it excludes amortisation.
    (100, "dep_amort_is", "Depreciation & amortisation (income statement)", "IS", [
        "DepreciationAndAmortization", "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationDepletionAndAmortizationIncludingDiscontinuedOperations",
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
        "CashAndDueFromBanks", "CashAndCashEquivalents"]),
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
    # `LongTermDebtNoncurrent` excludes current maturities; `LongTermDebt` is the
    # whole facility including them. Mixing the two and then adding the current
    # portion separately double-counts it, so they occupy different lines and the
    # debt build-up below chooses between them.
    (630, "long_term_debt", "Long-term debt (non-current portion)", "BS", [
        "LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations",
        "LongTermNotesPayable"]),
    (635, "long_term_debt_total", "Long-term debt (incl. current maturities)", "BS", [
        "LongTermDebt"]),
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
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        "CashFlowsFromUsedInOperatingActivities"]),
    (910, "dep_amort_cf", "Depreciation & amortisation (cash flow)", "CF", [
        "DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationDepletionAndAmortizationIncludingDiscontinuedOperations",
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
    # The IFRS tag below is the same gap as on revenue: a 20-F filer's capex was
    # sitting in the fact base unclaimed. The PP&E variants are US filers who tag a
    # narrower asset class than the headline tag. `PaymentsToAcquireRealEstate` is
    # admitted because for a property company it IS the capital expenditure; the
    # securities and loan `PaymentsToAcquire*` tags are refused - buying a bond is
    # not capex, and a broad pattern match over that prefix is what made an earlier
    # estimate of this gap 1,192 companies when the true figure was nearer 500.
    (980, "capex", "Capital expenditure", "CF", [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForCapitalImprovements",
        "PaymentsToAcquireProductiveAssets",
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
        "PaymentsToAcquireOtherPropertyPlantAndEquipment",
        "PaymentsToAcquireMachineryAndEquipment",
        "PaymentsToAcquireOtherProductiveAssets",
        "PaymentsToAcquireRealEstate"]),
    (990, "acquisitions", "Acquisitions, net of cash", "CF", [
        "PaymentsToAcquireBusinessesNetOfCashAcquired",
        "PaymentsToAcquireBusinessesAndInterestInAffiliates"]),
    (1000, "asset_sales", "Proceeds from asset sales", "CF", [
        "ProceedsFromSaleOfPropertyPlantAndEquipment",
        "ProceedsFromDivestitureOfBusinesses"]),
    (1010, "cfi", "Cash flow from investing", "CF", [
        "NetCashProvidedByUsedInInvestingActivities",
        "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",
        "CashFlowsFromUsedInInvestingActivities"]),
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
        "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
        "CashFlowsFromUsedInFinancingActivities"]),
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


def _assert_one_line_per_tag() -> None:
    """A tag may feed two lines only if they sit on different statements.

    The spread resolves each line independently, so a tag claimed by two lines of the
    SAME statement is counted twice and every subtotal built on them is wrong, while
    each individual figure still looks correct - the failure mode nothing downstream
    can detect.

    Across statements it is legitimate and load-bearing.
    `DepreciationDepletionAndAmortization` feeds both `dep_amort_is` and
    `dep_amort_cf` because D&A is presented on both statements, and `build_lines`
    separates them by preferring facts whose own `stmt` matches the line's statement:

        ORDER BY CASE WHEN stmt = statement THEN 0 ELSE 1 END, priority, filed DESC

    So the key is (tag, statement), not tag. Checked at import, so a bad edit cannot
    reach a build.
    """
    owner: dict[tuple[str, str], str] = {}
    for _, code, _, stmt, tags in TEMPLATE:
        if len(tags) != len(set(tags)):
            dupe = [t for t in tags if tags.count(t) > 1]
            raise ValueError(f"{code} lists a tag twice: {sorted(set(dupe))}")
        for tag in tags:
            key = (tag, stmt)
            if key in owner and owner[key] != code:
                raise ValueError(
                    f"tag {tag!r} is claimed by both {owner[key]!r} and {code!r} on "
                    f"statement {stmt}; both lines would take the same figure")
            owner[key] = code


_assert_one_line_per_tag()


def rows() -> list[tuple]:
    """Flatten to (line_no, line_code, label, statement, tag, priority)."""
    out = []
    for line_no, code, label, stmt, tags in TEMPLATE:
        for priority, tag in enumerate(tags):
            out.append((line_no, code, label, stmt, tag, priority))
    return out


ALL_MAPPED_TAGS = {tag for _, _, _, _, tags in TEMPLATE for tag in tags}
