"""
Access-control propagation engine (phase 2 — see
AI Docs/plan_nav_tabs_phase2_2026-07-30.md).

Owns the whole model described in the plan's §3: two invariants over the tab
family (hub -> nav tab -> root tab -> variant -> sub-tab gridstack) —
`admins` accumulates downward, `viewers` accumulates upward — repaired on
every write so every READ stays a single-node lookup. Components (canvas
widgets, Super Block Note sub-tabs) are explicitly OUTSIDE this engine; they
are subset-constrained against a resolved ancestor instead (§3.4), which is
why `NodeRef` below has no "component" kind.

Import direction: models <- access_control_service <- gridstack_service <-
nav_tab_service. This module must NEVER import from gridstack_service (that
would make the dependency circular, since gridstack_service is going to call
into this module for apply_write/plan_write). Two small helpers that would
otherwise live in gridstack_service are defined here instead
(`get_descendant_gridstack_ids`, `_get_gridstack_component`) and re-imported
there, so there is exactly one implementation of each — matching the
`_sbn_node_info` / `_has_sbn_children` precedent already set in this codebase
for the identical circularity between gridstack_service and
super_blocknote_service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy.orm import Session

from app.db_v2.models.component import ComponentV2
from app.db_v2.models.gridstack import GridstackV2
from app.db_v2.models.hub import HubV2
from app.db_v2.models.nav_tab import NavTabV2
from app.db_v2.models.tab import TabV2

from app.services.tab_service import (
    DEFAULT_ACCESS_CONTROL,
    HUB_ADMIN_ROLE,
    _intersect_group,
    _role_identity,
    _SCOPED_ROLE_SCOPES,
)

# A component's representation type — duplicated here (rather than imported
# from gridstack_service, which would be circular) since it's a two-value
# tuple, not logic. Kept in sync with GRIDSTACK_REPRESENTATION_TYPES there;
# only used by _create_gridstack_component's own sgs-flag mirroring below.
_GRIDSTACK_WIDGET_TYPE = "gridstack"
_SUPER_GRIDSTACK_WIDGET_TYPE = "super_gridstack"


# ---------------------------------------------------------
# NodeRef — the one abstraction spanning four storage locations
# ---------------------------------------------------------

NodeKind = Literal["hub", "nav_tab", "tab", "gridstack"]


@dataclass(frozen=True, order=True)
class NodeRef:
    kind: NodeKind
    id: int


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------
# Principal algebra
# ---------------------------------------------------------
#
# A principal is a user (identity: lowercased email) or a role (identity:
# the (name, scope, program, function) tuple _role_identity already
# produces). `to_principals`/`from_principals` convert a stored {"users":
# [...], "roles": [...]} group to/from a {identity: original_dict} mapping —
# keying on identity (not the dict itself) is what makes set algebra
# (union/difference) possible, while keeping the original dict around lets a
# round trip re-embed the exact stored shape rather than a synthesized one.

Principal = tuple


def _user_principal(entry: dict[str, Any]) -> Principal:
    return ("user", (entry.get("email") or "").strip().lower())


def _role_principal(entry: dict[str, Any]) -> Principal:
    return ("role", *_role_identity(entry))


def to_principals(group: dict[str, Any] | None) -> dict[Principal, dict[str, Any]]:
    group = group or {}
    out: dict[Principal, dict[str, Any]] = {}
    for u in group.get("users") or []:
        if isinstance(u, dict):
            out[_user_principal(u)] = u
    for r in group.get("roles") or []:
        if isinstance(r, dict):
            out[_role_principal(r)] = r
    return out


def from_principals(principals: dict[Principal, dict[str, Any]]) -> dict[str, list[Any]]:
    users = [v for k, v in principals.items() if k[0] == "user"]
    roles = [v for k, v in principals.items() if k[0] == "role"]
    return {"users": users, "roles": roles}


# ---------------------------------------------------------
# Principal wire (de)serialization -- for callers outside this module (the
# purge endpoint, the preview response) that need to build/describe a
# Principal from/for JSON without reaching into the private
# _user_principal/_role_principal constructors directly.
# ---------------------------------------------------------


def principal_from_payload(payload: dict[str, Any]) -> Principal:
    """Inverse of `principal_payload` below. A purge target is named by
    TYPE (user vs role) rather than by which group it currently sits in --
    the same principal may be an admin, a viewer, or both, and purge strips
    both."""
    kind = payload.get("type")
    if kind == "user":
        return _user_principal({"email": payload.get("email", "")})
    if kind == "role":
        return _role_principal(
            {
                "name": payload.get("name", ""),
                "scope": payload.get("scope", ""),
                "program": payload.get("program", ""),
                "function": payload.get("function", ""),
            }
        )
    raise ValueError(f"Unknown principal type: {kind!r}")


def principal_payload(principal: Principal, original: dict[str, Any]) -> dict[str, Any]:
    """The wire shape for one `grantedTo`/`revokedFrom` entry (plan §5.5).
    `original` is the stored dict `plan_write` already carries alongside the
    identity tuple, so this is a tag, not a new lookup."""
    if principal[0] == "user":
        return {"type": "user", "email": original.get("email", "")}
    return {
        "type": "role",
        "name": original.get("name", ""),
        "scope": original.get("scope", ""),
        "program": original.get("program", ""),
        "function": original.get("function", ""),
    }


def _group_is_universal(group: dict[str, Any] | None) -> bool:
    """Empty (no users, no roles) is the special universal set for a
    VIEWERS group (§3.3) — everyone passes. Never call this for an admins
    group: empty admins is a restrictive default (Hub Admins only), not a
    broadening one, and has no such meaning."""
    group = group or {}
    return not (group.get("users") or []) and not (group.get("roles") or [])


def _union_principals_into_group(
    current_group: dict[str, Any] | None,
    principals_to_add: dict[Principal, dict[str, Any]],
    *,
    is_viewers: bool,
) -> dict[str, Any]:
    """The write-time repair's core mutation: add `principals_to_add` to a
    stored group.

    Landmine 1 — the single highest-risk piece of this engine: an empty
    VIEWERS group means "open to everyone" (§3.3), the universal set. Union
    is not union there — union(universal, {alice}) is still universal, and
    naively writing {alice} into a currently-empty viewers group would
    silently narrow a public node down to one person. So for viewers, a
    currently-universal group is left untouched (still empty, still
    universal) rather than merged. Admins has no such reading of empty, so
    admins is always a plain merge.
    """
    current_group = current_group or {}
    if is_viewers and _group_is_universal(current_group):
        return current_group
    if not principals_to_add:
        return current_group
    merged = {**to_principals(current_group), **principals_to_add}
    return from_principals(merged)


def _subtract_principals_from_group(
    current_group: dict[str, Any] | None,
    principals_to_remove: dict[Principal, dict[str, Any]],
) -> dict[str, Any]:
    """Plain set difference. Deliberately has no universal-group special
    case: subtracting from an empty (universal) group is already a no-op
    under plain difference, which is also the only sound approximation of
    "everyone minus alice" this data model can represent (see the module
    docstring's note on invariant B during a universal->restricted
    transition) — it fails open rather than silently mis-narrowing."""
    if not principals_to_remove:
        return current_group or {}
    current_principals = to_principals(current_group)
    remaining = {k: v for k, v in current_principals.items() if k not in principals_to_remove}
    return from_principals(remaining)


# ---------------------------------------------------------
# Evaluation — the only two functions permitted to decide access (§5.1)
# ---------------------------------------------------------


def _group_contains(
    group: dict[str, Any] | None, email: str, role_names: list[str]
) -> bool:
    """Plain membership — no universal-empty reading. Used for `admins`."""
    group = group or {}
    email_norm = (email or "").strip().lower()
    users = group.get("users") or []
    if any(
        isinstance(u, dict) and (u.get("email") or "").strip().lower() == email_norm
        for u in users
    ):
        return True
    roles = group.get("roles") or []
    for role_obj in roles:
        if not isinstance(role_obj, dict):
            continue
        name = role_obj.get("name", "")
        scope = (role_obj.get("scope") or "").strip()
        # The JWT can't resolve Program/Function-scoped assignments (no
        # Airtable round-trip here) — same skip _user_can_view_widget uses.
        if name in role_names and scope not in _SCOPED_ROLE_SCOPES:
            return True
    return False


def _group_contains_or_universal(
    group: dict[str, Any] | None, email: str, role_names: list[str]
) -> bool:
    """Membership with the viewers-only "empty = everyone" reading (§3.3).
    Mirrors tab_service._user_can_view_widget exactly, generalised from a
    widget's `viewers` group to any node's."""
    if _group_is_universal(group):
        return True
    return _group_contains(group, email, role_names)


def can_view(ac: dict[str, Any] | None, email: str, role_names: list[str]) -> bool:
    role_names = role_names or []
    if HUB_ADMIN_ROLE in role_names:
        return True
    ac = ac or {}
    return _group_contains_or_universal(ac.get("viewers"), email, role_names)


def can_edit(ac: dict[str, Any] | None, email: str, role_names: list[str]) -> bool:
    """Rule C: `can_edit(N, P) = P in admins(N) AND P in viewers(N)`,
    evaluated (never stored — see the module docstring). The Hub-Admin-role
    bypass at the top matches every other admin gate in this product
    (`filter_widget_content_for_user`, the frontend's `canEditTab`/
    `canEdit`) and is also what realises "empty admins => Hub Admins only"
    (§3.3): with no bypass, an empty admins group would make even the
    literal Hub Admin role fail `_group_contains`."""
    role_names = role_names or []
    if HUB_ADMIN_ROLE in role_names:
        return True
    if not can_view(ac, email, role_names):
        return False
    ac = ac or {}
    return _group_contains(ac.get("admins"), email, role_names)


# ---------------------------------------------------------
# Lookup helpers duplicated from gridstack_service to avoid the circular
# import (see module docstring). Kept intentionally tiny.
# ---------------------------------------------------------


def _get_gridstack_component(db: Session, gridstack_id: int) -> ComponentV2 | None:
    return db.query(ComponentV2).filter(ComponentV2.current_grid_id == gridstack_id).first()


def _create_gridstack_component(db: Session, gridstack: GridstackV2) -> ComponentV2:
    """Same fallback `update_tab_by_document_id_v2` already has for a
    sub-tab gridstack created before the current_grid_id backfill. Never
    called for a root/variant's own gridstack (its AC lives on the TabV2
    row) — see `write_ac`'s "gridstack" branch."""
    sgs = (gridstack.settings or {}).get("sgs")
    component = ComponentV2(
        link=str(uuid4()),
        title=None,
        description=None,
        type=_SUPER_GRIDSTACK_WIDGET_TYPE if sgs else _GRIDSTACK_WIDGET_TYPE,
        x=None,
        y=None,
        width=None,
        height=None,
        props={"sgs": sgs} if sgs else None,
        access_control=None,
        current_grid_id=gridstack.id,
        gridstack_id=gridstack.id,
        page_content_id=None,
        super_blocknote_id=None,
    )
    db.add(component)
    db.flush()
    return component


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


