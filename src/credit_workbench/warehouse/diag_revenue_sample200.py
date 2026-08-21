"""A random sample of 200 non-financial nulls, checked against SEC's own record.

The purposive sample of 20 gave 10 recoverable, but it was chosen largest-first and
cannot support a population estimate. This draws 200 at random from a fixed seed and
classifies each against `companyfacts` - the complete undimensioned XBRL record SEC
publishes per filer.

Four outcomes, and the first is the one that matters most:

  LOST      SEC publishes a revenue under a tag `staging.tag_map` ALREADY CLAIMS, and we
            still hold null. That is a pipeline defect, not a mapping gap - the fact was
            available, the map wanted it, and it did not arrive.
  UNMAPPED  SEC publishes a revenue under a tag the map does not claim. A mapping gap.
  NONE      SEC publishes no annual revenue concept. The null is correct.
  ERROR     the filer's companyfacts could not be read.

Two corrections to the earlier check are built in. Period matching now accepts an end
date within seven days of ours, because a 52/53-week retailer closes its year on the
nearest Saturday and the earlier exact match silently failed them - Meritage Homes and
European Wax Center were both reported as having no revenue at SEC when they plainly do.
And the concept filter no longer excludes anything containing "tax", which had been
dropping RevenueFromContractWithCustomerExcludingAssessedTax.

Financial companies are excluded from the population, as instructed: SIC 60-67 never
enters the sample. So is any year before 2010, where XBRL was still phasing in and a
null says more about the mandate than about the filer.
"""
from __future__ import annotations

import datetime
import json
import time
from collections import Counter, defaultdict

import duckdb
import httpx

from credit_workbench.common.config import motherduck_token, sec_user_agent

SEED = 42
N = 200
DAY_TOLERANCE = 7          # 52/53-week fiscal calendars drift against the month end
MIN_DAYS, MAX_DAYS = 300, 400

# The loose run counted AvailableForSaleSecuritiesGrossUnrealizedGains as a revenue
# concept, because "AvailableForSale" contains "sales". So a company-year could be called
# recoverable on the strength of a securities disclosure. The rule is now a whitelist of
# concept shapes that actually ARE an income-statement revenue, and the ASC 932
# supplementary oil-and-gas measures are excluded: they are a standardised-measure
# disclosure, not the revenue line, and mapping them into a spread would be wrong.
REV_HINTS = ("revenue", "sales")
STARTS = ("revenue", "revenues", "salesrevenue", "totalrevenue", "oilandgasrevenue",
          "oilandgassalesrevenue", "contractsrevenue", "healthcareorganizationrevenue",
          "electricutilityrevenue", "regulatedoperatingrevenue",
          "regulatedandunregulatedoperatingrevenue", "unregulatedoperatingrevenue",
          "interestanddividendincome", "interestandfeeincome")
CONTAINS_OK = ("operatingrevenue", "salesrevenuenet", "salesrevenuegoods",
               "salesrevenueservices", "revenuefromcontract", "revenuefromsale",
               "revenuefromrendering", "revenuesnetofinterestexpense",
               "revenuesincludingintersegment", "realestaterevenue")
EXCLUDE = ("deferred", "unbilled", "contractwithcustomerliability", "remaining",
           "disaggregat", "costof", "expenserelated", "receivable", "percentage",
           "unearned", "backlog", "incometax", "taxexpense", "proforma",
           "proceedsfrom", "gainloss", "gainslosses", "impairment", "salesandmarketing",
           "taxeffect", "adjustment", "allowance", "availableforsale", "paymentsto",
           "salesandtransfers", "netincreasedecrease", "decreasedueto", "grossunrealized",
           "grossrealized", "baddebts", "lessprovision", "resultsofoperations",
           "mineralsinplace", "discontinuedoperation")

POPULATION = """
SELECT s.cik, s.company_name, s.sic, s.fy, s.period_end,
       s.operating_income IS NOT NULL AS has_operating_income
FROM marts.spreads_a s
WHERE s.basis = 'first_reported' AND s.is_primary_annual
  AND s.revenue IS NULL AND s.fy >= 2010
  AND substr(s.sic, 1, 2) NOT IN ('60','61','62','63','64','65','67')
"""


def parse(s: str) -> datetime.date:
    return datetime.date.fromisoformat(s)


