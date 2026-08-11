"""Tracker G1 — the narrative sections of the 10-K.

Everything until now came from XBRL. Risk factors, MD&A, the legal proceedings note and
the controls discussion are not tagged: they live in the filing's HTML, so nothing in
the warehouse touches them today. This fetches each annual report and splits it into
its numbered Items.

Two things make the split harder than it looks.

The table of contents repeats every heading, so the first "Item 1A." in a document is
almost never the real one. The rule used here is that the right occurrence is the one
with the largest gap to the *next* heading of any item: contents entries sit tens of
characters apart, body headings thousands. An earlier version of this measured the gap
between occurrences of the same item and produced sections that overlapped each other
several times over.

Cross-references ("as discussed in Item 1A") also match the heading pattern. Cutting
each section at the next *chosen* heading rather than the next raw match steps over
them.

Item 8 is skipped deliberately: it is the financial statements, which we already hold
completely and in structured form as 222m XBRL facts. Storing the HTML rendering again
would roughly double the size of this dataset to restate what we have.

One job, fetching asynchronously at just under SEC's ten-requests-a-second limit, is
much cheaper in runner minutes than spreading the same work over parallel jobs that
each have to throttle themselves down.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory

import duckdb
import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from credit_workbench.common import r2 as r2util
from credit_workbench.common.config import R2, motherduck_token, sec_user_agent

ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
PREFIX = "parquet/sec/narrative/sections"
REQUESTS_PER_SECOND = 9.0
IN_FLIGHT = 8
CHUNK = 400

ITEM_TITLES = {
    "1": "Business",
    "1A": "Risk Factors",
    "1B": "Unresolved Staff Comments",
    "1C": "Cybersecurity",
    "2": "Properties",
    "3": "Legal Proceedings",
    "4": "Mine Safety Disclosures",
    "5": "Market for Registrant's Common Equity",
    "6": "Selected Financial Data",
    "7": "Management's Discussion and Analysis",
    "7A": "Quantitative and Qualitative Disclosures About Market Risk",
    "9": "Changes in and Disagreements with Accountants",
    "9A": "Controls and Procedures",
    "9B": "Other Information",
    "10": "Directors, Executive Officers and Corporate Governance",
    "11": "Executive Compensation",
    "12": "Security Ownership",
    "13": "Certain Relationships and Related Transactions",
    "14": "Principal Accountant Fees and Services",
    "15": "Exhibits and Financial Statement Schedules",
}
# The financial statements are often placed after Item 15, so without this the last
# section runs to the end of the document and swallows the F-pages - content we already
# hold as XBRL. Treated as a boundary in the same way as Item 8.
FINANCIALS_RE = re.compile(
    r"^\s*(report of independent registered public accounting firm"
    r"|index to (the )?consolidated financial statements"
    r"|index to financial statements"
    r"|consolidated balance sheets?\s*$)",
    re.IGNORECASE | re.MULTILINE)

# Longest first so "1A" is not eaten by "1". Item 8 is matched only as a boundary.
ITEM_ALTERNATION = "1A|1B|1C|7A|9A|9B|10|11|12|13|14|15|1|2|3|4|5|6|7|8|9"
ITEM_RE = re.compile(
    rf"^\s*item\s*({ITEM_ALTERNATION})\s*[.:)\-–—]?\s",
    re.IGNORECASE | re.MULTILINE)


class TextExtractor(HTMLParser):
    """Strip tags, drop script/style, keep block boundaries as newlines."""

    BLOCK = {"p", "div", "br", "tr", "td", "th", "table", "li",
             "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)

    def text(self) -> str:
        out = "".join(self.parts).replace("\xa0", " ")
        out = re.sub(r"[ \t]+", " ", out)
        return re.sub(r"\n\s*\n+", "\n", out).strip()


def to_text(html: str) -> str:
    parser = TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001  malformed markup: keep whatever parsed
        pass
    return parser.text()


def split_sections(text: str) -> dict[str, str]:
    """Return {item: section text}, choosing body headings over contents entries."""
    matches = [(m.start(), m.group(1).upper()) for m in ITEM_RE.finditer(text)]
    if not matches:
        return {}
    # A pseudo-heading so a section cannot run past the start of the F-pages.
    fin = FINANCIALS_RE.search(text)
    if fin:
        matches.append((fin.start(), "__FIN__"))
        matches.sort()

    # The real heading is the occurrence followed by the most text before the next
    # heading of any item. Contents entries are a few tens of characters apart.
    best: dict[str, tuple[int, int]] = {}
    for i, (pos, item) in enumerate(matches):
        nxt = matches[i + 1][0] if i + 1 < len(matches) else len(text)
        gap = nxt - pos
        if item not in best or gap > best[item][1]:
            best[item] = (pos, gap)

    # Cut at the next chosen heading, not the next raw match, so a cross-reference
    # inside the body does not truncate the section.
    chosen = sorted((pos, item) for item, (pos, _) in best.items())
    out: dict[str, str] = {}
    for i, (pos, item) in enumerate(chosen):
        end = chosen[i + 1][0] if i + 1 < len(chosen) else len(text)
        if item in ITEM_TITLES:          # item 8 participates only as a boundary
            body = text[pos:end].strip()
            if len(body) >= 200:         # a stub is a mis-hit, not a section
                out[item] = body
    return out


class RateLimiter:
    """Space requests so the whole job stays under SEC's fair-access limit."""

    def __init__(self, per_second: float) -> None:
        self.tick = 1.0 / per_second
        self.next_at = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_at - now)
            self.next_at = max(now, self.next_at) + self.tick
        if delay:
            await asyncio.sleep(delay)


