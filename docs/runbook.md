# Runbook (tracker M5 — grows as pipelines land)

| Job (workflow) | Trigger | What it does | Prerequisites |
|---|---|---|---|
| `smoke` | every push + manual | proves the cloud toolchain works | none |
| `ingest-companyfacts` | manual (nightly cron once proven) | SEC companyfacts.zip → R2 raw zone | R2 + SEC secrets (docs/secrets.md) |

**Failures:** GitHub emails the repo owner automatically when a workflow fails.
Forward the email/screenshot to Claude in a session; logs live under the repo's
**Actions** tab. **Monthly:** freshness review against the tracker (M5).
