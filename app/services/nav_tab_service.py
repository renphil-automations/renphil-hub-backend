"""
Nav tab service layer (phase 1 — see AI Docs/plan_nav_tabs_2026-07-28.md).

A nav tab is the parent layer above root tabs: it owns a set of root TabV2
rows (TabV2.nav_tab_id) and renders as its own dashboard. This module is a
new peer of gridstack_service.py rather than more surface on that file.

Import direction is one-way: this module imports from gridstack_service,
never the reverse — matching how super_blocknote_service.py already depends
on gridstack_service.py for its own shared helpers.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db_v2.models.nav_tab import NavTabV2
from app.db_v2.models.tab import TabV2

from app.services.access_control_service import (
    NodeRef,
    apply_reparent_repair,
    apply_write,
    describe_write_plan,
    get_hub,
    node_summary,
    plan_write,
    principal_from_payload,
    purge_principal,
    reset_to_inherited,
)
from app.services.gridstack_service import (
    _UNSET,
    _access_control_or_default,
    _component_ids_for_gridstack_tree,
    _generate_id,
    _get_root_tab,
    _is_root,
    _root_tab_with_conflicting_slug,
    _slugify_title,
    _utc_now,
    _validate_document_id_value,
    _validate_order,
    _validate_title,
    delete_tab_subtree_by_document_id_v2,
    get_gridstack_by_document_id,
    get_tab_workspace_v2,
)

# Top-level path segments already owned by the app. A nav-tab slug that
# collided with one of these would be shadowed by the real route and the
# nav tab would be permanently unreachable — a silent failure that looks
# like a data bug, so it is rejected at the source instead.
#   App.tsx — matched ABOVE the /* catch-all: login, auth, aixscience
#   HomePage.tsx — sidebar sections + /profile: profile, fundraising,
#   tracking, tracking-airtable, funders, tickets, workflows, knowledge, admin
# 'dashboard' is deliberately ABSENT: it is a real nav tab (the protected
# Dashboard row), not a reserved word.
#
# Keep this list in sync with App.tsx's <Route> list and HomePage.tsx's
# parsePathname / SIDEBAR_TO_URL. Adding a new top-level section to the
# frontend without adding it here lets an existing nav tab shadow it.
RESERVED_NAV_SLUGS = frozenset(
    {
        "login",
        "auth",
        "aixscience",
        "profile",
        "fundraising",
        "tracking",
        "tracking-airtable",
        "funders",
        "tickets",
        "workflows",
        "knowledge",
        "admin",
    }
)


# ---------------------------------------------------------
# Slug resolution
# ---------------------------------------------------------

def _resolve_nav_slug(db: Session, title: str, exclude_id: int | None = None) -> str:
    """The single choke point for the top-level URL namespace. Rejects
    (rather than auto-suffixing) an empty, already-taken, or reserved slug —
    an admin who names a nav tab "Tracking" should be told the name is
    taken, not silently handed `tracking-2`."""
    slug = _slugify_title(title)
    if not slug:
        raise ValueError("Title must contain at least one letter or digit")
    if slug in RESERVED_NAV_SLUGS:
        raise ValueError(f'"{title}" is a reserved name and cannot be used for a nav tab')

    query = db.query(NavTabV2).filter(NavTabV2.slug == slug)
    if exclude_id is not None:
        query = query.filter(NavTabV2.id != exclude_id)
    if query.first() is not None:
        raise ValueError("A nav tab with this name already exists")

    return slug


# ---------------------------------------------------------
# Formatting
# ---------------------------------------------------------

def _format_nav_tab(nav_tab: NavTabV2) -> dict[str, Any]:
    return {
        "id": nav_tab.id,
        "documentId": nav_tab.document_id,
        "slug": nav_tab.slug,
        "title": nav_tab.title,
        "order": nav_tab.order if nav_tab.order is not None else 0,
        "access_control": _access_control_or_default(nav_tab.access_control),
        "protected": bool(nav_tab.protected),
        "icon": nav_tab.icon,
    }


# ---------------------------------------------------------
# Read API
# ---------------------------------------------------------

def get_nav_tabs_v2(db: Session) -> list[dict[str, Any]]:
    nav_tabs = db.query(NavTabV2).order_by(NavTabV2.order, NavTabV2.id).all()
    return [_format_nav_tab(t) for t in nav_tabs]


def get_nav_tab_by_document_id(db: Session, document_id: str) -> NavTabV2 | None:
    document_id = _validate_document_id_value(document_id, "documentId")
    return db.query(NavTabV2).filter(NavTabV2.document_id == document_id).first()


def get_dashboard_nav_tab(db: Session) -> NavTabV2 | None:
    """The single protected nav tab created by migrate_nav_tabs.py. Used as
    the fallback nav_tab_id for a root-tab create that doesn't specify one,
    so any existing caller (frontend or otherwise) keeps working unchanged."""
    return db.query(NavTabV2).filter(NavTabV2.protected.is_(True)).first()


def resolve_move_authorization_targets(
    db: Session, tab_document_id: str, nav_tab_document_id: str
) -> tuple[TabV2 | None, NavTabV2 | None]:
    """Looks up the root tab and destination nav tab for
    `PUT /v2/tabs/{id}/nav-tab`'s dual authorization check (plan §5.3,
    §9.9c) — it writes to both, so both must independently satisfy
    `can_edit` before `move_tab_to_nav_tab_v2` is even attempted. A plain
    per-node `Depends` can't express this: it needs both the path's tab AND
    the request body's destination nav tab, so the router checks inline
    using this lookup rather than a FastAPI dependency."""
    gridstack = get_gridstack_by_document_id(db, tab_document_id)
    tab = _get_root_tab(db, gridstack) if gridstack is not None and _is_root(gridstack) else None
    nav_tab = get_nav_tab_by_document_id(db, nav_tab_document_id)
    return tab, nav_tab


# ---------------------------------------------------------
# Create / update / reorder / delete
# ---------------------------------------------------------

def create_nav_tab_v2(
    db: Session,
    title: str,
    access_control: dict[str, Any] | None = None,
    order: int | None = None,
    icon: str | None = None,
) -> dict[str, Any]:
    try:
        title = _validate_title(title)
        if not title:
            raise ValueError("Title is required")
        order = _validate_order(order)

        slug = _resolve_nav_slug(db, title)

        if order is None:
            max_order = db.query(func.max(NavTabV2.order)).scalar()
            order = (max_order + 1) if max_order is not None else 0

        hub = get_hub(db)
        if hub is None:
            raise ValueError(
                "The hub row does not exist — run scripts/migrate_hub_ac_propagation.py"
            )

        now = _utc_now()
        nav_tab = NavTabV2(
            document_id=_generate_id(),
            slug=slug,
            title=title,
            order=order,
            # Materialized below via apply_write — starts empty, like any
            # brand new node, so the diff against the effective AC below
            # propagates correctly (harmless no-op for the common case).
            access_control=None,
            protected=False,
            icon=icon,
            created_at=now,
            updated_at=now,
        )
        db.add(nav_tab)
        db.flush()

        # A new nav tab inherits the hub's AC (§5.2) — invariant A would be
        # violated the instant it existed otherwise. apply_write also
        # correctly propagates any extra principals the caller passed
        # beyond that baseline (rules 1/2), same as an ordinary edit.
        effective_ac = access_control if access_control is not None else hub.access_control
        apply_write(db, NodeRef("nav_tab", nav_tab.id), effective_ac)

        db.commit()
        return _format_nav_tab(nav_tab)

    except Exception:
        db.rollback()
        raise


def update_nav_tab_v2(
    db: Session,
    document_id: str,
    title: str | None = None,
    order: int | None = None,
    access_control: dict[str, Any] | None = None,
    icon: Any = _UNSET,
) -> dict[str, Any] | None:
    try:
        nav_tab = get_nav_tab_by_document_id(db, document_id)
        if nav_tab is None:
            return None

        title = _validate_title(title)
        order = _validate_order(order)

        if title is not None:
            if nav_tab.protected:
                raise ValueError("The Dashboard nav tab cannot be renamed")
            # Renaming a nav tab to its own current title is not a
            # self-collision — exclude_id skips the row being renamed.
            nav_tab.slug = _resolve_nav_slug(db, title, exclude_id=nav_tab.id)
            nav_tab.title = title

        if order is not None:
            nav_tab.order = order

        if access_control is not None:
            apply_write(db, NodeRef("nav_tab", nav_tab.id), access_control)

        # `_UNSET` (not `None`) is the "leave alone" sentinel here, unlike
        # title/order/access_control above — icon needs a real three-way:
        # omitted (leave alone), a string (set), or explicit `None` (clear to
        # the default icon, §3.4's "Use default"). Matches the same
        # model_fields_set-driven pattern the airtable `pat` field already
        # uses for its own omit/set/clear distinction. No protected-row
        # guard, unlike the title branch above — an icon has no coupling to
        # `slug`/URL stability, so the Dashboard row can have its icon
        # changed like any other nav tab (§3.3).
        if icon is not _UNSET:
            nav_tab.icon = icon

        nav_tab.updated_at = _utc_now()

        db.commit()
        return _format_nav_tab(nav_tab)

    except Exception:
        db.rollback()
        raise


def reorder_nav_tabs_v2(db: Session, ordered_document_ids: list[str]) -> list[dict[str, Any]]:
    try:
        if not ordered_document_ids:
            raise ValueError("Reorder list cannot be empty")

        nav_tabs_by_document_id = {
            t.document_id: t
            for t in db.query(NavTabV2)
            .filter(NavTabV2.document_id.in_(ordered_document_ids))
            .all()
        }
        missing = [d for d in ordered_document_ids if d not in nav_tabs_by_document_id]
        if missing:
            raise ValueError(f"Nav tabs not found: {', '.join(missing)}")

        for index, document_id in enumerate(ordered_document_ids):
            nav_tab = nav_tabs_by_document_id[document_id]
            nav_tab.order = index
            nav_tab.updated_at = _utc_now()

        db.commit()
        return get_nav_tabs_v2(db)

    except Exception:
        db.rollback()
        raise


def delete_nav_tab_v2(db: Session, document_id: str) -> dict[str, Any] | None:
    """Cascades: every root TabV2 under this nav tab is deleted (subtree and
    all) via delete_tab_subtree_by_document_id_v2 — which already handles
    variants, gridstack descendants, and components — before the nav_tabs
    row itself is deleted last, so tabs.nav_tab_id's FK never dangles."""
    try:
        nav_tab = get_nav_tab_by_document_id(db, document_id)
        if nav_tab is None:
            return None
        if nav_tab.protected:
            raise ValueError("The Dashboard nav tab cannot be deleted")

        root_tabs = (
            db.query(TabV2)
            .filter(TabV2.nav_tab_id == nav_tab.id, TabV2.parent_tab_id.is_(None))
            .all()
        )

        deleted_tabs: list[dict[str, Any]] = []
        search_updates: list[dict[str, Any]] = []
        for root_tab in root_tabs:
            if not root_tab.document_id:
                continue
            result = delete_tab_subtree_by_document_id_v2(db, root_tab.document_id)
            if result:
                deleted_tabs.extend(result.get("deleted_tabs", []))
                search_updates.extend(result.get("search_updates", []))

        db.delete(nav_tab)
        db.commit()

        return {
            "message": "Nav tab deleted successfully",
            "deleted_count": len(deleted_tabs),
            "deleted_tabs": deleted_tabs,
            "search_updates": search_updates,
        }

    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------