# ---------------------------------------------------------
# The hub singleton
# ---------------------------------------------------------


def get_hub(db: Session) -> HubV2 | None:
    return db.query(HubV2).order_by(HubV2.id).first()


def hub_ref(db: Session) -> NodeRef:
    hub = get_hub(db)
    if hub is None:
        raise ValueError(
            "The hub row does not exist — run scripts/migrate_hub_ac_propagation.py"
        )
    return NodeRef("hub", hub.id)


def _hub_fallback_ac(db: Session) -> dict[str, Any]:
    """Used only when a resolution walk runs off the top of the tree without
    finding a non-NULL AC and without even a hub row (should not happen
    post-migration; defensive default only)."""
    hub = get_hub(db)
    if hub is not None and hub.access_control:
        return hub.access_control
    return DEFAULT_ACCESS_CONTROL


# ---------------------------------------------------------
# Per-kind read/write dispatch (§5.1)
# ---------------------------------------------------------


def read_ac(db: Session, ref: NodeRef) -> dict[str, Any] | None:
    if ref.kind == "hub":
        row = db.query(HubV2).filter(HubV2.id == ref.id).first()
        return row.access_control if row else None
    if ref.kind == "nav_tab":
        row = db.query(NavTabV2).filter(NavTabV2.id == ref.id).first()
        return row.access_control if row else None
    if ref.kind == "tab":
        row = db.query(TabV2).filter(TabV2.id == ref.id).first()
        return row.access_control if row else None
    if ref.kind == "gridstack":
        component = _get_gridstack_component(db, ref.id)
        return component.access_control if component else None
    raise ValueError(f"Unknown NodeRef kind: {ref.kind!r}")


