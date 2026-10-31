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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db_v2.models.role import RoleV2
from app.db_v2.models.role_assignment import RoleAssignmentV2
from app.db_v2.models.scope import ScopeV2
from app.db_v2.models.user import UserV2
from app.services.rbac_delegation_service import can_delegate
from app.services.rbac_graph_service import RbacGraphError, held_closures
from app.services.user_db_service import find_user_by_email

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
    rows = db.query(UserV2.id, UserV2.email).filter(UserV2.id.in_(hub_user_ids)).all()
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
    # Soft-deleted users' rows are kept but inert (plan_admin_soft_delete_user
    # §3.4): nothing to revoke while they're deleted.
    rows = (
        db.query(RoleAssignmentV2)
        .join(UserV2, UserV2.id == RoleAssignmentV2.user_id)
        .filter(UserV2.not_deleted())
        .order_by(RoleAssignmentV2.created_at.desc())
        .all()
    )
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
    `has_signed_in` rows: people who have signed in at least once (owner
    accepted 2026-10-09 that someone an admin added stays out of this picker
    until their first sign-in). Matches the name and BOTH email columns.

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

    This deliberately does NOT touch `has_signed_in` (open finding §2.4 — the
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
        db.query(UserV2)
        .filter(UserV2.has_signed_in.is_(True), UserV2.not_deleted())
        .filter(
            or_(
                UserV2.work_email.ilike(term, escape=HUB_USER_SEARCH_ESCAPE),
                UserV2.alias_email.ilike(term, escape=HUB_USER_SEARCH_ESCAPE),
                UserV2.name.ilike(term, escape=HUB_USER_SEARCH_ESCAPE),
            )
        )
        .order_by(UserV2.email)
        .limit(HUB_USER_SEARCH_LIMIT)
        .all()
    )
    return [{"id": r.id, "email": r.email, "name": r.name} for r in rows]


def get_assignment(db: Session, assignment_id: int) -> RoleAssignmentV2 | None:
    return db.query(RoleAssignmentV2).filter(RoleAssignmentV2.id == assignment_id).first()


def list_all_hub_users_with_assignments(db: Session) -> list[dict]:
    """Every `users` row someone can sign in as, each carrying every
    `role_assignments` row it holds directly (possibly none) — the data
    behind the Hub-Admin-only user management table (owner decision,
    2026-09-12). Unlike `search_hub_users` (the target picker), this is
    neither filtered to `has_signed_in` nor gated by a search term: the privacy
    boundary here is `require_hub_admin` itself (enforced by the router), not
    a query-length minimum. So since the 2026-10 merge it also lists HR rows
    nobody has signed in as yet (`has_signed_in` False). HR rows with no email
    at all are left out — nobody can sign in as them or be granted to them.

    Two queries total, not one-per-user: same N+1 avoidance as
    `_hub_user_email_map` and `list_revocable`'s hoisted closure — at 182
    hub_users rows, one-per-user would be 182 round-trips against Neon for a
    single page load.

    Soft-deleted rows are INCLUDED, flagged with `is_deleted` /
    `deleted_at` / `deleted_by` (plan_admin_soft_delete_user §3.4): the
    frontend shows them in their own "Deleted users" tab.
    """
    users = db.query(UserV2).filter(UserV2.email.isnot(None)).order_by(UserV2.email).all()
    all_assignments = db.query(RoleAssignmentV2).order_by(RoleAssignmentV2.created_at.desc()).all()

    by_user_id: dict[int, list[RoleAssignmentV2]] = {}
    for a in all_assignments:
        by_user_id.setdefault(a.user_id, []).append(a)

    return [_admin_row(u, by_user_id.get(u.id, [])) for u in users]


def _deleted_fields(user: UserV2) -> dict:
    return {
        "is_deleted": bool(user.is_deleted),
        "deleted_at": user.deleted_at,
        "deleted_by": user.deleted_by,
    }


def _admin_row(user: UserV2, assignments: list[RoleAssignmentV2]) -> dict:
    return {
        **_deleted_fields(user),
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "has_signed_in": user.has_signed_in,
        "assignments": [_serialize(a, user.email) for a in assignments],
    }


def admin_row_for_user(db: Session, user: UserV2) -> dict:
    """One user's row of `list_all_hub_users_with_assignments` (the Restore
    response, so the table can update that row in place)."""
    assignments = (
        db.query(RoleAssignmentV2)
        .filter(RoleAssignmentV2.user_id == user.id)
        .order_by(RoleAssignmentV2.created_at.desc())
        .all()
    )
    return _admin_row(user, assignments)


