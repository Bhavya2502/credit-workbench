"""How much of the filed data have we actually structured?

Two questions worth answering with numbers rather than assurance:

1. Tags — we store facts for every tag, but only a small named set is mapped into
   usable spread lines. What share of the filed facts, and of the filed value, does
   that mapped set actually carry?
2. Schedules — the detailed note schedules are *dimensioned* facts (by segment, by
   plan, by instrument, by jurisdiction). Only consolidated facts went into the
   point-in-time base, so those schedules live in the raw layer unless a specific
   extraction claimed them. Which axes have we claimed, and which are untouched?
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

PERIOD = "2026_06"

Q: list[tuple[str, str]] = [
    ("1. Tag universe vs what the spread template maps", """
        SELECT count(DISTINCT f.tag)                                   AS tags_with_facts,
               count(DISTINCT f.tag) FILTER (WHERE m.tag IS NOT NULL)  AS tags_mapped,
               round(100.0 * count(DISTINCT f.tag) FILTER (WHERE m.tag IS NOT NULL)
                     / count(DISTINCT f.tag), 2)                       AS pct_tags_mapped
        FROM staging.facts_pit f
        LEFT JOIN staging.tag_map m ON m.tag = f.tag
        WHERE f.is_latest AND f.period_year >= 2023"""),

    ("2. But how much of the DATA do those mapped tags carry?", """
        SELECT count(*)                                                AS facts,
               count(*) FILTER (WHERE m.tag IS NOT NULL)               AS facts_mapped,
               round(100.0 * count(*) FILTER (WHERE m.tag IS NOT NULL)
                     / count(*), 1)                                    AS pct_facts_mapped,
               round(100.0 * sum(abs(f.value)) FILTER (WHERE m.tag IS NOT NULL)
                     / nullif(sum(abs(f.value)), 0), 1)                AS pct_value_mapped
        FROM staging.facts_pit f
        LEFT JOIN staging.tag_map m ON m.tag = f.tag
        WHERE f.is_latest AND f.period_year >= 2023 AND f.uom = 'USD'"""),

    ("3. Face-of-statement vs note-level facts, and how much of each is mapped", """
        SELECT CASE WHEN f.stmt IN ('IS','BS','CF') THEN 'face of statements'
                    WHEN f.stmt IS NULL THEN 'note level (no statement)'
                    ELSE 'other statement' END                         AS layer,
               count(*)                                                AS facts,
               round(100.0 * count(*) FILTER (WHERE m.tag IS NOT NULL)
                     / count(*), 1)                                    AS pct_mapped
        FROM staging.facts_pit f
        LEFT JOIN staging.tag_map m ON m.tag = f.tag
        WHERE f.is_latest AND f.period_year >= 2023
        GROUP BY 1 ORDER BY 2 DESC"""),

    ("4. Consolidated vs dimensioned — the schedules live in the dimensioned half", f"""
        SELECT CASE WHEN dimn = '0' THEN 'consolidated (in facts_pit)'
                    ELSE 'dimensioned (schedules; raw only)' END        AS kind,
               count(*)                                                 AS facts,
               round(100.0 * count(*) / sum(count(*)) OVER (), 1)       AS pct
        FROM raw.fsn_num WHERE period = '{PERIOD}' GROUP BY 1"""),

    ("5. Which dimensional axes carry the schedules, and have we claimed them?", f"""
        SELECT regexp_extract(d.segments, '^([A-Za-z]+)=', 1)          AS axis,
               count(*)                                                AS facts,
               count(DISTINCT n.adsh)                                  AS filings,
               CASE WHEN regexp_extract(d.segments, '^([A-Za-z]+)=', 1)
                         IN ('BusinessSegments','StatementBusinessSegments',
                             'ProductOrService','StatementGeographical','Geographical',
                             'ConsolidationItems')            THEN 'yes - marts.segments'
                    WHEN regexp_extract(d.segments, '^([A-Za-z]+)=', 1)
                         LIKE 'ConcentrationRisk%'            THEN 'yes - marts.concentration'
                    WHEN regexp_extract(d.segments, '^([A-Za-z]+)=', 1)
                         = 'DebtInstrument'                   THEN 'yes - marts.debt_instruments'
                    ELSE 'NOT EXTRACTED' END                            AS status
        FROM raw.fsn_num n JOIN raw.fsn_dim d
          ON d.dimhash = n.dimh AND d.period = n.period
        WHERE n.period = '{PERIOD}' AND n.dimn <> '0'
        GROUP BY 1, 4 ORDER BY 2 DESC LIMIT 25"""),

    ("6. The biggest note tags we hold facts for but do not map", """
        SELECT f.tag, count(DISTINCT f.adsh) AS filings,
               round(sum(abs(f.value)) / 1e9) AS usd_bn
        FROM staging.facts_pit f
        LEFT JOIN staging.tag_map m ON m.tag = f.tag
        WHERE f.is_latest AND f.period_year >= 2023 AND f.uom = 'USD'
          AND m.tag IS NULL AND f.stmt IS NULL
        GROUP BY 1 ORDER BY filings DESC LIMIT 15"""),
]


def show(con, query: str) -> None:
    cur = con.execute(query)
    headers = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,.4g}" if isinstance(v, float) else str(v)))[:62]
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
