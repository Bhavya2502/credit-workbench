"""Tracker G3 — Management Risk metrics out of the stored proxy sections.

The scorecard wants four things: board independence, related-party dealings,
compensation structure and control quality. This reads them off the section text that
`ingest.proxy_sections` stored, so the parser can be corrected and re-run without
fetching tens of thousands of documents from SEC again.

**The fee table is found by its shape, not by its section.** The obvious approach - parse
the `audit_fees` section - fails, because that section is only recovered from 57% of
proxies: the fee table frequently sits under "Ratification of Appointment of Independent
Registered Public Accounting Firm", which is a heading about a vote rather than about
fees. Since the sections tile the document, the table is somewhere in *some* section, so
the search runs across all of a filing's text and looks for what a fee table actually is:
a contiguous block of rows whose first cell is one of the four Item 9(e) categories.

Requiring at least two distinct categories in one block is what makes this safe. An
earlier version took the first row matching each label anywhere in the document and got
16 of 40 filings wrong - a fee total of 11 where the table said 2,017, and in one case a
total of 4,011,243 lifted from the Rule 0-11 filing fee on the cover page. Every one of
those numbers looked perfectly plausible in isolation. The cover-page fee is a lone
"Total fee paid" line with no Audit or Tax row near it, so the two-category rule excludes
it by construction.

Three further traps, all found by looking at real tables rather than by reasoning about
them. These are two-year comparatives, so the *first* figure on a row is taken and the
row is never summed. Some tables are stated in thousands, which is why the note above the
block is read and `fee_units` is recorded rather than assumed. And a dash means nil,
not missing, so it reads as zero.

**What is deliberately not claimed.** Board independence is not extracted from prose. Of
40 sampled proxies the clean phrasing - "X of our Y directors are independent" - appeared
in 2%, while a looser pattern fired on 75% and, inspected, was matching sentences like
"acting as a liaison between the independent directors": a count with no count in it.
So independence is read from the director table where there is one, left null where
there is not, and the sentence is kept alongside as evidence rather than as a number.
A null here means "not stated in a form we can trust", which is a usable input to a
scorecard; a fabricated ratio is not.
"""
from __future__ import annotations

import re

import duckdb

from credit_workbench.common.config import motherduck_token

# ---------------------------------------------------------------- fee table

FEE_ROW = {
    "audit": r"^audit\s*fees?\b",
    "audit_related": r"^audit[\s-]related\s*fees?\b",
    "tax": r"^tax\s*fees?\b",
    "other": r"^all\s*other\s*fees?\b",
}
TOTAL_ROW = r"^total\b"
COMPONENTS = ("audit", "audit_related", "tax", "other")
BLOCK_GAP = 25          # rows of a table sit close together; 25 lines is generous
NUM = re.compile(r"^\(?\$?\s*([\d,]+(?:\.\d+)?)\)?$")
DASH = re.compile(r"^[—–\-]+$")
UNITS = re.compile(r"\(?\s*(?:\$\s*)?in\s+(thousands|millions)\s*\)?", re.IGNORECASE)


def money(cell: str) -> float | None:
    """A fee cell is a number, or a dash meaning nil. Anything else is not a figure."""
    c = cell.strip().lstrip("$").strip()
    if DASH.match(c):
        return 0.0
    m = NUM.match(c)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def row_label(cell: str) -> str:
    """Strip footnote markers and punctuation so 'Audit Fees(1):' reads as 'audit fees'."""
    lab = re.sub(r"[\(\[]\s*\d+\s*[\)\]]", " ", cell).lower()
    return re.sub(r"[^a-z\s-]", " ", lab).strip()


def first_figure(cells: list[str]) -> float | None:
    """The current year is the left-hand column; summing the row would double-count."""
    for c in cells:
        v = money(c)
        if v is not None:
            return v
    return None


def fee_blocks(text: str) -> list[dict]:
    """Every contiguous run of fee-category rows, with its position and figures."""
    lines = text.split("\n")
    marks: list[tuple[int, str, float | None]] = []
    for i, line in enumerate(lines):
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.split("|")]
        lab = row_label(cells[0])
        for name, pat in FEE_ROW.items():
            if re.match(pat, lab):
                marks.append((i, name, first_figure(cells[1:])))
                break
        else:
            if re.match(TOTAL_ROW, lab):
                marks.append((i, "total", first_figure(cells[1:])))
    if not marks:
        return []

    blocks: list[dict] = []
    cur: list[tuple[int, str, float | None]] = []
    for mark in marks:
        if cur and mark[0] - cur[-1][0] > BLOCK_GAP:
            blocks.append(_block(cur, lines))
            cur = []
        cur.append(mark)
    blocks.append(_block(cur, lines))
    # A real fee table names at least two of the four categories. The Rule 0-11 filing
    # fee on the cover page is a lone "Total fee paid" and is excluded by this.
    return [b for b in blocks
            if len(set(b["found"]) & set(COMPONENTS)) >= 2]


