# Runbook (tracker M5)

## Jobs

| Workflow | Trigger | What it does | Tracker |
|---|---|---|---|
| `smoke` | every push | proves the cloud toolchain works | A3 |
| `warehouse-init` | manual | creates database + schemas from `warehouse/schema.sql` | A1 |
| `ingest-companyfacts` | nightly 02:30 UTC | SEC companyfacts.zip → R2 raw zone | C1 |
| `ingest-entities` | weekly Sun 03:00 UTC | submissions.zip → entity master + filing index | B1 |
| `ingest-sec-bulk` | weekly Mon 04:00 UTC + manual | Financial Statement (and Notes) archives → R2 parquet | C2, C3 |
| `warehouse-build-views` | manual, after any ingest | rebuilds MotherDuck views + materialised tables | A1/A6 |
| `probe-sec` | manual | discovery helper: reports SEC URL patterns and archive layouts | — |

Backfills are year-batched and idempotent: an archive already in R2 is skipped, so an
interrupted run resumes simply by re-running it. `force: true` reloads anyway.

    gh workflow run ingest-sec-bulk -f dataset=fsn -f years=2015-2017

## Watch-outs

- **Actions minutes.** Private repos get 2,000 free minutes/month. Steady state is
  roughly 200 min/month (nightly + weekly jobs); full backfills are the expensive part
  and only run once.
- **SEC fair access.** Every request carries `SEC_USER_AGENT`; matrix jobs are capped at
  `max-parallel: 4` to stay well inside SEC's limits. Do not raise it casually.
- **Failures** email the repo owner. Logs are under the repo's **Actions** tab; forward
  the run URL to Claude and the failure gets diagnosed from the logs.

## Monthly review

Check freshness against the tracker workbook: newest `period` present in
`raw.fsn_sub` / `raw.fsds_sub`, row counts moving, R2 storage vs the cost line (M4).