def write_ac(db: Session, ref: NodeRef, ac: dict[str, Any] | None) -> None:
    if ref.kind == "hub":
        row = db.query(HubV2).filter(HubV2.id == ref.id).first()
        if row is None:
            raise ValueError("Hub row does not exist")
        row.access_control = ac
        row.updated_at = _utc_now()
        return
    if ref.kind == "nav_tab":
        row = db.query(NavTabV2).filter(NavTabV2.id == ref.id).first()
        if row is None:
            raise ValueError("Nav tab does not exist")
        row.access_control = ac
        row.updated_at = _utc_now()
        return
    if ref.kind == "tab":
        row = db.query(TabV2).filter(TabV2.id == ref.id).first()
        if row is None:
            raise ValueError("Tab does not exist")
        row.access_control = ac
        row.updated_at = _utc_now()
        return
    if ref.kind == "gridstack":
        gridstack = db.query(GridstackV2).filter(GridstackV2.id == ref.id).first()
        if gridstack is None:
            raise ValueError("Gridstack does not exist")
        component = _get_gridstack_component(db, ref.id)
        if component is None:
            component = _create_gridstack_component(db, gridstack)
        component.access_control = ac
        return
    raise ValueError(f"Unknown NodeRef kind: {ref.kind!r}")


# ---------------------------------------------------------
# Tree traversal (§4.2) — set-based, never a row-by-row walk
# ---------------------------------------------------------