# Moving roots between nav tabs
# ---------------------------------------------------------

def move_tab_to_nav_tab_v2(
    db: Session,
    tab_document_id: str,
    nav_tab_document_id: str,
) -> dict[str, Any] | None:
    """Root tabs only — a sub-tab gridstack has no nav_tab_id of its own, and
    a variant follows its parent tab rather than moving independently."""
    try:
        gridstack = get_gridstack_by_document_id(db, tab_document_id)
        if gridstack is None:
            return None
        if not _is_root(gridstack):
            raise ValueError("Only root tabs can be moved between nav tabs")

        tab = _get_root_tab(db, gridstack)
        if tab is None:
            return None
        if tab.parent_tab_id is not None:
            raise ValueError(
                "A tab variant cannot be moved between nav tabs on its own — move its parent tab instead"
            )

        destination = get_nav_tab_by_document_id(db, nav_tab_document_id)
        if destination is None:
            raise ValueError("Destination nav tab does not exist")

        if destination.id == tab.nav_tab_id:
            raise ValueError("This tab already belongs to that nav tab")

        # Slug-based, not exact title: "Home" and "home" address the same
        # URL in the destination, so moving one next to the other would make
        # whichever loses the `.find()` race unreachable.
        conflict = _root_tab_with_conflicting_slug(db, tab.title, destination.id)
        if conflict is not None:
            raise ValueError(
                f'A tab addressed as "{_slugify_title(tab.title)}" already exists in the '
                f'destination nav tab ("{conflict.title}"). Titles differing only in '
                f"capitalisation or punctuation share one URL, so rename one of them first."
            )

        max_order = (
            db.query(func.max(TabV2.order))
            .filter(TabV2.nav_tab_id == destination.id, TabV2.parent_tab_id.is_(None))
            .scalar()
        )
        new_order = (max_order + 1) if max_order is not None else 0

        tab.nav_tab_id = destination.id
        tab.order = new_order
        tab.updated_at = _utc_now()
        gridstack.position = new_order

        # A variant carries the same nav_tab_id as its parent tab (see
        # create_tab_variant_v2) — moving the root must move them too, or a
        # later delete of the OLD nav tab would destroy variants that
        # visually now live under the NEW one.
        variant_tabs = db.query(TabV2).filter(TabV2.parent_tab_id == tab.id).all()
        for variant in variant_tabs:
            variant.nav_tab_id = destination.id
            variant.updated_at = _utc_now()

        # §5.2 / landmine 3: the only operation that changes a node's
        # position in the tree, so the only one where both invariants can
        # break in one write. Additive only — see apply_reparent_repair's
        # docstring; NT_A's stale grants are cleaned up via
        # reset_to_inherited, never automatically here.
        db.flush()
        apply_reparent_repair(db, NodeRef("tab", tab.id))

        affected_component_ids = _component_ids_for_gridstack_tree(
            db, gridstack, include_variants=True
        )
        db.commit()

        response = get_tab_workspace_v2(db, tab_document_id)
        if response is not None:
            response["search_updates"] = [
                {"component_id": component_id, "action": "upsert"}
                for component_id in affected_component_ids
            ]
        return response

    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------
