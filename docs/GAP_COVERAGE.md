# Coverage report — all 24 gaps in DATA_GAPS_FOR_WAREHOUSE.md

**Date:** 18 August 2026 · **Source:** measured from the warehouse, not from recollection
(`warehouse/diag_gap_coverage.py`) · **Verified:** 26 of 26 objects present

---

## Summary

| Status | Count | Gaps |
|---|---|---|
| **Built and verified** | 8 | G-02, G-03, G-04, G-05, G-08, G-09, G-18, G-19 |
| **Answered — already existed or needed only a reply** | 5 | G-12, G-13, G-17, G-21, G-22 |
| **Confirmed real, deliberately not built** | 2 | G-10, G-16 |
| **Blocked on the owner** | 3 | G-01, G-06, G-20 |
| **Not requested / no free source** | 3 | G-11, G-14, G-15 |
| **Outstanding** | 1 | G-07 |
| **Documented defects, retained** | 1 | G-23 |
| **Mitigated as a side-effect** | 1 | G-24 |

Plus one deliverable **not on the list**: the controls pillar, `marts.control_signals`.

---

## Built and verified

Every mart below ships an invariant suite that fails the build rather than publishing a bad
table.

| Gap | Object | Rows | Checks |
|---|---|---|---|
| **G-02** | `marts.ratio_coverage` | 1,614,345 | 15/15 |
| **G-03** | `marts.outcome_counts` | 21,257 | 15/15 |
| **G-04** | `marts.adjusted_metrics` | 726,845 | 12/12 |
| **G-04** | `ref.adjustment_policy` | 5 | — |
| **G-05** | `marts.lease_adjustment` | 204,694 | (within G-04) |
| **G-08** | `marts.disclosed_kpis` | 1,449 | 10/10 |
| **G-08** | `ref.kpi_dictionary` | 18 | — |
| **G-08** | `staging.kpi_lines` | 19,968 | — |
| **G-09** | `marts.risk_themes` | 414,570 | 10/10 |
| **G-09** | `marts.risk_theme_prevalence` | 9,340 | — |
| **G-09** | `ref.risk_theme_dictionary` | 20 | — |
| **G-09** | `staging.risk_headings` | 1,326,167 | — |
| **G-18** | `marts.cohorts` | 10 | 11/11 |
| **G-18** | `marts.cohort_members` | 76,198 | — |
| **G-18** | `ref.cohort_definition` | 10 | — |
| **G-19** | `ref.company_names` | 1,052,192 | 9/9 |
| **G-19** | `ref.name_collisions` | 2,219 | — |
| **G-19** | `ref.company_filing_span` | 978,466 | — |

### Where the delivery differs from the request, and why

**G-02** was built at **two** industry grains, not the one asked for. At
`ratio × sic2 × size_band`, FY2024 yields 12,127 cells of which only 1,409 hold 30+ companies
and 7,663 hold fewer than 10 — median 6. Percentiles over six companies are noise wearing a
precise number. Both `sic2` and the 140 `peer_group` codes are published with
`is_sufficient`, and the trade-off is stated: SIC2 buys cell size, peer groups buy
comparability.

**G-04**'s policy is data, not a constant. Five named policies in `ref.adjustment_policy`;
`reported` included so the cost of each adjustment is visible, `lease_6x` beside `lease_8x`
so threshold sensitivity to the multiple can be measured.

**G-05** turned out to be a rule rather than a fudge. The reported lease liability appears in
17 filings for FY2017, 115 for FY2018, then **11,522 for FY2019**; only 1,203 of 452,942 rows
carry both eras. Use the reported liability where it exists, otherwise capitalise the rent.

**G-09** is classified on risk-factor **headings**, not body text. Body-text classification
marks 95.9% of filers with regulation and 89.8% with cyber — a table of ninety-per-cent
figures that separates nobody. At heading grain the average theme sits at 34.7%.

**G-18** cohorts deliberately retain companies that no longer exist: **44.6% of members
stopped filing before 2025**, 506 have a bankruptcy outcome. A cohort built from live
constituents drops exactly the companies a default rate is calibrated against.

---

## Answered — already existed, or needed only a reply

**Four gaps were filed against things that already existed**, all present in the generated
`data_dictionary.md` and absent from `DATA_GUIDE.md`. That is our documentation debt.

| Gap | Claimed | Reality |
|---|---|---|
| **G-17** | no grouping above SIC2 | `ref.industry_group` — 410 SIC codes → 140 peer groups |
| **G-22** | `marts.concentration` lacks vintage flags | 1,157,593 rows, flags present since 15 Aug |
| **G-19** (part) | no `former_names` | `ref.former_names` — 72,598 rows with dates |
| **G-12** | — | Confirmation requested and given: CRE stays analyst-input |
| **G-13** | India schemas unknown | Catalogued — see below |
| **G-21** | `cik` type inconsistent | `governance_metrics` aligned to BIGINT; 44 BIGINT / 18 VARCHAR remains |