def _parent_of(db: Session, ref: NodeRef) -> NodeRef | None:
    if ref.kind == "hub":
        return None
    if ref.kind == "nav_tab":
        return hub_ref(db)
    if ref.kind == "tab":
        tab = db.query(TabV2).filter(TabV2.id == ref.id).first()
        if tab is None:
            return None
        if tab.parent_tab_id is not None:
            return NodeRef("tab", tab.parent_tab_id)
        if tab.nav_tab_id is not None:
            return NodeRef("nav_tab", tab.nav_tab_id)
        # nav_tab_id IS NULL — an orphan root outside the materialized tree
        # (plan §7 step 3b / landmine 13). No parent visible to this engine.
        return None
    if ref.kind == "gridstack":
        gridstack = db.query(GridstackV2).filter(GridstackV2.id == ref.id).first()
        if gridstack is None:
            return None
        if gridstack.parent_id is not None:
            immediate_parent = (
                db.query(GridstackV2).filter(GridstackV2.id == gridstack.parent_id).first()
            )
            if immediate_parent is not None and immediate_parent.parent_id is None:
                # `ref` is a TOP sub-tab gridstack (§4.2's table) — its
                # immediate parent gridstack is itself a root/variant's own
                # canvas, which isn't a node in the propagation tree (its AC
                # lives on the owning TabV2, see below). Skip that hop
                # entirely rather than returning a "gridstack" ref for it:
                # its representation component is never written by write_ac
                # for a root/variant (see _create_gridstack_component's own
                # comment) and stays permanently NULL, so treating it as a
                # real ancestor would make plan_write/apply_write write a
                # degenerate {"viewers": {}}-shaped value into it — which
                # read_ac/_resolve_ac_upward would then treat as "found,
                # stop here" instead of correctly walking up to the TabV2.
                return NodeRef("tab", immediate_parent.parent_tab_id)
            return NodeRef("gridstack", gridstack.parent_id)
        # This gridstack is itself a root/variant's own canvas (parent_id
        # NULL). That canvas isn't a node in the propagation tree — its AC
        # lives on the owning TabV2 — so hand back "tab", not "gridstack".
        return NodeRef("tab", gridstack.parent_tab_id)
    raise ValueError(f"Unknown NodeRef kind: {ref.kind!r}")


def ancestors(db: Session, ref: NodeRef) -> list[NodeRef]:
    """Nearest-first. Empty if `ref` is the hub, or an orphan root."""
    result: list[NodeRef] = []
    seen: set[NodeRef] = set()
    current = _parent_of(db, ref)
    while current is not None and current not in seen:
        seen.add(current)
        result.append(current)
        current = _parent_of(db, current)
    return result


def descendants(db: Session, ref: NodeRef) -> list[NodeRef]:
    """Set-based per §4.2 — a handful of bulk queries, never a per-row walk."""
    if ref.kind == "hub":
        nav_tab_ids = [row[0] for row in db.query(NavTabV2.id).all()]
        result = [NodeRef("nav_tab", nid) for nid in nav_tab_ids]
        for nid in nav_tab_ids:
            result.extend(descendants(db, NodeRef("nav_tab", nid)))
        return result

    if ref.kind == "nav_tab":
        root_ids = [
            row[0]
            for row in db.query(TabV2.id)
            .filter(TabV2.nav_tab_id == ref.id, TabV2.parent_tab_id.is_(None))
            .all()
        ]
        result = [NodeRef("tab", rid) for rid in root_ids]
        variant_ids: list[int] = []
        if root_ids:
            variant_ids = [
                row[0]
                for row in db.query(TabV2.id).filter(TabV2.parent_tab_id.in_(root_ids)).all()
            ]
        result.extend(NodeRef("tab", vid) for vid in variant_ids)
        all_tab_ids = root_ids + variant_ids
        if all_tab_ids:
            gridstack_ids = [
                row[0]
                for row in db.query(GridstackV2.id)
                .filter(
                    GridstackV2.parent_tab_id.in_(all_tab_ids),
                    GridstackV2.parent_id.isnot(None),
                )
                .all()
            ]
            result.extend(NodeRef("gridstack", gid) for gid in gridstack_ids)
        return result

    if ref.kind == "tab":
        tab = db.query(TabV2).filter(TabV2.id == ref.id).first()
        if tab is None:
            return []
        result = []
        tab_ids = [ref.id]
        if tab.parent_tab_id is None:
            # ref is a root — its variants are descendants too (a variant
            # may never itself have variants, so this doesn't recurse).
            variant_ids = [
                row[0]
                for row in db.query(TabV2.id).filter(TabV2.parent_tab_id == ref.id).all()
            ]
            result.extend(NodeRef("tab", vid) for vid in variant_ids)
            tab_ids.extend(variant_ids)
        gridstack_ids = [
            row[0]
            for row in db.query(GridstackV2.id)
            .filter(
                GridstackV2.parent_tab_id.in_(tab_ids),
                GridstackV2.parent_id.isnot(None),
            )
            .all()
        ]
        result.extend(NodeRef("gridstack", gid) for gid in gridstack_ids)
        return result

    if ref.kind == "gridstack":
        return [NodeRef("gridstack", gid) for gid in get_descendant_gridstack_ids(db, ref.id)]

    raise ValueError(f"Unknown NodeRef kind: {ref.kind!r}")


# ---------------------------------------------------------
# Node summaries + write-plan description (§5.5) -- the preview modal's
# wire contract. plan_write/purge_principal/reset_to_inherited stay pure
# NodeRef/Principal algebra; this is the one place that renders those as
# {kind, documentId, title} for display.
# ---------------------------------------------------------