def create_hub_user(db: Session, *, email: str, name: str) -> dict:
    """Admin pre-registration of a login-only `users` row (owner decision,
    2026-10-04) — the row that lets an external (non-org-domain) email pass
    `AuthService._enforce_login_allowed`. Same email normalization as
    `_ensure_hub_user`. Flushes but does not commit; the router commits.

    Created with `has_signed_in` False — "has not signed in yet" (owner decision,
    2026-10-09); their first sign-in flips it (`_ensure_hub_user`).

    Raises `RbacGraphError("hub_user_exists")` for an email that already has
    a row — including an HR row whose work or alias email it is — whether
    found by the pre-check or by the unique index in a concurrent-insert race
    (the caller must roll back in that case). The code keeps its old name so
    the frontend contract does not change.

    The new row gets the default ⊥ assignment (`add_default_assignment`
    below), returned in `assignments` so the admin's detail modal shows it.

    An email that belongs to a SOFT-DELETED row raises
    `RbacGraphError("hub_user_deleted")` with that row's id and deletion
    date, so the UI can offer Restore instead (plan_admin_soft_delete_user
    §3.4): the kept row still holds the email in the unique indexes.
    """
    normalized = email.strip().lower()
    existing = find_user_by_email(db, normalized, include_deleted=True)
    if existing is not None and existing.is_deleted:
        raise RbacGraphError(
            "hub_user_deleted",
            f"{normalized} belongs to a deleted user. Restore them instead.",
            email=normalized,
            user_id=existing.id,
            deleted_at=existing.deleted_at.isoformat() if existing.deleted_at else None,
            deleted_by=existing.deleted_by,
        )
    if existing is not None:
        raise RbacGraphError(
            "hub_user_exists",
            f"A user with email {normalized} already exists.",
            email=normalized,
        )
    now = _utc_now()
    user = UserV2(work_email=normalized, name=name, has_signed_in=False, created_at=now, updated_at=now)
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        raise RbacGraphError(
            "hub_user_exists",
            f"A user with email {normalized} already exists.",
            email=normalized,
        ) from exc
    default = add_default_assignment(db, user.id)
    return _admin_row(user, [default] if default is not None else [])


# ---------------------------------------------------------
# The default assignment (owner decision, 2026-10-14)
# ---------------------------------------------------------


def public_pair_ids(db: Session) -> tuple[int, int] | None:
    """`(public role id, public scope id)` — the ⊥ pair, i.e. "User" on
    "Hub" — or `None` if either row is missing. Looked up by `is_public`,
    never by `key` or `name`, so renaming either row changes nothing here.
    Ordered by id only so the answer is deterministic; the partial unique
    indexes already keep each to one row."""
    role_row = db.query(RoleV2.id).filter(RoleV2.is_public.is_(True)).order_by(RoleV2.id).first()
    scope_row = db.query(ScopeV2.id).filter(ScopeV2.is_public.is_(True)).order_by(ScopeV2.id).first()
    if role_row is None or scope_row is None:
        return None
    return role_row[0], scope_row[0]


def add_default_assignment(db: Session, hub_user_id: int) -> RoleAssignmentV2 | None:
    """Give a NEWLY CREATED `hub_users` row the ⊥ pair, so that a user who
    holds nothing else still reaches content published to everyone — a user
    with zero assignment rows reaches no pair at all, ⊥ included (see
    `rbac_graph_service.audience_user_ids`).

    Called only from the two places a `hub_users` row is born
    (`dependencies._ensure_hub_user` and `create_hub_user` above), in the same
    transaction as the insert, and only on the insert branch: existing users
    are deliberately not backfilled, and a default that was revoked must not
    come back on the next request. Revoking and re-granting it afterwards
    follow the normal delegation rules (`routers/rbac_assignments.py`).

    The ⊥ pair confers no delegation power: its role has nothing strictly
    beneath it, so `can_delegate` fails every row it would be checked against.

    `granted_by_*` stay NULL — the system granted it, not a person. Returns
    `None` without writing anything if either ⊥ row is missing (logged), so
    a database without them can still provision users. Flushes, never
    commits; the caller owns the transaction.
    """
    pair = public_pair_ids(db)
    if pair is None:
        logger.warning(
            "Default assignment skipped for hub_user %s: no public role and/or public scope row",
            hub_user_id,
        )
        return None
    role_id, scope_id = pair
    assignment = RoleAssignmentV2(
        user_id=hub_user_id,
        role_id=role_id,
        scope_id=scope_id,
        granted_by_user_id=None,
        granted_by_email=None,
        created_at=_utc_now(),
    )
    db.add(assignment)
    db.flush()
    return assignment


