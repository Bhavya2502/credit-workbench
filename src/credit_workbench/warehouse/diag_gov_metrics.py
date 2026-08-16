"""Tracker G3, third probe — does the metric extractor hold up on real proxies?

Runs the whole chain the way the pipeline will - fetch, convert with rows intact, split
into sections, extract metrics - but against freshly fetched documents, so the extractor
is answered for before an ingest commits storage to the lake.

The one number that matters here is the share of filings whose fee components sum to the
total the filer published. The first version of this reader scored 6 ties against 16
mismatches, because it took the first row matching each label anywhere in the document
and so collected labels from vote tabulations and from the Rule 0-11 fee on the cover
page. That is the failure mode this project keeps meeting: the output looked like a fee
table in every case.

Coverage is reported per metric as well, because a metric that is right whenever it fires
and fires on a tenth of filings is a different proposition from one that fires on most,
and the scorecard needs to know which it is holding.
"""
from __future__ import annotations

import asyncio

import duckdb

from credit_workbench.common.config import motherduck_token
from credit_workbench.common.html_text import to_rows
from credit_workbench.ingest.proxy_sections import split_sections
from credit_workbench.transform.governance import (COMPONENTS, best_fee_block,
                                                   metrics_for_filing)
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

    # fetch_all keeps the raw markup and a cell-per-line conversion of it. Convert again
    # with rows intact, exactly as the ingest job does, or this would measure a
    # different pipeline from the one being shipped.
    scored = []
    for d in docs:
        secs = split_sections(to_rows(d.pop("_html")))
        scored.append((d, secs, metrics_for_filing(
            secs, {"cik": d["cik"], "adsh": d["adsh"], "form": "DEF 14A",
                   "filing_date": "", "period_of_report": ""})))

    n = len(scored)
    print(f"\n### 1. Coverage per metric, over {n} proxies")
    def pct(f):
        return f"{100 * sum(1 for _, _, m in scored if f(m)) / n:.0f}%"
    for label, f in (
            ("sections found (median)", None),
            ("audit fee figure", lambda m: m.get("audit_fees") is not None),
            ("all four fee categories",
             lambda m: m.get("fee_components_sum") is not None),
            ("stated total", lambda m: m.get("total_fees_stated") is not None),
            ("non-audit ratio", lambda m: m.get("non_audit_fee_ratio") is not None),
            ("director table", lambda m: m.get("directors_listed") is not None),
            ("directors marked independent",
             lambda m: m.get("directors_marked_independent") is not None),
            ("independence statement",
             lambda m: m.get("independence_statement") is not None),
            ("CEO pay ratio", lambda m: m.get("ceo_pay_ratio") is not None),
            ("related-party section", lambda m: m.get("related_party_chars") is not None),
            ("clawback policy", lambda m: m.get("has_clawback_policy")),
    ):
        if f is None:
            per = sorted(m["sections_found"] for _, _, m in scored)
            print(f"  {label:<30} {per[len(per)//2]}")
            continue
        print(f"  {label:<30} {pct(f):>5}")

    print("\n### 2. Do the fee components sum to the stated total?")
    ties = off = 0
    for d, _, m in scored:
        s, t = m.get("fee_components_sum"), m.get("total_fees_stated")
        if s is None or not t:
            continue
        if abs(s - t) <= max(1.0, 0.005 * t):
            ties += 1
        else:
            off += 1
            if off <= 8:
                print(f"    OFF  {d['adsh']}  parts={s:,.0f}  total={t:,.0f}  "
                      f"units={m.get('fee_units')}  from={m.get('fee_source_section')}")
    total = ties + off
    print(f"  ties {ties}  off {off}"
          + (f"   ({100*ties/total:.0f}% tying)" if total else ""))

    print("\n### 3. Which section did the fee table come from?")
    src: dict[str, int] = {}
    for _, _, m in scored:
        if m.get("fee_source_section"):
            src[m["fee_source_section"]] = src.get(m["fee_source_section"], 0) + 1
    for k, v in sorted(src.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<16} {v}")
    print("  (spread confirms the table is found by its shape, not its heading)")

    print("\n### 4. Fee magnitudes and units")
    vals = sorted(m["audit_fees"] for _, _, m in scored if m.get("audit_fees"))
    if vals:
        print(f"  audit fees  n={len(vals)}  min={vals[0]:,.0f}  "
              f"median={vals[len(vals)//2]:,.0f}  max={vals[-1]:,.0f}")
        print(f"  under 10,000 after scaling: {sum(1 for v in vals if v < 10_000)} "
              f"(should be near nil — a cluster here means a missed units note)")
    units: dict[str, int] = {}
    for _, _, m in scored:
        if m.get("fee_units"):
            units[m["fee_units"]] = units.get(m["fee_units"], 0) + 1
    print(f"  units detected: {units}")

    print("\n### 5. Boards, where a director table was found")
    dl = sorted(m["directors_listed"] for _, _, m in scored
                if m.get("directors_listed"))
    if dl:
        print(f"  n={len(dl)}  min={dl[0]}  median={dl[len(dl)//2]}  max={dl[-1]}")
    bad = [(d["adsh"], m["directors_marked_independent"], m["directors_listed"])
           for d, _, m in scored
           if m.get("directors_marked_independent") is not None
           and m.get("directors_listed")
           and m["directors_marked_independent"] > m["directors_listed"]]
    print(f"  marked independent exceeding listed: {len(bad)} (must be 0)")
    for adsh, mk, ls in bad[:4]:
        print(f"    {adsh}  marked={mk} listed={ls}")

    print("\n### 6. Independence and pay ratio, as extracted")
    for d, _, m in scored[:6]:
        print(f"\n    --- {d['adsh']}")
        print(f"      directors={m.get('directors_listed')} "
              f"independent={m.get('directors_marked_independent')} "
              f"pay_ratio={m.get('ceo_pay_ratio')}")
        st = m.get("independence_statement")
        print(f"      statement: {(st[:150] if st else None)!r}")

    print("\n### 7. Filings with no fee table at all — is there one to find?")
    misses = [(d, s) for d, s, m in scored if m.get("audit_fees") is None]
    print(f"  {len(misses)} of {n}")
    for d, secs in misses[:4]:
        anywhere = any(best_fee_block(t) for t in secs.values())
        has_label = any("audit fee" in t.lower() for t in secs.values())
        print(f"    {d['adsh']}  sections={len(secs)}  "
              f"fee label present={has_label}  block found anywhere={anywhere}")

    print(f"\n(components tracked: {', '.join(COMPONENTS)})")


if __name__ == "__main__":
    main()
