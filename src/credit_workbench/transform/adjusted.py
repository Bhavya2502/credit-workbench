"""G-04 and G-05 — agency-style adjusted metrics, with the policy made explicit.

Methodologies score adjusted figures, not reported ones: for retail and airlines especially,
unadjusted leverage is close to meaningless. `marts.adjustment_inputs` has held the
ingredients for months and nothing computed the arithmetic.

**The policy is data, not a constant.** The request was for the adjustment policy to be
exposed as parameters so a design tool can offer them as user choices rather than inherit
one house view. A SQL view cannot take arguments, so instead every company-year is computed
under several *named* policies and the policy definitions live in `ref.adjustment_policy`
where they can be read, cited and disagreed with. A consumer picks a `policy` the way they
would pick a `basis`. Nothing is hard-coded and nothing is hidden; `reported` is included so
the cost of each adjustment is visible rather than assumed.

**G-05, the ASC 840 to 842 splice.** Measured, the handover is clean: the balance-sheet
operating lease liability appears in 17 filings for FY2017 and 115 for FY2018, then 11,522
for FY2019 when ASC 842 took effect, while the old rent disclosure falls from 2,882 in
FY2014 to 21 by FY2025. Only 1,203 of 452,942 rows carry both. So the splice is a documented
coalesce rather than an estimation problem, which is a much better answer than the request
expected - it asked for a defensible fudge and the data supports a rule.

The rule, stated once so any threshold cut from it is reproducible: **use the reported lease
liability whenever it exists; otherwise capitalise the old rent disclosure.** How the second
is done is what the policies differ on. The raw columns stay untouched either way, so anyone
preferring their own splice still has everything they need.

**What is deliberately approximate.** FFO here is EBITDA less cash interest and cash tax,
which is a standard approximation and not any agency's exact definition - agencies adjust
further for items this warehouse does not isolate. It is labelled `ffo_approx` rather than
`ffo` for that reason. An adjusted number that pretends to more precision than its inputs
support is worse than an honest approximation.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

# Named policies. Each row states every parameter, so a consumer can read what they are
# getting instead of inferring it from a column name.
POLICIES = [
    # name, capitalise_op_leases, lease_multiple, prefer_reported_liability,
    # include_pension_deficit, description
    ("reported", False, None, False, False,
     "No adjustments. The baseline, so the cost of every adjustment below is visible."),
    ("lease_8x", True, 8.0, True, True,
     "Operating leases at the reported liability where it exists, else 8x rent expense - "
     "the long-standing agency convention. Unfunded pension added to debt in full."),
    ("lease_6x", True, 6.0, True, True,
     "As lease_8x but a 6x multiple, which suits shorter-lived leases. Published so the "
     "sensitivity of a threshold to the multiple can be measured rather than argued."),
    ("lease_only", True, 8.0, True, False,
     "Leases capitalised at 8x, pension deficit excluded from debt."),
    ("pension_only", False, None, False, True,
     "Pension deficit in debt, operating leases left uncapitalised."),
]

POLICY_TABLE = """
CREATE OR REPLACE TABLE ref.adjustment_policy (
    policy VARCHAR, capitalise_op_leases BOOLEAN, lease_multiple DOUBLE,
    prefer_reported_liability BOOLEAN, include_pension_deficit BOOLEAN,
    description VARCHAR)
