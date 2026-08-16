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

import argparse
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
# "Total", "Total fees", "Total fees paid" - but not "Total Compensation (Per Comp
# Table)", which pulled an entire Pay-versus-Performance table in as a fee block and
# supplied a total of 10,522,375 against components that had nothing to do with it.
TOTAL_ROW = r"^total(\s+fees?(\s+(paid|billed|and\s+\w+))?)?$|^total\s+fees?\b"
COMPONENTS = ("audit", "audit_related", "tax", "other")
BLOCK_GAP = 12          # a real fee table's rows are adjacent; 25 spanned other tables
NUM = re.compile(r"^\$?\s*([\d,]+(?:\.\d+)?)$")
FOOTNOTE = re.compile(r"^\(\s*\d{1,2}\s*\)$")
DASH = re.compile(r"^(?:[—–\-]+|nil|none|n/?a)$", re.IGNORECASE)
# The word "in" is optional and often absent: "(USD$ millions)", "($ thousands)",
# "2023 (in millions)". Missing those left a table of 0.88 unscaled, so an $880,000
# audit fee was stored as 88 cents.
UNITS = re.compile(r"\(?\s*(?:in\s+|\$\s*|usd\s*\$?\s*)*"
                   r"(thousands|millions)\s*\)?", re.IGNORECASE)


def money(cell: str) -> float | None:
    """A fee cell is a number, or a dash or 'nil' meaning zero.

    A parenthesised value is *not* read as a negative here. Filings put the footnote
    marker in its own cell - "Audit fees | (1) | $ | 700,140" - and reading "(1)" as a
    number returned an audit fee of 1, with audit-related 2 and tax 3: the footnote
    numbering, in a table whose real figures were right beside it. Fees are never
    negative, so nothing is lost by refusing parentheses outright.
    """
    c = cell.strip()
    if FOOTNOTE.match(c) or c.startswith("("):
        return None
    c = c.lstrip("$").strip()
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


def figures(cells: list[str]) -> list[float]:
    """Every cell on the row that parses as a figure, in order."""
    return [v for v in (money(c) for c in cells) if v is not None]


# No audit engagement costs less than a few thousand dollars, so at the table's own scale
# a figure below this cannot be a fee. Used to step over a footnote marker sitting in its
# own cell, which is not caught by refusing parentheses: Intel's 2023 proxy renders the
# marker as a bare "1", giving "Audit Fees | 1 | 12,345,678" and an audit fee of one
# dollar, with audit-related 2 and tax 3 - the footnote numbering again, in a table whose
# stated total of $20.1m was read correctly all along.
FEE_FLOOR = 1_000.0


def pick_figure(figs: list[float], scale: float) -> float | None:
    """The current year's figure, stepping over anything too small to be a fee.

    An explicit zero is taken immediately rather than skipped. These tables are two-year
    comparatives and a nil current year beside a non-nil prior one is common - "Tax Fees
    | - | 1,200" means nil this year, and treating the dash as too small to be a fee
    would report last year's number as this year's.
    """
    if not figs:
        return None
    for f in figs:
        if f == 0.0 or f * scale >= FEE_FLOOR:
            return f
    return figs[0]      # nothing plausible: keep what the document said


def fee_blocks(text: str) -> list[dict]:
    """Every contiguous run of fee-category rows, with its position and figures.

    The cheap substring test before `row_label` matters: this runs over every table row
    of every section of every proxy, and cleaning a label costs two regex substitutions
    before five more decide it was never a fee row. Every pattern here needs "fee" or
    "total" somewhere in the first cell, so testing for those first is a strict superset
    of what can match and skips almost every row for a fraction of the cost.
    """
    lines = text.split("\n")
    marks: list[tuple[int, str, float | None]] = []
    for i, line in enumerate(lines):
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.split("|")]
        head = cells[0].lower()
        if "fee" not in head and "total" not in head:
            continue
        lab = row_label(cells[0])
        for name, pat in FEE_ROW.items():
            if re.match(pat, lab):
                marks.append((i, name, figures(cells[1:])))
                break
        else:
            if re.match(TOTAL_ROW, lab):
                marks.append((i, "total", figures(cells[1:])))
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
    # The units note sits above the table, inside the same section, and has to be read
    # before the figures are chosen: whether a value is too small to be a fee depends on
    # the scale the table is stated at.
    head = "\n".join(lines[max(0, marks[0][0] - 12):marks[0][0] + 1])
    unit = UNITS.search(head)
    units = unit.group(1).lower() if unit else "dollars"

    found: dict[str, float] = {}
    for _, name, figs in marks:
        if name in found:
            continue
        val = pick_figure(figs, SCALE[units])
        if val is not None:
            found[name] = val
    return {"found": found, "start": marks[0][0], "end": marks[-1][0],
            "labels": len(marks), "units": units}


