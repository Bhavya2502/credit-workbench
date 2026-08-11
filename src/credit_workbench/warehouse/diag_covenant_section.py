"""Where does the maintenance covenant actually live?

The first probe showed that ratios scattered through an agreement are almost all
incurrence tests - conditions attached to a disposition, an investment, a restricted
payment, or the incurrence of more debt. Reading those as covenant levels would fill a
mart with basket thresholds that look exactly like covenants.

The maintenance covenant is different in kind: a standing obligation to keep a ratio
inside a level, tested every quarter, and it sits under its own heading. Find that
heading, and the extraction has an anchor that the scattered ratios do not.

What has to be established: how the section is titled, whether a heading match actually
lands on the covenant rather than a cross-reference to it, how long the section runs,
and whether the levels inside it are given as a single number or a table of dates.
"""
from __future__ import annotations

import re

import duckdb

from credit_workbench.common.config import motherduck_token

# Headings filers use for the maintenance covenant section. Deliberately not anchored
# to the end of the line: a real heading runs straight on into its own paragraph
# ("Financial Covenant. The Borrower shall not permit..."), so requiring the line to end
# shortly after the name matched only contents-page entries. Rejecting the contents page
# is the gap rule's job, not this pattern's.
HEADING_RE = re.compile(
    r"^[ \t]*(?:(?:section|article)\s+[\dIVXLC]+(?:\.\d+)*[.\s-]*)?"
    r"(financial covenants?|financial condition covenants?"
    r"|financial performance covenants?|covenant compliance)\b",
    re.IGNORECASE | re.MULTILINE)

# Every numbered heading in the agreement. The covenant section runs from its own
# heading to the NEXT heading of any kind - measuring to the next *financial covenant*
# heading instead let a table-of-contents entry run to the end of the document, which
# is the same trap the 10-K splitter hit and the same rule that fixes it.
SECTION_RE = re.compile(
    r"^[ \t]*(?:section\s+)?(?:\d{1,2}\.\d{1,3}|article\s+[IVXLC]+|\d{1,2}\.\s)",
    re.IGNORECASE | re.MULTILINE)

RATIO_RE = re.compile(
    r"(\d{1,2}(?:\.\d{1,3})?)\s*(?::|\s+to\s+|\s*/\s*)\s*1(?:\.0{1,3})?\b")

# A step-down schedule reads as a table of period ends against levels.
PERIOD_RE = re.compile(
    r"(?:fiscal quarter|fiscal year|quarter|period)\s+end(?:ing|ed)|"
    r"(?:march|june|september|december)\s+\d{1,2},?\s+20\d\d|"
    r"through\s+.{0,30}20\d\d|thereafter",
    re.IGNORECASE)


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")

    print("### 1. Which heading do filers use?")
    for label, pattern in [
            ("financial covenant(s)", "financial covenants?"),
            ("financial condition covenant", "financial condition covenants?"),
            ("financial performance covenant", "financial performance covenants?"),
            ("covenant compliance", "covenant compliance"),
            ("no such heading at all", "^(?!x)x")]:
        if label.startswith("no such"):
            n = con.execute("""
                SELECT count(*) FROM quali.debt_agreements
                WHERE doc_kind = 'credit_agreement'
                  AND NOT regexp_matches(lower(text), 'financial covenant')""").fetchone()[0]
        else:
            n = con.execute(f"""
                SELECT count(*) FROM quali.debt_agreements
                WHERE doc_kind = 'credit_agreement'
                  AND regexp_matches(lower(text), '{pattern}')""").fetchone()[0]
        print(f"  {label:<32} {n:>6,} credit agreements")

    docs = con.execute("""
        SELECT adsh, cik, text FROM quali.debt_agreements
        WHERE doc_kind = 'credit_agreement'
          AND lower(text) LIKE '%financial covenant%'
          AND lower(text) LIKE '%leverage ratio%'
        ORDER BY hash(adsh) LIMIT 25""").fetchall()
    print(f"\n### 2. Heading matches across {len(docs)} agreements")

    stats = []
    for adsh, cik, text in docs:
        hits = list(HEADING_RE.finditer(text))
        boundaries = [m.start() for m in SECTION_RE.finditer(text)]
        best, best_len = None, 0
        for m in hits:
            nxt = min((b for b in boundaries if b > m.start() + 20), default=len(text))
            span = nxt - m.start()
            if span > best_len:
                best, best_len = m, span
        stats.append((cik, len(hits), best_len,
                      text[best.start():best.start() + best_len] if best else ""))

    for cik, n_hits, span, _body in stats[:10]:
        print(f"  cik={cik}  heading matches={n_hits:<3} longest section={span:>7,} chars")

    print("\n### 3. What the covenant section actually says")
    shown = 0
    for cik, _n, span, body in stats:
        if span < 400 or shown >= 4:
            continue
        clean = re.sub(r"\s+", " ", body[:1400])
        ratios = sorted({m.group(1) for m in RATIO_RE.finditer(body)})
        periods = len(PERIOD_RE.findall(body))
        print(f"\n  --- cik={cik}  section={span:,} chars  "
              f"levels={ratios[:8]}  period markers={periods}")
        print(f"    {clean[:700]}")
        shown += 1

    print("\n### 4. Is the level a single number or a schedule?")
    counts = [(len({m.group(1) for m in RATIO_RE.finditer(b)}),
               len(PERIOD_RE.findall(b))) for _c, _n, s, b in stats if s >= 400]
    if counts:
        multi = sum(1 for r, _p in counts if r > 1)
        dated = sum(1 for _r, p in counts if p >= 2)
        print(f"  sections examined: {len(counts)}")
        print(f"  with more than one level: {100*multi/len(counts):.0f}%")
        print(f"  with dated period markers (a step-down table): "
              f"{100*dated/len(counts):.0f}%")


if __name__ == "__main__":
    main()
