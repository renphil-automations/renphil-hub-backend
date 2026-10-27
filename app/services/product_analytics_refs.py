"""Server-side verification and read-time naming of product-analytics ids.

A browser can put anything in `component_id` / `target_id`. A character
pattern cannot tell an opaque id from a name ("jane-doe-salary"), so nothing
the client sends is stored unless it is one of:

* a fixed catalog code (`pending`, `history`);
* a positive integer thread id that exists AND belongs to the event's widget;
* an id found, at ingest time, in the current Hub V2 records of the kind the
  event names (nav tab, tab, component of the right widget type, Super Block
  Note tab of that widget, Detail Cards card of that widget).

AND, for every record-backed id, the reporting user must CURRENTLY be able to
view it, decided by the same live V2 access fold (`ViewerAccess`) that
authorizes the Hub UI request for that record:

* nav tab, tab, Super Block Note root and page: `verdict(node).view` — the
  gate `nav_tabs.py`, `gridstack_service` and `super_blocknote_service` use
  to serve the tab or page at all;
* any other widget (links, button, metric, Detail Cards, thread widget,
  mirror): `is_granted(node)` — the payload gate the canvas uses, which
  already carries the mirror conjunction (position AND target);
* a thread: granted on its widget, and approved or authored by the reporter
  or editable by the reporter — `thread_service.get_thread_by_id`'s rule.

Hub Admins (`ViewerAccess.full_access`) see everything, exactly as in the UI.

Anything else — unknown, wrong kind, or real but not viewable by this user —
is dropped silently and identically: the event is still stored, without the
id, the response never says which ids were dropped or why, and nothing about
the decision is logged, so ingestion cannot be used to probe for records the
caller cannot see.

Names are resolved only when an All-Scopes admin READS events
(`resolve_names`). They are returned in that response and nowhere else: not
written to `hub_product_events`, not in the CSV export, not logged, not cached.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db_v2.models.component import ComponentV2
from app.db_v2.models.nav_tab import NavTabV2
from app.db_v2.models.page_content import PageContentV2
from app.db_v2.models.tab import TabV2
from app.db_v2.models.thread import ThreadV2
from app.services.access_visibility_service import ViewerAccess
from app.services.gridstack_service import _slugify_title

# What each event's ids are allowed to refer to.
#   target: None | "nav_tab" | "root_tab" | "tab" | "sbn_tab" | "thread" | "detail_card" | "view_code"
#   component: None, or the widget types whose component link may be stored.
@dataclass(frozen=True)
class IdRule:
    target: str | None = None
    component: frozenset[str] | None = None


VIEW_CODES = frozenset({"pending", "history"})

ID_RULES: dict[str, IdRule] = {
    "navigation_tab.selected": IdRule(target="nav_tab"),
    "root_tab.selected": IdRule(target="root_tab"),
    "view_tab.selected": IdRule(target="tab"),
    "sub_tab.selected": IdRule(target="tab"),
    "blocknote_tab.selected": IdRule(target="sbn_tab", component=frozenset({"super_block_note"})),
    "link.clicked": IdRule(component=frozenset({"links"})),
    "thread_item.opened": IdRule(target="thread", component=frozenset({"thread"})),
    "airtable_metric_card.opened": IdRule(component=frozenset({"airtable_metric"})),
    "quick_action_link.clicked": IdRule(component=frozenset({"quick_actions"})),
    "detail_card.details_opened": IdRule(target="detail_card", component=frozenset({"detail_cards"})),
    "button.clicked": IdRule(component=frozenset({"button"})),
    "calendar.add_myself": IdRule(component=frozenset({"calendar"})),
    "booking.schedule_now": IdRule(component=frozenset({"booking"})),
    "resource_carousel.document_opened": IdRule(component=frozenset({"resource_carousel"})),
    "resource_carousel.scrolled": IdRule(component=frozenset({"resource_carousel"})),
    "threads_management.tab_selected": IdRule(target="view_code"),
    "revisions_management.tab_selected": IdRule(target="view_code"),
    "profile_menu.go_to_profile": IdRule(),
}

_MAX_ID_LENGTH = 255
_THREAD_ID_RE = re.compile(r"^[1-9][0-9]{0,9}$")


def _candidate(value: Any) -> str | None:
    """A lookup key, or None. Shape only — existence is checked separately."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > _MAX_ID_LENGTH:
        return None
    return value


@dataclass
class _ComponentInfo:
    id: int
    link: str
    type: str
    title: str | None
    effective_type: str
    effective_id: int
    effective_page_content_id: int | None
    effective_props: dict


