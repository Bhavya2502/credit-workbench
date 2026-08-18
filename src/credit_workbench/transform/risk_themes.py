"""G-09 — risk factors classified by theme, at heading grain.

The request is for `marts.risk_themes` at `cik × filing × theme` plus a `theme × sic2 × fy`
aggregate, with the verbatim heading kept so any count is checkable back to the filing.

**Classifying body text would have produced nothing.** Measured over 2022-2025 Item 1A:
regulation appears for 95.9% of filers, competition 94.1%, liquidity 92.7%, pandemic 91.8%,
cyber 89.8%. Every issuer of any size mentions every common risk somewhere in fifty thousand
characters, so a body-text mart would be a table of ninety-per-cent figures with no
discriminating power at all - and it would look authoritative while having none.

**Headings carry the structure instead.** Risk factors are written as a list of individually
headed items, and the headings survive the conversion intact:

    Our indebtedness and restrictive covenants under our credit facilities could limit our
    operational and financial flexibility.
    A write-off of all or part of our goodwill or other intangible assets could adversely
    affect our operating results and net worth.

One heading is one risk the issuer chose to disclose, which is what the request actually
wants counted. Length alone does not find them - the median line in these sections is 134
characters, so a length window catches body paragraphs too. What does find them is the
formula they are written to: a risk followed by an adverse consequence. `HEADING_SHAPE`
below is that formula, and it is why this works where a length filter did not.

**Not every theme is a factor, and the mart says so.** Cyber security appears in 85-99% of
issuers in every industry measured - universal, and therefore useless for discriminating
between them. Climate runs 48% in pharmaceuticals to 93% in oil and gas; supply chain 37% in
insurance to 88% in semiconductors. `marts.risk_theme_prevalence` publishes the spread
between the highest and lowest industry for each theme, so a scorecard can tell a
discriminating theme from a universal one rather than weighting cyber and wondering why it
does nothing.

**Cost.** `staging.risk_headings` is permanent and fingerprinted on the heading rule, for
the reason G-08 learned the hard way: the narrative scan is the whole expense, it does not
depend on the theme vocabulary, and iterating on themes should not pay for it again. Change
the vocabulary and the headings are reused; change the heading rule and they rebuild
themselves.
"""
from __future__ import annotations

import argparse
import hashlib

import duckdb

from credit_workbench.common.config import motherduck_token

# The formula a risk-factor heading is written to: a risk, then an adverse consequence.
# This is the discriminator. A length window is not - the median line in Item 1A is 134
# characters, so length alone selects body paragraphs as readily as headings.
CONSEQUENCE = (
    r"(adversely\s+affect|material\s+adverse|negatively\s+(affect|impact)"
    r"|could\s+(harm|limit|reduce|result|cause|prevent|impair|lead)"
    r"|may\s+(harm|limit|reduce|result|cause|prevent|impair|lead|not\s+be\s+able)"
    r"|would\s+(harm|limit|reduce|result)"
    r"|have\s+a\s+material|subject\s+us\s+to|expose\s+us\s+to"
    r"|difficult|unable\s+to|failure\s+to)")

HEADING_MIN, HEADING_MAX = 40, 320

