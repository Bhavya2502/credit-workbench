"""Is `business_country` a country, a US state, or both at once?

The values include CA, NY, TX and also "China", so the field is not a country field.
"CA" is the dangerous case - California or Canada, and 2,211 companies carry it. This
compares it against `business_state` and `state_of_incorporation` to find a derivation
that is defensible, because a country column built on a guess would mislabel thousands
of companies while looking entirely reasonable.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

HELD = "d.cik IN (SELECT DISTINCT cik FROM marts.spreads_a)"

Q = [
    ("1. state and country side by side, most common pairs", f"""
        SELECT coalesce(nullif(d.business_state, ''), '(blank)') AS business_state,
               coalesce(nullif(d.business_country, ''), '(blank)') AS business_country,
               count(*) AS companies
        FROM ref.dim_company d WHERE {HELD}
        GROUP BY 1, 2 ORDER BY companies DESC LIMIT 20"""),

    ("2. Is business_country ever different from business_state?", f"""
        SELECT count(*) AS companies,
               count(*) FILTER (WHERE d.business_state = d.business_country) AS identical,
               count(*) FILTER (WHERE d.business_state <> d.business_country) AS differ,
               count(*) FILTER (WHERE d.business_state IS NULL
                                   OR d.business_state = '') AS state_blank
        FROM ref.dim_company d WHERE {HELD}"""),

    ("3. The non-US-looking values - are they spelled-out countries?", f"""
        SELECT d.business_country, count(*) AS companies
        FROM ref.dim_company d WHERE {HELD}
          AND length(d.business_country) > 2
        GROUP BY 1 ORDER BY companies DESC LIMIT 25"""),

    ("4. And state_of_incorporation, for the same companies", f"""
        SELECT coalesce(nullif(d.state_of_incorporation, ''), '(blank)') AS soi,
               coalesce(nullif(d.state_of_incorporation_description, ''), '(blank)') AS soi_name,
               count(*) AS companies
        FROM ref.dim_company d WHERE {HELD}
        GROUP BY 1, 2 ORDER BY companies DESC LIMIT 20"""),
]


def show(con, q):
    cur = con.execute(q)
    heads = [dd[0] for dd in cur.description]
    rows = [[("" if v is None else (f"{v:,}" if isinstance(v, int) else str(v)))[:46]
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