# Preview / purge / reset (§5.4, §6.3, §6.4) — read-only preview plus the
# two reset affordances, for a nav tab addressed by document_id. Mirrors the
# hub's own trio in hub_service.py and the tab-level trio in
# gridstack_service.py; kept here rather than centralized since each needs
# its own document_id -> NodeRef lookup.
# ---------------------------------------------------------


def preview_nav_tab_access_v2(
    db: Session, document_id: str, access_control: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Read-only — backs the preview modal. Never writes."""
    nav_tab = get_nav_tab_by_document_id(db, document_id)
    if nav_tab is None:
        return None
    plan = plan_write(db, NodeRef("nav_tab", nav_tab.id), access_control)
    return describe_write_plan(db, plan)


def purge_nav_tab_principal_v2(
    db: Session, document_id: str, principal_payload: dict[str, Any]
) -> dict[str, Any] | None:
    try:
        nav_tab = get_nav_tab_by_document_id(db, document_id)
        if nav_tab is None:
            return None
        principal = principal_from_payload(principal_payload)
        touched = purge_principal(db, NodeRef("nav_tab", nav_tab.id), principal)
        db.commit()
        return {"touched": [node_summary(db, r) for r in touched]}
    except Exception:
        db.rollback()
        raise


def reset_nav_tab_access_v2(db: Session, document_id: str) -> dict[str, Any] | None:
    """Discards this nav tab's own access_control and re-derives it from the
    hub's effective AC (§6.4's "match the parent again"). The hub is never
    NULL post-migration, so there is no landmine-14-style walk here — but
    reset_to_inherited itself is generic and handles it either way."""
    try:
        nav_tab = get_nav_tab_by_document_id(db, document_id)
        if nav_tab is None:
            return None
        touched = reset_to_inherited(db, NodeRef("nav_tab", nav_tab.id))
        db.commit()
        return {"touched": [node_summary(db, r) for r in touched]}
    except Exception:
        db.rollback()
        raise
