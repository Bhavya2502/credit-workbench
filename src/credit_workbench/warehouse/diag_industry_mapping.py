"""Is every company mapped, and mapped to the right industry?

The industry default rates rest on two joins nobody has audited end to end: company to
SIC, and SIC to peer group. This measures both, and separates what can be verified from
what cannot.

**Coverage is measurable.** Which companies in the warehouse reach an outcome row, which
outcome rows carry a SIC, and which SICs reach a peer group. A company missing from the
denominator is as damaging as a wrong rate, and silently so.

**Correctness is only partly measurable.** SIC on an SEC filing is self-assigned by the
filer and never audited by anyone, and we hold no external reference to check it against.
Four internal proxies are available and all four are run here:

  format     `sic2` is `substr(sic, 1, 2)`. If a code lost its leading zero anywhere,
             '0100' Agriculture reads as '10' Metal Mining. Silent and total.
  stability  a code that moves between years is a code somebody re-assigned; a code that
             moves *into* a shell bucket around a failure moves the default with it
  buckets    6770 Blank Checks, 9995 Non-operating and 8880 ADRs are legal forms, not
             industries. They are not wrong, but a default rate computed over them is
             not an industry default rate
  labels     one code carrying several descriptions means the label is unreliable even
             where the code is right

**Identity is the fourth join.** One company filing under two CIKs is two companies here,
each with a shorter history, and a default recorded against whichever one filed the 8-K.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

FULL = ("observation_date <= (SELECT max(observation_date) FROM marts.credit_outcomes)"
        " - INTERVAL 730 DAY")

# Codes that describe a legal form or a listing vehicle rather than a line of business.
FORM_CODES = "('6770', '9995', '8880', '6199', '0000')"

Q = [
    ("1. Universe - who reaches an outcome row at all", """
        SELECT (SELECT count(*) FROM ref.dim_company) AS companies_known,
               (SELECT count(DISTINCT cik) FROM marts.spreads_a) AS with_a_spread,
               (SELECT count(DISTINCT cik) FROM marts.ratio_values) AS with_ratios,
               (SELECT count(DISTINCT cik) FROM marts.credit_outcomes) AS with_outcomes"""),

    ("2. The companies with a spread but no outcome row - who are they?", """
        SELECT coalesce(d.entity_type, '(none)') AS entity_type,
               coalesce(d.filer_category, '(none)') AS filer_category,
               count(*) AS companies
        FROM (SELECT DISTINCT cik FROM marts.spreads_a) s
        LEFT JOIN ref.dim_company d ON d.cik = TRY_CAST(s.cik AS BIGINT)
        WHERE TRY_CAST(s.cik AS BIGINT) NOT IN
              (SELECT TRY_CAST(cik AS BIGINT) FROM marts.credit_outcomes)
        GROUP BY 1, 2 ORDER BY companies DESC LIMIT 12"""),

    ("3. FORMAT - is every sic exactly four characters? sic2 depends on it", """
        SELECT length(sic) AS sic_length,
               count(*) AS company_years,
               count(DISTINCT cik) AS companies,
               count(DISTINCT sic) AS distinct_codes,
               min(sic) AS example_low, max(sic) AS example_high
        FROM marts.credit_outcomes
        WHERE sic IS NOT NULL AND sic <> ''
        GROUP BY 1 ORDER BY 1"""),

    ("4. FORMAT - does sic2 actually equal the first two digits of a padded sic?", """
        SELECT count(*) AS company_years,
               count(*) FILTER (WHERE sic2 = substr(lpad(sic, 4, '0'), 1, 2))
                   AS sic2_consistent,
               count(*) FILTER (WHERE sic2 <> substr(lpad(sic, 4, '0'), 1, 2))
                   AS sic2_wrong,
               count(*) FILTER (WHERE TRY_CAST(sic AS INTEGER) IS NULL) AS non_numeric
        FROM marts.credit_outcomes
        WHERE sic IS NOT NULL AND sic <> ''"""),

    ("5. COVERAGE - company-level tagging inside the outcomes table", """
        SELECT count(DISTINCT co.cik) AS companies,
               count(DISTINCT co.cik) FILTER (WHERE co.sic IS NOT NULL AND co.sic <> '')
                   AS with_sic,
               count(DISTINCT co.cik) FILTER (WHERE h.sic4 IS NOT NULL)
                   AS in_sic_hierarchy,
               count(DISTINCT co.cik) FILTER (WHERE g.sic4 IS NOT NULL)
                   AS in_peer_group,
               count(DISTINCT co.cik) FILTER (WHERE co.default_24m AND g.sic4 IS NULL)
                   AS defaulters_unmapped
        FROM marts.credit_outcomes co
        LEFT JOIN ref.sic_hierarchy h ON h.sic4 = co.sic
        LEFT JOIN ref.industry_group g ON g.sic4 = co.sic"""),

    ("6. COVERAGE - what falls outside the peer groups, and does it default?", f"""
        SELECT co.sic,
               coalesce(mode(h.sic4_description), '(no description)') AS description,
               count(*) AS company_years,
               count(DISTINCT co.cik) AS companies,
               count(*) FILTER (WHERE co.default_24m) AS defaults
        FROM marts.credit_outcomes co
        LEFT JOIN ref.sic_hierarchy h ON h.sic4 = co.sic
        LEFT JOIN ref.industry_group g ON g.sic4 = co.sic
        WHERE g.sic4 IS NULL AND {FULL}
        GROUP BY 1 ORDER BY company_years DESC LIMIT 15"""),

    ("7. BUCKETS - legal-form codes counted as if they were industries", f"""
        SELECT co.sic,
               coalesce(mode(h.sic4_description), '(no description)') AS description,
               count(*) AS company_years,
               count(DISTINCT co.cik) AS companies,
               count(*) FILTER (WHERE co.default_24m) AS defaults,
               round(100.0 * count(*) FILTER (WHERE co.default_24m) / count(*), 2)
                   AS default_rate_pct
        FROM marts.credit_outcomes co
        LEFT JOIN ref.sic_hierarchy h ON h.sic4 = co.sic
        WHERE co.sic IN {FORM_CODES} AND {FULL}
        GROUP BY 1 ORDER BY company_years DESC"""),

    ("8. STABILITY - how many companies carry more than one SIC over their history", """
        SELECT codes_per_company, count(*) AS companies,
               sum(company_years) AS company_years,
               sum(defaults) AS defaults
        FROM (SELECT cik, count(DISTINCT sic) AS codes_per_company,
                     count(*) AS company_years,
                     count(*) FILTER (WHERE default_24m) AS defaults
              FROM marts.credit_outcomes GROUP BY cik)
        GROUP BY 1 ORDER BY 1"""),

    ("9. STABILITY - do the movers move into a shell bucket? (defaults would follow)", f"""
        WITH ranked AS (
            SELECT cik, sic, fy,
                   first_value(sic) OVER (PARTITION BY cik ORDER BY fy) AS first_sic,
                   last_value(sic) OVER (PARTITION BY cik ORDER BY fy
                       RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS last_sic
            FROM marts.credit_outcomes WHERE sic IS NOT NULL AND sic <> ''),
        movers AS (SELECT DISTINCT cik, first_sic, last_sic FROM ranked
                   WHERE first_sic <> last_sic)
        SELECT count(*) AS movers,
               count(*) FILTER (WHERE last_sic IN {FORM_CODES}) AS moved_into_form_code,
               count(*) FILTER (WHERE first_sic IN {FORM_CODES}) AS moved_out_of_form_code,
               count(*) FILTER (WHERE substr(lpad(first_sic, 4, '0'), 1, 2)
                                  <> substr(lpad(last_sic, 4, '0'), 1, 2))
                   AS changed_major_group
        FROM movers"""),

    ("10. STABILITY - the movers, worst first, with what they moved between", """
        WITH ranked AS (
            SELECT cik, sic, fy,
                   first_value(sic) OVER (PARTITION BY cik ORDER BY fy) AS first_sic,
                   last_value(sic) OVER (PARTITION BY cik ORDER BY fy
                       RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS last_sic
            FROM marts.credit_outcomes WHERE sic IS NOT NULL AND sic <> '')
        SELECT first_sic, last_sic, count(DISTINCT cik) AS companies
        FROM ranked WHERE first_sic <> last_sic
        GROUP BY 1, 2 ORDER BY companies DESC LIMIT 12"""),

    ("11. LABELS - codes that carry more than one description", """
        SELECT count(*) AS codes_in_use,
               count(*) FILTER (WHERE descriptions > 1) AS codes_with_several_labels,
               max(descriptions) AS worst
        FROM (SELECT sic, count(DISTINCT sic_description) AS descriptions
              FROM ref.dim_company
              WHERE sic IS NOT NULL AND sic <> '' AND sic_description IS NOT NULL
              GROUP BY sic)"""),

    ("12. IDENTITY - one company, two CIKs? Same name, different cik", """
        SELECT count(*) AS duplicated_names,
               sum(ciks) AS ciks_involved,
               sum(defaults) AS defaults_involved
        FROM (SELECT upper(trim(company_name)) AS nm,
                     count(DISTINCT cik) AS ciks,
                     count(*) FILTER (WHERE default_24m) AS defaults
              FROM marts.credit_outcomes
              WHERE company_name IS NOT NULL AND company_name <> ''
              GROUP BY 1 HAVING count(DISTINCT cik) > 1)"""),

    ("13. IMPACT - what the headline rate becomes with form codes removed", f"""
        SELECT 'all mapped rows' AS cut,
               count(*) AS company_years,
               count(*) FILTER (WHERE default_24m) AS defaults,
               round(100.0 * count(*) FILTER (WHERE default_24m) / count(*), 2) AS rate_pct
        FROM marts.credit_outcomes WHERE {FULL}
        UNION ALL
        SELECT 'excluding legal-form codes',
               count(*), count(*) FILTER (WHERE default_24m),
               round(100.0 * count(*) FILTER (WHERE default_24m) / count(*), 2)
        FROM marts.credit_outcomes
        WHERE {FULL} AND (sic IS NULL OR sic NOT IN {FORM_CODES})
        UNION ALL
        SELECT 'excluding movers (unstable SIC)',
               count(*), count(*) FILTER (WHERE default_24m),
               round(100.0 * count(*) FILTER (WHERE default_24m) / count(*), 2)
        FROM marts.credit_outcomes
        WHERE {FULL} AND cik IN (SELECT cik FROM marts.credit_outcomes
                                 GROUP BY cik HAVING count(DISTINCT sic) = 1)"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [d[0] for d in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)))[:60]
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
    for title, q in Q:
        print(f"\n### {title}")
        try:
            show(con, q)
        except Exception as exc:
            print(f"  (failed: {str(exc)[:200]})")


if __name__ == "__main__":
    main()