def block_ties(b: dict) -> bool:
    """Do the block's own components sum to the total printed in the same block?"""
    parts = [b["found"].get(c) for c in COMPONENTS]
    total = b["found"].get("total")
    if not total or any(p is None for p in parts):
        return False
    return abs(sum(parts) - total) <= max(1.0, 0.005 * total)


def best_fee_block(text: str) -> dict | None:
    """The block naming the most categories, preferring one that adds up.

    Internal consistency is the tie-breaker because a competing block can name all four
    categories and still be the wrong table - one sampled filing had a block in its
    related-party section whose parts came to 2,225,756 against a stated 1,000,000.
    A table whose own arithmetic works is the fee table; the other one is a coincidence.
    """
    blocks = fee_blocks(text)
    if not blocks:
        return None
    return max(blocks, key=lambda b: (len(set(b["found"]) & set(COMPONENTS)),
                                      block_ties(b), "total" in b["found"],
                                      b["labels"]))


SCALE = {"dollars": 1.0, "thousands": 1_000.0, "millions": 1_000_000.0}
# The largest audit fee paid by any company in the world is around $130m. $200m is a
# ceiling no real engagement approaches, so a figure above it is arithmetic, not a fee.
IMPLAUSIBLE_ABOVE = 200_000_000.0
PLAUSIBLE_FLOOR = 10_000.0


def applied_units(found: dict[str, float], units: str) -> tuple[str, bool]:
    """Honour the units note unless applying it produces an impossible fee.

    Hyatt's 2024 proxy heads its table "Type of Fees (in millions)" and then lists
    8,796,173 - which is $8.8m stated in dollars, and $8.8 *trillion* if the note is
    believed. The note is simply wrong, and no amount of care in reading it helps; the
    only defence is knowing what an audit fee can be. So the note is applied unless it
    lands outside the possible range while the unscaled figure sits inside it.
    """
    audit = found.get("audit")
    if audit is None or units == "dollars":
        return units, False
    scaled = audit * SCALE[units]
    if scaled > IMPLAUSIBLE_ABOVE and PLAUSIBLE_FLOOR <= audit <= IMPLAUSIBLE_ABOVE:
        return "dollars", True
    return units, False


def fees(text: str) -> dict:
    b = best_fee_block(text)
    if not b:
        return {}
    units, overridden = applied_units(b["found"], b["units"])
    k = SCALE[units]
    out = {f"{name}_fees": v * k for name, v in b["found"].items()
           if name in COMPONENTS}
    if "total" in b["found"]:
        out["total_fees_stated"] = b["found"]["total"] * k
    out["fee_units"] = units
    out["fee_units_overridden"] = overridden
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

# Requiring the whole cell to be a name rejected most real director rows, because filers
# pack the row into one cell: "Paul J. Bickel III Age 65 Director since 2017",
# "Thomas I. Vehrs (3) Colorado, USA Director", "Michael R. Egeck CEO, Leslie's, Inc.".
# So only the first two tokens are tested, and a stop list keeps phrases that begin with
# capitals - "Compensation Committee of Independent Directors" - from reading as a person.
TOKEN = re.compile(r"^[A-Z][A-Za-z.'’\-]+$")
INITIAL = re.compile(r"^[A-Z]\.$|^\([^)]{1,15}\)$")   # "J." or a ("Jay") nickname
NOT_A_NAME = {
    "name", "names", "director", "directors", "nominee", "nominees", "independent",
    "board", "committee", "committees", "total", "audit", "compensation", "nominating",
    "corporate", "governance", "executive", "chief", "shares", "share", "common",
    "class", "all", "other", "tax", "fee", "fees", "number", "amount", "year", "age",
    "summary", "equity", "option", "options", "award", "awards", "stock", "non",
    "named", "our", "the", "company", "annual", "report", "proposal", "meeting",
    "aggregate", "principal", "lead", "chair", "chairman", "position", "occupation",
    "percent", "percentage", "fiscal", "period", "title", "age(1)", "beneficial",
}
POSITIVE_INDEP = re.compile(r"^(yes|independent)$", re.IGNORECASE)
MIN_DIRECTORS = 4       # below this a match is usually a committee or officer table