def _block(marks, lines) -> dict:
    found: dict[str, float] = {}
    for _, name, val in marks:
        if name not in found and val is not None:
            found[name] = val
    # The units note sits above the table, inside the same section.
    head = "\n".join(lines[max(0, marks[0][0] - 12):marks[0][0] + 1])
    unit = UNITS.search(head)
    return {"found": found, "start": marks[0][0], "end": marks[-1][0],
            "labels": len(marks),
            "units": (unit.group(1).lower() if unit else "dollars")}


def best_fee_block(text: str) -> dict | None:
    """The block naming the most categories; on a tie, the one stating a total."""
    blocks = fee_blocks(text)
    if not blocks:
        return None
    return max(blocks, key=lambda b: (len(set(b["found"]) & set(COMPONENTS)),
                                      "total" in b["found"], b["labels"]))


SCALE = {"dollars": 1.0, "thousands": 1_000.0, "millions": 1_000_000.0}


def fees(text: str) -> dict:
    b = best_fee_block(text)
    if not b:
        return {}
    k = SCALE[b["units"]]
    out = {f"{name}_fees": v * k for name, v in b["found"].items()
           if name in COMPONENTS}
    if "total" in b["found"]:
        out["total_fees_stated"] = b["found"]["total"] * k
    out["fee_units"] = b["units"]
    parts = [b["found"].get(c) for c in COMPONENTS]
    if all(p is not None for p in parts):
        out["fee_components_sum"] = sum(parts) * k
    non_audit = sum(b["found"].get(c, 0.0) for c in ("audit_related", "tax", "other"))
    audit = b["found"].get("audit")
    if audit:
        out["non_audit_fee_ratio"] = non_audit / audit
    return out


# ---------------------------------------------------------------- director table

# A director table's header names the person and at least two attributes. Requiring the
# attributes is what separates it from any other table with a "Name" column.
DIR_HEADER_KEYS = (r"\bage\b", r"director\s+since|since\b|year\s+first",
                   r"\bindependent\b", r"committee", r"occupation|position|principal")
NAME_RE = re.compile(r"^[A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){1,3}"
                     r"(?:,?\s+(?:Jr|Sr|II|III|IV|Ph\.?D|M\.?D)\.?)?$")
INDEP_CELL = re.compile(r"^(yes|independent|x|✓|●|•)$", re.IGNORECASE)


def director_table(text: str) -> dict:
    """Count directors and those marked independent, from a header-anchored table."""
    lines = text.split("\n")
    best: dict = {}
    for i, line in enumerate(lines):
        if "|" not in line:
            continue
        low = line.lower()
        if not re.search(r"\bname\b|\bdirector\b|\bnominee\b", low):
            continue
        if sum(1 for k in DIR_HEADER_KEYS if re.search(k, low)) < 2:
            continue
        has_indep_col = "independent" in low
        listed = marked = 0
        for nxt in lines[i + 1:]:
            if "|" not in nxt:
                if listed:              # a gap after some rows ends the table
                    break
                continue
            cells = [c.strip() for c in nxt.split("|")]
            if not NAME_RE.match(cells[0]):
                if listed:
                    break
                continue
            listed += 1
            if any(INDEP_CELL.match(c) for c in cells[1:]) or \
                    any("independent" in c.lower() for c in cells[1:]):
                marked += 1
        if listed >= 3 and listed > best.get("directors_listed", 0):
            best = {"directors_listed": listed,
                    "directors_marked_independent": marked if has_indep_col else None}
    return best


# The sentence is kept as evidence, never parsed into a count: of 40 sampled proxies the
# clean phrasing appeared in 2%, and the loose one matched prose that counts nothing.
INDEP_SENTENCE = re.compile(
    r"[^.\n]{0,200}\b(?:board|committee)\b[^.\n]{0,200}\bindependen[^.\n]{0,200}\.")


def independence_statement(text: str) -> str | None:
    m = re.search(r"[^.\n]{0,160}\bdetermined\b[^.\n]{0,200}independen[^.\n]{0,160}\.",
                  text, re.IGNORECASE)
    if not m:
        m = INDEP_SENTENCE.search(text)
    return re.sub(r"\s+", " ", m.group(0)).strip()[:600] if m else None


# ---------------------------------------------------------------- other signals

RATIO_RE = re.compile(r"\b([\d,]{1,7}(?:\.\d+)?)\s*(?::|\bto\b)\s*1\b")
NONE_STATED = re.compile(
    r"\b(?:there\s+(?:were|have\s+been)\s+no|we\s+had\s+no|no\s+such)\b[^.]{0,120}"
    r"(?:related[\s-](?:party|person)|transactions)", re.IGNORECASE)
