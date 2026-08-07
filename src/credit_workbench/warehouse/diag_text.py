"""Section G diagnostic — what is actually in the footnote text we already hold?

8.9 GB of tagged text blocks are sitting in the lake unprocessed. Before building a
corpus on them, two things need establishing from the data rather than assumed:

1. Is the text complete? The SEC file carries both `srclen` (how long the text was in
   the filing) and `txtlen` (how much was stored). If those differ the corpus is
   truncated, and any analysis built on it inherits the truncation silently.
2. Which notes are actually tagged, and for how many filers — a corpus that only
   covers a third of companies is a different proposition from one that covers all.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

PERIOD = "2026_06"

Q: list[tuple[str, str]] = [
    ("1. TRUNCATION — is the stored text the whole text?", f"""
        SELECT count(*) AS text_blocks,
               count(*) FILTER (WHERE TRY_CAST(txtlen AS BIGINT)
                                    < TRY_CAST(srclen AS BIGINT)) AS truncated,
               round(100.0 * count(*) FILTER (WHERE TRY_CAST(txtlen AS BIGINT)
                                                  < TRY_CAST(srclen AS BIGINT))
                     / count(*), 1)                                AS pct_truncated,
               round(avg(TRY_CAST(srclen AS BIGINT)))              AS avg_source_chars,
               round(avg(TRY_CAST(txtlen AS BIGINT)))              AS avg_stored_chars,
               max(TRY_CAST(txtlen AS BIGINT))                     AS max_stored_chars
        FROM raw.fsn_txt WHERE period = '{PERIOD}'"""),

    ("2. Stored-length distribution — is there a hard cap?", f"""
        SELECT CASE WHEN TRY_CAST(txtlen AS BIGINT) < 500 THEN 'a. under 500'
                    WHEN TRY_CAST(txtlen AS BIGINT) < 2048 THEN 'b. 500-2047'
                    WHEN TRY_CAST(txtlen AS BIGINT) = 2048 THEN 'c. exactly 2048'
                    WHEN TRY_CAST(txtlen AS BIGINT) < 10000 THEN 'd. 2049-9999'
                    ELSE 'e. 10000+' END AS stored_length,
               count(*) AS blocks
        FROM raw.fsn_txt WHERE period = '{PERIOD}' GROUP BY 1 ORDER BY 1"""),

    ("3. Which notes are tagged, and how widely", f"""
        SELECT tag, count(DISTINCT adsh) AS filings,
               round(avg(TRY_CAST(srclen AS BIGINT))) AS avg_source_chars
        FROM raw.fsn_txt
        WHERE period = '{PERIOD}' AND tag LIKE '%TextBlock'
        GROUP BY 1 ORDER BY filings DESC LIMIT 25"""),

    ("4. Credit-relevant notes specifically", f"""
        SELECT CASE WHEN tag LIKE '%Debt%' THEN 'Debt'
                    WHEN tag LIKE '%Lease%' THEN 'Leases'
                    WHEN tag LIKE '%CommitmentsAndContingencies%' THEN 'Commitments & contingencies'
                    WHEN tag LIKE '%Pension%' OR tag LIKE '%Retirement%' THEN 'Pension / OPEB'
                    WHEN tag LIKE '%SegmentReporting%' THEN 'Segments'
                    WHEN tag LIKE '%RelatedParty%' THEN 'Related party'
                    WHEN tag LIKE '%Concentration%' OR tag LIKE '%Risk%' THEN 'Risks & concentrations'
                    WHEN tag LIKE '%FairValue%' THEN 'Fair value'
                    WHEN tag LIKE '%IncomeTax%' THEN 'Income taxes'
                    WHEN tag LIKE '%Subsequent%' THEN 'Subsequent events'
                    WHEN tag LIKE '%GoingConcern%' OR tag LIKE '%Liquidity%' THEN 'Going concern / liquidity'
               END AS note_type,
               count(DISTINCT adsh) AS filings, count(*) AS blocks
        FROM raw.fsn_txt
        WHERE period = '{PERIOD}' AND tag LIKE '%TextBlock' AND note_type IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC"""),

    ("5. Coverage — share of 10-K filers with any tagged note text", f"""
        SELECT count(DISTINCT s.adsh) AS ten_k_filings,
               count(DISTINCT t.adsh) AS with_note_text,
               round(100.0 * count(DISTINCT t.adsh) / count(DISTINCT s.adsh), 1) AS pct
        FROM raw.fsn_sub s
        LEFT JOIN raw.fsn_txt t ON t.adsh = s.adsh AND t.period = s.period
                               AND t.tag LIKE '%TextBlock'
        WHERE s.period = '{PERIOD}' AND s.form = '10-K'"""),

    ("6. Sample — the opening of a real commitments note", f"""
        SELECT substr(value, 1, 600) AS opening_600_chars
        FROM raw.fsn_txt
        WHERE period = '{PERIOD}' AND tag = 'CommitmentsAndContingenciesDisclosureTextBlock'
          AND TRY_CAST(txtlen AS BIGINT) > 3000
        LIMIT 1"""),
]


def show(con, query: str) -> None:
    cur = con.execute(query)
    headers = [d[0] for d in cur.description]
    rows = [[("" if v is None else str(v))[:200] for v in r] for r in cur.fetchall()]
    if not rows:
        print("  (no rows)")
        return
    widths = [min(max(len(h), *(len(r[i]) for r in rows)), 200)
              for i, h in enumerate(headers)]
    print("  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        print("  " + "  ".join(v.ljust(w) for v, w in zip(r, widths)))


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    for title, query in Q:
        print(f"\n### {title}")
        try:
            show(con, query)
        except Exception as exc:  # noqa: BLE001
            print(f"  (failed: {exc})")


if __name__ == "__main__":
    main()
