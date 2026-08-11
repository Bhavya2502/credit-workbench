"""Credit agreements and indentures — the documents that carry covenant terms.

A bank's credit analysis turns on what the loan documents actually permit: the financial
covenant levels and their definitions, the negative covenants, the baskets, the events
of default. None of that is in XBRL. `quali.note_signals` can tell you a covenant waiver
was *mentioned*, and `marts.credit_events` catches an acceleration after the fact, but
neither holds a single covenant term.

The agreements are filed as exhibits. EX-10 is the material contract - a credit
agreement, term loan, or amendment; EX-4 is the instrument defining security holders'
rights, which is where indentures live. They are attached to the 8-K that reports the
deal (item 1.01, entry into a material definitive agreement) and re-listed in the annual
report.

Identifying them is awkward: the `type` field in a filing's index.json holds the name of
the icon the web page draws, literally "text.gif", not "EX-10.1". The document *names*
do carry it, in a handful of conventions - ex10d1, ex-10_1, exhibit101 - so the filename
is what gets matched, and the size filter does the rest. A real credit agreement runs to
hundreds of kilobytes; a five-kilobyte EX-10 is a director's appointment letter.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import duckdb
import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from credit_workbench.common import r2 as r2util
from credit_workbench.common.config import R2, motherduck_token, sec_user_agent
from credit_workbench.ingest.filing_sections import (
    ARCHIVES, IN_FLIGHT, REQUESTS_PER_SECOND, RateLimiter, to_text,
)

PREFIX = "parquet/sec/narrative/exhibits"
CHUNK = 300
# Below this an EX-10 is an appointment letter or a short amendment, not an agreement
# worth parsing for terms. Recorded in the manifest either way.
MIN_AGREEMENT_BYTES = 60_000

# ex10d1.htm, ex-10_1.htm, exhibit101.htm, a10q3312024ex101.htm ...
EXHIBIT_RE = re.compile(r"ex(?:hibit)?[-_]?(\d{1,2})(?:[._dx-]?(\d{1,3}))?\D*$",
                        re.IGNORECASE)

# What kind of document is it? Judged from the opening text, which states what the
# agreement is; filename conventions are too thin to carry this.
# Order matters. An amendment carries only the terms that changed, so it must be
# recognised before the agreement patterns it also mentions - "First Amendment to Credit
# Agreement" is not a credit agreement. An *amended and restated* agreement is the
# opposite: a complete document. The two are distinguished by the word itself,
# "amendment" versus "amended", so the amendment pattern below cannot catch a restatement.
DOC_KINDS = [
    ("amendment", r"\b(amendment\s+no\.?\s*\d+|(first|second|third|fourth|fifth|sixth|"
                  r"seventh|eighth|ninth|tenth|eleventh|twelfth)\s+amendment|"
                  r"amendment\s+to)\b"),
    ("credit_agreement", r"\b(credit agreement|loan and security agreement|"
                         r"revolving credit|term loan agreement|financing agreement)\b"),
    ("indenture", r"\bindenture\b"),
    ("note_purchase", r"\bnote purchase agreement\b"),
    ("security_agreement", r"\b(security agreement|pledge agreement|"
                           r"guarantee and collateral)\b"),
    ("guarantee", r"\bguarant(y|ee) agreement\b"),
]
DOC_KIND_RES = [(kind, re.compile(pat, re.IGNORECASE)) for kind, pat in DOC_KINDS]


def classify(text: str) -> str:
    head = text[:6000]
    for kind, rx in DOC_KIND_RES:
        if rx.search(head):
            return kind
    return "other"


def exhibit_number(filename: str) -> str | None:
    """Return '10.1' style exhibit number from a document filename, if it looks like one."""
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    m = EXHIBIT_RE.search(stem)
    if not m:
        return None
    major, minor = m.group(1), m.group(2)
    if major not in ("4", "10"):        # only the debt-document families
        return None
    return f"{major}.{minor}" if minor else major


async def enumerate_filing(client: httpx.AsyncClient, limiter: RateLimiter,
                           sem: asyncio.Semaphore, row: tuple) -> list[dict]:
    """List the EX-4 / EX-10 documents inside one filing."""
    cik, adsh, form, filed, items = row
    nodash = str(adsh).replace("-", "")
    async with sem:
        await limiter.wait()
        try:
            resp = await client.get(f"{ARCHIVES}/{int(cik)}/{nodash}/index.json")
        except Exception:  # noqa: BLE001
            return []
        if resp.status_code != 200:
            return []
        try:
            entries = resp.json()["directory"]["item"]
        except Exception:  # noqa: BLE001
            return []

    out = []
    for entry in entries:
        name = entry.get("name") or ""
        if not name.lower().endswith((".htm", ".html", ".txt")):
            continue
        number = exhibit_number(name)
        if not number:
            continue
        out.append({"cik": str(cik), "adsh": str(adsh), "form": form,
                    "filing_date": str(filed), "items": str(items or ""),
                    "exhibit_number": number, "file_name": name,
                    "size_bytes": int(entry.get("size") or 0),
                    "url": f"{ARCHIVES}/{int(cik)}/{nodash}/{name}"})
    return out


async def fetch_exhibit(client: httpx.AsyncClient, limiter: RateLimiter,
                        sem: asyncio.Semaphore, doc: dict) -> dict | None:
    async with sem:
        for attempt in range(3):
            await limiter.wait()
            try:
                resp = await client.get(doc["url"])
            except Exception:  # noqa: BLE001
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 200:
                break
            if resp.status_code in (403, 429, 500, 502, 503):
                await asyncio.sleep(3 * (attempt + 1))
                continue
            return None
        else:
            return None
    text = to_text(resp.text)
    if len(text) < 2000:
        return None
    return {**doc, "doc_kind": classify(text), "char_len": len(text), "text": text}


FIELDS = ["cik", "adsh", "form", "filing_date", "items", "exhibit_number",
          "file_name", "size_bytes", "doc_kind", "char_len", "text"]


async def run_chunk(filings: list[tuple], fetch_bodies: bool = True) -> tuple[list, list]:
    headers = {"User-Agent": sec_user_agent(), "Accept-Encoding": "gzip, deflate"}
    limiter = RateLimiter(REQUESTS_PER_SECOND)
    sem = asyncio.Semaphore(IN_FLIGHT)
    limits = httpx.Limits(max_connections=IN_FLIGHT + 2)
    async with httpx.AsyncClient(headers=headers, timeout=180, limits=limits,
                                 follow_redirects=True) as client:
        listed = await asyncio.gather(
            *(enumerate_filing(client, limiter, sem, f) for f in filings))
        manifest = [d for group in listed for d in group]
        if not fetch_bodies:
            return manifest, []
        wanted = [d for d in manifest if d["size_bytes"] >= MIN_AGREEMENT_BYTES]
        bodies = await asyncio.gather(
            *(fetch_exhibit(client, limiter, sem, d) for d in wanted))
    return manifest, [b for b in bodies if b]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-all-agreements", action="store_true",
                    help="widen from item 2.03 to any material agreement (1.01)")
    args = ap.parse_args()
    lo, _, hi = args.years.partition("-")
    lo, hi = int(lo), int(hi or lo)

    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    # Item 2.03 is "creation of a direct financial obligation" - the debt-specific code.
    # Item 1.01 alone is any material agreement at all, and a sweep of it turned up
    # mostly SPAC trust and escrow agreements. Requiring 2.03 targets borrowings; 1.01
    # is kept only alongside it, since a credit agreement is usually reported as both.
    where = ("form LIKE '8-K%' AND items LIKE '%2.03%'"
             if not args.include_all_agreements else
             "form LIKE '8-K%' AND (items LIKE '%2.03%' OR items LIKE '%1.01%')")
    # Hash order, not filing order: taking the earliest filings of a year gave a
    # January sample that was almost entirely blank-cheque companies.
    order = "hash(accession_number)" if args.limit else "filed, accession_number"
    filings = con.execute(f"""
        SELECT cik, accession_number, form, TRY_CAST(filing_date AS DATE) AS filed, items
        FROM ref.filing_index
        WHERE {where}
          AND year(TRY_CAST(filing_date AS DATE)) BETWEEN {lo} AND {hi}
        ORDER BY {order}
        {f'LIMIT {args.limit}' if args.limit else ''}""").fetchall()
    print(f"{len(filings):,} filings reporting a financial obligation, {args.years}")
    if not filings:
        return

    if args.dry_run:
        manifest, bodies = asyncio.run(run_chunk(filings))
        print(f"\n{len(manifest)} EX-4/EX-10 documents found across {len(filings)} "
              f"filings ({len(manifest)/len(filings):.1f} per filing)")
        sizes = sorted(d["size_bytes"] for d in manifest)
        if sizes:
            print(f"  size KB: median={sizes[len(sizes)//2]/1000:,.0f}  "
                  f"p90={sizes[int(len(sizes)*0.9)]/1000:,.0f}  max={sizes[-1]/1000:,.0f}")
            print(f"  at or above the {MIN_AGREEMENT_BYTES/1000:.0f} KB threshold: "
                  f"{sum(1 for s in sizes if s >= MIN_AGREEMENT_BYTES)}")
        print(f"\n{len(bodies)} documents fetched and classified:")
        kinds: dict[str, list[int]] = {}
        for b in bodies:
            kinds.setdefault(b["doc_kind"], []).append(b["char_len"])
        for kind, lens in sorted(kinds.items(), key=lambda kv: -len(kv[1])):
            lens.sort()
            print(f"  {kind:<20} {len(lens):>4}  median {lens[len(lens)//2]:>9,} chars")
        print("\nDo the agreements actually contain covenant language?")
        for probe in ("leverage ratio", "fixed charge coverage", "events of default",
                      "restricted payment", "permitted indebtedness", "EBITDA"):
            hits = sum(1 for b in bodies if probe.lower() in b["text"].lower())
            print(f"  {probe:<24} {hits:>4} of {len(bodies)} documents")
        for b in bodies[:2]:
            print(f"\n  sample [{b['doc_kind']}] ex-{b['exhibit_number']} "
                  f"{b['char_len']:,} chars  {b['file_name']}")
            print(f"    {b['text'][:200]!r}")
        return

    cfg = R2.from_env()
    s3 = r2util.client(cfg)
    started = time.time()
    total_docs = 0

    with TemporaryDirectory() as tmp:
        for start in range(0, len(filings), CHUNK):
            chunk = filings[start:start + CHUNK]
            index = start // CHUNK
            key = f"{PREFIX}/filing_year={lo}_{hi}/exhibits_{index:05d}.parquet"
            if r2util.exists(s3, cfg.bucket, key):
                print(f"  chunk {index:>4}  already present, skipping")
                continue

            _manifest, bodies = asyncio.run(run_chunk(chunk))
            if not bodies:
                print(f"  chunk {index:>4}  no qualifying exhibits in {len(chunk)} filings")
                continue

            path = Path(tmp) / f"exhibits_{index:05d}.parquet"
            table = pa.table({
                name: pa.array([b[name] for b in bodies],
                               type=pa.int64() if name in ("size_bytes", "char_len")
                               else pa.string())
                for name in FIELDS})
            pq.write_table(table, path, compression="zstd")
            size = r2util.upload(s3, path, cfg.bucket, key)
            path.unlink()

            total_docs += len(bodies)
            elapsed = time.time() - started
            done = start + len(chunk)
            rate = done / max(elapsed, 1)
            print(f"  chunk {index:>4}  {len(chunk)} filings -> {len(bodies):>4} agreements"
                  f"  {size/1e6:5.1f} MB  {rate:4.1f} filings/s"
                  f"  ~{(len(filings)-done)/max(rate,0.01)/60:.0f} min left")

    print(f"DONE {args.years}: {total_docs:,} agreements from {len(filings):,} filings "
          f"in {(time.time()-started)/60:.0f} min")


if __name__ == "__main__":
    main()
