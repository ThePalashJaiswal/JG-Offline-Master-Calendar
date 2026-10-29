"""Exports. Every report is built from the same scoped query the user sees, so
exports can never include centres or events outside the user's access."""
from __future__ import annotations

import html
import io
from collections import defaultdict

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from jgevents.db.models import ApprovalAction, Event, User
from jgevents.domain.constants import (
    APPROVED_LIKE,
    COLOUR_HEX,
    STATUS_ICONS,
    STATUS_LABELS,
    Status,
    approval_status,
)
from jgevents.domain.schema import display_detail
from jgevents.services import events as ev_svc
from jgevents.services import planning
from jgevents.services.events import EventFilter
from jgevents.utils.dates import (
    fmt_date,
    fmt_dt,
    fmt_time,
    month_bounds,
    month_grid,
    month_label,
    now_utc,
    to_ist,
    today_ist,
)

REPORTS = {
    "calendar": "Monthly event calendar",
    "centre_plan": "Centre-wise event plan",
    "category": "Category-wise report",
    "backlog": "Approval backlog",
    "completeness": "Planning completeness by centre",
    "execution": "Execution status report",
    "history": "Event history (approvals & changes)",
    "participation": "Scholarship test & mock participation",
    "batch_whatsapp": "Batch launches & WhatsApp activity",
    "readiness": "Pre-event readiness & task compliance",
}


def event_row(ev: Event, with_details: bool = False) -> dict:
    row = {
        "Event ID": ev.code,
        "Centre": ev.centre.name if ev.centre else "National",
        "Category": ev.category.name,
        "Subtype": ev.subtype or "",
        "Title": ev.title,
        "Date": fmt_date(ev.event_date),
        "Day": ev.event_date.strftime("%a"),
        "Start": fmt_time(ev.start_time),
        "End": fmt_time(ev.end_time),
        "Status": STATUS_LABELS[Status(ev.status)],
        "Approval status": approval_status(ev.status),
        "Responsible": ev.responsible_person or "",
        "Target audience": ev.target_audience or "",
        "Venue": ev.venue or "",
        "Expected": ev.expected_participation,
        "Actual": ev.actual_participation,
        "Preparation deadline": fmt_date(ev.preparation_deadline),
        "Objective": ev.objective or "",
        "Remarks": ev.remarks or "",
        "Post-event remarks": ev.post_event_remarks or "",
        "Last review comment": ev.last_review_comment or "",
        "Changed after approval": "Yes" if ev.changed_after_approval else "",
        "Proposed (recurring)": "Yes" if ev.is_proposed else "",
        "Linked event": f"EVT-{ev.linked_event_id:06d}" if ev.linked_event_id else "",
        "National parent": f"EVT-{ev.parent_event_id:06d}" if ev.parent_event_id else "",
        "Created by": ev.created_by.full_name if ev.created_by else "",
        "Created (IST)": fmt_dt(ev.created_at),
        "Last modified by": ev.updated_by.full_name if ev.updated_by else "",
        "Last modified (IST)": fmt_dt(ev.updated_at),
    }
    if with_details:
        for fdef in ev.category.field_schema or []:
            row[fdef["label"]] = display_detail(fdef, (ev.details or {}).get(fdef["key"])).replace("—", "")
    return row


def events_df(events: list[Event], with_details: bool = False) -> pd.DataFrame:
    return pd.DataFrame([event_row(e, with_details) for e in events])