def node_summary(db: Session, ref: NodeRef) -> dict[str, Any]:
    """{kind, documentId, title} for one NodeRef. The four kinds don't share
    a title column: TabV2/NavTabV2 have `title`, GridstackV2 has `name`
    (matching gridstack_service's own `title = gridstack.name` convention),
    and HubV2 has neither -- there is exactly one hub, so its title is a
    literal."""
    if ref.kind == "hub":
        hub = db.query(HubV2).filter(HubV2.id == ref.id).first()
        return {"kind": "hub", "documentId": hub.document_id if hub else None, "title": "Hub"}
    if ref.kind == "nav_tab":
        row = db.query(NavTabV2).filter(NavTabV2.id == ref.id).first()
        return {
            "kind": "nav_tab",
            "documentId": row.document_id if row else None,
            "title": row.title if row else None,
        }
    if ref.kind == "tab":
        row = db.query(TabV2).filter(TabV2.id == ref.id).first()
        return {
            "kind": "tab",
            "documentId": row.document_id if row else None,
            "title": row.title if row else None,
        }
    if ref.kind == "gridstack":
        row = db.query(GridstackV2).filter(GridstackV2.id == ref.id).first()
        return {
            "kind": "gridstack",
            "documentId": row.document_id if row else None,
            "title": row.name if row else None,
        }
    raise ValueError(f"Unknown NodeRef kind: {ref.kind!r}")


def _principal_group_entries(
    db: Session,
    targets: dict[NodeRef, dict[Principal, dict[str, Any]]],
    group: Literal["admins", "viewers"],
) -> list[dict[str, Any]]:
    """Inverts a WritePlan target map (NodeRef -> {principal: original}) into
    one entry per principal (principal -> [NodeRef, ...]) -- the preview
    modal's sentence is "this restores edit for Alice on X, Y, Z", not "node
    X grants {alice, bob}", so the response is principal-first, not
    node-first."""
    nodes_by_principal: dict[Principal, list[NodeRef]] = {}
    original_by_principal: dict[Principal, dict[str, Any]] = {}
    for node, principals in targets.items():
        for principal, original in principals.items():
            nodes_by_principal.setdefault(principal, []).append(node)
            original_by_principal[principal] = original

    return [
        {
            "principal": principal_payload(principal, original_by_principal[principal]),
            "group": group,
            "nodes": [node_summary(db, n) for n in sorted(nodes, key=lambda r: (r.kind, r.id))],
        }
        for principal, nodes in nodes_by_principal.items()
    ]


def describe_write_plan(db: Session, plan: "WritePlan") -> dict[str, Any]:
    """The preview modal's wire response (§5.5, §6.3). Combines admin and
    viewer grants/revocations into one principal-first list each, tagged by
    `group`, rather than mirroring plan_write's four separate maps verbatim.

    `reactivatedEdit` isn't in §5.5's one-line schema sketch -- it's the
    whole reason the modal is mandatory (§6.3, landmine 9): a view grant can
    silently un-dormant an existing `admins` entry, and this is the one
    field that lets the modal say so before the write happens."""
    return {
        "touched": [node_summary(db, r) for r in plan.touched],
        "grantedTo": (
            _principal_group_entries(db, plan.admin_grants, "admins")
            + _principal_group_entries(db, plan.viewer_grants, "viewers")
        ),
        "revokedFrom": (
            _principal_group_entries(db, plan.admin_revocations, "admins")
            + _principal_group_entries(db, plan.viewer_revocations, "viewers")
        ),
        "narrowed": [
            node_summary(db, r) for r in sorted(plan.viewer_narrows, key=lambda r: (r.kind, r.id))
        ],
        "reactivatedEdit": [node_summary(db, r) for r in plan.reactivated_edit],
    }


# ---------------------------------------------------------
# Component parent resolution (§3.4) — NOT part of the NodeRef tree; a
# component's AC is subset-constrained against this, never propagated.
# ---------------------------------------------------------


def _resolve_ac_upward(db: Session, ref: NodeRef) -> dict[str, Any]:
    ac = read_ac(db, ref)
    if ac:
        return ac
    parent = _parent_of(db, ref)
    if parent is None:
        return _hub_fallback_ac(db)
    return _resolve_ac_upward(db, parent)


def resolved_ac_for_gridstack(db: Session, gridstack: GridstackV2) -> dict[str, Any]:
    """The effective ceiling (§3.4) for a component sitting directly on
    `gridstack`'s own canvas — the same NULL-skipping resolution
    `resolved_parent_ac` uses for a widget's `gridstack_id`, factored out so
    a bulk caller (one canvas save carrying many widgets) can compute it
    once rather than once per widget."""
    if gridstack.parent_id is None:
        # `gridstack` is a root/variant's own canvas — not a node in the
        # propagation tree (its AC lives on the owning TabV2, see
        # _parent_of's gridstack branch). Skip straight to the TabV2.
        ref = NodeRef("tab", gridstack.parent_tab_id)
    else:
        # `gridstack` is itself a sub-tab node with its own representation
        # component — resolve starting there.
        ref = NodeRef("gridstack", gridstack.id)
    return _resolve_ac_upward(db, ref)


