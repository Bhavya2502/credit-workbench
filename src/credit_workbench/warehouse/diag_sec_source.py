"""Go to SEC itself: does the source hold a revenue we are not carrying?

Every previous answer was circular. Each one asked `staging.facts_pit` whether a revenue
tag existed, and `facts_pit` is a derived layer - so if the loss happened upstream of it,
no query against it could ever reveal that.

There are two upstream sources and this platform uses one of them:

  Financial Statement Data Sets (`raw.fsn_num`)   what facts_pit is built from. DERA's
      quarterly extract, restricted to the primary financial statements as rendered.
  companyfacts.zip / the companyfacts API         every XBRL fact a filer ever tagged,
      including concepts that never appear in DERA's statement extract.

If those differ, revenue could be present at SEC and absent here through no fault of the
tag map at all. This asks SEC directly, filer by filer, for non-financial companies whose
revenue we hold as null - the real operators first, because a null for Xcel Energy cannot
be a disclosure fact.

Financial companies are excluded throughout, as instructed: SIC 60-67 never enters the
sample.
"""
from __future__ import annotations

import json
import time

import duckdb
import httpx

from credit_workbench.common.config import motherduck_token, sec_user_agent

REV_HINTS = ("revenue", "sales", "operatingrevenue", "revenues")
EXCLUDE = ("deferred", "unbilled", "contractwithcustomerliability", "remaining",
           "disaggregat", "costof", "expense", "receivable", "tax", "percentage",
           "unearned", "backlog")

SAMPLE = """
SELECT s.cik, s.company_name, s.sic, s.fy, s.period_end,
       s.total_assets, s.operating_income, s.net_income,
       s.operating_income IS NOT NULL AS is_real_operator
FROM marts.spreads_a s
WHERE s.basis = 'first_reported' AND s.is_primary_annual
  AND s.revenue IS NULL AND s.fy = 2023
  AND substr(s.sic, 1, 2) NOT IN ('60','61','62','63','64','65','67')
  AND ({filter})
ORDER BY coalesce(s.total_assets, 0) DESC
LIMIT {n}
"""


def annual_values(facts: dict, period_end: str) -> list[tuple[str, float, str]]:
    """Every revenue-ish concept SEC publishes with an annual period ending that day."""
    out = []
    for taxonomy in ("us-gaap", "ifrs-full"):
        for concept, body in facts.get("facts", {}).get(taxonomy, {}).items():
            low = concept.lower()
            if not any(h in low for h in REV_HINTS):
                continue
            if any(x in low for x in EXCLUDE):
                continue
            for unit, entries in body.get("units", {}).items():
                if not unit.startswith("USD") and unit not in ("EUR", "JPY", "GBP", "CHF"):
                    continue
                for e in entries:
                    if e.get("end") != period_end or not e.get("start"):
                        continue
                    days = ((_d(e["end"]) - _d(e["start"])).days
                            if e.get("start") else 0)
                    if 300 <= days <= 400:
                        out.append((f"{taxonomy}:{concept}", e.get("val"), unit))
    # largest first: the consolidated total is normally the biggest
    return sorted({(c, v, u) for c, v, u in out if v is not None},
                  key=lambda r: -abs(r[1]))[:4]


def _d(s: str):
    import datetime
    return datetime.date.fromisoformat(s)


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    rows = (con.execute(SAMPLE.format(filter="s.operating_income IS NOT NULL", n=8)).fetchall()
            + con.execute(SAMPLE.format(filter="s.operating_income IS NULL", n=7)).fetchall())
    heads = [d[0] for d in con.description]
    print(f"sampled {len(rows)} non-financial FY2023 company-years with a null revenue\n")

    client = httpx.Client(
        headers={"User-Agent": sec_user_agent(), "Accept-Encoding": "gzip, deflate"},
        timeout=60, follow_redirects=True)

    found = missing = errored = 0
    for r in rows:
        rec = dict(zip(heads, r))
        cik = int(rec["cik"])
        pe = str(rec["period_end"])
        label = f'{rec["company_name"][:38]:<40} SIC {rec["sic"]}  PE {pe}'
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
        try:
            resp = client.get(url)
            if resp.status_code != 200:
                print(f"{label}  -> HTTP {resp.status_code}")
                errored += 1
                continue
            facts = json.loads(resp.content)
        except Exception as exc:
            print(f"{label}  -> fetch failed: {str(exc)[:60]}")
            errored += 1
            continue

        hits = annual_values(facts, pe)
        held = con.execute("""
            SELECT count(*) FROM staging.facts_pit
            WHERE cik = ? AND period_end = ? AND qtrs = 4""",
            [cik, rec["period_end"]]).fetchone()[0]

        if hits:
            found += 1
            print(f"{label}  SEC HAS IT   (we hold {held:,} annual facts)")
            for concept, val, unit in hits:
                print(f"      {concept:<62} {val:>18,.0f} {unit}")
        else:
            missing += 1
            print(f"{label}  no annual revenue concept at SEC either "
                  f"(we hold {held:,} annual facts)")
        time.sleep(0.15)                       # SEC fair-access: stay well under 10/s

    print(f"\nSEC publishes a revenue: {found}   SEC has none either: {missing}   "
          f"errors: {errored}")


if __name__ == "__main__":
    main()
