# Version 2 – Pre-event readiness playbooks

Specification signed off on 29-10-2026. Every Batch Launch, Month-End Scholarship Test and All India Open Mock
gets dated, owned preparation tasks from its playbook when it is created.

## Approved playbooks (calendar days; T-N = N days before the event)

**Batch Launch** – minimum lead time 10 days

| Key | Task | Due | Owner | Critical | Needs | Proof |
|---|---|---|---|---|---|---|
| BL1 | Batch details final: subject, faculty, timing, fee | T-10 | Centre Head | Yes | – | – |
| BL2 | Admission link / landing page live and checked | T-7 | Marketing | Yes | BL1 | URL |
| BL3 | Counselling calls start | T-6 | Counselling | Yes | BL2 | Calls made |
| BL4 | Bulk WhatsApp sent | T-2 | Centre Head | Yes | BL2 | Auto: linked WhatsApp campaign completed |
| BL5 | Follow-up calls to no-shows | T+2 | Counselling | No | – | Calls made |

**Month-End Scholarship Test** – minimum lead time 5 days

| Key | Task | Due | Owner | Critical | Needs | Proof |
|---|---|---|---|---|---|---|
| ST1 | Cold calling starts | T-5 | Counselling | Yes | – | Calls made |
| ST2 | Question paper received at centre | T-2 | Centre Head | Yes | – | – |
| ST3 | Centre ready: seats and papers for every registered student | T-1 | Centre Head | Yes | ST2 | Seats ≥ registrations |
| ST4 | Scholarship list recorded | T+1 | Centre Head | Yes | – | Scholarships awarded |

**All India Open Mock** – minimum lead time 14 days (on the national event)

| Key | Task | Due | Owner | Critical | Needs | Proof |
|---|---|---|---|---|---|---|
| H1 | Papers dispatched / online test link ready (Head Office) | T-3 | HO Academic | Yes | – | – |
| H2 | All-India results published (Head Office) | T+7 | HO Academic | Yes | – | URL |
| C1 | Registration record set up (LeadSquared page or sheet) | T-14 | Centre Head | Yes | – | URL |
| C2 | Cold calling starts | T-10 | Counselling | Yes | C1 | Calls made |
| C3 | Bulk WhatsApp sent | T-3 | Centre Head | Yes | C1 | Auto: linked WhatsApp campaign completed |
| C4 | Centre ready: seats ≥ registrations (skipped for online) | T-1 | Centre Head | Yes | H1 | Seats ≥ registrations |
| C5 | Follow-up calls to participants | T+3 | Counselling | No | – | Calls made |

Playbooks are edited in **Administration → Playbooks**. Saving creates a new version; existing events keep the
version they were built from. Head Office can add newer tasks to an event with *Apply playbook vN* on its Readiness tab.

## Rules

* **Booking late:** creating or moving an event inside its minimum lead time is refused for Centre Heads. Head
  Office can do it with a reason; the event is marked *Booked late*.
* **Ticking:** Centre Heads tick their centre's tasks after checking with the team; Head Office ticks H-tasks and
  may tick on a centre's behalf. Proof is required where listed. A task can't be ticked while a task it depends
  on is open.
* **Done late** is recorded automatically when a task is ticked after its due date.
* **Waive:** Head Office only, with a reason. Shown as Waived, never as Done.
* **Reopen:** Centre Head until the event date; Head Office any time. Reason required; history kept in the audit log.
* **Audit flags:** Head Office flags a task with a comment; the Centre Head sees it (home banner, task page) and
  can reply; Head Office resolves it. Flags appear on the Readiness board and in reports.
* **Readiness:** Ready (all critical pre-event tasks done or waived) · On track · At risk (a critical task due within
  2 days) · Off track (a critical task overdue) · Blocked by Head Office (a centre task waits on an overdue HO task).
* **Completion:** an event can be marked Completed only when its critical post-event tasks (ST4, H2) are finished.
* **Date change:** open tasks are recalculated; finished tasks keep their dates and completion. The app shows old
  vs new deadlines before saving. **Cancel/reject** closes open tasks (reopened if Head Office reopens the event).
* **Alerts:** in-app only (no email/WhatsApp).
* Events created before v2 get no tasks unless Head Office adds them from the event's Readiness tab.
* The old free-text checklist is switched off for the three playbook categories.

## Database changes (migration `0002`)

New tables `playbooks`, `playbook_tasks`, `event_tasks` (unique per event + key), `task_flags`. New columns on
`events`: `booked_late`, `late_reason`, `late_approved_by_id`. Data updates: WhatsApp campaigns may link to Batch
Launches and Open Mocks; default checklists cleared for the three playbook categories. Playbook v1 rows are seeded
on start-up. The migration runs automatically when the app starts; it is reversible (`alembic downgrade 0001`).

## Where things are

| Who | Page | What |
|---|---|---|
| Centre Head | My centre | Banner for overdue / due-tomorrow tasks and unanswered flags; *Preparation tasks* tab |
| Everyone | Preparation tasks | Filterable task list, Excel export, open audit flags |
| Everyone | Master calendar | Tasks on their due dates (Show: Events / Tasks / Both; Overdue / Open / Done / Critical) |
| Everyone | Event → Readiness tab | Status, reasons, tasks, mark done / reopen / waive / flag |
| Head Office, viewers | Readiness board | Metrics that open their records, critical delays, events worst first, centre comparison |
| Head Office | Administration → Playbooks | Edit tasks, offsets, owners, dependencies, proof; version history |
| Head Office | Reports → Pre-event readiness | By centre (on-time rate, avg days late), events (readiness vs turnout), tasks |
