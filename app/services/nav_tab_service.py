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

from app.services import edit_lock_service
from app.services.access_visibility_service import (
    ViewerAccess,
    require_edit,
    resolve_hub_node,
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

def _format_nav_tab(
    nav_tab: NavTabV2,
    *,
    access: ViewerAccess | None = None,
    lock_view: edit_lock_service.LockView | None = None,
) -> dict[str, Any]:
    summary = {
        "id": nav_tab.id,
        "documentId": nav_tab.document_id,
        "slug": nav_tab.slug,
        "title": nav_tab.title,
        "order": nav_tab.order if nav_tab.order is not None else 0,
        "access_control": _access_control_or_default(nav_tab.access_control),
        "protected": bool(nav_tab.protected),
        "icon": nav_tab.icon,
        # this session: the resource_grants node "Manage Access" edits.
        # Independent of `access` — set on every read.
        "node_kind": "nav_tab",
        "node_id": nav_tab.id,
        # plan_lock_propagation_2026-09-08.md §3.1 decision 3 — this node's
        # own raw lock row. New here (a nav tab had no lock columns before
        # this plan); the acquire/release side isn't wired to any route
        # until §8 phase 5, so these read as free/unlocked on every nav tab
        # until then, same as the columns' own default.
        "locked": bool(nav_tab.locked),
        "locked_by": nav_tab.locked_by or "",
        "locked_at": nav_tab.locked_at,
        "lock_is_stale": bool(nav_tab.locked) and edit_lock_service.is_lock_stale(nav_tab.locked_at),
    }
    if access is not None:
        # A nav tab maps straight to its own node — no gridstack indirection
        # to resolve, unlike a tab/sub-grid (see resolve_gridstack_node).
        verdict = access.verdict(("nav_tab", nav_tab.id))
        summary["view"] = verdict.view
        summary["edit"] = verdict.edit
        summary["revealed"] = verdict.revealed

    # §4.3 — additive, same "only when a caller passes one" convention as
    # the triple above.
    if lock_view is not None:
        lock_node_state = lock_view.state_for(("nav_tab", nav_tab.id))
        summary["lock_state"] = lock_node_state.state
        summary["lock_holder"] = lock_node_state.holder
        summary["lock_holder_node_label"] = lock_node_state.node_label
        summary["lock_expires_at"] = lock_node_state.expires_at
    return summary


# ---------------------------------------------------------
# Read API
# ---------------------------------------------------------

def get_nav_tabs_v2(
    db: Session,
    *,
    access: ViewerAccess | None = None,
    lock_view: edit_lock_service.LockView | None = None,
) -> list[dict[str, Any]]:
    nav_tabs = db.query(NavTabV2).order_by(NavTabV2.order, NavTabV2.id).all()
    summaries = [_format_nav_tab(t, access=access, lock_view=lock_view) for t in nav_tabs]
    # §5.2: an invisible nav tab is chrome nobody should see a row for.
    if access is not None:
        summaries = [s for s in summaries if s["view"]]
    return summaries


def get_nav_tab_by_document_id(db: Session, document_id: str) -> NavTabV2 | None:
    document_id = _validate_document_id_value(document_id, "documentId")
    return db.query(NavTabV2).filter(NavTabV2.document_id == document_id).first()


def get_dashboard_nav_tab(db: Session) -> NavTabV2 | None:
    """The single protected nav tab created by migrate_nav_tabs.py. Used as
    the fallback nav_tab_id for a root-tab create that doesn't specify one,
    so any existing caller (frontend or otherwise) keeps working unchanged."""
    return db.query(NavTabV2).filter(NavTabV2.protected.is_(True)).first()


# ---------------------------------------------------------
# Locking — plan_lock_propagation_2026-09-08.md §8 phase 5 / §6.7 decision 3
# ---------------------------------------------------------

def lock_nav_tab_by_document_id_v2(
    db: Session,
    document_id: str,
    locked_by: str,
    force: bool = False,
    *,
    access: ViewerAccess | None = None,
) -> dict[str, Any] | None:
    """THIN WRAPPER, mirroring `gridstack_service.lock_tab_by_document_id_v2`
    exactly — see that function's own docstring for why the conflict logic
    lives in `edit_lock_service.acquire` rather than here. Simpler than the
    tab version: a nav tab needs no gridstack indirection to find its lock
    node — `("nav_tab", nav_tab.id)` already IS one, no
    `resolve_lock_node` translation step required. `force` is §4.2 decision
    8's subtree takeover, unchanged in meaning from the tab route.

    plan_ac_enforcement_closeout_2026-09-09.md §3: gated on `edit(n)`, the
    same single check that also covers force-unlock — see
    `gridstack_service.lock_tab_by_document_id_v2`'s docstring for why the
    `edit(parent(n))` disjunct and the subtree-takeover case both collapse
    into it. No `resolve_lock_node`/`resolve_gridstack_node` split needed
    here (§3.3's trap): `("nav_tab", nav_tab.id)` is already the correct AC
    node, not a lock-tree-only ref."""
    nav_tab = get_nav_tab_by_document_id(db, document_id)
    if nav_tab is None:
        return None

    require_edit(access, ("nav_tab", nav_tab.id))

    grant = edit_lock_service.acquire(db, ("nav_tab", nav_tab.id), locked_by, force=force)
    formatted = _format_nav_tab(nav_tab)
    # §4.1's "return the token + expires_at" — added ONLY here, matching
    # TabWorkspaceResponse.lock_token's own "never leaks to a caller who
    # merely has view access" convention (schemas/tab.py).
    #
    # findings_dev_login_live_testing_2026-09-12.md addendum: `expires_at`
    # was the half of that comment's own promise this function never kept —
    # `_format_nav_tab(nav_tab)` bare (no `lock_view`) never sets
    # `lock_expires_at` at all (see its own docstring: additive, "only when
    # a caller passes one"), so every nav-tab lock response came back with
    # `lock_expires_at: null` regardless of a real, successful acquire.
    # The frontend gates `setEditSession` on `lock_token && lock_expires_at`
    # both being truthy (Sidebar.tsx), so that session was NEVER actually
    # registered — meaning `X-Edit-Tokens` was never sent on the follow-up
    # write, and every nav-tab rename/delete 409'd EDIT_SESSION_MISSING,
    # lock conflict or not. `grant.expires_at` is already computed by
    # `acquire` above — this was always available, just never assigned.
    formatted["lock_token"] = grant.token
    formatted["lock_expires_at"] = grant.expires_at
    return formatted


def unlock_nav_tab_by_document_id_v2(
    db: Session,
    document_id: str,
    unlocked_by: str | None = None,
    force: bool = False,
    *,
    access: ViewerAccess | None = None,
) -> dict[str, Any] | None:
    """THIN WRAPPER — see `lock_nav_tab_by_document_id_v2`'s own comment on
    why the logic lives in `edit_lock_service.release`. `force` here is the
    EXISTING unlock force (owner decision 2026-09-03: unrestricted, skips
    ownership AND staleness) — the same flag
    `gridstack_service.unlock_tab_by_document_id_v2` takes, unchanged by
    this plan.

    plan_ac_enforcement_closeout_2026-09-09.md §3: same single `edit(n)`
    gate as lock, covering force-unlock too."""
    nav_tab = get_nav_tab_by_document_id(db, document_id)
    if nav_tab is None:
        return None

    require_edit(access, ("nav_tab", nav_tab.id))

    edit_lock_service.release(db, ("nav_tab", nav_tab.id), unlocked_by or "", force=force)
    return _format_nav_tab(nav_tab)


# ---------------------------------------------------------
# Create / update / reorder / delete
# ---------------------------------------------------------

def create_nav_tab_v2(
    db: Session,
    title: str,
    access_control: dict[str, Any] | None = None,
    order: int | None = None,
    icon: str | None = None,
    *,
    access: ViewerAccess | None = None,
) -> dict[str, Any]:
    try:
        title = _validate_title(title)
        if not title:
            raise ValueError("Title is required")
        order = _validate_order(order)

        # plan §6.3 "create a child of n" → n, applied one level up from
        # `create_tab_v2`'s root-tab branch (project_ac_enforcement_gap.md's
        # §6.8 nav-tab/hub split): a new nav tab is a child of the SINGLE hub
        # row, not of anything the caller names. Checked before the slug
        # uniqueness/reserved-word query below, so a caller without edit
        # never learns whether a title collides.
        #
        # plan_lock_propagation_2026-09-08.md §9 item 1: NO session check
        # here, deliberately — the hub is never lockable, and the owner's
        # call is that creating a nav tab should never be blocked by
        # someone editing a DIFFERENT one. No `session` parameter on this
        # function at all, so there is nothing for a future call site to
        # accidentally wire up here.
        require_edit(access, resolve_hub_node(db))

        slug = _resolve_nav_slug(db, title)

        if order is None:
            max_order = db.query(func.max(NavTabV2.order)).scalar()
            order = (max_order + 1) if max_order is not None else 0

        now = _utc_now()
        nav_tab = NavTabV2(
            document_id=_generate_id(),
            slug=slug,
            title=title,
            order=order,
            # Whatever the caller asked for, or nothing. A new nav tab used
            # to inherit the hub's access_control; there is no inheritance
            # any more, so NULL means "no access_control set" rather than
            # standing in for an ancestor's value.
            access_control=access_control,
            protected=False,
            icon=icon,
            created_at=now,
            updated_at=now,
        )
        db.add(nav_tab)
        db.flush()

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
    *,
    access: ViewerAccess | None = None,
    session: edit_lock_service.EditSession | None = None,
) -> dict[str, Any] | None:
    """THREE OPERATIONS, ONE GATE EACH, project_ac_enforcement_gap.md's §6.8
    nav-tab/hub split — the same conflation
    `gridstack_service.update_tab_by_document_id_v2` already has for root
    tabs, one level up: this single PUT does rename + access_control-edit
    (both §6.3 "edit n's grants"/"rename n" → `edit(n)`, n = this nav tab)
    and reorder (`order` is how a nav tab's own sibling position is
    persisted — see `reorder_nav_tabs_v2` below — squarely §6.3's "reorder n
    → parent(n)", parent = the hub). Icon carries no AC weight of its own
    (§3.4 of the icon feature) and rides along with whichever check the
    OTHER fields present already require.

    Gated ONCE, on the STRICTEST requirement any field present implies,
    before touching anything — same reasoning as the root-tab function:
    `edit(parent(n)) ⟹ edit(n)` by §6.1's fold construction, so checking the
    stricter requirement when `order` is present also clears the looser one
    a bundled rename would otherwise need separately.
    """
    try:
        nav_tab = get_nav_tab_by_document_id(db, document_id)
        if nav_tab is None:
            return None

        title = _validate_title(title)
        order = _validate_order(order)

        gate_node = resolve_hub_node(db) if order is not None else ("nav_tab", nav_tab.id)
        require_edit(access, gate_node)
        # plan_lock_propagation_2026-09-08.md §5.4: mirrors
        # gridstack_service.update_tab_by_document_id_v2's identical split.
        # order present -> this PUT's own reorder shape, session-EXEMPT
        # (refused only if THIS nav tab is held fresh by someone else).
        # order absent -> rename/icon/AC-edit, §9 item 1's "nav-tab rename/
        # icon/delete... require a live session on that nav tab" — full
        # token check, and `gate_node` here already IS that nav tab (no
        # hub-divergence the way delete below has).
        if order is not None:
            if session is not None:
                edit_lock_service.refuse_if_any_held(db, [("nav_tab", nav_tab.id)], session.holder)
        else:
            if session is not None:
                edit_lock_service.require_live_session(session, gate_node, db)

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
            nav_tab.access_control = access_control

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


