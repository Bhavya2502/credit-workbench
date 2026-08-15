"""Tracker B4 — the industry bridge.

Peer comparison needs groups that are both economically alike and populated. SIC gives
neither on its own: the four-digit code is precise but thin — 263 of the 400 codes in use
have fewer than ten companies, a median of six — while the two-digit major group is
populated but blunt, putting a pharmaceutical preparations business and a soap
manufacturer in the same bucket.

So this builds three things.

`ref.sic_hierarchy` is the structure of SIC itself: four-digit code, three-digit industry
group, two-digit major group, and the division it belongs to. Divisions are fixed by the
numbering scheme; the descriptions come from SEC's own labels already in our filing data
rather than a copied list.

`ref.industry_group` assigns each SIC code a peer group by rolling up only as far as it
must. A code with enough companies to compare against stays at four digits; a thin one
merges into its three-digit group, then its major group, then its division. The result is
a scheme that is as granular as the data supports and no more, with the peer count
recorded so anyone can see what a comparison rests on.

`custom_industry` is left empty. A house industry scheme is a business decision, not
something to infer; when one exists it loads into that column and everything downstream
picks it up.
"""
from __future__ import annotations

import argparse

import duckdb

from credit_workbench.common.config import motherduck_token

# A peer group smaller than this cannot support a percentile. It matches the grain the
# benchmark layer already uses.
MIN_PEERS = 30

# SIC divisions are fixed by the numbering scheme.
DIVISIONS = [
    ("A", 1, 9, "Agriculture, Forestry and Fishing"),
    ("B", 10, 14, "Mining"),
    ("C", 15, 17, "Construction"),
    ("D", 20, 39, "Manufacturing"),
    ("E", 40, 49, "Transportation, Communications, Utilities"),
    ("F", 50, 51, "Wholesale Trade"),
    ("G", 52, 59, "Retail Trade"),
    ("H", 60, 67, "Finance, Insurance and Real Estate"),
    ("I", 70, 89, "Services"),
    ("J", 91, 97, "Public Administration"),
    ("Z", 98, 99, "Nonclassifiable"),
]

DIVISION_SQL = "CASE\n" + "\n".join(
    f"        WHEN sic2_int BETWEEN {lo} AND {hi} THEN '{code}'"
    for code, lo, hi, _ in DIVISIONS) + "\n        ELSE '?' END"

DIVISION_NAME_SQL = "CASE\n" + "\n".join(
    f"        WHEN sic2_int BETWEEN {lo} AND {hi} THEN '{name}'"
    for code, lo, hi, name in DIVISIONS) + "\n        ELSE 'Unclassified' END"


