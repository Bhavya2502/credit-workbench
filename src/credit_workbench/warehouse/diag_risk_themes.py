"""G-09 — can risk factors be classified by theme, and on what?

The request is for `marts.risk_themes` at grain `cik × filing × theme`, plus a
`theme × sic2 × fy` aggregate, with the verbatim heading kept so any count is checkable
back to the filing. The raw material is `quali.risk_factors`, which is already held.

**The question that decides precision is where to classify.** Every Item 1A section of any
length mentions competition, regulation and cyber security somewhere - classifying body text
would mark nearly every issuer with nearly every theme and the aggregate would be a table of
90% figures saying nothing. Risk factors are conventionally written as a list of individually
headed items, and if those headings survive the conversion then classifying *them* gives one
theme per actual disclosed risk rather than one per passing mention.

That is the same question the proxy splitter turned on, and it was decisive there: heading
lines carried the structure and body text did not.

So this measures whether short heading-like lines exist in Item 1A, how many per section,
and what they say - before any vocabulary is written. It also counts theme keywords over
the body for comparison, so the difference between the two is visible rather than argued.

Cost discipline carried over from G-08, which timed out twice before it was learned: one
scan, all counts as FILTER aggregates in a single SELECT, and the line-shape questions
sampled rather than run over the full population.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

# Credit-relevant risk themes. Deliberately broad at this stage - the point is to find out
# which ones the filers themselves separate out, not to impose a taxonomy.
THEMES = {
    "cyber": "cyber",
    "supply_chain": "supply chain",
    "regulation": "regulatory",
    "litigation": "litigation",
    "climate": "climate",
    "interest_rates": "interest rate",
    "liquidity": "liquidity",
    "inflation": "inflation",
    "labour": "labor shortage",
    "competition": "competition",
    "customer_concentration": "customer concentration",
    "key_personnel": "key personnel",
    "pandemic": "pandemic",
    "geopolitical": "geopolitical",
    "currency": "foreign currency",
    "intellectual_property": "intellectual property",
    "acquisitions": "acquisitions",
    "impairment": "impairment",
    "indebtedness": "indebtedness",
    "artificial_intelligence": "artificial intelligence",
}


def one_scan(source: str, where: str, col: str = "text") -> str:
    cols = ",\n           ".join(
        f"count(DISTINCT cik) FILTER (WHERE t LIKE '%{p}%') AS {name}"
        for name, p in THEMES.items())
    return (f"WITH s AS (SELECT cik, lower({col}) AS t FROM {source} WHERE {where})\n"
            f"SELECT count(DISTINCT cik) AS companies,\n           {cols}\nFROM s")


RECENT = "substr(filing_date, 1, 4) BETWEEN '2022' AND '2025'"


def show_ranked(con, q, total_label="companies"):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    row = cur.fetchone()
    total = row[0] or 1
    print(f"  {total_label}: {total:,}")
    print(f"  {'theme':<26} {'companies':>10} {'% of filers':>12}")
    for name, n in sorted(zip(heads[1:], row[1:]), key=lambda kv: -(kv[1] or 0)):
        print(f"  {name:<26} {n:>10,} {100 * (n or 0) / total:>11.1f}%")


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    con.execute("SET temp_directory = '/tmp/duckdb_spill'")

    print("### 1. How much risk-factor text is there?")
    try:
        cur = con.execute("""
            SELECT count(*) AS sections, count(DISTINCT cik) AS companies,
                   count(DISTINCT substr(filing_date, 1, 4)) AS years,
                   round(median(char_len), 0) AS median_chars,
                   round(max(char_len), 0) AS max_chars
            FROM quali.risk_factors""")
        for h, v in zip([d[0] for d in cur.description], cur.fetchone()):
            print(f"  {h:<20} {v:,}")
    except Exception as exc:
        print(f"  (failed: {str(exc)[:190]})")

    # If body-text classification marks nearly everyone with nearly everything, it is
    # useless as a discriminator and headings are the only route.
    print("\n### 2. Theme keywords over the whole section body, 2022-2025")
    try:
        show_ranked(con, one_scan("quali.risk_factors", RECENT))
    except Exception as exc:
        print(f"  (failed: {str(exc)[:190]})")

    # Do individually headed risk items survive the conversion? A heading is a short line
    # followed by a run of prose - the same test that carried the proxy splitter.
    print("\n### 3. Do risk factors carry per-item headings?")
    try:
        cur = con.execute("""
            WITH s AS (
                SELECT cik, text FROM quali.risk_factors
                WHERE substr(filing_date, 1, 4) = '2024' AND char_len > 20000
                LIMIT 300
            ),
            lines AS (
                SELECT cik, trim(unnest(str_split(text, chr(10)))) AS line FROM s
            ),
            classified AS (
                SELECT cik, line, length(line) AS len,
                       length(line) BETWEEN 25 AND 180 AS heading_shaped,
                       regexp_matches(line, '[.!?]\\s*$') AS ends_sentence
                FROM lines WHERE length(line) > 0
            )
            SELECT count(*) AS lines,
                   count(*) FILTER (WHERE heading_shaped) AS heading_shaped,
                   count(*) FILTER (WHERE heading_shaped AND ends_sentence)
                       AS heading_shaped_ending_in_stop,
                   round(count(*) FILTER (WHERE heading_shaped)
                         / count(DISTINCT cik), 1) AS heading_lines_per_section,
                   round(median(len), 0) AS median_line_length
            FROM classified""")
        for h, v in zip([d[0] for d in cur.description], cur.fetchone()):
            print(f"  {h:<32} {v:,}")
    except Exception as exc:
        print(f"  (failed: {str(exc)[:190]})")

    print("\n### 4. What do those candidate headings actually say?")
    try:
        cur = con.execute("""
            WITH s AS (
                SELECT text FROM quali.risk_factors
                WHERE substr(filing_date, 1, 4) = '2024' AND char_len > 30000
                LIMIT 60
            ),
            lines AS (SELECT trim(unnest(str_split(text, chr(10)))) AS line FROM s)
            SELECT substr(line, 1, 145) AS heading FROM lines
            WHERE length(line) BETWEEN 40 AND 180
              AND regexp_matches(line, '^[A-Z]')
            LIMIT 22""")
        for (ln,) in cur.fetchall():
            print(f"  | {ln}")
    except Exception as exc:
        print(f"  (failed: {str(exc)[:190]})")

    # The aggregate the request wants is theme x sic2 x fy. Whether that is informative
    # depends on themes varying by industry; if every industry shows the same profile the
    # aggregate is decoration.
    print("\n### 5. Do themes vary by industry, or is every profile the same?")
    try:
        cur = con.execute("""
            WITH ind AS (SELECT DISTINCT cik, sic2 FROM marts.ratio_values WHERE fy >= 2022),
            s AS (
                SELECT r.cik, i.sic2, lower(r.text) AS t
                FROM quali.risk_factors r JOIN ind i ON i.cik = r.cik
                WHERE substr(r.filing_date, 1, 4) = '2024')
            SELECT sic2, count(DISTINCT cik) AS companies,
                   round(100.0 * count(DISTINCT cik) FILTER (WHERE t LIKE '%supply chain%')
                         / count(DISTINCT cik), 0) AS supply_chain,
                   round(100.0 * count(DISTINCT cik) FILTER (WHERE t LIKE '%climate%')
                         / count(DISTINCT cik), 0) AS climate,
                   round(100.0 * count(DISTINCT cik) FILTER (WHERE t LIKE '%interest rate%')
                         / count(DISTINCT cik), 0) AS interest_rates,
                   round(100.0 * count(DISTINCT cik) FILTER (WHERE t LIKE '%cyber%')
                         / count(DISTINCT cik), 0) AS cyber
            FROM s GROUP BY sic2 HAVING count(DISTINCT cik) >= 25
            ORDER BY companies DESC LIMIT 14""")
        heads = [d[0] for d in cur.description]
        print("  " + "  ".join(f"{h:<15}" for h in heads))
        for r in cur.fetchall():
            print("  " + "  ".join(f"{v!s:<15}" for v in r))
    except Exception as exc:
        print(f"  (failed: {str(exc)[:190]})")


if __name__ == "__main__":
    main()
