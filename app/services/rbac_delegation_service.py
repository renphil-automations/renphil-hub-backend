"""The §6 delegation rule
(plan_access_control_schema_2026-08-22.md §6,
session_handoff_2026-08-24-permission-management-plan.md §3.1, §4).

Kept separate from ``rbac_graph_service`` for the same split reason that
module documents for itself: that one owns the two DAGs' structural rules
(acyclicity) and their closures; this one owns the one RULE built on top of
those closures — who may create or revoke a `role_assignments` row. This
module writes nothing at all, and reads `role_assignments` only through
``held_closures``.

THE ⊥ CAVEAT, and it is a real one (algorithm plan §4.4's "one power to
watch"). "Any Scope" sits in `scope_descendants(anything)` by construction,
so the SCOPE HALF of the check below now passes for every user against a ⊥
target, and "Any Role" does the same to the role half for anyone holding a
role other than ⊥ itself. What stops that from being a hole is that ⊥ is
refused as an assignment outright, at the router
(`routers/rbac_assignments.py`), before this check is ever reached. That
refusal is not a nicety layered on afterwards — it is the other half of the
flag, and the two must never be separated. If grant-writing on NODES is
later gated by this same rule, ⊥ needs deciding on again there, from
scratch.

THE TRAP THIS MODULE EXISTS TO AVOID (design doc §6.2, restated a third
time because it is the single most likely bug in this phase): the check
below iterates the granter's own `role_assignments` ROWS and must succeed on
ONE of them entirely. It must never gather the granter's roles into one set
and their scopes into another and test the union — that reads as "Alice is
Program Lead on X and Hub Member on Y, so she may grant Program Member on Y"
which is exactly the pairing bug the assignment row exists to prevent
(plan §2.4), resurfacing here because the union query is the easier one to
write.

Hub Admin's unconditional bypass (plan session handoff 2026-08-24 §3.1) is
deliberately NOT folded into `can_delegate` itself — that function answers
one question ("does this row's closures cover the target"), and mixing in a
JWT-role check would make it untestable as a pure graph predicate. Callers
needing the full gate (bypass first, §6.1 check second) use
`assert_can_delegate` below.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.rbac_graph_service import (
    HeldClosure,
    RbacClosures,
    RbacGraphError,
    held_closures,
)


def can_delegate(
    db: Session,
    granter_hub_user_id: int,
    role_id: int,
    scope_id: int,
    *,
    closures: RbacClosures | None = None,
    held: list[HeldClosure] | None = None,
) -> bool:
    """§6.1: may the holder of ``granter_hub_user_id``'s assignments create
    or revoke ``(_, role_id, scope_id)``?

    Iterates the granter's own assignment ROWS and succeeds on one held
    ENTIRELY — never on the union of their roles crossed with the union of
    their scopes (§6.2, restated in this module's docstring).

    Role: strictly a proper descendant of the held role (``!=``excluded) —
    granting or revoking your own role, or anything at/above it, is refused.
    Scope: inclusive of the held scope itself — granting on your own scope
    is the primary use case (plan §6.1's asymmetry, deliberate).

    WHY THIS DOES NOT CALL ``effective_pairs``, which is the obvious
    consolidation and is wrong. That helper answers §4.2's match direction —
    "does the user hold a pair at or above this one" — over a FLAT set of
    pairs, and flattening has already thrown away which role came from which
    row. §6.1 needs the target role to be a PROPER descendant of a
    SPECIFIC held role, so a granter holding exactly (Program Lead, A) has
    (Program Lead, A) in their effective pairs and still may not grant it.
    Testing membership in the flat set would silently let everyone delegate
    their own role. Both functions are instead built on ``held_closures``,
    one level down, where the rows are still rows.

    ``closures`` shares one snapshot with a caller making several checks; it
    is otherwise built once per call, which is already the point — this used
    to call ``role_descendants``/``scope_descendants`` inside the loop, and
    each of those rescanned an entire edge table (algorithm plan §8.2).

    ``held`` GOES ONE STEP FURTHER, and exists for exactly one caller: audit
    finding §2.3, ``list_revocable``, which asks this question once per
    candidate row in the org. ``closures`` alone is not enough there —
    ``held_closures`` still issues its own ``held_assignments`` query on
    every call, so sharing the snapshot removes four of the five per-row
    queries and leaves the fifth, improving the NUMBER without fixing the
    SHAPE. Passing the expanded rows in removes the last one, and the loop
    below becomes pure Python.

    THE PRECONDITION, and it cannot be checked from here: ``held`` MUST be
    ``held_closures(db, granter_hub_user_id)`` for the SAME granter. Passing
    somebody else's rows answers a different question and this function has
    no way to notice — verifying it would take the very query the parameter
    exists to avoid. One caller passes it, immediately after computing it
    from the same id, and that adjacency is the guarantee.

    WHAT MUST NOT HAPPEN INSTEAD, since it is the obvious alternative and it
    is what the 2026-08-25 audit proposed before this parameter existed: do
    not lift the comparison below into the caller as a pre-computed
    "envelope" test. §6.1's properness clause — strictly BELOW a role you
    hold — is a per-row fact that is easy to get subtly wrong the second
    time, which is why the rule lives in this function and only this
    function. Hoisting the DATA is safe; hoisting the RULE is not.
    """
    rows = held if held is not None else held_closures(db, granter_hub_user_id, closures=closures)
    for one in rows:
        if role_id == one.role_id:
            continue  # not a PROPER descendant of itself
        if role_id not in one.role_ids:
            continue
        if scope_id not in one.scope_ids:
            continue
        return True
    return False


def assert_can_delegate(
    db: Session,
    *,
    granter_hub_user_id: int,
    is_hub_admin: bool,
    role_id: int,
    scope_id: int,
    closures: RbacClosures | None = None,
) -> None:
    """The full write-gate for an assignment create/revoke (session handoff
    2026-08-24 §3.1, §4): Hub Admin bypasses unconditionally; everyone else
    is checked against their own held assignments only.

    Revocation uses this exact same check, not "only what I granted" — the
    caller passes the assignment's role/scope regardless of who originally
    granted it (§6.3, §6.4; restated as the landmine in the handoff's §7).

    THE BOOTSTRAP TRAP (handoff §7): the Hub Admin branch must stay first
    and unconditional. `role_assignments` starts empty, so if this ever
    fell through to `can_delegate` before checking Hub Admin status, or
    required a `role_assignments` row to exist for the Hub Admin path too,
    the system would lock itself out of its own bootstrap with no way back
    in except direct DB access.

    ``closures``, added for plan_access_control_algorithm_2026-08-27.md
    §6.8: the caller (typically a router that also had to resolve
    ``is_hub_admin`` via ``app.dependencies.is_hub_admin``, which itself
    calls ``effective_pairs``) may pass a snapshot it is already holding so
    this shares it rather than building a second one. Purely a cost
    optimization — omitted, ``can_delegate`` builds its own exactly as
    before.
    """
    if is_hub_admin:
        return
    if can_delegate(db, granter_hub_user_id, role_id, scope_id, closures=closures):
        return
    raise RbacGraphError(
        "not_delegable",
        (
            "You do not hold a role and scope that lets you grant or revoke "
            "this combination. You can only act on roles strictly below one "
            "you hold, on that same scope or a scope it contains."
        ),
        role_id=role_id,
        scope_id=scope_id,
    )