"""

# One row per (cik, fy, basis) with the base figures the adjustments need.
#
# spread_lines is keyed on (cik, period_end, qtrs, statement, line_no, basis), so a fiscal
# year can hold more than one period_end - a fiscal-year change, or a transition period.
# Pivoting without collapsing that first would produce two rows per company-year and double
# every total. One period_end per (cik, fy, basis) is chosen, the latest.
BASE = """
CREATE OR REPLACE TEMP TABLE base AS
-- qtrs = 0 is an instant and qtrs = 4 is an annual flow. Both are needed: every debt,
-- lease and asset line is a balance-sheet instant, so filtering to qtrs = 4 kept the
-- income statement and cash flow and silently discarded the entire balance sheet. The
-- result was adjusted_leverage NULL on all 726,690 rows - and two invariants that
-- "passed" only because a NULL comparison counts as neither true nor false.
WITH picked AS (
    SELECT cik, fy, basis, max(period_end) AS period_end
    FROM marts.spread_lines
    WHERE qtrs IN (0, 4) AND fy IS NOT NULL
    GROUP BY cik, fy, basis
),
lines AS (
    SELECT l.cik, l.fy, l.basis, l.line_code, l.value
    FROM marts.spread_lines l
    JOIN picked p ON p.cik = l.cik AND p.fy = l.fy AND p.basis = l.basis
                 AND p.period_end = l.period_end
    WHERE l.qtrs IN (0, 4)
)
SELECT cik, fy, basis,
       max(value) FILTER (WHERE line_code = 'operating_income') AS ebit,
       coalesce(max(value) FILTER (WHERE line_code = 'dep_amort_is'),
                max(value) FILTER (WHERE line_code = 'dep_amort_cf')) AS dep_amort,
       max(value) FILTER (WHERE line_code = 'revenue') AS revenue,
       max(value) FILTER (WHERE line_code = 'interest_expense') AS interest_expense,
       max(value) FILTER (WHERE line_code = 'interest_paid') AS interest_paid,
       max(value) FILTER (WHERE line_code = 'taxes_paid') AS taxes_paid,
       max(value) FILTER (WHERE line_code = 'cfo') AS cfo,
       max(value) FILTER (WHERE line_code = 'total_assets') AS total_assets,
       -- Debt. long_term_debt_total already includes current maturities; where it is
       -- absent the non-current portion and the current portion are added instead. Short
       -- term borrowings are a separate facility, so they add in either case.
       --
       -- `has_any_debt_line` exists because coalescing an absent debt line to zero says
       -- "this company has no debt" when the truth is "no debt line was reported". The
       -- first version did that and produced a median adjusted leverage of 0.00 across
       -- 41,336 company-years - a number that looked like a result. Debt is NULL unless
       -- at least one line was actually present.
       count(*) FILTER (WHERE line_code IN ('long_term_debt_total', 'long_term_debt',
                                            'current_portion_ltd', 'short_term_debt')
                          AND value IS NOT NULL) > 0 AS has_any_debt_line,
       coalesce(max(value) FILTER (WHERE line_code = 'long_term_debt_total'),
                coalesce(max(value) FILTER (WHERE line_code = 'long_term_debt'), 0)
                + coalesce(max(value) FILTER (WHERE line_code = 'current_portion_ltd'), 0))
           AS long_term_debt,
       coalesce(max(value) FILTER (WHERE line_code = 'short_term_debt'), 0)
           AS short_term_debt,
       coalesce(max(value) FILTER (WHERE line_code = 'finance_lease_current'), 0)
       + coalesce(max(value) FILTER (WHERE line_code = 'finance_lease_noncurrent'), 0)
           AS finance_lease_debt,
       -- ASC 842 operating lease liability, straight off the balance sheet.
       nullif(coalesce(max(value) FILTER (WHERE line_code = 'operating_lease_current'), 0)
              + coalesce(max(value) FILTER (
                    WHERE line_code = 'operating_lease_noncurrent'), 0), 0)
           AS op_lease_liability_bs,
       max(value) FILTER (WHERE line_code = 'operating_lease_cost') AS op_lease_cost
FROM lines GROUP BY cik, fy, basis
"""

# The lease and pension ingredients that only adjustment_inputs carries.
INPUTS = """
CREATE OR REPLACE TEMP TABLE inputs AS
SELECT cik, fy, basis,
       max(op_lease_liability) AS op_lease_liability,
       max(op_lease_840_rent_expense) AS rent_840,
       max(op_lease_840_total) AS op_lease_840_ladder,
       max(op_lease_discount_rate) AS discount_rate,
       max(pension_obligation) AS pension_obligation,
       max(pension_plan_assets) AS pension_plan_assets,
       max(pension_funded_status) AS pension_funded_status