DOLLARS = re.compile(r"\$\s*([\d,]{4,})")


def pay_ratio(text: str) -> float | None:
    """The CEO pay ratio, bounded: outside these limits it is a different ratio."""
    for m in RATIO_RE.finditer(text):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if 1.0 <= v <= 10_000.0:
            return v
    return None


def related_party(text: str) -> dict:
    amounts = [float(m.group(1).replace(",", "")) for m in DOLLARS.finditer(text)]
    return {"related_party_chars": len(text),
            "related_party_none_stated": bool(NONE_STATED.search(text)),
            "related_party_max_amount": max(amounts) if amounts else None}


# ---------------------------------------------------------------- assembly

FLAG_SECTIONS = {"has_clawback_policy": "clawback", "has_hedging_policy": "hedging",
                 "has_say_on_pay": "say_on_pay", "has_pay_vs_performance": "pay_vs_perf",
                 "has_cda": "cda", "has_section16_disclosure": "section16"}

NUMERIC = ["audit_fees", "audit_related_fees", "tax_fees", "other_fees",
           "total_fees_stated", "fee_components_sum", "non_audit_fee_ratio",
           "ceo_pay_ratio", "related_party_max_amount"]
INTEGER = ["directors_listed", "directors_marked_independent", "sections_found",
           "related_party_chars"]
BOOLEAN = list(FLAG_SECTIONS)
TEXT = ["cik", "adsh", "form", "filing_date", "period_of_report", "fee_units",
        "fee_source_section", "independence_statement"]


def metrics_for_filing(sections: dict[str, str], meta: dict) -> dict:
    """One row of the mart, from one filing's sections."""
    row: dict = dict(meta)
    row["sections_found"] = len(sections)
    for flag, name in FLAG_SECTIONS.items():
        row[flag] = name in sections

    # Fees: search every section, because the table is often not under a fee heading.
    best: tuple[int, str, dict] | None = None
    for name, body in sections.items():
        b = best_fee_block(body)
        if not b:
            continue
        score = len(set(b["found"]) & set(COMPONENTS))
        if best is None or score > best[0]:
            best = (score, name, b)
    if best:
        row.update(fees(sections[best[1]]))
        row["fee_source_section"] = best[1]

    # Directors: the nominee and governance sections are where the table sits, but
    # nothing is lost by looking wider - the best table wins.
    for name in ("nominees", "governance", "independence", "committees"):
        found = director_table(sections.get(name, ""))
        if found.get("directors_listed", 0) > row.get("directors_listed", 0):
            row.update(found)

    for name in ("independence", "governance"):
        if sections.get(name) and not row.get("independence_statement"):
            row["independence_statement"] = independence_statement(sections[name])

    if sections.get("pay_ratio"):
        row["ceo_pay_ratio"] = pay_ratio(sections["pay_ratio"])
    if sections.get("related_party"):
        row.update(related_party(sections["related_party"]))
    return row


COLUMNS = TEXT + NUMERIC + INTEGER + BOOLEAN


def build(con) -> int:
    """Read the section view, write one row per proxy filing."""
    filings = con.execute("""
        SELECT cik, adsh, any_value(form) AS form, any_value(filing_date) AS filing_date,
               any_value(period_of_report) AS period_of_report
        FROM quali.proxy_sections
        GROUP BY cik, adsh""").fetchall()
    print(f"{len(filings):,} proxy filings to score")

    rows: list[dict] = []
    for cik, adsh, form, filed, period in filings:
        secs = dict(con.execute("""
            SELECT section, text FROM quali.proxy_sections
            WHERE cik = ? AND adsh = ?""", [cik, adsh]).fetchall())
        rows.append(metrics_for_filing(secs, {
            "cik": str(cik), "adsh": str(adsh), "form": form,
            "filing_date": str(filed), "period_of_report": str(period)}))
        if len(rows) % 2000 == 0:
            print(f"  scored {len(rows):,}")

    con.execute("CREATE SCHEMA IF NOT EXISTS marts")
    con.execute("DROP TABLE IF EXISTS marts.governance_metrics")
    cols = ", ".join(
        f"{c} " + ("VARCHAR" if c in TEXT else "BOOLEAN" if c in BOOLEAN
                   else "BIGINT" if c in INTEGER else "DOUBLE")
        for c in COLUMNS)
    con.execute(f"CREATE TABLE marts.governance_metrics ({cols})")
    con.executemany(
        f"INSERT INTO marts.governance_metrics VALUES ({', '.join(['?'] * len(COLUMNS))})",
        [[r.get(c) for c in COLUMNS] for r in rows])
    n = con.execute("SELECT count(*) FROM marts.governance_metrics").fetchone()[0]
    print(f"table marts.governance_metrics  {n:,} rows")
    return n


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    build(con)


if __name__ == "__main__":
    main()
