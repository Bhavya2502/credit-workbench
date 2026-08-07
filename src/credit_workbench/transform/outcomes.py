"""Tracker L2 (foundation) — observed credit outcomes as a model target.

A scoring model needs to be trained against something that actually happened. The
original plan was to replicate agency ratings, but the agencies block automated access
to their Rule 17g-7 histories (see ingest/probe_ratings.py). What this platform already
holds is arguably a better target for credit purposes anyway: the events themselves.

For every company-year this asks a single question — *after the accounts were filed,
did the company get into trouble?* — and answers it at 12 and 24 month horizons from
the 8-K event feed.

The observation date is the filing date, not the balance sheet date. Nobody could act
on figures before they were published, so an outcome measured from the period end
would credit the model with foresight it never had. This is the same discipline as the
first-reported basis in the spreads: the model must only ever see what was knowable.

  default   bankruptcy, debt acceleration or a non-reliance declaration (severity 5)
  distress  the above plus impairments, auditor changes and late-filing notifications

Delisting is deliberately *excluded* from distress. A company acquired at a premium
files the same delisting notice as one thrown off the exchange for non-compliance, and
the two are opposite credit outcomes. Including them together produced a target that
barely separated by leverage (23.9% to 28.7% across quintiles) while default separated
cleanly (3.5% to 8.8%) — the signature of a contaminated label. Delistings are kept as
their own column, and `adverse_delisting_24m` isolates those with no acquisition or
change-of-control filing nearby.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

DEFAULT_CATEGORIES = ("distress", "audit")     # severity 5 lives in these
HORIZONS = (12, 24)


def main() -> None:
    md = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")

    print("Building credit outcomes ...")
    md.execute("""
        CREATE OR REPLACE TABLE marts.credit_outcomes AS
        WITH obs AS (
            -- one observation per company-year, dated when the accounts were filed
            SELECT s.cik, s.company_name, s.sic, substr(s.sic, 1, 2) AS sic2,
                   s.fy, s.period_end, s.last_filed AS observation_date
            FROM marts.spreads_a s
            WHERE s.basis = 'latest' AND s.is_primary_annual
              AND NOT s.is_empty_spread AND s.last_filed IS NOT NULL),
        ev AS (
            SELECT cik, filing_date, category, severity, item_code, description
            FROM events.corp_events
            WHERE severity >= 4 AND filing_date IS NOT NULL),
        -- completion of an acquisition or a change of control: the benign
        -- explanation for a delisting
        ma AS (
            SELECT cik, filing_date FROM events.corp_events
            WHERE item_code IN ('2.01', '5.01') AND filing_date IS NOT NULL),
        joined AS (
            SELECT o.*, e.filing_date AS event_date, e.category, e.severity,
                   e.item_code, e.description,
                   date_diff('day', o.observation_date, e.filing_date) AS days_after,
                   EXISTS (SELECT 1 FROM ma
                           WHERE ma.cik = o.cik
                             AND ma.filing_date BETWEEN e.filing_date - INTERVAL 180 DAY
                                                    AND e.filing_date + INTERVAL 90 DAY)
                       AS near_ma_event
            FROM obs o
            LEFT JOIN ev e
              ON e.cik = o.cik
             AND e.filing_date > o.observation_date
             AND e.filing_date <= o.observation_date + INTERVAL 24 MONTH)
        SELECT cik, any_value(company_name) AS company_name, any_value(sic) AS sic,
               any_value(sic2) AS sic2, fy, period_end, observation_date,
               -- did anything severe happen, and how soon
               count(*) FILTER (WHERE event_date IS NOT NULL)          AS events_24m,
               min(days_after)                                          AS days_to_first_event,
               arg_min(category, days_after)                            AS first_event_category,
               arg_min(description, days_after)                         AS first_event,
               max(severity)                                            AS worst_severity_24m,
               -- distress excludes delisting: see the module docstring
               coalesce(bool_or(days_after <= 365 AND category <> 'listing'), FALSE)
                                                                        AS distress_12m,
               coalesce(bool_or(days_after <= 730 AND category <> 'listing'), FALSE)
                                                                        AS distress_24m,
               -- default: severity 5 only (bankruptcy, debt acceleration, non-reliance)
               coalesce(bool_or(severity = 5 AND days_after <= 365), FALSE) AS default_12m,
               coalesce(bool_or(severity = 5 AND days_after <= 730), FALSE) AS default_24m,
               coalesce(bool_or(item_code = '1.03' AND days_after <= 730), FALSE)
                                                                        AS bankruptcy_24m,
               coalesce(bool_or(item_code = '2.04' AND days_after <= 730), FALSE)
                                                                        AS debt_acceleration_24m,
               coalesce(bool_or(item_code = '4.02' AND days_after <= 730), FALSE)
                                                                        AS non_reliance_24m,
               coalesce(bool_or(category = 'late_filing' AND days_after <= 730), FALSE)
                                                                        AS late_filing_24m,
               coalesce(bool_or(category = 'listing' AND days_after <= 730), FALSE)
                                                                        AS delisting_24m,
               coalesce(bool_or(category = 'listing' AND days_after <= 730
                                AND NOT near_ma_event), FALSE)          AS adverse_delisting_24m
        FROM joined
        GROUP BY cik, fy, period_end, observation_date""")
    rows, companies = md.execute(
        "SELECT count(*), count(DISTINCT cik) FROM marts.credit_outcomes").fetchone()
    print(f"table marts.credit_outcomes  {rows:,} company-years, {companies:,} companies")

    # The modelling table: ratios and flags as at the filing, outcomes after it.
    print("Assembling the model dataset ...")
    md.execute("""
        CREATE OR REPLACE TABLE marts.model_dataset AS
        SELECT o.cik, o.company_name, o.sic, o.sic2, o.fy, o.period_end,
               o.observation_date,
               o.distress_12m, o.distress_24m, o.default_12m, o.default_24m,
               o.bankruptcy_24m, o.delisting_24m, o.adverse_delisting_24m,
               o.days_to_first_event, o.first_event_category,
               r.* EXCLUDE (cik, company_name, sic, sic2, fy, period_end, basis)
        FROM marts.credit_outcomes o
        JOIN marts.ratios r
          ON r.cik = o.cik AND r.fy = o.fy AND r.period_end = o.period_end
         -- first_reported, never latest: a model trained on restated figures is
         -- using numbers that did not exist on the observation date
         AND r.basis = 'first_reported'""")
    n, ncomp = md.execute(
        "SELECT count(*), count(DISTINCT cik) FROM marts.model_dataset").fetchone()
    print(f"table marts.model_dataset  {n:,} rows, {ncomp:,} companies")


if __name__ == "__main__":
    main()
