# Session state — 16 August 2026

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

**The proxy / governance gap (G3) is built and validated, but not yet backfilled.**
Code is on `main`; nothing has been fetched into the lake yet. Run it with:

```bash
gh workflow run ingest_proxy.yml -f parts=sections -f years=2019-2026
gh workflow run ingest_proxy.yml -f parts=register
gh workflow run ingest_proxy.yml -f parts=metrics
```

`sections_dry` first if anything in the splitter changed. The `metrics` job reads stored
text, so the extractor can be corrected and re-run without re-fetching from SEC.

What was found, in the order it mattered:

- **There is no governance XBRL to reuse.** `AuditorName`, `IcfrAuditorAttestationFlag`,
  `DocumentFinStmtErrorCorrectionFlag` and the Item 402(v) tags return *nothing* from
  `staging.facts_pit` — they are `dei` and `ecd` tags and we hold numeric
  financial-statement facts. The only auditor tags present are 926 IFRS
  `AuditorsRemuneration*` facts from foreign filers.
- **The 10-K does not help either.** Items 10–14 exist but run 432–1,910 characters at
  the median: they are the "incorporated by reference to our Proxy Statement" stubs.
- **208,038 DEF 14A are indexed, 165,078 with a primary document, 1994–2026. 6,809 of
  those filers also have financials** — the universe a scorecard can use.
  `--with-financials` restricts to it.
- **The 10-K splitter could not be reused.** Schedule 14A imposes no Item numbering, and
  no single proxy heading is universal — the most common, "audit committee report",
  appears in 28%. Sections are matched on *families* of phrasings instead, measured at
  75–92% per document, and a median of 13 of 20 sections is recovered per filing.
- **The converter was the real obstacle.** `to_text` puts every table cell on its own
  line, which separated each fee label from its number: 35% of filings had them on one
  line. `common.html_text.to_rows` keeps rows intact and takes that to 80%, at no cost in
  size. Everything numeric in a proxy is a table, so this was the whole problem.

**Two things are deliberately conservative, and should stay that way.**

Board independence is **not** extracted from prose. The clean phrasing appears in 2% of
proxies; a looser pattern fired on 75% and was matching sentences like "acting as a
liaison between the independent directors" — a count with no count in it. It comes from
the director table (38% of filings) and is `NULL` otherwise, with the sentence kept
alongside as evidence. **Do not coalesce that `NULL` to zero.**

The fee reader was wrong on 16 of 40 filings before it was region-scoped — reporting a
total of 11 against a table stating 2,017, and once lifting 4,011,243 from the Rule 0-11
cover-page filing fee. Requiring two of the four Item 9(e) categories inside one
contiguous block fixed it: 96% of filings with a stated total now tie to it. See
`docs/DATA_GUIDE.md` §10 for the full coverage table — **read it before scoring.**

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

### 3. Gaps the other session identified
| Gap | Size | Note |
|---|---|---|
| Proxy / DEF 14A (G3) | **built, needs backfill** | Code on `main`, validated on 60 live filings. Run `ingest_proxy.yml` as above |
| Macro / FRED (I2) | small | Free API key; a few series for GDP-β |
| Agency adjustments (D2–D4) | medium | Owner parked these. All 106 input columns exist in `marts.adjustment_inputs`; only the arithmetic is missing |
| Feature store (L1) | medium | The bridge to any scoring work; forces point-in-time discipline into one place |

### 4. Governance work worth doing next, in value order

- **The controls pillar — probed, and the signal is strong. Build this first.**
  `warehouse/diag_icfr.py` has already answered the design questions against the 116,812
  Item 9A sections in the lake, so this needs no fetching and no further exploration:

  | Finding | Number |
  |---|---|
  | Adverse ICFR conclusion → distress within 24m | **56.97%** |
  | Clean conclusion → distress within 24m | 24.79% |
  | Adverse rate, by filing year | 11.3–15.1% |
  | Conclusion extracted | 23,380 of 36,165 sections |
  | No conclusion found — the open gap | **12,929 (36%)** |

  A 2.3× distress lift is a stronger signal than most ratios in `marts.ratio_values`.
  Both polarities were read back sentence by sentence and are accurate.

  Two things to carry over. **Do not use "material weakness" as a finding** — 37.7% of
  sections mention it, because Item 9A carries the definition as boilerplate; use the
  polarity of the conclusion sentence, which is what was validated. And the 36% recall
  gap is probably word order: "maintained effective internal control over financial
  reporting" puts "effective" before the subject, where the patterns in `diag_icfr.py`
  expect it after. **That is a hypothesis, not a measured fact** — test it before
  building on it.

  **This probably explains the `quali.note_signals.material_weakness` failure.** That
  signal is documented in `DATA_GUIDE.md` §7 as discriminating inversely — flagged
  companies defaulted *less* — and it is built the phrase way, matching
  `material weakness … identified|in internal control` with negation guards
  (`transform/note_corpus.py`). The phrase catches the definition and the
  remediation-of-a-fixed-weakness discussion, and thorough filers write longer controls
  discussions than terse small ones, which is a mechanism for inverting the sign. The
  polarity of the conclusion sentence does not have that problem, and pointed the right
  way at 2.3×.

  **Do not read this as a like-for-like win.** The old signal was measured on
  `default_24m` over XBRL note text; the new one on `distress_24m` over Item 9A. Two
  different labels and two different sources. The claim that is supported is that
  polarity works where the phrase did not — not that one is a drop-in replacement for
  the other. Anyone building this should re-measure against `default_24m` as well, and
  consider whether `note_signals.material_weakness` should then be retired or rebuilt.

  The adverse rate of 11–15% is higher than the ~5% quoted for accelerated filers and is
  not an error: this population is all SEC filers, where management-only assessments by
  smaller reporting companies fail far more often. The distress lift corroborates it.
- **The last third of fee tables.** The 33% not read are filers who describe fees in
  prose ("Audit Fees. The aggregate fees billed by…") or transpose the table so the
  categories are column headers. Two separate small parsers, each verifiable against the
  same tie-to-stated-total property.
- **Director tables at 38%.** The remainder present directors as biography blocks with the
  name as a heading rather than as a table. That is a different extractor, not a tweak.
- **Say-on-pay support levels** come from 8-K item 5.07, not the proxy — a real governance
  signal and `marts.credit_events` already ingests 8-Ks.

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
