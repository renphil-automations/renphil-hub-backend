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

Names and open destinations are resolved only when an authorized reader READS
events (`resolve_names`), and only for records that reader can open now. They
are returned in that response and nowhere else: not written to
`hub_product_events`, not in the CSV export, not logged, not cached.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, quote, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db_v2.models.component import ComponentV2
from app.db_v2.models.gridstack import GridstackV2
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


def _sub_tab_nodes(db: Session, document_ids: Iterable[str]) -> dict[str, tuple[str, int]]:
    """Access node per sub-tab `document_id`.

    Nav-tree child tabs and Super Gridstack sub-tabs are sub-gridstacks (what
    `/v2/tabs/{id}/children` returns), not TabV2 rows. A sub-gridstack holds
    no grants; its node is its representation component, the same rule
    `access_visibility_service.resolve_gridstack_node` applies when a
    `tabs_v2` route serves it. A sub-grid with no representation row stays
    unresolved, i.e. invisible.
    """
    document_ids = {value for value in document_ids if value}
    if not document_ids:
        return {}
    grids = dict(db.execute(
        select(GridstackV2.document_id, GridstackV2.id)
        .where(GridstackV2.document_id.in_(document_ids), GridstackV2.parent_id.is_not(None))
    ).all())
    if not grids:
        return {}
    representations = dict(db.execute(
        select(ComponentV2.current_grid_id, ComponentV2.id).where(ComponentV2.current_grid_id.in_(set(grids.values())))
    ).all())
    return {
        document_id: ("component", representations[grid_id])
        for document_id, grid_id in grids.items()
        if grid_id in representations
    }


def _norm_email(value: str | None) -> str:
    return (value or "").strip().lower()


