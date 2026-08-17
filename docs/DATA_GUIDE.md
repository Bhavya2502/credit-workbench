# Credit Workbench — data guide

Everything needed to query this warehouse correctly. Column names are verified against
the live database; the SQL below runs as written.

**Database:** MotherDuck `credit_workbench` · **Coverage:** SEC filers, 2009–2026 ·
**Scale:** 373.2m facts, 21,671 companies

---

## 1. Connect

```python
import duckdb
con = duckdb.connect("md:credit_workbench")   # prompts for auth on first use
```

With a token in the environment (never hard-code it):

```python
import os, duckdb
con = duckdb.connect(f"md:credit_workbench?motherduck_token={os.environ['MOTHERDUCK_TOKEN']}")
```

Read-only access is granted by a **MotherDuck share**, not by passing the token.

---

## 2. Three rules that decide whether your answer is right

Read these before writing any query. Each one has produced wrong numbers in this project.

### Rule 1 — always filter the vintage

`staging.facts_pit`, `marts.facts_dimensioned` and `marts.segments` hold **every
restatement** of every figure. A balance-sheet date is re-reported by ~3.1 filings; only
59% of segment rows are the latest vintage.

```sql
WHERE is_latest            -- best current knowledge
WHERE is_first_report      -- as first published; USE THIS FOR ANY MODEL
```

Without one of these you count each figure about three times. Anything a model trains on
must use `is_first_report`, or it learns from restatements that did not exist at the
observation date.

**What it costs, concretely.** Intel's FY2024 Client Computing Group revenue:

```sql
-- WRONG: $338bn
SELECT sum(value) FROM marts.segments
WHERE TRY_CAST(cik AS BIGINT) = 50863 AND fy = 2024
  AND member = 'ClientComputingGroup' AND qtrs = 4 AND uom = 'USD'
  AND tag = 'RevenueFromContractWithCustomerExcludingAssessedTax';

-- RIGHT: $30.29bn, the reported figure
SELECT sum(value) FROM marts.segments
WHERE TRY_CAST(cik AS BIGINT) = 50863 AND fy = 2024
  AND member = 'ClientComputingGroup' AND qtrs = 4 AND uom = 'USD'
  AND tag = 'RevenueFromContractWithCustomerExcludingAssessedTax'
  AND is_latest;
```

An eleven-fold overstatement that looks like a plausible number.

`marts.concentration` **now carries the flags too** — 1,157,593 rows, 701,161 latest
(60.6%) — so filter it the same way as the others. An earlier version of this guide told
you to dedupe it by hand on `filed`; that instruction was stale from 15 August and is
withdrawn. `marts.segments_all_vintages` and `marts.concentration_all_vintages` remain for
anyone who wants every vintage deliberately.

### Rule 2 — `marts.facts_by_note` is many-to-many

A figure disclosed on the balance sheet *and* in the debt note is two rows. That is the
disclosure, not duplication. Before summing money, pick a `note_category`:

```sql
WHERE note_category = 'statement'     -- primary statements
WHERE note_category = 'note'          -- note text
WHERE note_category = 'note_detail'   -- the schedules inside notes
```

### Rule 3 — dimensioned facts cross-tabulate

Filers cross one axis with another (fair-value level × asset class × recurring). Summing
every cell counts the same money repeatedly. For a clean total:

```sql
WHERE dimension_count = 1   -- this axis is the only one on the fact
```

---

## 3. Which table answers which question

