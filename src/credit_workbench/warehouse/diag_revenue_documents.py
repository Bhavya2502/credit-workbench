"""Does the company actually have revenue, or does our source merely lack it?

Every answer so far has confused two different claims. "SEC's XBRL carries no revenue
concept" is not "the company had no revenue". XBRL is a tagging layer laid over a filing;
the income statement itself is a table in the 10-K, and it exists whether or not anyone
tagged it. `quali.filing_sections` cannot settle it either - Item 8 is deliberately not
stored, because this platform takes financials from XBRL by design.

So the null verdicts are tested twice, from the outside in.

**Stage A - every taxonomy, not two.** The sample check scanned `us-gaap` and
`ifrs-full` only. A filer using a company extension namespace, or `srt`, would have been
recorded as having no revenue. This enumerates every taxonomy companyfacts returns and
searches all of them.

**Stage B - the document itself.** For company-years still showing nothing, the 10-K is
fetched from EDGAR and its income statement read as text. `common.html_text.to_rows`
keeps table rows intact, which is what makes a revenue label and its number legible on
one line - the same property that made the proxy fee tables readable.

If Stage B finds revenue on the face of a statement that no taxonomy carries, then the
figure exists, nobody tagged it, and no XBRL-based pipeline can ever reach it. That is a
different and much more serious finding than a mapping gap, and it is the one the
question is really about.
"""
from __future__ import annotations

import datetime
import json
import re
import time
from collections import Counter

import duckdb
import httpx

from credit_workbench.common.config import motherduck_token, sec_user_agent
from credit_workbench.common.html_text import to_rows

SEED, N, DOCS = 42, 200, 25
DAY_TOLERANCE, MIN_DAYS, MAX_DAYS = 7, 300, 400

STARTS = ("revenue", "revenues", "salesrevenue", "totalrevenue", "oilandgasrevenue",
          "oilandgassalesrevenue", "contractsrevenue", "healthcareorganizationrevenue",
          "electricutilityrevenue", "regulatedoperatingrevenue", "netsales",
          "regulatedandunregulatedoperatingrevenue", "unregulatedoperatingrevenue",
          "interestanddividendincome", "interestandfeeincome", "totalrevenues")
CONTAINS_OK = ("operatingrevenue", "salesrevenue", "revenuefromcontract",
               "revenuefromsale", "revenuefromrendering", "revenuesnetofinterest",
               "revenuesincludingintersegment", "realestaterevenue", "netrevenue",
               "totalrevenue")
EXCLUDE = ("deferred", "unbilled", "contractwithcustomerliability", "remaining",
           "disaggregat", "costof", "expenserelated", "receivable", "percentage",
           "unearned", "backlog", "incometax", "taxexpense", "proforma", "proceedsfrom",
           "gainloss", "gainslosses", "impairment", "salesandmarketing", "taxeffect",
           "adjustment", "allowance", "availableforsale", "paymentsto",
           "salesandtransfers", "netincreasedecrease", "decreasedueto", "grossunrealized",
           "grossrealized", "baddebts", "lessprovision", "resultsofoperations",
           "mineralsinplace", "discontinuedoperation")

POPULATION = """
SELECT s.cik, s.company_name, s.sic, s.fy, s.period_end
FROM marts.spreads_a s
WHERE s.basis = 'first_reported' AND s.is_primary_annual
  AND s.revenue IS NULL AND s.fy >= 2010
  AND substr(s.sic, 1, 2) NOT IN ('60','61','62','63','64','65','67')
"""

# A revenue label on the face of an income statement, followed by a number on the same
# row. to_rows() is what keeps them on the same row.
REV_LINE = re.compile(
    r"(?im)^[^\n]{0,80}?\b("
    r"total\s+revenues?|total\s+net\s+revenues?|net\s+revenues?|revenues?"
    r"|net\s+sales|total\s+sales|sales,?\s+net|total\s+operating\s+revenues?"
    r"|oil\s+and\s+gas\s+revenues?|operating\s+revenues?"
    r")\b[^\n]{0,60}?[\s\$\(]([\d][\d,]{3,})")


def parse(s: str) -> datetime.date:
    return datetime.date.fromisoformat(s)


def revenue_in_facts(facts: dict, period_end: datetime.date, taxonomies=None):
    hits = {}
    node = facts.get("facts", {})
    for taxonomy in (taxonomies if taxonomies is not None else node.keys()):
        for concept, body in node.get(taxonomy, {}).items():
            low = concept.lower()
            if any(x in low for x in EXCLUDE):
                continue
            if not (low.startswith(STARTS) or any(c in low for c in CONTAINS_OK)):
                continue
            for unit, entries in body.get("units", {}).items():
                if not (unit.startswith("USD") or unit in ("EUR", "JPY", "GBP", "CHF")):
                    continue
                for e in entries:
                    if not (e.get("start") and e.get("end") and e.get("val") is not None):
                        continue
                    try:
                        end, start = parse(e["end"]), parse(e["start"])
                    except ValueError:
                        continue
                    if abs((end - period_end).days) > DAY_TOLERANCE:
                        continue
                    if MIN_DAYS <= (end - start).days <= MAX_DAYS:
                        hits[f"{taxonomy}:{concept}"] = float(e["val"])
    return hits