def verify_identifiers(
    db: Session,
    rows: list[dict[str, Any]],
    *,
    access_for: Callable[[], ViewerAccess],
    actor_email: str,
    require_edit: bool = False,
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
    sub_tabs = _sub_tab_nodes(db, tab_ids - set(tabs))
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
        verdict = access().verdict(node)
        return verdict.edit if require_edit else verdict.view

    def is_granted(node: tuple[str, int]) -> bool:
        return access().verdict(node).edit if require_edit else access().is_granted(node)

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
            if target in tabs:
                verified = can_view(("tab", tabs[target][0]))
            else:
                verified = target in sub_tabs and can_view(sub_tabs[target])
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


def filter_editable_events(
    db: Session,
    events: list[dict[str, Any]],
    *,
    access: ViewerAccess,
    actor_email: str,
) -> list[dict[str, Any]]:
    """Return only events tied to a resource the viewer can edit *now*.

    This uses the same identifier resolver as ingest, with the stronger
    ``edit`` predicate.  Untargeted events and fixed UI-only targets such as
    Pending/History are deliberately excluded: they cannot be safely tied to
    one editable Hub resource and must not become a side channel.
    """
    if access.full_access:
        return events
    rows = [dict(event) for event in events]
    verify_identifiers(
        db, rows, access_for=lambda: access, actor_email=actor_email,
        require_edit=True,
    )
    out: list[dict[str, Any]] = []
    for original, row in zip(events, rows):
        rule = ID_RULES.get(original.get("event_name"), IdRule())
        # Fixed view codes and rows without a verified resource have no
        # editable-object proof, so they never reach an editor's analytics.
        if row.get("component_id") or (rule.target not in (None, "view_code") and row.get("target_id")):
            out.append(row)
    return out


def verify_engagement_identifiers(
    db: Session,
    rows: list[dict[str, Any]],
    *,
    access_for: Callable[[], ViewerAccess],
    require_edit: bool = False,
) -> None:
    """Keep active-time bindings only when they are real and accessible.

    An active-time interval is tied to the navigation tree and the rendered
    tab, never inferred from its route template.  Both identifiers are
    browser supplied, so they receive the same live Hub visibility check as
    click analytics.  A missing or inaccessible id is silently discarded;
    callers receive no record-existence signal.
    """
    nav_candidates = {_candidate(row.get("nav_tab_id")) for row in rows}
    tab_candidates = {_candidate(row.get("tab_id")) for row in rows}
    nav_candidates.discard(None)
    tab_candidates.discard(None)
    nav_rows = db.execute(
        select(NavTabV2.document_id, NavTabV2.id).where(NavTabV2.document_id.in_(nav_candidates))
    ).all() if nav_candidates else []
    tab_rows = db.execute(
        select(TabV2.document_id, TabV2.id).where(TabV2.document_id.in_(tab_candidates))
    ).all() if tab_candidates else []
    nav_ids = dict(nav_rows)
    tab_nodes: dict[str, tuple[str, int]] = {document_id: ("tab", tab_id) for document_id, tab_id in tab_rows}
    tab_nodes.update(_sub_tab_nodes(db, tab_candidates - set(tab_nodes)))
    # The nav tree each tab binding belongs to, loaded once per batch: a
    # TabV2's own nav_tab_id (a variant carries its parent's), or, for a
    # sub-tab, that of the tab owning its gridstack.
    owner_nav: dict[str, int | None] = {}
    if tab_nodes:
        owner_nav.update(db.execute(
            select(TabV2.document_id, TabV2.nav_tab_id).where(TabV2.document_id.in_(set(tab_nodes)))
        ).all())
        sub_docs = {doc for doc, node in tab_nodes.items() if node[0] != "tab"}
        if sub_docs:
            owner_nav.update(db.execute(
                select(GridstackV2.document_id, TabV2.nav_tab_id)
                .join(TabV2, GridstackV2.parent_tab_id == TabV2.id)
                .where(GridstackV2.document_id.in_(sub_docs))
            ).all())
    access: ViewerAccess | None = None

    def allowed(ref: tuple[str, int]) -> bool:
        nonlocal access
        if access is None:
            access = access_for()
        verdict = access.verdict(ref)
        return verdict.edit if require_edit else verdict.view

    for row in rows:
        nav = _candidate(row.get("nav_tab_id"))
        tab = _candidate(row.get("tab_id"))
        valid_nav = nav if nav and nav in nav_ids and allowed(("nav_tab", nav_ids[nav])) else None
        valid_tab = tab if tab and tab in tab_nodes and allowed(tab_nodes[tab]) else None

        # When the browser supplies both bindings, they must describe the
        # same page tree.  Validating each identifier independently would let
        # a modified client combine two pages it may view and attribute time
        # to the wrong navigation context.  A tab-only interval remains valid
        # (for routes without a nav binding); a mismatched pair is discarded
        # entirely, exactly like any other malformed binding.
        if valid_nav and valid_tab and owner_nav.get(valid_tab) != nav_ids[valid_nav]:
            valid_nav = None
            valid_tab = None

        row["nav_tab_id"] = valid_nav
        row["tab_id"] = valid_tab


def filter_editable_engagements(
    db: Session,
    intervals: list[dict[str, Any]],
    *,
    access: ViewerAccess,
) -> list[dict[str, Any]]:
    """Return active-time intervals tied to a currently editable page.

    A tab binding is the precise page-level proof and takes priority. A
    nav-only interval is retained only when that nav tree itself is editable.
    Older route-only rows stay invisible to editors instead of leaking time.
    """
    if access.full_access:
        return intervals
    checked = [dict(interval) for interval in intervals]
    verify_engagement_identifiers(db, checked, access_for=lambda: access, require_edit=True)
    return [row for row in checked if row.get("tab_id") or row.get("nav_tab_id")]


_MAX_SUB_TAB_DEPTH = 20


def _sub_tab_trails(db: Session, document_ids: set[str]) -> dict[str, dict[str, Any]]:
    """Name, access node and URL trail of each sub-tab (sub-gridstack).

    The trail is the sub-gridstack names from just below the root canvas down
    to the sub-tab, plus the TabV2 that owns that root canvas: the Hub URL is
    that tab's path followed by each name's slug (`pathSegmentsForTab` and
    `SgsCanvasHost`'s `${routeBase}/${slug}`). A broken chain gets no trail.
    """
    if not document_ids:
        return {}
    columns = (GridstackV2.id, GridstackV2.document_id, GridstackV2.name, GridstackV2.parent_id, GridstackV2.parent_tab_id)
    grids = {row.id: row for row in db.execute(
        select(*columns).where(GridstackV2.document_id.in_(document_ids), GridstackV2.parent_id.is_not(None))
    ).all()}
    targets = {row.document_id: row.id for row in grids.values()}
    frontier = {row.parent_id for row in grids.values()} - set(grids)
    for _ in range(_MAX_SUB_TAB_DEPTH):
        if not frontier:
            break
        loaded = db.execute(select(*columns).where(GridstackV2.id.in_(frontier))).all()
        grids.update({row.id: row for row in loaded})
        frontier = {row.parent_id for row in loaded if row.parent_id is not None} - set(grids)
    nodes = _sub_tab_nodes(db, set(targets))
    out: dict[str, dict[str, Any]] = {}
    for document_id, grid_id in targets.items():
        names: list[str] = []
        base_tab_id = None
        current = grids.get(grid_id)
        for _ in range(_MAX_SUB_TAB_DEPTH + 1):
            if current is None:
                break
            if current.parent_id is None:
                base_tab_id = current.parent_tab_id
                break
            names.insert(0, current.name or "")
            current = grids.get(current.parent_id)
        out[document_id] = {
            "name": grids[grid_id].name,
            "node": nodes.get(document_id),
            "base_tab_id": base_tab_id,
            "segments": names,
        }
    return out


def _component_location_paths(
    db: Session,
    components: dict[str, dict[str, Any]],
) -> dict[str, tuple[str, int]]:
    """Resolve component links to the live page that contains them.

    A component link by itself is not a useful destination.  The former
    Analytics fallback sent every component to ``/dashboard?component=…``,
    which loses its containing page.  This follows the authoritative
    gridstack/tab tree and returns a current path plus the root TabV2 id used
    by the caller for a live view-access check.  Nothing here is persisted.
    """
    grid_ids = {info.get("grid_id") for info in components.values() if info.get("grid_id") is not None}
    if not grid_ids:
        return {}

    grids: dict[int, dict[str, Any]] = {}
    frontier = set(grid_ids)
    for _ in range(_MAX_SUB_TAB_DEPTH + 2):
        pending = frontier - set(grids)
        if not pending:
            break
        rows = db.execute(
            select(GridstackV2.id, GridstackV2.parent_id, GridstackV2.parent_tab_id, GridstackV2.name)
            .where(GridstackV2.id.in_(pending))
        ).all()
        if not rows:
            break
        for grid_id, parent_id, parent_tab_id, name in rows:
            grids[grid_id] = {"parent_id": parent_id, "parent_tab_id": parent_tab_id, "name": name or ""}
        frontier = {parent_id for _, parent_id, _, _ in rows if parent_id is not None}

    tab_ids = {grid["parent_tab_id"] for grid in grids.values() if grid.get("parent_tab_id") is not None}
    tabs: dict[int, dict[str, Any]] = {}
    frontier = set(tab_ids)
    for _ in range(_MAX_SUB_TAB_DEPTH + 2):
        pending = frontier - set(tabs)
        if not pending:
            break
        rows = db.execute(
            select(TabV2.id, TabV2.title, TabV2.parent_tab_id, TabV2.nav_tab_id).where(TabV2.id.in_(pending))
        ).all()
        if not rows:
            break
        for tab_id, title, parent_id, nav_id in rows:
            tabs[tab_id] = {"title": title or "", "parent_id": parent_id, "nav_id": nav_id}
        frontier = {parent_id for _, _, parent_id, _ in rows if parent_id is not None}

    nav_ids = {tab["nav_id"] for tab in tabs.values() if tab.get("nav_id") is not None}
    nav_slugs = dict(
        db.execute(select(NavTabV2.id, NavTabV2.slug).where(NavTabV2.id.in_(nav_ids))).all()
    ) if nav_ids else {}

    locations: dict[str, tuple[str, int]] = {}
    for link, component in components.items():
        grid = grids.get(component.get("grid_id"))
        if grid is None:
            continue
        grid_segments: list[str] = []
        for _ in range(_MAX_SUB_TAB_DEPTH + 1):
            if grid is None:
                break
            if grid["parent_id"] is not None and grid["name"]:
                grid_segments.insert(0, grid["name"])
            if grid["parent_id"] is None:
                break
            grid = grids.get(grid["parent_id"])

        root_tab_id = grid["parent_tab_id"] if grid is not None else None
        tab = tabs.get(root_tab_id)
        if tab is None:
            continue
        tab_segments: list[str] = []
        for _ in range(_MAX_SUB_TAB_DEPTH + 1):
            if tab is None:
                break
            tab_segments.insert(0, tab["title"])
            if tab["parent_id"] is None:
                break
            tab = tabs.get(tab["parent_id"])
        if tab is None or not tab_segments:
            continue
        nav_slug = nav_slugs.get(tab["nav_id"])
        segments = [_slugify_title(part) for part in [*tab_segments, *grid_segments]]
        if nav_slug and all(segments):
            locations[link] = (f"/{nav_slug}/" + "/".join(segments), root_tab_id)
    return locations


def _component_display_name(component: dict[str, Any] | None) -> str | None:
    """Return a readable, non-identifying label for a live component.

    Component titles are optional.  Falling back to the component UUID makes
    an otherwise authorized analytics row impossible to understand, so use a
    stable type label instead.  The identifier remains only an opaque internal
    reference and is never made the visible destination label.
    """
    if not component:
        return None
    title = component.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    component_type = component.get("type")
    if isinstance(component_type, str) and component_type.strip():
        return f"{component_type.replace('_', ' ').replace('-', ' ').title()} component"
    return "Hub component"


def _legacy_component_hint(value: Any) -> tuple[str, str] | None:
    """A legacy ``/page?component=<link>`` hint, never a destination.

    A short-lived pre-release build recorded a handful of literal Hub paths
    that included the component deep-link query.  The stored path cannot be
    shown again: it can contain title slugs and an opaque identifier.  We use
    it only as a candidate lookup key, then require the *current* component
    placement and the reader's live access before returning a fresh path.
    """
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return None
    candidates = parse_qs(parsed.query, keep_blank_values=False).get("component", [])
    candidate = _candidate(candidates[0]) if len(candidates) == 1 else None
    if not candidate:
        return None
    return parsed.path.rstrip("/") or "/", candidate


def component_root_tab_ids(db: Session, links: Iterable[str]) -> dict[str, int]:
    """Current root-tab placement for a set of component links.

    This is deliberately a read-time relationship. Product event rows store
    only the component's opaque link, never a page name or page URL. A page
    insight therefore follows a widget's *current* placement and cannot
    expose a historical location after a move or a deletion.
    """
    requested = {link for link in links if isinstance(link, str) and link}
    if not requested:
        return {}
    rows = db.execute(
        select(ComponentV2.link, ComponentV2.id, ComponentV2.title, ComponentV2.type, ComponentV2.props, ComponentV2.gridstack_id)
        .where(ComponentV2.link.in_(requested))
    ).all()
    components = {
        link: {"id": cid, "title": title, "type": ctype, "props": props or {}, "grid_id": grid_id}
        for link, cid, title, ctype, props, grid_id in rows
        if link
    }
    return {link: root_tab_id for link, (_path, root_tab_id) in _component_location_paths(db, components).items()}


def resolve_names(
    db: Session,
    events: list[dict[str, Any]],
    *,
    access: ViewerAccess | None = None,
) -> list[dict[str, Any]]:
    """Return read-time names and safe, live destinations for events.

    Read-time only. Unknown or deleted ids get None and stay opaque.
    ``open_path`` is generated from current Hub records rather than the
    stored route template, so it is both readable and safe to follow.
    ``open_component_link`` feeds the existing component deep-link navigator;
    a mirror deliberately resolves to its original component while retaining
    ``mirror_component_link`` for audit context. None of these derived
    values are persisted, exported, logged or cached.

    ``access`` is the reader's live access (None only for trusted internal
    callers). A name or destination is returned only when that reader can
    currently open the record — the same gates the Hub uses to serve it:
    ``view`` for a nav tab, tab, sub-tab or Super Block Note page, ``granted``
    for a widget, and, for a mirror, ``granted`` on the original as well.
    Anything else is returned exactly like a deleted record.
    """

    def can_view(node: tuple[str, int] | None) -> bool:
        return node is not None and (access is None or access.verdict(node).view)

    def is_granted(node: tuple[str, int] | None) -> bool:
        return node is not None and (access is None or access.is_granted(node))

    nav_ids, tab_ids, links, thread_ids = set(), set(), set(), set()
    legacy_component_hints: dict[int, tuple[str, str]] = {}
    for index, event in enumerate(events):
        rule = ID_RULES.get(event.get("event_name"), IdRule())
        if event.get("component_id"):
            links.add(event["component_id"])
        # A legacy URI is *not* trusted as a component binding. Keep only a
        # shape-checked candidate so it can be validated against the current
        # component table and placement below.
        if not event.get("component_id"):
            hint = _legacy_component_hint(event.get("page_route"))
            if hint:
                legacy_component_hints[index] = hint
                links.add(hint[1])
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
        select(ComponentV2.link, ComponentV2.id, ComponentV2.title, ComponentV2.type, ComponentV2.props, ComponentV2.gridstack_id)
        .where(ComponentV2.link.in_(links))
    ).all() if links else []
    components = {link: {"id": cid, "title": title, "type": ctype, "props": props or {}, "grid_id": grid_id}
                  for link, cid, title, ctype, props, grid_id in component_rows}
    mirror_target_links = {
        info["props"].get("target_link")
        for info in components.values()
        if info["type"] == "mirror" and isinstance(info["props"].get("target_link"), str)
    }
    mirror_target_rows = db.execute(
        select(ComponentV2.link, ComponentV2.id, ComponentV2.title, ComponentV2.type, ComponentV2.gridstack_id)
        .where(ComponentV2.link.in_(mirror_target_links))
    ).all() if mirror_target_links else []
    mirror_targets = {link: {"id": cid, "title": title, "type": ctype, "grid_id": grid_id}
                      for link, cid, title, ctype, grid_id in mirror_target_rows}
    component_locations = _component_location_paths(db, {**components, **mirror_targets})

    nav_rows = db.execute(
        select(NavTabV2.document_id, NavTabV2.id, NavTabV2.title, NavTabV2.slug).where(NavTabV2.document_id.in_(nav_ids))
    ).all() if nav_ids else []
    nav_details = {document_id: {"id": nid, "title": title, "slug": slug} for document_id, nid, title, slug in nav_rows}
    tab_rows = db.execute(
        select(TabV2.document_id, TabV2.id, TabV2.title, TabV2.parent_tab_id, TabV2.nav_tab_id)
        .where(TabV2.document_id.in_(tab_ids))
    ).all() if tab_ids else []
    tab_details = {document_id: {"id": tid, "title": title, "parent_id": parent_id, "nav_id": nav_id}
                   for document_id, tid, title, parent_id, nav_id in tab_rows}
    # Child, view and sub-tab ids that are sub-gridstacks rather than TabV2s.
    sub_tabs = _sub_tab_trails(db, tab_ids - set(tab_details))
    parent_ids = {info["parent_id"] for info in tab_details.values() if info["parent_id"] is not None}
    parent_ids |= {trail["base_tab_id"] for trail in sub_tabs.values() if trail["base_tab_id"] is not None}
    parent_rows = db.execute(
        select(TabV2.id, TabV2.title, TabV2.parent_tab_id, TabV2.nav_tab_id)
        .where(TabV2.id.in_(parent_ids))
    ).all() if parent_ids else []
    parent_details = {tab_id: {"title": title, "parent_id": parent_id, "nav_id": nav_id}
                      for tab_id, title, parent_id, nav_id in parent_rows}
    nav_ids_for_tabs = {
        nav_id for nav_id in [
            *(info["nav_id"] for info in tab_details.values()),
            *(info["nav_id"] for info in parent_details.values()),
        ] if nav_id is not None
    }
    nav_by_id = dict(db.execute(select(NavTabV2.id, NavTabV2.slug).where(NavTabV2.id.in_(nav_ids_for_tabs))).all()) if nav_ids_for_tabs else {}
    threads = {
        tid: {"title": title, "component_id": component_id}
        for tid, title, component_id in db.execute(
            select(ThreadV2.id, ThreadV2.title, ThreadV2.component_id).where(ThreadV2.id.in_(thread_ids))
        ).all()
    } if thread_ids else {}

    def root_tab_path(tab_id: int | None) -> str | None:
        """`/<nav>/<root tab>` for a root TabV2 id; None for a variant or a gap."""
        details = parent_details.get(tab_id) if tab_id is not None else None
        if details is None or details["parent_id"] is not None:
            return None
        nav_slug = nav_by_id.get(details["nav_id"])
        slug = _slugify_title(details["title"] or "")
        return f"/{nav_slug}/{slug}" if nav_slug and slug else None

    out = []
    for index, event in enumerate(events):
        rule = ID_RULES.get(event.get("event_name"), IdRule())
        target = event.get("target_id")
        target_name = None
        open_path = None
        open_component_link = None
        open_component_name = None
        mirror_component_link = None
        if target:
            if rule.target == "nav_tab":
                details = nav_details.get(target)
                if details and can_view(("nav_tab", details["id"])):
                    target_name = details["title"]
                    if details["slug"]:
                        open_path = f"/{details['slug']}"
            elif rule.target in ("root_tab", "tab") and target in tab_details:
                details = tab_details[target]
                if can_view(("tab", details["id"])):
                    target_name = details["title"]
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
            elif rule.target == "tab" and target in sub_tabs:
                trail = sub_tabs[target]
                if can_view(trail["node"]):
                    target_name = trail["name"]
                    base = root_tab_path(trail["base_tab_id"])
                    slugs = [_slugify_title(name) for name in trail["segments"]]
                    if base and slugs and all(slugs):
                        open_path = "/".join([base, *slugs])
            elif rule.target == "sbn_tab":
                details = components.get(target)
                if details and can_view(("component", details["id"])):
                    target_name = _component_display_name(details)
                    if details["type"] != "mirror":
                        open_component_link = target
                        open_component_name = _component_display_name(details)
            elif rule.target == "thread" and _THREAD_ID_RE.match(target):
                thread = threads.get(int(target))
                if thread and is_granted(("component", thread["component_id"])):
                    target_name = thread["title"]
        component_link = event.get("component_id")
        if not component_link and index in legacy_component_hints:
            legacy_path, candidate = legacy_component_hints[index]
            current = component_locations.get(candidate)
            candidate_component = components.get(candidate)
            # Do not let a stale path claim a currently unrelated component.
            # The fresh path must still be the same page before it becomes a
            # readable destination.  It must also still be the component
            # type expected by the recorded action, so an old link URI can
            # never be reinterpreted as (for example) a Button click.
            if (
                current
                and candidate_component
                and current[0].rstrip("/") == legacy_path
                and rule.component
                and candidate_component["type"] in rule.component
            ):
                component_link = candidate
        component = components.get(component_link) if component_link else None
        if component is not None and not is_granted(("component", component["id"])):
            component = None
        component_name = _component_display_name(component)
        if open_component_link is None and component_link and component:
            if component["type"] == "mirror":
                candidate = component["props"].get("target_link")
                target_component = mirror_targets.get(candidate) if isinstance(candidate, str) else None
                if (
                    target_component
                    and target_component["type"] != "mirror"
                    and is_granted(("component", target_component["id"]))
                ):
                    open_component_link = candidate
                    open_component_name = _component_display_name(target_component)
                    mirror_component_link = component_link
            else:
                open_component_link = component_link
                open_component_name = component_name

        # A component deep link must retain the page hint that contains the
        # component.  The query parameter is server-derived from the current
        # component row; the route is not taken from the recorded event.
        # Refuse the enhanced destination unless the reader can still view
        # that containing root tab as well as the component itself.
        if open_component_link:
            location = component_locations.get(open_component_link)
            if location and can_view(("tab", location[1])):
                open_path = f"{location[0]}?component={quote(open_component_link, safe='')}"
        resolved = {
            **event,
            "component_name": component_name or None,
            "target_name": target_name or None,
            "open_path": open_path,
            "open_component_link": open_component_link,
            "open_component_name": open_component_name or None,
            "mirror_component_link": mirror_component_link,
        }
        _, resolved["destination_label"], resolved["destination_href"] = _destination(resolved)
        out.append(resolved)
    return out


