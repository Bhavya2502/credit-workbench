"""Verification for M2 and instrument-level debt."""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

CHECKS: list[tuple[str, str]] = [
    ("M2 — does the filers' own arithmetic hold?", """
        SELECT count(*) AS subtotal_checks, count(DISTINCT adsh) AS filings,
               round(100.0 * count(*) FILTER (WHERE ties) / count(*), 1) AS pct_tie,
               round(100.0 * count(*) FILTER (WHERE children_unmapped > 0) / count(*), 1)
                   AS pct_with_unmapped_component
        FROM marts.calc_check WHERE parent_value <> 0"""),

    ("M2 — tie rate for the subtotals our spread depends on", """
        SELECT subtotal_tag, count(*) AS checks,
               round(100.0 * count(*) FILTER (WHERE ties) / count(*), 1) AS pct_tie,
               round(median(relative_gap), 4) AS median_relative_gap
        FROM marts.calc_check
        WHERE parent_value <> 0
          AND subtotal_tag IN ('Assets','AssetsCurrent','Liabilities','LiabilitiesCurrent',
                               'StockholdersEquity','LiabilitiesAndStockholdersEquity',
                               'Revenues','OperatingIncomeLoss','NetIncomeLoss',
                               'NetCashProvidedByUsedInOperatingActivities')
        GROUP BY 1 ORDER BY checks DESC"""),

    ("M2 — components filers declare that our map misses (top by reach)", """
        SELECT subtotal_tag, missing_component, our_line_for_subtotal, filings,
               round(abs_value_carried / 1e9, 1) AS usd_bn
        FROM staging.map_gaps ORDER BY filings DESC LIMIT 20"""),

    ("M2 — where the gaps concentrate", """
        SELECT our_line_for_subtotal, count(*) AS distinct_missing_components,
               sum(filings) AS total_filings
        FROM staging.map_gaps GROUP BY 1 ORDER BY 3 DESC LIMIT 12"""),

    ("Debt — instrument coverage", """
        SELECT count(*) AS instrument_periods, count(DISTINCT cik) AS companies,
               count(DISTINCT member) AS distinct_instruments,
               min(fy) AS from_fy, max(fy) AS to_fy,
               round(100.0 * count(face_amount) / count(*), 1) AS pct_with_face_amount,
               round(100.0 * count(stated_rate) / count(*), 1) AS pct_with_rate,
               round(100.0 * count(maturity_year) / count(*), 1) AS pct_with_maturity
        FROM marts.debt_instruments"""),

    ("Debt — instrument mix", """
        SELECT instrument_type, count(*) AS instrument_periods,
               count(DISTINCT cik) AS companies,
               round(median(stated_rate) * 100, 2) AS median_coupon_pct
        FROM marts.debt_instruments GROUP BY 1 ORDER BY 2 DESC"""),

    ("Debt — how maturity was determined", """
        SELECT maturity_source, count(*) AS rows,
               round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
        FROM marts.debt_instruments GROUP BY 1 ORDER BY 2 DESC"""),

    ("Debt — maturity wall across the market (latest year)", """
        SELECT years_to_maturity, count(DISTINCT cik) AS companies,
               round(sum(amount_due) / 1e9) AS usd_bn_due
        FROM marts.debt_maturity_profile
        WHERE fy = 2024 AND years_to_maturity BETWEEN 0 AND 10
        GROUP BY 1 ORDER BY 1"""),

    ("Debt — undrawn revolver capacity, a liquidity measure", """
        SELECT count(*) AS company_periods, count(DISTINCT cik) AS companies,
               round(median(pct_undrawn), 2) AS median_pct_undrawn,
               round(sum(undrawn) / 1e9) AS total_undrawn_usd_bn
        FROM marts.revolver_capacity WHERE fy >= 2020 AND pct_undrawn BETWEEN 0 AND 1"""),
]


def show(con, query: str) -> None:
    cur = con.execute(query)
    headers = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,.4g}" if isinstance(v, float) else str(v)))[:60]
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
    for title, query in CHECKS:
        print(f"\n### {title}")
        try:
            show(con, query)
        except Exception as exc:  # noqa: BLE001
            print(f"  (check failed: {exc})")


if __name__ == "__main__":
    main()