| You want | Use | Filter |
|---|---|---|
| Standardised income statement / balance sheet / cash flow | `marts.spread_lines` | `basis` |
| Ratios for scoring | `marts.ratio_values` | `basis='first_reported'` |
| Peer-relative position | `marts.ratio_percentiles` | use `credit_percentile_size` |
| A ready modelling table | `marts.model_dataset` | already first-reported |
| Distress / default labels | `marts.credit_outcomes` | `distress_12m`, `default_24m` |
| Any raw XBRL number | `staging.facts_pit` | `is_latest` or `is_first_report` |
| Note schedules (by subsidiary, plan, instrument…) | `marts.facts_dimensioned` | `is_latest` |
| Lease ladders, pension, tax detail, share counts | `marts.adjustment_inputs` | `basis` |
| Debt instrument terms and rates | `marts.debt_instruments` | — |
| Revolver headroom | `marts.revolver_capacity` | — |
| Covenant levels | `marts.covenant_headline` | already high-confidence |
| Segment / geographic detail | `marts.segments` | `axis` |
| Guarantor vs non-guarantor | `marts.legal_entity_detail` | `entity_role` |
| Level 1/2/3 fair value | `marts.fair_value_hierarchy` | `dimension_count=1` |
| Risk factors, MD&A text | `quali.risk_factors`, `quali.mdna` | — |
| Credit-warning signals from notes | `quali.note_signals` | see caveat below |
| 8-K events | `marts.credit_events` | `category`, `severity` |
| Everything in one note | `marts.facts_by_note` | `note_type` + `note_category` |
| What a tag means | `ref.tag_catalog` | — |
| Company master | `ref.dim_company` | — |
| **Peer groups above SIC2** | `ref.industry_group` | join `sic4` = your `sic` |
| **SIC hierarchy (group/division)** | `ref.sic_hierarchy` | — |
| **Which ratios compute for a cohort, and their distribution** | `marts.ratio_coverage` | `is_sufficient` |
| **How many credit events a cohort holds** | `marts.outcome_counts` | `can_calibrate_*` |

---

## 4. Working queries

### One company's ratios over time

```sql
SELECT fy, ratio, value
FROM marts.ratio_values
WHERE cik = '0000320193' AND basis = 'first_reported'
  AND ratio IN ('debt_to_ebitda', 'ebitda_interest_cover', 'ffo_to_debt')
ORDER BY fy, ratio;
```

### Screen on leverage and coverage, peer-adjusted

```sql
SELECT p.cik, p.company_name, p.sic2, p.fy,
       p.ratio, p.value, p.credit_percentile_size
FROM marts.ratio_percentiles p
WHERE p.fy = 2024 AND p.ratio = 'debt_to_ebitda'
  AND p.credit_percentile_size >= 90        -- weakest decile within size peers
ORDER BY p.credit_percentile_size DESC
LIMIT 50;
```

### A company's spread, one statement

```sql
SELECT line_no, label, value
FROM marts.spread_lines
WHERE cik = '0000320193' AND fy = 2024 AND qtrs = 4
  AND basis = 'latest' AND statement = 'IS'
ORDER BY line_no;
```

### Covenant headroom — level against actual

```sql
SELECT h.cik, h.covenant_type, h.direction, h.binding_level,
       r.value AS actual, h.filing_date, h.evidence
FROM marts.covenant_headline h
JOIN marts.ratio_values r
  ON r.cik = h.cik AND r.basis = 'latest' AND r.fy = 2024
 AND r.ratio = 'debt_to_ebitda'
WHERE h.covenant_type = 'total_leverage' AND h.direction = 'max'
ORDER BY (h.binding_level - r.value);          -- smallest headroom first
```

### Everything in one company's debt note

```sql
SELECT note_title, tag, presented_label, value, uom
FROM marts.facts_by_note
WHERE cik = '0000320193' AND fy = 2024
  AND note_type = 'debt' AND note_category = 'note_detail'
  AND is_latest
ORDER BY report, line;
```

### Structural subordination — how much sits at non-guarantors

```sql
SELECT entity_role, tag, sum(value) AS total
FROM marts.legal_entity_detail
WHERE cik = '0000320193' AND fy = 2024
  AND tag IN ('Assets', 'Revenues') AND uom = 'USD'
GROUP BY 1, 2 ORDER BY 2, 1;
```

### Fair-value Level 3 concentration

```sql
SELECT cik, company_name, period_end,
       sum(value) FILTER (WHERE hierarchy_level = 'Level 3') AS level3,
       sum(value) FILTER (WHERE hierarchy_level LIKE 'Level%') AS total,
       round(100.0 * sum(value) FILTER (WHERE hierarchy_level = 'Level 3')
             / nullif(sum(value) FILTER (WHERE hierarchy_level LIKE 'Level%'), 0), 1) AS pct_level3
FROM marts.fair_value_hierarchy
WHERE tag = 'AssetsFairValueDisclosure' AND uom = 'USD'
  AND qtrs = 0 AND dimension_count = 1 AND fy = 2024
GROUP BY 1, 2, 3
HAVING total > 0
ORDER BY pct_level3 DESC LIMIT 25;
```

