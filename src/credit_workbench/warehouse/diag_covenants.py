"""How are covenants actually written, before anything tries to parse them?

The workbench holds 17,867 debt agreements and not one covenant level. Extracting them
is the point of having the corpus, but a parser written from imagination will produce
plausible numbers that are wrong, which is worse than none - a leverage covenant read
from the wrong sentence still looks like a leverage covenant.

So: look at the text first. Establish how filers phrase the level ("not greater than
4.00 to 1.00", "shall not exceed 4.00:1.00", "at least 1.00 to 1.00"), whether the
covenant name sits close enough to the number to be tied to it, how often a covenant
steps down over time so a single number would be wrong, and whether ratios appear in
places that are not covenants at all.
"""
from __future__ import annotations

import re

import duckdb

from credit_workbench.common.config import motherduck_token

# A ratio as agreements write it: 4.00 to 1.00, 4.00:1.00, 4.0x
RATIO_RE = re.compile(
    r"(\d{1,2}(?:\.\d{1,3})?)\s*(?::|\s+to\s+|\s*/\s*)\s*1(?:\.0{1,3})?\b",
    re.IGNORECASE)

COVENANT_NAMES = [
    ("total leverage", r"(consolidated\s+)?(total\s+)?(net\s+)?leverage ratio"),
    ("senior secured leverage", r"senior secured (net )?leverage ratio"),
    ("first lien leverage", r"first lien (net )?leverage ratio"),
    ("interest coverage", r"interest (expense )?coverage ratio"),
    ("fixed charge coverage", r"fixed charge coverage ratio"),
    ("debt service coverage", r"debt service coverage ratio"),
    ("current ratio", r"current ratio"),
    ("minimum liquidity", r"minimum liquidity|liquidity covenant"),
    ("minimum EBITDA", r"minimum (consolidated )?ebitda"),
    ("net worth", r"(tangible )?net worth"),
]

DIRECTION_RE = re.compile(
    r"(not (?:be )?(?:greater|more) than|shall not exceed|no greater than|not in excess of"
    r"|not (?:be )?less than|at least|equal to or greater than|no less than)",
    re.IGNORECASE)


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")

    print("### 1. How many agreements even have a financial covenant section?")
    for row in con.execute("""
        SELECT doc_kind, count(*) AS docs,
               round(100.0 * count(*) FILTER (
                   WHERE lower(text) LIKE '%financial covenant%'), 1) / count(*) * 100
                   AS pct_financial_covenant,
               round(100.0 * count(*) FILTER (
                   WHERE lower(text) LIKE '%leverage ratio%') / count(*), 1) AS pct_leverage,
               round(100.0 * count(*) FILTER (
                   WHERE lower(text) LIKE '%fixed charge coverage%') / count(*), 1)
                   AS pct_fcc
        FROM quali.debt_agreements
        GROUP BY 1 ORDER BY docs DESC""").fetchall():
        print(f"  {row[0]:<20} docs={row[1]:>6,}  leverage={row[3]:>5}%  fcc={row[4]:>5}%")

    print("\n### 2. Which covenant names appear, and how often")
    for label, pattern in COVENANT_NAMES:
        n = con.execute(f"""
            SELECT count(*) FROM quali.debt_agreements
            WHERE doc_kind IN ('credit_agreement', 'amendment')
              AND regexp_matches(lower(text), '{pattern}')""").fetchone()[0]
        print(f"  {label:<26} {n:>6,} agreements")

    # Pull a handful of real agreements and look at the sentences around the ratios.
    print("\n### 3. Real covenant sentences, as written")
    docs = con.execute("""
        SELECT adsh, cik, doc_kind, text FROM quali.debt_agreements
        WHERE doc_kind = 'credit_agreement'
          AND lower(text) LIKE '%leverage ratio%'
          AND lower(text) LIKE '%financial covenant%'
        ORDER BY hash(adsh) LIMIT 6""").fetchall()

    shown = 0
    for adsh, cik, kind, text in docs:
        low = text.lower()
        for m in RATIO_RE.finditer(text):
            start = max(0, m.start() - 260)
            window = text[start:m.end() + 60].replace("\n", " ")
            window = re.sub(r"\s+", " ", window)
            if not re.search(r"leverage ratio|coverage ratio", window, re.IGNORECASE):
                continue
            direction = DIRECTION_RE.search(window)
            print(f"\n  [{cik}] ratio={m.group(1)}  "
                  f"direction={direction.group(0) if direction else 'NONE FOUND'}")
            print(f"    ...{window[-240:]}")
            shown += 1
            if shown >= 12:
                break
        if shown >= 12:
            break

    print("\n### 4. Do covenants step down over time? (several levels in one agreement)")
    rows = con.execute("""
        SELECT adsh, text FROM quali.debt_agreements
        WHERE doc_kind = 'credit_agreement' AND lower(text) LIKE '%leverage ratio%'
        ORDER BY hash(adsh) LIMIT 40""").fetchall()
    counts = []
    for _adsh, text in rows:
        near = set()
        for m in RATIO_RE.finditer(text):
            w = text[max(0, m.start() - 200):m.end()]
            if re.search(r"leverage ratio", w, re.IGNORECASE):
                near.add(m.group(1))
        counts.append(len(near))
    counts.sort()
    if counts:
        print(f"  agreements sampled: {len(counts)}")
        print(f"  distinct leverage levels each: median={counts[len(counts)//2]}, "
              f"max={counts[-1]}, "
              f"share with more than one={100*sum(1 for c in counts if c > 1)/len(counts):.0f}%")

    print("\n### 5. Where else do ratios appear? (false-positive risk)")
    sample = docs[0][3] if docs else ""
    contexts: dict[str, int] = {}
    for m in RATIO_RE.finditer(sample):
        w = sample[max(0, m.start() - 120):m.end()].lower()
        for key in ("leverage ratio", "coverage ratio", "pricing", "applicable margin",
                    "pro forma", "definition", "exhibit", "schedule"):
            if key in w:
                contexts[key] = contexts.get(key, 0) + 1
                break
        else:
            contexts["(no recognised context)"] = contexts.get("(no recognised context)", 0) + 1
    for key, n in sorted(contexts.items(), key=lambda kv: -kv[1]):
        print(f"  {key:<28} {n}")


if __name__ == "__main__":
    main()
