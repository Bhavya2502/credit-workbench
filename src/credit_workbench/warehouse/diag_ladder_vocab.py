"""The complete rung vocabulary of each ladder, from the concept root.

An earlier probe matched `LesseeOperatingLeaseLiabilityPaymentsDue%` and showed the top
14 rungs. Both choices hid data. us-gaap carries at least three parallel families for
the same operating-lease ladder -

    LesseeOperatingLeaseLiabilityPaymentsDue*        the form that was mapped
    LesseeOperatingLeaseLiabilityPayments*           e.g. PaymentsRemainderOfFiscalYear,
                                                    no "Due" in the name at all
    LesseeOperatingLeaseLiabilityToBePaid*           the newer taxonomy naming

- and a top-14 cut truncated the tail besides. So: match on the concept root only, no
guessed middle segment, and list every rung above a low threshold rather than a fixed
number of them.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

ROOTS = {
    "operating lease":         "LesseeOperatingLeaseLiability%",
    "finance lease":           "FinanceLeaseLiability%",
    "debt maturities":         "LongTermDebtMaturitiesRepayments%",
    "intangible amortisation": "FiniteLivedIntangibleAssetsAmortizationExpense%",
    "operating lease (ASC840)": "OperatingLeasesFutureMinimumPayments%",
}

Q: list[tuple[str, str]] = [
    (f"Full vocabulary above 400 filings, by concept root", " UNION ALL ".join(f"""
        (SELECT '{name}' AS ladder, f.tag, count(DISTINCT f.adsh) AS filings,
                (m.tag IS NOT NULL) AS mapped_in_d1
         FROM staging.facts_pit f
         LEFT JOIN (SELECT DISTINCT source_tag AS tag FROM staging.note_inputs) m
                USING (tag)
         WHERE f.is_latest AND f.tag LIKE '{pat}' AND f.period_year >= 2021
         GROUP BY f.tag, m.tag
         HAVING count(DISTINCT f.adsh) >= 400)""" for name, pat in ROOTS.items())
     + " ORDER BY ladder, filings DESC"),
]


def show(con, query: str) -> None:
    cur = con.execute(query)
    headers = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)))[:72]
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