def _assert_update_does_not_remove_last_hub_admin(
    db: Session,
    assignment: RoleAssignmentV2,
    new_role_id: int,
    new_scope_id: int,
) -> None:
    """The admin floor's update-shaped twin. `_assert_not_the_last_hub_admin`
    above answers "is it safe to DELETE this row"; this answers "is it safe
    to REPOINT this row at a different role/scope", which is not the same
    question — an admin moving `(hub_admin, universal A)` to
    `(hub_admin, universal B)` never stops being an admin pairing, and must
    not be blocked just because it is momentarily the only row on the way to
    still being one. So this only escalates when the EDIT itself would take
    the row OUT of admin-pair status, exactly mirroring the delete guard's
    "was this row the last one" check but computed against the row's
    prospective new values rather than its absence.

    Same non-negotiables as the delete-side guard (read its docstring): no
    `is_hub_admin` bypass — only a Hub Admin can reach this code path at all
    (the router requires it), so a bypass here would be decorative — and
    this does not close the `role_assignments.user_id` CASCADE gap or
    substitute for `BOOTSTRAP_ADMIN_EMAILS`.
    """
    hub_admin_role_id, universal_scope_ids = _hub_admin_pair_ids(db)
    if hub_admin_role_id is None or not universal_scope_ids:
        return

    was_admin_pair = (
        assignment.role_id == hub_admin_role_id and assignment.scope_id in universal_scope_ids
    )
    if not was_admin_pair or _holder_is_deleted(db, assignment):
        return  # nothing protected about this row today; nothing to check

    still_admin_pair = new_role_id == hub_admin_role_id and new_scope_id in universal_scope_ids
    if still_admin_pair:
        return  # moved between admin pairs (e.g. one universal scope to another) — fine

    # Only non-deleted holders count (plan_admin_soft_delete_user §3.4).
    remaining = (
        live_admin_pairs(db, hub_admin_role_id, universal_scope_ids)
        .filter(RoleAssignmentV2.id != assignment.id)
        .count()
    )
    if remaining == 0:
        raise RbacGraphError(
            "last_hub_admin",
            (
                "This is the only Hub Admin assignment on a hub-wide scope. "
                "Changing its role or scope away from Hub Admin would leave "
                "the hub with nobody who can administer it, and there is no "
                "role above Hub Admin that could grant it back. Assign Hub "
                "Admin to somebody else first, then change this one."
            ),
            assignment_id=assignment.id,
            role_id=assignment.role_id,
            scope_id=assignment.scope_id,
        )

    signed_in_remaining = (
        live_admin_pairs(db, hub_admin_role_id, universal_scope_ids)
        .filter(RoleAssignmentV2.id != assignment.id, UserV2.has_signed_in.is_(True))
        .count()
    )
    if signed_in_remaining == 0:
        logger.warning(
            "Admin floor is nominal: assignment %s was moved off Hub Admin "
            "leaving %s hub-wide Hub Admin assignment(s), none of which "
            "belong to a hub user who has signed in. Nobody who has signed in "
            "can administer the hub. BOOTSTRAP_ADMIN_EMAILS (plan §6.5) is the "
            "recovery path.",
            assignment.id,
            remaining,
        )


