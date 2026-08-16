"""Tracker G3, fourth probe — why the director table is being missed.

The metric extractor found a director table in 18% of sampled proxies and reported a
median board of four, which is wrong on its face: a listed company's board runs to eight
or eleven. So the parser is both missing tables and, where it fires, counting something
that is not a board. A median of four is the useful kind of wrong - visible - and the
invariant suite would reject it, which is the check earning its place.

Rather than adjust the name pattern and hope, this dumps what the parser matched, what it
rejected and why. Two hypotheses are worth separating: that director tables exist and the
row test is too strict, or that many proxies present directors as biography blocks with
the name as a heading and no table at all, in which case there is nothing to find and the
metric should say so rather than guess.

The same run asks the smaller open question about fees: 20 of 60 filings contained a fee
label but yielded no block, so either those labels are not in table rows or the rows carry
only one category.
"""
from __future__ import annotations

import asyncio
import re

import duckdb

from credit_workbench.common.config import motherduck_token
from credit_workbench.common.html_text import to_rows
from credit_workbench.ingest.proxy_sections import split_sections
from credit_workbench.transform.governance import (DIR_HEADER_KEYS, FEE_ROW, NAME_RE,
                                                   fee_blocks, row_label)
from credit_workbench.warehouse.diag_governance import fetch_all

SAMPLE = 60


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
        WHERE rn <= 10
        ORDER BY rn
        LIMIT {SAMPLE}""").fetchall()
    print(f"### Fetching {len(rows)} proxies")
    docs = asyncio.run(fetch_all(rows))
    print(f"  fetched {len(docs)}")
    if not docs:
        return
    for d in docs:
        d["r"] = to_rows(d.pop("_html"))
        d["secs"] = split_sections(d["r"])

    # ---- 1. how many candidate header rows are there, and what rejects them?
    print("\n### 1. Header rows: found, and why candidates are rejected")
    stats = {"has name-ish row": 0, "and >=2 attribute keys": 0,
             "and >=4 person rows follow": 0, "no name-ish row at all": 0}
    for d in docs:
        text = d["r"]
        namey = attr = personed = 0
        for i, line in enumerate(text.split("\n")):
            if "|" not in line:
                continue
            low = line.lower()
            if not re.search(r"\bname\b|\bdirector\b|\bnominee\b", low):
                continue
            namey += 1
            if sum(1 for k in DIR_HEADER_KEYS if re.search(k, low)) >= 2:
                attr += 1
                lines = text.split("\n")
                got = sum(1 for nxt in lines[i + 1:i + 40]
                          if "|" in nxt and NAME_RE.match(nxt.split("|")[0].strip()))
                if got >= 4:
                    personed += 1
        stats["has name-ish row"] += namey > 0
        stats["and >=2 attribute keys"] += attr > 0
        stats["and >=4 person rows follow"] += personed > 0
        stats["no name-ish row at all"] += namey == 0
    for k, v in stats.items():
        print(f"  {k:<32} {v} of {len(docs)} ({100*v/len(docs):.0f}%)")

    # ---- 2. the header rows we accept, and the rows immediately under them
    print("\n### 2. Accepted header rows and the first rows beneath "
          "(does the name test hold?)")
    shown = 0
    for d in docs:
        lines = d["r"].split("\n")
        for i, line in enumerate(lines):
            if "|" not in line or shown >= 5:
                continue
            low = line.lower()
            if not re.search(r"\bname\b|\bnominee\b", low):
                continue
            if sum(1 for k in DIR_HEADER_KEYS if re.search(k, low)) < 2:
                continue
            shown += 1
            print(f"\n    --- {d['adsh']}")
            print(f"      HEADER | {line.strip()[:104]}")
            for nxt in lines[i + 1:i + 9]:
                if "|" not in nxt:
                    continue
                first = nxt.split("|")[0].strip()
                ok = "MATCH  " if NAME_RE.match(first) else "reject "
                print(f"      {ok}| {nxt.strip()[:100]}")
            break

    # ---- 3. what do the rejected first cells look like?
    print("\n### 3. First cells rejected by the name test, near a director header")
    rejected: list[str] = []
    for d in docs:
        lines = d["r"].split("\n")
        for i, line in enumerate(lines):
            low = line.lower()
            if "|" not in line or not re.search(r"\bname\b|\bnominee\b", low):
                continue
            if sum(1 for k in DIR_HEADER_KEYS if re.search(k, low)) < 2:
                continue
            for nxt in lines[i + 1:i + 25]:
                if "|" not in nxt:
                    continue
                first = nxt.split("|")[0].strip()
                if first and not NAME_RE.match(first) and len(first) < 60:
                    rejected.append(first)
            break
    for r in rejected[:30]:
        print(f"    {r!r}")

    # ---- 4. do proxies name directors in prose instead?
    print("\n### 4. Where there is no table, are directors named as headings?")
    for d in docs[:4]:
        secs = d["secs"]
        body = secs.get("nominees") or secs.get("governance") or ""
        if not body:
            continue
        # A biography block heading: a short line of a plausible personal name.
        names = [ln.strip() for ln in body.split("\n")
                 if "|" not in ln and NAME_RE.match(ln.strip())]
        print(f"    {d['adsh']}  name-like headings in nominees/governance: "
              f"{len(names)}  {names[:6]}")

    # ---- 5. the fee misses
    print("\n### 5. Fee labels that yielded no block")
    for d in docs:
        if any(fee_blocks(t) for t in d["secs"].values()):
            continue
        labelled = []
        for name, body in d["secs"].items():
            for ln in body.split("\n"):
                lab = row_label(ln.split("|")[0])
                if any(re.match(p, lab) for p in FEE_ROW.values()):
                    labelled.append((name, "|" in ln, ln.strip()[:88]))
        if not labelled:
            continue
        print(f"\n    --- {d['adsh']}  {len(labelled)} labelled lines")
        for sec, has_row, ln in labelled[:6]:
            print(f"      [{sec:<14} row={has_row!s:<5}] {ln}")


if __name__ == "__main__":
    main()