def find_filing(client, cik: int, period_end: datetime.date):
    """The annual filing whose report date is nearest our period end."""
    try:
        r = client.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
        if r.status_code != 200:
            return None
        sub = json.loads(r.content)
    except Exception:
        return None
    blocks = [sub.get("filings", {}).get("recent", {})]
    for extra in sub.get("filings", {}).get("files", [])[:4]:
        try:
            rr = client.get(f"https://data.sec.gov/submissions/{extra['name']}")
            if rr.status_code == 200:
                blocks.append(json.loads(rr.content))
        except Exception:
            pass
        time.sleep(0.12)
    best = None
    for b in blocks:
        forms = b.get("form", [])
        for i, form in enumerate(forms):
            if form not in ("10-K", "10-K/A", "20-F", "40-F", "10-KT"):
                continue
            rd = b.get("reportDate", [None] * len(forms))[i]
            if not rd:
                continue
            try:
                gap = abs((parse(rd) - period_end).days)
            except ValueError:
                continue
            if gap <= 20 and (best is None or gap < best[0]):
                best = (gap, b["accessionNumber"][i], b["primaryDocument"][i])
    return best[1:] if best else None


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    rows = con.execute(
        f"SELECT * FROM ({POPULATION}) USING SAMPLE {N} ROWS (reservoir, {SEED})"
    ).fetchall()
    heads = [d[0] for d in con.description]
    sample = [dict(zip(heads, r)) for r in rows]
    print(f"re-drawn sample: {len(sample)} (seed {SEED}), FY2010+, non-financial\n")

    client = httpx.Client(
        headers={"User-Agent": sec_user_agent(), "Accept-Encoding": "gzip, deflate"},
        timeout=120, follow_redirects=True)

    # ---------------------------------------------------------------- Stage A
    print("### Stage A - search EVERY taxonomy, not just us-gaap and ifrs-full")
    still_none, taxonomies_seen, extra_hits = [], Counter(), []
    for rec in sample:
        cik, pe = int(rec["cik"]), rec["period_end"]
        pe = pe if isinstance(pe, datetime.date) else parse(str(pe))
        try:
            r = client.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")
            facts = json.loads(r.content) if r.status_code == 200 else None
        except Exception:
            facts = None
        time.sleep(0.12)
        if facts is None:
            continue
        for t in facts.get("facts", {}):
            taxonomies_seen[t] += 1
        two = revenue_in_facts(facts, pe, ("us-gaap", "ifrs-full"))
        allt = revenue_in_facts(facts, pe, None)
        if not two and allt:
            extra_hits.append((rec["company_name"], rec["fy"], sorted(allt)[0],
                               list(allt.values())[0]))
        if not allt:
            still_none.append(rec)

    print(f"  taxonomies present across the sample: "
          f"{', '.join(f'{k} ({v})' for k, v in taxonomies_seen.most_common())}")
    print(f"  found ONLY outside us-gaap/ifrs-full: {len(extra_hits)}")
    for name, fy, concept, val in extra_hits[:10]:
        print(f"     {name[:34]:<36} FY{fy}  {concept:<44} {val:>16,.0f}")
    print(f"  still no revenue in ANY taxonomy: {len(still_none)} of {len(sample)}")

    # ---------------------------------------------------------------- Stage B
    print(f"\n### Stage B - read the actual 10-K for {min(DOCS, len(still_none))} of them")
    checked = found = notfound = failed = 0
    for rec in still_none[:DOCS]:
        cik, pe = int(rec["cik"]), rec["period_end"]
        pe = pe if isinstance(pe, datetime.date) else parse(str(pe))
        label = f'{rec["company_name"][:32]:<34} {rec["sic"]} FY{rec["fy"]}'
        filing = find_filing(client, cik, pe)
        if not filing:
            print(f"  {label}  no annual filing found on EDGAR")
            failed += 1
            continue
        adsh, doc = filing
        url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
               f"{adsh.replace('-', '')}/{doc}")
        try:
            resp = client.get(url)
            if resp.status_code != 200:
                print(f"  {label}  document HTTP {resp.status_code}")
                failed += 1
                continue
            text = to_rows(resp.text)
        except Exception as exc:
            print(f"  {label}  fetch/parse failed: {str(exc)[:50]}")
            failed += 1
            continue
        checked += 1
        hits = REV_LINE.findall(text)
        if hits:
            found += 1
            sample_hits = "; ".join(f"{a.strip()} {b}" for a, b in hits[:3])
            print(f"  {label}  REVENUE IN DOCUMENT -> {sample_hits[:96]}")
        else:
            notfound += 1
            print(f"  {label}  no revenue line in the document either "
                  f"({len(text):,} chars)")
        time.sleep(0.15)

    print(f"\n  documents read {checked}   revenue line present {found}   "
          f"absent {notfound}   could not fetch {failed}")
    if checked:
        print(f"  => {100.0 * found / checked:.0f}% of 'no revenue anywhere in XBRL' "
              f"company-years DO show a revenue line in the filing")


if __name__ == "__main__":
    main()
