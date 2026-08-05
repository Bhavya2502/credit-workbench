-- Credit Workbench warehouse schemas (tracker A6). Target: MotherDuck (DuckDB).
-- Design rationale and naming conventions: docs/architecture.md
-- Zones: raw (as-received) -> staging (typed, deduped, point-in-time) -> marts (analysis-ready)
-- Side schemas: ref (reference/master data), quali (text corpus indexes), events (event feeds)

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS marts;
CREATE SCHEMA IF NOT EXISTS ref;
CREATE SCHEMA IF NOT EXISTS quali;
CREATE SCHEMA IF NOT EXISTS events;

-- ---------------------------------------------------------------- ref
-- Tracker B1: SEC company master (weekly upsert from company_tickers.json + submissions API)
CREATE TABLE IF NOT EXISTS ref.dim_company (
    cik            BIGINT PRIMARY KEY,
    ticker         VARCHAR,
    company_name   VARCHAR NOT NULL,
    sic            VARCHAR,          -- 4-digit SIC from EDGAR
    sic_desc       VARCHAR,
    naics          VARCHAR,          -- via ref.sic_naics crosswalk (B4)
    fiscal_year_end VARCHAR,         -- MMDD as reported
    exchange       VARCHAR,
    state_incorp   VARCHAR,
    is_active      BOOLEAN DEFAULT TRUE,
    updated_at     TIMESTAMP NOT NULL
);

-- Tracker B4: static crosswalk, single load
CREATE TABLE IF NOT EXISTS ref.sic_naics (
    sic    VARCHAR NOT NULL,
    naics  VARCHAR NOT NULL,
    title  VARCHAR
);

-- ---------------------------------------------------------------- raw
-- SEC bulk datasets are not declared here: the ingest jobs land them in R2 as parquet
-- and warehouse/build_views.py exposes them, so column sets always track the SEC spec.
--   raw.fsds_num / _pre / _tag        views over parquet   (C2, face financials)
--   raw.fsds_sub                      materialised          filing headers
--   raw.fsn_num / _txt / _dim / _pre / _cal / _ren / _tag   views over parquet (C3)
--       _txt = full footnote text, _dim = segment/axis dimensions
--   raw.fsn_sub                       materialised          filing headers incl. public float
--   ref.filing_index                  view over parquet     every EDGAR filing (feeds H1/H3)
--   ref.xbrl_tag                      materialised          deduplicated tag dictionary

-- ---------------------------------------------------------------- staging (built by transforms)
-- staging.facts_pit  (C4): deduped facts with point-in-time vintage flags (first-reported vs restated)
-- staging.tag_map    (C5): us-gaap tag -> spread template line mapping, with coverage stats
-- staging.note_inputs (D1): lease/pension/debt/one-off inputs extracted for adjustments
-- Definitions land here as each transform is built, every column documented in docs/ (A7).

-- ---------------------------------------------------------------- marts (built by transforms)
-- marts.spreads_a / spreads_q (C6): bank-style spread template, annual + quarterly + TTM
-- marts.spreads_adj (D3): agency-style adjusted figures (adj debt, adj EBITDA, FFO, RCF)
-- marts.ratios (E1), marts.benchmarks (E2): ratio library + industry percentile tables
-- marts.segments (F1), marts.concentration (F2)
-- marts.ratings / marts.transitions (H2): NRSRO rating histories + transition matrices

-- ---------------------------------------------------------------- events
-- events.corp_events (H1): 8-K item-code feed; events.filing_signals (H3): NT filings, Form 25

-- ---------------------------------------------------------------- quali
-- quali.filing_sections (G1), quali.audit_flags (G2), quali.governance (G3), quali.scores (G5)