# theme, label, pattern matched against the heading. Written for heading language rather
# than for the topic in the abstract - a heading says "our indebtedness", not "leverage".
THEMES = [
    ("indebtedness", "Leverage and debt service",
     r"indebtedness|leverage|debt\s+service|credit\s+facilit|covenant"
     r"|refinanc|principal\s+and\s+interest"),
    ("liquidity", "Liquidity and going concern",
     r"liquidity|going\s+concern|sufficient\s+cash|additional\s+capital"
     r"|access\s+to\s+capital|fund\s+our\s+operations"),
    ("customer_concentration", "Customer concentration",
     r"(few|small\s+number|limited\s+number|concentration)\s+of\s+(direct\s+and\s+)?"
     r"(indirect\s+)?(customers|clients)|significant\s+customers?"
     r"|depend(ent|s)?\s+on\s+a\s+(few|limited)"),
    ("supply_chain", "Supply chain and suppliers",
     r"suppliers?|supply\s+chain|raw\s+material|component\s+shortage"
     r"|single\s+source|manufactur(ing|ers)\s+capacity|subcontractor"),
    ("cyber", "Cyber security and data",
     r"cyber|data\s+breach|information\s+security|ransomware"
     r"|unauthorized\s+access|privacy"),
    ("climate", "Climate and environmental",
     r"climate|greenhouse|carbon|environmental\s+(law|regulation|liabilit)"
     r"|extreme\s+weather|natural\s+disaster"),
    ("regulation", "Regulation and compliance",
     r"regulat|compliance\s+with|government\s+(spending|contract|action)"
     r"|licens(e|ing)|tariff|sanction"),
    ("litigation", "Litigation",
     r"litigation|lawsuit|legal\s+proceeding|claims?\s+against|product\s+liability"),
    ("key_personnel", "Key personnel and labour",
     r"key\s+personnel|qualified\s+personnel|retain\s+(our\s+)?(key\s+)?employees"
     r"|labor\s+(disruption|shortage|dispute)|unioni[sz]|personnel\s+turnover"),
    ("competition", "Competition and pricing",
     r"competit|pricing\s+pressure|price\s+sensitive|market\s+share"),
    ("technology", "Technology obsolescence",
     r"obsolete|technological\s+change|new\s+technolog|innovat"
     r"|artificial\s+intelligence"),
    ("impairment", "Impairment and write-offs",
     r"impair|write-?off|goodwill|intangible\s+assets"),
    ("interest_rates", "Interest rates and financing cost",
     r"interest\s+rate|variable\s+rate|borrowing\s+cost"),
    ("currency", "Foreign exchange and international",
     r"foreign\s+currency|exchange\s+rate|international\s+operations"
     r"|foreign\s+operations"),
    ("acquisitions", "Acquisitions and integration",
     r"acquisitions?|integrat|divestiture|business\s+combination"),
    ("customer_demand", "Demand and cyclicality",
     r"cyclical|downturn|demand\s+for\s+our|economic\s+conditions|recession"),
    ("intellectual_property", "Intellectual property",
     r"intellectual\s+property|patent|trademark|trade\s+secret|infring"),
    ("controls", "Internal control and reporting",
     r"internal\s+control|material\s+weakness|restat(e|ement)"
     r"|financial\s+reporting"),
    ("insurance", "Insurance and uninsured loss",
     r"insurance|uninsured|indemnit"),
    ("catastrophe", "Pandemic, conflict and catastrophe",
     r"pandemic|epidemic|covid|war|armed\s+conflict|terroris|geopolitical"),
]

THEME_TABLE = """
CREATE OR REPLACE TABLE ref.risk_theme_dictionary (
    theme VARCHAR, label VARCHAR, pattern VARCHAR)
"""


def headings_table() -> str:
    """One pass over Item 1A, keeping the lines that read like risk headings."""
    return f"""
CREATE OR REPLACE TABLE staging.risk_headings AS
WITH ind AS (
    SELECT DISTINCT cik, sic2 FROM marts.ratio_values WHERE fy >= 2015
),
src AS (
    SELECT r.cik, i.sic2, r.adsh, r.filing_date,
           TRY_CAST(substr(r.period_of_report, 1, 4) AS INTEGER) AS fy, r.text
    FROM quali.risk_factors r
    JOIN ind i ON i.cik = r.cik
    WHERE substr(r.filing_date, 1, 4) >= '2015' AND r.char_len > 5000
),
exploded AS (
    SELECT cik, sic2, adsh, filing_date, fy,
           trim(unnest(str_split(text, chr(10)))) AS line
    FROM src
)
SELECT cik, sic2, adsh, filing_date, fy, line AS heading
FROM exploded
WHERE fy IS NOT NULL
  AND length(line) BETWEEN {HEADING_MIN} AND {HEADING_MAX}
  AND regexp_matches(line, '^[A-Z(]')
  AND regexp_matches(lower(line), '{CONSEQUENCE}')
"""


def build_themes() -> str:
    """Classify each heading. A heading may carry several themes; that is the disclosure."""
    branches = "\n    UNION ALL\n".join(
        f"""SELECT cik, sic2, adsh, filing_date, fy, '{theme}' AS theme, heading
FROM staging.risk_headings WHERE regexp_matches(lower(heading), '{pat}')"""
        for theme, _label, pat in THEMES)
    return f"""
CREATE OR REPLACE TABLE marts.risk_themes AS
WITH hit AS (
    {branches}
),
ranked AS (
    -- One row per filing and theme, keeping the longest heading as the evidence: a longer
    -- heading states the risk more fully, and the request wants the verbatim text kept so
    -- any count can be checked back to the filing.
    SELECT *, row_number() OVER (PARTITION BY cik, adsh, theme
                                 ORDER BY length(heading) DESC) AS rn,
           count(*) OVER (PARTITION BY cik, adsh, theme) AS headings_for_theme
    FROM hit
)
SELECT cik, adsh, fy, sic2, filing_date, theme, headings_for_theme,
       substr(heading, 1, 400) AS example_heading
FROM ranked WHERE rn = 1
"""


