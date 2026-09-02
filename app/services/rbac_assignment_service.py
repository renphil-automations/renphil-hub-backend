"""Assignment CRUD and listings
(plan_access_control_schema_2026-08-22.md §3.6,
session_handoff_2026-08-24-permission-management-plan.md §5.3, §5.4).

Split from ``rbac_delegation_service`` the same way ``rbac_service`` is split
from ``rbac_graph_service``: that module owns the §6.1 RULE, this one owns
the rows — resolving a target email to a `hub_users` id, inserting/deleting
`role_assignments`, and serializing for the three read surfaces the frontend
needs (what I hold, what I granted, what I could revoke right now).

The write gate (`assert_can_delegate`) is deliberately NOT called from this
module — it is called once, from the router, so the 409 mapping stays in one
place (`_conflict`) and this module never imports FastAPI.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db_v2.models.hub_user import HubUserV2
from app.db_v2.models.role import RoleV2
from app.db_v2.models.role_assignment import RoleAssignmentV2
from app.db_v2.models.scope import ScopeV2
from app.services.rbac_delegation_service import can_delegate
from app.services.rbac_graph_service import RbacGraphError, held_closures

logger = logging.getLogger(__name__)

HUB_USER_SEARCH_MIN_LENGTH = 3
HUB_USER_SEARCH_LIMIT = 20

# The escape character for the LIKE patterns in `search_hub_users`. A single
# backslash — doubled here only because this is a Python string literal.
#
# Verified on BOTH DIALECTS on 2026-08-31 rather than assumed, because the
# suite builds its schema on in-memory SQLite while production is Postgres,
# and a fix that held on only one is a fix no test can prove. SQLAlchemy
# renders `ilike(term, escape="\\")` as `lower(x) LIKE lower(?) ESCAPE '\'`
# on SQLite and `x ILIKE %(p)s ESCAPE '\'` on Postgres, and both agreed on
# every probe: `%%%` and `___` return nothing, while a literal `_` in an
# address matches literally.
HUB_USER_SEARCH_ESCAPE = "\\"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------
# Serialization
# ---------------------------------------------------------


def _hub_user_email_map(db: Session, hub_user_ids: set[int]) -> dict[int, str]:
    """One query for every target/granter email a page of assignments needs,
    rather than one per row — same N+1 avoidance as rbac_service's edge maps.

    Safe to join live (never a snapshot) for the TARGET side: `user_id`
    CASCADEs (plan §3.6), so an assignment row can never outlive the
    hub_users row it points to. `granted_by_email` stays a stored snapshot
    for the granter side, since that FK is SET NULL, not CASCADE.
    """
    if not hub_user_ids:
        return {}
    rows = db.query(HubUserV2.id, HubUserV2.email).filter(HubUserV2.id.in_(hub_user_ids)).all()
    return {row[0]: row[1] for row in rows}


def _serialize(assignment: RoleAssignmentV2, user_email: str) -> dict:
    return {
        "id": assignment.id,
        "user_id": assignment.user_id,
        "user_email": user_email,
        "role_id": assignment.role_id,
        "scope_id": assignment.scope_id,
        "granted_by_user_id": assignment.granted_by_user_id,
        "granted_by_email": assignment.granted_by_email,
        "created_at": assignment.created_at,
    }


def _serialize_many(db: Session, assignments: list[RoleAssignmentV2]) -> list[dict]:
    emails = _hub_user_email_map(db, {a.user_id for a in assignments})
    return [_serialize(a, emails.get(a.user_id, "")) for a in assignments]


# ---------------------------------------------------------
# Reads
# ---------------------------------------------------------


def list_my_assignments(db: Session, hub_user_id: int) -> list[dict]:
    """Every `(role, scope)` the caller holds directly — one row per
    assignment, not the expanded closure (plan §2.4). The closure is a
    client-side concern, same as the roles/scopes screen (§8)."""
    rows = (
        db.query(RoleAssignmentV2)
        .filter(RoleAssignmentV2.user_id == hub_user_id)
        .order_by(RoleAssignmentV2.created_at.desc())
        .all()
    )
    return _serialize_many(db, rows)


def list_granted_by_me(db: Session, hub_user_id: int) -> list[dict]:
    """Audit trail: every assignment the caller personally granted, whether
    or not they could still revoke it today (see `list_revocable` for that).
    A delegator losing their own assignment does not cascade to what they
    granted (plan §6.4), so these two lists can genuinely diverge."""
    rows = (
        db.query(RoleAssignmentV2)
        .filter(RoleAssignmentV2.granted_by_user_id == hub_user_id)
        .order_by(RoleAssignmentV2.created_at.desc())
        .all()
    )
    return _serialize_many(db, rows)


def list_revocable(db: Session, hub_user_id: int, *, is_hub_admin: bool) -> list[dict]:
    """Every assignment, ORG-WIDE, the caller is currently eligible to
    revoke under §6.1's symmetric rule (design doc §6.3) — not just the ones
    they granted. A Hub Admin is eligible for all of them, unconditionally,
    same bypass as everywhere else in this phase.

    A plain per-row `can_delegate` call, not a bulk query: both graphs are
    tens of rows (rbac_graph_service's own docstring), so doing this in
    Python rather than as one large SQL join keeps the eligibility rule in
    exactly one place instead of two.

    THE CALLER'S OWN STATE IS HOISTED OUT OF THAT LOOP, and it is the whole
    of audit finding §2.3 (2026-08-25). `can_delegate` answers a question
    about the CANDIDATE row using facts about the CALLER, and the caller's
    facts do not change between rows — but the call used to rebuild them
    every time: one `RbacClosures` (both edge tables, the public roles, the
    scopes) plus one `held_assignments`, five queries per candidate. At 71
    assignment rows that measured **357 statements** for a non-admin against
    3 for a Hub Admin, who bypasses the loop entirely. Against Neon each one
    is a network round-trip, and only non-admins pay — i.e. exactly the
    delegating users this page exists for.

    Measured again after the change: **7 statements**, flat in the number of
    rows. The audit's own figure was 244; the shape had grown since, because
    `RbacClosures` added two more queries per instantiation than the free
    closure helpers it replaced.

    THE RULE ITSELF DID NOT MOVE. `can_delegate` is still the only place
    §6.1 is expressed, and it is still asked once per candidate row — what
    it no longer does is re-derive the granter's held closures each time.
    Reimplementing the eligibility test here as a set membership would be
    faster still and is precisely what `can_delegate`'s docstring warns
    against: properness ("strictly below a role you hold") is a per-row fact
    that flattening destroys.
    """
    rows = db.query(RoleAssignmentV2).order_by(RoleAssignmentV2.created_at.desc()).all()
    if not is_hub_admin:
        # ONE snapshot, ONE expansion of the caller's own assignments, reused
        # for every candidate. Built here rather than inside the comprehension
        # so it cannot accidentally become per-row again.
        held = held_closures(db, hub_user_id)
        rows = [
            r
            for r in rows
            if can_delegate(db, hub_user_id, r.role_id, r.scope_id, held=held)
        ]
    return _serialize_many(db, rows)


def search_hub_users(db: Session, query: str) -> list[dict]:
    """Target picker (handoff §5.4). Requires a real search term — the
    query itself must already be at least `HUB_USER_SEARCH_MIN_LENGTH`
    characters, enforced by the router's `Query(min_length=...)`, so this
    never has to decide what "no query" should return. Restricted to
    `is_active` rows: an inactive person cannot presently be granted a role
    either way, so surfacing them in the picker only invites a confusing
    `target_not_provisioned`-style failure with no matching error code.

    THE QUERY IS ESCAPED BEFORE IT BECOMES A PATTERN, and that is a privacy
    control rather than tidiness (audit finding §2.2, 2026-08-25). The
    router's `min_length=3` is the whole of what stops this endpoint from
    being an org-wide people dump — §5.4 decided a real search prefix is
    required. Interpolating the query straight into `%...%` handed the
    caller LIKE's two metacharacters, and three of either satisfies the
    minimum: `%%%` and `___` each returned the entire directory, 20 rows at
    a time. Reproduced against the live code before this was changed, and
    `tests/test_admin_floor.TestTheWildcardBypass` is that reproduction.

    ORDER IS LOAD-BEARING: the backslash must be doubled FIRST. Escaping
    `%` first would turn it into `\\%`, and the later backslash pass would
    then re-escape that into `\\\\%` — the escape character escaping itself,
    leaving the `%` unguarded and the hole exactly where it was.

    This deliberately does NOT touch `is_active` (open finding §2.4 — the
    flag is enforced nowhere else, and making this the exception is a
    separate decision) or the silent truncation at `HUB_USER_SEARCH_LIMIT`
    (open finding §2.6). Both are pinned by tests so a later fix has
    something to update.
    """
    safe = (
        query.strip()
        .lower()
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    term = f"%{safe}%"
    rows = (
        db.query(HubUserV2)
        .filter(HubUserV2.is_active.is_(True))
        .filter(
            or_(
                HubUserV2.email.ilike(term, escape=HUB_USER_SEARCH_ESCAPE),
                HubUserV2.name.ilike(term, escape=HUB_USER_SEARCH_ESCAPE),
            )
        )
        .order_by(HubUserV2.email)
        .limit(HUB_USER_SEARCH_LIMIT)
        .all()
    )
    return [{"id": r.id, "email": r.email, "name": r.name} for r in rows]


def get_assignment(db: Session, assignment_id: int) -> RoleAssignmentV2 | None:
    return db.query(RoleAssignmentV2).filter(RoleAssignmentV2.id == assignment_id).first()


# ---------------------------------------------------------
# Writes — delegation gate is asserted by the ROUTER, not here (see module
# docstring); this module only knows how to insert/delete once cleared.
# ---------------------------------------------------------


def create_assignment(
    db: Session,
    *,
    granter_hub_user_id: int,
    granter_email: str,
    target_email: str,
    role_id: int,
    scope_id: int,
) -> dict:
    """Insert `(target, role, scope)`. Caller must already have: validated
    the delegation gate, and confirmed `role_id`/`scope_id` exist (both the
    router's job — see `rbac_assignments.py`).

    Target provisioning is deliberately NOT done here (plan session handoff
    2026-08-24 §3.2): granting requires the target to already hold a
    `hub_users` row, i.e. to have logged in at least once. No
    auto-create-on-grant — that is `target_not_provisioned`, below.
    """
    normalized_target = target_email.strip().lower()
    target = db.query(HubUserV2).filter(HubUserV2.email == normalized_target).first()
    if target is None:
        raise RbacGraphError(
            "target_not_provisioned",
            (
                f"{target_email!r} has not signed in yet, so there is no "
                f"account to grant a role to. They must log in at least "
                f"once first."
            ),
            email=normalized_target,
        )

    existing = (
        db.query(RoleAssignmentV2)
        .filter(
            RoleAssignmentV2.user_id == target.id,
            RoleAssignmentV2.role_id == role_id,
            RoleAssignmentV2.scope_id == scope_id,
        )
        .first()
    )
    if existing is not None:
        raise RbacGraphError(
            "duplicate_assignment",
            f"{target_email!r} already holds that role on that scope.",
            user_id=target.id,
            role_id=role_id,
            scope_id=scope_id,
        )

    assignment = RoleAssignmentV2(
        user_id=target.id,
        role_id=role_id,
        scope_id=scope_id,
        granted_by_user_id=granter_hub_user_id,
        granted_by_email=granter_email,
        created_at=_utc_now(),
    )
    db.add(assignment)
    db.flush()
    return _serialize(assignment, normalized_target)


# ---------------------------------------------------------
# The admin floor (plan_access_control_algorithm_2026-08-27.md §6.5, §6.8)
# ---------------------------------------------------------

# The DB `key` of the Hub Admin role. NOT `tab_service.HUB_ADMIN_ROLE`, which
# is the string "Hub Admin" — a JWT role DISPLAY NAME issued by Airtable, and
# a different namespace entirely. `roles.key` is the handle code is allowed to
# reference; `roles.name` is what admins rename freely (see RoleV2's comment
# on the split). Confusing the two silently makes the guard below match
# nothing, which fails OPEN.
#
# Lives here, beside its only reader, rather than in config: the value has to
# equal a `roles.key` exactly, and making it environment-overridable would add
# a way to point the admin floor at a role that does not exist. §10 item 6's
# cutover is what will need to find this, so it is a module constant with this
# comment on it rather than an inline literal.
HUB_ADMIN_ROLE_KEY = "hub_admin"


def _hub_admin_pair_ids(db: Session) -> tuple[int | None, set[int]]:
    """`(hub_admin role id, universal scope ids)` — the literal pair §6.8
    defines an admin as holding.

    Universal scopes come back as a SET even though the partial unique index
    `uq_scopes_single_universal` permits only one. Same defensive shape
    `RbacClosures` uses for the ⊥ ids and for the same reason: unioning a set
    of any size is the same operation, and this code should not independently
    assume what the index already guarantees.
    """
    role_row = db.query(RoleV2.id).filter(RoleV2.key == HUB_ADMIN_ROLE_KEY).first()
    universal_ids = {
        row[0] for row in db.query(ScopeV2.id).filter(ScopeV2.is_universal.is_(True)).all()
    }
    return (role_row[0] if role_row else None), universal_ids


def _assert_not_the_last_hub_admin(db: Session, assignment: RoleAssignmentV2) -> None:
    """THE ADMIN FLOOR. Refuses the removal of the final
    `(hub_admin, universal_scope)` row in `role_assignments`.

    ─────────────────────────────────────────────────────────────────────
    THE HUB ADMIN BYPASS DOES NOT APPLY HERE. Read this before "fixing" it.
    ─────────────────────────────────────────────────────────────────────
    Every other gate in this subsystem lets a Hub Admin through first and
    unconditionally — `assert_can_delegate` documents that as the bootstrap
    trap, and `resource_grant_authz_service` repeats it. This one does not,
    and the inversion is deliberate.

    The reason is that the bypass and this rule protect opposite things. The
    bypass exists so an admin is never locked OUT of an operation. This rule
    exists so the hub is never left with no admin at all. Only a Hub Admin
    can revoke a Hub Admin's assignment in the first place — under §6.1
    nobody else holds a role strictly above `hub_admin`, because nothing is
    above it — so a guard the bypass skipped would be a guard that never ran
    for the only person who can trip it. It would be decorative.

    So: NO `is_hub_admin` PARAMETER, and no caller may pass one. That is why
    this lives in the service rather than the router. `assert_can_delegate`
    is asserted by the router by deliberate design (see this module's own
    docstring — the gate is the router's job so the 409 mapping stays in one
    place), but that is an AUTHORIZATION rule, and authorization is exactly
    what a Hub Admin is entitled to skip. This is an INTEGRITY rule, the same
    kind as `delete_role`'s `role_in_use` pre-count, which also lives in the
    service and also applies to everyone. Integrity rules belong next to the
    write they constrain, where no caller can forget to invoke them.

    ─────────────────────────────────────────────────────────────────────
    THIS DOES NOT REPLACE `BOOTSTRAP_ADMIN_EMAILS` (§6.5, condition 3).
    ─────────────────────────────────────────────────────────────────────
    It is a data-layer protection and it protects exactly one path: a DELETE
    through this service. It does not survive a bad migration, a restore to
    the wrong snapshot, an empty table, or direct database access — and
    `role_assignments` has zero rows as of 2026-08-31, so "already empty" is
    the CURRENT state, not a hypothetical.

    Recovery through `can_delegate` is impossible by construction: it needs a
    role strictly ABOVE `hub_admin`, and nothing is above it. §6.5 therefore
    specifies a settings-level `BOOTSTRAP_ADMIN_EMAILS` list PERMANENTLY,
    precisely because it lives outside the database. Neither this guard nor
    `is_system` closes that requirement, and §6.5 must not be marked
    satisfied on the strength of either.

    ─────────────────────────────────────────────────────────────────────
    THE GAP THIS GUARD CANNOT SEE, stated rather than papered over.
    ─────────────────────────────────────────────────────────────────────
    `role_assignments.user_id` is `ON DELETE CASCADE` (`role_assignment.py`).
    Deleting a `hub_users` row therefore removes that person's assignments
    WITHOUT passing through this function — including the last admin's. No
    endpoint exposes a hub_users delete or deactivate today, so the path is
    reachable only by direct DB access, but the guard is not complete and
    should not be described as if it were. Closing it properly means either a
    database trigger or an application-level pre-check wherever a hub_users
    delete is eventually built.

    ─────────────────────────────────────────────────────────────────────
    WHAT COUNTS AS AN ADMIN (§6.8, confirmed with the owner 2026-08-31).
    ─────────────────────────────────────────────────────────────────────
    The LITERAL pair — a `role_assignments` row whose `role_id` is the
    `hub_admin` role and whose `scope_id` is a universal scope. Not a closure
    match: `(Hub Admin, Program A)` is not an admin for this purpose, because
    §6.5 condition 1 requires hub-wide scope for a hub-node grant to match at
    all. A role added ABOVE `hub_admin` would not count either, and that is
    the intended reading — the floor should not silently change meaning when
    somebody edits the role DAG.

    `is_active` is deliberately NOT part of the count (owner decision, same
    date). The flag is enforced nowhere else in this codebase — open finding
    §2.4 — and making the admin floor the single exception would be
    inconsistent, as well as letting a deactivation silently tighten a rule
    no endpoint exposes. Instead, an allowed delete that leaves no ACTIVE
    admin logs a warning: the floor is then nominal, satisfied by somebody
    who cannot log in.
    """
    hub_admin_role_id, universal_scope_ids = _hub_admin_pair_ids(db)

    # No `hub_admin` role row, or no universal scope: the protected pair
    # cannot exist, so no assignment can be the last one. Return rather than
    # raise — this is the state a fresh database is in, and erroring here
    # would make an unrelated revoke fail for a reason nobody could act on.
    if hub_admin_role_id is None or not universal_scope_ids:
        return

    # Is the row being deleted an admin assignment at all? If not there is
    # nothing to protect, and this is also what makes the EMPTY-TABLE case
    # correct: revoking an ordinary assignment in a hub with zero admins must
    # succeed, not error. The count below is never even reached for it.
    if (
        assignment.role_id != hub_admin_role_id
        or assignment.scope_id not in universal_scope_ids
    ):
        return

    remaining = (
        db.query(RoleAssignmentV2)
        .filter(
            RoleAssignmentV2.role_id == hub_admin_role_id,
            RoleAssignmentV2.scope_id.in_(universal_scope_ids),
            RoleAssignmentV2.id != assignment.id,
        )
        .count()
    )
    if remaining == 0:
        raise RbacGraphError(
            "last_hub_admin",
            (
                "This is the only Hub Admin assignment on a hub-wide scope. "
                "Removing it would leave the hub with nobody who can "
                "administer it, and there is no role above Hub Admin that "
                "could grant it back. Assign Hub Admin to somebody else "
                "first, then remove this one."
            ),
            assignment_id=assignment.id,
            role_id=assignment.role_id,
            scope_id=assignment.scope_id,
        )

    # Allowed — but say so when the floor is only nominally standing. See the
    # `is_active` note above: the count deliberately ignores the flag, so this
    # is the observable signal that it would have mattered.
    active_remaining = (
        db.query(RoleAssignmentV2)
        .join(HubUserV2, HubUserV2.id == RoleAssignmentV2.user_id)
        .filter(
            RoleAssignmentV2.role_id == hub_admin_role_id,
            RoleAssignmentV2.scope_id.in_(universal_scope_ids),
            RoleAssignmentV2.id != assignment.id,
            HubUserV2.is_active.is_(True),
        )
        .count()
    )
    if active_remaining == 0:
        logger.warning(
            "Admin floor is nominal: assignment %s was revoked leaving %s "
            "hub-wide Hub Admin assignment(s), none of which belong to an "
            "active hub user. Nobody who can sign in can administer the hub. "
            "BOOTSTRAP_ADMIN_EMAILS (plan §6.5) is the recovery path.",
            assignment.id,
            remaining,
        )


def delete_assignment(db: Session, assignment_id: int) -> bool:
    """Removing an assignment can never violate the DELEGATION rule — it only
    ever shrinks access — so there is nothing left to authorize once the
    caller's gate (router-side) has already passed.

    It can, however, violate an INTEGRITY rule, and that is what
    `_assert_not_the_last_hub_admin` is. Read its docstring before changing
    anything here; in particular it must NOT gain an `is_hub_admin` bypass,
    and it does not make §6.5's `BOOTSTRAP_ADMIN_EMAILS` unnecessary.
    """
    assignment = get_assignment(db, assignment_id)
    if assignment is None:
        return False
    _assert_not_the_last_hub_admin(db, assignment)
    db.delete(assignment)
    db.flush()
    return True