def revenue_concepts(facts: dict, period_end: datetime.date):
    """Annual revenue concepts SEC publishes for a period ending near ours."""
    out = []
    for taxonomy in ("us-gaap", "ifrs-full"):
        for concept, body in facts.get("facts", {}).get(taxonomy, {}).items():
            low = concept.lower()
            if any(x in low for x in EXCLUDE):
                continue
            if not (low.startswith(STARTS) or any(c in low for c in CONTAINS_OK)):
                continue
            for unit, entries in body.get("units", {}).items():
                if not (unit.startswith("USD") or unit in ("EUR", "JPY", "GBP", "CHF")):
                    continue
                for e in entries:
                    if not e.get("start") or not e.get("end") or e.get("val") is None:
                        continue
                    try:
                        end, start = parse(e["end"]), parse(e["start"])
                    except ValueError:
                        continue
                    if abs((end - period_end).days) > DAY_TOLERANCE:
                        continue
                    if not MIN_DAYS <= (end - start).days <= MAX_DAYS:
                        continue
                    out.append((concept, float(e["val"])))
    best = {}
    for concept, val in out:
        best[concept] = max(best.get(concept, 0), abs(val))
    return best


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    pop_size = con.execute(f"SELECT count(*) FROM ({POPULATION})").fetchone()[0]
    rows = con.execute(
        f"SELECT * FROM ({POPULATION}) USING SAMPLE {N} ROWS (reservoir, {SEED})"
    ).fetchall()
    heads = [d[0] for d in con.description]
    sample = [dict(zip(heads, r)) for r in rows]
    print(f"population: {pop_size:,} non-financial null-revenue company-years, FY2010+")
    print(f"sample:     {len(sample)} drawn at random, seed {SEED}\n")

    mapped_tags = {r[0] for r in con.execute(
        "SELECT DISTINCT tag FROM staging.tag_map WHERE line_code = 'revenue'").fetchall()}
    print(f"tag_map claims {len(mapped_tags)} revenue tags\n")

    client = httpx.Client(
        headers={"User-Agent": sec_user_agent(), "Accept-Encoding": "gzip, deflate"},
        timeout=90, follow_redirects=True)

    cache: dict[int, dict | None] = {}
    verdicts = Counter()
    unmapped_concepts = Counter()
    lost_cases = []
    by_division = defaultdict(Counter)
    by_era = defaultdict(Counter)

    for i, rec in enumerate(sample, 1):
        cik = int(rec["cik"])
        pe = rec["period_end"]
        pe = pe if isinstance(pe, datetime.date) else parse(str(pe))
        if cik not in cache:
            url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
            try:
                resp = client.get(url)
                cache[cik] = json.loads(resp.content) if resp.status_code == 200 else None
            except Exception:
                cache[cik] = None
            time.sleep(0.12)                   # SEC fair access: well under 10 req/s
        facts = cache[cik]
        era = "2010-2016" if rec["fy"] <= 2016 else ("2017-2021" if rec["fy"] <= 2021
                                                     else "2022-2025")

        if facts is None:
            verdict = "ERROR"
        else:
            found = revenue_concepts(facts, pe)
            if not found:
                verdict = "NONE"
            elif set(found) & mapped_tags:
                verdict = "LOST"
                hit = sorted(set(found) & mapped_tags)[0]
                lost_cases.append((rec["company_name"], rec["sic"], rec["fy"],
                                   hit, found[hit]))
            else:
                verdict = "UNMAPPED"
                for c in found:
                    unmapped_concepts[c] += 1
        verdicts[verdict] += 1
        by_era[era][verdict] += 1
        by_division[str(rec["sic"])[:2]][verdict] += 1
        if i % 50 == 0:
            print(f"  ... {i}/{len(sample)} checked")

    ok = sum(v for k, v in verdicts.items() if k != "ERROR")
    print("\n### Verdicts")
    for k in ("LOST", "UNMAPPED", "NONE", "ERROR"):
        n = verdicts[k]
        pct = f"{100.0 * n / ok:.1f}%" if ok and k != "ERROR" else ""
        print(f"  {k:<10}{n:>5}  {pct:>7}")

    recoverable = verdicts["LOST"] + verdicts["UNMAPPED"]
    rate = recoverable / ok if ok else 0
    lo, hi = max(0.0, rate - 1.96 * (rate * (1 - rate) / ok) ** 0.5), \
             min(1.0, rate + 1.96 * (rate * (1 - rate) / ok) ** 0.5)
    print(f"\n  recoverable {recoverable}/{ok} = {100 * rate:.1f}% "
          f"(95% CI {100 * lo:.1f}-{100 * hi:.1f}%)")
    print(f"  extrapolated to the population: {int(rate * pop_size):,} of {pop_size:,} "
          f"company-years (CI {int(lo * pop_size):,}-{int(hi * pop_size):,})")

    print("\n### LOST - tag_map already claims these, and we still hold null")
    if lost_cases:
        for name, sic, fy, tag, val in lost_cases[:20]:
            print(f"  {name[:34]:<36} {sic}  FY{fy}  {tag:<52} {val:>16,.0f}")
    else:
        print("  none - every recoverable case is a mapping gap, not a lost fact")

    print("\n### UNMAPPED - concepts to add, by how many sampled company-years")
    for concept, n in unmapped_concepts.most_common(20):
        print(f"  {concept:<66}{n:>5}")

    print("\n### By era")
    for era in sorted(by_era):
        c = by_era[era]
        tot = sum(v for k, v in c.items() if k != "ERROR")
        rec = c["LOST"] + c["UNMAPPED"]
        print(f"  {era:<12}{tot:>5} checked   recoverable {rec:>4} "
              f"({100.0 * rec / tot if tot else 0:.0f}%)")

    print("\n### By SIC major group, worst first")
    ranked = sorted(by_division.items(),
                    key=lambda kv: -(kv[1]["LOST"] + kv[1]["UNMAPPED"]))
    for sic2, c in ranked[:12]:
        tot = sum(v for k, v in c.items() if k != "ERROR")
        rec = c["LOST"] + c["UNMAPPED"]
        if rec:
            print(f"  SIC {sic2}  {tot:>4} checked   recoverable {rec:>3}")


if __name__ == "__main__":
    main()
