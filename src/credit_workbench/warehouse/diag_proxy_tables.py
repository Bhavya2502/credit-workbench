"""Tracker G3, second probe — does keeping table rows intact make proxies extractable?

The first probe found the auditor fee table in 90% of proxies but a fee label and its
number on the same line in only 35%. The cause is in the converter, not the documents:
`to_text` emits a newline around every `<td>`, so a table row arrives as one cell per
line and the label loses its number. One sampled filing padded every cell with
zero-width spaces, which `to_text` does not strip either, so the label and its figure
ended up nine lines apart.

Everything the Management Risk scorecard wants numerically - the audit and non-audit fee
split, the director table, the summary compensation table - is an HTML *table*. So the
converter is the whole problem, and this compares two of them on the same documents:
the existing one, and one that renders each `<tr>` as a single line.

Two further things get measured because they are what would go wrong later. Whether the
fee components actually sum to the stated total, since that is the invariant the mart
would be checked against and it is worth knowing it holds before writing the check. And
whether the *first* number on a fee row is the current year, because these tables are
almost always two-year comparatives - summing the row would double-count, which is the
join fan-out mistake in a different costume.
"""
from __future__ import annotations

import asyncio
import re

import duckdb

from credit_workbench.common.config import motherduck_token
from credit_workbench.common.html_text import to_rows
from credit_workbench.warehouse.diag_governance import fetch_all

SAMPLE = 40


# ---------------------------------------------------------------- fee extraction

FEE_ROW = {
    "audit": r"^audit\s*fees?\b",
    "audit_related": r"^audit[\s-]related\s*fees?\b",
    "tax": r"^tax\s*fees?\b",
    "other": r"^all\s*other\s*fees?\b",
    "total": r"^total\s*(fees|and)?\b",
}
NUM = re.compile(r"^\(?\$?\s*([\d,]+(?:\.\d+)?)\)?$")
DASH = re.compile(r"^[—–\-]$")


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