def looks_like_person(cell: str) -> bool:
    parts = cell.replace(",", " ").split()
    if len(parts) < 2:
        return False
    first, second = parts[0], parts[1]
    if first.lower() in NOT_A_NAME or second.lower().strip(".") in NOT_A_NAME:
        return False
    return bool(TOKEN.match(first)) and bool(TOKEN.match(second) or INITIAL.match(second))


def director_table(text: str) -> dict:
    """Count directors and those marked independent, from a header-anchored table.

    The independence count is only taken when the header actually carries an
    independence column. Without that guard a committee-membership bullet would read as
    an independence mark, and a committee matrix is the table most likely to sit here.
    """
    lines = text.split("\n")
    best: dict = {}
    for i, line in enumerate(lines):
        if "|" not in line:
            continue
        low = line.lower()
        # Substring test before the word-boundary regex, for the same reason as in
        # fee_blocks: a superset of what can match, at a fraction of the cost, over
        # every table row of every section.
        if ("name" not in low and "director" not in low and "nominee" not in low):
            continue
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
            if not looks_like_person(cells[0]):
                if listed:
                    break
                continue
            listed += 1
            if has_indep_col and any(POSITIVE_INDEP.match(c) for c in cells[1:]):
                marked += 1
        if listed >= MIN_DIRECTORS and listed > best.get("directors_listed", 0):
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
BOOLEAN = [*FLAG_SECTIONS, "fee_units_overridden"]
TEXT = ["cik", "adsh", "form", "filing_date", "period_of_report", "fee_units",
        "fee_source_section", "independence_statement"]


def metrics_for_filing(sections: dict[str, str], meta: dict) -> dict:
    """One row of the mart, from one filing's sections."""
    row: dict = dict(meta)
    row["sections_found"] = len(sections)
    for flag, name in FLAG_SECTIONS.items():
        row[flag] = name in sections

    # Fees: search every section, because the table is often not under a fee heading -
    # it lands under "Ratification of Appointment" as often as not. The same preference
    # for a block that adds up applies across sections as within one.
    best: tuple[tuple, str] | None = None
    for name, body in sections.items():
        b = best_fee_block(body)
        if not b:
            continue
        score = (len(set(b["found"]) & set(COMPONENTS)), block_ties(b),
                 "total" in b["found"])
        if best is None or score > best[0]:
            best = (score, name)
    if best:
        row.update(fees(sections[best[1]]))
        row["fee_source_section"] = best[1]

    # Directors: every section, for the same reason as the fees. Restricting this to the
    # nominee and governance sections found a table in 18% of filings where 62% have one,
    # because which heading won the table's part of the document varies by filer.
    for body in sections.values():
        found = director_table(body)
        if found.get("directors_listed", 0) > (row.get("directors_listed") or 0):
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


