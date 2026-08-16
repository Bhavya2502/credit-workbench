"""Tracker G3 — what would a proxy extractor actually be built on?

Three questions, in the order that decides whether the work is worth doing.

**What proxies do we hold?** `ref.filing_index` carries the form type, but a proxy is
only fetchable if `primary_document` is populated, and only useful if it is the annual
meeting proxy rather than a merger solicitation or a one-page additional-materials
filing. Counting `form = 'DEF 14A'` alone would overstate what is reachable.

**What governance data is already structured?** This matters more than it looks. The
auditor's name and the ICFR attestation flag have been `dei` tags since FY2021, the
financial-statement error-correction flag since FY2022, and Item 402(v) Pay versus
Performance arrives in its own `ecd` namespace - all of which would land in the 373m
facts we already hold. Anything already tagged should never be re-parsed out of HTML.
The point of asking first is to shrink the parser, not to justify it.

**Do proxies even split?** The 10-K extractor keys on `^item\\s*N`, a numbering that
Schedule 14A does not impose - proxies carry prose headings whose wording is the
filer's choice. So the heading vocabulary has to be read off real documents rather than
assumed from the regulation, and the candidate patterns below are measured for recall
per document before a line of the splitter is written.

Two things are probed that experience says will otherwise be discovered late. Fee
tables and director tables are HTML *tables*, so after tag-stripping the labels and the
numbers land on separate lines - a structured extractor that assumes "Audit Fees $1,234"
on one line will silently find nothing. And the sentence that states board independence
is phrased freely. Both are dumped verbatim here rather than guessed at.
"""
from __future__ import annotations

import asyncio
import re
from collections import Counter

import duckdb
import httpx

from credit_workbench.common.config import motherduck_token, sec_user_agent
from credit_workbench.ingest.filing_sections import ARCHIVES, RateLimiter, to_text

SAMPLE = 40

# ---------------------------------------------------------------- warehouse probes