def fees_from_rows(text: str) -> dict[str, float]:
    """Read the fee table from row-per-line text.

    Takes the *first* figure on the row: these tables are two-year comparatives and the
    current year is the left-hand column. Summing the row would double-count.
    """
    found: dict[str, float] = {}
    for line in text.split("\n"):
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 2:
            continue
        label = re.sub(r"[\(\[]\s*\d+\s*[\)\]]", "", cells[0]).strip().lower()
        label = re.sub(r"[^a-z\s-]", " ", label).strip()
        for name, pat in FEE_ROW.items():
            if name in found or not re.match(pat, label):
                continue
            for c in cells[1:]:
                v = money(c)
                if v is not None:
                    found[name] = v
                    break
    return found


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    rows = con.execute(f"""
        SELECT cik, accession_number, primary_document
        FROM (
          SELECT cik, accession_number, primary_document,
                 row_number() OVER (
                   PARTITION BY year(TRY_CAST(filing_date AS DATE))
                   ORDER BY hash(accession_number)) AS rn
          FROM ref.filing_index
          WHERE form = 'DEF 14A'
            AND primary_document IS NOT NULL AND primary_document <> ''
            AND year(TRY_CAST(filing_date AS DATE)) BETWEEN 2019 AND 2026)
        WHERE rn <= 7
        ORDER BY rn
        LIMIT {SAMPLE}""").fetchall()
    print(f"### Fetching {len(rows)} proxies (same sample as the first probe)")
    docs = asyncio.run(fetch_all(rows))
    print(f"  fetched {len(docs)}")
    if not docs:
        return

    # Both converters run over the same retained markup, so the comparison cannot be
    # confounded by a different sample.
    for d in docs:
        d["rows"] = to_rows(d.pop("_html"))

    # ---- 1. does the label keep its number?
    print("\n### 1. Fee label and its figure on the SAME line")
    print(f"  {'converter':<22} {'audit fees + number':>21}")
    for name, key in (("to_text (existing)", "text"), ("to_rows (row-per-line)", "rows")):
        n = 0
        for d in docs:
            for ln in d[key].split("\n"):
                if re.search(r"audit\s*fees?", ln, re.I) and re.search(r"[\d,]{4,}", ln):
                    n += 1
                    break
        print(f"  {name:<22} {f'{n} of {len(docs)}  ({100*n/len(docs):.0f}%)':>21}")

    # ---- 2. can the whole quartet be read, and does it tie?
    print("\n### 2. Reading the fee table as numbers")
    parsed = [(d, fees_from_rows(d["rows"])) for d in docs]
    got_audit = [f for _, f in parsed if "audit" in f]
    print(f"  audit fee figure read:        {len(got_audit)} of {len(docs)} "
          f"({100*len(got_audit)/len(docs):.0f}%)")
    for k in ("audit_related", "tax", "other", "total"):
        n = sum(1 for _, f in parsed if k in f)
        print(f"  {k+' figure read:':<29} {n} of {len(docs)} "
              f"({100*n/len(docs):.0f}%)")

    print("\n### 3. Do the components sum to the stated total? "
          "(the invariant the mart would be checked against)")
    ties = off = 0
    for d, f in parsed:
        parts = [f.get(k) for k in ("audit", "audit_related", "tax", "other")]
        if f.get("total") is None or any(p is None for p in parts):
            continue
        s = sum(parts)
        t = f["total"]
        if t and abs(s - t) <= max(1.0, 0.005 * t):
            ties += 1
        else:
            off += 1
            if off <= 6:
                print(f"    OFF  {d['adsh']}  parts={s:,.0f}  total={t:,.0f}  {f}")
    print(f"  ties: {ties}   off: {off}")

    print("\n### 4. Fee magnitudes — are these dollars, or thousands?")
    vals = sorted(f["audit"] for _, f in parsed if f.get("audit"))
    if vals:
        print(f"  n={len(vals)}  min={vals[0]:,.0f}  "
              f"median={vals[len(vals)//2]:,.0f}  max={vals[-1]:,.0f}")
        print("  under 10,000 (probably stated in thousands): "
              f"{sum(1 for v in vals if v < 10_000)}")

    print("\n### 5. A parsed fee table next to its source rows")
    for d, f in parsed[:3]:
        if not f:
            continue
        print(f"\n    --- {d['adsh']}  ->  {f}")
        for ln in d["rows"].split("\n"):
            if re.search(r"(audit|tax|all other|total)\s*(-?related)?\s*fees?", ln, re.I):
                print(f"      | {ln.strip()[:110]}")

    # ---- 6. the director table
    print("\n### 6. Director rows — does a name keep its committee and independence "
          "markers on one line?")
    hits = 0
    for d in docs:
        cand = [ln for ln in d["rows"].split("\n")
                if ln.count("|") >= 2 and re.search(r"\bindependent\b", ln, re.I)]
        if cand:
            hits += 1
    print(f"  documents with a row containing 'independent' and >=3 cells: "
          f"{hits} of {len(docs)} ({100*hits/len(docs):.0f}%)")
    shown = 0
    for d in docs:
        cand = [ln for ln in d["rows"].split("\n")
                if ln.count("|") >= 2 and re.search(r"\bindependent\b", ln, re.I)]
        if not cand or shown >= 3:
            continue
        shown += 1
        print(f"\n    --- {d['adsh']} ---")
        for ln in cand[:6]:
            print(f"      | {ln.strip()[:110]}")

    # ---- 7. how much bigger is the row-preserving text?
    print("\n### 7. Size cost of keeping rows")
    a = sum(len(d["text"]) for d in docs)
    b = sum(len(d["rows"]) for d in docs)
    print(f"  to_text total {a:,}   to_rows total {b:,}   ratio {b/a:.2f}x")


if __name__ == "__main__":
    main()
