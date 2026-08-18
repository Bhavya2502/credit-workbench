"""Two open questions: the ICFR recall gap, and how filers state their own ratings.

**ICFR.** `diag_icfr.py` established that the conclusion sentence discriminates the right
way - an adverse conclusion travels with 56.97% distress inside 24 months against 24.79%
for a clean one - but it extracted a conclusion from only 23,380 of 36,165 Item 9A
sections. 12,929 yielded nothing. The hypothesis recorded there was word order:
"maintained effective internal control over financial reporting" puts the adjective before
the subject, where the existing patterns expect it after. That was explicitly left as a
hypothesis and is tested here, by measuring the incremental recall of each pattern family
and, crucially, whether the adverse rate stays put. A pattern that lifts recall while
dragging the adverse rate to 40% is matching the wrong sentences.

**Ratings.** 541 FY2024 MD&A sections mention Moody's, 324 S&P and 261 Fitch. Whether a
usable reference set can be built from that depends entirely on phrasing, which nobody here
has read. The rating symbols are hostile to matching on their own - a bare "A" or "C" is a
letter, "BBB" appears in prose, Moody's "Ca" is a word fragment - so the only safe route is
agency-adjacent context, and what that context looks like has to be seen before any pattern
is committed to.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

ICFR = "(?:internal control over financial reporting|icfr)"
MOODYS_SYM = r"\b(?:Aaa|Aa[123]|A[123]|Baa[123]|Ba[123]|B[123]|Caa[123])\b"
SP_SYM = r"\b(?:AAA|AA[+-]?|BBB[+-]?|BB[+-]?|CCC[+-]?)\b"
AGENCY = r"(?:Moody|Standard & Poor|S&P|Fitch)"

# Existing patterns, from diag_icfr.py.
EFF_A = rf"{ICFR}[^.]{{0,120}}?(?:was|were|is|are|remained)\s+effective"
NEG_A = rf"{ICFR}[^.]{{0,120}}?(?:was|were|is|are)\s+(?:not\s+effective|ineffective)"
# The hypothesis: adjective before subject.
EFF_B = r"maintained\s+effective\s+internal\s+control"
NEG_B = r"did\s+not\s+maintain\s+effective\s+internal\s+control"
# Looser still, either order - an upper bound on what is reachable at all.
EFF_C = rf"(?:{ICFR}[^.]{{0,60}}effective|effective[^.]{{0,60}}{ICFR})"

Q = [
    ("1. Recall of each pattern family", f"""
        SELECT count(*) AS sections,
               count(*) FILTER (WHERE regexp_matches(lower(text), '{EFF_A}')
                                  OR regexp_matches(lower(text), '{NEG_A}'))
                   AS conclusion_A_only,
               count(*) FILTER (WHERE regexp_matches(lower(text), '{EFF_A}')
                                  OR regexp_matches(lower(text), '{NEG_A}')
                                  OR regexp_matches(lower(text), '{EFF_B}')
                                  OR regexp_matches(lower(text), '{NEG_B}'))
                   AS conclusion_A_plus_B,
               count(*) FILTER (WHERE regexp_matches(lower(text), '{EFF_C}')
                                  OR regexp_matches(lower(text), '{NEG_A}')
                                  OR regexp_matches(lower(text), '{NEG_B}'))
                   AS conclusion_with_C
        FROM quali.filing_sections
        WHERE item = '9A' AND substr(filing_date, 1, 4) BETWEEN '2020' AND '2025'"""),

    ("2. The adverse rate under each - a shift means wrong sentences are matching", f"""
        SELECT round(100.0 * count(*) FILTER (
                   WHERE regexp_matches(lower(text), '{NEG_A}')) / count(*), 2)
                   AS pct_adverse_A,
               round(100.0 * count(*) FILTER (
                   WHERE regexp_matches(lower(text), '{NEG_A}')
                      OR regexp_matches(lower(text), '{NEG_B}')) / count(*), 2)
                   AS pct_adverse_A_plus_B,
               count(*) FILTER (WHERE regexp_matches(lower(text), '{NEG_B}')
                            AND NOT regexp_matches(lower(text), '{NEG_A}'))
                   AS adverse_only_from_B
        FROM quali.filing_sections
        WHERE item = '9A' AND substr(filing_date, 1, 4) BETWEEN '2020' AND '2025'"""),

    ("3. What B adds that A missed - read the sentences", f"""
        SELECT cik, substr(filing_date, 1, 10) AS filed,
               regexp_extract(lower(text), '[^.]{{0,110}}{EFF_B}[^.]{{0,60}}') AS sentence
        FROM quali.filing_sections
        WHERE item = '9A' AND substr(filing_date, 1, 4) = '2024'
          AND regexp_matches(lower(text), '{EFF_B}')
          AND NOT regexp_matches(lower(text), '{EFF_A}')
          AND NOT regexp_matches(lower(text), '{NEG_A}')
        LIMIT 5"""),

    ("4. Still no conclusion after A and B - are these even ICFR discussions?", f"""
        SELECT count(*) AS still_missing,
               count(*) FILTER (WHERE lower(text) LIKE '%internal control%')
                   AS mentions_internal_control,
               count(*) FILTER (WHERE lower(text) LIKE '%disclosure controls%')
                   AS mentions_disclosure_controls,
               round(median(char_len), 0) AS median_chars
        FROM quali.filing_sections
        WHERE item = '9A' AND substr(filing_date, 1, 4) BETWEEN '2020' AND '2025'
          AND NOT regexp_matches(lower(text), '{EFF_A}')
          AND NOT regexp_matches(lower(text), '{NEG_A}')
          AND NOT regexp_matches(lower(text), '{EFF_B}')
          AND NOT regexp_matches(lower(text), '{NEG_B}')"""),

    ("5. G-07 — which MD&A sections mention an agency at all?", """
        SELECT count(*) AS mdna_sections,
               count(*) FILTER (WHERE lower(text) LIKE '%moody%') AS moodys,
               count(*) FILTER (WHERE lower(text) LIKE '%standard & poor%'
                                   OR lower(text) LIKE '%s&p global%'
                                   OR lower(text) LIKE '%s&p ratings%') AS sp,
               count(*) FILTER (WHERE lower(text) LIKE '%fitch%') AS fitch,
               count(*) FILTER (WHERE lower(text) LIKE '%credit rating%')
                   AS says_credit_rating
        FROM quali.mdna WHERE substr(filing_date, 1, 4) BETWEEN '2019' AND '2025'"""),

    ("6. Moody's near a Moody's symbol - the real phrasing", f"""
        SELECT cik, substr(filing_date, 1, 10) AS filed,
               regexp_extract(text, '[^.]{{0,90}}Moody[^.]{{0,130}}') AS sentence
        FROM quali.mdna
        WHERE substr(filing_date, 1, 4) = '2024' AND lower(text) LIKE '%moody%'
          AND regexp_matches(text, '{MOODYS_SYM}')
        LIMIT 8"""),

    ("7. S&P and Fitch, whose symbols collide with ordinary prose", f"""
        SELECT cik, substr(filing_date, 1, 10) AS filed,
               regexp_extract(text, '[^.]{{0,90}}{AGENCY}[^.]{{0,130}}') AS sentence
        FROM quali.mdna
        WHERE substr(filing_date, 1, 4) = '2024'
          AND (lower(text) LIKE '%standard & poor%' OR lower(text) LIKE '%fitch%')
          AND regexp_matches(text, '{SP_SYM}')
        LIMIT 8"""),

    ("8. How often is an agency within 120 characters of a symbol?", f"""
        SELECT count(*) AS sections,
               count(*) FILTER (WHERE regexp_matches(text,
                   '{AGENCY}[^.]{{0,120}}(?:{MOODYS_SYM}|{SP_SYM})')) AS agency_then_symbol,
               count(*) FILTER (WHERE regexp_matches(text,
                   '(?:{MOODYS_SYM}|{SP_SYM})[^.]{{0,120}}{AGENCY}')) AS symbol_then_agency
        FROM quali.mdna WHERE substr(filing_date, 1, 4) = '2024'"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)))[:148]
             for v in r] for r in cur.fetchall()]
    if not rows:
        print("  (no rows)")
        return
    w = [min(max(len(h), *(len(r[i]) for r in rows)), 148) for i, h in enumerate(heads)]
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
