"""Invariants for the risk-theme marts (G-09).

The failure this guards against is the one the probe found before a line was written:
classifying body text marks 90%+ of issuers with every common theme, producing a table that
looks authoritative and discriminates nothing. So the checks test that the mart has *not*
drifted back into that state - themes must not be near-universal across the board, and at
least some must vary between industries by a wide margin.

The other checks are structural: one row per filing and theme, every row carrying the
verbatim heading the request asked for, and every heading actually reading like a heading.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

CHECKS: list[tuple[str, str, str]] = [
    ("the mart covers companies, filings and years",
     """SELECT count(*) AS rows, count(DISTINCT cik) AS companies,
               count(DISTINCT adsh) AS filings, count(DISTINCT theme) AS themes,
               count(DISTINCT fy) AS years
        FROM marts.risk_themes""",
     "rows > 50000 and companies > 3000 and themes >= 15 and years > 5"),

    ("one row per company, filing and theme",
     """SELECT count(*) AS rows, count(DISTINCT (cik, adsh, theme)) AS distinct_keys
        FROM marts.risk_themes""",
     "rows == distinct_keys"),

    ("every theme in the mart is in the dictionary, and every entry produces rows",
     """SELECT (SELECT count(*) FROM ref.risk_theme_dictionary) AS defined,
               (SELECT count(DISTINCT theme) FROM marts.risk_themes) AS producing,
               (SELECT count(*) FROM marts.risk_themes t
                WHERE NOT EXISTS (SELECT 1 FROM ref.risk_theme_dictionary d
                                  WHERE d.theme = t.theme)) AS undocumented""",
     "undocumented == 0 and producing >= defined - 2"),

    # The whole reason this is built at heading grain. If themes crept back to
    # near-universal the mart would have the same defect body-text classification had.
    ("themes are not near-universal across the board",
     """SELECT count(*) AS themes,
               count(*) FILTER (WHERE mean_share > 85) AS near_universal,
               round(avg(mean_share), 1) AS avg_share
        FROM (SELECT theme, avg(issuer_share) AS mean_share
              FROM marts.risk_theme_prevalence WHERE fy = 2024 GROUP BY theme)""",
     "themes >= 15 and near_universal <= 3 and avg_share < 70"),

    # Some themes must genuinely separate industries or the aggregate is decoration.
    ("at least a few themes discriminate between industries",
     """SELECT count(DISTINCT theme) FILTER (WHERE discriminates) AS discriminating,
               count(DISTINCT theme) AS total_themes,
               round(max(industry_spread_pp), 1) AS widest_spread
        FROM marts.risk_theme_prevalence WHERE fy = 2024""",
     "discriminating >= 5 and widest_spread > 30"),

    ("every row carries the verbatim heading the request asked for",
     """SELECT count(*) AS rows,
               count(*) FILTER (WHERE example_heading IS NULL
                                   OR length(example_heading) < 40) AS no_evidence,
               count(*) FILTER (WHERE headings_for_theme < 1) AS impossible_count
        FROM marts.risk_themes""",
     "no_evidence == 0 and impossible_count == 0"),

    # A heading is a risk plus a consequence. If rows appeared without that shape the
    # heading rule has leaked and body paragraphs are being classified.
    ("the headings read like risk headings",
     """SELECT count(*) AS rows,
               round(100.0 * count(*) FILTER (WHERE regexp_matches(lower(example_heading),
                   'adversely|adverse|could|may|would|unable|failure|difficult|subject us'))
                     / count(*), 1) AS pct_with_consequence
        FROM marts.risk_themes""",
     "pct_with_consequence > 95"),

    ("prevalence shares are shares",
     """SELECT count(*) AS rows,
               count(*) FILTER (WHERE issuer_share > 100 OR issuer_share < 0) AS impossible,
               count(*) FILTER (WHERE issuers_with_theme > issuers_total) AS more_than_all
        FROM marts.risk_theme_prevalence""",
     "rows > 500 and impossible == 0 and more_than_all == 0"),

    ("the mart joins to the companies it is meant to describe",
     """SELECT count(DISTINCT t.cik) AS with_ratios
        FROM marts.risk_themes t
        JOIN (SELECT DISTINCT cik FROM marts.ratio_values) r ON r.cik = t.cik""",
     "with_ratios > 3000"),

    # Sanity on the classification itself: an oil and gas issuer should raise climate more
    # than a pharmaceutical one. If that ordering inverted, the themes are mislabelled.
    ("climate is raised more by energy than by pharmaceuticals",
     """SELECT round(avg(issuer_share) FILTER (WHERE sic2 = '13'), 1) AS energy,
               round(avg(issuer_share) FILTER (WHERE sic2 = '28'), 1) AS pharma
        FROM marts.risk_theme_prevalence
        WHERE theme = 'climate' AND fy BETWEEN 2022 AND 2025""",
     "energy > pharma"),
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
