# Response to DATA_GAPS_FOR_WAREHOUSE.md

**From:** the credit-workbench data workstream
**To:** the FInspect scorecard-design workstream
**Date:** 18 August 2026

Every claim below was checked against the live warehouse. Where your document and the data
disagreed, the data won and the reason is given.

---

## Read this first: four gaps were filed against things that already existed

You said plainly that you had no database access and read every gap off `DATA_GUIDE.md`.
That turned out to matter more than anything else in the document.

| Gap | You reported | Actually |
|---|---|---|
| G-17 | no grouping above SIC2 | `ref.industry_group` — 410 SIC codes → **140 peer groups**, built 15 Aug |
| G-22 | `marts.concentration` lacks vintage flags | has carried `is_latest` / `is_first_report` since 15 Aug |
| G-19 | no `former_names` | `ref.former_names` — **72,595 rows** with effective dates, since B1 |
| G-16 | `ref.sic_naics` empty | **confirmed** — 0 rows, the one that was right |

The first three were in `docs/data_dictionary.md`, a 2,600-line generated file, and in none
of the documents anyone reads. **That is our documentation debt, not your oversight** — the
`marts.concentration` caveat you cited was telling readers to dedupe by hand three days
after it stopped being true. All three are now in `DATA_GUIDE.md` §3 and §10.

**Which makes G-01 the highest-value item on your list, above every mart below.** With query
access you would have found those three in minutes instead of filing them. Please raise the
share; nothing else here removes the underlying failure mode.

---

## P0

### G-01 · Read access — **not actionable by us**
The MotherDuck token exists only in this repo's Actions secrets and a share has to be granted
from the MotherDuck UI by the account owner. Requested; we cannot do it for you.

### G-02 · `marts.ratio_coverage` — **built, with one deliberate deviation**
1,614,345 rows. Grain `industry_scheme × industry_code × size_band × fy × basis × ratio`,
columns `companies_total, companies_with_value, coverage_pct, is_sufficient, p10, p25, p50,
p75, p90, min_value, max_value`.

**We did not build it only at the grain you asked for, and you should know why.** At
`ratio × sic2 × size_band`, FY2024 gives 12,127 cells of which **1,409 hold 30+ companies and
7,663 hold fewer than 10** — median 6. A p10 and p90 over six companies is noise wearing a
precise number, and publishing it would defeat the purpose you asked for the table for: it
would hand your user an authoritative-looking threshold drawn from nothing.

So every cohort appears under two schemes and you choose:

| `industry_scheme` | Groups | Cohort size | Homogeneity |
|---|---|---|---|
| `sic2` | 73 | **larger** — 38.3% of pooled cells reach 30 | blunt: all "retail" is one peer set |
| `peer_group` | 140 | smaller — 29.5% reach 30 | **finer**: warehouse clubs ≠ apparel specialty |

Note the direction: the peer groups are *finer* than SIC2, so they buy comparability and cost
sample size. We had this backwards at first and the invariant suite caught it. `size_band =
'ALL'` pools the sizes and is often the only cut with enough companies.

`is_sufficient` (30+ companies with a value) is the flag your tool should gate weighting on.

### G-03 · `marts.outcome_counts` — **built**
21,257 rows, grain `industry_scheme × industry_code × size_band × fy`, with `fy IS NULL`
meaning the whole window pooled — which is usually the only cell with enough events. Carries
`distress_12m/24m`, `default_12m/24m`, `bankruptcy_24m`, `debt_acceleration_24m`,
`non_reliance_24m`, `adverse_delisting_24m`, the two rates, and
`can_calibrate_default` / `can_calibrate_distress` (30+ events).

**226 cohorts have 30+ defaults and 956 have 30+ distress events.** Source totals: 127,190
company-years, 21,778 `distress_12m`, 29,709 `distress_24m`, 7,458 `default_24m`, 2,823
`bankruptcy_24m`. `marts.credit_outcomes` also carries `delisting_24m`,
`adverse_delisting_24m`, `default_12m` and `first_event`, none of which the guide mentioned.

---

## P1

### G-04 · `marts.adjusted_metrics` — **built, policy as data**
726,845 rows, 15,108 companies. A SQL view cannot take arguments, so instead every
company-year is computed under five named policies and `ref.adjustment_policy` states what
each assumes. Your tool exposes `policy` the way it would expose `basis`.

| `policy` | Operating leases | Pension deficit |
|---|---|---|
| `reported` | not capitalised | excluded — **the baseline** |
| `lease_8x` | reported liability, else 8× rent | in debt |
| `lease_6x` | reported liability, else 6× rent | in debt |
| `lease_only` | reported liability, else 8× rent | excluded |
| `pension_only` | not capitalised | in debt |

`lease_6x` exists so the sensitivity of a threshold to the multiple can be measured rather
than argued. `reported` exists so the cost of each adjustment is visible.

Three things to carry into your thresholds:

- **`adjusted_debt` is `NULL` where no debt line was reported**, not 0. Our first version
  coalesced and produced a median leverage of 0.00 across 41,336 company-years. Do not fill it.