def build() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")

    # ---------------------------------------------------------------- hierarchy
    # Descriptions come from SEC's own labels on the filings, taken as the most common
    # label per code so a single mislabelled filing cannot rename an industry.
    con.execute(f"""
        CREATE OR REPLACE TABLE ref.sic_hierarchy AS
        WITH labelled AS (
            SELECT lpad(CAST(sic AS VARCHAR), 4, '0') AS sic4,
                   sic_description,
                   count(*) AS n
            FROM ref.dim_company
            WHERE sic IS NOT NULL AND sic_description IS NOT NULL
            GROUP BY 1, 2
            QUALIFY row_number() OVER (PARTITION BY sic4 ORDER BY n DESC) = 1),
        codes AS (
            SELECT DISTINCT lpad(CAST(sic AS VARCHAR), 4, '0') AS sic4
            FROM ref.dim_company WHERE sic IS NOT NULL)
        SELECT c.sic4,
               substr(c.sic4, 1, 3) AS sic3,
               substr(c.sic4, 1, 2) AS sic2,
               TRY_CAST(substr(c.sic4, 1, 2) AS INTEGER) AS sic2_int,
               l.sic_description AS sic4_description
        FROM codes c LEFT JOIN labelled l ON l.sic4 = c.sic4""")

    con.execute(f"""
        CREATE OR REPLACE TABLE ref.sic_hierarchy AS
        SELECT sic4, sic3, sic2, sic4_description,
               {DIVISION_SQL} AS division_code,
               {DIVISION_NAME_SQL} AS division_name
        FROM ref.sic_hierarchy""")
    n = con.execute("SELECT count(*) FROM ref.sic_hierarchy").fetchone()[0]
    print(f"table ref.sic_hierarchy  {n:,} SIC codes")

    # ---------------------------------------------------------------- peer groups
    # Counts over several years, so a group is not reclassified because one year was
    # thin. Companies with financials only - a code used solely by shells is not a peer
    # group anyone will compare against.
    con.execute(f"""
        CREATE OR REPLACE TABLE ref.industry_group AS
        WITH universe AS (
            SELECT DISTINCT cik, lpad(CAST(sic AS VARCHAR), 4, '0') AS sic4
            FROM marts.ratio_values
            WHERE fy BETWEEN 2020 AND 2024 AND sic IS NOT NULL),
        u AS (
            SELECT u.cik, u.sic4, h.sic3, h.sic2, h.division_code, h.division_name,
                   h.sic4_description
            FROM universe u JOIN ref.sic_hierarchy h ON h.sic4 = u.sic4),
        counts AS (
            SELECT sic4, sic3, sic2, division_code, division_name, sic4_description,
                   count(DISTINCT cik) OVER (PARTITION BY sic4) AS n4,
                   count(DISTINCT cik) OVER (PARTITION BY sic3) AS n3,
                   count(DISTINCT cik) OVER (PARTITION BY sic2) AS n2,
                   count(DISTINCT cik) OVER (PARTITION BY division_code) AS n1
            FROM u),
        assigned AS (
            SELECT DISTINCT sic4, sic3, sic2, division_code, division_name,
                   sic4_description, n4, n3, n2, n1,
                   -- Roll up only as far as necessary to reach a comparable peer count.
                   CASE WHEN n4 >= {MIN_PEERS} THEN 'sic4'
                        WHEN n3 >= {MIN_PEERS} THEN 'sic3'
                        WHEN n2 >= {MIN_PEERS} THEN 'sic2'
                        ELSE 'division' END AS industry_level
            FROM counts)
        SELECT sic4, sic3, sic2, division_code, division_name, sic4_description,
               industry_level,
               CASE industry_level WHEN 'sic4' THEN sic4 WHEN 'sic3' THEN sic3
                                   WHEN 'sic2' THEN sic2 ELSE division_code END
                   AS industry_code,
               CASE industry_level
                    WHEN 'sic4' THEN sic4_description
                    WHEN 'sic3' THEN 'SIC ' || sic3 || ' group'
                    WHEN 'sic2' THEN 'SIC ' || sic2 || ' major group'
                    ELSE division_name END AS industry_label,
               CASE industry_level WHEN 'sic4' THEN n4 WHEN 'sic3' THEN n3
                                   WHEN 'sic2' THEN n2 ELSE n1 END AS peers,
               -- A house scheme is a business decision, not something to infer. Load it
               -- here and everything downstream follows.
               CAST(NULL AS VARCHAR) AS custom_industry
        FROM assigned""")

    rows, groups = con.execute("""
        SELECT count(*), count(DISTINCT industry_code) FROM ref.industry_group""").fetchone()
    print(f"table ref.industry_group  {rows:,} SIC codes mapped to "
          f"{groups:,} peer groups")

    print("\nHow the roll-up landed:")
    for level, codes, grps, med in con.execute("""
        SELECT industry_level, count(*) AS codes,
               count(DISTINCT industry_code) AS groups,
               round(median(peers), 0) AS median_peers
        FROM ref.industry_group GROUP BY 1
        ORDER BY CASE industry_level WHEN 'sic4' THEN 1 WHEN 'sic3' THEN 2
                                     WHEN 'sic2' THEN 3 ELSE 4 END""").fetchall():
        print(f"  {level:<9} {codes:>4} codes -> {grps:>3} groups, "
              f"median {med:>4.0f} peers")

    print("\nLargest peer groups:")
    for code, label, level, peers in con.execute("""
        SELECT industry_code, any_value(industry_label), any_value(industry_level),
               max(peers)
        FROM ref.industry_group GROUP BY industry_code
        ORDER BY max(peers) DESC LIMIT 12""").fetchall():
        print(f"  {code:<5} {str(label)[:46]:<46} {level:<9} {peers:>5}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.parse_args()
    build()


if __name__ == "__main__":
    main()
