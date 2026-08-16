"""Is the ICFR conclusion extractable from Item 9A? — the controls pillar of G3

The proxy gives auditor independence through the fee split, but the strongest control
signal in a filing is whether management concluded that internal control over financial
reporting was effective, and whether a material weakness was identified. That sits in
Item 9A of the 10-K, and 116,812 of those sections are already in the lake - no fetching
required.

The obvious approach is to search for "material weakness", and it would be wrong on
almost every filing. Item 9A carries the definition as boilerplate - "a material weakness
is a deficiency, or a combination of deficiencies, in internal control over financial
reporting such that there is a reasonable possibility..." - so a filer with clean controls
still uses the phrase. This is the exact failure this project keeps meeting: a section
that contains the words without making the statement.

So what gets measured here is the difference between the definition and the finding. The
conclusion sentence carries polarity - "was effective" against "was not effective" - and
that is testable. Reported below: how often the boilerplate appears, how often a
conclusion is found, which way it points, and whether the split is credible. Roughly
95% of filers conclude effective in any year, so a much higher rate of "not effective"
means the negation is being matched loosely, and a much lower one means it is being missed.

Scans are restricted to a sample and to recent years: this table is 116,812 sections of
median 4,135 characters and MotherDuck's daily compute is limited.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

# A conclusion is a sentence about ICFR or disclosure controls carrying a polarity. The
# negation must be adjacent to "effective" - "not effective", "was ineffective" - because
# Item 9A is full of sentences that mention effectiveness without concluding anything.
EFFECTIVE = r"(?:internal control over financial reporting|icfr)[^.]{0,120}?" \
            r"(?:was|were|is|are|remained)\s+effective"
NOT_EFFECTIVE = r"(?:internal control over financial reporting|icfr)[^.]{0,120}?" \
                r"(?:was|were|is|are)\s+(?:not\s+effective|ineffective)"
# An identified weakness, as opposed to the definition of one.
FOUND_WEAKNESS = r"(?:identified|following|described below|concluded that)[^.]{0,80}" \
                 r"material\s+weakness|material\s+weakness(?:es)?\s+(?:in|described|" \
                 r"identified|existed|relating)"
DEFINITION = r"material\s+weakness\s+is\s+a\s+deficiency"

Q = [
    ("1. How big is Item 9A, and over what period?", """
        SELECT count(*) AS sections, count(DISTINCT cik) AS companies,
               min(substr(filing_date, 1, 4)) AS first_year,
               max(substr(filing_date, 1, 4)) AS last_year,
               round(median(char_len), 0) AS median_chars
        FROM quali.filing_sections WHERE item = '9A'"""),

    ("2. The trap: how often is 'material weakness' present at all, "
     "and how often only as the definition?", f"""
        SELECT count(*) AS sections,
               round(100.0 * count(*) FILTER (
                   WHERE lower(text) LIKE '%material weakness%') / count(*), 1)
                   AS pct_mentioning,
               round(100.0 * count(*) FILTER (
                   WHERE regexp_matches(lower(text), '{DEFINITION}')) / count(*), 1)
                   AS pct_definition_boilerplate
        FROM quali.filing_sections
        WHERE item = '9A' AND substr(filing_date, 1, 4) BETWEEN '2020' AND '2025'"""),

    ("3. Is a conclusion found, and which way does it point?", f"""
        SELECT count(*) AS sections,
               count(*) FILTER (WHERE regexp_matches(lower(text), '{NOT_EFFECTIVE}'))
                   AS not_effective,
               count(*) FILTER (WHERE regexp_matches(lower(text), '{EFFECTIVE}'))
                   AS effective,
               count(*) FILTER (
                   WHERE NOT regexp_matches(lower(text), '{EFFECTIVE}')
                     AND NOT regexp_matches(lower(text), '{NOT_EFFECTIVE}'))
                   AS no_conclusion_found
        FROM quali.filing_sections
        WHERE item = '9A' AND substr(filing_date, 1, 4) BETWEEN '2020' AND '2025'"""),

    # Both patterns firing is expected, not a contradiction: a filer often states that
    # disclosure controls were effective and ICFR was not, or describes a remediated
    # weakness. What matters is that the negative is not swamping the positive.
    ("4. The adverse rate — about 5% of filers is the credible range", f"""
        SELECT substr(filing_date, 1, 4) AS filing_year,
               count(*) AS sections,
               round(100.0 * count(*) FILTER (
                   WHERE regexp_matches(lower(text), '{NOT_EFFECTIVE}'))
                     / count(*), 1) AS pct_not_effective,
               round(100.0 * count(*) FILTER (
                   WHERE regexp_matches(lower(text), '{FOUND_WEAKNESS}'))
                     / count(*), 1) AS pct_weakness_identified
        FROM quali.filing_sections
        WHERE item = '9A' AND substr(filing_date, 1, 4) BETWEEN '2019' AND '2025'
        GROUP BY 1 ORDER BY 1"""),

    # The property that makes this worth having: an adverse control opinion should be
    # commoner among companies that go on to distress. If it is not, the signal is noise.
    ("5. Does an adverse conclusion travel with distress? (the real test)", f"""
        SELECT regexp_matches(lower(s.text), '{NOT_EFFECTIVE}') AS icfr_not_effective,
               count(*) AS company_years,
               round(100.0 * avg(CASE WHEN o.distress_24m THEN 1.0 ELSE 0.0 END), 2)
                   AS pct_distress_24m
        FROM quali.filing_sections s
        JOIN marts.credit_outcomes o
          ON lpad(CAST(o.cik AS VARCHAR), 10, '0') = lpad(CAST(s.cik AS VARCHAR), 10, '0')
         AND substr(CAST(o.observation_date AS VARCHAR), 1, 4) = substr(s.filing_date, 1, 4)
        WHERE s.item = '9A' AND substr(s.filing_date, 1, 4) BETWEEN '2019' AND '2024'
        GROUP BY 1"""),

    ("6. Sections where a weakness was identified — read the sentence", f"""
        SELECT cik, substr(filing_date, 1, 10) AS filed,
               regexp_extract(lower(text),
                   '[^.]{{0,120}}(?:{NOT_EFFECTIVE})[^.]{{0,80}}') AS conclusion
        FROM quali.filing_sections
        WHERE item = '9A' AND substr(filing_date, 1, 4) = '2024'
          AND regexp_matches(lower(text), '{NOT_EFFECTIVE}')
        LIMIT 6"""),

    ("7. And clean ones, to confirm the positive pattern is not matching negatives", f"""
        SELECT cik, substr(filing_date, 1, 10) AS filed,
               regexp_extract(lower(text),
                   '[^.]{{0,60}}(?:{EFFECTIVE})[^.]{{0,40}}') AS conclusion
        FROM quali.filing_sections
        WHERE item = '9A' AND substr(filing_date, 1, 4) = '2024'
          AND regexp_matches(lower(text), '{EFFECTIVE}')
          AND NOT regexp_matches(lower(text), '{NOT_EFFECTIVE}')
        LIMIT 5"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)))[:150]
             for v in r] for r in cur.fetchall()]
    if not rows:
        print("  (no rows)")
        return
    w = [min(max(len(h), *(len(r[i]) for r in rows)), 150)
         for i, h in enumerate(heads)]
    print("  " + "  ".join(h.ljust(x) for h, x in zip(heads, w)))
    print("  " + "  ".join("-" * x for x in w))
    for r in rows:
        print("  " + "  ".join(v.ljust(x) for v, x in zip(r, w)))


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    for title, q in Q:
        print(f"\n### {title}")
        try:
            show(con, q)
        except Exception as exc:
            print(f"  (failed: {str(exc)[:200]})")


if __name__ == "__main__":
    main()