# The aggregate the request asked for, plus the thing it needs to be usable: how much a
# theme varies between industries. Cyber sits at 85-99% everywhere and is not a factor;
# climate runs 48% to 93% and is.
PREVALENCE = """
CREATE OR REPLACE TABLE marts.risk_theme_prevalence AS
WITH universe AS (
    SELECT sic2, fy, count(DISTINCT cik) AS issuers
    FROM staging.risk_headings GROUP BY sic2, fy
),
by_theme AS (
    SELECT t.sic2, t.fy, t.theme, count(DISTINCT t.cik) AS issuers_with_theme
    FROM marts.risk_themes t GROUP BY t.sic2, t.fy, t.theme
),
joined AS (
    SELECT b.theme, b.sic2, b.fy, b.issuers_with_theme, u.issuers AS issuers_total,
           round(100.0 * b.issuers_with_theme / nullif(u.issuers, 0), 1) AS issuer_share
    FROM by_theme b JOIN universe u ON u.sic2 = b.sic2 AND u.fy = b.fy
    WHERE u.issuers >= 20
)
SELECT j.*,
       round(max(issuer_share) OVER (PARTITION BY theme, fy)
             - min(issuer_share) OVER (PARTITION BY theme, fy), 1) AS industry_spread_pp,
       max(issuer_share) OVER (PARTITION BY theme, fy)
         - min(issuer_share) OVER (PARTITION BY theme, fy) >= 20 AS discriminates
FROM joined j
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-headings", action="store_true",
                    help="rebuild the heading table even if the rule is unchanged")
    args = ap.parse_args()

    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    con.execute("SET temp_directory = '/tmp/duckdb_spill'")
    con.execute("SET preserve_insertion_order = false")
    for schema in ("ref", "marts", "staging"):
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

    con.execute(THEME_TABLE)
    con.executemany("INSERT INTO ref.risk_theme_dictionary VALUES (?, ?, ?)", THEMES)
    print(f"table ref.risk_theme_dictionary  {len(THEMES)} themes")

    # Fingerprinted on the heading rule, not the theme list: changing a theme pattern must
    # not trigger the expensive scan, and changing the heading rule must.
    fingerprint = hashlib.sha256(
        f"{CONSEQUENCE}|{HEADING_MIN}|{HEADING_MAX}".encode()).hexdigest()[:16]
    con.execute("""CREATE TABLE IF NOT EXISTS staging.risk_headings_build
                   (fingerprint VARCHAR, built_at TIMESTAMP, headings BIGINT)""")
    have = con.execute("SELECT fingerprint FROM staging.risk_headings_build "
                       "ORDER BY built_at DESC LIMIT 1").fetchone()
    exists = con.execute("""SELECT count(*) FROM information_schema.tables
                            WHERE table_schema = 'staging'
                              AND table_name = 'risk_headings'""").fetchone()[0]

    if args.refresh_headings or not exists or not have or have[0] != fingerprint:
        why = ("asked for" if args.refresh_headings else
               "absent" if not exists else "heading rule changed")
        print(f"building staging.risk_headings ({why}) - one pass over Item 1A")
        con.execute(headings_table())
        n = con.execute("SELECT count(*) FROM staging.risk_headings").fetchone()[0]
        con.execute("INSERT INTO staging.risk_headings_build VALUES (?, now(), ?)",
                    [fingerprint, n])
    else:
        n = con.execute("SELECT count(*) FROM staging.risk_headings").fetchone()[0]
        print("reusing staging.risk_headings - heading rule unchanged")
    print(f"table staging.risk_headings  {n:,} headings")

    con.execute(build_themes())
    rows, cos, filings = con.execute("""
        SELECT count(*), count(DISTINCT cik), count(DISTINCT adsh)
        FROM marts.risk_themes""").fetchone()
    print(f"table marts.risk_themes  {rows:,} rows, {cos:,} companies, "
          f"{filings:,} filings")

    con.execute(PREVALENCE)
    n = con.execute("SELECT count(*) FROM marts.risk_theme_prevalence").fetchone()[0]
    print(f"table marts.risk_theme_prevalence  {n:,} rows")

    print("\nHeadings per filing, and themes per filing:")
    cur = con.execute("""
        SELECT round(median(h), 0) AS median_headings, round(median(t), 0) AS median_themes
        FROM (SELECT adsh, count(*) AS h FROM staging.risk_headings GROUP BY adsh) a
        JOIN (SELECT adsh, count(*) AS t FROM marts.risk_themes GROUP BY adsh) b
          USING (adsh)""")
    for h, v in zip([d[0] for d in cur.description], cur.fetchone()):
        print(f"  {h:<20} {v}")

    print("\nWhich themes discriminate between industries, and which are universal?")
    cur = con.execute("""
        SELECT theme, round(avg(issuer_share), 1) AS mean_share,
               round(avg(industry_spread_pp), 1) AS mean_spread_pp,
               bool_or(discriminates) AS ever_discriminates
        FROM marts.risk_theme_prevalence WHERE fy = 2024
        GROUP BY theme ORDER BY mean_spread_pp DESC""")
    print(f"  {'theme':<24} {'mean share':>11} {'spread pp':>10} {'discriminates':>14}")
    for r in cur.fetchall():
        print(f"  {r[0]:<24} {r[1]:>10.1f}% {r[2]:>10.1f} {r[3]!s:>14}")


if __name__ == "__main__":
    main()