def resolved_parent_ac(db: Session, component: ComponentV2) -> dict[str, Any]:
    """The effective ceiling a component's own `access_control` is checked
    against (§3.4) — the NEAREST ANCESTOR with a non-NULL access_control,
    never the literal immediate parent (landmine 14: comparing against an
    unresolved NULL parent passes vacuously, since empty reads as universal
    — §3.3 — so the check would validate nothing).

    Walks: SBN parent chain -> the owning gridstack -> its sub-tab
    representation chain -> the owning TabV2 (root or variant) -> its nav
    tab -> the hub.
    """
    if component.super_blocknote_id is not None:
        parent = (
            db.query(ComponentV2).filter(ComponentV2.id == component.super_blocknote_id).first()
        )
        if parent is None:
            return _hub_fallback_ac(db)
        if parent.access_control:
            return parent.access_control
        return resolved_parent_ac(db, parent)

    gridstack = db.query(GridstackV2).filter(GridstackV2.id == component.gridstack_id).first()
    if gridstack is None:
        return _hub_fallback_ac(db)

    return resolved_ac_for_gridstack(db, gridstack)


# ---------------------------------------------------------
# plan_write / apply_write — the four repairs (§3.2)
# ---------------------------------------------------------


@dataclass
class WritePlan:
    ref: NodeRef
    next_ac: dict[str, Any]
    touched: list[NodeRef]
    admin_grants: dict[NodeRef, dict[Principal, dict[str, Any]]]
    viewer_grants: dict[NodeRef, dict[Principal, dict[str, Any]]]
    admin_revocations: dict[NodeRef, dict[Principal, dict[str, Any]]]
    viewer_revocations: dict[NodeRef, dict[Principal, dict[str, Any]]]
    viewer_narrows: dict[NodeRef, dict[str, Any]]
    reactivated_edit: list[NodeRef]


def _add_targets(
    target_map: dict[NodeRef, dict[Principal, dict[str, Any]]],
    nodes: list[NodeRef],
    principals: dict[Principal, dict[str, Any]],
) -> None:
    if not principals:
        return
    for node in nodes:
        target_map.setdefault(node, {}).update(principals)


def plan_write(db: Session, ref: NodeRef, next_ac: dict[str, Any] | None) -> WritePlan:
    """Pure — computes what a write of `next_ac` to `ref` would touch,
    without writing anything. Diffs `next_ac` against `ref`'s CURRENTLY
    STORED access_control, then applies the four repairs from §3.2 in their
    mandated order: edit added, view added, edit removed, view removed. Plus
    a fifth, added this session and not in §3.2's original text (see the
    2026-07-30 phase-2 handoff's "open design item"): a VIEWERS group's own
    transition from universal (empty, §3.3) to a named, restricted set has
    no named principal to diff as "removed", so rule 4 never fires for it —
    every descendant would otherwise keep reading as universal forever.
    Rule 5 below closes that gap."""
    prev_ac = read_ac(db, ref) or {}
    next_ac = next_ac or {}

    prev_admins = to_principals(prev_ac.get("admins"))
    next_admins = to_principals(next_ac.get("admins"))
    prev_viewers = to_principals(prev_ac.get("viewers"))
    next_viewers = to_principals(next_ac.get("viewers"))

    added_admins = {k: v for k, v in next_admins.items() if k not in prev_admins}
    removed_admins = {k: v for k, v in prev_admins.items() if k not in next_admins}
    added_viewers = {k: v for k, v in next_viewers.items() if k not in prev_viewers}
    removed_viewers = {k: v for k, v in prev_viewers.items() if k not in next_viewers}

    desc = descendants(db, ref)
    anc = ancestors(db, ref)

    admin_grants: dict[NodeRef, dict[Principal, dict[str, Any]]] = {}
    viewer_grants: dict[NodeRef, dict[Principal, dict[str, Any]]] = {}
    admin_revocations: dict[NodeRef, dict[Principal, dict[str, Any]]] = {}
    viewer_revocations: dict[NodeRef, dict[Principal, dict[str, Any]]] = {}

    # Rule 1: edit added -> admins(N + descendants); then, so the grant is
    # usable (rule C), viewers(N + descendants + ancestors).
    if added_admins:
        _add_targets(admin_grants, [ref, *desc], added_admins)
        _add_targets(viewer_grants, [ref, *desc, *anc], added_admins)

    # Rule 2: view added -> viewers(N + ancestors).
    if added_viewers:
        _add_targets(viewer_grants, [ref, *anc], added_viewers)

    # Rule 3: edit removed -> admins(N + ancestors). Never descendants.
    if removed_admins:
        _add_targets(admin_revocations, [ref, *anc], removed_admins)

    # Rule 4: view removed -> viewers(N + descendants). admins untouched.
    if removed_viewers:
        _add_targets(viewer_revocations, [ref, *desc], removed_viewers)

    # Rule 5: viewers(N) narrows from universal to a named set S. Mutually
    # exclusive with rule 4 (removed_viewers is structurally empty whenever
    # prev.viewers was universal, since there are no named principals to
    # diff out of it) so the two never fire on the same write. Targets only
    # DESCENDANTS — not `ref` (already gets S verbatim) and not ancestors
    # (an ancestor was already at least as open as N was before N narrowed,
    # so narrowing N can't newly violate invariant B at an ancestor edge).
    viewer_narrows: dict[NodeRef, dict[str, Any]] = {}
    if _group_is_universal(prev_ac.get("viewers")) and not _group_is_universal(
        next_ac.get("viewers")
    ):
        narrow_target = next_ac.get("viewers") or {}
        viewer_narrows = {node: narrow_target for node in desc}

    touched = {
        ref,
        *admin_grants,
        *viewer_grants,
        *admin_revocations,
        *viewer_revocations,
        *viewer_narrows,
    }

    # Landmine 9: a principal who already holds a (possibly dormant, per
    # rule C) `admins` entry at a node newly regains effective edit there
    # the moment this write makes them a viewer of that same node.
    reactivated_edit: list[NodeRef] = []
    for node, granted in viewer_grants.items():
        node_ac = next_ac if node == ref else (read_ac(db, node) or {})
        node_prev_viewers = prev_viewers if node == ref else to_principals(node_ac.get("viewers"))
        node_admins = to_principals(node_ac.get("admins"))
        newly_viewable = {p for p in granted if p not in node_prev_viewers}
        if any(p in node_admins for p in newly_viewable):
            reactivated_edit.append(node)

    return WritePlan(
        ref=ref,
        next_ac=next_ac,
        touched=sorted(touched, key=lambda r: (r.kind, r.id)),
        admin_grants=admin_grants,
        viewer_grants=viewer_grants,
        admin_revocations=admin_revocations,
        viewer_revocations=viewer_revocations,
        viewer_narrows=viewer_narrows,
        reactivated_edit=sorted(reactivated_edit, key=lambda r: (r.kind, r.id)),
    )


