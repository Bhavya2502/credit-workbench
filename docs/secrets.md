# Secrets inventory (tracker A5)

**Rule:** secrets (passwords, API keys, tokens) live in exactly one place — this repo's
**Settings → Secrets and variables → Actions** on github.com. Never in code, never in chat,
never in a file on the owner's laptop. The owner pastes each value into GitHub's form
personally; Claude never sees or handles the values.

| Secret name | What it is | Where the owner gets it | Needed for |
|---|---|---|---|
| `MOTHERDUCK_TOKEN` | Warehouse access token | MotherDuck app → Settings → Access Tokens | all warehouse jobs |
| `R2_ACCOUNT_ID` | Cloudflare account ID | Cloudflare dashboard → R2 (shown on overview) | ingest jobs |
| `R2_ACCESS_KEY_ID` | R2 API key (public half) | Cloudflare → R2 → Manage API Tokens → Create | ingest jobs |
| `R2_SECRET_ACCESS_KEY` | R2 API key (secret half) | shown once at key creation — paste immediately | ingest jobs |
| `SEC_USER_AGENT` | Contact string required by SEC fair-access policy | just text, e.g. `YourFirm credit-workbench you@example.com` (SEC requires a real contact address in the live value) | SEC downloads |
| `FRED_API_KEY` | Free API key for macro series (later, item I2) | fred.stlouisfed.org → My Account → API Keys | macro ingest |

How to add one: github.com → this repo → **Settings** tab → **Secrets and variables** →
**Actions** → **New repository secret** → enter the name exactly as above, paste the value,
**Add secret**. Values can be replaced any time; they can never be viewed again after saving,
which is the point.
