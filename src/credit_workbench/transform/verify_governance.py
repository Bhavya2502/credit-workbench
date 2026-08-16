"""Invariants for the proxy and governance layer.

Written with the mart rather than after it, because a check written afterwards is written
to pass. The specific thing being guarded against here is known, not hypothetical: an
earlier version of the fee reader took the first row matching each label anywhere in the
document and was wrong on 16 of 40 filings, reporting a total of 11 against a table that
said 2,017 and in one case lifting a total of 4,011,243 off the Rule 0-11 cover-page
filing fee. Every wrong figure looked entirely plausible on its own. Only a property the
number must satisfy catches that, which is why the components are checked against the
stated total rather than merely eyeballed.

Two of these checks are about arithmetic that the filer themselves published, so they are
strict. The rest are coverage floors set below what a trial run measured, so they fail on
a regression rather than on ordinary variation between filers.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

CHECKS: list[tuple[str, str, str]] = [
    # Sections ----------------------------------------------------------------
    ("proxy sections extracted across companies and years",
     """SELECT count(*) AS sections, count(DISTINCT adsh) AS proxies,
               count(DISTINCT cik) AS companies,
               count(DISTINCT substr(filing_date, 1, 4)) AS years
        FROM quali.proxy_sections""",
     "sections > 10000 and companies > 1000 and years >= 3"),

    ("every proxy section joins back to a real filing",
     """SELECT count(*) AS orphans FROM (
            SELECT s.adsh FROM quali.proxy_sections s
            LEFT JOIN ref.filing_index f ON f.accession_number = s.adsh
            WHERE f.accession_number IS NULL LIMIT 50000)""",
     "orphans == 0"),

    # The natural key includes the filer: one proxy can be filed by several
    # co-registrants sharing an accession number, so a section legitimately repeats
    # once per CIK. Anything beyond that is fan-out, which has bitten this project
    # three times on other tables.
    ("sections are distinct per company, filing and section",
     """SELECT count(*) AS rows, count(DISTINCT (cik, adsh, section)) AS distinct_triples
        FROM quali.proxy_sections""",
     "rows == distinct_triples"),

    ("no proxy section has swallowed the document",
     """SELECT count(*) AS sections,
               round(100.0 * count(*) FILTER (WHERE char_len > 400000)
                     / count(*), 3) AS pct_over_400k, max(char_len) AS longest
        FROM quali.proxy_sections""",
     "pct_over_400k < 1.0"),

    ("each section reads like itself",
     """SELECT round(100.0 * count(*) FILTER (
                   WHERE section <> 'independence'
                      OR lower(text) LIKE '%independen%') / count(*), 1) AS pct_indep,
               round(100.0 * count(*) FILTER (
                   WHERE section <> 'related_party'
                      OR lower(text) LIKE '%related%') / count(*), 1) AS pct_related,
               round(100.0 * count(*) FILTER (
                   WHERE section <> 'committees'
                      OR lower(text) LIKE '%committee%') / count(*), 1) AS pct_committee
        FROM quali.proxy_sections""",
     "pct_indep > 99 and pct_related > 99 and pct_committee > 99"),

    ("table rows survived the conversion — fees are still beside their labels",
     """SELECT round(100.0 * count(*) FILTER (WHERE text LIKE '%|%')
                     / count(*), 1) AS pct_with_rows
        FROM quali.proxy_sections WHERE section = 'audit_fees'""",
     "pct_with_rows > 80"),

    # Metrics -----------------------------------------------------------------
    ("one metric row per proxy filing, no fan-out",
     """SELECT count(*) AS rows, count(DISTINCT (cik, adsh)) AS distinct_pairs,
               (SELECT count(DISTINCT (cik, adsh)) FROM quali.proxy_sections) AS source
        FROM marts.governance_metrics""",
     "rows == distinct_pairs and rows == source"),

    ("the fee components sum to the total the filer stated",
     """SELECT count(*) AS both_present,
               round(100.0 * count(*) FILTER (
                   WHERE abs(fee_components_sum - total_fees_stated)
                         <= greatest(1.0, 0.005 * total_fees_stated))
                     / count(*), 1) AS pct_tying
        FROM marts.governance_metrics
        WHERE fee_components_sum IS NOT NULL AND total_fees_stated IS NOT NULL
          AND total_fees_stated > 0""",
     "both_present > 100 and pct_tying > 95"),

    ("audit fees are a plausible size for an audited registrant",
     """SELECT count(*) AS n, round(median(audit_fees), 0) AS median_audit,
               count(*) FILTER (WHERE audit_fees < 10000) AS under_10k,
               count(*) FILTER (WHERE audit_fees > 500000000) AS over_500m
        FROM marts.governance_metrics WHERE audit_fees > 0""",
     "n > 100 and median_audit > 100000 and over_500m == 0"),

    # If the units note above the table were being missed, tables stated in thousands
    # would land a thousandfold low and show up as a cluster of implausibly small fees.
    ("the units note above the table is being read",
     """SELECT count(*) FILTER (WHERE fee_units = 'thousands') AS thousands,
               count(*) FILTER (WHERE fee_units = 'dollars') AS dollars,
               round(100.0 * count(*) FILTER (WHERE audit_fees < 10000)
                     / count(*), 2) AS pct_under_10k
        FROM marts.governance_metrics WHERE audit_fees IS NOT NULL""",
     "dollars > 0 and pct_under_10k < 5"),

    ("the non-audit ratio is a ratio, and mostly small as independence requires",
     """SELECT count(*) AS n, round(median(non_audit_fee_ratio), 3) AS median_ratio,
               count(*) FILTER (WHERE non_audit_fee_ratio < 0) AS negative,
               round(100.0 * count(*) FILTER (WHERE non_audit_fee_ratio > 1.0)
                     / count(*), 1) AS pct_over_one
        FROM marts.governance_metrics WHERE non_audit_fee_ratio IS NOT NULL""",
     "n > 100 and negative == 0 and median_ratio < 1.0 and pct_over_one < 25"),

    # Independence is read from the director table or left null. The one thing that
    # must never happen is more directors marked independent than listed.
    ("no filing marks more directors independent than it lists",
     """SELECT count(*) AS both_present,
               count(*) FILTER (WHERE directors_marked_independent > directors_listed)
                   AS impossible
        FROM marts.governance_metrics
        WHERE directors_listed IS NOT NULL
          AND directors_marked_independent IS NOT NULL""",
     "impossible == 0"),

    ("boards are a plausible size where a table was found",
     """SELECT count(*) AS n, round(median(directors_listed), 0) AS median_directors,
               count(*) FILTER (WHERE directors_listed > 30) AS over_30
        FROM marts.governance_metrics WHERE directors_listed IS NOT NULL""",
     "n > 100 and median_directors between 5 and 15 and over_30 == 0"),

    ("the CEO pay ratio stays inside its bounds",
     """SELECT count(*) AS n, round(median(ceo_pay_ratio), 0) AS median_ratio,
               count(*) FILTER (WHERE ceo_pay_ratio < 1 OR ceo_pay_ratio > 10000)
                   AS out_of_bounds
        FROM marts.governance_metrics WHERE ceo_pay_ratio IS NOT NULL""",
     "n > 50 and out_of_bounds == 0"),

    # A row of all-nulls is worse than no row: it looks like a scored filing.
    ("every metric row carries at least one extracted signal",
     """SELECT count(*) AS rows,
               count(*) FILTER (
                   WHERE audit_fees IS NULL AND directors_listed IS NULL
                     AND ceo_pay_ratio IS NULL AND related_party_chars IS NULL
                     AND NOT has_clawback_policy AND NOT has_cda
                     AND sections_found = 0) AS empty_rows
        FROM marts.governance_metrics""",
     "empty_rows == 0"),

    ("the mart joins to the companies that have financials",
     """SELECT count(DISTINCT g.cik) AS scored_filers_with_ratios
        FROM marts.governance_metrics g
        WHERE lpad(CAST(g.cik AS VARCHAR), 10, '0') IN
              (SELECT lpad(CAST(cik AS VARCHAR), 10, '0') FROM marts.ratio_values)""",
     "scored_filers_with_ratios > 500"),
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
            else f"{k}={v:,.1f}" if isinstance(v, float) else f"{k}={v}"
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