def reorder_nav_tabs_v2(
    db: Session,
    ordered_document_ids: list[str],
    *,
    access: ViewerAccess | None = None,
    holder: str | None = None,
) -> list[dict[str, Any]]:
    try:
        if not ordered_document_ids:
            raise ValueError("Reorder list cannot be empty")

        # plan §6.3 "reorder n → parent(n)" — unlike root-tab reordering
        # (`reorder_tabs_by_document_id_v2`), every nav tab shares the SAME
        # parent (the single hub row), so there is no per-item resolution
        # needed: one gate, checked before the existence lookup below so a
        # caller without edit never learns which of the named document_ids
        # are real.
        require_edit(access, resolve_hub_node(db))

        nav_tabs_by_document_id = {
            t.document_id: t
            for t in db.query(NavTabV2)
            .filter(NavTabV2.document_id.in_(ordered_document_ids))
            .all()
        }
        missing = [d for d in ordered_document_ids if d not in nav_tabs_by_document_id]
        if missing:
            raise ValueError(f"Nav tabs not found: {', '.join(missing)}")

        # plan_lock_propagation_2026-09-08.md §5.4/§9 item 1: the hub-gated
        # permission check above stays exactly as it is (creating/reordering
        # nav tabs is edit(hub), and the hub is never lockable) — but §5.4's
        # conflict check still applies uniformly across all four reorder
        # surfaces, so a nav tab actually held fresh by someone else still
        # refuses this.
        if holder:
            edit_lock_service.refuse_if_any_held(
                db,
                [("nav_tab", t.id) for t in nav_tabs_by_document_id.values()],
                holder,
            )

        for index, document_id in enumerate(ordered_document_ids):
            nav_tab = nav_tabs_by_document_id[document_id]
            nav_tab.order = index
            nav_tab.updated_at = _utc_now()

        db.commit()
        return get_nav_tabs_v2(db)

    except Exception:
        db.rollback()
        raise