def build(con, rebuild: bool = False) -> int:
    """Read the section view a filing year at a time, one mart row per proxy filing.

    Three shapes were tried and the first two were wrong in instructive ways. A query per
    filing meant 4,859 round trips for a single year, against a daily compute allowance
    this project has already exhausted once. Batching by accession cut the round trips
    but not the work, because the view is parquet in the lake and every
    `WHERE adsh IN (…)` rescans all of it.

    Reading everything in one grouped, ordered statement then ran MotherDuck out of
    memory outright: grouping and sorting both have to materialise the `text` column,
    1.7GB across 488,146 sections, on an instance with about a gigabyte. The lesson is
    that the expensive work belongs on the runner, which has memory to spare, and the
    warehouse should be asked only to hand over rows.

    So each year is read unsorted and ungrouped, assembled here, and scored. Filings
    already in the mart are skipped and rows commit in batches, so an interrupted run
    resumes instead of restarting.
    """
    con.execute("CREATE SCHEMA IF NOT EXISTS marts")
    cols = ", ".join(
        f"{c} " + ("VARCHAR" if c in TEXT else "BOOLEAN" if c in BOOLEAN
                   else "BIGINT" if c in INTEGER else "DOUBLE")
        for c in COLUMNS)
    if rebuild:
        con.execute("DROP TABLE IF EXISTS marts.governance_metrics")
    con.execute(f"CREATE TABLE IF NOT EXISTS marts.governance_metrics ({cols})")

    # Score only what is not scored yet. At roughly 0.4 seconds a filing this is a
    # four-hour job over eight years, so a run that is interrupted has to be resumable
    # rather than start again - the same reason the fetch skips filings already in the
    # lake. Use --rebuild when the extractor itself changes, since then every existing
    # row is stale by definition.
    # Keyed on (cik, adsh), not adsh alone: co-registrants share an accession number, so
    # skipping by accession would drop a parent because its subsidiary was scored.
    done = {(str(a), str(b)) for a, b in con.execute(
        "SELECT DISTINCT cik, adsh FROM marts.governance_metrics").fetchall()}
    if done:
        print(f"{len(done):,} filings already scored, skipping them")

    insert = (f"INSERT INTO marts.governance_metrics "
              f"VALUES ({', '.join(['?'] * len(COLUMNS))})")
    buf: list[dict] = []
    scored = 0

    def commit() -> None:
        """Write the buffer out, so an interrupted run keeps what it has done."""
        if buf:
            con.executemany(insert, [[r.get(c) for c in COLUMNS] for r in buf])
            buf.clear()

    # One filing year per query, and no sort or grouping asked of the warehouse.
    #
    # Reading the lot in one statement, grouped by (cik, adsh, section) and ordered by
    # accession, ran MotherDuck out of memory: both operators have to materialise the
    # `text` column, which is 1.7GB across 488,146 sections, and the free instance has
    # about a gigabyte. The grouping was redundant anyway - invariant 3 proves that
    # triple is already unique - and the ordering existed only so filings arrived
    # together, which is cheaper to arrange here than there.
    #
    # So each year is read unsorted and assembled in the runner, which has the memory to
    # spare: a year of unscored sections is roughly 230MB. Years also give the run a
    # coarse resume point on top of the per-filing one.
    years = [r[0] for r in con.execute(
        "SELECT DISTINCT filing_year FROM quali.proxy_sections "
        "ORDER BY filing_year").fetchall()]
    print(f"filing years present: {', '.join(str(y) for y in years)}")

    for year in years:
        reader = con.execute("""
            SELECT cik, adsh, form, filing_date, period_of_report, section, text
            FROM quali.proxy_sections
            WHERE filing_year = ?""", [year]).fetch_record_batch(5_000)
        secs_by_filing: dict[tuple[str, str], dict[str, str]] = {}
        meta_by_filing: dict[tuple[str, str], dict] = {}
        for batch in reader:
            d = batch.to_pydict()
            for i in range(batch.num_rows):
                key = (str(d["cik"][i]), str(d["adsh"][i]))
                if key in done:
                    continue
                if key not in meta_by_filing:
                    meta_by_filing[key] = {
                        "cik": key[0], "adsh": key[1], "form": d["form"][i],
                        "filing_date": str(d["filing_date"][i]),
                        "period_of_report": str(d["period_of_report"][i])}
                secs_by_filing.setdefault(key, {})[d["section"][i]] = d["text"][i]

        for key, secs in secs_by_filing.items():
            buf.append(metrics_for_filing(secs, meta_by_filing[key]))
            scored += 1
            if len(buf) >= 2000:
                commit()
        commit()
        print(f"  {year}: scored {len(secs_by_filing):,}  (running total {scored:,})")
        secs_by_filing.clear()
        meta_by_filing.clear()

    n = con.execute("SELECT count(*) FROM marts.governance_metrics").fetchone()[0]
    print(f"{scored:,} filings scored this run, {len(done):,} already present")
    print(f"table marts.governance_metrics  {n:,} rows")
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true",
                    help="drop and rescore everything — use when the extractor changed")
    args = ap.parse_args()
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    build(con, rebuild=args.rebuild)


if __name__ == "__main__":
    main()