FROM marts.adjustment_inputs
WHERE fy IS NOT NULL
GROUP BY cik, fy, basis
"""

# G-05, published as a table rather than a view. It reads `base` and `inputs`, which are
# temp tables, and a view over a temp table resolves at query time - so the first version
# was permanently broken the moment the build session ended, which the invariant suite
# caught by asking the view a question after the build had finished.
LEASE_VIEW = """
CREATE OR REPLACE TABLE marts.lease_adjustment AS
-- Clamped identically to the mart. Without that the two disagreed on 139 rows where a
-- filer reported a negative lease liability: the view called it ASC 842 and the mart, which
-- clamps, fell through to the 840 branch.
SELECT b.cik, b.fy, b.basis,
       nullif(greatest(coalesce(i.op_lease_liability, b.op_lease_liability_bs), 0), 0)
           AS reported_lease_liability,
       i.rent_840,
       i.op_lease_840_ladder,
       i.discount_rate,
       nullif(greatest(coalesce(b.op_lease_cost, i.rent_840), 0), 0) AS rent_or_lease_cost,
       CASE
           WHEN nullif(greatest(coalesce(i.op_lease_liability,
                                         b.op_lease_liability_bs), 0), 0) IS NOT NULL
               THEN 'asc842_reported_liability'
           WHEN i.rent_840 IS NOT NULL THEN 'asc840_rent_capitalised'
           WHEN i.op_lease_840_ladder IS NOT NULL THEN 'asc840_ladder_only'
           ELSE 'none'
       END AS lease_source