UNAVAILABLE_DESTINATION = "Deleted or unavailable destination"


def _destination(event: dict[str, Any]) -> tuple[str, str, str | None]:
    """(group key, readable label, internal href or None) for a resolved event.

    The label is only ever a current, access-checked name from
    `resolve_names`; the href only a server-derived path or the ACL-checked
    component deep link. The stored route template, a title slug, a query
    string or an opaque id is never a label: without a current name the
    destination is the neutral, non-clickable `UNAVAILABLE_DESTINATION`.
    """
    name = event.get("target_name") or event.get("open_component_name") or event.get("component_name")
    path = event.get("open_path")
    component_link = event.get("open_component_link")
    if name and path:
        return f"path:{path}", name, path
    if name and component_link:
        return f"component:{component_link}", name, f"/dashboard?component={quote(component_link, safe='')}"
    return "unresolved", UNAVAILABLE_DESTINATION, None


def summarize_locations(
    db: Session,
    groups: list[dict[str, Any]],
    *,
    access: ViewerAccess | None = None,
) -> list[dict[str, Any]]:
    """Combine exact event groups into readable, clickable destinations.

    This is intentionally a read-time view, after the database has grouped by
    safe stored ids. ``href`` is either a server-derived internal path or the
    existing component deep-link form, and only for a record the reader can
    currently open (see `resolve_names`). Unresolved, deleted or inaccessible
    records share one neutral, non-clickable row (see `_destination`); an old
    stored route is never echoed, since it could carry title slugs, component
    ids or query data.
    """
    combined: dict[str, dict[str, Any]] = {}
    for event in resolve_names(db, groups, access=access):
        key, label, href = _destination(event)
        item = combined.setdefault(key, {
            "key": key,
            "label": label,
            "href": href,
            "count": 0,
            "mirror": bool(event.get("mirror_component_link")),
        })
        item["count"] += int(event.get("count") or 0)
        item["mirror"] = item["mirror"] or bool(event.get("mirror_component_link"))
    return sorted(combined.values(), key=lambda item: (-item["count"], item["label"].casefold()))[:25]
