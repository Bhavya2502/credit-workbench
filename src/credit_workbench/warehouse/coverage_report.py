"""How much of every filing have we actually structured? Re-runnable, not a one-off.

"Cover all notes, schedules and tags" only means something if it can be measured, so
this is the standing scorecard. It separates three questions that are easy to conflate:

  reachable   is the fact in a mart at all, so a query can find it?
  labelled    do we know what the fact means, from the taxonomy?
  modelled    has it been mapped into a named line of the spread, a note input, or a
              classified schedule - the form an analyst actually works with?

Reachability should be ~100%: every fact belongs somewhere. Labelling follows the
taxonomy. Modelling is the one that will always trail, because it is judgement work,
and the point of this report is to show exactly where the frontier is.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

Q: list[tuple[str, str]] = [
    # Both marts hold every vintage, so the denominator must too. Filtering is_latest
    # on one side only understated this at 81.6%.
    ("1. Reachability — is every filed fact in a mart?", """
        WITH filed AS (SELECT count(*) AS n FROM raw.fsn_num WHERE iprx = '0'
                         AND value IS NOT NULL AND value <> ''),
             consolidated AS (SELECT count(*) AS n FROM staging.facts_pit),
             dimensioned  AS (SELECT count(*) AS n FROM marts.facts_dimensioned)
        SELECT (SELECT n FROM filed)                       AS facts_filed,
               (SELECT n FROM consolidated)                AS in_facts_pit,
               (SELECT n FROM dimensioned)                 AS in_facts_dimensioned,
               round(100.0 * ((SELECT n FROM consolidated) + (SELECT n FROM dimensioned))
                     / (SELECT n FROM filed), 1)           AS pct_reachable"""),

    ("1b. Of those, one vintage per figure — what a query returns by default", """
        SELECT (SELECT count(*) FROM staging.facts_pit WHERE is_latest)
                   AS consolidated_latest,
               (SELECT count(*) FROM marts.facts_dimensioned WHERE is_latest)
                   AS schedules_latest"""),

    ("2. Tags — reachable, labelled, modelled", """
        SELECT count(*)                                            AS tags_total,
               count(*) FILTER (WHERE standard_taxonomy)           AS standard_taxonomy,
               count(*) FILTER (WHERE label IS NOT NULL)           AS labelled,
               count(*) FILTER (WHERE in_spread_template)          AS in_spread,
               count(*) FILTER (WHERE consolidated_facts > 0)      AS on_consolidated,
               count(*) FILTER (WHERE dimensioned_facts > 0)       AS in_schedules
        FROM ref.tag_catalog"""),

    ("3. Where the VALUE sits — mapped tags carry more weight than their count suggests", """
        SELECT round(100.0 * sum(total_facts) FILTER (WHERE in_spread_template)
                     / sum(total_facts), 1)                  AS pct_facts_in_spread,
               round(100.0 * sum(total_facts) FILTER (WHERE standard_taxonomy)
                     / sum(total_facts), 1)                  AS pct_facts_standard_taxonomy,
               round(100.0 * sum(total_facts) FILTER (WHERE label IS NOT NULL)
                     / sum(total_facts), 1)                  AS pct_facts_labelled
        FROM ref.tag_catalog"""),

    ("4. Axes — every one is reachable; these have a named front door", """
        SELECT c.axis, c.member_rows, c.distinct_members,
               coalesce(v.view_name,
                        'generic (marts.facts_dimensioned + ref.dimension_index)')
                   AS access,
               coalesce(v.purpose, '') AS purpose
        FROM ref.dimension_catalog c
        LEFT JOIN ref.named_axis_view v ON v.axis = c.axis
        ORDER BY c.member_rows DESC LIMIT 20"""),

    ("5. The structured marts and what they hold", """
        SELECT 'marts.facts_dimensioned' AS mart, count(*) AS rows FROM marts.facts_dimensioned
        UNION ALL SELECT 'marts.legal_entity_detail', count(*) FROM marts.legal_entity_detail
        UNION ALL SELECT 'marts.fair_value_hierarchy', count(*) FROM marts.fair_value_hierarchy
        UNION ALL SELECT 'marts.segments', count(*) FROM marts.segments
        UNION ALL SELECT 'marts.debt_instruments', count(*) FROM marts.debt_instruments
        UNION ALL SELECT 'marts.adjustment_inputs', count(*) FROM marts.adjustment_inputs
        UNION ALL SELECT 'marts.spread_lines', count(*) FROM marts.spread_lines
        UNION ALL SELECT 'marts.ratio_values', count(*) FROM marts.ratio_values
        UNION ALL SELECT 'marts.concentration', count(*) FROM marts.concentration
        UNION ALL SELECT 'marts.credit_events', count(*) FROM marts.credit_events
        UNION ALL SELECT 'quali.note_text', count(*) FROM quali.note_text
        ORDER BY rows DESC"""),

    ("6. Notes as text — the narrative half", """
        SELECT (SELECT count(*) FROM quali.note_text)              AS text_blocks,
               (SELECT count(DISTINCT adsh) FROM quali.note_signals) AS filings_scanned,
               (SELECT count(*) FROM ref.signal_definitions)       AS signals_defined"""),

    # Reachable is not the same as covered. A fact was findable long before it could be
    # found under the note it belongs to, and this is the measure of that second thing.
    ("7. Notes — can a fact be found under the note it was presented in?", """
        SELECT (SELECT count(*) FROM ref.note_index)     AS reports_titled,
               (SELECT count(*) FROM ref.tag_note_map)   AS tag_to_note_links,
               (SELECT count(DISTINCT note_type) FROM ref.note_index) AS note_types,
               (SELECT count(DISTINCT adsh) FROM ref.note_index) AS filings"""),

    ("7b. What the notes hold, by type", """
        SELECT note_type,
               sum(reports) FILTER (WHERE note_category = 'note') AS note_text,
               sum(reports) FILTER (WHERE note_category = 'note_detail') AS schedules,
               sum(filings) AS filings
        FROM ref.note_catalog
        WHERE note_category IN ('note', 'note_detail')
        GROUP BY 1 ORDER BY filings DESC LIMIT 18"""),

    ("8. The frontier — biggest tags not yet modelled anywhere", """
        SELECT tag, coalesce(label, '(company extension)') AS label,
               greatest(consolidated_filings, dimensioned_filings) AS filings,
               CASE WHEN dimensioned_facts > consolidated_facts THEN 'schedule'
                    ELSE 'consolidated' END AS mostly
        FROM ref.tag_catalog
        WHERE NOT in_spread_template AND standard_taxonomy
          AND tag NOT IN (SELECT DISTINCT source_tag FROM staging.note_inputs)
        ORDER BY filings DESC LIMIT 25"""),
]


def show(con, query: str) -> None:
    cur = con.execute(query)
    headers = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,.4g}" if isinstance(v, float) else
                                    f"{v:,}" if isinstance(v, int) else str(v)))[:64]
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