### Debt maturity wall

```sql
SELECT cik, company_name, period_end,
       debt_due_y1, debt_due_y2, debt_due_y3, debt_due_y4, debt_due_y5,
       debt_due_thereafter
FROM marts.adjustment_inputs
WHERE basis = 'latest' AND fy = 2024 AND debt_due_y1 IS NOT NULL
ORDER BY debt_due_y1 DESC LIMIT 25;
```

### Risk factor text for an LLM pass

```sql
SELECT cik, filing_date, char_len, text
FROM quali.risk_factors
WHERE cik = '0000320193'
ORDER BY filing_date DESC LIMIT 1;
```

### Find any tag and what claims it

```sql
SELECT tag, label, total_facts, companies, in_spread_template, spread_line
FROM ref.tag_catalog
WHERE tag ILIKE '%leverage%' AND standard_taxonomy
ORDER BY total_facts DESC LIMIT 20;
```

---

## 5. Key columns

Only the columns you need most. `information_schema.columns` has the rest.

**`staging.facts_pit`** — `cik, company_name, sic, adsh, tag, uom, value, period_end,
qtrs, fy, period_year, stmt, filed, is_first_report, is_latest, times_reported`
`qtrs`: 0 = instant (balance sheet), 4 = annual, 1 = quarterly.

**`marts.spread_lines`** — `cik, period_end, qtrs, fy, statement, line_no, line_code,
label, value, basis, source_tag`
`statement`: `IS`, `BS`, `CF`, `MEMO`. `basis`: `latest` | `first_reported`.

**`marts.ratio_values`** — `cik, sic2, basis, fy, period_end, size_band, ratio, value`

**`marts.ratio_percentiles`** — adds `percentile_in_industry, percentile_in_size_peers,
credit_percentile, credit_percentile_size, peer_count`
Use `credit_percentile_size`; higher = weaker credit.

**`marts.model_dataset`** — one row per company-year, first-reported, with 60+ ratio
columns plus labels `distress_12m, distress_24m, default_12m, default_24m,
bankruptcy_24m` and `observation_date`.

**`marts.covenant_terms`** — `cik, adsh, filing_date, covenant_type, direction, level,
unit, level_index, is_schedule, applies_from, confidence, sentence, file_name`
`direction`: `max` (ceiling) | `min` (floor). `confidence`: `high` | `low`.

**`marts.covenant_headline`** — `cik, covenant_type, direction, binding_level,
levels_in_schedule, filing_date, evidence` — high-confidence only, most recent agreement.

**`marts.debt_instruments`** — `member, instrument_type, maturity_year, maturity_source,
face_amount, carrying_amount, stated_rate, effective_rate, basis_spread, fair_value,
facility_maximum, facility_remaining, facility_drawn`

**`marts.credit_outcomes`** — `observation_date, events_24m, days_to_first_event,
first_event_category, worst_severity_24m, distress_12m, distress_24m, default_12m,
default_24m, bankruptcy_24m, debt_acceleration_24m, non_reliance_24m, late_filing_24m`

**`marts.facts_by_note`** — `cik, adsh, period_end, qtrs, fy, tag, uom, value, is_latest,
note_title, note_title_normalised, note_type, note_category, presented_label, line, report`

**`marts.adjustment_inputs`** — 106 columns grouped by prefix: `op_lease_*`,
`fin_lease_*`, `op_lease_840_*`, `pension_*`, `debt_due_*`, `intangible_amort_*`,
`ppe_*`, tax (`effective_tax_rate`, `deferred_tax_*`, `unrecognised_tax_benefits`),
capital structure (`common_shares_*`, `preferred_shares_*`, `treasury_shares`,
`entity_public_float`), `receivables_gross`, `allowance_doubtful_accounts`,
`derivative_notional`, and one-off items.

**`quali.filing_sections`** — `cik, adsh, filing_date, period_of_report, item,
item_title, char_len, text`. `item`: `'1'`, `'1A'`, `'1C'`, `'3'`, `'7'`, `'7A'`, `'9A'`…
Item 8 is deliberately absent — it is the financial statements, held as XBRL.

**`marts.segments`** — `axis, member, full_dimension, tag, value`.
`axis` ∈ `BusinessSegments`, `ProductOrService`, `Geographical`, `ConsolidationItems`.

