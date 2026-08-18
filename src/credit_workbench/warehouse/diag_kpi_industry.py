"""G-08, second probe — the first one measured against the wrong denominator.

The first probe reported KPI hit rates as a share of all 7,594 MD&A filers, which makes
every sector metric look hopeless: load factor 0.4%, all-in sustaining cost 0.1%. That is an
artefact of the denominator. Twenty-seven companies mentioning load factor may be nearly
every listed airline, and a dictionary entry that covers most of an industry is worth having
however small the industry is against the whole market.

So this asks the question that decides where the dictionary pays: **within the industry the
KPI belongs to, what share of companies disclose it?** A metric covering 80% of REITs is
buildable and useful; one covering 8% of them is not, whatever the absolute count.

It also splits the two line shapes the first probe found, because they need different
machinery and only one of them is affordable. A KPI stated in prose - "occupancy rate to
98.2% in Fiscal 2022 from 97.3%" - carries its value on the same line and can be read from
the text already stored. A KPI stated in a table arrives as a bare label, because the 10-K
narrative was converted cell-per-line at ingest, and its value is on some other line. The
second kind cannot be recovered without re-deriving 1.79m sections from source HTML, which
is a re-fetch on the scale of the original ingest. Knowing the ratio decides whether G-08 is
a dictionary or a re-ingestion project.

One scan per question. The previous version of this probe timed out by asking each phrase in
its own SELECT, and `quali.filing_sections` is an unpartitioned view over parquet, so the
number of passes is the only cost lever that matters.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

# KPI mapped to the SIC major groups it belongs to, so coverage is measured against the
# companies that could plausibly disclose it rather than against the whole market.
KPI_INDUSTRY = {
    "reit_occupancy": ("occupancy rate", ["65", "67"]),
    "reit_ffo": ("funds from operations", ["65", "67"]),
    "semi_asp": ("average selling price", ["36", "38"]),
    "saas_arr": ("annual recurring revenue", ["73"]),
    "saas_rpo": ("remaining performance obligations", ["73"]),
    "retail_comp_sales": ("comparable store sales", ["53", "54", "56", "57", "59"]),
    "retail_store_count": ("store count", ["53", "54", "56", "57", "59"]),
    "restaurant_auv": ("average unit volume", ["58"]),
    "hotel_revpar": ("revpar", ["70"]),
    "hotel_adr": ("average daily rate", ["70"]),
    "airline_load_factor": ("load factor", ["45"]),
    "airline_rasm": ("available seat mile", ["45"]),
    "energy_boe": ("barrels of oil equivalent", ["13", "29"]),
    "energy_reserves": ("proved reserves", ["13", "29"]),
    "mining_aisc": ("all-in sustaining cost", ["10", "12", "14"]),
    "telecom_arpu": ("average revenue per user", ["48"]),
    "utility_mwh": ("megawatt hour", ["49"]),
    "mfg_backlog": ("backlog", ["35", "36", "37"]),
    "health_admissions": ("admissions", ["80"]),
}


def coverage_query() -> str:
    """One pass: for each KPI, companies mentioning it inside its own industry."""
    parts = []
    for name, (phrase, sics) in KPI_INDUSTRY.items():
        sic_list = ", ".join(f"'{s}'" for s in sics)
        parts.append(
            f"SELECT '{name}' AS kpi, '{'/'.join(sics)}' AS sic2_group, "
            f"count(DISTINCT cik) FILTER (WHERE sic2 IN ({sic_list})) AS in_industry, "
            f"count(DISTINCT cik) FILTER (WHERE sic2 IN ({sic_list}) "
            f"                              AND t LIKE '%{phrase}%') AS disclosing, "
            f"count(DISTINCT cik) FILTER (WHERE sic2 NOT IN ({sic_list}) "
            f"                              AND t LIKE '%{phrase}%') AS outside_industry "
            f"FROM tagged")
    # sic2 comes from marts.ratio_values, not ref.dim_company, which carries `sic` and no
    # `sic2` - the column probe said so and I wrote the query without consulting it. Using
    # ratio_values also restricts to companies that have financials, which is the universe
    # a scorecard can use anyway.
    return ("WITH ind AS (\n"
            "    SELECT DISTINCT cik, sic2 FROM marts.ratio_values WHERE fy >= 2022),\n"
            "tagged AS (\n"
            "    SELECT m.cik, i.sic2, lower(m.text) AS t\n"
            "    FROM quali.mdna m JOIN ind i ON i.cik = m.cik\n"
            "    WHERE substr(m.filing_date, 1, 4) BETWEEN '2022' AND '2025')\n"
            + "\nUNION ALL\n".join(parts))


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    con.execute("SET temp_directory = '/tmp/duckdb_spill'")

    print("### 1. Coverage within the industry the KPI belongs to")
    print("    (outside_industry is the false-positive tell: a sector metric that "
          "appears\n     more often outside its sector than inside is not a sector "
          "metric)")
    try:
        cur = con.execute(coverage_query())
        rows = cur.fetchall()
        print(f"\n  {'kpi':<22} {'sic2':<14} {'in_ind':>7} {'disclosing':>11} "
              f"{'pct':>7} {'outside':>8}")
        for kpi, sics, in_ind, disc, outside in sorted(
                rows, key=lambda r: -(r[3] / max(r[2], 1))):
            pct = 100.0 * disc / max(in_ind, 1)
            print(f"  {kpi:<22} {sics:<14} {in_ind:>7,} {disc:>11,} "
                  f"{pct:>6.1f}% {outside:>8,}")
    except Exception as exc:
        print(f"  (failed: {str(exc)[:190]})")

    # The shape split. A label alone on a short line is a table header whose value the
    # cell-per-line conversion put elsewhere; a label inside a long line with a number is
    # prose and is readable from what is already stored.
    print("\n### 2. Prose or table? — the split that decides what is buildable")
    try:
        cur = con.execute("""
            WITH s AS (
                SELECT text FROM quali.mdna
                WHERE substr(filing_date, 1, 4) = '2024'
                  AND regexp_matches(lower(text),
                      'occupancy rate|revpar|load factor|comparable store sales'
                      || '|annual recurring revenue|average selling price')
                LIMIT 600
            ),
            lines AS (SELECT trim(unnest(str_split(text, chr(10)))) AS line FROM s),
            kpi AS (
                SELECT line, length(line) AS len,
                       regexp_matches(line, '[0-9]') AS has_digit
                FROM lines
                WHERE regexp_matches(lower(line),
                    'occupancy rate|revpar|load factor|comparable store sales'
                    || '|annual recurring revenue|average selling price')
            )
            SELECT count(*) AS kpi_lines,
                   count(*) FILTER (WHERE len <= 60 AND NOT has_digit)
                       AS bare_label_table_shape,
                   count(*) FILTER (WHERE len > 60 AND has_digit) AS prose_with_value,
                   count(*) FILTER (WHERE len <= 60 AND has_digit) AS short_with_value,
                   count(*) FILTER (WHERE len > 60 AND NOT has_digit) AS prose_no_value,
                   round(100.0 * count(*) FILTER (WHERE len > 60 AND has_digit)
                         / count(*), 1) AS pct_prose_with_value
            FROM kpi""")
        heads = [d[0] for d in cur.description]
        for r in cur.fetchall():
            for h, v in zip(heads, r):
                print(f"  {h:<26} {v}")
    except Exception as exc:
        print(f"  (failed: {str(exc)[:190]})")

    print("\n### 3. How many companies are in each candidate industry at all?")
    try:
        cur = con.execute("""
            SELECT r.sic2, any_value(c.sic_description) AS example,
                   count(DISTINCT r.cik) AS companies_with_ratios
            FROM marts.ratio_values r
            LEFT JOIN ref.dim_company c ON c.cik = r.cik
            WHERE r.fy = 2024
              AND r.sic2 IN ('10','12','13','14','29','35','36','37','38','45',
                             '48','49','53','54','56','57','58','59','65','67',
                             '70','73','80')
            GROUP BY r.sic2 ORDER BY companies_with_ratios DESC""")
        for r in cur.fetchall():
            print(f"  {r[0]:<5} {str(r[1])[:50]:<52} {r[2]:>6,}")
    except Exception as exc:
        print(f"  (failed: {str(exc)[:190]})")


if __name__ == "__main__":
    main()
