# Launch checklist – seven initial centres

## Week 0 – decisions only Head Office can make
- [ ] Confirm which categories are **mandatory** each month, network-wide and per centre (default: scholarship test only).
- [ ] Confirm the **planning deadline** (default: 25th of the previous month) and who approves (HO admins).
- [ ] Confirm the scholarship-test pattern (default: last Sunday 10:00–12:00) and the batch programme list.
- [ ] Name one **HO approver** and one **backup approver** so approvals never wait on leave.
- [ ] Decide the go-live month (recommend: plan the **next full month**, not the current one).

## Week 0 – technical
- [ ] Private GitHub repo created; CI green on `main`.
- [ ] Supabase **Pro** project (Mumbai) created; Session-pooler URL stored in Streamlit secrets.
- [ ] App deployed; first Super Admin created; `BOOTSTRAP_TOKEN` removed.
- [ ] Weekly `pg_dump` job scheduled and one restore tested.

## Week 1 – people
- [ ] HO admin accounts created (minimum two).
- [ ] Centre Head accounts – one per centre, assigned to the right centre:
      Delhi · Lucknow · Prayagraj · Jabalpur · Bhopal · Indore · Jaipur.
- [ ] Viewer accounts for leadership (scope: all centres).
- [ ] Temporary passwords shared privately (WhatsApp DM, not group); everyone signed in and changed password.
- [ ] 30-minute walkthrough per centre: plan → submit → read feedback → record outcome.

## Week 1–2 – pilot
- [ ] Two centres plan the next month end-to-end first (suggest one large, one small).
- [ ] HO reviews in the app only – no parallel Excel approvals.
- [ ] Collect friction notes; adjust required fields/labels in the category editor (no code needed).

## Week 2–3 – full rollout
- [ ] All seven centres submit by the deadline.
- [ ] Dashboard "Plans not submitted" = 0 on deadline day; chase from the dashboard drill-down.
- [ ] After each event the Centre Head records actual participation and closes it; "Overdue / at risk" reviewed weekly.

## Month-end routine
- [ ] Export *Monthly event calendar* and *Execution status* (Reports) for the MIS pack.
- [ ] Review *Approval backlog* ageing and *Planning completeness by centre*.
- [ ] Generate next month's scholarship-test proposals (Monthly planning → Recurring events).
