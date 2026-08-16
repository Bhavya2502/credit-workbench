# Session state — 15 August 2026

Where the project stands, what was just done, and what to pick up. Read this first, then
`DATA_GUIDE.md` if you need to query, `decisions.md` for why things are the way they are.

---

## Read these in this order

| Document | For |
|---|---|
| `docs/SESSION_STATE.md` | this file — current state and next steps |
| `docs/DATA_GUIDE.md` | querying the warehouse; verified column names and working SQL |
| `docs/decisions.md`, `docs/architecture.md` | why the platform is shaped this way |
| `docs/runbook.md` | job schedule — **partly stale**, see constraints below |
| `docs/secrets.md` | which secrets exist and where they come from |

Two published companions: the **Credit Workbench Catalogue** (every table, grain,
pitfalls) and the **Credit Workbench Handoff** (architecture, decisions, defect log,
what remains). Ask the owner for the artifact links.

---

## Just completed

**Segments and concentration gained vintage flags.** They were the only fact marts
without `is_latest`, so a naive sum counted every restatement — Intel's FY2024 Client
Computing revenue came out at $338bn against a reported $30.29bn, an elevenfold
overstatement. Both are now materialised tables carrying `is_latest`,
`is_first_report` and `filings_reporting`; `marts.segments_all_vintages` and
`marts.concentration_all_vintages` remain for anyone who wants every vintage.

- `marts.segments` — 13,162,664 rows, 7,742,116 latest (59%)
- `marts.concentration` — 1,157,593 rows, 701,161 latest (61%)
- `marts.segment_concentration` — 39,390 rows, rebuilt on one vintage

**Anything computed from segments before 15 August was likely ~40% overstated.**

**The industry bridge (tracker B4) is built.**

- `ref.sic_hierarchy` — 449 SIC codes with three-digit group, major group and division.
  Descriptions come from SEC's own labels already in our filing data.
- `ref.industry_group` — 410 codes mapped to **140 peer groups**, rolled up only as far
  as needed to reach 30 comparable companies. `peers` records what each comparison
  actually rests on.

Why 140: four-digit SIC is too thin (263 of 400 codes have fewer than ten companies,
median six) and the 70 two-digit major groups are too blunt. 140 sits in the gap.

---

## Pick up here

### 1. The house industry scheme — needs input from the owner
`ref.industry_group.custom_industry` is deliberately `NULL`. The other session works to a
~90-industry scheme; that is a business decision and was not invented. **Ask for the
definition, then it is a data load into that column** and everything downstream follows.
The 140 data-derived groups work as a default meanwhile.

### 2. `ref.sic_naics` is empty
Needs the Census SIC→NAICS concordance, a separate fetch. Only worth doing if something
actually requires NAICS — the SIC hierarchy above may be enough for cyclicality grouping.

### 3. Gaps the other session identified, all confirmed genuine
| Gap | Size | Note |
|---|---|---|
| Proxy / DEF 14A (G3) | large | ~100k filings. Reuse `ingest/filing_sections.py` — same shape as the 10-K extractor |
| Macro / FRED (I2) | small | Free API key; a few series for GDP-β |
| Agency adjustments (D2–D4) | medium | Owner parked these. All 106 input columns exist in `marts.adjustment_inputs`; only the arithmetic is missing |
| Feature store (L1) | medium | The bridge to any scoring work; forces point-in-time discipline into one place |

---

## Constraints that changed this session

- **MotherDuck enforces a daily compute limit** on the free plan, and it was hit on
  15 August. Heavy scans over `marts.facts_dimensioned` (222m) or `ref.tag_note_map`
  (279m) consume it fast. Filter by `cik`, `fy` or `period_year` before aggregating. If
  the other session is running exploratory queries, that competes with any rebuild.
- **GitHub Actions is free and unlimited** — the repository was made public on
  15 August after verifying the history held no credential values. This was the binding
  constraint twice before; it no longer is.
- **The two weekly crons are paused** (`ingest_entities`, `ingest_sec_bulk`). Uncomment
  two lines in each workflow to restore. `runbook.md` predates this.

---

## Working practices that earned their place

**Probe before building.** Every material defect in this project was found by inspecting
real data first; the ones that got through were where that step was skipped. Diagnostics
go in `warehouse/diag_*.py` and run through the `explore` workflow:

```bash
gh workflow run explore.yml -f module=warehouse.diag_something
```

**Extend the invariant suite in the same commit as the mart.** Nine suites live in
`transform/verify_*.py` and fail the build rather than publishing a bad table. A check
written after the fact is a check written to pass. The one defect that reached production
— 428 duplicate rows from a trial run — got there because no check covered it yet.

**Text and XBRL both fail quietly.** A wrong covenant level looks exactly like a right
one. Nothing here can be validated by looking at the output, only by testing a property
the value must satisfy.

**Watch for join fan-out.** Three separate occurrences so far: `(tag, version)` on the
tag dictionary, multi-vintage facts, and exhibit number where the file name was the real
key. Each inflated totals while every individual row looked correct.

---

## Housekeeping

- Repo: `github.com/Bhavya2502/credit-workbench` (public). Local clone at
  `D:\Tools\companies_data\credit-workbench` is disposable — GitHub is the source of
  truth. No data lands locally by design.
- Tracker: `Credit_Workbench_Tracker.xlsx` v2.6, 39/83 done. **Not yet updated** for the
  segments fix or the industry bridge — worth doing early next session.
- Commit identity is fixed in the owner's global instructions; use it.
