"""The controls pillar — management's ICFR conclusion, out of Item 9A.

The Management Risk scorecard needs control quality and the proxy does not carry it. Item
9A does, and 116,812 of those sections are already in the lake, so this needs no fetching.

**Why the conclusion sentence and not the phrase.** Searching for "material weakness" would
flag most filers: Item 9A carries the *definition* of one as boilerplate, so a company with
clean controls still uses the words. `quali.note_signals.material_weakness` is built that
way and is documented as discriminating inversely - flagged companies default *less* - which
is that trap in its finished form. Polarity of the conclusion sentence has no such problem.

**Measured, and this is why it is worth having.** An adverse ICFR conclusion travels with
56.97% distress inside 24 months against 24.79% for a clean one - a 2.3x lift, better than
most ratios in `marts.ratio_values`.

**Two patterns, because one was biased.** The obvious phrasing puts the adjective after the
subject: "internal control over financial reporting was not effective". The other inverts it:
"the company has not maintained effective internal control". Matching only the first found a
conclusion in 23,236 of 36,165 sections at an adverse rate of 13.03%; adding the second
reaches 26,445 at 15.51%, and all 896 of that increase is genuine adverse conclusions the
first form could not see. So the first pattern was not merely incomplete - it under-reported
adverse cases specifically, the direction that matters most for a risk signal.

A third, looser form was tested and rejected: any adjacency of "effective" to the subject
reaches 30,854 sections but cannot tell polarity, since "not effective" contains "effective".
Recall is not worth a signal that cannot say which way it points.

**What is left null.** 9,720 sections state no conclusion this can read. They are real
controls discussions - 9,449 mention internal control, 9,407 mention disclosure controls, at
a median of 3,003 characters - they simply phrase it some other way. Null means "not stated
in a form we can trust", as everywhere else in this warehouse.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

ICFR = "(?:internal control over financial reporting|icfr)"

# Adjective after the subject.
NEG_AFTER = ICFR + r"[^.]{0,120}?(?:was|were|is|are)\s+(?:not\s+effective|ineffective)"
POS_AFTER = ICFR + r"[^.]{0,120}?(?:was|were|is|are|remained)\s+effective"
# Adjective before the subject; both negative forms are in use.
NEG_BEFORE = r"(?:did\s+not\s+maintain|not\s+maintained)\s+effective\s+internal\s+control"
POS_BEFORE = r"maintained\s+effective\s+internal\s+control"

# Disclosure controls are a separate conclusion under Item 9A(a) and are often the only one
# a smaller reporting company gives, so they are recorded separately rather than conflated.
DC = "disclosure controls and procedures"
NEG_DC = DC + r"[^.]{0,80}?(?:were|was|are|is)\s+(?:not\s+effective|ineffective)"
POS_DC = DC + r"[^.]{0,80}?(?:were|was|are|is)\s+effective"

# A weakness that was *identified*, as distinct from the definition of one. The definition
# always reads "a material weakness is a deficiency"; a finding names or locates it.
FOUND = (r"(?:identified|following|described below|concluded that)"
         r"[^.]{0,80}material\s+weakness"
         r"|material\s+weakness(?:es)?\s+(?:in|described|identified|existed)")
DEFN = r"material\s+weakness\s+is\s+a\s+deficiency"
REMED = r"remediat"

BUILD = f"""
CREATE OR REPLACE TABLE marts.control_signals AS
WITH s AS (
    SELECT cik, adsh, filing_date, period_of_report, char_len, lower(text) AS t
    FROM quali.filing_sections WHERE item = '9A'
),
flagged AS (
    SELECT cik, adsh, filing_date,
           TRY_CAST(substr(period_of_report, 1, 4) AS INTEGER) AS fy,
           char_len,
           regexp_matches(t, '{NEG_AFTER}') AS neg_after,
           regexp_matches(t, '{NEG_BEFORE}') AS neg_before,
           regexp_matches(t, '{POS_AFTER}') AS pos_after,
           regexp_matches(t, '{POS_BEFORE}') AS pos_before,
           regexp_matches(t, '{NEG_DC}') AS neg_dc,
           regexp_matches(t, '{POS_DC}') AS pos_dc,
           regexp_matches(t, '{FOUND}') AS weakness_found,
           regexp_matches(t, '{DEFN}') AS has_definition,
           regexp_matches(t, '{REMED}') AS remediation_discussed,
           t
    FROM s
),
witnessed AS (
    -- The evidence sentence is extracted only for the pattern that actually matched.
    -- A coalesce over four regexp_extract calls evaluates all four on every one of
    -- 116,812 sections, and each one scans the whole document twice over for the
    -- surrounding context. A CASE does one.
    SELECT * EXCLUDE (t),
           CASE
               WHEN neg_after THEN
                   regexp_extract(t, '[^.]{{0,140}}{NEG_AFTER}[^.]{{0,60}}')
               WHEN neg_before THEN
                   regexp_extract(t, '[^.]{{0,140}}{NEG_BEFORE}[^.]{{0,60}}')
               WHEN pos_after THEN
                   regexp_extract(t, '[^.]{{0,140}}{POS_AFTER}[^.]{{0,60}}')
               WHEN pos_before THEN
                   regexp_extract(t, '[^.]{{0,140}}{POS_BEFORE}[^.]{{0,60}}')
           END AS sentence
    FROM flagged
)
SELECT cik, adsh, filing_date, fy, char_len,
       CASE
           WHEN neg_after OR neg_before THEN 'not_effective'
           WHEN pos_after OR pos_before THEN 'effective'
       END AS icfr_conclusion,
       -- Which form found it, so a later reader can tell how much rests on each pattern.
       CASE
           WHEN neg_after OR pos_after THEN 'adjective_after_subject'
           WHEN neg_before OR pos_before THEN 'adjective_before_subject'
       END AS icfr_pattern,
       -- Both polarities present is usually a remediated weakness: adverse for the prior
       -- year, clean for this one. The negative takes precedence and the conflict is
       -- flagged rather than resolved silently, because which year a conclusion belongs to
       -- cannot be read off the sentence.
       (neg_after OR neg_before) AND (pos_after OR pos_before) AS conflicting_conclusions,
       CASE
           WHEN neg_dc THEN 'not_effective'
           WHEN pos_dc THEN 'effective'
       END AS disclosure_controls_conclusion,
       weakness_found AS material_weakness_identified,
       has_definition AS carries_weakness_definition,
       remediation_discussed,
       regexp_replace(trim(sentence), '[ ]+', ' ', 'g') AS conclusion_sentence