def delete_nav_tab_v2(
    db: Session,
    document_id: str,
    *,
    access: ViewerAccess | None = None,
    session: edit_lock_service.EditSession | None = None,
) -> dict[str, Any] | None:
    """Cascades: every root TabV2 under this nav tab is deleted (subtree and
    all) via delete_tab_subtree_by_document_id_v2 — which already handles
    variants, gridstack descendants, and components — before the nav_tabs
    row itself is deleted last, so tabs.nav_tab_id's FK never dangles.

    Gated on `edit(parent(n))` = `edit(hub)` — §6.3's delete row — checked
    ONCE, at this boundary, not once per cascaded root tab: the same
    reasoning `delete_tab_subtree_by_document_id_v2` already gives for its
    own single check (being entrusted with the parent is being entrusted
    with the whole subtree, cascade included). The cascade calls that
    function WITHOUT `access` (see below), matching every other internal
    caller in this design — `access=None` means "no check requested", not
    "deny" (access_visibility_service.require_edit's own docstring).

    Checked BEFORE the `protected` business rule, not instead of it: an
    AC-gated caller who cannot edit the hub gets 404/403 without ever
    learning whether the row is protected; a caller who CAN edit the hub
    still hits the protected refusal below exactly as before this session.
    """
    try:
        nav_tab = get_nav_tab_by_document_id(db, document_id)
        if nav_tab is None:
            return None
        require_edit(access, resolve_hub_node(db))
        # plan_lock_propagation_2026-09-08.md §9 item 1: the permission
        # gate above stays edit(hub) (§6.3's own "delete n -> parent(n)"),
        # UNCHANGED — but the hub has no lock node to check a session
        # against (it is never lockable), so the SESSION check deliberately
        # targets the nav tab being deleted itself, not its AC parent:
        # "nav-tab rename/icon/delete... require a live session on that nav
        # tab." This is the one call site in this module where the session
        # target and the permission gate are NOT the same node.
        if session is not None:
            edit_lock_service.require_live_session(session, ("nav_tab", nav_tab.id), db)
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
    *,
    access: ViewerAccess | None = None,
    session: edit_lock_service.EditSession | None = None,
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

        # plan §6.3 "move n → parent(n)" — the SAME two-parent judgement
        # call `move_tab_by_document_id_v2` makes at the gridstack level
        # (project_ac_enforcement_gap.md item 2 §3: a move has a source AND
        # a destination, unlike delete/reorder's single parent), applied one
        # level up. A root tab's true AC parent is its nav tab —
        # `NodeTree`'s own branch, `("tab", id) -> ("nav_tab", nav_tab_id)`
        # when there is no `parent_tab_id` — so moving it between nav tabs
        # requires edit on BOTH: the OLD one (content is leaving it) and the
        # NEW one (content is arriving in it), the same bar a file-system
        # move sets for source and destination directories. Checked after
        # the structural validations above (root/variant, destination
        # exists, not a no-op) so a caller without edit never learns those
        # passed, and before the slug-conflict query below for the same
        # reason create_tab_v2 checks edit before its own conflict query.
        #
        # Variants need NO separate check here, unlike delete's cascade into
        # them: a variant's true AC parent is the ROOT TAB it varies
        # (`TabV2.parent_tab_id`, §3.1), not the nav tab it happens to share
        # `nav_tab_id` with — moving the root changes the ROOT's AC parent,
        # not the variant's, so `variant_tabs` below get their `nav_tab_id`
        # column updated (placement metadata, matching `create_tab_variant_v2`)
        # without crossing any AC boundary of their own.
        require_edit(access, ("nav_tab", tab.nav_tab_id) if tab.nav_tab_id is not None else None)
        require_edit(access, ("nav_tab", destination.id))
        # A move is not a reorder — full session check on BOTH nav tabs,
        # mirroring gridstack_service.move_tab_by_document_id_v2's own
        # two-parent treatment.
        if session is not None and tab.nav_tab_id is not None:
            edit_lock_service.require_live_session(session, ("nav_tab", tab.nav_tab_id), db)
        if session is not None:
            edit_lock_service.require_live_session(session, ("nav_tab", destination.id), db)

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

        db.flush()

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
