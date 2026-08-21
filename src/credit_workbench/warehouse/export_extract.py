"""A flat company-year extract: identity, industry, and four measures.

    uv run python -m credit_workbench.warehouse.export_extract

One row per company-year, 129,224 of them, as CSV. The requested columns come first and
in the requested order; everything after them is there so a figure can be checked rather
than taken on trust.

**gics_sub_industry is empty, and that is not an oversight.** GICS is licensed from S&P
and MSCI and nothing in this platform ingests it; every column name in the database was
searched and the only classification field outside SIC is `ref.sic_naics.naics`, which
holds zero rows. The column is emitted anyway so the schema matches what was asked for,
and `sic_description`, `peer_group` and `division` follow it as the classifications that
do exist.

**form_type is chosen, not looked up.** `marts.spreads_a` carries no form. The forms live
on `marts.spread_lines`, one per line, and the spread resolves each line against the best
filing available for it - so 81,318 company-periods draw lines from more than one form,
a 10-K and its 10-K/A being the common case. `form_type` is the form that supplied the
most lines for that period, and `forms_in_period` says how many were involved so a mixed
row is visible rather than hidden.

**ebitda is derived, so its inputs travel with it.** `operating_income` is the tag as
filed and is present for 93,533 company-years; `ebit_calc` is the platform's derived EBIT
at 107,141. Where all three are present, `ebitda` agrees with `ebit_calc + D&A` to within
1% on 93.8% of rows - the rest differ on whether the D&A came from the income statement
or the cash flow, which is why both are columns.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

from credit_workbench.common.config import motherduck_token

OUT = Path("export")
BASIS = "first_reported"

EXTRACT = f"""
WITH forms AS (
    -- one form per company-period: the one that supplied the most lines
    SELECT cik, period_end,
           arg_max(form, n) AS form_type,
           count(*) AS forms_in_period
    FROM (SELECT cik, period_end, form, count(*) AS n
          FROM marts.spread_lines
          WHERE basis = '{BASIS}' AND qtrs IN (0, 4) AND form IS NOT NULL
          GROUP BY cik, period_end, form)
    GROUP BY cik, period_end)
SELECT s.cik,
       s.company_name,
       s.fy                              AS fiscal_year,
       s.period_end                      AS fiscal_period_end_date,
       f.form_type,
       s.sic,
       CAST(NULL AS VARCHAR)             AS gics_sub_industry,
       s.revenue,
       s.ebitda,
       s.total_assets,
       s.capex,
       -- everything below is context for the columns above
       h.sic4_description                AS sic_description,
       g.industry_code                   AS peer_group_code,
       g.industry_label                  AS peer_group,
       h.division_name                   AS division,
       s.operating_income,
       s.ebit_calc,
       s.dep_amort_is,
       s.dep_amort_cf,
       f.forms_in_period,
       s.last_filed                      AS filed_date,
       '{BASIS}'                         AS basis
FROM marts.spreads_a s
LEFT JOIN forms f ON f.cik = s.cik AND f.period_end = s.period_end
LEFT JOIN ref.sic_hierarchy h ON h.sic4 = s.sic
LEFT JOIN ref.industry_group g ON g.sic4 = s.sic
WHERE s.basis = '{BASIS}' AND s.is_primary_annual
ORDER BY s.company_name, s.fy
"""


def guard(con) -> int:
    """No join here may multiply rows; each would inflate the extract silently."""
    expected = con.execute(f"""
        SELECT count(*) FROM marts.spreads_a
        WHERE basis = '{BASIS}' AND is_primary_annual""").fetchone()[0]
    actual = con.execute(f"SELECT count(*) FROM ({EXTRACT})").fetchone()[0]
    print(f"  guard {actual:,} extract rows against {expected:,} source rows")
    if actual != expected:
        raise SystemExit(
            f"a join fanned out ({actual:,} vs {expected:,}): the extract would repeat "
            "company-years. Fix the key before writing the file.")
    dupes = con.execute(
        f"SELECT count(*) - count(DISTINCT (cik, fiscal_year)) FROM ({EXTRACT})"
    ).fetchone()[0]
    if dupes:
        raise SystemExit(f"{dupes:,} duplicated company-years in the extract.")
    return actual


def main() -> None:
    OUT.mkdir(exist_ok=True)
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    con.execute("SET temp_directory = '/tmp/duckdb_spill'")
    rows = guard(con)

    path = OUT / "company_year_extract.csv"
    con.execute(f"COPY ({EXTRACT}) TO '{path.as_posix()}' (HEADER, DELIMITER ',')")
    print(f"wrote {path}  {rows:,} rows  {path.stat().st_size / 1e6:.1f} MB")

    print("\nfill rate per column:")
    cols = [d[0] for d in con.execute(f"SELECT * FROM ({EXTRACT}) LIMIT 0").description]
    sel = ", ".join(f"count({c}) AS {c}" for c in cols)
    got = con.execute(f"SELECT {sel} FROM ({EXTRACT})").fetchone()
    for c, n in zip(cols, got):
        print(f"  {c:<26}{n:>9,}  {100.0 * n / rows:>5.1f}%")

    print("\nform_type breakdown:")
    for form, n in con.execute(f"""
            SELECT form_type, count(*) FROM ({EXTRACT})
            GROUP BY 1 ORDER BY 2 DESC LIMIT 12""").fetchall():
        print(f"  {str(form):<12}{n:>9,}")


if __name__ == "__main__":
    main()