---

## 6. Join keys

| Key | Joins |
|---|---|
| `cik` | company. **Cast before joining** — zero-padded string in some tables, integer in others |
| `adsh` | the filing (accession number) |
| `period_end` + `qtrs` | the reporting period |
| `fy` | fiscal year, derived from `period_end`, not the filer's own `fy` field |
| `dimh` + `period` | dimensioned fact → `ref.dimension_index.dimhash` |
| `adsh` + `report` + `period` | fact → `ref.note_index` |

```sql
-- cik types differ between tables; normalise
ON lpad(CAST(a.cik AS VARCHAR), 10, '0') = lpad(CAST(b.cik AS VARCHAR), 10, '0')
```

---

## 7. Caveats that will bite

- **`material_weakness` in `quali.note_signals` discriminates inversely** — companies
  flagged default *less*. Do not put it in a model. Documented, not removed.
- **Superseded tables remain.** `marts.ratios` (254,380 rows) is *not*
  `marts.ratio_values` (6,660,518). `marts.spreads_a`/`spreads_q` are *not*
  `marts.spread_lines` (52,658,794). The current ones are listed in this guide.
- **`ref.sic_naics` is empty.** The crosswalk was never built.
- **Covenants cover 2,690 companies**, not all filers — only those filing a credit
  agreement as an 8-K exhibit. Absence is not evidence of covenant-lite.
- **`net_worth max`** holds ~30 implausible rows (a net-worth ceiling of 40). Left visible.
- **ASC 840 vs ASC 842 leases are separate columns.** `op_lease_840_*` is the pre-2019
  regime. Splicing them into one series is your call, not a default.
- **Other schemas are not this project.** `gold`, `silver`, `catalog` belong to a separate
  credit workstream (India LGD/PD, IBBI insolvency, FDIC, Bondora). `hn`, `kaggle`, `nyc`,
  `stackoverflow_survey` are MotherDuck samples.
- **MotherDuck enforces a daily compute limit** on the free plan. Heavy scans over
  `marts.facts_dimensioned` (222m rows) or `ref.tag_note_map` (279m rows) consume it
  quickly. Filter by `cik`, `fy` or `period_year` before aggregating, and prefer the
  materialised marts over the raw layer.

### Segment coverage is not patchy — check the size band

A company with one reportable segment discloses none, so absence is usually correct:

| Size band | Companies with segments |
|---|---|
| Over $10bn | 89.0% |
| $1bn–$10bn | 88.4% |
| $100m–$1bn | 84.8% |
| Under $100m | 62.8% |

If a specific large company returns nothing, check the `cik` format before concluding the
data is missing — `TRY_CAST(cik AS BIGINT) = 50863` is the safest form.

---

## 8. Not in the database

Plan around these — they do not exist:

agency adjustments (inputs only, no adjusted debt/EBITDA/FFO) · credit ratings and rating
histories · macro series and CCAR/DFAST scenario paths · projections or forecasts ·
feature store or fitted model · Census QFR, Damodaran or ratio-by-rating benchmarks ·
SIC–NAICS crosswalk · non-US filers (IFRS appears only via 20-F)

Governance data now exists — see §11 — but read the coverage table there before scoring
on it. There is no `AuditorName`, ICFR attestation flag or Item 402(v) tag anywhere in
`staging.facts_pit`: those are `dei` and `ecd` tags, and the warehouse holds numeric
financial-statement facts. Everything governance is read out of the proxy text.

---

## 9. Orientation queries

```sql
-- everything available
SELECT table_schema, table_name FROM information_schema.tables
WHERE table_schema IN ('staging','marts','quali','ref') ORDER BY 1, 2;

-- columns of anything
SELECT column_name, data_type FROM information_schema.columns
WHERE table_schema = 'marts' AND table_name = 'model_dataset' ORDER BY ordinal_position;

-- what ratios exist
SELECT * FROM ref.ratio_definitions;

-- what note types exist
SELECT note_type, note_category, filings FROM ref.note_catalog ORDER BY filings DESC;

-- which mart claims a tag
SELECT * FROM ref.modelled_tags WHERE tag = 'Revenues';

-- coverage for one company
SELECT 'facts' AS layer, count(*) FROM staging.facts_pit WHERE cik='0000320193' AND is_latest
UNION ALL SELECT 'ratios', count(*) FROM marts.ratio_values WHERE cik='0000320193'
UNION ALL SELECT 'covenants', count(*) FROM marts.covenant_terms WHERE cik='0000320193'
UNION ALL SELECT 'risk factors', count(*) FROM quali.risk_factors WHERE cik='0000320193';
```