FROM base b LEFT JOIN inputs i ON i.cik = b.cik AND i.fy = b.fy AND i.basis = b.basis
"""

ADJUSTED = """
CREATE OR REPLACE TABLE marts.adjusted_metrics AS
WITH j AS (
    SELECT b.*, i.op_lease_liability, i.rent_840, i.op_lease_840_ladder,
           i.discount_rate, i.pension_obligation, i.pension_plan_assets,
           i.pension_funded_status,
           -- A lease liability and a rent expense are both non-negative by definition.
           -- Where a filer or a tag mapping produces a negative one it is a data error,
           -- and letting it through made adjusted debt fall below reported debt on 21
           -- rows and EBITDAR fall below EBITDA on 35 - both impossible, and both then
           -- inverted the lease multiple so that 8x capitalised less than 6x.
           nullif(greatest(coalesce(i.op_lease_liability, b.op_lease_liability_bs), 0), 0)
               AS reported_lease_liability,
           nullif(greatest(coalesce(b.op_lease_cost, i.rent_840), 0), 0)
               AS rent_or_lease_cost
    FROM base b LEFT JOIN inputs i
      ON i.cik = b.cik AND i.fy = b.fy AND i.basis = b.basis
),
priced AS (
    SELECT j.*, p.policy, p.capitalise_op_leases, p.lease_multiple,
           p.prefer_reported_liability, p.include_pension_deficit,
           -- The splice. Reported liability first where the policy prefers it, then the
           -- old rent disclosure at the policy's multiple.
           CASE
               WHEN NOT p.capitalise_op_leases THEN 0.0
               WHEN p.prefer_reported_liability
                    AND j.reported_lease_liability IS NOT NULL
                   THEN j.reported_lease_liability
               WHEN j.rent_840 IS NOT NULL THEN j.rent_840 * p.lease_multiple
               ELSE 0.0
           END AS capitalised_leases,
           CASE
               WHEN NOT p.include_pension_deficit THEN 0.0
               WHEN j.pension_obligation IS NOT NULL AND j.pension_plan_assets IS NOT NULL
                   THEN greatest(j.pension_obligation - j.pension_plan_assets, 0.0)
               WHEN j.pension_funded_status IS NOT NULL
                   THEN greatest(-j.pension_funded_status, 0.0)
               ELSE 0.0
           END AS pension_deficit
    FROM j CROSS JOIN ref.adjustment_policy p
)
SELECT cik, fy, basis, policy,
       ebit, dep_amort, revenue, interest_expense, cfo, total_assets,
       ebit + coalesce(dep_amort, 0) AS ebitda,
       rent_or_lease_cost AS rent,
       -- The rent add-back is part of the same adjustment as capitalising the lease: if
       -- the lease is put into debt then its rent belongs back in earnings, and if it is
       -- not then neither does the rent. Adding it under every policy meant the
       -- `reported` baseline adjusted its own denominator while leaving the numerator
       -- alone, which is what let leverage *fall* when leases were capitalised.
       ebit + coalesce(dep_amort, 0)
           + CASE WHEN capitalise_op_leases THEN coalesce(rent_or_lease_cost, 0) ELSE 0 END
           AS ebitdar,
       CASE WHEN has_any_debt_line THEN long_term_debt + short_term_debt END
           AS reported_debt,
       finance_lease_debt,
       capitalised_leases,
       pension_deficit,
       CASE WHEN has_any_debt_line
            THEN long_term_debt + short_term_debt + finance_lease_debt
                 + capitalised_leases + pension_deficit END AS adjusted_debt,
       -- Labelled approx deliberately: EBITDA less cash interest and cash tax is a
       -- standard shape, not any agency's exact FFO.
       nullif(ebit + coalesce(dep_amort, 0)
              - coalesce(interest_paid, interest_expense, 0)
              - coalesce(taxes_paid, 0), 0) AS ffo_approx,
       CASE WHEN has_any_debt_line
             AND (ebit + coalesce(dep_amort, 0)
                  + CASE WHEN capitalise_op_leases THEN coalesce(rent_or_lease_cost, 0)
                         ELSE 0 END) > 0
            THEN (long_term_debt + short_term_debt + finance_lease_debt
                  + capitalised_leases + pension_deficit)
                 / (ebit + coalesce(dep_amort, 0)
                    + CASE WHEN capitalise_op_leases
                           THEN coalesce(rent_or_lease_cost, 0) ELSE 0 END)
       END AS adjusted_leverage,
       -- Fixed-charge cover always carries the rent in both halves: it is a measure of
       -- whether earnings service the fixed charges, and rent is one whether or not the
       -- lease has been capitalised into debt.
       CASE WHEN (coalesce(interest_expense, 0) + coalesce(rent_or_lease_cost, 0)) > 0
            THEN (ebit + coalesce(dep_amort, 0) + coalesce(rent_or_lease_cost, 0))
                 / (coalesce(interest_expense, 0) + coalesce(rent_or_lease_cost, 0))
       END AS fixed_charge_cover,
       CASE WHEN has_any_debt_line
             AND (long_term_debt + short_term_debt + finance_lease_debt
                  + capitalised_leases + pension_deficit) > 0
            THEN (ebit + coalesce(dep_amort, 0)
                  - coalesce(interest_paid, interest_expense, 0)
                  - coalesce(taxes_paid, 0))
                 / (long_term_debt + short_term_debt + finance_lease_debt
                    + capitalised_leases + pension_deficit)
       END AS ffo_to_adjusted_debt,
       CASE
           WHEN reported_lease_liability IS NOT NULL THEN 'asc842_reported_liability'
           WHEN rent_840 IS NOT NULL THEN 'asc840_rent_capitalised'
           ELSE 'none'
       END AS lease_source