### G-13 · what the catalogue found

`gold` / `silver` / `catalog` are **not schemas in `credit_workbench`** — they are a separate
MotherDuck database, **`credit_data`**, reachable by the same token.

| Table | Rows | Coverage |
|---|---|---|
| `credit_data.gold.india_corporate_lgd_panel` | 912 | Apr 2018 – Oct 2025 |
| `credit_data.gold.india_retail_pd_panel` | 233,154 | **disbursals Aug–Oct 2018 only** |
| `credit_data.silver.ibbi_cirp_cases` | 1,162 | CIRP cases |
| `credit_data.silver.ibbi_liquidation_cases` | 560 | liquidations |
| `credit_data.silver.ibbi_liquidation_waterfall` | 187 | distribution waterfall |
| `credit_data.silver.ibbi_voluntary_liquidations` | 299 | voluntary liquidations |

Three caveats: the retail PD panel is a **three-month cross-section**, not a panel through
time; the IBBI date columns are **text in mixed `DD-MM-YYYY` / `DD-MM-YY` formats** so
reported ranges are lexical, not chronological; and the IBBI tables are **extracted from
published documents** and carry their own QA flags (`structural_ok`).

---

## Confirmed real, deliberately not built

**G-10 · Bank regulatory data.** Our first answer called this a new ingest on the scale of the
SEC bulk load. **That was wrong.** `credit_data.silver.fdic_bank_financials` holds
**1,093,173 rows, 1992-12-31 → 2026-03-31**, at institution-quarter grain, 63 columns keyed on
`CERT`. Two aggregates sit above it. The requester's hypothesis — cataloguing and exposing is
most of the work — holds. **The real task is the CIK↔CERT crosswalk**, which exists in neither
database and cannot be done on name given the 2,219 collisions under G-19.

**G-16 · SIC→NAICS.** `ref.sic_naics` confirmed empty (0 rows). We argue against building it:
`ref.industry_group` now serves the cohorting NAICS was wanted for. Overrule if you disagree.

---

## Blocked on the owner

| Gap | Action needed |
|---|---|
| **G-01** | Grant a MotherDuck read-only share. **Highest-value item on the list** — four gaps were filed because this workstream could not query |
| **G-06** | Add `FRED_API_KEY` to Actions secrets; it is in `docs/secrets.md` but was never set |
| **G-20** | Decide on renaming `marts.ratios` / `spreads_a` / `spreads_q`. Not done unilaterally because a second workstream shares this account |

---

## Not requested, or no free source

**G-14** (non-US filers) and **G-15** (private/SME) are both marked *"noting rather than
requesting"* in the source document — constraints recorded, not work asked for.

**G-11** (insurance statutory) has no free bulk source; NAIC filings are not downloadable the
way FFIEC Call Reports are.

---

## Outstanding

**G-07 · Stated issuer ratings.** Deferred by the owner to last. Not built. Groundwork done:
in FY2024 MD&A alone, 541 sections mention Moody's, 324 S&P, 261 Fitch. The extraction needs
agency-anchored proximity with an agency-scale cross-check (`Ba1` beside Moody's is credible,
beside S&P is not) and must exclude "S&P 500 Index" references, which are a confirmed false
positive.

---

## G-23 · Defects kept visible, as asked

- `quali.note_signals.material_weakness` still discriminates inversely and is **retained,
  documented**. The probable cause is now known: it matches the phrase, and Item 9A carries
  the *definition* of a material weakness as boilerplate (8.4% of clean filers use the words).
- `marts.control_signals` publishes both the phrase flag and the conclusion polarity so the
  comparison stays measurable: **44.0% distress for an adverse conclusion against 39.5% for
  the phrase**. An invariant asserts the polarity keeps winning.
- `fee_units_overridden` marks the 0.8% of proxy filings whose own units note was wrong.
- Board independence and `adjusted_debt` are `NULL` where not credibly stated. **Do not
  coalesce either to zero.**

## G-24 · Compute budget

Mitigated as a side-effect: `ratio_coverage`, `outcome_counts`, `adjusted_metrics`,
`control_signals`, `risk_themes` and `disclosed_kpis` are all materialised, keeping consumers
off the 222m- and 279m-row tables. `staging.kpi_lines` and `staging.risk_headings` are
fingerprinted so the expensive narrative scans happen once and rebuild themselves only when
their extraction rule changes.