def _load_components(db: Session, links: Iterable[str]) -> dict[str, _ComponentInfo]:
    links = {link for link in links if link}
    if not links:
        return {}
    rows = db.execute(
        select(ComponentV2.id, ComponentV2.link, ComponentV2.type, ComponentV2.title,
               ComponentV2.page_content_id, ComponentV2.props)
        .where(ComponentV2.link.in_(links))
    ).all()
    out: dict[str, _ComponentInfo] = {}
    mirror_targets: dict[str, str] = {}
    for cid, link, ctype, title, page_content_id, props in rows:
        props = props or {}
        out[link] = _ComponentInfo(cid, link, ctype, title, ctype, cid, page_content_id, props)
        if ctype == "mirror" and isinstance(props.get("target_link"), str):
            mirror_targets[link] = props["target_link"]
    if mirror_targets:
        # A mirror shows another widget; the event belongs to what it shows.
        targets = db.execute(
            select(ComponentV2.id, ComponentV2.link, ComponentV2.type, ComponentV2.page_content_id, ComponentV2.props)
            .where(ComponentV2.link.in_(set(mirror_targets.values())))
        ).all()
        by_link = {t.link: t for t in targets}
        for mirror_link, target_link in mirror_targets.items():
            target = by_link.get(target_link)
            if target is not None and target.type != "mirror":
                info = out[mirror_link]
                info.effective_type = target.type
                info.effective_id = target.id
                info.effective_page_content_id = target.page_content_id
                info.effective_props = target.props or {}
    return out


def _card_ids(db: Session, components: Iterable[_ComponentInfo]) -> dict[int, set[str]]:
    """Detail Cards card ids per (effective) component id."""
    components = list(components)
    page_ids = {c.effective_page_content_id for c in components if c.effective_page_content_id}
    contents: dict[int, Any] = {}
    if page_ids:
        contents = dict(db.execute(
            select(PageContentV2.id, PageContentV2.content).where(PageContentV2.id.in_(page_ids))
        ).all())
    out: dict[int, set[str]] = {}
    for component in components:
        data = contents.get(component.effective_page_content_id) if component.effective_page_content_id else None
        if not isinstance(data, dict):
            data = component.effective_props  # rows that predate page_content
        cards = data.get("cards") if isinstance(data, dict) else None
        ids = set()
        for card in cards if isinstance(cards, list) else []:
            if isinstance(card, dict) and isinstance(card.get("id"), str):
                ids.add(card["id"])
        out[component.effective_id] = ids
    return out


def _norm_email(value: str | None) -> str:
    return (value or "").strip().lower()