---

## 10. Cohorts, coverage and outcome counts

Built for scorecard design: what computes for whom, how it is distributed, and whether
there are enough events to calibrate against.

### Peer groups above SIC2 — `ref.industry_group`

`sic4, sic3, sic2, division_code, division_name, sic4_description, industry_level,
industry_code, industry_label, peers, custom_industry`. **Join on `sic4`**, which matches
the `sic` column carried by `marts.ratio_values` and `marts.credit_outcomes`.

410 SIC codes map to **140 peer groups**. Each was rolled up only as far as needed to reach
30 comparable companies — `industry_level` records how far that was, and `peers` how many it
reached. Four-digit SIC is too thin (263 of 400 codes have fewer than ten companies, median
six) and the 70 two-digit major groups are too blunt; 140 sits in the gap.

`custom_industry` is deliberately `NULL` — it is reserved for a house scheme that is a
business decision, not a data one. `ref.sic_hierarchy` carries the raw hierarchy beneath.

### `marts.ratio_coverage` — which ratios are usable for a cohort

Grain: `industry_scheme × industry_code × size_band × fy × basis × ratio`. Columns
`companies_total, companies_with_value, coverage_pct, is_sufficient, p10, p25, p50, p75,
p90, min_value, max_value`.

**Read `industry_scheme` first.** Every cohort appears twice, and the choice is a genuine
trade-off — neither scheme dominates:

| `industry_scheme` | Groups | Cohort size | Homogeneity |
|---|---|---|---|
| `sic2` | 73 | **larger** — 38.3% of pooled cells reach 30 companies | blunt: all "retail" is one peer set |
| `peer_group` | 140 | smaller — 29.5% reach 30 | **finer**: warehouse clubs separated from apparel specialty |

The 140 peer groups sit *between* four-digit SIC and the major groups. They were built to be
more homogeneous than `sic2`, **not larger** — being finer, their cohorts are necessarily
smaller. Use `peer_group` when comparability matters more than sample size, `sic2` when you
need the cell to be big enough at all, and `is_sufficient` (30+ companies with a value) to
decide either way.

Why the flag matters: at `ratio × sic2 × size_band`, FY2024 gives 12,127 cells of which only
1,409 hold 30+ companies and 7,663 hold fewer than 10 — median 6. **A p10 and p90 over six
companies are noise wearing a precise number.** `size_band = 'ALL'` pools the sizes and is
often the only cut with enough companies: it lifts the sufficient share from 2–17% to 29–41%.

```sql
-- thresholds for a factor, only where the cohort can support them
SELECT industry_code, industry_label, size_band, p25, p50, p75, companies_with_value
FROM marts.ratio_coverage
WHERE ratio = 'debt_to_ebitda' AND fy = 2024 AND basis = 'first_reported'
  AND industry_scheme = 'peer_group' AND is_sufficient
ORDER BY p50 DESC;
```

### `marts.outcome_counts` — whether a scale can be calibrated

Grain: `industry_scheme × industry_code × size_band × fy`, with `fy IS NULL` meaning the
whole window pooled. Counts for `distress_12m/24m`, `default_12m/24m`, `bankruptcy_24m`,
`debt_acceleration_24m`, `non_reliance_24m`, `adverse_delisting_24m`, plus
`distress_24m_rate`, `default_24m_rate` and the flags `can_calibrate_default` /
`can_calibrate_distress` (30+ events).

The pooled `fy IS NULL` row is usually the only one with enough events for a cohort. Source
totals across the whole warehouse: 127,190 company-years, 21,778 `distress_12m`, 29,709
`distress_24m`, 7,458 `default_24m`, 2,823 `bankruptcy_24m`.

---

## 11. Governance and the Management Risk scorecard (tracker G3)

`quali.proxy_sections` holds DEF 14A text split into named governance sections, and
`marts.governance_metrics` holds one narrow row per proxy filing with the numbers read out
of it. Named views: `quali.proxy_governance`, `proxy_independence`, `proxy_related_party`,
`proxy_audit_fees`, `proxy_compensation`, `proxy_committees`.

