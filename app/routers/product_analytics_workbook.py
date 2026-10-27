"""Excel workbook for Bot Management → Product usage.

Built the same way as the Agent analytics workbook (`bot_management`
`/analytics/export/xlsx`): xlsxwriter, one sheet per report with a native
Excel chart, its chart data and the supporting detail rows, in the same
visual style (it reuses `_write_analytics_report_sheet`).

Privacy, by construction:
* People's emails AND current resource names are written ONLY when the admin
  explicitly asks for the per-person export (`include_people=True`). Names
  are looked up at export time (`resolve_names`, as the admin screen does)
  and are never stored. Without it there is no "Top people" or "Visited by
  person" sheet, no user or name column, and the user filter is described
  without its value; only catalog descriptions, route templates and opaque
  ids are written.
* Every text cell is written as a string; formulas and URLs are disabled
  for the whole workbook (`strings_to_formulas` / `strings_to_urls` off).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from typing import Any

import xlsxwriter

from app.routers.bot_management import (
    _analytics_datetime,
    _analytics_export_timezone,
    _analytics_timestamp,
    _write_analytics_report_sheet,
)
from app.services import product_analytics_service as service

_TRIGGER_LABELS = {"click": "Click", "reselect": "Reselect", "default": "Default"}
_ENV_LABELS = {"production": "Production", "sandbox": "Sandbox", "local": "Local", "unconfigured": "Unconfigured"}
_TAB_KIND_LABELS = {
    "nav_tab": "Navigation tab", "root": "Root tab", "variant": "Variant tab",
    "child": "Child tab", "sgs": "Sub-tab", "sbn": "BlockNote tab",
}
_CODE_LABELS = {
    "pending": "Pending", "history": "History", "previous": "Previous", "next": "Next", "arrow": "Arrow",
    "button": "Button", "external": "External link", "internal": "Hub link", "email": "Email link",
    "scheme": "App link", "none": "No destination",
}
_MAX_ZERO_FILL_DAYS = 366
_MIN_DAYS = 14
_FILTER_LABELS = [
    ("date_from", "From"), ("date_to", "To"), ("actor_email", "User"), ("page_route", "Page route"),
    ("component_type", "Component"), ("event_name", "Action"), ("trigger", "Trigger"), ("environment", "Environment"),
]


def _humanize(code: str | None) -> str:
    text = (code or "").replace("_", " ").replace("-", " ").strip()
    return text[:1].upper() + text[1:] if text else "Unknown"


def _trigger_kind(event: dict[str, Any]) -> str:
    if event.get("trigger") == "default":
        return "default"
    return "reselect" if (event.get("properties") or {}).get("reselect") else "click"


def _context(event: dict[str, Any]) -> str:
    props = event.get("properties") or {}
    parts: list[str] = []
    if isinstance(props.get("tab_kind"), str):
        parts.append(_TAB_KIND_LABELS.get(props["tab_kind"], props["tab_kind"]))
    for key in ("view", "direction", "destination_kind", "button_style"):
        if isinstance(props.get(key), str):
            parts.append(_CODE_LABELS.get(props[key], props[key]))
    if isinstance(props.get("item_index"), int) and not isinstance(props.get("item_index"), bool):
        parts.append(f"item {props['item_index'] + 1}")
    return " · ".join(parts)


def _visited(event: dict[str, Any], descriptions: dict[str, str]) -> tuple[tuple[str, str], str, str, str]:
    """What an event points at, for the per-person view (names included).

    Returns (group key, visited label, type, "in" widget). Uses the live
    names added by `resolve_names`; an unknown or deleted record keeps its
    opaque id."""
    props = event.get("properties") or {}
    kind = _TAB_KIND_LABELS.get(props.get("tab_kind")) if isinstance(props.get("tab_kind"), str) else None
    kind = kind or _humanize(event.get("component_type"))
    target_id, component_id = event.get("target_id"), event.get("component_id")
    target_name, component_name = event.get("target_name"), event.get("component_name")
    view = props.get("view") if isinstance(props.get("view"), str) else None
    if target_id and target_id != view:
        label = target_name or f"{target_id} (name unavailable)"
        inside = component_name or ""
        key = ("target", target_id)
    elif component_id:
        label = component_name or f"{component_id} (name unavailable)"
        inside = ""
        key = ("component", component_id)
    else:
        context = _context(event)
        label = descriptions.get(event.get("event_name") or "", event.get("event_name") or "")
        if context:
            label = f"{label} · {context}"
        inside = ""
        key = ("action", label)
    return (key[0], f"{event.get('component_type')}|{key[1]}"), label, kind, inside


def _percent(count: int, total: int) -> str:
    return f"{round(count * 100 / total)}%" if total else "0%"


def _day_series(by_day: list[dict[str, Any]], filters: service.EventFilters) -> list[tuple[str, int]]:
    """Daily counts, zero-filled across the filtered range (or at least the
    two weeks up to the last recorded day) so quiet days show in the chart.
    Counts are not changed."""
    counts = {str(d["day"]): int(d["count"]) for d in by_day}
    if not counts and not (filters.date_from and filters.date_to):
        return []
    days = sorted(counts)
    end = filters.date_to or date.fromisoformat(days[-1])
    # At least two weeks, like the dashboard chart, unless "From" is set.
    start = filters.date_from or min(date.fromisoformat(days[0]), end - timedelta(days=_MIN_DAYS - 1))
    if start > end:
        start, end = end, start
    if (end - start).days + 1 > _MAX_ZERO_FILL_DAYS:
        return [(day, counts[day]) for day in days]
    out = []
    current = start
    while current <= end:
        key = current.isoformat()
        out.append((key, counts.get(key, 0)))
        current += timedelta(days=1)
    return out


def _filters_description(filters: service.EventFilters, *, include_people: bool) -> str:
    parts = []
    for key, label in _FILTER_LABELS:
        value = getattr(filters, key, None)
        if value in (None, ""):
            continue
        if key == "actor_email" and not include_people:
            parts.append(f"{label}: (one person, email not included)")
        else:
            parts.append(f"{label}: {value}")
    return "; ".join(parts) if parts else "None (all recorded events)"


_PEOPLE_COLORS = ["#FF7F50", "#0A84FF", "#30D158", "#BF5AF2", "#FF9F0A", "#64D2FF", "#FF6482", "#8E8E93"]
_MAX_MATRIX_TYPES = 7


def _write_people_sheets(
    workbook: xlsxwriter.Workbook,
    *,
    summary: dict[str, Any],
    events: list[dict[str, Any]],
    descriptions: dict[str, str],
    viewer_tz: Any,
    subtitle: str,
    formats: dict[str, Any],
) -> None:
    """"Top people" (Agent-workbook layout) plus "Visited by person": each
    person's clicks by component type (stacked bar) and, per person, every
    tab, widget, page or thread they used, with counts and last visit."""
    per_person: dict[str, Counter] = defaultdict(Counter)
    last_seen: dict[str, str] = {}
    by_type: dict[str, Counter] = defaultdict(Counter)
    type_totals: Counter = Counter()
    items: dict[str, dict[tuple[str, str], dict[str, Any]]] = defaultdict(dict)
    for event in events:
        email = event.get("actor_email") or "Unknown user"
        kind = _trigger_kind(event)
        stamp = _analytics_timestamp(event.get("occurred_at"), viewer_tz)
        per_person[email][kind] += 1
        last_seen[email] = max(last_seen.get(email, ""), stamp)
        component = _humanize(event.get("component_type"))
        by_type[email][component] += 1
        type_totals[component] += 1
        key, label, item_type, inside = _visited(event, descriptions)
        item = items[email].setdefault(key, {"label": label, "type": item_type, "in": inside, "counts": Counter(), "last": ""})
        item["counts"][kind] += 1
        item["last"] = max(item["last"], stamp)

    by_user = summary.get("by_user") or []
    people = [r["key"] or "Unknown user" for r in by_user]
    # Anyone in the exported rows but beyond the summary's top 25.
    people += sorted((p for p in per_person if p not in people), key=lambda p: -sum(per_person[p].values()))

    def top_item(email: str) -> str:
        if not items[email]:
            return ""
        best = max(items[email].values(), key=lambda i: sum(i["counts"].values()))
        return best["label"]

    _write_analytics_report_sheet(
        workbook, sheet_name="Top people", title="Top people", subtitle=subtitle,
        series_headers=("User", "Events"),
        series_rows=[(r["key"] or "Unknown user", int(r["count"])) for r in by_user],
        detail_headers=["User", "Events", "Clicks", "Reselects", "Defaults", "Items used", "Most used", "Last event"],
        detail_rows=[[
            email, sum(per_person[email].values()), per_person[email]["click"], per_person[email]["reselect"],
            per_person[email]["default"], len(items[email]), top_item(email), last_seen.get(email, ""),
        ] for email in people],
        chart_kind="pie",
    )

    ws = workbook.add_worksheet("Visited by person")
    ws.hide_gridlines(2)
    ws.set_tab_color("#FF7F50")
    types = [t for t, _ in type_totals.most_common(_MAX_MATRIX_TYPES)]
    has_other = len(type_totals) > len(types)
    columns = types + (["Other"] if has_other else [])
    ws.set_column(0, 0, 40)
    ws.set_column(1, 1, 18)
    ws.set_column(2, 2, 28)
    ws.set_column(3, max(3, len(columns) + 1), 12)
    ws.set_column(7, 7, 21)  # "Last visit" in the per-person tables
    last_col = max(8, len(columns) + 1)
    ws.merge_range(0, 0, 0, last_col, "What each person used", formats["title"])
    ws.merge_range(1, 0, 1, last_col, subtitle, formats["subtitle"])
    ws.set_row(0, 28)
    if not people:
        ws.write_string(4, 0, "No matching events", formats["note"])
        return

    # A. Person × component type matrix and a stacked bar chart.
    ws.write_string(3, 0, "Events by person and component", formats["section"])
    ws.write_row(4, 0, ["Person", *columns, "Total"], formats["header"])
    for row, email in enumerate(people, start=5):
        ws.write_string(row, 0, email, formats["text"])
        for col, component in enumerate(types, start=1):
            ws.write_number(row, col, by_type[email][component], formats["count"])
        if has_other:
            other = sum(c for t, c in by_type[email].items() if t not in types)
            ws.write_number(row, len(types) + 1, other, formats["count"])
        ws.write_number(row, len(columns) + 1, sum(by_type[email].values()), formats["count"])
    last_matrix_row = 4 + len(people)
    chart = workbook.add_chart({"type": "bar", "subtype": "stacked"})
    for col, name in enumerate(columns, start=1):
        chart.add_series({
            "name": ["Visited by person", 4, col],
            "categories": ["Visited by person", 5, 0, last_matrix_row, 0],
            "values": ["Visited by person", 5, col, last_matrix_row, col],
            "fill": {"color": _PEOPLE_COLORS[(col - 1) % len(_PEOPLE_COLORS)]},
            "gap": 60,
        })
    chart.set_title({"name": "Clicks per person by component"})
    chart.set_legend({"position": "bottom"})
    chart.set_y_axis({"reverse": True})
    chart.set_style(10)
    chart_height = max(260, 26 * len(people) + 110)
    chart.set_size({"width": 760, "height": chart_height})
    chart_row = last_matrix_row + 2
    ws.insert_chart(chart_row, 0, chart)

    # B. Per person: everything they used, grouped (collapsible in Excel).
    row = chart_row + chart_height // 20 + 3
    ws.write_string(row, 0, "What each person visited", formats["section"])
    row += 1
    ws.write_string(row, 0, "Rows are grouped per person; use the outline buttons on the left to collapse them.", formats["note"])
    row += 2
    detail_headers = ["Visited", "Type", "In", "Clicks", "Reselects", "Defaults", "Total", "Last visit"]
    for email in people:
        counts = per_person[email]
        ws.merge_range(row, 0, row, 7, (
            f"{email}  ·  {sum(counts.values())} events ({counts['click']} clicks, {counts['reselect']} reselects, "
            f"{counts['default']} defaults)  ·  {len(items[email])} items  ·  last {last_seen.get(email, '')}"
        ), formats["section"])
        row += 1
        ws.write_row(row, 0, detail_headers, formats["header"])
        ws.set_row(row, None, None, {"level": 1})
        row += 1
        ranked = sorted(items[email].values(), key=lambda i: (-sum(i["counts"].values()), i["label"]))
        for item in ranked:
            c = item["counts"]
            ws.write_string(row, 0, item["label"], formats["text"])
            ws.write_string(row, 1, item["type"], formats["text"])
            ws.write_string(row, 2, item["in"], formats["text"])
            ws.write_number(row, 3, c["click"], formats["count"])
            ws.write_number(row, 4, c["reselect"], formats["count"])
            ws.write_number(row, 5, c["default"], formats["count"])
            ws.write_number(row, 6, sum(c.values()), formats["count"])
            ws.write_string(row, 7, item["last"], formats["text"])
            ws.set_row(row, None, None, {"level": 1})
            row += 1
        row += 1
    ws.freeze_panes(3, 0)


def build_product_usage_workbook(
    *,
    summary: dict[str, Any],
    events: list[dict[str, Any]],
    total_events: int,
    filters: service.EventFilters,
    include_people: bool,
    environment: str,
    viewer_time_zone: str | None,
    generated_at: datetime | None = None,
) -> bytes:
    output = BytesIO()
    workbook = xlsxwriter.Workbook(output, {
        "in_memory": True,
        # Text is text: nothing an event carries can become a formula or link.
        "strings_to_formulas": False,
        "strings_to_urls": False,
    })
    workbook.set_properties({"title": "RenPhil Hub product usage", "company": "Renaissance Philanthropy"})
    viewer_tz, tz_label = _analytics_export_timezone(viewer_time_zone)
    generated = (generated_at or datetime.now(timezone.utc)).astimezone(viewer_tz).strftime("%Y-%m-%d %H:%M")
    subtitle = (
        f"RenPhil Hub product usage ({_ENV_LABELS.get(environment, environment)}). Times are shown in {tz_label}; "
        f"days are UTC. People's emails and current names {'included' if include_people else 'not included'}."
    )
    descriptions = {name: entry.description for name, entry in service.EVENT_CATALOG.items()}
    components_of = {name: entry.component_type for name, entry in service.EVENT_CATALOG.items()}

    totals = summary.get("totals") or {}
    total = int(totals.get("events") or 0)
    triggers = {row["key"]: int(row["count"]) for row in summary.get("by_trigger") or []}
    clicks, defaults = triggers.get("click", 0), triggers.get("default", 0)
    by_day = summary.get("by_day") or []
    reselects = sum(1 for e in events if _trigger_kind(e) == "reselect")

    # Per-day split from the exported rows (complete when nothing was capped).
    daily: dict[str, Counter] = defaultdict(Counter)
    daily_people: dict[str, set] = defaultdict(set)
    for event in events:
        when = _analytics_datetime(event.get("occurred_at"))
        if when is None:
            continue
        key = when.astimezone(timezone.utc).date().isoformat()
        daily[key][_trigger_kind(event)] += 1
        daily_people[key].add(event.get("actor_email"))

    # ── Overview ─────────────────────────────────────────────────────────
    ws = workbook.add_worksheet("Overview")
    ws.hide_gridlines(2)
    ws.set_tab_color("#FF7F50")
    ws.set_column(0, 0, 30)
    ws.set_column(1, 1, 46)
    ws.set_column(2, 2, 3)
    ws.set_column(3, 10, 12)
    title_f = workbook.add_format({"bold": True, "font_size": 18, "font_color": "#1D1D1F"})
    subtitle_f = workbook.add_format({"font_size": 9, "font_color": "#6E6E73"})
    section_f = workbook.add_format({"bold": True, "font_color": "#1D1D1F", "bg_color": "#F5F5F7"})
    header_f = workbook.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#FF7F50"})
    label_f = workbook.add_format({"font_color": "#6E6E73", "bottom": 1, "bottom_color": "#E5E5EA"})
    value_f = workbook.add_format({"bold": True, "font_color": "#1D1D1F", "bottom": 1, "bottom_color": "#E5E5EA", "align": "left"})
    number_f = workbook.add_format({"bold": True, "font_color": "#1D1D1F", "num_format": "#,##0", "bottom": 1, "bottom_color": "#E5E5EA", "align": "left"})
    text_f = workbook.add_format({"font_color": "#1D1D1F", "bottom": 1, "bottom_color": "#E5E5EA"})
    count_f = workbook.add_format({"font_color": "#1D1D1F", "num_format": "#,##0", "bottom": 1, "bottom_color": "#E5E5EA"})
    note_f = workbook.add_format({"italic": True, "font_size": 9, "font_color": "#6E6E73", "text_wrap": True, "valign": "top"})

    ws.merge_range(0, 0, 0, 7, "Hub product usage", title_f)
    ws.merge_range(1, 0, 1, 7, subtitle, subtitle_f)
    ws.set_row(0, 28)
    ws.write_string(3, 0, "Summary", section_f)
    ws.write_string(3, 1, "", section_f)
    active_days = [d for d in by_day if int(d["count"])]
    rows: list[tuple[str, Any]] = [
        ("Recorded events", total),
        ("People", int(totals.get("users") or 0)),
        ("Deliberate clicks", clicks),
        ("  of which reselects", reselects),
        ("Default selections", defaults),
        ("Click share", _percent(clicks, total)),
        ("Active days", len(active_days)),
        ("First day (UTC)", str(active_days[0]["day"]) if active_days else "—"),
        ("Last day (UTC)", str(active_days[-1]["day"]) if active_days else "—"),
        ("Filters", _filters_description(filters, include_people=include_people)),
        ("People's emails and names", "Included" if include_people else "Not included"),
        ("Generated", f"{generated} ({tz_label})"),
    ]
    for index, (label, value) in enumerate(rows, start=4):
        ws.write_string(index, 0, label, label_f)
        if isinstance(value, int):
            ws.write_number(index, 1, value, number_f)
        else:
            ws.write_string(index, 1, str(value), value_f)
    note_row = 4 + len(rows) + 1
    capped = total_events > len(events)
    ws.merge_range(note_row, 0, note_row + 2, 1, (
        "Counts come from Hub navigation and widget clicks. Defaults are tabs the Hub opened automatically after a click. "
        + ("Emails and current tab, widget, page and thread names are included (looked up at export time). " if include_people else "Names of tabs, widgets and pages are not exported; ids are opaque. ")
        + (f"The Events sheet holds the newest {len(events):,} of {total_events:,} matching events." if capped else "")
    ), note_f)

    ws.write_string(3, 3, "Trigger", header_f)
    ws.write_string(3, 4, "Events", header_f)
    trigger_rows = [("Clicks", clicks - reselects), ("Reselects", reselects), ("Defaults", defaults)]
    for index, (label, count) in enumerate(trigger_rows, start=4):
        ws.write_string(index, 3, label, text_f)
        ws.write_number(index, 4, count, count_f)
    if total:
        chart = workbook.add_chart({"type": "doughnut"})
        chart.add_series({
            "name": "How events start",
            "categories": ["Overview", 4, 3, 6, 3],
            "values": ["Overview", 4, 4, 6, 4],
            "points": [{"fill": {"color": "#FF7F50"}}, {"fill": {"color": "#FFB495"}}, {"fill": {"color": "#0A84FF"}}],
        })
        chart.set_title({"name": "How events start"})
        chart.set_style(10)
        chart.set_hole_size(55)
        chart.set_size({"width": 420, "height": 260})
        ws.insert_chart(8, 3, chart)

    # ── Report sheets (same layout as the Agent analytics workbook) ───────
    day_series = _day_series(by_day, filters)
    _write_analytics_report_sheet(
        workbook, sheet_name="Activity by day", title="Activity by day", subtitle=subtitle,
        series_headers=("Day (UTC)", "Events"), series_rows=day_series,
        detail_headers=["Day (UTC)", "Events", "Clicks", "Reselects", "Defaults", "People"],
        detail_rows=[[
            day, count, daily[day]["click"], daily[day]["reselect"], daily[day]["default"], len(daily_people[day]),
        ] for day, count in day_series if count],
        chart_kind="column",
    )
    by_event = summary.get("by_event") or []
    _write_analytics_report_sheet(
        workbook, sheet_name="By action", title="Events by action", subtitle=subtitle,
        series_headers=("Action", "Events"),
        series_rows=[(descriptions.get(r["key"], r["key"]), int(r["count"])) for r in by_event],
        detail_headers=["Action", "Event code", "Component", "Events", "Share"],
        detail_rows=[[
            descriptions.get(r["key"], r["key"]), r["key"], _humanize(components_of.get(r["key"])), int(r["count"]), _percent(int(r["count"]), total),
        ] for r in by_event],
        chart_kind="bar",
    )
    by_component = summary.get("by_component") or []
    _write_analytics_report_sheet(
        workbook, sheet_name="By component", title="Events by component", subtitle=subtitle,
        series_headers=("Component", "Events"),
        series_rows=[(_humanize(r["key"]), int(r["count"])) for r in by_component],
        detail_headers=["Component", "Component code", "Events", "Share"],
        detail_rows=[[_humanize(r["key"]), r["key"], int(r["count"]), _percent(int(r["count"]), total)] for r in by_component],
        chart_kind="pie",
    )
    by_page = summary.get("by_page") or []
    _write_analytics_report_sheet(
        workbook, sheet_name="Top pages", title="Top pages (route templates)", subtitle=subtitle,
        series_headers=("Page route", "Events"),
        series_rows=[(r["key"], int(r["count"])) for r in by_page],
        detail_headers=["Page route", "Events", "Share"],
        detail_rows=[[r["key"], int(r["count"]), _percent(int(r["count"]), total)] for r in by_page],
        chart_kind="bar",
    )
    if include_people:
        _write_people_sheets(workbook, summary=summary, events=events, descriptions=descriptions,
                             viewer_tz=viewer_tz, subtitle=subtitle, formats={
                                 "title": title_f, "subtitle": subtitle_f, "section": section_f, "header": header_f,
                                 "text": text_f, "count": count_f, "note": note_f,
                             })

    # Wider first columns for readable action, component, route and email
    # labels (the shared helper sizes columns for the Agent workbook's data).
    for sheet_name, widths in (
        ("Activity by day", [16, 12, 12, 12, 12, 12]),
        ("By action", [46, 30, 22, 12, 10]),
        ("By component", [26, 24, 12, 10]),
        ("Top pages", [34, 12, 10]),
        ("Top people", [34, 12, 12, 12, 12, 14, 40, 21]),
    ):
        sheet = workbook.get_worksheet_by_name(sheet_name)
        if sheet is not None:
            for index, width in enumerate(widths):
                sheet.set_column(index, index, width)

    # ── Events ───────────────────────────────────────────────────────────
    ev = workbook.add_worksheet("Events")
    ev.hide_gridlines(2)
    ev.set_tab_color("#FF7F50")
    headers = ["When", "Action", "Trigger", "Context", "Component", "Page route", "Environment"]
    widths = [20, 44, 11, 26, 18, 26, 13]
    if include_people:
        headers += ["User", "Visited", "In"]
        widths += [30, 36, 28]
    headers += ["Component id", "Target id", "Event code", "Event id"]
    widths += [24, 24, 30, 38]
    for index, width in enumerate(widths):
        ev.set_column(index, index, width)
    ev.merge_range(0, 0, 0, len(headers) - 1, "Product usage events", title_f)
    ev.merge_range(1, 0, 1, len(headers) - 1, subtitle, subtitle_f)
    ev.set_row(0, 28)
    ev.write_row(4, 0, headers, header_f)
    for row_index, event in enumerate(events, start=5):
        values: list[str] = [
            _analytics_timestamp(event.get("occurred_at"), viewer_tz),
            descriptions.get(event.get("event_name") or "", event.get("event_name") or ""),
            _TRIGGER_LABELS[_trigger_kind(event)],
            _context(event),
            _humanize(event.get("component_type")),
            event.get("page_route") or "",
            _ENV_LABELS.get(event.get("environment") or "", event.get("environment") or ""),
        ]
        if include_people:
            _, visited_label, _, visited_in = _visited(event, descriptions)
            values += [event.get("actor_email") or "", visited_label, visited_in]
        values += [event.get("component_id") or "", event.get("target_id") or "", event.get("event_name") or "", event.get("event_id") or ""]
        for column_index, value in enumerate(values):
            ev.write_string(row_index, column_index, value, text_f)
    if events:
        ev.autofilter(4, 0, 4 + len(events), len(headers) - 1)
    else:
        ev.write_string(5, 0, "No matching events", note_f)
    ev.freeze_panes(5, 0)

    workbook.close()
    return output.getvalue()