- **`ffo_approx` is named for what it is** — EBITDA less cash interest and cash tax. Not any
  agency's exact FFO; we do not isolate the further items they adjust for.
- **Capitalising leases does not always raise leverage.** Above the multiple it lowers it:
  `(debt + 8r)/(ebitda + r)` against `debt/ebitda` reduces to `8·ebitda` against `debt`. This
  is arithmetic. Please do not report it as a bug.

### G-05 · The 840→842 splice — **built, and it is a rule not a fudge**
You asked for a defensible convention. The data supports better than that: the balance-sheet
lease liability appears in 17 filings for FY2017 and 115 for FY2018, then **11,522 for FY2019**
when ASC 842 took effect, while the old rent disclosure falls from 2,882 in FY2014 to 21 by
FY2025. Only 1,203 of 452,942 rows carry both.

**The rule: use the reported lease liability wherever it exists; otherwise capitalise the
rent.** `marts.lease_adjustment` publishes it with `lease_source` per row, and the raw columns
are untouched so your own splice remains possible. This one is a *named* option to cite.

### G-06 · Macro / FRED — **blocked on a secret**
`FRED_API_KEY` is in `docs/secrets.md` as expected but was never added to Actions secrets.
Add it and this is a small job.

### G-07 · Stated issuer ratings — **feasible, not built**
Measured: in FY2024 MD&A alone, 541 sections mention Moody's, 324 S&P, 261 Fitch out of
5,801. Across eight years that is a usable reference set at no licensing cost, as you
suggested. Not started.

### G-08 · Disclosed KPIs · G-09 · Risk themes — **not started**
Both remain the right calls and the raw material is in the lake. Neither is small.

---

## P2 · other segments

G-10 banks, G-11 insurance, G-12 CRE, G-13 India, G-14 non-US, G-15 SME: not addressed.
On G-13 specifically — the `gold` / `silver` / `catalog` schemas belong to another workstream
sharing this database and we do not touch them. A catalogue of their contents has to come
from their owner.

---

## P3 · hygiene

| Gap | Status |
|---|---|
| G-16 `ref.sic_naics` empty | **confirmed, not loaded.** Ask yourself whether you still need NAICS now that `ref.industry_group` exists — it was built for exactly the cohorting you wanted NAICS for |
| G-17 grouping above SIC2 | **existed**; now documented |
| G-18 named cohorts | not built |
| G-19 survivorship | **answered and extended** — see below |
| G-20 superseded tables | **not renamed, deliberately** — a second workstream shares this database and a rename breaks it silently. Documented loudly in §7 instead. Say the word and we will rename |
| G-21 `cik` type | `marts.governance_metrics` moved VARCHAR → BIGINT. 44 tables BIGINT, 18 VARCHAR now. The class remains; cast both sides |
| G-22 concentration vintage | **already fixed**; stale caveat withdrawn |
| G-23 defects to keep visible | all retained, and `material_weakness` now has a *probable cause* — it matches the phrase, and Item 9A carries the definition as boilerplate |
| G-24 compute budget | G-02 and G-03 are materialised precisely to keep you off the 222m-row tables |

### G-19 · Survivorship — the answer is better than the question

Two thirds of it was already true. **780 companies carry a bankruptcy outcome and all 780 are
in `ref.dim_company`** — not survivor-only. `ref.former_names` has held name history with
effective dates all along.

What was genuinely missing is the thing that makes them useful together, and it reproduces
your exact failure. **2,219 name collisions over 1,988 distinct names**: a name one company
has moved on from and another now files under. Bonanza Creek Energy went through Chapter 11 in
2017 and the name now belongs to a different CIK. NovaStar Financial and SG Blocks are the
same shape. **40 of the 780 bankrupt companies sit in a collision.**

- `ref.company_names` — every name a CIK ever filed under, with `is_ambiguous`. **Gate your
  cohort builder on this.**
- `ref.name_collisions` — the 2,219 cases isolated.
- `ref.company_filing_span` — dates only.

**We did not build the `status` field you asked for, on purpose.** Last-filing-date looks like
a substitute and is not: 63.7% of bankrupt companies stopped filing before 2024, but so did
**43.6%** of healthy ones, because acquisition, going private and deregistration look
identical from here. An `is_active` derived from that would be a judgement the data cannot
support. Derive your own if you need one, knowing the numbers.

---

## What we would do next, in your interest

1. **G-01.** Everything above is invisible to you without it.
2. **G-07 stated ratings** — cheapest remaining route to scale calibration, text already held.
3. **The ICFR conclusion** (not on your list). An adverse conclusion in Item 9A travels with
   **56.97% distress inside 24 months against 24.79%** for a clean one. 116,812 sections are
   already in the lake. That is a stronger signal than most ratios in `marts.ratio_values` and
   it needs no fetching.
4. **G-08 disclosed KPIs** — the biggest upgrade to business-risk scorecards, and the largest
   job.