**`quali.proxy_sections`** — `cik, adsh, form, filing_date, period_of_report, section,
section_title, char_len, text`. 20 sections; a median of 13 are found per filing.
`section` ∈ `governance, independence, committees, risk_oversight, attendance, nominees,
related_party, audit_fees, audit_report, cda, summary_comp, director_comp, pay_ratio,
pay_vs_perf, ownership, section16, equity_plan, say_on_pay, clawback, hedging`.

Unlike `quali.filing_sections`, **this text keeps table rows on one line**, cells joined
by ` | `. That is deliberate: the fee, director and compensation tables are only readable
while a label still sits beside its number. Any parser you write over this text should
split lines on `|`.

**`marts.governance_metrics`** — `audit_fees, audit_related_fees, tax_fees, other_fees,
total_fees_stated, fee_components_sum, non_audit_fee_ratio, fee_units,
fee_source_section, directors_listed, directors_marked_independent,
independence_statement, ceo_pay_ratio, related_party_chars, related_party_none_stated,
related_party_max_amount, sections_found`, plus `has_clawback_policy, has_hedging_policy,
has_say_on_pay, has_pay_vs_performance, has_cda, has_section16_disclosure`.

### Coverage, measured on a 60-filing sample — read this before scoring

Measured on the full filing-year 2024 load — 4,864 scored filings, not a sample.

| Metric | Fires on | Trust |
|---|---|---|
| `audit_fees`, `non_audit_fee_ratio` | 77% | high — **98.4%** of filings stating a total tie to it; median $1.40m |
| all four fee categories | 52% | high |
| related-party section | 75% | high as text; the dollar figure is the largest in the section, **not a total** |
| CEO pay ratio | 39% | good — bounded to 1…10,000; median 87 |
| section flags (`has_*`) | 38–75% | presence of the section, not the substance of the policy |
| director table (`directors_listed`) | 47% | good — median board 7, floor of 4 |
| `directors_marked_independent` | 8% | only where the table carries an independence column |
| `independence_statement` | 57% | evidence text for a human or LLM pass, **not a count** |

`fee_units_overridden` marks the 0.8% of filings whose own units note was ignored because
applying it produced an impossible fee — Hyatt's 2024 proxy heads its table "(in
millions)" above figures plainly in dollars. Treat those rows as read-but-corrected.

**Board independence is deliberately not a number.** The clean phrasing — "X of our Y
directors are independent" — appears in 2% of proxies, and a looser pattern matched prose
like "acting as a liaison between the independent directors", a count with no count in it.
So it is read from the director table where there is one and left `NULL` where there is
not. A `NULL` means "not stated in a form we can trust". Do not coalesce it to zero.

**Fees are found by the table's shape, not its heading** — the fee table sits under
"Ratification of Appointment…" as often as under a fee heading, so `fee_source_section`
records where it was actually found. The 33% not found are filers who describe fees in
prose, or who transpose the table so the categories are column headers.

`fee_units` records whether the table was stated in dollars, thousands or millions; the
stored figures are already scaled to dollars.

```sql
-- auditor independence: non-audit fees as a share of audit fees, weakest first
SELECT g.cik, g.filing_date, g.audit_fees, g.non_audit_fee_ratio,
       g.directors_listed, g.related_party_none_stated
FROM marts.governance_metrics g
WHERE g.non_audit_fee_ratio IS NOT NULL AND g.audit_fees > 0
ORDER BY g.non_audit_fee_ratio DESC
LIMIT 25;
```

---

## 12. Regenerating this

Row counts move as backfills land. These run against the warehouse without anything
landing locally:

```bash
gh workflow run explore.yml -f module=warehouse.catalogue        # full object inventory
gh workflow run explore.yml -f module=warehouse.coverage_report  # reachable/labelled/modelled
gh workflow run explore.yml -f module=warehouse.columns          # column lists
gh workflow run explore.yml -f module=warehouse.modelled_tags    # tag → mart registry
```

Repository: `github.com/Bhavya2502/credit-workbench` (public).
Every mart has an invariant suite in `src/credit_workbench/transform/verify_*.py` that
fails its build rather than publishing a bad table — extend the suite whenever you extend
a mart.
