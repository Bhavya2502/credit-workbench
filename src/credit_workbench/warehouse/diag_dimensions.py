"""Inspect the dimension table before building on it.

Every time a build has gone in without this step it has come back wrong — the
concentration axis carried its facts on `ConcentrationRiskPercentage1`, not the tag the
name suggested, and `dim.segt` turned out to be a truncation flag rather than a label.
So: check the shape of the axes, confirm the members are the things they claim to be,
and confirm the tags being added to D1 actually exist before spending runner minutes.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

NEW_D1_TAGS = [
    "FiniteLivedIntangibleAssetsAmortizationExpenseNextTwelveMonths",
    "FiniteLivedIntangibleAssetsAmortizationExpenseYearTwo",
    "FiniteLivedIntangibleAssetsAmortizationExpenseYearThree",
    "FiniteLivedIntangibleAssetsAmortizationExpenseYearFour",
    "FiniteLivedIntangibleAssetsAmortizationExpenseYearFive",
    "FiniteLivedIntangibleAssetsAmortizationExpenseRemainderOfFiscalYear",
    "FiniteLivedIntangibleAssetsGross",
    "FiniteLivedIntangibleAssetsAccumulatedAmortization",
    "FinanceLeaseLiabilityPaymentsDueNextTwelveMonths",
    "FinanceLeaseLiabilityPaymentsDueYearTwo",
    "FinanceLeaseLiabilityPaymentsDueYearThree",
    "FinanceLeaseLiabilityPaymentsDueYearFour",
    "FinanceLeaseLiabilityPaymentsDueYearFive",
    "FinanceLeaseLiabilityPaymentsDueAfterYearFive",
    "FinanceLeaseLiabilityPaymentsDue",
    "FinanceLeaseLiabilityUndiscountedExcessAmount",
    "PropertyPlantAndEquipmentGross",
    "AccumulatedDepreciationDepletionAndAmortizationPropertyPlantAndEquipment",
]
TAGS_SQL = ", ".join(f"'{t}'" for t in NEW_D1_TAGS)

Q: list[tuple[str, str]] = [
    ("0. Do the tags I am about to add to D1 actually exist, and how widely?", f"""
        SELECT tag, count(DISTINCT adsh) AS filings, count(DISTINCT cik) AS companies,
               max(period_year) AS last_year
        FROM staging.facts_pit
        WHERE is_latest AND tag IN ({TAGS_SQL})
        GROUP BY 1 ORDER BY filings DESC"""),

    ("0b. Any of those tags with NO facts at all (typo check)", f"""
        WITH want(tag) AS (VALUES {', '.join(f"('{t}')" for t in NEW_D1_TAGS)})
        SELECT w.tag AS tag_with_no_facts FROM want w
        WHERE NOT EXISTS (SELECT 1 FROM staging.facts_pit f
                          WHERE f.tag = w.tag AND f.is_latest)"""),

    ("1. Archive span of the dimension table — what range must the build cover?", """
        SELECT min(period) AS first_archive, max(period) AS last_archive,
               count(DISTINCT period) AS archives, count(*) AS dim_rows
        FROM raw.fsn_dim"""),

    ("2. How many axes does a typical dimension hash carry? (explosion factor)", """
        SELECT len(str_split(rtrim(segments, ';'), ';')) AS axes_in_hash,
               count(*) AS hashes,
               round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
        FROM raw.fsn_dim WHERE segments IS NOT NULL AND segments <> ''
        GROUP BY 1 ORDER BY 1"""),

    ("3. Full axis ranking across ALL archives, not one month", """
        SELECT split_part(pair, '=', 1) AS axis, count(*) AS dim_rows,
               count(DISTINCT split_part(pair, '=', 2)) AS distinct_members
        FROM (SELECT unnest(str_split(rtrim(segments, ';'), ';')) AS pair
              FROM raw.fsn_dim WHERE segments IS NOT NULL AND segments <> '')
        GROUP BY 1 ORDER BY 2 DESC LIMIT 30"""),

    ("4. LegalEntity members — are these really subsidiaries?", """
        SELECT split_part(pair, '=', 2) AS member, count(*) AS dim_rows
        FROM (SELECT unnest(str_split(rtrim(segments, ';'), ';')) AS pair
              FROM raw.fsn_dim WHERE segments LIKE '%LegalEntity=%')
        WHERE split_part(pair, '=', 1) = 'LegalEntity'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 20"""),

    ("5. Fair-value hierarchy members — expect Level 1/2/3", """
        SELECT split_part(pair, '=', 2) AS member, count(*) AS dim_rows
        FROM (SELECT unnest(str_split(rtrim(segments, ';'), ';')) AS pair
              FROM raw.fsn_dim WHERE segments LIKE '%FairValue%')
        WHERE split_part(pair, '=', 1) = 'FairValueByFairValueHierarchyLevel'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 15"""),

    ("6. Which tags carry the fair-value hierarchy facts?", """
        SELECT n.tag, count(*) AS facts, count(DISTINCT n.adsh) AS filings
        FROM raw.fsn_num n JOIN raw.fsn_dim d
          ON d.dimhash = n.dimh AND d.period = n.period
        WHERE d.segments LIKE 'FairValueByFairValueHierarchyLevel=%'
          AND n.dimn <> '0' AND n.iprx = '0'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 15"""),

    ("7. Sizing the build: dimensioned facts to be written, all archives", """
        SELECT count(*) AS dimensioned_facts_iprx0
        FROM raw.fsn_num WHERE dimn <> '0' AND iprx = '0'
          AND value IS NOT NULL AND value <> ''"""),
]


def show(con, query: str) -> None:
    cur = con.execute(query)
    headers = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,.4g}" if isinstance(v, float) else str(v)))[:70]
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
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    for title, query in Q:
        print(f"\n### {title}")
        try:
            show(con, query)
        except Exception as exc:  # noqa: BLE001
            print(f"  (failed: {exc})")


if __name__ == "__main__":
    main()
