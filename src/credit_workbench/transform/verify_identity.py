"""Invariants for the name-resolution tables (G-19).

The point of these tables is to stop a cohort silently containing a survivor in place of a
failed company, so the checks are about whether they can actually do that: every company
must appear, every former name must be reachable, and the ambiguity flag must agree with the
count it is derived from.

The substantive one is that companies with a bankruptcy outcome are present. If a delisted
issuer were absent from the name index, a cohort built through it would drop exactly the
companies whose outcomes matter, which is the survivorship bias the whole exercise is
guarding against.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

CHECKS: list[tuple[str, str, str]] = [
    ("every company in the master has at least its current name indexed",
     """SELECT (SELECT count(*) FROM ref.dim_company
                WHERE company_name IS NOT NULL AND trim(company_name) <> '') AS in_master,
               (SELECT count(*) FROM ref.company_names
                WHERE name_role = 'current') AS current_names""",
     "current_names == in_master"),

    ("every former name is indexed",
     """SELECT (SELECT count(*) FROM ref.former_names
                WHERE former_name IS NOT NULL AND trim(former_name) <> '') AS in_source,
               (SELECT count(*) FROM ref.company_names
                WHERE name_role = 'former') AS former_names""",
     "former_names == in_source"),

    ("the ambiguity flag agrees with the count it comes from",
     """SELECT count(*) AS rows,
               count(*) FILTER (WHERE is_ambiguous <> (ciks_with_this_name > 1))
                   AS disagree,
               count(*) FILTER (WHERE ciks_with_this_name < 1) AS impossible_count
        FROM ref.company_names""",
     "disagree == 0 and impossible_count == 0"),

    # The reason the tables exist. If this were zero the collision risk would be
    # hypothetical; measured, it is 1,237 names.
    ("name collisions are found, and every one has two distinct companies",
     """SELECT count(*) AS rows,
               count(DISTINCT name_key) AS distinct_names,
               count(*) FILTER (WHERE former_holder_cik = current_holder_cik) AS same_cik
        FROM ref.name_collisions""",
     "rows > 500 and distinct_names > 500 and same_cik == 0"),

    ("every collision name is flagged ambiguous in the name index",
     """SELECT count(*) AS collisions,
               count(*) FILTER (WHERE NOT n.is_ambiguous) AS not_flagged
        FROM ref.name_collisions c
        JOIN ref.company_names n ON n.name_key = c.name_key AND n.cik = c.current_holder_cik
        """,
     "collisions > 500 and not_flagged == 0"),

    # Survivorship, asserted rather than assumed: the companies whose outcomes matter most
    # must be reachable through the name index.
    ("companies with a bankruptcy outcome are all in the name index",
     """SELECT count(DISTINCT o.cik) AS bankrupt_ciks,
               count(DISTINCT n.cik) AS in_name_index
        FROM marts.credit_outcomes o
        LEFT JOIN ref.company_names n ON n.cik = o.cik
        WHERE o.bankruptcy_24m""",
     "bankrupt_ciks > 700 and in_name_index == bankrupt_ciks"),

    ("the filing span covers the companies that have financials",
     """SELECT (SELECT count(DISTINCT cik) FROM marts.ratio_values) AS with_ratios,
               (SELECT count(DISTINCT r.cik) FROM marts.ratio_values r
                JOIN ref.company_filing_span s ON s.cik = r.cik) AS with_span""",
     "with_span == with_ratios"),

    ("a filing span never ends before it starts",
     """SELECT count(*) AS rows,
               count(*) FILTER (WHERE last_filing < first_filing) AS impossible,
               count(*) FILTER (WHERE filings < 1) AS empty
        FROM ref.company_filing_span""",
     "rows > 100000 and impossible == 0 and empty == 0"),

    # Documented in the module as the reason no is_active flag is derived. Asserted so that
    # if the discrimination ever became strong enough to justify one, someone notices.
    ("last filing date remains a weak proxy for failure, as documented",
     """WITH flagged AS (
            SELECT cik, bool_or(bankruptcy_24m) AS went_bankrupt
            FROM marts.credit_outcomes GROUP BY cik)
        SELECT round(100.0 * count(*) FILTER (WHERE f.went_bankrupt
                                                AND s.last_filing_year < 2024)
                     / nullif(count(*) FILTER (WHERE f.went_bankrupt), 0), 1)
                   AS pct_bankrupt_stopped,
               round(100.0 * count(*) FILTER (WHERE NOT f.went_bankrupt
                                                AND s.last_filing_year < 2024)
                     / nullif(count(*) FILTER (WHERE NOT f.went_bankrupt), 0), 1)
                   AS pct_healthy_stopped
        FROM flagged f JOIN ref.company_filing_span s ON s.cik = f.cik""",
     "pct_bankrupt_stopped > pct_healthy_stopped "
     "and pct_healthy_stopped > 20"),
]


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    failures = 0
    for i, (name, query, assertion) in enumerate(CHECKS, 1):
        try:
            cur = con.execute(query)
            row = dict(zip([d[0] for d in cur.description], cur.fetchone()))
        except Exception as exc:  # noqa: BLE001
            print(f"{i:2}. ERROR  {name}\n      {str(exc)[:150]}")
            failures += 1
            continue
        detail = ", ".join(
            f"{k}={v:,}" if isinstance(v, int)
            else f"{k}={v:,.2f}" if isinstance(v, float) else f"{k}={v}"
            for k, v in row.items())
        ok = eval(assertion, {}, {k: (v if v is not None else 0)  # noqa: S307
                                  for k, v in row.items()})
        print(f"{i:2}. {'PASS' if ok else 'FAIL'}  {name}\n      {detail}")
        failures += not ok
    print(f"\n{len(CHECKS) - failures}/{len(CHECKS)} checks passed")
    if failures:
        raise SystemExit(f"{failures} invariant(s) failed")


if __name__ == "__main__":
    main()
