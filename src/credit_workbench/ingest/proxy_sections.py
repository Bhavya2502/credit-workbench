"""Tracker G3 — the governance narrative, out of the DEF 14A proxy statement.

The Management Risk scorecard wants board independence, related-party dealings,
compensation structure and control quality. None of it is in XBRL: probing the warehouse
for `AuditorName`, the ICFR attestation flag, the error-correction flag and the Item
402(v) tags returned nothing at all, because `staging.facts_pit` carries numeric
financial-statement facts and these are neither. Nor is it in the 10-K narrative we
already hold - Items 10 through 14 are there, but at a median of 432 to 1,910 characters
they are the "incorporated by reference to our Proxy Statement" stubs, which is the
filing telling us where to go.

So this fetches the proxy itself. 208,038 DEF 14A filings are indexed, 165,078 of them
with a primary document; 6,809 of those filers also have financials, which is the
universe a credit scorecard can actually use.

**Why this cannot reuse the 10-K splitter.** That one keys on `^item\\s*N`, and Schedule
14A imposes no such numbering - proxy headings are prose of the filer's choosing. Read
off 40 sampled filings, no single heading is universal: the most common, "audit committee
report", appears in 28%. What does carry is a *family* of phrasings per section, measured
at 75-92% of documents for the sections that matter. Those families are below, with the
recall each was measured at, so a later reader can tell a thin section from a broken one.

Two properties of the 10-K splitter do transfer, and both are load-bearing. The table of
contents repeats every heading - it appeared in 42% of the sample - so the right
occurrence is the one with the largest gap to the next heading of any section, contents
entries sitting tens of characters apart where body headings sit thousands. And cutting
at the next *chosen* heading rather than the next raw match steps over cross-references.

Text is stored with table rows intact (see `common.html_text`), because the fee table,
the director table and the compensation tables are all HTML tables and the numbers are
only recoverable while a row is still one line. The numeric extraction itself lives in
`transform.governance`, reading this stored text, so the parser can be corrected without
re-fetching tens of thousands of documents.
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
from credit_workbench.common.html_text import to_rows
from credit_workbench.ingest.filing_sections import ARCHIVES, RateLimiter

PREFIX = "parquet/sec/narrative/proxy_sections"
REQUESTS_PER_SECOND = 9.0
IN_FLIGHT = 8
CHUNK = 200          # proxies are ~5x a 10-K in text, so a smaller chunk per parquet
MIN_SECTION = 400    # shorter than this is a contents entry or a cross-reference

# Section families, with the share of 40 sampled proxies in which the phrase appeared as
# a *heading* line. Low numbers are mostly regulatory rather than parse failures:
# smaller reporting companies owe no CD&A or pay ratio, Item 405 is required only when
# there are delinquent filers, and the equity plan table is often in the 10-K instead.
SECTIONS: dict[str, tuple[str, str]] = {
    "governance": ("Corporate Governance", r"corporate governance"),                # 82%
    "independence": ("Director Independence",                                       # 75%
                     r"(director|board)\s+independence"
                     r"|independence\s+of\s+(the\s+)?(our\s+)?(board|directors)"
                     r"|independent\s+directors"),
    "committees": ("Board Committees",                                              # 92%
                   r"committees?\s+of\s+the\s+board|board\s+committees"
                   r"|committee\s+membership|audit\s+committee"),
    "risk_oversight": ("Risk Oversight",                                            # 75%
                       r"risk\s+oversight|board.{0,15}role\s+in\s+risk"
                       r"|oversight\s+of\s+risk"),
    "attendance": ("Board Meetings and Attendance",                                 # 60%
                   r"meetings?\s+of\s+the\s+board|board\s+meetings|attendance\s+at"),
    "nominees": ("Election of Directors",                                           # 88%
                 r"election\s+of\s+directors|nominees\s+for\s+(election\s+as\s+)?"
                 r"director|our\s+director\s+nominees"),
    "related_party": ("Related Person Transactions",                                # 80%
                      r"related[\s-]person\s+transactions"
                      r"|related[\s-]party\s+transactions"
                      r"|certain\s+relationships\s+and\s+related"
                      r"|transactions\s+with\s+related"),
    "audit_fees": ("Principal Accountant Fees",                                     # 82%
                   r"principal\s+accountant\s+fees"
                   r"|audit\s+(and\s+non[\s-]audit\s+)?fees"
                   r"|fees\s+(paid|billed)\s+to"
                   r"|independent\s+registered\s+public\s+accounting\s+firm\s+fees"),
    "audit_report": ("Audit Committee Report",                                      # 75%
                     r"report\s+of\s+the\s+audit\s+committee"
                     r"|audit\s+committee\s+report"),
    "cda": ("Compensation Discussion and Analysis",                                 # 55%
            r"compensation\s+discussion\s+and\s+analysis"),
    "summary_comp": ("Summary Compensation Table", r"summary\s+compensation\s+table"),
    "director_comp": ("Director Compensation", r"director\s+compensation"),         # 75%
    "pay_ratio": ("CEO Pay Ratio", r"(ceo\s+)?pay\s+ratio"),                        # 52%
    "pay_vs_perf": ("Pay Versus Performance",                                       # 50%
                    r"pay\s+versus\s+performance|pay[\s-]for[\s-]performance"),
    "ownership": ("Security Ownership", r"security\s+ownership|beneficial\s+ownership"),
    "section16": ("Section 16(a) Reports",                                          # 52%
                  r"section\s+16\(a\)|delinquent\s+section\s+16"),
    "equity_plan": ("Equity Compensation Plan Information",                         # 38%
                    r"equity\s+compensation\s+plan\s+information"),
    "say_on_pay": ("Advisory Vote on Executive Compensation",                       # 68%
                   r"advisory\s+vote\s+on\s+(the\s+)?(named\s+)?executive\s+compensation"
                   r"|say[\s-]on[\s-]pay"),
    "clawback": ("Clawback Policy",                                                 # 58%
                 r"clawback|recoupment|recovery\s+of\s+erroneously"),
    "hedging": ("Hedging and Pledging", r"hedging|pledging"),                       # 70%
}
TITLES = {k: v[0] for k, v in SECTIONS.items()}
PATTERNS = {k: re.compile(v[1], re.IGNORECASE) for k, v in SECTIONS.items()}

# A heading is a short line. Requiring that is what separates a heading from the same
# words used in a sentence, and it is why the families above were measured as headings
# rather than as substring hits.
HEADING_LINE = re.compile(r"^[^\n]{3,120}$", re.MULTILINE)


def heading_marks(text: str) -> list[tuple[int, str]]:
    """Every (position, section) where a section's family matches a short heading line.

    A multi-cell table row cannot be a heading. Without that rule the fee section began
    at its own `Audit Fees | 188,041 | 175,000` row rather than at the heading above the
    table: the largest-gap rule picks the last match before a long run of prose, and the
    last match inside a table is a data row. That silently discarded everything above it
    - including the "(in thousands)" note that says what the figures mean.

    Filings do put real headings inside single-cell layout tables, and those survive:
    one cell produces no separator. Dropping rows that carry one also removes contents
    entries of the form "Corporate Governance | 12" for free.
    """
    marks: list[tuple[int, str]] = []
    for m in HEADING_LINE.finditer(text):
        line = m.group(0).strip()
        if len(line) < 3 or "|" in line:
            continue
        for name, rx in PATTERNS.items():
            if rx.search(line):
                marks.append((m.start(), name))
    marks.sort()
    return marks


def split_sections(text: str) -> dict[str, str]:
    """Return {section: text}, preferring body headings over contents entries."""
    marks = heading_marks(text)
    if not marks:
        return {}

    # The real heading is the occurrence followed by the most text before the next
    # heading of any section. Contents entries sit a few tens of characters apart.
    best: dict[str, tuple[int, int]] = {}
    for i, (pos, name) in enumerate(marks):
        nxt = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        gap = nxt - pos
        if name not in best or gap > best[name][1]:
            best[name] = (pos, gap)

    # Cut at the next chosen heading, not the next raw match, so a subheading or a
    # cross-reference inside the body does not truncate the section.
    chosen = sorted((pos, name) for name, (pos, _) in best.items())
    out: dict[str, str] = {}
    for i, (pos, name) in enumerate(chosen):
        end = chosen[i + 1][0] if i + 1 < len(chosen) else len(text)
        body = text[pos:end].strip()
        if len(body) >= MIN_SECTION:
            out[name] = body
    return out


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

    text = to_rows(resp.text)
    sections = split_sections(text)
    rows = [[str(cik), str(adsh), form, str(filed), str(period), name,
             TITLES[name], len(body), body]
            for name, body in sections.items()]
    return {"adsh": str(adsh), "text": text, "rows": rows}


FIELDS = ["cik", "adsh", "form", "filing_date", "period_of_report",
          "section", "section_title", "char_len", "text"]


async def run_chunk(rows: list[tuple], keep_text: bool = False) -> list:
    headers = {"User-Agent": sec_user_agent(), "Accept-Encoding": "gzip, deflate"}
    limiter = RateLimiter(REQUESTS_PER_SECOND)
    sem = asyncio.Semaphore(IN_FLIGHT)
    limits = httpx.Limits(max_connections=IN_FLIGHT + 2)
    async with httpx.AsyncClient(headers=headers, timeout=180, limits=limits,
                                 follow_redirects=True) as client:
        results = await asyncio.gather(
            *(fetch_one(client, limiter, sem, r) for r in rows))
    docs = [d for d in results if d]
    if keep_text:
        return docs
    return [row for d in docs for row in d["rows"]]


# The check that a section is what it claims: text that says none of the words the
# section is about means the heading matched something else. Alternatives are allowed
# because one word was not enough - a clawback section headed "Clawback Policy" need
# never use "recover", and scoring it against that word alone flagged a sound section.
PROBES = {
    "independence": ("independent",), "audit_fees": ("fee",),
    "related_party": ("related",), "committees": ("committee",),
    "cda": ("compensation",), "risk_oversight": ("risk",),
    "ownership": ("shares",), "summary_comp": ("salary",),
    "clawback": ("clawback", "recoup", "recover", "forfeit"),
    "nominees": ("director",), "pay_ratio": ("ratio",), "audit_report": ("audit",),
    "hedging": ("hedg", "pledg"), "section16": ("section 16", "form 4", "beneficial"),
}


def dry_run(filings: list[tuple]) -> None:
    """Extract and report quality, writing nothing.

    Reports what would otherwise be discovered after hours of fetching: how often each
    section is found, how long it is, whether it reads like itself, and - because a
    proxy's headings are not in a canonical order the way a 10-K's items are - whether
    any section has swallowed the document.
    """
    docs = asyncio.run(run_chunk(filings, keep_text=True))
    rows = [r for d in docs for r in d["rows"]]
    if not rows:
        print("no sections extracted at all")
        return

    lengths: dict[str, list[int]] = {}
    for r in rows:
        lengths.setdefault(r[5], []).append(r[7])
    doc_chars = {d["adsh"]: len(d["text"]) for d in docs}

    print(f"\n{len(docs)} of {len(filings)} filings fetched, "
          f"{len(rows):,} sections extracted "
          f"({sum(1 for d in docs if not d['rows'])} yielded none)")
    sizes = sorted(doc_chars.values())
    print(f"document text: median {sizes[len(sizes)//2]:,} chars, max {sizes[-1]:,}")

    print(f"\n{'section':<16} {'found':>6} {'% docs':>7} {'median':>9} {'p90':>9} "
          f"{'max':>10}")
    for name in SECTIONS:
        got = sorted(lengths.get(name, []))
        if not got:
            print(f"{name:<16} {0:>6} {0:>6.0f}% {'-':>9} {'-':>9} {'-':>10}")
            continue
        print(f"{name:<16} {len(got):>6} {100*len(got)/len(docs):>6.0f}% "
              f"{got[len(got)//2]:>9,} {got[int(len(got)*0.9)]:>9,} {got[-1]:>10,}")

    print("\nDoes each section read like itself?")
    for name, words in PROBES.items():
        texts = [r[8] for r in rows if r[5] == name]
        if not texts:
            continue
        hits = sum(1 for t in texts if any(w in t.lower() for w in words))
        flag = "  <-- suspect" if hits / len(texts) < 0.8 else ""
        print(f"  {name:<16} says {'/'.join(words):<28} "
              f"{100*hits/len(texts):5.0f}% of {len(texts):>4}{flag}")

    # A proxy's headings have no canonical order, so a badly chosen heading does not
    # produce a short section - it produces one that runs to the end of the document.
    # That is the failure this splitter is most likely to have, so measure it directly.
    print("\nHas any section swallowed the document? (section chars / document chars)")
    worst: list[tuple[float, str, str]] = []
    for d in docs:
        n = len(d["text"]) or 1
        for r in d["rows"]:
            worst.append((r[7] / n, r[5], d["adsh"]))
    worst.sort(reverse=True)
    over_half = sum(1 for share, _, _ in worst if share > 0.5)
    print(f"  sections taking over half the document: {over_half} of {len(worst):,}")
    for share, name, adsh in worst[:6]:
        print(f"    {share:5.0%}  {name:<16} {adsh}")

    print("\nShortest sections kept (mis-hits show up here):")
    for r in sorted(rows, key=lambda r: r[7])[:4]:
        print(f"  {r[5]:<16} {r[7]:>6,} chars  {r[1]}  {r[8][:80]!r}")

    print("\nSections per filing:")
    per = sorted(len(d["rows"]) for d in docs)
    print(f"  median {per[len(per)//2]}  min {per[0]}  max {per[-1]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", required=True, help="e.g. 2019-2026")
    ap.add_argument("--forms", default="DEF 14A")
    ap.add_argument("--limit", type=int, default=0, help="cap filings, for a trial run")
    ap.add_argument("--dry-run", action="store_true",
                    help="extract and report quality, write nothing")
    ap.add_argument("--with-financials", action="store_true",
                    help="only filers that have ratios — the scorecard's universe")
    args = ap.parse_args()
    lo, _, hi = args.years.partition("-")
    lo, hi = int(lo), int(hi or lo)
    forms = tuple(f.strip() for f in args.forms.split(","))

    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    form_list = ", ".join(f"'{f}'" for f in forms)
    financials = ("""
          AND lpad(CAST(cik AS VARCHAR), 10, '0') IN
              (SELECT lpad(CAST(cik AS VARCHAR), 10, '0') FROM marts.ratio_values)"""
                  if args.with_financials else "")
    # A spread of filers rather than the first N by accession, so a trial run does not
    # sample one filing agent's house style.
    order = "hash(accession_number)" if args.limit else "filed, accession_number"
    filings = con.execute(f"""
        SELECT cik, accession_number, primary_document, form,
               TRY_CAST(filing_date AS DATE) AS filed, report_date
        FROM ref.filing_index
        WHERE form IN ({form_list})
          AND primary_document IS NOT NULL AND primary_document <> ''
          AND year(TRY_CAST(filing_date AS DATE)) BETWEEN {lo} AND {hi}
          {financials}
        ORDER BY {order}
        {f'LIMIT {args.limit}' if args.limit else ''}""").fetchall()
    print(f"{len(filings):,} proxies indexed for {args.years} ({', '.join(forms)})"
          f"{' — filers with financials only' if args.with_financials else ''}")
    if not filings:
        return

    if args.dry_run:
        dry_run(filings)
        return

    # Skip filings already in the lake, whichever run put them there. Without this a
    # trial year followed by a wider range writes the same filings into two partitions
    # and the view counts every section twice - the fan-out this project has met three
    # times already. The invariant suite would catch it; not creating it is better.
    try:
        done = {r[0] for r in con.execute(
            "SELECT DISTINCT adsh FROM quali.proxy_sections").fetchall()}
    except Exception:  # noqa: BLE001  view absent on the first run
        done = set()
    if done:
        before = len(filings)
        filings = [f for f in filings if str(f[1]) not in done]
        print(f"  {before - len(filings):,} already in the lake, "
              f"{len(filings):,} to fetch")
    if not filings:
        print("nothing left to fetch")
        return

    cfg = R2.from_env()
    s3 = r2util.client(cfg)
    started = time.time()
    done_rows = 0

    with TemporaryDirectory() as tmp:
        for start in range(0, len(filings), CHUNK):
            chunk = filings[start:start + CHUNK]
            index = start // CHUNK
            rows = asyncio.run(run_chunk(chunk))
            if not rows:
                print(f"  chunk {index:>4}  no sections from {len(chunk)} filings")
                continue

            # Partition by each row's own filing year rather than by the range asked
            # for, so a partition means what its name says and two runs over different
            # ranges cannot disagree about where a year lives. The file is named after
            # the first accession it holds, which makes the key deterministic and stops
            # a later run's chunk 0 overwriting an earlier one's.
            by_year: dict[str, list] = {}
            for r in rows:
                by_year.setdefault(str(r[3])[:4] or "unknown", []).append(r)

            written = 0
            for year, part in sorted(by_year.items()):
                key = f"{PREFIX}/filing_year={year}/proxy_{part[0][1]}.parquet"
                if r2util.exists(s3, cfg.bucket, key):
                    continue
                path = Path(tmp) / f"proxy_{part[0][1]}.parquet"
                cols = list(zip(*part))
                table = pa.table({
                    name: pa.array(col, type=pa.int64() if name == "char_len"
                                   else pa.string())
                    for name, col in zip(FIELDS, cols)})
                pq.write_table(table, path, compression="zstd")
                written += r2util.upload(s3, path, cfg.bucket, key)
                path.unlink()
            size = written

            done_rows += len(rows)
            elapsed = time.time() - started
            filings_done = start + len(chunk)
            rate = filings_done / max(elapsed, 1)
            remaining = (len(filings) - filings_done) / max(rate, 0.01) / 60
            print(f"  chunk {index:>4}  {len(chunk)} proxies -> {len(rows):>5,} sections"
                  f"  {size/1e6:5.1f} MB  {rate:4.1f} filings/s"
                  f"  ~{remaining:.0f} min left")

    print(f"DONE {args.years}: {done_rows:,} sections from {len(filings):,} proxies "
          f"in {(time.time() - started)/60:.0f} min")


if __name__ == "__main__":
    main()
