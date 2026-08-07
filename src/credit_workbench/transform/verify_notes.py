"""Verification for Section G — is the qualitative signal real?

Same standard as the credit-outcome target: a signal earns its place only if companies
carrying it actually fare worse. Phrase matching that fires on healthy and distressed
companies alike is noise dressed up as insight.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

CHECKS: list[tuple[str, str]] = [
    ("Corpus size and coverage", """
        SELECT count(*) AS filings, count(DISTINCT cik) AS companies,
               min(fy) AS from_fy, max(fy) AS to_fy,
               round(avg(note_blocks)) AS avg_notes_per_filing,
               round(avg(total_chars)) AS avg_chars_per_filing
        FROM quali.note_signals"""),

    ("Note types available in the corpus", """
        SELECT note_type, count(*) AS blocks, count(DISTINCT adsh) AS filings
        FROM quali.note_text
        WHERE period >= '2024' AND note_type <> 'other'
        GROUP BY 1 ORDER BY 3 DESC LIMIT 15"""),

    ("Signal prevalence — how often each fires", """
        SELECT 'going_concern' AS signal, count(*) FILTER (WHERE going_concern) AS hits,
               round(100.0 * count(*) FILTER (WHERE going_concern) / count(*), 2) AS pct
        FROM quali.note_signals
        UNION ALL SELECT 'material_weakness', count(*) FILTER (WHERE material_weakness),
               round(100.0 * count(*) FILTER (WHERE material_weakness) / count(*), 2)
        FROM quali.note_signals
        UNION ALL SELECT 'covenant_breach', count(*) FILTER (WHERE covenant_breach),
               round(100.0 * count(*) FILTER (WHERE covenant_breach) / count(*), 2)
        FROM quali.note_signals
        UNION ALL SELECT 'covenant_waiver', count(*) FILTER (WHERE covenant_waiver),
               round(100.0 * count(*) FILTER (WHERE covenant_waiver) / count(*), 2)
        FROM quali.note_signals
        UNION ALL SELECT 'event_of_default', count(*) FILTER (WHERE event_of_default),
               round(100.0 * count(*) FILTER (WHERE event_of_default) / count(*), 2)
        FROM quali.note_signals
        UNION ALL SELECT 'cross_default', count(*) FILTER (WHERE cross_default),
               round(100.0 * count(*) FILTER (WHERE cross_default) / count(*), 2)
        FROM quali.note_signals
        UNION ALL SELECT 'liquidity_warning', count(*) FILTER (WHERE liquidity_warning),
               round(100.0 * count(*) FILTER (WHERE liquidity_warning) / count(*), 2)
        FROM quali.note_signals
        UNION ALL SELECT 'restatement', count(*) FILTER (WHERE restatement),
               round(100.0 * count(*) FILTER (WHERE restatement) / count(*), 2)
        FROM quali.note_signals
        UNION ALL SELECT 'class_action', count(*) FILTER (WHERE class_action),
               round(100.0 * count(*) FILTER (WHERE class_action) / count(*), 2)
        FROM quali.note_signals
        ORDER BY hits DESC"""),

    ("DISCRIMINATION TEST — does each signal predict actual distress?", """
        WITH j AS (
            SELECT s.*, o.distress_12m, o.default_24m, o.bankruptcy_24m
            FROM quali.note_signals s
            JOIN marts.credit_outcomes o
              ON o.cik = s.cik AND o.period_end = s.period_end)
        SELECT signal, flagged_filings,
               round(pct_default_when_flagged, 2) AS pct_default_flagged,
               round(pct_default_when_not, 2)     AS pct_default_not,
               round(pct_default_when_flagged / nullif(pct_default_when_not, 0), 1)
                   AS lift
        FROM (
            SELECT 'going_concern' AS signal, count(*) FILTER (WHERE going_concern) AS flagged_filings,
                   100.0 * count(*) FILTER (WHERE going_concern AND default_24m)
                       / nullif(count(*) FILTER (WHERE going_concern), 0) AS pct_default_when_flagged,
                   100.0 * count(*) FILTER (WHERE NOT going_concern AND default_24m)
                       / nullif(count(*) FILTER (WHERE NOT going_concern), 0) AS pct_default_when_not
            FROM j
            UNION ALL SELECT 'material_weakness', count(*) FILTER (WHERE material_weakness),
                   100.0 * count(*) FILTER (WHERE material_weakness AND default_24m)
                       / nullif(count(*) FILTER (WHERE material_weakness), 0),
                   100.0 * count(*) FILTER (WHERE NOT material_weakness AND default_24m)
                       / nullif(count(*) FILTER (WHERE NOT material_weakness), 0) FROM j
            UNION ALL SELECT 'covenant_breach', count(*) FILTER (WHERE covenant_breach),
                   100.0 * count(*) FILTER (WHERE covenant_breach AND default_24m)
                       / nullif(count(*) FILTER (WHERE covenant_breach), 0),
                   100.0 * count(*) FILTER (WHERE NOT covenant_breach AND default_24m)
                       / nullif(count(*) FILTER (WHERE NOT covenant_breach), 0) FROM j
            UNION ALL SELECT 'covenant_waiver', count(*) FILTER (WHERE covenant_waiver),
                   100.0 * count(*) FILTER (WHERE covenant_waiver AND default_24m)
                       / nullif(count(*) FILTER (WHERE covenant_waiver), 0),
                   100.0 * count(*) FILTER (WHERE NOT covenant_waiver AND default_24m)
                       / nullif(count(*) FILTER (WHERE NOT covenant_waiver), 0) FROM j
            UNION ALL SELECT 'event_of_default', count(*) FILTER (WHERE event_of_default),
                   100.0 * count(*) FILTER (WHERE event_of_default AND default_24m)
                       / nullif(count(*) FILTER (WHERE event_of_default), 0),
                   100.0 * count(*) FILTER (WHERE NOT event_of_default AND default_24m)
                       / nullif(count(*) FILTER (WHERE NOT event_of_default), 0) FROM j
            UNION ALL SELECT 'cross_default', count(*) FILTER (WHERE cross_default),
                   100.0 * count(*) FILTER (WHERE cross_default AND default_24m)
                       / nullif(count(*) FILTER (WHERE cross_default), 0),
                   100.0 * count(*) FILTER (WHERE NOT cross_default AND default_24m)
                       / nullif(count(*) FILTER (WHERE NOT cross_default), 0) FROM j
            UNION ALL SELECT 'liquidity_warning', count(*) FILTER (WHERE liquidity_warning),
                   100.0 * count(*) FILTER (WHERE liquidity_warning AND default_24m)
                       / nullif(count(*) FILTER (WHERE liquidity_warning), 0),
                   100.0 * count(*) FILTER (WHERE NOT liquidity_warning AND default_24m)
                       / nullif(count(*) FILTER (WHERE NOT liquidity_warning), 0) FROM j
            UNION ALL SELECT 'restatement', count(*) FILTER (WHERE restatement),
                   100.0 * count(*) FILTER (WHERE restatement AND default_24m)
                       / nullif(count(*) FILTER (WHERE restatement), 0),
                   100.0 * count(*) FILTER (WHERE NOT restatement AND default_24m)
                       / nullif(count(*) FILTER (WHERE NOT restatement), 0) FROM j)
        WHERE flagged_filings >= 100
        ORDER BY lift DESC"""),

    ("Going concern against bankruptcy specifically", """
        WITH j AS (
            SELECT s.going_concern, o.bankruptcy_24m
            FROM quali.note_signals s
            JOIN marts.credit_outcomes o
              ON o.cik = s.cik AND o.period_end = s.period_end)
        SELECT going_concern, count(*) AS filings,
               round(100.0 * count(*) FILTER (WHERE bankruptcy_24m) / count(*), 2)
                   AS pct_bankrupt_24m
        FROM j GROUP BY 1 ORDER BY 1"""),
]


def show(con, query: str) -> None:
    cur = con.execute(query)
    headers = [d[0] for d in cur.description]
    rows = [["" if v is None else (f"{v:,.4g}" if isinstance(v, float) else str(v))
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
