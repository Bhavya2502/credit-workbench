"""G-08 — what would a disclosed-KPI extractor actually be built on?

The request calls this the single biggest upgrade available to business-risk scorecards, and
it is right: same-store sales, RASM, load factor, ARR, backlog, occupancy and production
volumes are what make a sector scorecard sector-specific rather than a generic ratio set
wearing an industry label. None of it is in XBRL; all of it is in narrative already held, so
nothing needs fetching.

Three unknowns decide the design, and guessing any would waste a long build: whether a value
sits beside its label, which section carries which KPI, and which industries hold enough
companies to be worth a dictionary entry.

**On cost, learned the hard way.** The first version of this file asked each phrase in its
own SELECT and stitched them with UNION ALL - 28 phrases over two sources, so 56 full scans
of a parquet-backed view holding 1.79m sections. It timed out after an hour without
producing a single row. The same 28 counts are one scan when written as FILTER aggregates in
a single SELECT, which is how they are written now. `quali.filing_sections` is a view over
parquet and is not partitioned by item, so every query against it reads the whole narrative
dataset; the number of passes is therefore the only lever that matters.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

# Candidate vocabulary spread across the industries with the most listed companies rather
# than concentrated in one. Plain phrases, not regexes, so the hit rates say something about
# the filers' language and not about my regex writing.
KPI_PHRASES = {
    "retail_comp_sales": "comparable store sales",
    "retail_same_store": "same-store sales",
    "retail_sqft": "selling square feet",
    "retail_store_count": "store count",
    "restaurant_auv": "average unit volume",
    "airline_rasm": "available seat mile",
    "airline_load_factor": "load factor",
    "saas_arr": "annual recurring revenue",
    "saas_nrr": "net revenue retention",
    "saas_rpo": "remaining performance obligations",
    "hotel_revpar": "revpar",
    "hotel_adr": "average daily rate",
    "reit_occupancy": "occupancy rate",
    "reit_ssnoi": "same-store net operating income",
    "reit_ffo": "funds from operations",
    "energy_boe": "barrels of oil equivalent",
    "energy_reserves": "proved reserves",
    "mining_aisc": "all-in sustaining cost",
    "telecom_arpu": "average revenue per user",
    "telecom_churn": "churn rate",
    "utility_mwh": "megawatt hour",
    "mfg_backlog": "backlog",
    "mfg_utilisation": "capacity utilization",
    "semi_asp": "average selling price",
    "health_admissions": "admissions",
}


def one_scan(source: str, where: str) -> str:
    """All phrase counts as FILTER aggregates over a single pass."""
    cols = ",\n           ".join(
        f"count(DISTINCT cik) FILTER (WHERE t LIKE '%{p}%') AS {name}"
        for name, p in KPI_PHRASES.items())
    return (f"WITH s AS (SELECT cik, lower(text) AS t FROM {source} WHERE {where})\n"
            f"SELECT count(DISTINCT cik) AS companies_total,\n           {cols}\nFROM s")


RECENT = "substr(filing_date, 1, 4) BETWEEN '2022' AND '2025'"


def show_wide(con, q, title):
    """Phrase counts come back as one very wide row; print it as a ranked column."""
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    row = cur.fetchone()
    pairs = sorted(zip(heads[1:], row[1:]), key=lambda kv: -(kv[1] or 0))
    print(f"  companies with a section at all: {row[0]:,}")
    print(f"  {'kpi':<24} {'companies':>10} {'% of filers':>12}")
    for name, n in pairs:
        if not n:
            continue
        print(f"  {name:<24} {n:>10,} {100 * n / max(row[0], 1):>11.1f}%")


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    con.execute("SET temp_directory = '/tmp/duckdb_spill'")

    print("\n### 1. KPI language in MD&A (item 7), 2022-2025 — one scan")
    try:
        show_wide(con, one_scan("quali.mdna", RECENT), "mdna")
    except Exception as exc:
        print(f"  (failed: {str(exc)[:190]})")

    print("\n### 2. KPI language in Item 1 Business — a different set should win")
    try:
        show_wide(con, one_scan(
            "(SELECT * FROM quali.filing_sections WHERE item = '1')", RECENT), "item1")
    except Exception as exc:
        print(f"  (failed: {str(exc)[:190]})")

    # The decisive question: is a KPI value on the same line as its label, or has the
    # cell-per-line conversion separated them the way it separated the audit fees?
    print("\n### 3. Does a KPI label sit on the same line as a number?")
    try:
        cur = con.execute("""
            WITH s AS (
                SELECT cik, text FROM quali.mdna
                WHERE substr(filing_date, 1, 4) = '2024'
                  AND regexp_matches(lower(text),
                      'load factor|comparable store sales|occupancy rate|revpar')
                LIMIT 400
            ),
            lines AS (SELECT unnest(str_split(text, chr(10))) AS line FROM s)
            SELECT count(*) AS kpi_lines,
                   count(*) FILTER (WHERE regexp_matches(line, '[0-9]')) AS with_a_digit,
                   count(*) FILTER (WHERE regexp_matches(line, '[0-9]+\\.?[0-9]*\\s*%'))
                       AS with_a_percentage,
                   round(median(length(line)), 0) AS median_line_length,
                   round(100.0 * count(*) FILTER (WHERE regexp_matches(line, '[0-9]'))
                         / count(*), 1) AS pct_with_a_digit
            FROM lines
            WHERE regexp_matches(lower(line),
                'load factor|comparable store sales|occupancy rate|revpar')""")
        heads = [d[0] for d in cur.description]
        for r in cur.fetchall():
            for h, v in zip(heads, r):
                print(f"  {h:<24} {v}")
    except Exception as exc:
        print(f"  (failed: {str(exc)[:190]})")

    print("\n### 4. Real lines, so the shape is read rather than assumed")
    try:
        cur = con.execute("""
            WITH s AS (
                SELECT text FROM quali.mdna
                WHERE substr(filing_date, 1, 4) = '2024'
                  AND regexp_matches(lower(text), 'load factor|revpar|occupancy rate')
                LIMIT 200
            ),
            lines AS (SELECT unnest(str_split(text, chr(10))) AS line FROM s)
            SELECT substr(trim(line), 1, 150) AS line FROM lines
            WHERE regexp_matches(lower(line), 'load factor|revpar|occupancy rate')
              AND length(trim(line)) BETWEEN 12 AND 150
            LIMIT 18""")
        for (ln,) in cur.fetchall():
            print(f"  | {ln}")
    except Exception as exc:
        print(f"  (failed: {str(exc)[:190]})")

    print("\n### 5. Where the companies are — build the dictionary where it pays")
    try:
        cur = con.execute("""
            SELECT r.sic2, any_value(c.sic_description) AS example,
                   count(DISTINCT r.cik) AS companies
            FROM marts.ratio_values r
            LEFT JOIN ref.dim_company c ON c.cik = r.cik
            WHERE r.fy = 2024
            GROUP BY r.sic2 ORDER BY companies DESC LIMIT 18""")
        for r in cur.fetchall():
            print(f"  {r[0]:<6} {str(r[1])[:52]:<54} {r[2]:>6,}")
    except Exception as exc:
        print(f"  (failed: {str(exc)[:190]})")


if __name__ == "__main__":
    main()
