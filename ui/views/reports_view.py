from __future__ import annotations

import streamlit as st

from jgevents.services import reports
from jgevents.services.events import EventFilter
from jgevents.utils.dates import month_label
from ui.components.widgets import event_filters
from ui.core import db, me, month_picker, page_header


def render() -> None:
    user = me()
    page_header("Reports & exports", "Every export respects your access and the filters below")
    y, m = month_picker("rep")
    f = event_filters("rep", user)
    key = st.selectbox("Report", list(reports.REPORTS), format_func=reports.REPORTS.get, key="rep_key")
    flt = EventFilter(centre_ids=f.centre_ids, category_ids=f.category_ids, statuses=f.statuses,
                      approval_statuses=f.approval_statuses, responsible=f.responsible)
    with db() as s:
        sheets = reports.build_report(s, user, key, flt, y, m)
        html_doc = reports.printable_month(s, user, y, m, EventFilter(
            centre_ids=f.centre_ids, category_ids=f.category_ids, statuses=f.statuses,
            approval_statuses=f.approval_statuses, responsible=f.responsible))
    stem = f"jg_{key}_{y}-{m:02d}"
    c1, c2, c3 = st.columns(3)
    c1.download_button("⬇ Excel (all sheets)", reports.to_excel(sheets), f"{stem}.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", on_click="ignore",
                       width="stretch", type="primary")
    first = next(iter(sheets.values()))
    c2.download_button("⬇ CSV (first sheet)", reports.to_csv(first), f"{stem}.csv", "text/csv", on_click="ignore",
                       width="stretch")
    c3.download_button("🖨 Printable monthly overview", html_doc.encode("utf-8"), f"jg_calendar_{y}-{m:02d}.html",
                       "text/html", on_click="ignore", width="stretch",
                       help="Open the file in a browser and print / save as PDF")
    st.markdown(f"#### {reports.REPORTS[key]} · {month_label(y, m)}")
    for name, df in sheets.items():
        if len(sheets) > 1:
            st.markdown(f"**{name}**")
        if df.empty:
            st.info("No records for the selected month and filters.")
        else:
            st.dataframe(df, hide_index=True, width="stretch")
