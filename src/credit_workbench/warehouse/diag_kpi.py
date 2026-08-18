"""G-08 — what would a disclosed-KPI extractor actually be built on?

The request calls this the single biggest upgrade available to business-risk scorecards, and
it is right: same-store sales, RASM, load factor, ARR, backlog, occupancy and production
volumes are what make a sector scorecard sector-specific rather than a generic ratio set
wearing an industry label. None of it is in XBRL. All of it is in narrative this warehouse
already holds, so nothing needs fetching.

Three unknowns decide the design, and guessing any of them would waste a long build.

**Where the numbers live.** MD&A is the obvious home but Item 1 Business carries store
counts, capacity and segment descriptions, and Item 7 carries the year-on-year comparison.
Both are held; which one to read is measurable rather than arguable.

**Whether a value sits beside its label.** This is the audit-fee problem again, and it
decided that whole build: `quali.filing_sections` is converted cell-per-line, so a KPI
disclosed in a table arrives with its label and its number on separate lines. If that is the
common case here too, the extractor needs the row-preserving converter and the section text
has to be re-derived - which is a far larger job than a regex over what is already stored.
Better to know now than after writing the dictionary.

**Which industries pay for the effort.** The dictionary is built industry by industry, so it
should start where the companies are. Coverage is counted against `marts.ratio_values`,
because a KPI for a company with no financials cannot feed a scorecard.

Deliberately cheap: filtered to 2019 onward and to one section per filing, because G-24 is a
complaint about compute and this is a text scan over tens of thousands of documents.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

# Candidate vocabulary, spread across the industries with the most listed companies rather
# than concentrated in one. Kept as phrases, not regexes, so the hit rates below say
# something about the language and not about my regex writing.
KPI_PHRASES = {
    "retail_comp_sales": "comparable store sales",
    "retail_same_store": "same-store sales",
    "retail_sqft": "square feet of selling space",
    "retail_store_count": "store count",
    "restaurant_auv": "average unit volume",
    "airline_rasm": "revenue per available seat mile",
    "airline_casm": "cost per available seat mile",
    "airline_load_factor": "load factor",
    "airline_asm": "available seat miles",
    "saas_arr": "annual recurring revenue",
    "saas_nrr": "net revenue retention",
    "saas_backlog": "remaining performance obligations",
    "hotel_revpar": "revpar",
    "hotel_adr": "average daily rate",
    "reit_occupancy": "occupancy rate",
    "reit_noi": "same-store net operating income",
    "reit_ffo": "funds from operations",
    "energy_production": "barrels of oil equivalent",
    "energy_reserves": "proved reserves",
    "mining_aisc": "all-in sustaining cost",
    "telecom_arpu": "average revenue per user",
    "telecom_churn": "churn rate",
    "telecom_subs": "subscribers",
    "utility_mwh": "megawatt hours",
    "manufacturing_backlog": "backlog",
    "manufacturing_utilisation": "capacity utilization",
    "healthcare_admissions": "admissions",
    "semi_asp": "average selling price",
}

# Built once: the phrase hit rate per section type, as a UNION so it is one scan each.
def phrase_query(source: str, where: str) -> str:
    parts = [
        f"SELECT '{name}' AS kpi, "
        f"count(*) FILTER (WHERE lower(text) LIKE '%{phrase}%') AS sections_mentioning, "
        f"count(DISTINCT cik) FILTER (WHERE lower(text) LIKE '%{phrase}%') AS companies "
        f"FROM {source} WHERE {where}"
        for name, phrase in KPI_PHRASES.items()
    ]
    return " UNION ALL ".join(parts) + " ORDER BY companies DESC"


RECENT = "substr(filing_date, 1, 4) BETWEEN '2019' AND '2025'"

Q = [
    ("1. How much narrative is there to read, by section?", """
        SELECT item, count(*) AS sections, count(DISTINCT cik) AS companies,
               round(median(char_len), 0) AS median_chars
        FROM quali.filing_sections
        WHERE item IN ('1', '7', '7A') AND substr(filing_date, 1, 4) >= '2019'
        GROUP BY 1 ORDER BY 1"""),

    ("2. Which KPIs appear in MD&A (item 7) at all?", phrase_query("quali.mdna", RECENT)),

    ("3. And in Item 1 Business — a different set should win there", phrase_query(
        "(SELECT * FROM quali.filing_sections WHERE item = '1')", RECENT)),

    # The decisive question. If labels and numbers are on separate lines, this is the
    # audit-fee problem again and the section text has to be re-derived with the
    # row-preserving converter before any dictionary is worth writing.
    ("4. Does a KPI label sit on the same line as a number?", """
        WITH s AS (
            SELECT cik, text FROM quali.mdna
            WHERE substr(filing_date, 1, 4) = '2024'
              AND (lower(text) LIKE '%load factor%'
                OR lower(text) LIKE '%comparable store sales%'
                OR lower(text) LIKE '%occupancy rate%'
                OR lower(text) LIKE '%average daily rate%')
        ),
        lines AS (
            SELECT cik, unnest(str_split(text, chr(10))) AS line FROM s
        )
        SELECT count(*) AS kpi_lines,
               count(*) FILTER (WHERE regexp_matches(line, '[0-9]')) AS with_any_digit,
               count(*) FILTER (WHERE regexp_matches(line,
                   '[0-9]+\\.?[0-9]*\\s*%')) AS with_a_percentage,
               count(*) FILTER (WHERE regexp_matches(line,
                   '\\$\\s*[0-9,]+')) AS with_a_dollar_amount,
               round(median(length(line)), 0) AS median_line_length
        FROM lines
        WHERE regexp_matches(lower(line),
            'load factor|comparable store sales|occupancy rate|average daily rate')"""),

    ("5. Real sentences, so the phrasing is read rather than assumed", """
        SELECT cik, substr(filing_date, 1, 10) AS filed,
               regexp_extract(text,
                   '[^.\\n]{0,110}(?i:load factor|comparable store sales|'
                   || 'occupancy rate|average daily rate|revenue per available seat mile)'
                   || '[^.\\n]{0,110}') AS sentence
        FROM quali.mdna
        WHERE substr(filing_date, 1, 4) = '2024'
          AND regexp_matches(lower(text),
              'load factor|comparable store sales|occupancy rate|average daily rate')
        LIMIT 10"""),

    # Where the companies are, so the dictionary is built where it pays.
    ("6. Which industries hold the most companies with financials?", """
        SELECT r.sic2, any_value(c.sic_description) AS example_description,
               count(DISTINCT r.cik) AS companies
        FROM marts.ratio_values r
        LEFT JOIN ref.dim_company c ON c.cik = r.cik
        WHERE r.fy = 2024
        GROUP BY r.sic2 ORDER BY companies DESC LIMIT 20"""),

    ("7. Does the KPI language track the industry it should?", """
        WITH tagged AS (
            SELECT m.cik, c.sic2, lower(m.text) AS t
            FROM quali.mdna m
            JOIN ref.dim_company c ON c.cik = m.cik
            WHERE substr(m.filing_date, 1, 4) = '2024'
        )
        SELECT sic2,
               count(DISTINCT cik) FILTER (WHERE t LIKE '%load factor%') AS load_factor,
               count(DISTINCT cik) FILTER (WHERE t LIKE '%comparable store sales%')
                   AS comp_sales,
               count(DISTINCT cik) FILTER (WHERE t LIKE '%revpar%') AS revpar,
               count(DISTINCT cik) FILTER (WHERE t LIKE '%annual recurring revenue%')
                   AS arr,
               count(DISTINCT cik) AS companies
        FROM tagged GROUP BY sic2
        HAVING load_factor + comp_sales + revpar + arr > 2
        ORDER BY companies DESC LIMIT 15"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)))[:118]
             for v in r] for r in cur.fetchall()]
    if not rows:
        print("  (no rows)")
        return
    w = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(heads)]
    print("  " + "  ".join(h.ljust(x) for h, x in zip(heads, w)))
    print("  " + "  ".join("-" * x for x in w))
    for r in rows:
        print("  " + "  ".join(v.ljust(x) for v, x in zip(r, w)))


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    con.execute("SET temp_directory = '/tmp/duckdb_spill'")
    for title, q in Q:
        print(f"\n### {title}")
        try:
            show(con, q)
        except Exception as exc:
            print(f"  (failed: {str(exc)[:190]})")


if __name__ == "__main__":
    main()
