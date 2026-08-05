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
-- Tracker C3: SEC Financial Statement and Notes data sets, loaded verbatim from TSVs.
-- Column set mirrors the SEC file spec, loaded per monthly/quarterly archive.
CREATE TABLE IF NOT EXISTS raw.fsn_sub (      -- one row per filing (submission)
    adsh     VARCHAR PRIMARY KEY,             -- accession number
    cik      BIGINT NOT NULL,
    name     VARCHAR,
    sic      VARCHAR,
    form     VARCHAR,                         -- 10-K, 10-Q, ...
    period   DATE,                            -- balance sheet date
    fy       SMALLINT,
    fp       VARCHAR,                         -- FY, Q1..Q3
    filed    DATE,
    accepted TIMESTAMP,
    prevrpt  BOOLEAN,                         -- superseded by amendment?
    detail   BOOLEAN,
    instance VARCHAR,
    src_file VARCHAR                          -- which SEC archive this came from
);

CREATE TABLE IF NOT EXISTS raw.fsn_num (      -- one row per numeric fact (incl. notes detail)
    adsh     VARCHAR NOT NULL,
    tag      VARCHAR NOT NULL,                -- us-gaap or extension tag
    version  VARCHAR NOT NULL,
    ddate    DATE NOT NULL,                   -- data date
    qtrs     SMALLINT NOT NULL,               -- 0 = instant, N = duration in quarters
    uom      VARCHAR NOT NULL,                -- USD, shares, ...
    dimh     VARCHAR,                         -- dimension hash (segments/axes live here)
    iprx     SMALLINT,
    value    DOUBLE,
    footnote VARCHAR,
    src_file VARCHAR
);

CREATE TABLE IF NOT EXISTS raw.fsn_tag (      -- tag dictionary
    tag      VARCHAR NOT NULL,
    version  VARCHAR NOT NULL,
    abstract BOOLEAN,
    datatype VARCHAR,
    iord     VARCHAR,
    crdr     VARCHAR,
    tlabel   VARCHAR,
    doc      VARCHAR
);

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