def apply_write(db: Session, ref: NodeRef, next_ac: dict[str, Any] | None) -> list[NodeRef]:
    """Runs `plan_write`, then materializes it. `ref` first gets `next_ac`
    verbatim — the caller's own, fully-specified new state — and is then
    put through the SAME grant/revoke loops as every other touched node,
    not skipped.

    That inclusion is load-bearing, not redundant: rule 1's second clause
    (§3.2) unions the newly-added admins into viewers "of N, every
    descendant, and every ancestor" — N is `ref` itself. A caller that
    grants someone `admins` without separately adding them to `next_ac`'s
    own `viewers` would otherwise leave rule C false at the very node being
    written (admins present, viewers absent) — a grant dead on arrival at
    its own origin. For every OTHER grant/revoke category, reprocessing
    `ref` is a harmless no-op by construction (e.g. `admin_grants[ref]` is
    exactly the set already newly present in `next_ac.admins`, so unioning
    it again changes nothing) — it's only rule 1's cross-group clause where
    `next_ac` doesn't already carry the derived value.

    The rule-5 (viewer_narrows) loop runs BEFORE the viewer_grants loop, not
    after — that ordering is load-bearing too, for a reason symmetrical to
    the one above. If the same write both adds an admin (rule 1) and narrows
    N's viewers from universal (rule 5), a descendant D that was itself
    still universal at that moment would have rule 1's union into D's
    viewers silently suppressed (landmine 1's "union into a universal
    viewers group is a no-op" — the new admin isn't a NAMED removal, so
    nothing is added), and the grantee would end up with `admins` but no
    `viewers` on D — admin-but-can't-edit, exactly the bug rule 1 exists to
    prevent, just reintroduced by rule 5. Narrowing D to the new named set
    FIRST makes D non-universal before rule 1's union runs, so that union is
    a normal merge and the grantee lands in both groups on D as intended.
    """
    plan = plan_write(db, ref, next_ac)

    write_ac(db, ref, plan.next_ac)

    for node, principals in plan.admin_grants.items():
        current = read_ac(db, node) or {}
        new_admins = _union_principals_into_group(
            current.get("admins"), principals, is_viewers=False
        )
        write_ac(db, node, {**current, "admins": new_admins})

    for node, narrow_target in plan.viewer_narrows.items():
        current = read_ac(db, node) or {}
        # Rule 5, run before viewer_grants (see docstring above) — reuses
        # tab_service's component-subset intersection helper for a third
        # purpose beyond §5.2's original scope: collapsing a descendant's
        # still-universal viewers down to the ancestor's newly narrowed set,
        # rather than narrowing a component against a resolved parent.
        new_viewers = _intersect_group(current.get("viewers") or {}, narrow_target)
        write_ac(db, node, {**current, "viewers": new_viewers})

    for node, principals in plan.viewer_grants.items():
        current = read_ac(db, node) or {}
        new_viewers = _union_principals_into_group(
            current.get("viewers"), principals, is_viewers=True
        )
        write_ac(db, node, {**current, "viewers": new_viewers})

    for node, principals in plan.admin_revocations.items():
        current = read_ac(db, node) or {}
        new_admins = _subtract_principals_from_group(current.get("admins"), principals)
        write_ac(db, node, {**current, "admins": new_admins})

    for node, principals in plan.viewer_revocations.items():
        current = read_ac(db, node) or {}
        new_viewers = _subtract_principals_from_group(current.get("viewers"), principals)
        write_ac(db, node, {**current, "viewers": new_viewers})

    return plan.touched