def build_report(session: Session, actor: User, key: str, f: EventFilter, year: int, month: int) -> dict[str, pd.DataFrame]:
    if f.date_from is None and f.date_to is None:
        f.date_from, f.date_to = month_bounds(year, month)
    events = ev_svc.list_events(session, actor, f)
    if key == "calendar":
        return {"Calendar": events_df(events)}
    if key == "centre_plan":
        groups = defaultdict(list)
        for e in events:
            groups[e.centre.name if e.centre else "National"].append(e)
        return {name[:31]: events_df(evs, True) for name, evs in sorted(groups.items())} or {"Plan": pd.DataFrame()}
    if key == "category":
        df = events_df(events)
        sheets = {"Summary": (df.pivot_table(index="Centre", columns="Category", values="Event ID", aggfunc="count",
                                             fill_value=0, margins=True, margins_name="Total").reset_index()
                              if not df.empty else df)}
        groups = defaultdict(list)
        for e in events:
            groups[e.category.name].append(e)
        for name, evs in sorted(groups.items()):
            sheets[name[:31]] = events_df(evs, True)
        return sheets
    if key == "backlog":
        today = today_ist()
        rows = []
        for e in events:
            if e.status not in (Status.SUBMITTED, Status.RETURNED):
                continue
            r = event_row(e)
            since = e.submitted_at or e.updated_at
            r["Waiting (days)"] = (today - since.date()).days if since else None
            r["Days to event"] = (e.event_date - today).days
            rows.append(r)
        return {"Approval backlog": pd.DataFrame(rows)}
    if key == "completeness":
        rows = []
        for c in planning.network_overview(session, actor, year, month):
            if f.centre_ids and c.centre.id not in f.centre_ids:
                continue
            rows.append({
                "Centre": c.centre.name, "Month": month_label(year, month), "Plan status": c.plan_status_label,
                "Submissions": c.plan.submission_count if c.plan else 0,
                "Deadline": fmt_date(c.deadline), "Live events": c.total, "Unsubmitted": c.unsubmitted,
                "Incomplete entries": len(c.incomplete),
                "Missing mandatory": ", ".join(f"{ln.category.name} ({ln.planned}/{ln.min_count})"
                                               for ln in c.missing_mandatory),
                "Completeness %": c.score, "Overdue": "Yes" if c.overdue else "",
                **{ln.category.name: ln.planned for ln in c.lines},
            })
        return {"Completeness": pd.DataFrame(rows)}
    if key == "execution":
        today = today_ist()
        rows = []
        for e in events:
            if e.status not in APPROVED_LIKE and e.status != Status.CANCELLED:
                continue
            r = event_row(e)
            r["Execution"] = ("Completed" if e.status == Status.COMPLETED else "Cancelled"
                              if e.status == Status.CANCELLED else "Overdue – update pending"
                              if e.event_date < today else "Scheduled")
            if e.expected_participation and e.actual_participation is not None:
                r["Turnout %"] = round(100 * e.actual_participation / e.expected_participation)
            rows.append(r)
        return {"Execution": pd.DataFrame(rows)}
    if key == "history":
        ids = [e.id for e in events]
        acts = list(session.scalars(select(ApprovalAction).where(ApprovalAction.event_id.in_(ids))
                                    .order_by(ApprovalAction.created_at))) if ids else []
        by_id = {e.id: e for e in events}
        rows = [{
            "When (IST)": fmt_dt(a.created_at), "Event ID": f"EVT-{a.event_id:06d}",
            "Centre": by_id[a.event_id].centre.name if by_id[a.event_id].centre else "National",
            "Title": by_id[a.event_id].title, "Action": a.action.replace("_", " "),
            "From": a.from_status or "", "To": a.to_status or "", "By": a.actor.full_name, "Comment": a.comment or "",
        } for a in acts]
        return {"History": pd.DataFrame(rows)}
    if key == "participation":
        rows = []
        for e in events:
            if e.category.key not in ("scholarship_test", "open_mock"):
                continue
            d = e.details or {}
            rows.append({
                "Event ID": e.code, "Centre": e.centre.name if e.centre else "National", "Category": e.category.name,
                "Title": e.title, "Date": fmt_date(e.event_date), "Status": STATUS_LABELS[Status(e.status)],
                "Expected": e.expected_participation, "Registrations": d.get("registrations"),
                "Actual": e.actual_participation,
                "Turnout %": round(100 * e.actual_participation / e.expected_participation)
                if e.expected_participation and e.actual_participation is not None else None,
                "Scholarships awarded": d.get("scholarships_awarded"),
            })
        return {"Participation": pd.DataFrame(rows)}
    if key == "batch_whatsapp":
        launches = [e for e in events if e.category.key == "batch_launch"]
        camps = [e for e in events if e.category.key == "whatsapp_campaign"]
        extra_ids = [e.id for e in launches]
        if extra_ids:  # campaigns for these launches may fall in an earlier month
            camps += [c for c in session.scalars(select(Event).where(Event.linked_event_id.in_(extra_ids))).unique()
                      if c.id not in {x.id for x in camps}
                      and (f.centre_ids is None or c.centre_id in f.centre_ids)]
            from jgevents.services.permissions import can_view_centre
            camps = [c for c in camps if can_view_centre(actor, c.centre_id)]
        rows = []
        for b in launches:
            linked = [c for c in camps if c.linked_event_id == b.id]
            rows.append({
                "Batch event": b.code, "Centre": b.centre.name, "Batch": (b.details or {}).get("batch_name", ""),
                "Subject": (b.details or {}).get("subject", ""), "Launch date": fmt_date(b.event_date),
                "Launch status": STATUS_LABELS[Status(b.status)], "WhatsApp campaigns": len(linked),
                "Dispatched": sum(1 for c in linked if c.status == Status.COMPLETED),
                "Messages sent": sum((c.details or {}).get("recipients_sent") or 0 for c in linked),
                "Responses": sum((c.details or {}).get("responses") or 0 for c in linked),
            })
        return {"Batch launches": pd.DataFrame(rows), "WhatsApp campaigns": events_df(camps, True)}
    if key == "readiness":
        return readiness_report(session, actor, f, year, month)
    raise ValueError(f"Unknown report {key}")