async def fetch_one(client: httpx.AsyncClient, limiter: RateLimiter,
                    sem: asyncio.Semaphore, row: tuple) -> dict | None:
    cik, adsh, doc, form, filed, period = row
    url = f"{ARCHIVES}/{int(cik)}/{str(adsh).replace('-', '')}/{doc}"
    async with sem:
        for attempt in range(3):
            await limiter.wait()
            try:
                resp = await client.get(url)
            except Exception:  # noqa: BLE001
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 200:
                break
            if resp.status_code in (403, 429, 500, 502, 503):
                await asyncio.sleep(3 * (attempt + 1))
                continue
            return None            # 404 and friends: nothing to retry for
        else:
            return None
        if resp.status_code != 200:
            return None

    text = to_text(resp.text)
    sections = split_sections(text)
    rows = [[str(cik), str(adsh), form, str(filed), str(period), item,
             ITEM_TITLES[item], len(body), body]
            for item, body in sections.items()]
    return {"adsh": str(adsh), "text": text, "rows": rows}


FIELDS = ["cik", "adsh", "form", "filing_date", "period_of_report",
          "item", "item_title", "char_len", "text"]


async def run_chunk(rows: list[tuple], keep_text: bool = False) -> list:
    headers = {"User-Agent": sec_user_agent(), "Accept-Encoding": "gzip, deflate"}
    limiter = RateLimiter(REQUESTS_PER_SECOND)
    sem = asyncio.Semaphore(IN_FLIGHT)
    limits = httpx.Limits(max_connections=IN_FLIGHT + 2)
    async with httpx.AsyncClient(headers=headers, timeout=120, limits=limits,
                                 follow_redirects=True) as client:
        results = await asyncio.gather(
            *(fetch_one(client, limiter, sem, r) for r in rows))
    docs = [d for d in results if d]
    if keep_text:
        return docs
    return [row for d in docs for row in d["rows"]]