FROM witnessed
"""


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    con.execute("SET temp_directory = '/tmp/duckdb_spill'")
    con.execute("CREATE SCHEMA IF NOT EXISTS marts")
    con.execute(BUILD)

    rows, cos, concl = con.execute("""
        SELECT count(*), count(DISTINCT cik), count(icfr_conclusion)
        FROM marts.control_signals""").fetchone()
    print(f"table marts.control_signals  {rows:,} rows, {cos:,} companies, "
          f"{concl:,} with a conclusion ({100 * concl / max(rows, 1):.0f}%)")

    print("\nConclusion by filing year, with the adverse rate:")
    cur = con.execute("""
        SELECT substr(filing_date, 1, 4) AS filing_year, count(*) AS sections,
               count(icfr_conclusion) AS with_conclusion,
               count(*) FILTER (WHERE icfr_conclusion = 'not_effective') AS adverse,
               round(100.0 * count(*) FILTER (WHERE icfr_conclusion = 'not_effective')
                     / nullif(count(icfr_conclusion), 0), 1) AS pct_adverse
        FROM marts.control_signals
        WHERE substr(filing_date, 1, 4) BETWEEN '2019' AND '2025'
        GROUP BY 1 ORDER BY 1""")
    for r in cur.fetchall():
        print("  " + "  ".join(f"{v!s:<15}" for v in r))

    print("\nWhich pattern carried the conclusion:")
    cur = con.execute("""
        SELECT icfr_pattern, count(*) AS n,
               count(*) FILTER (WHERE icfr_conclusion = 'not_effective') AS adverse
        FROM marts.control_signals WHERE icfr_conclusion IS NOT NULL
        GROUP BY 1 ORDER BY n DESC""")
    for r in cur.fetchall():
        print("  " + "  ".join(f"{v!s:<28}" for v in r))

    print("\nThe test that matters - does an adverse conclusion travel with distress?")
    cur = con.execute("""
        SELECT c.icfr_conclusion, count(*) AS company_years,
               round(100.0 * avg(CASE WHEN o.distress_24m THEN 1.0 ELSE 0.0 END), 2)
                   AS pct_distress_24m,
               round(100.0 * avg(CASE WHEN o.default_24m THEN 1.0 ELSE 0.0 END), 2)
                   AS pct_default_24m
        FROM marts.control_signals c
        JOIN marts.credit_outcomes o ON o.cik = c.cik AND o.fy = c.fy
        WHERE c.icfr_conclusion IS NOT NULL
        GROUP BY 1 ORDER BY 1""")
    for r in cur.fetchall():
        print("  " + "  ".join(f"{v!s:<18}" for v in r))

    print("\nAnd the phrase-based alternative, to show why polarity was used instead:")
    cur = con.execute("""
        SELECT c.material_weakness_identified, count(*) AS company_years,
               round(100.0 * avg(CASE WHEN o.distress_24m THEN 1.0 ELSE 0.0 END), 2)
                   AS pct_distress_24m
        FROM marts.control_signals c
        JOIN marts.credit_outcomes o ON o.cik = c.cik AND o.fy = c.fy
        GROUP BY 1 ORDER BY 1""")
    for r in cur.fetchall():
        print("  " + "  ".join(f"{v!s:<18}" for v in r))


if __name__ == "__main__":
    main()
