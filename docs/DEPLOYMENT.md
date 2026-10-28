# Deployment, operations and maintenance

Target setup: **GitHub (private repo) → Streamlit Community Cloud (app) → Supabase PostgreSQL (data)**.
Any host that runs `streamlit run app.py` with a `DATABASE_URL` works the same way (Render, Railway,
a company VM, Docker).

> **Do not run the production database on a free tier.** Supabase Free projects have no automatic backups and
> pause after 7 days without activity (e.g. a quiet holiday week). Use **Supabase Pro** (daily backups, 7-day
> retention) or an equivalent paid managed Postgres. This is the one component where saving ~$25/month
> risks losing every plan and approval record.

## 1. GitHub

```bash
cd master-events-tracker
git init -b main            # already initialised if you received the repo with history
git add -A && git commit -m "Initial import"
# create an EMPTY private repo on github.com (no README), then:
git remote add origin https://github.com/<org-or-user>/master-events-tracker.git
git push -u origin main
```

`.gitignore` already excludes `.streamlit/secrets.toml`, local `.db` files and caches.
The GitHub Actions workflow (`.github/workflows/ci.yml`) runs lint + the full test suite on SQLite **and**
PostgreSQL 16 on every push and pull request. Protect `main` so merges require CI to pass.

## 2. Database (Supabase)

1. Create a project in the **Mumbai (ap-south-1)** region; save the database password in your password manager.
2. *Project settings → Database → Connection string* → choose **Session pooler** (port **5432**). The direct
   connection string is IPv6; Streamlit Cloud needs the IPv4-friendly pooler. Avoid the transaction
   pooler (6543): it does not keep session state between statements, which conflicts with the server-side
   prepared statements the Postgres driver uses.
3. Append `?sslmode=require`. The app accepts `postgres://`, `postgresql://` or `postgresql+psycopg://`.

You do **not** run SQL by hand: the app applies Alembic migrations and seeds the 7 centres, 7 categories,
default requirements and the scholarship-test rule on first start. To do it ahead of time from a laptop:

```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql://postgres.<ref>:<password>@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?sslmode=require"
python manage.py migrate
python manage.py create-superadmin     # interactive; creates the first Super Admin
python manage.py status
```

## 3. Streamlit Community Cloud

1. share.streamlit.io → **Create app** → pick the repo, branch `main`, main file `app.py`.
2. *Advanced settings* → Python **3.12** (CI runs 3.12; development testing used 3.13).
3. *Secrets* – paste (see `.streamlit/secrets.toml.example`):
   ```toml
   DATABASE_URL = "postgresql://postgres.<ref>:<password>@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?sslmode=require"
   BOOTSTRAP_TOKEN = "<long random string>"   # only if you did not run create-superadmin
   ```
4. Deploy. On an empty database the sign-in page offers **Create the first Super Admin** (requires the
   bootstrap token). Afterwards **delete `BOOTSTRAP_TOKEN`** from the secrets.
5. Sharing: set the app to private and invite users if your workspace allows it; the app has its own login
   either way, so a public URL exposes only the sign-in page.

Notes on Community Cloud: apps sleep after 12 hours without traffic (first visit then takes ~30–60 s to wake);
resources are modest. For a guaranteed always-on internal tool, run the same repo on Render/Railway/a VM
(command: `streamlit run app.py --server.port $PORT --server.address 0.0.0.0`).

## 4. Roles and first-day configuration

Sign in as Super Admin →
1. **Users & access** → create Head Office admins, one Centre Head per centre (assign the centre), viewers.
   A temporary password is shown once; users must change it at first sign-in.
2. **Rules & categories** → confirm requirements (mandatory/optional per category and centre), the planning
   deadline day, the scholarship-test recurrence and the batch-launch programme list.
3. **Centres** → confirm names/codes; add Patna/Raipur/Chandigarh only when they actually go live.

## 5. Migrations (schema changes)

1. Change `jgevents/db/models.py`.
2. `alembic revision --autogenerate -m "describe change"` against a dev Postgres; review the generated file.
3. `pytest` – `test_migrations_match_models` fails if a model change has no migration.
4. Merge; on the next start the app upgrades the database automatically (serialised by an advisory lock).
   Prefer additive changes (new nullable columns) so the old and new versions can briefly coexist.

## 6. Backups and recovery

| Layer | Recommendation |
|---|---|
| Automatic | Supabase Pro daily backups (7-day retention). Add Point-in-Time Recovery if losing up to a day of edits is unacceptable. |
| Independent copy | Weekly `pg_dump` to company storage: `pg_dump "$DATABASE_URL" -Fc -f jg_events_$(date +%F).dump` (keep 12 weeks). |
| Business-level | Month-end Excel export (Reports → Monthly event calendar + Event history) filed with the MIS pack. |
| Restore drill | Once a quarter restore the latest dump into a scratch database: `pg_restore -d "$SCRATCH_URL" --clean jg_events_YYYY-MM-DD.dump`, point a local copy of the app at it and spot-check a month. |

Nothing is ever hard-deleted by the app (cancelled/rejected events, deactivated centres and users stay), so
most "we lost X" incidents are recoverable from the audit log without a restore.

## 7. Routine maintenance

* Monthly: review the audit log for unusual activity; deactivate leavers (never delete).
* Quarterly: restore drill; review dependency updates (`pip list --outdated`), bump pins in a branch,
  let CI run, then merge.
* Streamlit upgrades: the UI uses only core Streamlit widgets (no custom components), so upgrades are low-risk;
  still run `pytest` (includes AppTest UI smoke tests) before bumping the pin.

## 8. Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
export APP_ENV=development              # uses ./jg_events_dev.db (SQLite) when DATABASE_URL is unset
python manage.py seed-demo              # demo users/events – NEVER on production
streamlit run app.py
pytest -q                               # SQLite; set TEST_DATABASE_URL to a throwaway Postgres DB to test PG
```