FROM priced
WHERE ebit IS NOT NULL OR cfo IS NOT NULL
"""


def drop_object(con, schema: str, name: str) -> None:
    """Drop whatever is there, whichever kind it is.

    An earlier build published `lease_adjustment` as a view and this one publishes a table,
    and `CREATE OR REPLACE TABLE` will not replace an object of a different type. Issuing
    both `DROP VIEW IF EXISTS` and `DROP TABLE IF EXISTS` does not fix it either: `IF
    EXISTS` suppresses the error for an object that is absent, not for one of the wrong
    type, so the view drop raised on a table. Asking the catalogue first is the only
    version that is genuinely re-runnable.
    """
    got = con.execute("""
        SELECT table_type FROM information_schema.tables
        WHERE table_schema = ? AND table_name = ?""", [schema, name]).fetchone()
    if not got:
        return
    kind = "VIEW" if got[0] == "VIEW" else "TABLE"
    con.execute(f"DROP {kind} {schema}.{name}")
    print(f"drop  {schema}.{name} (was a {kind.lower()})")


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    con.execute("SET temp_directory = '/tmp/duckdb_spill'")
    con.execute("SET preserve_insertion_order = false")
    con.execute("CREATE SCHEMA IF NOT EXISTS marts")
    con.execute("CREATE SCHEMA IF NOT EXISTS ref")

    con.execute(POLICY_TABLE)
    con.executemany(
        "INSERT INTO ref.adjustment_policy VALUES (?, ?, ?, ?, ?, ?)", POLICIES)
    print(f"table ref.adjustment_policy  {len(POLICIES)} policies")

    for label, sql in (("base", BASE), ("inputs", INPUTS)):
        con.execute(sql)
        n = con.execute(f"SELECT count(*) FROM {label}").fetchone()[0]
        print(f"temp  {label:<8} {n:,} rows")

    # The pivot must not have multiplied company-years. Asserted here rather than hoped
    # for: spread_lines can hold two period_ends in one fiscal year.
    rows, keys = con.execute(
        "SELECT count(*), count(DISTINCT (cik, fy, basis)) FROM base").fetchone()
    if rows != keys:
        raise SystemExit(f"base is not one row per (cik, fy, basis): {rows:,} vs {keys:,}")
    print(f"guard base is one row per (cik, fy, basis)  {rows:,}")

    drop_object(con, "marts", "lease_adjustment")
    con.execute(LEASE_VIEW)
    n = con.execute("SELECT count(*) FROM marts.lease_adjustment").fetchone()[0]
    print(f"view  marts.lease_adjustment  {n:,} rows")

    con.execute(ADJUSTED)
    rows, cos = con.execute("""
        SELECT count(*), count(DISTINCT cik) FROM marts.adjusted_metrics""").fetchone()
    print(f"table marts.adjusted_metrics  {rows:,} rows, {cos:,} companies")

    print("\nWhat each policy does to leverage (fy 2024, first_reported, median):")
    cur = con.execute("""
        SELECT policy,
               count(*) FILTER (WHERE adjusted_leverage IS NOT NULL) AS with_leverage,
               round(median(adjusted_leverage), 2) AS median_leverage,
               round(median(capitalised_leases), 0) AS median_cap_leases,
               round(median(fixed_charge_cover), 2) AS median_fcc
        FROM marts.adjusted_metrics
        WHERE fy = 2024 AND basis = 'first_reported'
        GROUP BY 1 ORDER BY median_leverage DESC NULLS LAST""")
    heads = [d[0] for d in cur.description]
    print("  " + "  ".join(f"{h:<16}" for h in heads))
    for r in cur.fetchall():
        print("  " + "  ".join(f"{('' if v is None else v)!s:<16}" for v in r))

    print("\nWhere the lease figure came from (fy, first_reported):")
    cur = con.execute("""
        SELECT fy, count(*) FILTER (WHERE lease_source = 'asc842_reported_liability')
                   AS asc842,
               count(*) FILTER (WHERE lease_source = 'asc840_rent_capitalised') AS asc840,
               count(*) FILTER (WHERE lease_source = 'none') AS none
        FROM marts.adjusted_metrics
        WHERE basis = 'first_reported' AND policy = 'lease_8x' AND fy BETWEEN 2016 AND 2025
        GROUP BY 1 ORDER BY 1""")
    for r in cur.fetchall():
        print("  " + "  ".join(f"{v!s:<10}" for v in r))


if __name__ == "__main__":
    main()