def verify_identifiers(
    db: Session,
    rows: list[dict[str, Any]],
    *,
    access_for: Callable[[], ViewerAccess],
    actor_email: str,
) -> None:
    """Keep only ids that the event's rule allows, the Hub records confirm,
    AND the reporting user can currently view.

    Mutates each row's `component_id` / `target_id` in place (None when not
    verified). Issues a bounded number of IN-queries per batch.

    `access_for` returns the reporter's live `ViewerAccess`; it is called at
    most once per batch, and only when at least one id survives the existence
    checks, so a batch with no ids costs no access computation. An
    inaccessible id takes exactly the same path as a nonexistent one.
    """
    nav_ids: set[str] = set()
    tab_ids: set[str] = set()
    component_links: set[str] = set()
    thread_ids: set[int] = set()
    for row in rows:
        rule = ID_RULES.get(row["event_name"], IdRule())
        row["component_id"] = _candidate(row.get("component_id")) if rule.component else None
        target = _candidate(row.get("target_id"))
        if rule.target is None:
            target = None
        elif rule.target == "view_code":
            target = target if target in VIEW_CODES else None
        elif rule.target == "thread":
            target = target if target and _THREAD_ID_RE.match(target) else None
        row["target_id"] = target
        if row["component_id"]:
            component_links.add(row["component_id"])
        if target is None:
            continue
        if rule.target == "nav_tab":
            nav_ids.add(target)
        elif rule.target in ("root_tab", "tab"):
            tab_ids.add(target)
        elif rule.target == "sbn_tab":
            component_links.add(target)
        elif rule.target == "thread":
            thread_ids.add(int(target))

    nav_pk: dict[str, int] = {}
    if nav_ids:
        nav_pk = dict(db.execute(
            select(NavTabV2.document_id, NavTabV2.id).where(NavTabV2.document_id.in_(nav_ids))
        ).all())
    tabs: dict[str, tuple[int, int | None]] = {}
    if tab_ids:
        tabs = {
            document_id: (tab_id, parent_tab_id)
            for document_id, tab_id, parent_tab_id in db.execute(
                select(TabV2.document_id, TabV2.id, TabV2.parent_tab_id).where(TabV2.document_id.in_(tab_ids))
            ).all()
        }
    components = _load_components(db, component_links)
    sbn_parent: dict[str, int | None] = {}
    if components:
        sbn_parent = dict(db.execute(
            select(ComponentV2.link, ComponentV2.super_blocknote_id).where(ComponentV2.link.in_(set(components)))
        ).all())
    threads: dict[int, tuple[int, str, str]] = {}
    if thread_ids:
        threads = {
            thread_id: (component_id, status, author_email)
            for thread_id, component_id, status, author_email in db.execute(
                select(ThreadV2.id, ThreadV2.component_id, ThreadV2.status, ThreadV2.author_email)
                .where(ThreadV2.id.in_(thread_ids))
            ).all()
        }
    cards = _card_ids(db, [
        components[r["component_id"]] for r in rows
        if r["event_name"] == "detail_card.details_opened" and r.get("component_id") in components
    ])

    # The reporter's live access, resolved lazily and at most once per batch.
    resolved: list[ViewerAccess] = []

    def access() -> ViewerAccess:
        if not resolved:
            resolved.append(access_for())
        return resolved[0]

    def can_view(node: tuple[str, int]) -> bool:
        return access().verdict(node).view

    def is_granted(node: tuple[str, int]) -> bool:
        return access().is_granted(node)

    actor = _norm_email(actor_email)

    for row in rows:
        rule = ID_RULES.get(row["event_name"], IdRule())
        component = components.get(row["component_id"]) if row["component_id"] else None
        if component is not None and component.effective_type not in (rule.component or ()):
            component = None
        if component is not None:
            node = ("component", component.id)
            # A Super Block Note root mounts on `view` (it may be revealed by a
            # granted page beneath it); every other widget needs `granted`.
            # For a mirror, `granted` already requires the target as well.
            allowed = can_view(node) if component.type == "super_block_note" else is_granted(node)
            if not allowed:
                component = None
        row["component_id"] = component.link if component else None

        target = row["target_id"]
        if target is None or rule.target == "view_code":
            continue
        verified = False
        if rule.target == "nav_tab":
            verified = target in nav_pk and can_view(("nav_tab", nav_pk[target]))
        elif rule.target == "root_tab":
            verified = target in tabs and tabs[target][1] is None and can_view(("tab", tabs[target][0]))
        elif rule.target == "tab":
            verified = target in tabs and can_view(("tab", tabs[target][0]))
        elif rule.target == "sbn_tab":
            child = components.get(target)
            verified = component is not None and child is not None and (
                child.id == component.id or sbn_parent.get(target) == component.id
            ) and can_view(("component", child.id))
        elif rule.target == "thread":
            thread = threads.get(int(target))
            if component is not None and thread is not None and thread[0] == component.effective_id:
                widget = ("component", thread[0])
                verified = is_granted(widget) and (
                    thread[1] == "approved"
                    or _norm_email(thread[2]) == actor
                    or access().verdict(widget).edit
                )
        elif rule.target == "detail_card":
            verified = component is not None and target in cards.get(component.effective_id, set())
        row["target_id"] = target if verified else None


