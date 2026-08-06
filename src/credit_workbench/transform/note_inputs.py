"""Tracker D1 — note-level inputs for rating-agency adjustments.

Agency adjustments need figures that never appear on the face of the statements: the
lease maturity ladder and discount rate, the pension benefit obligation against plan
assets, the debt repayment schedule, capitalised interest, guarantees. All of it is
tagged in the notes, and this pulls it into two shapes:

  staging.note_inputs      long form, every captured note fact with its category
  marts.adjustment_inputs  one row per company-year, wide, ready for D2/D3

Nothing is adjusted here — that is D3's job. This stage only assembles the raw
material, so the arithmetic of an adjustment can be reviewed separately from the
question of whether the right figure was picked up.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import R2, motherduck_token

LAKE = "r2://credit-workbench-raw"
PIT = f"{LAKE}/parquet/derived/facts_pit"
OUT = f"{LAKE}/parquet/derived/note_inputs"

# category -> {output column: [tags in priority order]}
CATEGORIES: dict[str, dict[str, list[str]]] = {
    "operating_lease": {
        "op_lease_liability": ["OperatingLeaseLiability"],
        "op_lease_liability_current": ["OperatingLeaseLiabilityCurrent"],
        "op_lease_liability_noncurrent": ["OperatingLeaseLiabilityNoncurrent"],
        "op_lease_rou_asset": ["OperatingLeaseRightOfUseAsset"],
        "op_lease_cost": ["OperatingLeaseCost", "LeaseCost"],
        "op_lease_cash_paid": ["OperatingLeasePayments"],
        "op_lease_discount_rate": ["OperatingLeaseWeightedAverageDiscountRatePercent"],
        "op_lease_remaining_term": ["OperatingLeaseWeightedAverageRemainingLeaseTerm1"],
        "op_lease_due_y1": ["LesseeOperatingLeaseLiabilityPaymentsDueNextTwelveMonths"],
        "op_lease_due_y2": ["LesseeOperatingLeaseLiabilityPaymentsDueYearTwo"],
        "op_lease_due_y3": ["LesseeOperatingLeaseLiabilityPaymentsDueYearThree"],
        "op_lease_due_y4": ["LesseeOperatingLeaseLiabilityPaymentsDueYearFour"],
        "op_lease_due_y5": ["LesseeOperatingLeaseLiabilityPaymentsDueYearFive"],
        "op_lease_due_thereafter": ["LesseeOperatingLeaseLiabilityPaymentsDueAfterYearFive"],
        "op_lease_undiscounted_total": ["LesseeOperatingLeaseLiabilityPaymentsDue"],
        "op_lease_imputed_interest": ["LesseeOperatingLeaseLiabilityUndiscountedExcessAmount"],
    },
    "finance_lease": {
        "fin_lease_liability": ["FinanceLeaseLiability"],
        "fin_lease_liability_current": ["FinanceLeaseLiabilityCurrent"],
        "fin_lease_liability_noncurrent": ["FinanceLeaseLiabilityNoncurrent"],
        "fin_lease_rou_asset": ["FinanceLeaseRightOfUseAsset"],
        "fin_lease_interest": ["FinanceLeaseInterestExpense"],
        "fin_lease_amortisation": ["FinanceLeaseRightOfUseAssetAmortization"],
    },
    "pension_opeb": {
        "pension_obligation": ["DefinedBenefitPlanBenefitObligation"],
        "pension_plan_assets": ["DefinedBenefitPlanFairValueOfPlanAssets"],
        "pension_funded_status": ["DefinedBenefitPlanFundedStatusOfPlan"],
        "pension_service_cost": ["DefinedBenefitPlanServiceCost"],
        "pension_interest_cost": ["DefinedBenefitPlanInterestCost"],
        "pension_expected_return": ["DefinedBenefitPlanExpectedReturnOnPlanAssets"],
        "pension_net_periodic_cost": ["DefinedBenefitPlanNetPeriodicBenefitCost"],
        "pension_contributions": ["DefinedBenefitPlanContributionsByEmployer"],
        "pension_discount_rate": [
            "DefinedBenefitPlanAssumptionsUsedCalculatingBenefitObligationDiscountRate"],
    },
    "debt_schedule": {
        "debt_due_y1": ["LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths"],
        "debt_due_y2": ["LongTermDebtMaturitiesRepaymentsOfPrincipalInYearTwo"],
        "debt_due_y3": ["LongTermDebtMaturitiesRepaymentsOfPrincipalInYearThree"],
        "debt_due_y4": ["LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFour"],
        "debt_due_y5": ["LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFive"],
        "debt_due_thereafter": ["LongTermDebtMaturitiesRepaymentsOfPrincipalAfterYearFive"],
        "debt_due_remainder_fy": [
            "LongTermDebtMaturitiesRepaymentsOfPrincipalRemainderOfFiscalYear"],
        "debt_unamortised_discount": [
            "DebtInstrumentUnamortizedDiscount",
            "DebtInstrumentUnamortizedDiscountPremiumAndDebtIssuanceCostsNet"],
        "debt_fair_value": ["DebtInstrumentFairValue", "LongTermDebtFairValue"],
    },
    "one_off_items": {
        "restructuring_charge": ["RestructuringCharges"],
        "impairment_goodwill": ["GoodwillImpairmentLoss"],
        "impairment_assets": ["AssetImpairmentCharges"],
        "debt_extinguishment_gain_loss": ["GainsLossesOnExtinguishmentOfDebt"],
    },
    "off_balance_sheet": {
        "capitalised_interest": ["InterestCostsCapitalized"],
        "guarantee_max_exposure": ["GuaranteeObligationsMaximumExposure"],
        "loss_contingency_accrual": ["LossContingencyAccrualAtCarryingValue"],
        "loss_contingency_possible": ["LossContingencyEstimateOfPossibleLoss"],
        "purchase_obligation": ["UnrecordedUnconditionalPurchaseObligation",
                                "PurchaseObligation"],
    },
}

ALL_TAGS = sorted({t for cols in CATEGORIES.values() for tags in cols.values()
                   for t in tags})
TAG_TO_COL = {t: (cat, col)
              for cat, cols in CATEGORIES.items()
              for col, tags in cols.items() for t in tags}
PRIORITY = {t: i for cols in CATEGORIES.values() for tags in cols.values()
            for i, t in enumerate(tags)}


def connect() -> duckdb.DuckDBPyConnection:
    cfg = R2.from_env()
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"""
        CREATE OR REPLACE SECRET r2_lake (
            TYPE R2, KEY_ID '{cfg.access_key_id}', SECRET '{cfg.secret_access_key}',
            ACCOUNT_ID '{cfg.account_id}', REGION 'auto')""")
    con.execute("SET memory_limit = '9GB'")
    con.execute("SET preserve_insertion_order = false")
    con.execute("SET temp_directory = '/tmp/duckdb'")
    return con


def main() -> None:
    con = connect()
    con.execute("""CREATE OR REPLACE TABLE tagmap (
                       tag VARCHAR, category VARCHAR, col VARCHAR, priority INTEGER)""")
    con.executemany("INSERT INTO tagmap VALUES (?, ?, ?, ?)",
                    [(t, TAG_TO_COL[t][0], TAG_TO_COL[t][1], PRIORITY[t])
                     for t in ALL_TAGS])
    print(f"{len(ALL_TAGS)} note tags across {len(CATEGORIES)} categories")

    print("Extracting note inputs ...")
    con.execute(f"""
        COPY (
            SELECT f.cik, f.company_name, f.sic, f.basis, f.period_end, f.qtrs, f.fy,
                   f.adsh, f.filed, m.category, m.col, f.tag AS source_tag,
                   f.value, f.uom, f.period_year
            FROM (
                SELECT *, 'latest' AS basis FROM read_parquet(
                    '{PIT}/*/*.parquet', hive_partitioning = true) WHERE is_latest
                UNION ALL BY NAME
                SELECT *, 'first_reported' AS basis FROM read_parquet(
                    '{PIT}/*/*.parquet', hive_partitioning = true) WHERE is_first_report
            ) f
            JOIN tagmap m ON m.tag = f.tag
            QUALIFY row_number() OVER (
                PARTITION BY f.cik, f.basis, f.period_end, f.qtrs, m.col
                ORDER BY m.priority, f.filed DESC) = 1
        ) TO '{OUT}' (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (period_year),
                      OVERWRITE_OR_IGNORE, FILENAME_PATTERN 'ni_{{i}}')""")
    n = con.execute(f"SELECT count(*) FROM read_parquet('{OUT}/*/*.parquet')").fetchone()[0]
    print(f"  {n:,} note facts captured")

    # ------------------------------------------------------------------ wide mart
    cols = [c for cat in CATEGORIES.values() for c in cat]
    pivot = ",\n               ".join(
        f"max(CASE WHEN col = '{c}' THEN value END) AS {c}" for c in cols)

    md = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    md.execute("DROP VIEW IF EXISTS staging.note_inputs")
    md.execute(f"""
        CREATE VIEW staging.note_inputs AS
        SELECT * FROM read_parquet('{OUT}/*/*.parquet', hive_partitioning = true)""")
    print("view  staging.note_inputs")

    md.execute(f"""
        CREATE OR REPLACE TABLE marts.adjustment_inputs AS
        SELECT cik, any_value(company_name) AS company_name, any_value(sic) AS sic,
               basis, period_end, max(fy) AS fy,
               {pivot}
        FROM staging.note_inputs
        WHERE qtrs IN (0, 4)
        GROUP BY cik, basis, period_end""")
    rows, companies = md.execute(
        "SELECT count(*), count(DISTINCT cik) FROM marts.adjustment_inputs").fetchone()
    print(f"table marts.adjustment_inputs  {rows:,} company-periods, "
          f"{companies:,} companies, {len(cols)} input columns")


if __name__ == "__main__":
    main()
