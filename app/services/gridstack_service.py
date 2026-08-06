"""
Phase 3 v2 service layer: CRUD + GridCanvasContent<->rows translation for the
normalized tabs/gridstacks/components/page_content schema (app.db_v2).

Every function here mirrors a same-named function in tab_service.py, but the
response shapes match exactly (TabSummaryResponse / TabWorkspaceResponse /
PageContentWorkspaceResponse) so the v2 router can reuse the v1 Pydantic
schemas unchanged, and so the frontend needs no new data model for v2 tabs.

A "tab" the frontend addresses via documentId is either a TabV2 (root) or a
nested GridstackV2 (sub-tab) — both have their own document_id, and every
lookup here goes through GridstackV2.document_id (a root's own gridstack row
carries the same document_id as its TabV2, see create_tab_v2).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any
from uuid import uuid4
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db_v2.models.tab import TabV2
from app.db_v2.models.gridstack import GridstackV2
from app.db_v2.models.component import ComponentV2
from app.db_v2.models.page_content import PageContentV2
from app.db_v2.models.nav_tab import NavTabV2

from app.services.access_control_service import (
    NodeRef,
    apply_reparent_repair,
    apply_write,
    describe_write_plan,
    node_summary,
    plan_write,
    principal_from_payload,
    purge_principal,
    reset_to_inherited,
    resolved_ac_for_gridstack,
)
from app.services.tab_service import DEFAULT_ACCESS_CONTROL, access_control_subset_violation


# ---------------------------------------------------------
# Constants shared with tab_service.py's validation rules
# ---------------------------------------------------------

MAX_TITLE_LENGTH = 255
MAX_DOCUMENT_ID_LENGTH = 255
MAX_LOCKED_BY_LENGTH = 255

MIN_ORDER_VALUE = -2147483648
MAX_ORDER_VALUE = 2147483647

RESTRICTED_WIDGET_TYPE = "restricted"
MIRROR_WIDGET_TYPE = "mirror"

# A component row that represents a gridstack itself (see ComponentV2's
# current_grid_id), not a real widget on any canvas. Every gridstack gets
# exactly one, created alongside it. Starts as GRIDSTACK_WIDGET_TYPE; flips
# to SUPER_GRIDSTACK_WIDGET_TYPE while its gridstack's settings.sgs tab-bar
# config is set, and back when it's cleared (see the sync in
# update_tab_content_v2). Never a pickable/renderable widget — every query
# that lists "real" components on a canvas must exclude both types.
GRIDSTACK_WIDGET_TYPE = "gridstack"
SUPER_GRIDSTACK_WIDGET_TYPE = "super_gridstack"
GRIDSTACK_REPRESENTATION_TYPES = (GRIDSTACK_WIDGET_TYPE, SUPER_GRIDSTACK_WIDGET_TYPE)

AIRTABLE_WIDGET_TYPE = "airtable"

# Keys on an `airtable` widget's data blob that decide WHO sees WHICH rows,
# name the data source, or hold the credential used to fetch it.
#
# `PUT /v2/tabs/{document_id}/content` — the canvas save that lands in
# update_tab_content_v2 below — has NO auth dependency, so anything writable
# through it is writable by anyone who can reach the API. Without this list,
# an attacker who can no longer *read* the PAT (it is stripped on the way
# out) could still neutralise the personalize filter around it and have the
# server fetch every row under the stored token. These keys are therefore
# read back from storage on every canvas save and the incoming values
# discarded; the only way to change them is the authenticated
# `PUT /data/airtable/component/{link}/config` endpoint.
#
# The component's own `access_control` COLUMN is protected the same way (it
# is absent from this tuple only because it is a column, not blob data) —
# see the two call sites in update_tab_content_v2.
#
# NEVER add a field to the frontend's AirtableWidgetData that decides
# visibility, names the data source, or holds a secret without adding it
# here too: omitting one silently reopens the bypass this list exists to
# close. Background: AI Docs/plan_airtable_personalize_backend_enforcement.md
AIRTABLE_PROTECTED_DATA_FIELDS = (
    "pat",
    "patUpdatedAt",
    "sourceUrl",
    "personalizeEnabled",
    "personalizeColumn",
)

# Keys the SERVER derives on every read and must never store. `hasPat` stands
# in for the stripped `pat` so the UI can render "configured" without ever
# seeing the token. It rides the same blob that round-trips through the canvas
# save, so it has to be dropped on the way back in — otherwise a client could
# assert `hasPat: true` for a widget with no token, and the stored value would
# be indistinguishable from the computed one on the next read.
AIRTABLE_COMPUTED_DATA_FIELDS = ("hasPat",)


# ---------------------------------------------------------
# Small validation / generation helpers
# ---------------------------------------------------------

def _slugify_title(title: str) -> str:
    """Mirrors the frontend's slugifyTitle (DashboardV2Page.tsx), which is
    what turns a tab title into its URL segment.

    Lives in this module rather than nav_tab_service so both slug namespaces
    share one implementation: nav-tab slugs (stored, via _resolve_nav_slug)
    and root-tab slugs (derived at render time, enforced here by
    _root_tab_with_conflicting_slug). Import direction is one-way —
    nav_tab_service imports from here, never the reverse — so this is the
    only place the two can share it.
    """
    value = (title or "").lower()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    return value[:80]


def _validate_title(title: str | None) -> str | None:
    if title is None:
        return None
    clean_title = title.strip()
    if not clean_title:
        raise ValueError("Title cannot be empty")
    if len(clean_title) > MAX_TITLE_LENGTH:
        raise ValueError(f"Title cannot be longer than {MAX_TITLE_LENGTH} characters")
    return clean_title


def _validate_document_id_value(document_id: str | None, field_name: str = "documentId") -> str | None:
    if document_id is None:
        return None
    clean_document_id = document_id.strip()
    if not clean_document_id:
        raise ValueError(f"{field_name} cannot be empty")
    if len(clean_document_id) > MAX_DOCUMENT_ID_LENGTH:
        raise ValueError(f"{field_name} cannot be longer than {MAX_DOCUMENT_ID_LENGTH} characters")
    return clean_document_id


def _validate_locked_by(value: str | None) -> str | None:
    if value is None:
        return None
    clean_value = value.strip()
    if len(clean_value) > MAX_LOCKED_BY_LENGTH:
        raise ValueError(f"locked_by cannot be longer than {MAX_LOCKED_BY_LENGTH} characters")
    return clean_value


def _validate_order(order: int | None) -> int | None:
    if order is None:
        return None
    if order < MIN_ORDER_VALUE or order > MAX_ORDER_VALUE:
        raise ValueError(f"Order must be between {MIN_ORDER_VALUE} and {MAX_ORDER_VALUE}")
    return order


def _generate_id() -> str:
    return str(uuid4())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _access_control_or_default(access_control: dict[str, Any] | None) -> dict[str, Any]:
    return access_control if access_control else DEFAULT_ACCESS_CONTROL


def _create_gridstack_component(
    db: Session, gridstack: GridstackV2, access_control: dict[str, Any] | None = None
) -> ComponentV2:
    """Create the ComponentV2 row that represents `gridstack` itself (see
    current_grid_id on the model). Called once per gridstack, right after it
    is flushed (so `gridstack.id` exists). Its type/props mirror the
    gridstack's own settings.sgs at creation time, matching the sync in
    update_tab_content_v2 — this lets a first tab-variant that inherits its
    root's settings (including an already-set sgs config) start out already
    flagged as a super gridstack, with no special-casing needed here.

    `access_control` is only ever passed for a sub-tab gridstack (a root or
    variant's own AC lives on its TabV2 row, never here) — see
    _safe_access_control_for_gridstack, the sub-tab branch of create_tab_v2,
    and the non-root branch of update_tab_by_document_id_v2."""
    sgs = (gridstack.settings or {}).get("sgs")
    component = ComponentV2(
        link=_generate_id(),
        title=None,
        description=None,
        type=SUPER_GRIDSTACK_WIDGET_TYPE if sgs else GRIDSTACK_WIDGET_TYPE,
        x=None,
        y=None,
        width=None,
        height=None,
        props={"sgs": sgs} if sgs else None,
        access_control=access_control,
        current_grid_id=gridstack.id,
        gridstack_id=gridstack.id,
        page_content_id=None,
        super_blocknote_id=None,
    )
    db.add(component)
    db.flush()
    return component


# ---------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------

def get_gridstack_by_document_id(db: Session, document_id: str) -> GridstackV2 | None:
    document_id = _validate_document_id_value(document_id, "document_id")
    return db.query(GridstackV2).filter(GridstackV2.document_id == document_id).first()


def _is_root(gridstack: GridstackV2) -> bool:
    return gridstack.parent_id is None


def _get_root_tab(db: Session, gridstack: GridstackV2) -> TabV2 | None:
    return db.query(TabV2).filter(TabV2.id == gridstack.parent_tab_id).first()


# Sentinel for the optional `root_tab` param on `_safe_locked_pair` /
# `_safe_access_control_for_gridstack` below — distinguishes "caller hasn't
# fetched the root tab, look it up yourself" from "caller already fetched it
# and it's genuinely None" (no matching TabV2 row), so a `None` result never
# triggers a wasted duplicate re-query. Profiling against live Neon found
# `_format_tab_summary`/`get_tab_workspace_v2` each fetching the SAME TabV2
# row 3 times (once directly, once via each of these two helpers) — at
# ~165ms/round-trip to the DB, that's ~330ms of pure duplicate work on every
# root-tab summary/workspace call. Callers that already have the tab (both
# functions above fetch it once up front when the gridstack is root) pass it
# through instead of letting these helpers re-fetch it.
_ROOT_TAB_NOT_FETCHED: Any = object()


def _get_gridstack_component(db: Session, gridstack_id: int) -> ComponentV2 | None:
    """The ComponentV2 row that represents `gridstack_id` itself (see
    current_grid_id on the model) — the sub-tab access_control migration's
    new home. May be None for a gridstack created before the current_grid_id
    backfill (migrate_subtab_access_control_to_components.py) has run for it."""
    return db.query(ComponentV2).filter(ComponentV2.current_grid_id == gridstack_id).first()


def _has_children(db: Session, gridstack_id: int) -> bool:
    return (
        db.query(GridstackV2.id)
        .filter(GridstackV2.parent_id == gridstack_id)
        .first()
        is not None
    )


def _has_content(db: Session, gridstack_id: int) -> bool:
    return (
        db.query(ComponentV2.id)
        .filter(
            ComponentV2.gridstack_id == gridstack_id,
            ComponentV2.type.notin_(GRIDSTACK_REPRESENTATION_TYPES),
        )
        .first()
        is not None
    )


def _safe_access_control_for_gridstack(
    db: Session,
    gridstack: GridstackV2,
    root_tab: TabV2 | None = _ROOT_TAB_NOT_FETCHED,
) -> dict[str, Any]:
    if _is_root(gridstack):
        tab = root_tab if root_tab is not _ROOT_TAB_NOT_FETCHED else _get_root_tab(db, gridstack)
        return _access_control_or_default(tab.access_control if tab else None)

    # A sub-tab's access_control lives on its own representation component
    # (current_grid_id) — see migrate_subtab_access_control_to_components.py,
    # which relocated it off the legacy gridstacks.settings["access_control"]
    # location (now unused; that migration + this read/write path have both
    # been deployed and verified).
    component = _get_gridstack_component(db, gridstack.id)
    return _access_control_or_default(component.access_control if component else None)


def _safe_locked_pair(
    db: Session,
    gridstack: GridstackV2,
    root_tab: TabV2 | None = _ROOT_TAB_NOT_FETCHED,
) -> tuple[bool, str]:
    if _is_root(gridstack):
        tab = root_tab if root_tab is not _ROOT_TAB_NOT_FETCHED else _get_root_tab(db, gridstack)
        if tab is None:
            return False, ""
        return bool(tab.locked), (tab.locked_by or "")
    # Nested gridstacks have no lock columns — lock granularity is
    # whole-tab-only in this schema (Phase 2 decision).
    return False, ""


def _has_variants(db: Session, tab_id: int) -> bool:
    return db.query(TabV2.id).filter(TabV2.parent_tab_id == tab_id).first() is not None


def _format_tab_summary(db: Session, gridstack: GridstackV2) -> dict[str, Any]:
    is_root = _is_root(gridstack)
    # Fetched once here (only when root — non-root gridstacks never had this
    # query) and threaded into both helpers below instead of each re-fetching
    # the identical TabV2 row — see `_ROOT_TAB_NOT_FETCHED`'s doc comment.
    root_tab = _get_root_tab(db, gridstack) if is_root else None

    locked, locked_by = _safe_locked_pair(db, gridstack, root_tab)
    node_id = gridstack.parent_tab_id if is_root else gridstack.id
    title = None
    order = gridstack.position if gridstack.position is not None else 0
    has_variants = False
    nav_tab_document_id = None
    nav_tab_title = None

    if is_root:
        title = root_tab.title if root_tab else gridstack.name
        order = root_tab.order if root_tab and root_tab.order is not None else order
        has_variants = _has_variants(db, root_tab.id) if root_tab is not None else False
        # Lets the mirror picker group roots by dashboard, and disambiguates
        # two roots that share a title across different nav tabs.
        if root_tab is not None and root_tab.nav_tab_id is not None:
            nav_tab = db.query(NavTabV2).filter(NavTabV2.id == root_tab.nav_tab_id).first()
            if nav_tab is not None:
                nav_tab_document_id = nav_tab.document_id
                nav_tab_title = nav_tab.title
    else:
        title = gridstack.name

    return {
        "id": node_id,
        "documentId": gridstack.document_id,
        "title": title,
        "order": order,
        "locked": locked,
        "locked_by": locked_by,
        "has_children": _has_children(db, gridstack.id),
        "has_content": _has_content(db, gridstack.id),
        "has_variants": has_variants,
        "access_control": _safe_access_control_for_gridstack(db, gridstack, root_tab),
        "navTabDocumentId": nav_tab_document_id,
        "navTabTitle": nav_tab_title,
        "apiVersion": "v2",
    }


# ---------------------------------------------------------
# Content serialization: ComponentV2 rows <-> GridCanvasContent
# ---------------------------------------------------------

def _raw_component_data(
    db: Session,
    component: ComponentV2,
    preloaded_page_content: dict[int, Any] | None = None,
) -> dict[str, Any]:
    """The component's `data` blob EXACTLY as stored, secrets included.

    Server-internal only. Every client-facing path must go through
    `_resolve_component_data` below, which strips what viewers must not see.

    Callers that legitimately need the raw blob are the ones that read it in
    order to write it back (`update_airtable_component_config`,
    `_apply_airtable_protection`'s stored-value read) or that need the secret
    itself (`get_airtable_component_config`, `get_airtable_pat_for_component`).
    Reading the sanitised view in any of those would silently DELETE the PAT
    on the next write.

    Every component type's `data` lives in `page_content` via
    `page_content_id` (not `props` — see ComponentV2's docstring). `block_note`
    wraps the raw stored `Block[]` as `{"content": [...]}`; every other type's
    `page_content.content` already IS its `data` dict, returned as-is.

    Never called on a mirror component itself (only ever on mirror TARGETS,
    which by construction are never mirrors — see `_serialize_component`'s
    cycle guard) — falls back to `{}` rather than raising, since this is
    defense-in-depth for an invariant already enforced elsewhere, not a case
    that should ever surface a 500.

    Rows that predate this change (data still sitting in `props`, no
    `page_content_id` yet) fall back to reading `props` — purely transitional;
    any subsequent save through `update_tab_content_v2`/`_write_component_data`
    moves that row onto `page_content` for good.

    `preloaded_page_content` (keyed by `PageContentV2.id`) is an optional
    batch-fetched map — see `_serialize_gridstack_content`, which fetches
    every top-level component's page_content in ONE round trip instead of
    this function querying it individually per component (confirmed via
    profiling against live Neon to cost ~165ms/component). A miss (the id
    isn't in the map — e.g. a mirror's TARGET, which may live outside the
    batch that was built for the mirror's own gridstack) falls back to the
    original single-row query, so this is purely an optimization, never a
    correctness requirement — every caller that doesn't pass it (the single-
    component call sites: mirror-target/by-link resolution, Airtable config
    read/write) behaves exactly as before.
    """
    if component.type == MIRROR_WIDGET_TYPE:
        return {}

    if component.page_content_id is not None:
        if preloaded_page_content is not None and component.page_content_id in preloaded_page_content:
            stored = preloaded_page_content[component.page_content_id]
        else:
            page_content = (
                db.query(PageContentV2)
                .filter(PageContentV2.id == component.page_content_id)
                .first()
            )
            stored = page_content.content if page_content else None
        if component.type == "block_note":
            return {"content": stored if stored is not None else []}
        return stored if isinstance(stored, dict) else {}

    # Legacy fallback: no page_content_id yet — this row's data (if any) is
    # still sitting in `props` from before the universal page_content move.
    props = component.props or {}
    legacy_data = {k: v for k, v in props.items() if k not in ("min_w", "min_h", "locked", "locked_by", "order")}
    if component.type == "block_note":
        return {"content": legacy_data.get("content", [])}
    return legacy_data


def _resolve_component_data(
    db: Session,
    component: ComponentV2,
    preloaded_page_content: dict[int, Any] | None = None,
) -> dict[str, Any]:
    """The CLIENT-FACING `data` blob for one component — shared by the normal
    serializer path, a mirror's resolution of its target, and the by-link
    lookup endpoint, so all three agree on what a client is allowed to see.

    Identical to `_raw_component_data` except that an `airtable` widget's
    stored PAT is removed and replaced with the boolean `hasPat`. Doing it
    here rather than at each endpoint covers the tab serializer, mirror-target
    resolution and the by-link lookup in one place — including
    `GET /v2/tabs/{id}/content`, which has no auth dependency and would
    otherwise hand a workspace-scoped Airtable token to anyone who asked.

    `hasPat` is computed on every read and never persisted — see
    `_write_component_data`, which drops it again on the way back in.

    `preloaded_page_content` — see `_raw_component_data`'s doc comment; passed
    straight through.
    """
    data = _raw_component_data(db, component, preloaded_page_content)

    if component.type == AIRTABLE_WIDGET_TYPE:
        pat = data.get("pat")
        data = {k: v for k, v in data.items() if k not in AIRTABLE_COMPUTED_DATA_FIELDS}
        data.pop("pat", None)
        data["hasPat"] = bool(isinstance(pat, str) and pat.strip())

    return data


def _write_component_data(db: Session, component: ComponentV2, data: dict[str, Any] | None) -> None:
    """Persists `data` into `page_content`, creating a new `PageContentV2` row
    on first write if `page_content_id` is still unset. Mirrors
    `_resolve_component_data`'s wrap/unwrap convention for `block_note`. Never
    called for a `mirror` component (its only persisted data is `target_link`,
    which lives in `props`, not `page_content` — see `update_tab_content_v2`).

    Server-derived keys (AIRTABLE_COMPUTED_DATA_FIELDS) are dropped here
    rather than at each caller: BOTH write paths — the canvas save and the
    config endpoint — funnel through this function, so stripping once here
    makes it impossible for a caller to forget."""
    stored = (data or {}).get("content", []) if component.type == "block_note" else (data or {})

    if component.type == AIRTABLE_WIDGET_TYPE and isinstance(stored, dict):
        stored = {k: v for k, v in stored.items() if k not in AIRTABLE_COMPUTED_DATA_FIELDS}

    if component.page_content_id is not None:
        page_content = (
            db.query(PageContentV2)
            .filter(PageContentV2.id == component.page_content_id)
            .first()
        )
    else:
        page_content = None

    if page_content is not None:
        page_content.content = stored
    else:
        page_content = PageContentV2(content=stored)
        db.add(page_content)
        db.flush()
        component.page_content_id = page_content.id


def _apply_airtable_protection(
    stored: dict[str, Any] | None, incoming: dict[str, Any] | None
) -> dict[str, Any]:
    """Return `incoming` with every AIRTABLE_PROTECTED_DATA_FIELDS key forced
    back to its stored value, dropping the key entirely when nothing is
    stored yet.

    Called on the canvas-save path only, which is unauthenticated — see
    AIRTABLE_PROTECTED_DATA_FIELDS for why. A brand-new airtable widget
    therefore lands with none of its protected fields set; the frontend
    follows the content save with a `PUT /data/airtable/component/{link}/config`
    call (which IS authenticated) to populate them.
    """
    incoming = incoming if isinstance(incoming, dict) else {}
    stored = stored if isinstance(stored, dict) else {}

    protected = {
        key: stored[key]
        for key in AIRTABLE_PROTECTED_DATA_FIELDS
        if key in stored
    }
    unprotected = {
        key: value
        for key, value in incoming.items()
        if key not in AIRTABLE_PROTECTED_DATA_FIELDS
    }
    return {**unprotected, **protected}


def _pat_hint(pat: Any) -> str | None:
    """A non-secret identifier for a stored PAT, safe to show an editor.

    Airtable tokens are `pat<id>.<secret>`; the part before the dot is the
    token id Airtable's own token-management UI displays, so echoing it lets
    an admin match a widget against a token in that UI (which is the only
    way to answer "which widgets use the token I am about to revoke?" once
    the PAT itself is write-only).

    Any token not matching that shape is reduced to a short digest instead —
    never echo part of a secret whose structure we do not recognise.
    """
    if not isinstance(pat, str) or not pat.strip():
        return None
    token = pat.strip()
    prefix, separator, _secret = token.partition(".")
    if separator and prefix.startswith("pat") and len(prefix) > 3:
        return prefix
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _sbn_node_info(db: Session, component: ComponentV2) -> dict[str, Any] | None:
    """Derived descriptor for a component that is a Super Block Note SUB-TAB
    (an ordinary `ComponentV2` carrying `super_blocknote_id` — see
    super_blocknote_service.py) rather than a top-level canvas widget.
    `None` for everything else, which is every component that could be a
    mirror target before sub-tabs became pickable.

    Exists because both kinds serialize under the same `type`
    (`"block_note"`), so a client cannot otherwise tell them apart — and a
    mirror must, since a sub-tab with its own nested sub-tabs has to render
    as a Super Block Note scoped to that node (sidebar + content pane); a
    bare text pane would silently hide those children.

    Never persisted — recomputed on every read, exactly like the
    `mirroredType`/`mirroredData` it sits beside (a mirror's only stored
    state is `props.target_link`; see `update_tab_content_v2`). Costs one
    extra existence query, and only for a target that IS an SBN sub-tab.

    Deliberately duplicates super_blocknote_service's `_has_sbn_children`
    rather than importing it: that module imports from this one, so the
    dependency only runs in that direction.
    """
    if component.super_blocknote_id is None:
        return None
    has_children = (
        db.query(ComponentV2.id)
        .filter(ComponentV2.super_blocknote_id == component.id)
        .first()
        is not None
    )
    return {"hasChildren": has_children}


def _serialize_component(
    db: Session,
    component: ComponentV2,
    preloaded_page_content: dict[int, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Returns (layout_entry, widget_entry) for one component, matching
    gridContent.ts's GridWidgetLayout / GridWidgetEntry shapes.

    `preloaded_page_content` — see `_raw_component_data`'s doc comment; passed
    straight through to whichever `_resolve_component_data` call applies (the
    component itself, or a mirror's target)."""

    props = component.props or {}

    layout_entry: dict[str, Any] = {
        "id": str(component.id),
        "x": component.x if component.x is not None else 0,
        "y": component.y if component.y is not None else 0,
        "w": component.width if component.width is not None else 6,
        "h": component.height if component.height is not None else 6,
        "type": component.type,
    }
    if props.get("min_w") is not None:
        layout_entry["minW"] = props["min_w"]
    if props.get("min_h") is not None:
        layout_entry["minH"] = props["min_h"]

    if component.type == MIRROR_WIDGET_TYPE:
        target_link = props.get("target_link")
        target = (
            db.query(ComponentV2).filter(ComponentV2.link == target_link).first()
            if target_link
            else None
        )

        if target is None or target.type == MIRROR_WIDGET_TYPE:
            # Dangling target, or defensive cycle guard (a mirror should
            # never be able to target another mirror — the picker already
            # excludes mirrors as pickable targets; this is defense in depth).
            widget_entry = {
                "type": MIRROR_WIDGET_TYPE,
                "link": component.link,
                "data": {
                    "targetLink": target_link,
                    "mirroredType": None,
                    "mirroredData": None,
                    "mirroredSbn": None,
                },
            }
        else:
            widget_entry = {
                "type": MIRROR_WIDGET_TYPE,
                "link": component.link,
                "data": {
                    "targetLink": target_link,
                    "mirroredType": target.type,
                    "mirroredData": _resolve_component_data(db, target, preloaded_page_content),
                    # Non-null only when the target is a Super Block Note
                    # sub-tab — see `_sbn_node_info`.
                    "mirroredSbn": _sbn_node_info(db, target),
                },
            }
            # Key simplification: substitute the TARGET's own access_control
            # so filter_widget_content_for_user (unchanged) enforces exactly
            # the same visibility a direct view of the target would have.
            if target.access_control:
                widget_entry["access_control"] = target.access_control

        # A mirror has its own independent title/description (labeling the
        # mirror INSTANCE itself, e.g. "Announcements (mirrored)") — never
        # the target's, which stays fully separate metadata.
        widget_entry["title"] = component.title
        widget_entry["description"] = component.description

        return layout_entry, widget_entry

    data = _resolve_component_data(db, component, preloaded_page_content)

    # Every widget entry carries its own stable `link` (not just mirrors) —
    # this is how the mirror target picker learns what to reference; the
    # widget's `layout`/`widgets` KEY (str(component.id)) is only a local,
    # same-canvas identity and must never be used for cross-tab addressing.
    widget_entry = {
        "type": component.type,
        "link": component.link,
        "title": component.title,
        "description": component.description,
        "data": data,
    }
    if component.access_control:
        widget_entry["access_control"] = component.access_control

    return layout_entry, widget_entry


def _serialize_gridstack_content(db: Session, gridstack: GridstackV2) -> dict[str, Any]:
    # Excludes Super Block Note descendants (super_blocknote_id set) even
    # though they share this same gridstack_id (a NOT NULL column they must
    # still populate) — they're never real top-level canvas widgets, only
    # reachable through their own SBN tree (see super_blocknote_service.py).
    # Also excludes the gridstack's own self-representation component (see
    # current_grid_id on ComponentV2) — never a real widget either.
    components = (
        db.query(ComponentV2)
        .filter(
            ComponentV2.gridstack_id == gridstack.id,
            ComponentV2.super_blocknote_id.is_(None),
            ComponentV2.type.notin_(GRIDSTACK_REPRESENTATION_TYPES),
        )
        .order_by(ComponentV2.id.asc())
        .all()
    )

    # Batch-fetch every top-level component's page_content in ONE round trip
    # instead of `_raw_component_data` querying it individually per component
    # — confirmed via profiling against live Neon: each such query costs
    # ~165ms regardless of complexity (remote-DB round-trip latency, not
    # query cost), so this was the dominant per-widget cost on any
    # multi-widget canvas. A mirror's target may fall outside this batch (a
    # different gridstack) — `_raw_component_data` falls back to its own
    # single query in that case, so this is a pure optimization, not a
    # correctness dependency.
    page_content_ids = [c.page_content_id for c in components if c.page_content_id is not None]
    preloaded_page_content: dict[int, Any] = {}
    if page_content_ids:
        preloaded_page_content = {
            row.id: row.content
            for row in db.query(PageContentV2).filter(PageContentV2.id.in_(page_content_ids)).all()
        }

    layout: list[dict[str, Any]] = []
    widgets: dict[str, Any] = {}

    for component in components:
        layout_entry, widget_entry = _serialize_component(db, component, preloaded_page_content)
        layout.append(layout_entry)
        widgets[str(component.id)] = widget_entry

    content: dict[str, Any] = {
        "schemaVersion": 2,
        "layout": layout,
        "widgets": widgets,
    }

    settings = gridstack.settings or {}
    if settings.get("sgs"):
        content["sgs"] = settings["sgs"]

    return content


def _format_page_content(db: Session, gridstack: GridstackV2) -> dict[str, Any]:
    return {
        "documentId": gridstack.document_id,
        "content": _serialize_gridstack_content(db, gridstack),
    }


def get_component_by_link_v2(db: Session, link: str) -> dict[str, Any] | None:
    """Resolves a component by its stable `link` — backs the mirror picker's
    "paste a link" flow (as opposed to browsing tabs/children/workspace).
    Returns None for an unknown link, or one that points at a mirror itself
    (cycle prevention — matches the picker's browse mode, which never lists
    mirrors as pickable targets)."""
    link = (link or "").strip()
    if not link:
        return None
    component = db.query(ComponentV2).filter(ComponentV2.link == link).first()
    if component is None or component.type == MIRROR_WIDGET_TYPE:
        return None
    return {
        "type": component.type,
        "title": component.title,
        "data": _resolve_component_data(db, component),
        # Lets a pasted link to a Super Block Note sub-tab render the same
        # way a browsed one does, immediately, before the first save round
        # trip re-resolves it — see `_sbn_node_info`.
        "sbn": _sbn_node_info(db, component),
    }


# ---------------------------------------------------------
# Airtable widget configuration (protected fields)
#
# The only supported way to write AIRTABLE_PROTECTED_DATA_FIELDS or an
# airtable widget's access_control. Both endpoints backing these functions
# require authentication, unlike the canvas save.
# ---------------------------------------------------------

_UNSET: Any = object()


def _airtable_component_by_link(db: Session, link: str) -> ComponentV2 | None:
    link = (link or "").strip()
    if not link:
        return None
    component = db.query(ComponentV2).filter(ComponentV2.link == link).first()
    if component is None or component.type != AIRTABLE_WIDGET_TYPE:
        return None
    return component


def get_airtable_component_config(db: Session, link: str) -> dict[str, Any] | None:
    """Non-secret view of one airtable widget's stored configuration.

    Never returns the PAT itself — `hasPat` says whether one is stored and
    `patHint` identifies WHICH token it is (see _pat_hint). Returns None for
    an unknown link or a component that is not an airtable widget.
    """
    component = _airtable_component_by_link(db, link)
    if component is None:
        return None

    # RAW: this function's whole job is to report ON the secret (hasPat /
    # patHint), which the sanitised view has already removed.
    data = _raw_component_data(db, component)
    pat = data.get("pat")

    return {
        "link": component.link,
        "sourceUrl": data.get("sourceUrl") or "",
        "personalizeEnabled": bool(data.get("personalizeEnabled")),
        "personalizeColumn": data.get("personalizeColumn") or None,
        "hasPat": bool(isinstance(pat, str) and pat.strip()),
        "patHint": _pat_hint(pat),
        "patUpdatedAt": data.get("patUpdatedAt"),
        "access_control": component.access_control,
    }


def get_airtable_component_data(db: Session, link: str) -> dict[str, Any] | None:
    """The airtable widget's client-safe data blob (PAT already stripped).

    Used by the row-fetch endpoint to read the fields that are NOT part of
    the protected config — `selectedColumns`, `filters` — which the canvas
    save legitimately owns. Returns None for an unknown or non-airtable link.
    """
    component = _airtable_component_by_link(db, link)
    if component is None:
        return None
    return _resolve_component_data(db, component)


def get_airtable_pat_for_component(db: Session, link: str) -> str | None:
    """The stored PAT for one airtable widget, in clear.

    SERVER-INTERNAL. The only legitimate consumers are the endpoints that
    fetch rows on the caller's behalf — the token must never leave the
    server. Deliberately does NOT go through `_resolve_component_data`, which
    strips exactly this value on every client-facing path.

    Returns None for an unknown link, a non-airtable component, or a widget
    with no token configured.
    """
    component = _airtable_component_by_link(db, link)
    if component is None:
        return None
    pat = _raw_component_data(db, component).get("pat")
    return pat.strip() if isinstance(pat, str) and pat.strip() else None


def update_airtable_component_config(
    db: Session,
    link: str,
    *,
    source_url: Any = _UNSET,
    pat: Any = _UNSET,
    personalize_enabled: Any = _UNSET,
    personalize_column: Any = _UNSET,
    access_control: Any = _UNSET,
) -> dict[str, Any] | None:
    """Write one or more protected fields. Arguments left at `_UNSET` are
    untouched, so a caller can update a single field without resending the
    rest (and, critically, without having to resend a PAT it cannot read).

    `pat` has three-way semantics, because "leave it alone" and "clear it"
    must be distinguishable in a write-only field:
      * omitted, or an empty/whitespace string → preserve the stored token
        (what the UI sends when the admin edited other fields but did not
        type a new token);
      * a non-empty string → replace, refreshing `patUpdatedAt`;
      * an explicit ``None`` → clear the token and its timestamp.

    Returns the updated config (same shape as get_airtable_component_config),
    or None if the link is unknown / not an airtable widget.
    """
    component = _airtable_component_by_link(db, link)
    if component is None:
        return None

    try:
        # RAW: this reads the blob in order to write it back. Reading the
        # sanitised view would drop the stored `pat` from `data` and so
        # delete the token whenever any OTHER field is updated.
        data = dict(_raw_component_data(db, component))

        if source_url is not _UNSET:
            data["sourceUrl"] = (source_url or "").strip()

        if personalize_enabled is not _UNSET:
            data["personalizeEnabled"] = bool(personalize_enabled)

        if personalize_column is not _UNSET:
            data["personalizeColumn"] = (personalize_column or "").strip() or None

        if pat is not _UNSET:
            if pat is None:
                data.pop("pat", None)
                data.pop("patUpdatedAt", None)
            elif str(pat).strip():
                new_pat = str(pat).strip()
                # Only stamp when the token actually CHANGES. The client may
                # resend the same value (it still round-trips in the widget
                # blob until the read-side strip lands), and bumping the
                # timestamp on every save would turn "when was this token last
                # rotated" into "when was this widget last saved" — destroying
                # the one signal that makes a write-only PAT auditable.
                if new_pat != data.get("pat"):
                    data["pat"] = new_pat
                    data["patUpdatedAt"] = _utc_now().isoformat()

        if access_control is not _UNSET:
            component.access_control = access_control

        _write_component_data(db, component, data)
        db.commit()

        return get_airtable_component_config(db, link)

    except Exception:
        db.rollback()
        raise


def _get_gridstack_ancestor_chain(db: Session, gridstack: GridstackV2) -> list[str]:
    """Root-first ordered `document_id`s from the root's immediate child
    down to (and including) `gridstack` itself — i.e. the sequence of SGS/
    nav-tree sub-tab clicks needed to reach `gridstack`'s own canvas. Empty
    if `gridstack` is already the root (`_is_root`)."""
    chain: list[str] = []
    current: GridstackV2 | None = gridstack
    while current is not None and current.parent_id is not None:
        chain.append(current.document_id)
        current = db.query(GridstackV2).filter(GridstackV2.id == current.parent_id).first()
    chain.reverse()
    return chain


def _get_sbn_ancestor_chain(db: Session, component: ComponentV2) -> list[str]:
    """Root-first ordered component `link`s from the top-level Super Block
    Note widget down to (and including) `component` itself. Empty if
    `component` isn't an SBN descendant (`super_blocknote_id is None`) —
    i.e. it's already a top-level canvas widget, addressed directly via its
    own gridstack location instead."""
    if component.super_blocknote_id is None:
        return []
    chain: list[str] = [component.link]
    current = component
    while current.super_blocknote_id is not None:
        current = db.query(ComponentV2).filter(ComponentV2.id == current.super_blocknote_id).first()
        if current is None:
            break
        chain.append(current.link)
    chain.reverse()
    return chain


def get_component_by_link_for_access_check_v2(db: Session, link: str) -> ComponentV2 | None:
    """Returns the raw component row for `link` — None for an unknown link,
    or one that points at a mirror (same cycle-prevention convention as
    `resolve_component_location_v2`, which this is meant to be called
    alongside: a mirror is never a navigable "original", so it's never a
    visibility-checkable target either).

    Lets a caller (the `.../location` router) run its own
    `access_control_service.can_view` check against the component's own
    `access_control` (falling back to `resolved_parent_ac` when unset)
    BEFORE calling `resolve_component_location_v2` — keeping the
    permission decision in the router, matching this codebase's existing
    convention (see `move_tab_to_nav_tab`'s own `can_edit` checks) rather
    than teaching the resolver itself about the caller's identity."""
    link = (link or "").strip()
    if not link:
        return None
    component = db.query(ComponentV2).filter(ComponentV2.link == link).first()
    if component is None or component.type == MIRROR_WIDGET_TYPE:
        return None
    return component


def resolve_component_location_v2(db: Session, link: str) -> dict[str, Any] | None:
    """Resolves a component's `link` into the full path needed to navigate
    to and locate it in the UI — backs the mirror widget's "jump to
    original" affordance. Returns None for an unknown link, or one that
    points at a mirror itself (same cycle-prevention convention as
    `get_component_by_link_v2` — a mirror is never a navigable "original").

    Callers reachable from outside the app (the `.../location` router) must
    run their own visibility check first, via
    `get_component_by_link_for_access_check_v2` — this function performs
    none itself, matching `get_component_by_link_v2`'s existing contract."""
    link = (link or "").strip()
    if not link:
        return None
    component = db.query(ComponentV2).filter(ComponentV2.link == link).first()
    if component is None or component.type == MIRROR_WIDGET_TYPE:
        return None

    sbn_path = _get_sbn_ancestor_chain(db, component)
    # An SBN descendant's own gridstack_id is its SBN root's — see
    # ComponentV2's docstring — so the gridstack/tab lookup below must
    # always resolve against the top-level widget, not the descendant.
    top_level_component = component
    if sbn_path:
        top_level_component = db.query(ComponentV2).filter(ComponentV2.link == sbn_path[0]).first()
        if top_level_component is None:
            return None

    gridstack = (
        db.query(GridstackV2).filter(GridstackV2.id == top_level_component.gridstack_id).first()
    )
    if gridstack is None:
        return None

    root_tab = _get_root_tab(db, gridstack)
    if root_tab is None:
        return None

    # A tab VARIANT is itself a TabV2 with parent_tab_id set (see
    # create_tab_variant_v2), and it owns its own root gridstack — so for a
    # component living inside a variant, `_get_root_tab` returns the VARIANT
    # row, not the tab shown in the tab bar. Report the two separately: the
    # real root to switch to, and the variant to select once there.
    #
    # Collapsing them (returning only the variant) is what made
    # cross-variant navigation land on the wrong canvas: the frontend looks
    # `rootTabDocumentId` up in its root-tab list, which excludes variants,
    # finds nothing, gives up, and leaves the user on whichever variant is
    # active by default — the first one. That silently *looks* correct
    # whenever the target happens to live in the first variant.
    variant_document_id = None
    if root_tab.parent_tab_id is not None:
        variant_document_id = root_tab.document_id
        parent_tab = db.query(TabV2).filter(TabV2.id == root_tab.parent_tab_id).first()
        if parent_tab is None:
            return None
        root_tab = parent_tab

    nav_tab_document_id = None
    if root_tab.nav_tab_id is not None:
        nav_tab = db.query(NavTabV2).filter(NavTabV2.id == root_tab.nav_tab_id).first()
        nav_tab_document_id = nav_tab.document_id if nav_tab is not None else None

    return {
        "rootTabDocumentId": root_tab.document_id,
        "variantDocumentId": variant_document_id,
        "navTabDocumentId": nav_tab_document_id,
        "gridstackPath": _get_gridstack_ancestor_chain(db, gridstack),
        "sbnPath": sbn_path,
        "componentLink": component.link,
    }


# ---------------------------------------------------------
# Read API
# ---------------------------------------------------------

def get_root_tabs_v2(db: Session, nav_tab_id: int | None = None) -> list[dict[str, Any]]:
    # A root gridstack whose owning TabV2 itself has parent_tab_id set is a
    # tab variant, not a top-level tab — it must only ever surface via
    # get_tab_variants_v2, never duplicated into the main tab bar.
    #
    # `nav_tab_id` defaults to None, which preserves the historical unscoped
    # behaviour — every root across every nav tab. GET /v2/tabs/root and the
    # mirror picker rely on that default; only the scoped
    # GET /v2/nav-tabs/{document_id}/tabs endpoint passes a value.
    query = (
        db.query(GridstackV2)
        .join(TabV2, GridstackV2.parent_tab_id == TabV2.id)
        .filter(GridstackV2.parent_id.is_(None), TabV2.parent_tab_id.is_(None))
    )
    if nav_tab_id is not None:
        query = query.filter(TabV2.nav_tab_id == nav_tab_id)
    root_gridstacks = query.all()
    summaries = [_format_tab_summary(db, g) for g in root_gridstacks]
    summaries.sort(key=lambda s: (s["order"], s["id"] or 0))
    return summaries


def get_tab_variants_v2(db: Session, parent_document_id: str) -> list[dict[str, Any]] | None:
    parent_gridstack = get_gridstack_by_document_id(db, parent_document_id)
    if parent_gridstack is None or not _is_root(parent_gridstack):
        return None
    parent_tab = _get_root_tab(db, parent_gridstack)
    if parent_tab is None:
        return None

    variant_tabs = db.query(TabV2).filter(TabV2.parent_tab_id == parent_tab.id).all()
    summaries: list[dict[str, Any]] = []
    for variant_tab in variant_tabs:
        variant_gridstack = (
            db.query(GridstackV2)
            .filter(GridstackV2.parent_tab_id == variant_tab.id, GridstackV2.parent_id.is_(None))
            .first()
        )
        if variant_gridstack is None:
            continue
        summaries.append(_format_tab_summary(db, variant_gridstack))

    summaries.sort(key=lambda s: (s["order"], s["id"] or 0))
    return summaries


def _migrate_root_content_to_variant(
    db: Session,
    root_gridstack: GridstackV2,
    variant_gridstack: GridstackV2,
    variant_tab: TabV2,
) -> None:
    """Move a root tab's entire canvas into its first variant's gridstack.

    Once any variant exists, the root's own canvas is no longer rendered (the
    variant shown in its place is — see DashboardV2Page), so on the 0->1
    transition the root's content must move into this first variant or it's
    stranded (reachable only by deleting the variant). Mirrors the same
    "first child adopts the content, container is left empty" pattern already
    used by SGS's own 0->1 sub-tab migration and the SBN root-content
    transplant.

    Two things carry the content, and they're structural, not a serialized
    blob — which is exactly why the frontend `updateTabContentV2` path can't
    do this (it only diffs top-level widgets within one gridstack):
      1. Direct child gridstacks (the root's SGS sub-tab subtree) are
         re-parented onto the variant's gridstack. Their descendants keep
         their own parent_id chain but all adopt the variant tab as their
         owning tab (parent_tab_id) — matching move_tab_by_document_id_v2's
         cascade on an ordinary move.
      2. Top-level components sitting directly on the root canvas are moved
         across. Super Block Note descendants (super_blocknote_id set) aren't
         top-level and are skipped here, but cascade with their SBN root via
         _cascade_gridstack_id_to_sbn_descendants (same rule as the by-link
         re-parenting branch in update_tab_content_v2).
    The root gridstack is left empty — a pure container from then on.
    """
    child_gridstacks = (
        db.query(GridstackV2).filter(GridstackV2.parent_id == root_gridstack.id).all()
    )
    for child in child_gridstacks:
        child.parent_id = variant_gridstack.id
        child.parent_tab_id = variant_tab.id
        for descendant_id in get_descendant_gridstack_ids(db, child.id):
            descendant = db.query(GridstackV2).filter(GridstackV2.id == descendant_id).first()
            if descendant is not None:
                descendant.parent_tab_id = variant_tab.id

    top_level_components = (
        db.query(ComponentV2)
        .filter(
            ComponentV2.gridstack_id == root_gridstack.id,
            ComponentV2.super_blocknote_id.is_(None),
            ComponentV2.type.notin_(GRIDSTACK_REPRESENTATION_TYPES),
        )
        .all()
    )
    for component in top_level_components:
        component.gridstack_id = variant_gridstack.id
        _cascade_gridstack_id_to_sbn_descendants(db, component.id, variant_gridstack.id)


def create_tab_variant_v2(
    db: Session,
    parent_document_id: str,
    title: str,
    access_control: dict[str, Any] | None = None,
    order: int | None = None,
) -> dict[str, Any]:
    try:
        title = _validate_title(title)
        if not title:
            raise ValueError("Title is required")
        parent_document_id = _validate_document_id_value(parent_document_id, "parentDocumentId")
        order = _validate_order(order)

        parent_gridstack = get_gridstack_by_document_id(db, parent_document_id)
        if parent_gridstack is None or not _is_root(parent_gridstack):
            raise ValueError("Parent tab does not exist")

        parent_tab = _get_root_tab(db, parent_gridstack)
        if parent_tab is None:
            raise ValueError("Parent tab does not exist")

        if parent_tab.parent_tab_id is not None:
            raise ValueError("A tab variant cannot itself have tab variants")

        parent_ac = _access_control_or_default(parent_tab.access_control)
        effective_ac = access_control if access_control is not None else parent_ac

        existing = (
            db.query(TabV2)
            .filter(TabV2.parent_tab_id == parent_tab.id, TabV2.title == title)
            .first()
        )
        if existing is not None:
            raise ValueError("A tab variant with this title already exists under the same parent")

        # The first variant created under a root adopts the root's entire
        # existing canvas (see _migrate_root_content_to_variant); later
        # variants start blank. Determine this before the new variant row is
        # added below so the count reflects only pre-existing variants.
        is_first_variant = (
            db.query(TabV2).filter(TabV2.parent_tab_id == parent_tab.id).count() == 0
        )

        now = _utc_now()
        new_tab = TabV2(
            document_id=_generate_id(),
            title=title,
            order=order,
            # Materialized below via apply_write, after the content
            # migration (is_first_variant) so any re-parented descendants
            # are already in this variant's subtree when it propagates.
            access_control=None,
            locked=False,
            locked_by="",
            parent_tab_id=parent_tab.id,
            # A variant carries the same nav_tab_id as its parent tab — it is
            # not an independent placement (see NavTabV2's docstring).
            nav_tab_id=parent_tab.nav_tab_id,
            created_at=now,
            updated_at=now,
        )
        db.add(new_tab)
        db.flush()

        new_gridstack = GridstackV2(
            document_id=new_tab.document_id,
            name=title,
            # The first variant inherits the root gridstack's settings (e.g.
            # the SGS tab-bar position) since it adopts the root's whole
            # canvas; later variants start with a blank settings dict.
            settings=dict(parent_gridstack.settings or {}) if is_first_variant else {},
            position=order,
            parent_id=None,
            parent_tab_id=new_tab.id,
        )
        db.add(new_gridstack)
        db.flush()
        _create_gridstack_component(db, new_gridstack)

        if is_first_variant:
            _migrate_root_content_to_variant(db, parent_gridstack, new_gridstack, new_tab)

        apply_write(db, NodeRef("tab", new_tab.id), effective_ac)

        affected_component_ids = _component_ids_for_gridstack_tree(db, new_gridstack)
        db.commit()
        response = _format_tab_summary(db, new_gridstack)
        response["search_updates"] = [
            {"component_id": component_id, "action": "upsert"}
            for component_id in affected_component_ids
        ]
        return response

    except Exception:
        db.rollback()
        raise


def reorder_tab_variants_v2(
    db: Session,
    parent_document_id: str,
    ordered_document_ids: list[str],
) -> list[dict[str, Any]] | None:
    try:
        parent_gridstack = get_gridstack_by_document_id(db, parent_document_id)
        if parent_gridstack is None or not _is_root(parent_gridstack):
            return None
        parent_tab = _get_root_tab(db, parent_gridstack)
        if parent_tab is None:
            return None

        variants_by_document_id = {
            v.document_id: v
            for v in db.query(TabV2).filter(TabV2.parent_tab_id == parent_tab.id).all()
        }
        for doc_id in ordered_document_ids:
            if doc_id not in variants_by_document_id:
                raise ValueError(f"{doc_id} is not a tab variant of this tab")

        for index, doc_id in enumerate(ordered_document_ids):
            variant_tab = variants_by_document_id[doc_id]
            variant_tab.order = index
            variant_tab.updated_at = _utc_now()
            variant_gridstack = (
                db.query(GridstackV2)
                .filter(GridstackV2.parent_tab_id == variant_tab.id, GridstackV2.parent_id.is_(None))
                .first()
            )
            if variant_gridstack is not None:
                variant_gridstack.position = index

        db.commit()
        # Position is UI-only. Component ingestion does not store variant
        # order, so reordering must not create a Qdrant update job.
        return get_tab_variants_v2(db, parent_document_id)

    except Exception:
        db.rollback()
        raise


def get_tab_children_v2(db: Session, document_id: str) -> list[dict[str, Any]] | None:
    gridstack = get_gridstack_by_document_id(db, document_id)
    if gridstack is None:
        return None

    children = (
        db.query(GridstackV2)
        .filter(GridstackV2.parent_id == gridstack.id)
        .all()
    )
    summaries = [_format_tab_summary(db, c) for c in children]
    summaries.sort(key=lambda s: (s["order"], s["id"] or 0))
    return summaries


def get_tab_content_v2(db: Session, document_id: str) -> dict[str, Any] | None:
    gridstack = get_gridstack_by_document_id(db, document_id)
    if gridstack is None:
        return None
    return _format_page_content(db, gridstack)


def get_tab_workspace_v2(db: Session, document_id: str) -> dict[str, Any] | None:
    gridstack = get_gridstack_by_document_id(db, document_id)
    if gridstack is None:
        return None

    is_root = _is_root(gridstack)
    # Fetched once here (only when root) and threaded into both
    # `_safe_locked_pair`/`_safe_access_control_for_gridstack` calls below
    # instead of each re-fetching the identical TabV2 row — see
    # `_ROOT_TAB_NOT_FETCHED`'s doc comment.
    root_tab = _get_root_tab(db, gridstack) if is_root else None

    has_variants = False
    if is_root:
        node_id = root_tab.id if root_tab else None
        title = root_tab.title if root_tab else gridstack.name
        order = root_tab.order if root_tab and root_tab.order is not None else (gridstack.position or 0)
        has_variants = _has_variants(db, root_tab.id) if root_tab is not None else False
    else:
        node_id = gridstack.id
        title = gridstack.name
        order = gridstack.position if gridstack.position is not None else 0

    parent = None
    if gridstack.parent_id is not None:
        parent_gridstack = db.query(GridstackV2).filter(GridstackV2.id == gridstack.parent_id).first()
        if parent_gridstack is not None:
            parent = {
                "id": parent_gridstack.id,
                "documentId": parent_gridstack.document_id,
                "title": parent_gridstack.name,
                "order": parent_gridstack.position if parent_gridstack.position is not None else 0,
            }

    children = (
        db.query(GridstackV2)
        .filter(GridstackV2.parent_id == gridstack.id)
        .all()
    )
    child_summaries = [_format_tab_summary(db, c) for c in children]
    child_summaries.sort(key=lambda s: (s["order"], s["id"] or 0))

    locked, locked_by = _safe_locked_pair(db, gridstack, root_tab)

    return {
        "id": node_id,
        "documentId": gridstack.document_id,
        "title": title,
        "order": order,
        "parent": parent,
        "page_content": _format_page_content(db, gridstack),
        "access_control": _safe_access_control_for_gridstack(db, gridstack, root_tab),
        # The effective (NULL-skipping resolved, landmine-14) AC of THIS
        # node itself -- also, not coincidentally, the exact ceiling
        # update_tab_content_v2 already validates each widget on this
        # canvas against (§3.4). Exposed on read so the frontend's
        # parent-scoped component picker (plan §6.1's last bullet, commit
        # 8) has one source for that ceiling instead of re-implementing the
        # walk-up in TypeScript.
        "resolved_access_control": resolved_ac_for_gridstack(db, gridstack),
        "locked": locked,
        "locked_by": locked_by,
        "children": child_summaries,
        "has_variants": has_variants,
        "apiVersion": "v2",
    }


# ---------------------------------------------------------
# Content write: diff incoming GridCanvasContent against existing rows
# ---------------------------------------------------------

def _cascade_gridstack_id_to_sbn_descendants(
    db: Session, component_id: int, new_gridstack_id: int
) -> list[int]:
    """When a Super Block Note's top-level widget is re-parented into a
    different gridstack (see the by-`link` re-parenting branch in
    `update_tab_content_v2`), every descendant (`super_blocknote_id` chain)
    must move with it. Several other code paths assume a whole SBN tree
    shares one `gridstack_id` and use it to find/delete the tree as a unit —
    `delete_tab_subtree_by_document_id_v2`'s own component-deletion query
    (`ComponentV2.gridstack_id == gid`) is the concrete case that surfaced
    this: without this cascade, deleting the gridstack the root moved into
    finds only the root, not its children, which still reference it via
    `super_blocknote_id` — a foreign key violation."""
    changed_ids: list[int] = []
    children = db.query(ComponentV2).filter(ComponentV2.super_blocknote_id == component_id).all()
    for child in children:
        child.gridstack_id = new_gridstack_id
        changed_ids.append(child.id)
        changed_ids.extend(
            _cascade_gridstack_id_to_sbn_descendants(db, child.id, new_gridstack_id)
        )
    return changed_ids


def _collect_sbn_descendant_ids(db: Session, component_id: int) -> list[int]:
    """Every component reachable from `component_id` via the `super_blocknote_id`
    chain, at any depth. When a Super Block Note's top-level widget is removed
    from a canvas (see the delete branch in `update_tab_content_v2`), these
    must be deleted before `component_id` itself — they still reference it via
    that self-referential FK, and deleting only the top-level row is a
    foreign key violation (the same reasoning `_cascade_gridstack_id_to_sbn_
    descendants` above documents for the re-parent case)."""
    ids: list[int] = []
    children = db.query(ComponentV2.id).filter(ComponentV2.super_blocknote_id == component_id).all()
    for (child_id,) in children:
        ids.append(child_id)
        ids.extend(_collect_sbn_descendant_ids(db, child_id))
    return ids


def _delete_component_and_page_content(db: Session, component: ComponentV2) -> None:
    if component.page_content_id is not None:
        page_content = (
            db.query(PageContentV2).filter(PageContentV2.id == component.page_content_id).first()
        )
        if page_content is not None:
            db.delete(page_content)
    db.delete(component)


def _component_persistence_signature(db: Session, component: ComponentV2) -> dict[str, Any]:
    """Return only database state that can affect this component's index.

    Comparing the committed representation here is more reliable than a
    frontend event registry: widget editors can emit multiple equivalent
    objects, while this signature changes only when persisted component
    state actually changes.
    """
    page_content: Any = None
    if component.page_content_id is not None:
        stored = (
            db.query(PageContentV2)
            .filter(PageContentV2.id == component.page_content_id)
            .first()
        )
        page_content = stored.content if stored is not None else None
    return {
        "type": component.type,
        "title": component.title,
        "description": component.description,
        "x": component.x,
        "y": component.y,
        "width": component.width,
        "height": component.height,
        "props": component.props,
        "access_control": component.access_control,
        "gridstack_id": component.gridstack_id,
        "super_blocknote_id": component.super_blocknote_id,
        "current_grid_id": component.current_grid_id,
        "page_content": page_content,
    }


def _search_update_receipts(updates: dict[int, str]) -> list[dict[str, Any]]:
    return [
        {"component_id": component_id, "action": action}
        for component_id, action in updates.items()
    ]


def _component_ids_for_gridstack_tree(
    db: Session,
    gridstack: GridstackV2,
    *,
    include_variants: bool = False,
) -> list[int]:
    """Return every component whose searchable metadata owns this path."""
    gridstack_ids = [gridstack.id, *get_descendant_gridstack_ids(db, gridstack.id)]
    if include_variants and _is_root(gridstack):
        tab = _get_root_tab(db, gridstack)
        if tab is not None:
            variants = db.query(TabV2).filter(TabV2.parent_tab_id == tab.id).all()
            for variant in variants:
                variant_gridstack = (
                    db.query(GridstackV2)
                    .filter(GridstackV2.document_id == variant.document_id)
                    .first()
                )
                if variant_gridstack is not None:
                    gridstack_ids.extend(
                        [
                            variant_gridstack.id,
                            *get_descendant_gridstack_ids(db, variant_gridstack.id),
                        ]
                    )
    rows = (
        db.query(ComponentV2.id)
        .filter(ComponentV2.gridstack_id.in_(set(gridstack_ids)))
        .all()
    )
    return [row[0] for row in rows]


def update_tab_content_v2(
    db: Session,
    document_id: str,
    content: dict[str, Any] | list[Any] | None,
) -> dict[str, Any] | None:
    try:
        gridstack = get_gridstack_by_document_id(db, document_id)
        if gridstack is None:
            return None

        # Component subset rule (§3.4) — a widget's access_control may only
        # narrow the gridstack's own resolved ceiling, never widen it. This is
        # a BULK path (one save carries every widget on the canvas), so the
        # ceiling is resolved once, lazily, rather than once per widget.
        _widget_ac_ceiling: dict[str, Any] | None = None

        def _resolved_widget_ac_ceiling() -> dict[str, Any]:
            nonlocal _widget_ac_ceiling
            if _widget_ac_ceiling is None:
                _widget_ac_ceiling = resolved_ac_for_gridstack(db, gridstack)
            return _widget_ac_ceiling

        search_updates: dict[int, str] = {}
        incoming = content if isinstance(content, dict) else {}
        incoming_layout = {entry.get("id"): entry for entry in (incoming.get("layout") or [])}
        incoming_widgets = incoming.get("widgets") or {}

        # Excludes Super Block Note descendants (super_blocknote_id set) and
        # the gridstack's own self-representation component (current_grid_id)
        # — both share this gridstack_id but are never part of the top-level
        # canvas diff; without this filter, every save of an unrelated
        # top-level widget would see them as "removed" (never present in
        # incoming_widgets, which only ever carries top-level entries) and
        # delete them. Matches _serialize_gridstack_content's same filter.
        existing_components = {
            str(c.id): c
            for c in db.query(ComponentV2)
            .filter(
                ComponentV2.gridstack_id == gridstack.id,
                ComponentV2.super_blocknote_id.is_(None),
                ComponentV2.type.notin_(GRIDSTACK_REPRESENTATION_TYPES),
            )
            .all()
        }

        incoming_ids = set(incoming_widgets.keys())

        # Delete components removed from the canvas. A removed component may
        # be a Super Block Note's top-level widget, in which case its whole
        # descendant tree (`super_blocknote_id` chain) must go first — those
        # rows still reference it via that self-referential FK, and deleting
        # only the top-level row is a foreign key violation. Descendant ids
        # are always strictly higher than their SBN parent's (a child can't
        # reference a parent that doesn't exist yet), so deleting in
        # descending-id order is child-before-parent; flushing each delete
        # forces SQLAlchemy to execute them in that order rather than batching
        # same-table deletes into one arbitrary-order executemany (same
        # reasoning as delete_tab_subtree_by_document_id_v2's per-node flush).
        for existing_id, component in list(existing_components.items()):
            if existing_id not in incoming_ids:
                search_updates[component.id] = "delete"
                descendant_ids = sorted(
                    _collect_sbn_descendant_ids(db, component.id), reverse=True
                )
                for descendant_id in descendant_ids:
                    descendant = (
                        db.query(ComponentV2).filter(ComponentV2.id == descendant_id).first()
                    )
                    if descendant is None:
                        continue
                    _delete_component_and_page_content(db, descendant)
                    db.flush()
                _delete_component_and_page_content(db, component)
                db.flush()
                del existing_components[existing_id]

        # A widget entry can legitimately arrive here still carrying its OLD,
        # already-persisted `link` even though the row currently belongs to a
        # DIFFERENT gridstack — e.g. SgsCanvasHost migrating a plain canvas's
        # content into a freshly-created SGS sub-tab. `existing_components`
        # above is scoped to THIS gridstack, so such a widget would otherwise
        # look "new" and get a freshly-generated row/link below — orphaning
        # any Super Block Note children (`super_blocknote_id` points at the
        # OLD row's id, never carried over) and breaking any mirror already
        # targeting the OLD `link`. Resolving by `link` first lets the
        # existing row be re-parented (gridstack_id updated) in place instead.
        incoming_links = {
            entry.get("link")
            for entry in incoming_widgets.values()
            if isinstance(entry, dict) and entry.get("link")
        }
        components_by_link: dict[str, ComponentV2] = {}
        if incoming_links:
            components_by_link = {
                c.link: c
                for c in db.query(ComponentV2).filter(ComponentV2.link.in_(incoming_links)).all()
            }

        # Update-in-place or insert.
        for widget_id, widget_entry in incoming_widgets.items():
            if not isinstance(widget_entry, dict):
                continue

            layout_entry = incoming_layout.get(widget_id) or {}
            widget_type = widget_entry.get("type")
            widget_data = widget_entry.get("data")

            # Structural-metadata-only now — a widget's actual `data` never
            # lives in `props` (see ComponentV2's docstring); it's persisted
            # via `_write_component_data` below instead.
            if widget_type == MIRROR_WIDGET_TYPE:
                structural_props: dict[str, Any] = {
                    "target_link": (widget_data or {}).get("targetLink")
                    if isinstance(widget_data, dict)
                    else None
                }
            else:
                structural_props = {}
                if layout_entry.get("minW") is not None:
                    structural_props["min_w"] = layout_entry["minW"]
                if layout_entry.get("minH") is not None:
                    structural_props["min_h"] = layout_entry["minH"]

            access_control = widget_entry.get("access_control")
            title = widget_entry.get("title")
            description = widget_entry.get("description")

            # NULL means inherit (§3.4) — nothing to check. Airtable's is
            # protected (never actually written from this unauthenticated
            # path — see AIRTABLE_PROTECTED_DATA_FIELDS above) and a
            # mirror's stored value is inert (serialized access is derived
            # from the mirror's target, never its own column) — validating
            # either would reject values that are never actually enforced.
            if (
                access_control
                and widget_type != AIRTABLE_WIDGET_TYPE
                and widget_type != MIRROR_WIDGET_TYPE
            ):
                violation = access_control_subset_violation(
                    access_control, _resolved_widget_ac_ceiling()
                )
                if violation is not None:
                    raise ValueError(f"Widget '{widget_id}': {violation}")

            existing = existing_components.get(widget_id)
            before_signature = (
                _component_persistence_signature(db, existing)
                if existing is not None
                else None
            )
            if existing is None:
                link = widget_entry.get("link")
                candidate = components_by_link.get(link) if link else None
                if (
                    candidate is not None
                    and candidate.gridstack_id != gridstack.id
                    and candidate.super_blocknote_id is None
                ):
                    before_signature = _component_persistence_signature(db, candidate)
                    candidate.gridstack_id = gridstack.id
                    for descendant_id in _cascade_gridstack_id_to_sbn_descendants(
                        db, candidate.id, gridstack.id
                    ):
                        search_updates[descendant_id] = "upsert"
                    existing = candidate

            if existing is not None:
                # Read the stored blob BEFORE mutating `existing.type` below:
                # _resolve_component_data branches on the component's type, so
                # a type change would otherwise make it unwrap the wrong shape.
                # RAW, not the sanitised view: the sanitised one has `pat`
                # removed, so preserving from it would delete the stored token
                # on every canvas save.
                stored_data = (
                    _raw_component_data(db, existing)
                    if widget_type == AIRTABLE_WIDGET_TYPE
                    else None
                )

                existing.type = widget_type
                existing.x = layout_entry.get("x", existing.x)
                existing.y = layout_entry.get("y", existing.y)
                existing.width = layout_entry.get("w", existing.width)
                existing.height = layout_entry.get("h", existing.height)
                # Three independent reasons to leave the stored AC alone.
                # An airtable widget's access_control is protected — see
                # AIRTABLE_PROTECTED_DATA_FIELDS. This save path is
                # unauthenticated, so letting it clear the AC would defeat the
                # check on GET /data/airtable/component/{link}/rows. Changing
                # it goes through the authenticated config endpoint instead.
                # The serializer intentionally omits an empty access object.
                # Absence therefore means "preserve", not "overwrite with
                # null". Treating it as null made every empty-access sibling
                # look modified on an otherwise one-widget content save.
                # Mirrors are a third special case: their serialized access
                # is derived from the target for read filtering, not the
                # mirror row's own persisted access.
                if (
                    widget_type != AIRTABLE_WIDGET_TYPE
                    and widget_type != MIRROR_WIDGET_TYPE
                    and "access_control" in widget_entry
                ):
                    existing.access_control = access_control
                existing.title = title
                existing.description = description

                # Merge, don't overwrite: a top-level widget that's the SBN
                # root (type == "super_block_note") may already have
                # locked/locked_by set in `props` via the SBN lock endpoints —
                # a plain resize/move save here must not silently wipe that.
                existing.props = {**(existing.props or {}), **structural_props}

                if widget_type != MIRROR_WIDGET_TYPE:
                    data_to_write = widget_data if isinstance(widget_data, dict) else {}
                    if widget_type == AIRTABLE_WIDGET_TYPE:
                        data_to_write = _apply_airtable_protection(stored_data, data_to_write)
                    _write_component_data(db, existing, data_to_write)
                db.flush()
                if before_signature != _component_persistence_signature(db, existing):
                    search_updates[existing.id] = "upsert"
            else:
                new_component = ComponentV2(
                    link=_generate_id(),
                    type=widget_type,
                    title=title,
                    description=description,
                    props=structural_props,
                    # A brand-new airtable widget gets no access_control here
                    # (protected — see AIRTABLE_PROTECTED_DATA_FIELDS); the
                    # frontend sets it via the authenticated config endpoint
                    # right after this save assigns the component its `link`.
                    access_control=(
                        None if widget_type == AIRTABLE_WIDGET_TYPE else access_control
                    ),
                    x=layout_entry.get("x", 0),
                    y=layout_entry.get("y", 0),
                    width=layout_entry.get("w", 6),
                    height=layout_entry.get("h", 6),
                    gridstack_id=gridstack.id,
                    page_content_id=None,
                    current_grid_id=None,
                )
                db.add(new_component)
                db.flush()
                if widget_type != MIRROR_WIDGET_TYPE:
                    # The flush above (hoisted out of this branch so a mirror's
                    # id is populated for the receipt below too) already gave
                    # new_component its id.
                    data_to_write = widget_data if isinstance(widget_data, dict) else {}
                    if widget_type == AIRTABLE_WIDGET_TYPE:
                        # Nothing stored yet, so this drops every protected
                        # field rather than trusting the unauthenticated body.
                        data_to_write = _apply_airtable_protection(None, data_to_write)
                    _write_component_data(db, new_component, data_to_write)
                search_updates[new_component.id] = "upsert"

        # Persist the Super GridStack tab-bar config. It rides in the canvas
        # `content` (content.sgs) but is stored on the gridstack's own
        # `settings` — this is the only runtime path that writes it (previously
        # the field was silently dropped here, so a custom tab-bar position
        # reverted to the 'top' default on every reload). Content is
        # authoritative: set it when present, drop it when the canvas is no
        # longer SGS-flagged. SGS rendering itself is driven by having child
        # gridstacks, not by this flag, so this only governs tab-bar position.
        if isinstance(content, dict):
            settings = dict(gridstack.settings or {})
            incoming_sgs = content.get("sgs")
            settings_changed = False
            if incoming_sgs is not None:
                if settings.get("sgs") != incoming_sgs:
                    settings["sgs"] = incoming_sgs
                    gridstack.settings = settings
                    settings_changed = True
            elif "sgs" in settings:
                del settings["sgs"]
                gridstack.settings = settings
                settings_changed = True

            # Keep this gridstack's own self-representation component (see
            # current_grid_id on ComponentV2) in sync: gridstack while
            # untouched/plain, super_gridstack while its tab-bar config is
            # set, reverting back to gridstack if that config is cleared.
            if settings_changed:
                gridstack_component = (
                    db.query(ComponentV2)
                    .filter(ComponentV2.current_grid_id == gridstack.id)
                    .first()
                )
                if gridstack_component is not None:
                    representation_before = _component_persistence_signature(
                        db, gridstack_component
                    )
                    new_sgs = settings.get("sgs")
                    gridstack_component.type = (
                        SUPER_GRIDSTACK_WIDGET_TYPE if new_sgs else GRIDSTACK_WIDGET_TYPE
                    )
                    gridstack_component.props = {"sgs": new_sgs} if new_sgs else None
                    db.flush()
                    if representation_before != _component_persistence_signature(
                        db, gridstack_component
                    ):
                        search_updates[gridstack_component.id] = "upsert"

        if _is_root(gridstack):
            tab = _get_root_tab(db, gridstack)
            if tab is not None:
                tab.updated_at = _utc_now()

        db.commit()

        response = _format_page_content(db, gridstack)
        response["search_updates"] = _search_update_receipts(search_updates)
        return response

    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------
# Create / update / delete
# ---------------------------------------------------------

def _gridstack_exists_under_parent(
    db: Session,
    name: str,
    parent_tab_id: int,
    parent_id: int,
) -> GridstackV2 | None:
    name = _validate_title(name)
    return (
        db.query(GridstackV2)
        .filter(
            GridstackV2.name == name,
            GridstackV2.parent_tab_id == parent_tab_id,
            GridstackV2.parent_id == parent_id,
        )
        .first()
    )


def _root_tab_with_conflicting_slug(
    db: Session, title: str, nav_tab_id: int | None, exclude_tab_id: int | None = None
) -> TabV2 | None:
    """The root tab in `nav_tab_id` whose title addresses the same URL as
    `title`, or None.

    Compares SLUGS, not raw titles. A root tab's URL segment is derived from
    its title at render time (slugifyTitle, DashboardV2Page.tsx) rather than
    stored, so the slug — not the title — is what actually has to be unique.
    An exact `TabV2.title == title` match is case- and punctuation-sensitive,
    so it let "home" sit beside "Home": both slugify to `home`, both claim
    `/<nav>/home`, and `resolveTabByPath` takes the first `.find()` hit —
    silently making the other unreachable by URL, with no error anywhere.

    Scoped per nav tab, and to roots only via parent_tab_id.is_(None) — a
    variant's title does not block a root's (fixing a latent bug where the
    previously unscoped query let a *variant* named "Home" block creating a
    root named "Home"). Variants are excluded on purpose: they are selected
    by a pill, never addressed by their own URL segment.

    Slugification is a Python-side transform (NFKD + combining-mark
    stripping) with no SQL equivalent, so this filters in the database on
    what it can and compares slugs in memory. The candidate set is one nav
    tab's root tabs — tens of rows, not thousands.
    """
    title = _validate_title(title)
    slug = _slugify_title(title)

    query = db.query(TabV2).filter(
        TabV2.nav_tab_id == nav_tab_id,
        TabV2.parent_tab_id.is_(None),
    )
    if exclude_tab_id is not None:
        query = query.filter(TabV2.id != exclude_tab_id)

    for candidate in query.all():
        if not slug:
            # An all-punctuation/emoji title slugifies to "" and has no
            # usable URL either way (pathSegmentsForTab bails on an empty
            # segment). Comparing on "" would make every such title collide
            # with every other, reporting "already exists" for two titles
            # that plainly differ — so fall back to the exact-title rule.
            if candidate.title == title:
                return candidate
            continue
        if _slugify_title(candidate.title or "") == slug:
            return candidate
    return None


def create_tab_v2(
    db: Session,
    title: str,
    parent_document_id: str | None = None,
    content: dict[str, Any] | list[Any] | None = None,
    order: int | None = None,
    access_control: dict[str, Any] | None = None,
    nav_tab_id: int | None = None,
) -> dict[str, Any]:
    try:
        title = _validate_title(title)
        parent_document_id = _validate_document_id_value(parent_document_id, "parentDocumentId")
        order = _validate_order(order)

        parent_gridstack: GridstackV2 | None = None
        if parent_document_id is not None:
            parent_gridstack = get_gridstack_by_document_id(db, parent_document_id)
            if parent_gridstack is None:
                raise ValueError("Parent tab does not exist")

        now = _utc_now()

        created_gridstack_component: ComponentV2
        if parent_gridstack is None:
            conflict = _root_tab_with_conflicting_slug(db, title, nav_tab_id)
            if conflict is not None:
                raise ValueError(
                    f'A tab addressed as "{_slugify_title(title)}" already exists here '
                    f'("{conflict.title}"). Titles differing only in capitalisation or '
                    f"punctuation share one URL, so pick a more distinct name."
                )

            nav_tab_row = (
                db.query(NavTabV2).filter(NavTabV2.id == nav_tab_id).first()
                if nav_tab_id is not None
                else None
            )
            parent_ac = _access_control_or_default(
                nav_tab_row.access_control if nav_tab_row else None
            )
            effective_ac = access_control if access_control is not None else parent_ac

            new_tab = TabV2(
                document_id=_generate_id(),
                title=title,
                order=order,
                # Materialized below via apply_write.
                access_control=None,
                locked=False,
                locked_by="",
                nav_tab_id=nav_tab_id,
                created_at=now,
                updated_at=now,
            )
            db.add(new_tab)
            db.flush()

            new_gridstack = GridstackV2(
                document_id=new_tab.document_id,
                name=title,
                settings={},
                position=order,
                parent_id=None,
                parent_tab_id=new_tab.id,
            )
            db.add(new_gridstack)
            db.flush()
            created_gridstack_component = _create_gridstack_component(db, new_gridstack)

            apply_write(db, NodeRef("tab", new_tab.id), effective_ac)
        else:
            existing = _gridstack_exists_under_parent(
                db, title, parent_tab_id=parent_gridstack.parent_tab_id, parent_id=parent_gridstack.id
            )
            if existing is not None:
                raise ValueError("A tab with this title already exists under the same parent")

            # "The parent's" AC (§5.2) — the owning TabV2's AC for a "top"
            # sub-tab directly on a root/variant's own canvas, or the parent
            # sub-tab's own AC for a nested one; _safe_access_control_for_
            # gridstack already implements exactly that distinction.
            parent_ac = _safe_access_control_for_gridstack(db, parent_gridstack)
            effective_ac = access_control if access_control is not None else parent_ac

            new_gridstack = GridstackV2(
                document_id=_generate_id(),
                name=title,
                settings={},
                position=order,
                parent_id=parent_gridstack.id,
                parent_tab_id=parent_gridstack.parent_tab_id,
            )
            db.add(new_gridstack)
            db.flush()
            # Materialized below via apply_write.
            created_gridstack_component = _create_gridstack_component(db, new_gridstack)

            apply_write(db, NodeRef("gridstack", new_gridstack.id), effective_ac)

        content_receipts: list[dict[str, Any]] = []
        if content:
            content_result = update_tab_content_v2(db, new_gridstack.document_id, content)
            if content_result is not None:
                content_receipts = list(content_result.get("search_updates") or [])

        db.commit()

        response = _format_tab_summary(db, new_gridstack)
        receipts: dict[int, str] = {
            created_gridstack_component.id: "upsert"
        }
        for item in content_receipts:
            receipts[int(item["component_id"])] = str(item["action"])
        response["search_updates"] = _search_update_receipts(receipts)
        return response

    except Exception:
        db.rollback()
        raise


def update_tab_by_document_id_v2(
    db: Session,
    document_id: str,
    title: str | None = None,
    order: int | None = None,
    access_control: dict[str, Any] | None = None,
    locked: bool | None = None,
    locked_by: str | None = None,
) -> dict[str, Any] | None:
    try:
        title = _validate_title(title)
        order = _validate_order(order)
        locked_by = _validate_locked_by(locked_by)

        gridstack = get_gridstack_by_document_id(db, document_id)
        if gridstack is None:
            return None

        if _is_root(gridstack):
            tab = _get_root_tab(db, gridstack)
            if tab is None:
                return None

            # Renaming into another root's slug orphans one of them from the
            # URL exactly as creating a duplicate would — and this path had
            # NO uniqueness check at all, so it walked straight around the
            # create-time one. Checked first, before the access-control
            # mutations below, so a rejection leaves nothing half-applied.
            #
            # Variants (parent_tab_id set) are exempt: they are selected by a
            # pill, never addressed by their own URL segment, and they keep
            # their own sibling-title rule in create_tab_variant_v2.
            if title is not None and tab.parent_tab_id is None:
                conflict = _root_tab_with_conflicting_slug(
                    db, title, tab.nav_tab_id, exclude_tab_id=tab.id
                )
                if conflict is not None:
                    raise ValueError(
                        f'A tab addressed as "{_slugify_title(title)}" already exists here '
                        f'("{conflict.title}"). Titles differing only in capitalisation or '
                        f"punctuation share one URL, so pick a more distinct name."
                    )

            if title is not None:
                tab.title = title
                gridstack.name = title
            if order is not None:
                tab.order = order
                gridstack.position = order
            if access_control is not None:
                # apply_write materializes the change through the whole
                # propagation engine (§3.2) — this covers both a root tab
                # and a variant uniformly; there is no longer a separate
                # subset/cascade rule for variants (§5.2, §10 commit 3).
                apply_write(db, NodeRef("tab", tab.id), access_control)
            if locked is not None:
                tab.locked = locked
            if locked_by is not None:
                tab.locked_by = locked_by
            tab.updated_at = _utc_now()
        else:
            if title is not None:
                gridstack.name = title
            if order is not None:
                gridstack.position = order
            if access_control is not None:
                # write_ac's gridstack branch creates the representation
                # component on demand (same fallback this call used to do
                # by hand for a pre-backfill sub-tab gridstack).
                apply_write(db, NodeRef("gridstack", gridstack.id), access_control)
            if locked is not None or locked_by is not None:
                raise ValueError(
                    "Locking is only supported for top-level tabs in this schema version"
                )

        # The indexed component payload reads the owning root/variant title
        # and access control, but never the visual order. A nested gridstack's
        # own title/access settings are resolved live by component-link
        # navigation and are likewise absent from the Qdrant document.
        should_refresh_index = _is_root(gridstack) and any(
            value is not None for value in (title, access_control)
        )
        affected_component_ids = (
            _component_ids_for_gridstack_tree(
                db,
                gridstack,
            )
            if should_refresh_index
            else []
        )
        db.commit()
        response = get_tab_workspace_v2(db, document_id)
        if response is not None and affected_component_ids:
            response["search_updates"] = [
                {"component_id": component_id, "action": "upsert"}
                for component_id in affected_component_ids
            ]
        return response

    except Exception:
        db.rollback()
        raise


def lock_tab_by_document_id_v2(db: Session, document_id: str, locked_by: str) -> dict[str, Any] | None:
    try:
        locked_by = _validate_locked_by(locked_by)
        if not locked_by:
            raise ValueError("locked_by is required")

        gridstack = get_gridstack_by_document_id(db, document_id)
        if gridstack is None:
            return None

        if not _is_root(gridstack):
            raise ValueError("Locking is only supported for top-level tabs in this schema version")

        tab = _get_root_tab(db, gridstack)
        if tab is None:
            return None

        if tab.locked and tab.locked_by and tab.locked_by != locked_by:
            raise ValueError(f"Tab is already locked by {tab.locked_by}")

        tab.locked = True
        tab.locked_by = locked_by
        tab.updated_at = _utc_now()

        db.commit()
        return get_tab_workspace_v2(db, document_id)

    except Exception:
        db.rollback()
        raise


def unlock_tab_by_document_id_v2(
    db: Session,
    document_id: str,
    unlocked_by: str | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    try:
        unlocked_by = _validate_locked_by(unlocked_by)

        gridstack = get_gridstack_by_document_id(db, document_id)
        if gridstack is None:
            return None

        if not _is_root(gridstack):
            raise ValueError("Locking is only supported for top-level tabs in this schema version")

        tab = _get_root_tab(db, gridstack)
        if tab is None:
            return None

        if not force and tab.locked and tab.locked_by and unlocked_by and tab.locked_by != unlocked_by:
            raise ValueError(f"Tab is locked by {tab.locked_by}")

        tab.locked = False
        tab.locked_by = ""
        tab.updated_at = _utc_now()

        db.commit()
        return get_tab_workspace_v2(db, document_id)

    except Exception:
        db.rollback()
        raise


def get_descendant_gridstack_ids(db: Session, gridstack_id: int) -> list[int]:
    descendants: list[int] = []
    visited: set[int] = set()

    def walk(current_id: int) -> None:
        if current_id in visited:
            return
        visited.add(current_id)
        children = db.query(GridstackV2).filter(GridstackV2.parent_id == current_id).all()
        for child in children:
            descendants.append(child.id)
            walk(child.id)

    walk(gridstack_id)
    return descendants


def _is_descendant_of(db: Session, possible_descendant_id: int, possible_ancestor_id: int) -> bool:
    return possible_descendant_id in get_descendant_gridstack_ids(db, possible_ancestor_id)


def move_tab_by_document_id_v2(
    db: Session,
    document_id: str,
    new_parent_document_id: str | None = None,
    order: int | None = None,
) -> dict[str, Any] | None:
    try:
        new_parent_document_id = _validate_document_id_value(new_parent_document_id, "newParentDocumentId")
        order = _validate_order(order)

        gridstack = get_gridstack_by_document_id(db, document_id)
        if gridstack is None:
            return None

        if _is_root(gridstack):
            raise ValueError("Moving a root tab is not supported in this schema version")

        new_parent: GridstackV2 | None = None
        if new_parent_document_id is not None:
            new_parent = get_gridstack_by_document_id(db, new_parent_document_id)
            if new_parent is None:
                raise ValueError("New parent tab does not exist")
            if new_parent.id == gridstack.id:
                raise ValueError("A tab cannot be moved under itself")
            if _is_descendant_of(db, new_parent.id, gridstack.id):
                raise ValueError("A tab cannot be moved under one of its descendants")

            gridstack.parent_id = new_parent.id
            gridstack.parent_tab_id = new_parent.parent_tab_id
            for descendant_id in get_descendant_gridstack_ids(db, gridstack.id):
                descendant = db.query(GridstackV2).filter(GridstackV2.id == descendant_id).first()
                if descendant is not None:
                    descendant.parent_tab_id = new_parent.parent_tab_id

            # This reparents a sub-tab gridstack exactly like
            # nav_tab_service.move_tab_to_nav_tab_v2 reparents a root tab —
            # same landmine 3 shape (both invariants can break in one
            # write), just not analyzed by the plan text. Same repair.
            db.flush()
            apply_reparent_repair(db, NodeRef("gridstack", gridstack.id))
        else:
            raise ValueError("Moving a sub-tab to root is not supported in this schema version")

        if order is not None:
            gridstack.position = order

        affected_component_ids = _component_ids_for_gridstack_tree(db, gridstack)
        db.commit()
        response = get_tab_workspace_v2(db, document_id)
        if response is not None:
            response["search_updates"] = [
                {"component_id": component_id, "action": "upsert"}
                for component_id in affected_component_ids
            ]
        return response

    except Exception:
        db.rollback()
        raise


def reorder_tabs_by_document_id_v2(db: Session, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        if not items:
            raise ValueError("Reorder items cannot be empty")

        document_ids = [_validate_document_id_value(item["documentId"], "documentId") for item in items]
        orders = [_validate_order(item["order"]) for item in items]

        if len(document_ids) != len(set(document_ids)):
            raise ValueError("Duplicate documentId values are not allowed")

        gridstacks = (
            db.query(GridstackV2)
            .filter(GridstackV2.document_id.in_(document_ids))
            .all()
        )
        by_document_id = {g.document_id: g for g in gridstacks}

        missing = [d for d in document_ids if d not in by_document_id]
        if missing:
            raise ValueError(f"Tabs not found: {', '.join(missing)}")

        parent_ids = {g.parent_id for g in gridstacks}
        if len(parent_ids) != 1:
            raise ValueError("All reordered tabs must belong to the same parent level")

        parent_id = next(iter(parent_ids))

        # Every root gridstack in this schema has parent_id None regardless
        # of which nav tab it belongs to, so the parent_id check above alone
        # would let a batch mix roots from two different nav tabs. Reject
        # that here too — dead from the frontend today (root order is
        # actually persisted per-tab via updateTabV2), but this endpoint is
        # reachable directly.
        if parent_id is None:
            owning_tab_ids = {g.parent_tab_id for g in gridstacks}
            owning_tabs = (
                db.query(TabV2).filter(TabV2.id.in_(owning_tab_ids)).all()
            )
            nav_tab_ids = {t.nav_tab_id for t in owning_tabs}
            if len(nav_tab_ids) != 1:
                raise ValueError("All reordered tabs must belong to the same nav tab")

        for document_id, order in zip(document_ids, orders):
            gridstack = by_document_id[document_id]
            gridstack.position = order
            if _is_root(gridstack):
                tab = _get_root_tab(db, gridstack)
                if tab is not None:
                    tab.order = order

        db.commit()

        if parent_id is None:
            response = get_root_tabs_v2(db)
        else:
            parent = db.query(GridstackV2).filter(GridstackV2.id == parent_id).first()
            if parent is None:
                return []
            response = get_tab_children_v2(db, parent.document_id) or []

        # Reorder changes only the UI position. None of the fields fetched by
        # component ingestion changes, so response-model defaults intentionally
        # leave search_updates empty.
        return response

    except Exception:
        db.rollback()
        raise


def delete_tab_subtree_by_document_id_v2(db: Session, document_id: str) -> dict[str, Any] | None:
    try:
        gridstack = get_gridstack_by_document_id(db, document_id)
        if gridstack is None:
            return None

        deleted_tabs: list[dict[str, Any]] = []
        deleted_component_ids: list[int] = []

        # A root tab may own tab variants (TabV2.parent_tab_id) — a wholly
        # separate nesting axis from GridstackV2.parent_id below. Each
        # variant is its own full independent subtree (own root gridstack,
        # own descendants/components), so deleting it needs this exact same
        # recursive process, and must happen before this function's own
        # gridstack-subtree walk deletes the owning TabV2 row further down
        # (a variant's parent_tab_id would otherwise dangle once its parent
        # row is gone). Variants can never themselves have variants (depth
        # is strictly one level), so this never recurses more than once.
        if _is_root(gridstack):
            owning_tab = _get_root_tab(db, gridstack)
            if owning_tab is not None:
                variant_tabs = (
                    db.query(TabV2).filter(TabV2.parent_tab_id == owning_tab.id).all()
                )
                for variant_tab in variant_tabs:
                    if not variant_tab.document_id:
                        continue
                    variant_result = delete_tab_subtree_by_document_id_v2(db, variant_tab.document_id)
                    if variant_result:
                        deleted_tabs.extend(variant_result.get("deleted_tabs", []))
                        deleted_component_ids.extend(
                            int(item["component_id"])
                            for item in variant_result.get("search_updates", [])
                        )

        # get_descendant_gridstack_ids returns a pre-order walk (each node
        # appears before its own descendants) — reversing it alone already
        # guarantees every descendant is deleted before its ancestors within
        # that set. The originally-requested node must always be deleted
        # LAST regardless, since (unlike v1's tabs_parent_lnk, which has no
        # real FK) GridstackV2.parent_id is a genuine FK constraint.
        descendant_ids = get_descendant_gridstack_ids(db, gridstack.id)
        gridstack_ids_to_delete = list(reversed(descendant_ids)) + [gridstack.id]

        for gid in gridstack_ids_to_delete:
            node = db.query(GridstackV2).filter(GridstackV2.id == gid).first()
            if node is None:
                continue

            deleted_tabs.append({"id": node.id, "documentId": node.document_id, "title": node.name})

            # Descending by id, not query order: a Super Block Note's own
            # nested sub-tab components (super_blocknote_id set) always have
            # a strictly higher id than their SBN parent (the parent must
            # already exist, and be flushed to get its id, before a child
            # row referencing it can be created) — so id-descending order
            # always deletes children before the parent they reference,
            # exactly like the gridstack-level fix below for parent_id.
            components = (
                db.query(ComponentV2)
                .filter(ComponentV2.gridstack_id == gid)
                .order_by(ComponentV2.id.desc())
                .all()
            )
            for component in components:
                deleted_component_ids.append(component.id)
                if component.page_content_id is not None:
                    page_content = (
                        db.query(PageContentV2)
                        .filter(PageContentV2.id == component.page_content_id)
                        .first()
                    )
                    if page_content is not None:
                        db.delete(page_content)
                db.delete(component)
                # Same self-referential-FK-without-relationship reasoning as
                # the gridstack flush below — force each delete to actually
                # execute now, in this loop's intended child-before-parent
                # order, rather than letting SQLAlchemy batch them into one
                # arbitrary-order executemany.
                db.flush()

            is_root_node = node.parent_id is None
            db.delete(node)
            # Flush immediately: without a declared ORM `relationship()` on
            # the self-referential parent_id FK, SQLAlchemy has no way to
            # know it must order same-table deletes child-before-parent —
            # it may batch multiple gridstacks deletes into one executemany
            # in arbitrary order at commit time. Flushing per-node forces
            # each delete to actually execute in the loop's intended order.
            db.flush()

            if is_root_node:
                tab = db.query(TabV2).filter(TabV2.id == node.parent_tab_id).first()
                if tab is not None:
                    db.delete(tab)

        db.commit()

        return {
            "message": "Tab subtree deleted successfully",
            "deleted_count": len(deleted_tabs),
            "deleted_tabs": deleted_tabs,
            "search_updates": [
                {"component_id": component_id, "action": "delete"}
                for component_id in deleted_component_ids
            ],
        }

    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------
# Preview / purge / reset (§5.4, §6.3, §6.4) — for a tab-family node (root,
# variant, or sub-tab gridstack) addressed by its document_id. Resolves to
# the same NodeRef("tab", ...) / NodeRef("gridstack", ...) split
# update_tab_by_document_id_v2 already makes; factored out here since these
# three new functions and that existing one all need it.
# ---------------------------------------------------------


def resolve_tab_ref_by_document_id_v2(db: Session, document_id: str) -> NodeRef | None:
    gridstack = get_gridstack_by_document_id(db, document_id)
    if gridstack is None:
        return None
    if _is_root(gridstack):
        tab = _get_root_tab(db, gridstack)
        if tab is None:
            return None
        return NodeRef("tab", tab.id)
    return NodeRef("gridstack", gridstack.id)


def preview_tab_access_v2(
    db: Session, document_id: str, access_control: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Read-only — backs the preview modal. Never writes."""
    ref = resolve_tab_ref_by_document_id_v2(db, document_id)
    if ref is None:
        return None
    plan = plan_write(db, ref, access_control)
    return describe_write_plan(db, plan)


def _refresh_index_for_touched(db: Session, touched: list[NodeRef]) -> list[dict[str, Any]]:
    """Purge/reset can touch many nodes across the tree in one call — but,
    matching update_tab_by_document_id_v2's own comment, only a ROOT/variant
    TabV2's access_control is embedded in the indexed component payload; a
    sub-tab's is resolved live by component-link navigation and was never
    indexed. So only the "tab" kind entries among `touched` (root or
    variant) need their component subtrees reindexed."""
    search_updates: list[dict[str, Any]] = []
    for ref in touched:
        if ref.kind != "tab":
            continue
        tab = db.query(TabV2).filter(TabV2.id == ref.id).first()
        if tab is None or tab.document_id is None:
            continue
        gridstack = get_gridstack_by_document_id(db, tab.document_id)
        if gridstack is None:
            continue
        for component_id in _component_ids_for_gridstack_tree(db, gridstack):
            search_updates.append({"component_id": component_id, "action": "upsert"})
    return search_updates


def purge_tab_principal_v2(
    db: Session, document_id: str, principal_payload: dict[str, Any]
) -> dict[str, Any] | None:
    try:
        ref = resolve_tab_ref_by_document_id_v2(db, document_id)
        if ref is None:
            return None
        principal = principal_from_payload(principal_payload)
        touched = purge_principal(db, ref, principal)
        search_updates = _refresh_index_for_touched(db, touched)
        db.commit()
        return {
            "touched": [node_summary(db, r) for r in touched],
            "search_updates": search_updates,
        }
    except Exception:
        db.rollback()
        raise


def reset_tab_access_v2(db: Session, document_id: str) -> dict[str, Any] | None:
    """Discards this node's own access_control and re-derives it from its
    resolved parent (§6.4's "match the parent again" — the drift-repair
    tool for landmine 2)."""
    try:
        ref = resolve_tab_ref_by_document_id_v2(db, document_id)
        if ref is None:
            return None
        touched = reset_to_inherited(db, ref)
        search_updates = _refresh_index_for_touched(db, touched)
        db.commit()
        return {
            "touched": [node_summary(db, r) for r in touched],
            "search_updates": search_updates,
        }
    except Exception:
        db.rollback()
        raise