def update_assignment(
    db: Session,
    assignment_id: int,
    *,
    role_id: int | None,
    scope_id: int | None,
) -> dict | None:
    """In-place edit of an existing assignment's role and/or scope (owner
    decision, 2026-09-12 — the Hub-Admin management table's "update" action).
    Caller (the router) must already have: validated `require_hub_admin`,
    confirmed any provided `role_id`/`scope_id` exist, and refused a ⊥
    (public) role or scope other than the exact default pair via
    `_assert_not_public`.

    Deliberately a true in-place mutation — same `id`, same `created_at`,
    same `granted_by_*` — rather than delete-and-recreate, so editing an
    assignment's scope (say) does not rewrite who is on record as having
    granted it or when. Whichever of `role_id`/`scope_id` is left `None`
    keeps the assignment's current value, the same partial-update shape as
    `rbac_service.update_role`/`update_scope`.

    Returns `None` if the assignment does not exist, so the router can 404 —
    matching `delete_assignment`'s shape below rather than raising.
    """
    assignment = get_assignment(db, assignment_id)
    if assignment is None:
        return None

    new_role_id = role_id if role_id is not None else assignment.role_id
    new_scope_id = scope_id if scope_id is not None else assignment.scope_id

    if (new_role_id, new_scope_id) != (assignment.role_id, assignment.scope_id):
        duplicate = (
            db.query(RoleAssignmentV2)
            .filter(
                RoleAssignmentV2.user_id == assignment.user_id,
                RoleAssignmentV2.role_id == new_role_id,
                RoleAssignmentV2.scope_id == new_scope_id,
                RoleAssignmentV2.id != assignment.id,
            )
            .first()
        )
        if duplicate is not None:
            raise RbacGraphError(
                "duplicate_assignment",
                "This user already holds that role on that scope.",
                user_id=assignment.user_id,
                role_id=new_role_id,
                scope_id=new_scope_id,
            )

    _assert_update_does_not_remove_last_hub_admin(db, assignment, new_role_id, new_scope_id)

    assignment.role_id = new_role_id
    assignment.scope_id = new_scope_id
    db.flush()

    email = _hub_user_email_map(db, {assignment.user_id}).get(assignment.user_id, "")
    return _serialize(assignment, email)


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
    2026-08-24 §3.2): granting requires the target to already have a `users`
    row signing in as that email. Since the 2026-10 merge that includes HR
    rows nobody has signed in as yet and admin-added people, so a role can
    now be granted BEFORE someone's first sign-in. No auto-create-on-grant —
    an unknown email is `target_not_provisioned`, below.
    """
    normalized_target = target_email.strip().lower()
    target = find_user_by_email(db, normalized_target)
    if target is None:
        raise RbacGraphError(
            "target_not_provisioned",
            (
                f"{target_email!r} is not a Hub user, so there is no account "
                f"to grant a role to. Add them on the Access Control page's "
                f"Users tab first."
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


def live_admin_pairs(db: Session, hub_admin_role_id: int, universal_scope_ids: set[int]):
    """Query over the literal admin pairs whose holder is NOT soft-deleted —
    the only ones the admin floor counts (plan_admin_soft_delete_user §3.4).
    A deleted admin's pair is kept for Restore but administers nothing, so
    counting it would let the floor be "satisfied" by nobody."""
    return (
        db.query(RoleAssignmentV2)
        .join(UserV2, UserV2.id == RoleAssignmentV2.user_id)
        .filter(
            RoleAssignmentV2.role_id == hub_admin_role_id,
            RoleAssignmentV2.scope_id.in_(universal_scope_ids),
            UserV2.not_deleted(),
        )
    )


def _holder_is_deleted(db: Session, assignment: RoleAssignmentV2) -> bool:
    """A deleted user's pair is not a live admin, so moving or removing it
    can't lower the floor."""
    return bool(db.query(UserV2.is_deleted).filter(UserV2.id == assignment.user_id).scalar())


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
    Deleting a `users` row therefore removes that person's assignments
    WITHOUT passing through this function — including the last admin's. The
    app never hard-deletes a user: the admin delete
    (`DELETE /v2/rbac/admin/hub-users/{id}`) is a SOFT delete since
    2026-10-19 and keeps every assignment, but it still removes a live
    admin, so it runs its own pre-check,
    `user_admin_service._refuse_last_hub_admin`, which counts other USERS
    rather than other assignments. Direct database access is still
    unguarded; only a database trigger would close that.

    SOFT-DELETED HOLDERS DON'T COUNT (plan_admin_soft_delete_user §3.4):
    `remaining` goes through `live_admin_pairs`, and a deleted holder's own
    pair is not protected (removing it can't lower the floor).

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

    `has_signed_in` is deliberately NOT part of the count (owner decision, same
    date). The flag is enforced nowhere else in this codebase — open finding
    §2.4 — and making the admin floor the single exception would be
    inconsistent. Instead, an allowed delete that leaves no SIGNED-IN admin
    logs a warning: the floor is then nominal, satisfied by somebody who has
    never signed in.
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
        or _holder_is_deleted(db, assignment)
    ):
        return

    # Only non-deleted holders count (plan_admin_soft_delete_user §3.4).
    remaining = (
        live_admin_pairs(db, hub_admin_role_id, universal_scope_ids)
        .filter(RoleAssignmentV2.id != assignment.id)
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
    # `has_signed_in` note above: the count deliberately ignores the flag, so this
    # is the observable signal that it would have mattered.
    signed_in_remaining = (
        live_admin_pairs(db, hub_admin_role_id, universal_scope_ids)
        .filter(RoleAssignmentV2.id != assignment.id, UserV2.has_signed_in.is_(True))
        .count()
    )
    if signed_in_remaining == 0:
        logger.warning(
            "Admin floor is nominal: assignment %s was revoked leaving %s "
            "hub-wide Hub Admin assignment(s), none of which belong to a "
            "hub user who has signed in. Nobody who has signed in can "
            "administer the hub. "
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