def readiness_report(session: Session, actor: User, f: EventFilter, year: int, month: int) -> dict[str, pd.DataFrame]:
    """Operational compliance (were tasks done on time?) beside outcomes (expected vs actual turnout).
    Shown side by side for comparison only; no causal claim is made."""
    from jgevents.services import tasks as task_svc
    start, end = month_bounds(year, month)
    events = [e for e in ev_svc.list_events(session, actor, EventFilter(
        date_from=start, date_to=end, centre_ids=f.centre_ids, category_ids=f.category_ids))
        if task_svc.event_tasks(session, e.id)]
    all_tasks = [t for e in events for t in task_svc.event_tasks(session, e.id)]
    dep = task_svc.dependency_map(session, all_tasks)
    today = today_ist()
    task_rows, ev_rows = [], []
    by_centre: dict[str, dict] = defaultdict(lambda: dict(due=0, on_time=0, late=0, overdue=0, waived=0,
                                                          crit_late_days=[], flags=0, events=0, all_crit_on_time=0))
    flags = {fl.task_id for fl in task_svc.list_flags(session, actor, open_only=False,
                                                       task_ids=[t.id for t in all_tasks])}
    for e in events:
        ts = [t for t in all_tasks if t.event_id == e.id]
        r = task_svc.readiness(session, e, ts, dep)
        crit_pre = [t for t in ts if t.is_critical and t.offset_days <= 0]
        crit_on_time = all(t.status in ("waived", "not_needed") or (t.status == "done" and not t.done_late)
                           for t in crit_pre)
        c = by_centre[e.centre.name if e.centre else "National"]
        c["events"] += 1
        c["all_crit_on_time"] += crit_on_time and all(t.due_date < today or t.status != "open" for t in crit_pre)
        regs = next((t.proof_count2 for t in ts if t.proof_type == "seats" and t.proof_count2 is not None), None)
        ev_rows.append({
            "Event ID": e.code, "Centre": e.centre.name if e.centre else "National", "Category": e.category.name,
            "Title": e.title, "Date": fmt_date(e.event_date), "Status": STATUS_LABELS[Status(e.status)],
            "Readiness": r.label, "Reasons": " | ".join(r.reasons), "Tasks": r.total, "Done": r.done,
            "Done late": r.done_late, "Waived": r.waived, "Overdue": r.overdue,
            "All critical prep on time": "Yes" if crit_on_time else "No", "Booked late": "Yes" if e.booked_late else "",
            "Expected": e.expected_participation, "Registrations": regs, "Actual": e.actual_participation,
            "Turnout % of expected": round(100 * e.actual_participation / e.expected_participation)
            if e.expected_participation and e.actual_participation is not None else None,
        })
        for t in ts:
            st = task_svc.state_of(t, dep)
            if st not in ("closed", "not_needed") and t.due_date <= today:
                c["due"] += 1
            c["on_time"] += st == "done"
            c["late"] += st == "done_late"
            c["overdue"] += st == "overdue"
            c["waived"] += st == "waived"
            c["flags"] += t.id in flags
            if t.is_critical and t.status == "done" and t.done_late and t.done_at:
                c["crit_late_days"].append((to_ist(t.done_at).date() - t.due_date).days)
            task_rows.append({
                "Task": t.code, "Event ID": e.code, "Centre": e.centre.name if e.centre else "National",
                "Event": e.title, "Key": t.key, "Task title": t.title, "T": task_svc.t_label(t.offset_days),
                "Due": fmt_date(t.due_date), "Owner": t.owner_label, "Critical": "Yes" if t.is_critical else "",
                "State": task_svc.STATE_LABELS[st], "Done (IST)": fmt_dt(t.done_at),
                "Done by": t.done_by.full_name if t.done_by else "", "Proof link": t.proof_url or "",
                "Proof count": t.proof_count, "Registrations": t.proof_count2, "Reason / note": t.status_reason or "",
                "Audit flag": "Yes" if t.id in flags else "",
            })
    centre_rows = []
    for name, c in sorted(by_centre.items()):
        finished = c["on_time"] + c["late"]
        centre_rows.append({
            "Centre": name, "Events": c["events"], "Tasks due so far": c["due"], "Done on time": c["on_time"],
            "Done late": c["late"], "Overdue now": c["overdue"], "Waived": c["waived"],
            "On-time rate %": round(100 * c["on_time"] / finished) if finished else None,
            "Avg days late (critical)": round(sum(c["crit_late_days"]) / len(c["crit_late_days"]), 1)
            if c["crit_late_days"] else None,
            "Audit flags": c["flags"],
        })
    return {"By centre": pd.DataFrame(centre_rows), "Events": pd.DataFrame(ev_rows), "Tasks": pd.DataFrame(task_rows)}


