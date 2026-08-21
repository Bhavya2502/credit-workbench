"""Validate the country derivation, and say which country it actually is.

Two questions were put to the industry sheet's `country` column, and both deserve
evidence rather than an assurance.

**On what basis was it mapped?** The rule was: `business_country = business_state` means
the code is a US state, so the company is United States. That was inferred from the
observed pattern, not from a published specification, and an inference used on 12,800
companies should be checked. Q1 tests every value the rule called "United States"
against the actual US postal state, DC and territory codes - if any value is not a real
US state code, the rule is wrong and the count of how wrong is here.

**Isn't everything US anyway, given it is SEC data?** SEC registration says where a
company *lists*, not where it is *based* or *incorporated*, and all three differ. This
matters most for the Cayman Islands and Bermuda, which are incorporation domiciles
rather than places anyone operates from. Q3 and Q4 put business address against state of
incorporation so the difference is visible and the right field can be chosen
deliberately.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

US_CODES = """('AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN',
   'IA','KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ',
   'NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT','VA',
   'WA','WV','WI','WY','DC','PR','VI','GU','AS','MP')"""

HELD = "d.cik IN (SELECT DISTINCT cik FROM marts.spreads_a)"

Q = [
    ("1. VALIDATE - is every 'code == description' value a real US state code?", f"""
        SELECT count(*) AS companies_rule_called_us,
               count(*) FILTER (WHERE d.business_state IN {US_CODES})
                   AS confirmed_real_us_state,
               count(*) FILTER (WHERE d.business_state NOT IN {US_CODES})
                   AS NOT_a_us_state_rule_would_be_wrong
        FROM ref.dim_company d
        WHERE {HELD} AND d.business_country = d.business_state
          AND d.business_country IS NOT NULL AND d.business_country <> ''"""),

    ("2. The exceptions, if any - values the rule called US but are not US states", f"""
        SELECT d.business_state, d.business_country, count(*) AS companies
        FROM ref.dim_company d
        WHERE {HELD} AND d.business_country = d.business_state
          AND d.business_state NOT IN {US_CODES}
        GROUP BY 1, 2 ORDER BY companies DESC LIMIT 15"""),

    ("3. WHICH COUNTRY - business address vs state of incorporation", f"""
        WITH c AS (
            SELECT d.cik,
                   CASE WHEN d.business_country IS NULL OR d.business_country = ''
                          THEN 'Unknown'
                        WHEN d.business_country = d.business_state THEN 'United States'
                        WHEN d.business_country = 'United States' THEN 'United States'
                        ELSE trim(regexp_extract(d.business_country, '([^,]+)$', 1))
                   END AS address_country,
                   CASE WHEN d.state_of_incorporation IS NULL
                          OR d.state_of_incorporation = '' THEN 'Unknown'
                        WHEN d.state_of_incorporation IN {US_CODES} THEN 'United States'
                        ELSE trim(regexp_extract(
                             coalesce(nullif(d.state_of_incorporation_description, ''),
                                      d.state_of_incorporation), '([^,]+)$', 1))
                   END AS incorporation_country
            FROM ref.dim_company d WHERE {HELD})
        SELECT address_country, incorporation_country, count(*) AS companies
        FROM c
        WHERE address_country <> incorporation_country
        GROUP BY 1, 2 ORDER BY companies DESC LIMIT 20"""),

    ("4. How many companies are US-listed but not US-based, either way?", f"""
        WITH c AS (
            SELECT d.cik,
                   CASE WHEN d.business_country IS NULL OR d.business_country = ''
                          THEN 'Unknown'
                        WHEN d.business_country = d.business_state THEN 'United States'
                        WHEN d.business_country = 'United States' THEN 'United States'
                        ELSE trim(regexp_extract(d.business_country, '([^,]+)$', 1))
                   END AS address_country,
                   CASE WHEN d.state_of_incorporation IS NULL
                          OR d.state_of_incorporation = '' THEN 'Unknown'
                        WHEN d.state_of_incorporation IN {US_CODES} THEN 'United States'
                        ELSE 'Foreign'
                   END AS incorp
            FROM ref.dim_company d WHERE {HELD})
        SELECT count(*) AS companies,
               count(*) FILTER (WHERE address_country = 'United States') AS us_address,
               count(*) FILTER (WHERE address_country NOT IN ('United States', 'Unknown'))
                   AS foreign_address,
               count(*) FILTER (WHERE address_country = 'Unknown') AS unknown_address,
               count(*) FILTER (WHERE incorp = 'Foreign') AS foreign_incorporated
        FROM c"""),

    ("5. Do foreign-address filers actually file 10-K, or 20-F/40-F?", f"""
        WITH c AS (
            SELECT d.cik,
                   CASE WHEN d.business_country = d.business_state
                          OR d.business_country = 'United States' THEN 'United States'
                        WHEN d.business_country IS NULL OR d.business_country = ''
                          THEN 'Unknown'
                        ELSE 'Foreign' END AS addr
            FROM ref.dim_company d WHERE {HELD})
        SELECT c.addr, s.form, count(DISTINCT c.cik) AS companies
        FROM c JOIN quali.filing_sections s ON s.cik = c.cik
        WHERE s.form IN ('10-K', '20-F', '40-F')
        GROUP BY 1, 2 ORDER BY 1, companies DESC"""),

    ("6. The blanks - 685 companies with no address at all. Who are they?", f"""
        SELECT coalesce(nullif(d.entity_type, ''), '(none)') AS entity_type,
               coalesce(nullif(d.sic_description, ''), '(no sic)') AS industry,
               count(*) AS companies
        FROM ref.dim_company d
        WHERE {HELD} AND (d.business_country IS NULL OR d.business_country = '')
        GROUP BY 1, 2 ORDER BY companies DESC LIMIT 10"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)))[:44]
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
            print(f"  (failed: {str(exc)[:200]})")


if __name__ == "__main__":
    main()
