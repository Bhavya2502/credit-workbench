# Credit Workbench

Cloud-only data platform behind the corporate credit analysis prototype: financial spreads,
rating-agency-style adjustments, peer benchmarks, qualitative flags, early-warning events,
and a scoring model — built from authoritative structured sources (SEC XBRL first).

**Owner:** Bhavya (Peaks2Tails) · **Operator:** Claude, session by session.

## Operating principle — zero local footprint

Nothing is downloaded or stored on the owner's machine.

- **Raw data** lands in Cloudflare **R2** object storage (`credit-workbench-raw` bucket)
- **Processing** runs on **GitHub Actions** cloud runners (scheduled or manually triggered)
- **Serving** is from **MotherDuck** (cloud DuckDB warehouse)
- The owner's laptop only *orchestrates*: it edits this repo and triggers cloud jobs

## Layout

```
src/credit_workbench/
  common/config.py       environment-based settings (secrets come from GitHub Actions)
  ingest/                one module per source (SEC, GLEIF, FRED, ...)
  smoke.py               environment self-test used by CI
warehouse/schema.sql     warehouse schemas + table definitions (design: docs/architecture.md)
docs/                    architecture, secrets inventory, decisions, runbook
.github/workflows/       the cloud jobs (CI + ingest pipelines)
```

## Status

Build progress is tracked in `Credit_Workbench_Tracker.xlsx` (held by the owner; web mirror
on claude.ai). Current stage: Phase 1 — free US core (SEC EDGAR backbone).