def resolve_names(db: Session, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return admin-view-only names and safe, live destinations for events.

    Read-time only, for the All-Scopes admin view. Unknown or deleted ids get
    None and stay opaque. ``open_path`` is generated from current Hub records
    rather than the stored route template, so it is both readable and safe to
    follow. ``open_component_link`` feeds the existing component deep-link
    navigator; a mirror deliberately resolves to its original component while
    retaining ``mirror_component_link`` for audit context. None of these
    derived values are persisted, exported, logged or cached.
    """
    nav_ids, tab_ids, links, thread_ids = set(), set(), set(), set()
    for event in events:
        rule = ID_RULES.get(event.get("event_name"), IdRule())
        if event.get("component_id"):
            links.add(event["component_id"])
        target = event.get("target_id")
        if not target:
            continue
        if rule.target == "nav_tab":
            nav_ids.add(target)
        elif rule.target in ("root_tab", "tab"):
            tab_ids.add(target)
        elif rule.target == "sbn_tab":
            links.add(target)
        elif rule.target == "thread" and _THREAD_ID_RE.match(target):
            thread_ids.add(int(target))
    component_rows = db.execute(
        select(ComponentV2.link, ComponentV2.title, ComponentV2.type, ComponentV2.props)
        .where(ComponentV2.link.in_(links))
    ).all() if links else []
    components = {link: {"title": title, "type": ctype, "props": props or {}}
                  for link, title, ctype, props in component_rows}
    mirror_target_links = {
        info["props"].get("target_link")
        for info in components.values()
        if info["type"] == "mirror" and isinstance(info["props"].get("target_link"), str)
    }
    mirror_target_rows = db.execute(
        select(ComponentV2.link, ComponentV2.title, ComponentV2.type)
        .where(ComponentV2.link.in_(mirror_target_links))
    ).all() if mirror_target_links else []
    mirror_targets = {link: {"title": title, "type": ctype} for link, title, ctype in mirror_target_rows}

    nav_rows = db.execute(
        select(NavTabV2.document_id, NavTabV2.title, NavTabV2.slug).where(NavTabV2.document_id.in_(nav_ids))
    ).all() if nav_ids else []
    nav_details = {document_id: {"title": title, "slug": slug} for document_id, title, slug in nav_rows}
    tab_rows = db.execute(
        select(TabV2.document_id, TabV2.title, TabV2.parent_tab_id, TabV2.nav_tab_id)
        .where(TabV2.document_id.in_(tab_ids))
    ).all() if tab_ids else []
    tab_details = {document_id: {"title": title, "parent_id": parent_id, "nav_id": nav_id}
                   for document_id, title, parent_id, nav_id in tab_rows}
    parent_ids = {info["parent_id"] for info in tab_details.values() if info["parent_id"] is not None}
    parent_rows = db.execute(
        select(TabV2.id, TabV2.title, TabV2.nav_tab_id)
        .where(TabV2.id.in_(parent_ids))
    ).all() if parent_ids else []
    parent_details = {tab_id: {"title": title, "nav_id": nav_id} for tab_id, title, nav_id in parent_rows}
    nav_ids_for_tabs = {
        nav_id for nav_id in [
            *(info["nav_id"] for info in tab_details.values()),
            *(info["nav_id"] for info in parent_details.values()),
        ] if nav_id is not None
    }
    nav_by_id = dict(db.execute(select(NavTabV2.id, NavTabV2.slug).where(NavTabV2.id.in_(nav_ids_for_tabs))).all()) if nav_ids_for_tabs else {}
    thread_names = dict(db.execute(select(ThreadV2.id, ThreadV2.title).where(ThreadV2.id.in_(thread_ids))).all()) if thread_ids else {}

    out = []
    for event in events:
        rule = ID_RULES.get(event.get("event_name"), IdRule())
        target = event.get("target_id")
        target_name = None
        open_path = None
        open_component_link = None
        mirror_component_link = None
        if target:
            if rule.target == "nav_tab":
                details = nav_details.get(target)
                target_name = details["title"] if details else None
                if details and details["slug"]:
                    open_path = f"/{details['slug']}"
            elif rule.target in ("root_tab", "tab"):
                details = tab_details.get(target)
                target_name = details["title"] if details else None
                if details:
                    parent = parent_details.get(details["parent_id"])
                    nav_slug = nav_by_id.get(details["nav_id"] or (parent or {}).get("nav_id"))
                    slug = _slugify_title(details["title"] or "")
                    if nav_slug and slug:
                        if details["parent_id"] is None:
                            open_path = f"/{nav_slug}/{slug}"
                        else:
                            parent_slug = _slugify_title((parent or {}).get("title") or "")
                            if parent_slug:
                                open_path = f"/{nav_slug}/{parent_slug}/{slug}"
            elif rule.target == "sbn_tab":
                details = components.get(target)
                target_name = details["title"] if details else None
                if details and details["type"] != "mirror":
                    open_component_link = target
            elif rule.target == "thread" and _THREAD_ID_RE.match(target):
                target_name = thread_names.get(int(target))
        component_link = event.get("component_id")
        component = components.get(component_link) if component_link else None
        component_name = component["title"] if component else None
        if open_component_link is None and component_link and component:
            if component["type"] == "mirror":
                candidate = component["props"].get("target_link")
                target_component = mirror_targets.get(candidate) if isinstance(candidate, str) else None
                if target_component and target_component["type"] != "mirror":
                    open_component_link = candidate
                    mirror_component_link = component_link
            else:
                open_component_link = component_link
        out.append({
            **event,
            "component_name": component_name or None,
            "target_name": target_name or None,
            "open_path": open_path,
            "open_component_link": open_component_link,
            "mirror_component_link": mirror_component_link,
        })
    return out