Q = [
    ("1. Which proxy forms do we hold, and are they fetchable?", """
        SELECT form,
               count(*) AS filings,
               count(DISTINCT cik) AS companies,
               count(*) FILTER (WHERE primary_document IS NOT NULL
                                  AND primary_document <> '') AS with_document,
               min(year(TRY_CAST(filing_date AS DATE))) AS first_year,
               max(year(TRY_CAST(filing_date AS DATE))) AS last_year
        FROM ref.filing_index
        WHERE form LIKE '%14A' OR form LIKE '%14C'
        GROUP BY form ORDER BY filings DESC"""),

    ("2. DEF 14A by filing year — is coverage even?", """
        SELECT year(TRY_CAST(filing_date AS DATE)) AS filing_year,
               count(*) AS filings, count(DISTINCT cik) AS companies
        FROM ref.filing_index
        WHERE form = 'DEF 14A'
        GROUP BY 1 ORDER BY 1"""),

    ("3. How many proxy filers also have financials? (the joinable universe)", """
        SELECT count(DISTINCT f.cik) AS proxy_filers_with_ratios
        FROM ref.filing_index f
        WHERE f.form = 'DEF 14A'
          AND year(TRY_CAST(f.filing_date AS DATE)) >= 2019
          AND lpad(CAST(f.cik AS VARCHAR), 10, '0') IN
              (SELECT lpad(CAST(cik AS VARCHAR), 10, '0')
               FROM marts.ratio_values WHERE fy >= 2019)"""),

    ("4. One filer per year, or several? (DEF 14A per company-year)", """
        SELECT proxies_in_year, count(*) AS company_years
        FROM (SELECT cik, year(TRY_CAST(filing_date AS DATE)) AS y,
                     count(*) AS proxies_in_year
              FROM ref.filing_index WHERE form = 'DEF 14A' GROUP BY 1, 2)
        GROUP BY 1 ORDER BY 1 LIMIT 6"""),

    # Anything already tagged must not be re-parsed from HTML. Restricting to the
    # governance tags keeps this a filtered scan rather than a walk over 222m rows.
    ("5. Governance tags already in the warehouse (dei / ecd)", """
        SELECT tag, count(*) AS facts, count(DISTINCT cik) AS companies,
               min(fy) AS first_fy, max(fy) AS last_fy
        FROM staging.facts_pit
        WHERE tag IN ('AuditorName', 'AuditorFirmId', 'AuditorLocation',
                      'IcfrAuditorAttestationFlag',
                      'DocumentFinStmtErrorCorrectionFlag',
                      'EntityShellCompany', 'EntityVoluntaryFilers',
                      'EntityWellKnownSeasonedIssuer')
        GROUP BY tag ORDER BY facts DESC"""),

    ("6. Does anything from the Pay-versus-Performance namespace exist?", """
        SELECT tag, count(*) AS facts, count(DISTINCT cik) AS companies
        FROM ref.tag_catalog
        WHERE lower(tag) LIKE '%compensationactuallypaid%'
           OR lower(tag) LIKE '%peo%'
           OR lower(tag) LIKE '%payratio%'
           OR lower(tag) LIKE '%clawback%'
           OR lower(tag) LIKE '%erroneouslyawarded%'
        GROUP BY tag ORDER BY facts DESC LIMIT 25"""),

    ("7. What does ref.tag_catalog actually look like? (column names first)", """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'ref' AND table_name = 'tag_catalog'
        ORDER BY ordinal_position"""),

    ("8. Governance-ish tags by name, whatever the namespace", """
        SELECT tag, count(*) AS n
        FROM staging.facts_pit
        WHERE fy >= 2022
          AND (lower(tag) LIKE '%auditor%' OR lower(tag) LIKE '%compensation%actually%'
               OR lower(tag) LIKE '%payratio%')
        GROUP BY tag ORDER BY n DESC LIMIT 20"""),

    ("9. Is the 10-K controls section (Item 9A) already covering ICFR?", """
        SELECT count(*) AS sections, count(DISTINCT cik) AS companies,
               round(median(char_len), 0) AS median_chars
        FROM quali.filing_sections WHERE item = '9A'"""),

    ("10. And Item 10/11/13 — the 10-K governance items, often incorporated by "
     "reference to the proxy", """
        SELECT item, count(*) AS sections, count(DISTINCT cik) AS companies,
               round(median(char_len), 0) AS median_chars
        FROM quali.filing_sections WHERE item IN ('10', '11', '12', '13', '14')
        GROUP BY item ORDER BY item"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)))[:56]
             for v in r] for r in cur.fetchall()]
    if not rows:
        print("  (no rows)")
        return
    w = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(heads)]
    print("  " + "  ".join(h.ljust(x) for h, x in zip(heads, w)))
    print("  " + "  ".join("-" * x for x in w))
    for r in rows:
        print("  " + "  ".join(v.ljust(x) for v, x in zip(r, w)))


# ---------------------------------------------------------------- document probes

# Candidate section vocabulary, drawn from Schedule 14A and Reg S-K Items 401-407 but
# deliberately given several phrasings each, because the wording is the filer's choice.
# What matters here is the measured recall per document, not the taxonomy.
CANDIDATES: dict[str, str] = {
    "governance": r"corporate governance",
    "independence": r"(director|board)\s+independence|independence\s+of\s+(the\s+)?"
                    r"(our\s+)?(board|directors)|independent\s+directors",
    "committees": r"committees?\s+of\s+the\s+board|board\s+committees|"
                  r"committee\s+membership|audit\s+committee",
    "risk_oversight": r"risk\s+oversight|board.{0,15}role\s+in\s+risk|"
                      r"oversight\s+of\s+risk",
    "attendance": r"(meetings?\s+of\s+the\s+board|board\s+meetings|"
                  r"attendance\s+at)",
    "nominees": r"election\s+of\s+directors|nominees\s+for\s+(election\s+as\s+)?"
                r"director|our\s+director\s+nominees",
    "related_party": r"related[\s-]person\s+transactions|related[\s-]party\s+"
                     r"transactions|certain\s+relationships\s+and\s+related|"
                     r"transactions\s+with\s+related",
    "audit_fees": r"principal\s+accountant\s+fees|audit\s+(and\s+non[\s-]audit\s+)?fees|"
                  r"fees\s+(paid|billed)\s+to|independent\s+registered\s+public\s+"
                  r"accounting\s+firm\s+fees",
    "audit_report": r"(report\s+of\s+the\s+audit\s+committee|audit\s+committee\s+report)",
    "cda": r"compensation\s+discussion\s+and\s+analysis",
    "summary_comp": r"summary\s+compensation\s+table",
    "director_comp": r"director\s+compensation",
    "pay_ratio": r"(ceo\s+)?pay\s+ratio",
    "pay_vs_perf": r"pay\s+versus\s+performance|pay[\s-]for[\s-]performance",
    "ownership": r"security\s+ownership|beneficial\s+ownership",
    "section16": r"section\s+16\(a\)|delinquent\s+section\s+16",
    "equity_plan": r"equity\s+compensation\s+plan\s+information",
    "say_on_pay": r"advisory\s+vote\s+on\s+(the\s+)?(named\s+)?executive\s+compensation|"
                  r"say[\s-]on[\s-]pay|approval.{0,20}executive\s+compensation",
    "clawback": r"clawback|recoupment|recovery\s+of\s+erroneously",
    "hedging": r"hedging|pledging",
    "auditor_ratify": r"ratif(y|ication)\s+.{0,40}(accounting\s+firm|auditor)",
}

# Phrasings that state board independence as a countable fact. Free-form on purpose:
# the probe reports which ones actually fire and on what text.
INDEP_PATTERNS = [
    r"(\w+|\d+)\s+of\s+(?:our|the)\s+(\w+|\d+)\s+(?:current\s+)?directors?\s+"
    r"(?:are|is|were|qualify)\b[^.]{0,60}independen",
    r"all\s+(?:of\s+)?(?:our|the)\s+directors?[^.]{0,80}except[^.]{0,60}independen",
    r"(?:board|we)\s+(?:has|have)\s+determined\s+that\s+(\w+|\d+)[^.]{0,80}independen",
    r"(\w+|\d+)\s+(?:of\s+)?(?:the\s+)?(\w+|\d+)\s+(?:members|nominees)[^.]{0,60}"
    r"independen",
]

FEE_LABELS = ("audit fees", "audit-related fees", "tax fees", "all other fees",
              "total fees")


async def fetch(client, limiter, sem, row):
    cik, adsh, doc = row
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
                # The raw markup is kept so a second probe can compare converters on
                # exactly these documents without fetching them from SEC again.
                return {"adsh": str(adsh), "cik": str(cik), "url": url,
                        "raw": len(resp.text), "_html": resp.text,
                        "text": to_text(resp.text)}
            if resp.status_code in (403, 429, 500, 502, 503):
                await asyncio.sleep(3 * (attempt + 1))
                continue
            return None
    return None


async def fetch_all(rows):
    headers = {"User-Agent": sec_user_agent(), "Accept-Encoding": "gzip, deflate"}
    limiter, sem = RateLimiter(9.0), asyncio.Semaphore(8)
    limits = httpx.Limits(max_connections=10)
    async with httpx.AsyncClient(headers=headers, timeout=120, limits=limits,
                                 follow_redirects=True) as client:
        got = await asyncio.gather(*(fetch(client, limiter, sem, r) for r in rows))
    return [d for d in got if d]


def probe_documents(rows) -> None:
    docs = asyncio.run(fetch_all(rows))
    if not docs:
        print("  (nothing fetched)")
        return
    print(f"\n  fetched {len(docs)} of {len(rows)} proxies")
    sizes = sorted(len(d["text"]) for d in docs)
    raws = sorted(d["raw"] for d in docs)
    print(f"  text chars   median {sizes[len(sizes)//2]:,}  "
          f"min {sizes[0]:,}  max {sizes[-1]:,}")
    print(f"  raw html     median {raws[len(raws)//2]:,}  max {raws[-1]:,}")
    tiny = [d for d in docs if len(d["text"]) < 20_000]
    print(f"  under 20k chars: {len(tiny)} "
          f"(a real annual proxy is far bigger — these are cover pages or wrappers)")
    for d in tiny[:3]:
        print(f"    {d['adsh']}  {len(d['text']):>7,}  {d['text'][:110]!r}")

    # ---- 12. does the candidate vocabulary actually hit?
    print("\n### 12. Candidate heading recall — % of documents containing the phrase "
          "anywhere, and as a short line (a heading rather than a mention)")
    print(f"  {'section':<16} {'anywhere':>9} {'as heading':>11} "
          f"{'headings/doc':>13}")
    for name, pat in CANDIDATES.items():
        rx = re.compile(pat, re.IGNORECASE)
        anywhere = heading = 0
        per_doc = []
        for d in docs:
            body = d["text"]
            if rx.search(body):
                anywhere += 1
            hits = [ln for ln in body.split("\n")
                    if 3 <= len(ln.strip()) <= 120 and rx.search(ln)]
            if hits:
                heading += 1
                per_doc.append(len(hits))
        med = sorted(per_doc)[len(per_doc) // 2] if per_doc else 0
        print(f"  {name:<16} {100*anywhere/len(docs):>8.0f}% "
              f"{100*heading/len(docs):>10.0f}% {med:>13}")

    # ---- 13. what the headings actually say, read off the documents
    # A heading is a short line followed by a substantial run of prose. Ranking by
    # document frequency rather than raw count keeps one filer's repeated running
    # header from dominating.
    print("\n### 13. Most common headings, read off the documents "
          "(short line followed by >=800 chars of prose)")
    docfreq: Counter[str] = Counter()
    for d in docs:
        lines = [ln.strip() for ln in d["text"].split("\n")]
        seen = set()
        for i, ln in enumerate(lines):
            if not (3 <= len(ln) <= 90) or not re.search(r"[A-Za-z]", ln):
                continue
            if re.fullmatch(r"[\d\W]+", ln) or ln.endswith("."):
                continue
            run = 0
            for nxt in lines[i + 1:]:
                if 3 <= len(nxt) <= 90 and not re.fullmatch(r"[\d\W]+", nxt):
                    break
                run += len(nxt)
            if run >= 800:
                key = re.sub(r"\s+", " ", ln.lower())
                key = re.sub(r"^(item|proposal|part)\s*[\divxlc]+[.:)\-–—]?\s*", "", key)
                key = re.sub(r"[^a-z0-9 ()'/&-]", "", key).strip()
                if len(key) >= 3:
                    seen.add(key)
        docfreq.update(seen)
    for phrase, n in docfreq.most_common(45):
        print(f"  {100*n/len(docs):>5.0f}%  {phrase[:88]}")

    # ---- 14. board independence: how is it phrased?
    print("\n### 14. Is board independence stated as a countable fact?")
    fired = Counter()
    examples: list[str] = []
    for d in docs:
        low = re.sub(r"\s+", " ", d["text"].lower())
        for i, pat in enumerate(INDEP_PATTERNS):
            m = re.search(pat, low)
            if m:
                fired[i] += 1
                if len(examples) < 8:
                    examples.append(f"p{i}  {d['adsh']}  ...{m.group(0)[:150]}")
    for i, pat in enumerate(INDEP_PATTERNS):
        print(f"  pattern {i}  {100*fired[i]/len(docs):>5.0f}% of docs   "
              f"{pat[:64]}")
    hit_any = sum(1 for d in docs
                  if any(re.search(p, re.sub(r"\s+", " ", d["text"].lower()))
                         for p in INDEP_PATTERNS))
    print(f"  any pattern: {100*hit_any/len(docs):.0f}% of documents")
    print("  examples:")
    for e in examples:
        print(f"    {e}")
    # A structured count needs a number. Words are as common as digits here.
    print("\n  How often does the word 'independent' appear per document? "
          "(a section exists even when no sentence counts it)")
    counts = sorted(d["text"].lower().count("independent") for d in docs)
    print(f"    median {counts[len(counts)//2]}  min {counts[0]}  max {counts[-1]}")

    # ---- 15. the fee table, verbatim — this decides whether fees are extractable
    print("\n### 15. The auditor fee table after tag-stripping "
          "(labels and numbers separate? this decides the extractor's shape)")
    shown = 0
    with_fees = 0
    for d in docs:
        low = d["text"].lower()
        if "audit fees" not in low and "audit-related fees" not in low:
            continue
        with_fees += 1
        if shown >= 3:
            continue
        shown += 1
        pos = low.rfind("audit fees")
        print(f"\n    --- {d['adsh']} ---")
        for ln in d["text"][max(0, pos - 200):pos + 700].split("\n")[:26]:
            if ln.strip():
                print(f"      | {ln.strip()[:96]}")
    print(f"\n  documents containing a fee label: {with_fees} of {len(docs)} "
          f"({100*with_fees/len(docs):.0f}%)")
    print("  which fee labels appear:")
    for lab in FEE_LABELS:
        n = sum(1 for d in docs if lab in d["text"].lower())
        print(f"    {lab:<22} {100*n/len(docs):>5.0f}%")
    # If a label and its number sit on one line, a line regex works; if not, the
    # extractor has to walk forward across lines. Measure it, do not assume.
    same_line = 0
    for d in docs:
        for ln in d["text"].split("\n"):
            low = ln.lower()
            if "audit fees" in low and re.search(r"[\d,]{4,}", ln):
                same_line += 1
                break
    print(f"  'audit fees' and a number on the SAME line: {same_line} of {len(docs)} "
          f"({100*same_line/len(docs):.0f}%)")

    # ---- 16. director table, verbatim
    print("\n### 16. Around the word 'Independent' near a director table "
          "(is committee membership a matrix of symbols?)")
    for d in docs[:2]:
        m = re.search(r"^\s*(name|director)\b.{0,80}$", d["text"],
                      re.IGNORECASE | re.MULTILINE)
        if not m:
            continue
        print(f"\n    --- {d['adsh']} ---")
        for ln in d["text"][m.start():m.start() + 600].split("\n")[:22]:
            if ln.strip():
                print(f"      | {ln.strip()[:96]}")


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    for title, q in Q:
        print(f"\n### {title}")
        try:
            show(con, q)
        except Exception as exc:
            print(f"  (failed: {str(exc)[:170]})")

    # A spread of filers and years, not the first N by accession, so the heading
    # vocabulary is not one era's or one filing agent's house style.
    print(f"\n### 11. Fetching {SAMPLE} real proxies to read their structure")
    try:
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
        print(f"  {len(rows)} sampled across filing years 2019-2026")
    except Exception as exc:
        print(f"  (query failed: {str(exc)[:170]})")
        return
    if rows:
        probe_documents(rows)


if __name__ == "__main__":
    main()
