# Master Events Tracker for JG Offline

A permanent, multi-centre event planning and monitoring app for Judiciary Gold's offline centres
(Delhi, Lucknow, Prayagraj, Jabalpur, Bhopal, Indore, Jaipur – more can be added from the admin screens).

Centre Heads plan and submit each month's events; Head Office reviews, approves or returns them, and follows
execution on one consolidated calendar for any month.

**Stack:** Python 3.12+ · Streamlit · SQLAlchemy 2 · Alembic · PostgreSQL (Supabase/Neon) · bcrypt · pandas/openpyxl.

## Features

| Area | What you get |
|---|---|
| Centre Head portal | Upcoming / this month / next month, awaiting submission, awaiting approval, returned, approved, needs-outcome, completed & cancelled; next-month plan status, deadline and completeness |
| Category-specific forms | Batch launch · Bulk WhatsApp campaign (linkable to batch launches) · Offline seminar · Month-end scholarship test · PMI · Informal centre engagement (custom titles) · All India Open Mock (national parent + per-centre records). New categories and fields are configured in the app – no code |
| Monthly planning | One plan per centre-month, completeness indicator (mandatory categories, incomplete entries), configurable deadlines, submit / duplicate protection / resubmit, HO plan-level approve or return |
| Approvals | Queue by centre, per-event approve/return/reject with comments, bulk approve, re-approval after material changes |
| Master calendar | Month grid with all centres, day agenda, chronological list, filters, Today/◀/▶ across years, status styling, clash flags, quick-view with HO actions |
| Execution | In progress / completed, actual participation, post-event remarks, per-category outcome fields, preparation checklists, reference links, overdue flags |
| HO dashboard | Filterable metrics (every tile drills into the records), events by category/centre, planning status by centre, next 7 days, completion rate |
| Reports | 9 reports to Excel/CSV, printable monthly overview (HTML → print/PDF); all exports respect user access and filters |
| Admin | Centres, users & roles, requirements per category/centre, deadlines, recurrence rules, category editor, audit log |
| **v2 – Pre-event readiness** | Playbooks for Batch Launch, Scholarship Test and All India Open Mock generate dated, owned tasks per event; lead-time enforcement; proof-based ticking; readiness status; Readiness board; audit flags; task calendar; playbook versioning. See [docs/V2_READINESS.md](docs/V2_READINESS.md) |
| Security | bcrypt passwords, lockout, forced temporary-password change, server-side authorization on every action, optimistic locking against lost updates, full audit trail |

## Quick start (local, 2 minutes)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
export APP_ENV=development          # local SQLite file; use DATABASE_URL for Postgres
python manage.py seed-demo          # prints demo logins (admin, ho.admin, viewer, ch.bpl, …)
streamlit run app.py
```

Demo passwords are printed by `seed-demo`. **Never run `seed-demo` against production.**

## Tests

```bash
pytest -q                                                   # SQLite
TEST_DATABASE_URL=postgresql+psycopg://user:pw@host/throwaway_db pytest -q   # PostgreSQL (wipes that DB)
```

The suite covers permissions (including id-tampering), the full approval workflow, planning/duplicate
submission, recurrence, national events, conflict rules, optimistic locking, persistence across restarts,
migration drift, every page rendering for every role, and a UI-driven create → submit → approve flow.

## Project layout

```
app.py                      Streamlit entry: auth gate, role-based navigation
manage.py                   CLI: migrate, create-superadmin, seed-demo, status
jgevents/                   Business logic (no Streamlit imports)
  config.py                 secrets/env loading
  db/                       models, session, migrations runner, reference seed
  domain/                   roles, lifecycle & transitions, category field schemas + validation
  services/                 permissions, auth, directory, events, planning, recurrence, dashboard, reports, audit
  utils/dates.py            IST helpers, DD-MM-YYYY formatting, month maths
ui/                         Streamlit layer
  core.py                   DB handle, current user, error handling, navigation helpers
  components/               filters, tables, event forms, calendar views
  views/                    pages (dashboards, planning, calendar, event, approvals, reports, admin, auth)
migrations/                 Alembic environment + versions
tests/                      pytest suite + Streamlit AppTest harness
docs/                       ARCHITECTURE · DEPLOYMENT · CATEGORIES · LAUNCH_CHECKLIST
.github/workflows/ci.yml    lint + tests on SQLite and Postgres 16
```

## Deploying

See **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** – GitHub, Supabase, Streamlit Cloud secrets, first Super Admin,
migrations, backups/restore and maintenance. Before go-live, work through
**[docs/LAUNCH_CHECKLIST.md](docs/LAUNCH_CHECKLIST.md)**.

## Known limitations (v1)

* Refreshing the browser tab signs the user out (Streamlit keeps sessions per tab). Sessions otherwise last 10 hours.
* WhatsApp campaigns are tracked manually – the app does not send messages.
* Attachments are links (Drive/SharePoint), not uploaded files.
* No email/WhatsApp notifications yet; users see their queues on sign-in.
* On phones the month grid stacks vertically – use the List or Day agenda views.