def dry_run(filings: list[tuple]) -> None:
    """Report how well the split worked, without writing anything.

    The checks are the ones that catch a splitter silently returning rubbish: how often
    each item is found at all, how long the sections are, and whether the text reads
    like the item it claims to be - risk factors that never say "risk", or an MD&A that
    never says "compared", mean the heading matched the contents page.
    """
    docs = asyncio.run(run_chunk(filings, keep_text=True))
    rows = [r for d in docs for r in d["rows"]]
    if not rows:
        print("no sections extracted at all")
        return

    by_filing: dict[str, dict[str, int]] = {}
    lengths: dict[str, list[int]] = {}
    for _cik, adsh, _form, _filed, _period, item, _title, n, _text in rows:
        by_filing.setdefault(adsh, {})[item] = n
        lengths.setdefault(item, []).append(n)

    print(f"\n{len(by_filing)} filings yielded sections "
          f"({len(filings) - len(by_filing)} yielded none)")
    print(f"\n{'item':<5} {'found':>6} {'% of filings':>13} {'median chars':>13} "
          f"{'max chars':>11}")
    for item in ITEM_TITLES:
        got = lengths.get(item, [])
        if not got:
            print(f"{item:<5} {0:>6} {0:>12.0f}% {'-':>13} {'-':>11}")
            continue
        got.sort()
        print(f"{item:<5} {len(got):>6} {100*len(got)/len(by_filing):>12.0f}% "
              f"{got[len(got)//2]:>13,} {got[-1]:>11,}")

    print("\nDoes the text read like the item it claims to be?")
    probes = {"1A": "risk", "7": "compared", "3": "legal", "1": "business",
              "9A": "internal control", "7A": "market risk"}
    for item, word in probes.items():
        texts = [r[8] for r in rows if r[5] == item]
        if not texts:
            continue
        hits = sum(1 for t in texts if word in t.lower())
        print(f"  item {item:<3} contains '{word}': {100*hits/len(texts):5.0f}% "
              f"of {len(texts)} sections")

    worst = sorted(rows, key=lambda r: r[7])[:3]
    print("\nShortest sections extracted (mis-hits show up here):")
    for r in worst:
        print(f"  item {r[5]:<3} {r[7]:>6,} chars  {r[1]}  {r[8][:90]!r}")

    # Risk factors were found in well under every filing. Absence is expected for
    # smaller reporting companies, which are exempt from Item 1A; a parse failure is
    # not. Separate the two rather than assume the innocent explanation.
    # Measure what actually decides the outcome: how many Item 1A headings the document
    # contains and how much text follows the best of them. Reporting the context of the
    # *first* heading told me nothing, because the first is nearly always the contents
    # page - a section is dropped only when the best candidate is too short to keep.
    missing = [d for d in docs if "1A" not in {r[5] for r in d["rows"]}]
    print(f"\nItem 1A absent in {len(missing)} of {len(docs)} filings - why?")
    buckets = {"no heading at all": 0, "best candidate under 200 chars": 0,
               "candidate long enough - a real bug": 0}
    shown = 0
    for d in missing:
        text = d["text"]
        all_marks = [m.start() for m in ITEM_RE.finditer(text)]
        mine = [m.start() for m in
                re.finditer(r"^\s*item\s*1a\b", text, re.IGNORECASE | re.MULTILINE)]
        if not mine:
            buckets["no heading at all"] += 1
            continue
        best = 0
        for pos in mine:
            after = [p for p in all_marks if p > pos]
            best = max(best, (min(after) if after else len(text)) - pos)
        if best < 200:
            buckets["best candidate under 200 chars"] += 1
            if shown < 3:
                shown += 1
                print(f"    short  {d['adsh']}  best candidate {best} chars: "
                      f"{text[mine[0]:mine[0] + 100]!r}")
        else:
            buckets["candidate long enough - a real bug"] += 1
            print(f"    BUG    {d['adsh']}  best candidate {best} chars")
    for label, n in buckets.items():
        print(f"  {label:<34} {n}")

    print("\nItem 15 length (should be an exhibit list, not the F-pages):")
    fifteen = sorted(r[7] for r in rows if r[5] == "15")
    if fifteen:
        print(f"  n={len(fifteen)}  median={fifteen[len(fifteen)//2]:,}  "
              f"p90={fifteen[int(len(fifteen)*0.9)]:,}  max={fifteen[-1]:,}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", required=True, help="e.g. 2019-2026")
    ap.add_argument("--forms", default="10-K")
    ap.add_argument("--limit", type=int, default=0, help="cap filings, for a trial run")
    ap.add_argument("--dry-run", action="store_true",
                    help="extract and report quality, write nothing")
    args = ap.parse_args()
    lo, _, hi = args.years.partition("-")
    lo, hi = int(lo), int(hi or lo)
    forms = tuple(f.strip() for f in args.forms.split(","))

    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    form_list = ", ".join(f"'{f}'" for f in forms)
    filings = con.execute(f"""
        SELECT cik, accession_number, primary_document, form,
               TRY_CAST(filing_date AS DATE) AS filed, report_date
        FROM ref.filing_index
        WHERE form IN ({form_list})
          AND primary_document IS NOT NULL AND primary_document <> ''
          AND year(TRY_CAST(filing_date AS DATE)) BETWEEN {lo} AND {hi}
        ORDER BY filed, accession_number
        {f'LIMIT {args.limit}' if args.limit else ''}""").fetchall()
    print(f"{len(filings):,} filings to fetch for {args.years} ({', '.join(forms)})")
    if not filings:
        return

    if args.dry_run:
        dry_run(filings)
        return

    cfg = R2.from_env()
    s3 = r2util.client(cfg)
    started = time.time()
    done_rows = 0

    with TemporaryDirectory() as tmp:
        for start in range(0, len(filings), CHUNK):
            chunk = filings[start:start + CHUNK]
            index = start // CHUNK
            key = f"{PREFIX}/filing_year={lo}_{hi}/sections_{index:05d}.parquet"
            if r2util.exists(s3, cfg.bucket, key):
                print(f"  chunk {index:>4}  already present, skipping")
                continue

            rows = asyncio.run(run_chunk(chunk))
            if not rows:
                print(f"  chunk {index:>4}  no sections extracted from "
                      f"{len(chunk)} filings")
                continue

            path = Path(tmp) / f"sections_{index:05d}.parquet"
            cols = list(zip(*rows))
            table = pa.table({
                name: pa.array(col, type=pa.int64() if name == "char_len"
                               else pa.string())
                for name, col in zip(FIELDS, cols)})
            pq.write_table(table, path, compression="zstd")
            size = r2util.upload(s3, path, cfg.bucket, key)
            path.unlink()

            done_rows += len(rows)
            elapsed = time.time() - started
            filings_done = start + len(chunk)
            rate = filings_done / max(elapsed, 1)
            remaining = (len(filings) - filings_done) / max(rate, 0.01) / 60
            print(f"  chunk {index:>4}  {len(chunk)} filings -> {len(rows):>5,} sections"
                  f"  {size/1e6:5.1f} MB  {rate:4.1f} filings/s"
                  f"  ~{remaining:.0f} min left")

    print(f"DONE {args.years}: {done_rows:,} sections from {len(filings):,} filings "
          f"in {(time.time() - started)/60:.0f} min")


if __name__ == "__main__":
    main()