def to_excel(sheets: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, df in sheets.items():
            (df if not df.empty else pd.DataFrame({"Info": ["No records for the selected filters"]})).to_excel(
                xw, sheet_name=name[:31] or "Sheet", index=False)
            ws = xw.sheets[name[:31] or "Sheet"]
            for col in ws.columns:
                width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
                ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 8), 60)
            ws.freeze_panes = "A2"
    return buf.getvalue()


def to_csv(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")  # BOM so Excel opens Hindi names correctly


def printable_month(session: Session, actor: User, year: int, month: int, f: EventFilter) -> str:
    start, end = month_bounds(year, month)
    f.date_from, f.date_to = start, end
    events = [e for e in ev_svc.list_events(session, actor, f) if e.status not in (Status.REJECTED,)]
    by_day = defaultdict(list)
    for e in events:
        by_day[e.event_date].append(e)
    esc = html.escape
    cells = []
    for week in month_grid(year, month):
        row = []
        for d in week:
            if d is None:
                row.append("<td class='out'></td>")
                continue
            items = "".join(
                f"<div class='ev' style='border-left:3px solid {COLOUR_HEX.get(e.category.colour, '#888')}'>"
                f"{STATUS_ICONS[Status(e.status)]} {fmt_time(e.start_time)} <b>{esc(e.centre.code if e.centre else 'NAT')}</b> "
                f"{esc(e.title[:40])}</div>" for e in by_day.get(d, []))
            row.append(f"<td><div class='d'>{d.day}</div>{items}</td>")
        cells.append("<tr>" + "".join(row) + "</tr>")
    rows = "".join(
        f"<tr><td>{fmt_date(e.event_date)}</td><td>{fmt_time(e.start_time)}–{fmt_time(e.end_time)}</td>"
        f"<td>{esc(e.centre.name if e.centre else 'National')}</td><td>{esc(e.category.name)}</td>"
        f"<td>{esc(e.title)}</td><td>{esc(e.responsible_person or '')}</td>"
        f"<td>{STATUS_LABELS[Status(e.status)]}</td></tr>" for e in events)
    legend = " ".join(f"{STATUS_ICONS[s]} {STATUS_LABELS[s]}" for s in Status)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>JG Offline events – {month_label(year, month)}</title>
<style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:24px;color:#1f2430}}
h1{{font-size:20px;margin:0}} .sub{{color:#666;font-size:12px;margin:4px 0 16px}}
table{{border-collapse:collapse;width:100%}} .cal td{{border:1px solid #ddd;vertical-align:top;height:90px;width:14.28%;padding:3px;font-size:10px}}
.cal th{{font-size:11px;padding:4px;background:#f3f3f3;border:1px solid #ddd}} .out{{background:#fafafa}}
.d{{font-weight:600;font-size:11px}} .ev{{padding:1px 3px;margin:2px 0;background:#f7f7f9;border-radius:2px}}
.list td,.list th{{border-bottom:1px solid #e5e5e5;padding:4px 6px;font-size:11px;text-align:left}}
h2{{font-size:15px;margin-top:24px}} @media print{{body{{margin:8mm}} h2{{page-break-before:always}}}}
</style></head><body>
<h1>Judiciary Gold Offline – Master Events Calendar · {month_label(year, month)}</h1>
<div class="sub">{len(events)} events · generated {fmt_dt(now_utc())} IST · Internal use only · {legend}</div>
<table class="cal"><tr>{''.join(f'<th>{d}</th>' for d in ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'])}</tr>{''.join(cells)}</table>
<h2>Event list</h2>
<table class="list"><tr><th>Date</th><th>Time</th><th>Centre</th><th>Category</th><th>Title</th><th>Responsible</th><th>Status</th></tr>{rows}</table>
</body></html>"""

