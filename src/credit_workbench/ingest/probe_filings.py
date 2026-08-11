"""Look at real 10-K documents before building anything to parse them.

Everything so far has come from XBRL, where the shape is declared. The narrative
sections are the opposite: HTML written by each filer's counsel, with no schema. So
before writing a section splitter, establish on actual documents -

  scale       how many 10-Ks, how big, and therefore how long a backfill takes at
              SEC's fair-access rate
  addressing  whether `primaryDocument` in the filing index resolves to the document,
              which would save one request per filing over reading an index
  splitting   whether "Item 1A." style headers can actually be found, and how badly
              the table of contents duplicates them - the classic trap, since a naive
              first-match split returns the contents page instead of the section
  exhibits    what document types a filing carries, and how many filings would have to
              be enumerated to find credit agreements

No parsing library is declared in this project, so the probe also checks whether a
plain-stdlib strip is good enough or whether a dependency is warranted.
"""
from __future__ import annotations

import re
import time
from html.parser import HTMLParser

import duckdb
import httpx

from credit_workbench.common.config import motherduck_token, sec_user_agent

ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
# SEC asks for no more than 10 requests a second; this probe stays well under.
PAUSE = 0.2


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
        out = "".join(self.parts)
        out = out.replace("\xa0", " ")
        out = re.sub(r"[ \t]+", " ", out)
        return re.sub(r"\n\s*\n+", "\n", out).strip()


# Deliberately loose: find every candidate, then judge which is the real heading by
# how much text follows it. Filers write "Item 1A.", "ITEM 1A -", "Item 1A:" and more.
ITEM_RE = re.compile(
    r"^\s*item\s*(1A|1B|1|2|3|4|5|6|7A|7|8|9A|9B|9|10|11|12|13|14|15)\s*[.:\-–—]?\s*(.{0,80})",
    re.IGNORECASE | re.MULTILINE)


def fetch(client: httpx.Client, url: str) -> httpx.Response:
    time.sleep(PAUSE)
    return client.get(url)


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")

    print("### 1. How many annual reports, and how big is the backfill?")
    rows = con.execute("""
        SELECT CASE WHEN form LIKE '10-K%' THEN '10-K family'
                    WHEN form LIKE '20-F%' THEN '20-F (foreign issuers)'
                    ELSE form END AS form_group,
               count(*) AS filings,
               count(*) FILTER (WHERE primaryDocument IS NOT NULL
                                  AND primaryDocument <> '') AS with_primary_doc,
               round(sum(TRY_CAST(size AS BIGINT)) / 1e9, 1) AS total_submission_gb
        FROM ref.filing_index
        WHERE form LIKE '10-K%' OR form LIKE '20-F%'
        GROUP BY 1 ORDER BY filings DESC""").fetchall()
    for r in rows:
        print(f"  {r[0]:<24} filings={r[1]:>8,}  with primaryDocument={r[2]:>8,}  "
              f"full submissions={r[3]} GB")

    print("\n### 2. 10-K filings by year")
    for year, n in con.execute("""
        SELECT year(TRY_CAST(filing_date AS DATE)) AS y, count(*) AS n
        FROM ref.filing_index WHERE form = '10-K'
        GROUP BY 1 HAVING y >= 2009 ORDER BY 1""").fetchall():
        print(f"  {year}  {n:>7,}")

    print("\n### 3. How many filings would an exhibit sweep have to enumerate?")
    for label, n in con.execute("""
        SELECT '8-K with item 1.01 (material definitive agreement)' AS what, count(*)
        FROM ref.filing_index WHERE form LIKE '8-K%' AND items LIKE '%1.01%'
        UNION ALL SELECT '10-K family (exhibit index each year)', count(*)
        FROM ref.filing_index WHERE form LIKE '10-K%'
        UNION ALL SELECT '10-Q family', count(*)
        FROM ref.filing_index WHERE form LIKE '10-Q%'""").fetchall():
        print(f"  {label:<52} {n:>10,}")

    # ---------------------------------------------------------------- live fetches
    samples = con.execute("""
        SELECT cik, accession_number, primaryDocument, filing_date,
               TRY_CAST(size AS BIGINT) AS size
        FROM ref.filing_index
        WHERE form = '10-K' AND primaryDocument LIKE '%.htm'
          AND filing_date >= '2023-01-01'
        USING SAMPLE 4 ROWS""").fetchall()

    headers = {"User-Agent": sec_user_agent(), "Accept-Encoding": "gzip, deflate"}
    with httpx.Client(headers=headers, timeout=120, follow_redirects=True) as client:
        for cik, adsh, doc, filed, size in samples:
            nodash = str(adsh).replace("-", "")
            url = f"{ARCHIVES}/{int(cik)}/{nodash}/{doc}"
            print(f"\n### 4. Sample 10-K  cik={cik}  filed={filed}")
            print(f"  {url}")
            try:
                resp = fetch(client, url)
            except Exception as exc:  # noqa: BLE001
                print(f"  FETCH FAILED: {exc}")
                continue
            print(f"  HTTP {resp.status_code}  {len(resp.content)/1e6:.2f} MB html")
            if resp.status_code != 200:
                continue
            parser = TextExtractor()
            parser.feed(resp.text)
            text = parser.text()
            print(f"  -> {len(text)/1000:.0f}k chars of text "
                  f"({100*len(text)/max(len(resp.content), 1):.0f}% of html bytes)")

            hits: dict[str, list[int]] = {}
            for m in ITEM_RE.finditer(text):
                hits.setdefault(m.group(1).upper(), []).append(m.start())
            for item in ("1", "1A", "3", "7", "7A", "9A"):
                pos = hits.get(item, [])
                if not pos:
                    print(f"  Item {item:<3} NOT FOUND")
                    continue
                gaps = [(pos[i + 1] - pos[i]) if i + 1 < len(pos)
                        else len(text) - pos[i] for i in range(len(pos))]
                print(f"  Item {item:<3} {len(pos)} occurrence(s); "
                      f"largest following block {max(gaps)/1000:.0f}k chars "
                      f"at occurrence #{gaps.index(max(gaps)) + 1}")

        # What does a filing actually contain, document by document?
        cik, adsh = samples[0][0], samples[0][1]
        nodash = str(adsh).replace("-", "")
        print(f"\n### 5. Documents inside one filing (index.json), cik={cik}")
        try:
            resp = fetch(client, f"{ARCHIVES}/{int(cik)}/{nodash}/index.json")
            items = resp.json()["directory"]["item"]
            print(f"  {len(items)} documents")
            for it in sorted(items, key=lambda d: -int(d.get("size") or 0))[:18]:
                print(f"    {str(it.get('type') or '-'):<12} "
                      f"{int(it.get('size') or 0)/1000:>9,.0f} KB  {it['name'][:52]}")
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED: {exc}")


if __name__ == "__main__":
    main()