# ---------------------------------------------------------
# Reparent repair — for a write that changes a node's OWN position in the
# tree rather than its AC. Two call sites, both outside this module's
# original scope (plan §5.2, landmine 3, which only analyzed the first):
# nav_tab_service.move_tab_to_nav_tab_v2 and
# gridstack_service.move_tab_by_document_id_v2 — the latter reparents a
# sub-tab gridstack under any other gridstack in the tree (only checked
# against "not itself, not a descendant") and has the identical shape;
# found while wiring commit 3, since the plan text never analyzes it.
# ---------------------------------------------------------


def apply_reparent_repair(db: Session, ref: NodeRef) -> list[NodeRef]:
    """Additive-only repair for an operation that changes `ref`'s PARENT
    POINTER without writing `ref`'s own access_control (a structural move,
    not an AC edit) — the one class of write where both invariants can
    break at once (landmine 3), since it's the only kind of write that
    changes which ancestors/descendants even apply.

    Callers must already have updated the row's own parent pointer (and any
    descendants' denormalized parent_tab_id) and flushed, so `ancestors`/
    `descendants`/`_parent_of` below resolve through the NEW position.

    Three unions (§5.2's table), reusing the same principal algebra as
    `apply_write`'s rule 1/2:
      1. the new parent's EFFECTIVE (nearest-non-null, landmine-14-style
         resolved) admins -> admins of `ref` and its entire subtree
         (invariant A).
      2. those same principals -> viewers of `ref`, its subtree, and every
         ancestor (rule C — otherwise step 1's grant is dead on arrival
         wherever it landed).
      3. `ref`'s OWN pre-existing viewers -> viewers of every ancestor
         (invariant B).

    Nothing is ever removed — the old parent's grants are left exactly as
    they were (additive only; see landmine 3). `reset_to_inherited(ref)` is
    the intended cleanup for whoever wants `ref` to stop carrying them.
    """
    subtree = [ref, *descendants(db, ref)]
    anc = ancestors(db, ref)

    parent = _parent_of(db, ref)
    dest_admins: dict[Principal, dict[str, Any]] = {}
    if parent is not None:
        dest_admins = to_principals(_resolve_ac_upward(db, parent).get("admins"))

    ref_viewers = to_principals((read_ac(db, ref) or {}).get("viewers"))

    touched: set[NodeRef] = set()

    if dest_admins:
        for node in subtree:
            current = read_ac(db, node) or {}
            new_admins = _union_principals_into_group(
                current.get("admins"), dest_admins, is_viewers=False
            )
            write_ac(db, node, {**current, "admins": new_admins})
            touched.add(node)

        for node in [*subtree, *anc]:
            current = read_ac(db, node) or {}
            new_viewers = _union_principals_into_group(
                current.get("viewers"), dest_admins, is_viewers=True
            )
            write_ac(db, node, {**current, "viewers": new_viewers})
            touched.add(node)

    if ref_viewers:
        for node in anc:
            current = read_ac(db, node) or {}
            new_viewers = _union_principals_into_group(
                current.get("viewers"), ref_viewers, is_viewers=True
            )
            write_ac(db, node, {**current, "viewers": new_viewers})
            touched.add(node)

    return sorted(touched, key=lambda r: (r.kind, r.id))


# ---------------------------------------------------------
# Reset helpers (§5.1, §6.4) — materialization destroys provenance
# (landmine 2), so these are the only way to walk a drifted node back.
# ---------------------------------------------------------


def purge_principal(db: Session, ref: NodeRef, principal: Principal) -> list[NodeRef]:
    """Strip `principal` from `admins` and `viewers` of `ref` and its whole
    subtree (unconditional — not a diffed propagation), then apply rule 3
    upward if they held admin at `ref` itself. At the hub, this is the
    global kill switch."""
    touched: list[NodeRef] = []
    admin_removed_at_ref = False

    for node in [ref, *descendants(db, ref)]:
        current = read_ac(db, node) or {}
        admins_principals = to_principals(current.get("admins"))
        viewers_principals = to_principals(current.get("viewers"))
        changed = False
        if principal in admins_principals:
            del admins_principals[principal]
            changed = True
            if node == ref:
                admin_removed_at_ref = True
        if principal in viewers_principals:
            del viewers_principals[principal]
            changed = True
        if changed:
            write_ac(
                db,
                node,
                {
                    **current,
                    "admins": from_principals(admins_principals),
                    "viewers": from_principals(viewers_principals),
                },
            )
            touched.append(node)

    if admin_removed_at_ref:
        for node in ancestors(db, ref):
            current = read_ac(db, node) or {}
            admins_principals = to_principals(current.get("admins"))
            if principal in admins_principals:
                del admins_principals[principal]
                write_ac(db, node, {**current, "admins": from_principals(admins_principals)})
                touched.append(node)

    return touched


def reset_to_inherited(db: Session, ref: NodeRef) -> list[NodeRef]:
    """Discard `ref`'s own access_control and re-derive it from its
    parent's EFFECTIVE (resolved, per landmine-14-style walk-up) AC, then
    re-propagate through the normal `apply_write` machinery — so whatever
    `ref` gains or loses cascades exactly as an ordinary write would."""
    parent = _parent_of(db, ref)
    if parent is None:
        raise ValueError("This node has no parent to inherit from")
    parent_ac = _resolve_ac_upward(db, parent)
    return apply_write(db, ref, parent_ac)
