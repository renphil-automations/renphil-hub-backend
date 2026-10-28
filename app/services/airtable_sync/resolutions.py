"""The items API's service: list conflicts / warnings, and resolve one
(v1 plan §5.4, C1, C4; plan v2 §6; owner 2026-10-22: lands in phase 4).

A resolution
  - takes the sync lock and refuses (409 `run_in_progress`) while a live run
    holds the run lock, and (409 `bulk_in_progress`) while a live background
    bulk job other than its caller does (`bulk_jobs.refuse_if_busy`);
  - checks the choice against the kind's choices AND the ones the item's
    payload offered (e.g. `delete_scope` only when the scope was unused);
  - marks the item `resolved` (resolution, resolution_data, who, when);
  - applies the choice at once, from the sync record's `latest_airtable`
    (C4), logging each change with `resolution_item_id` and `actor_email`
    (`run_id` NULL), plus one `item_resolved` row.
A person-level answer (Link / Create new / Assign default) is applied only
when no other person-level item is open for that person and no answered
item of theirs still in force is a Skip; otherwise the next run applies it
(remembered answers drive the Users step). A pair answer (Accept Airtable,
Remove, Keep) applies at once when the person is a live, linked Hub user
with no open person-level item and no other item holds the pair; otherwise
the Program Roles step applies the remembered answer next run. Keep current
only moves the base, so it always applies. Skip / Keep are remembered by
fingerprint (G1).

A person's HR answers (`field_conflict` Accept Airtable / Keep current, E1's
Apply + …) are person-level answers too: the person's fields, link or
creation all apply together once their last open item is answered (C4).

Items with no buttons ("—") and kinds a later phase answers → 409.
Every refusal is an `AirtableSyncError` (409); the router rolls back.
After the commit, `PostCommit` deletes replaced headshot objects (A-5) and
sweeps the edit locks (lock_sweep.py). Bulk resolve (v1 §5.4 "Accept all /
Keep all") is `resolve_item` in a loop, stopping at the first error: here
in one request (`bulk_resolve`), or in the background with tracked progress
(bulk_jobs.py, what the tab uses).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db_v2.models.airtable_sync import AirtableSyncItemV2, AirtableSyncLogV2, AirtableSyncRecordV2
from app.db_v2.models.role import RoleV2
from app.db_v2.models.role_assignment import RoleAssignmentV2
from app.db_v2.models.scope import ScopeEdgeV2, ScopeV2
from app.db_v2.models.user import UserV2
from app.config import get_settings
from app.services import rbac_assignment_service, rbac_service, user_admin_service
from app.services.airtable_sync.hr_fields import Plan
from app.services.airtable_sync.items import NOT_REMEMBERED, ItemBook
from app.services.airtable_sync.lock_sweep import release_unauthorized_locks
from app.services.airtable_sync.program_roles_step import PairWriter, Target, retire_person_pairs, split_key
from app.services.airtable_sync.bulk_jobs import refuse_if_busy
from app.services.airtable_sync.runner import _as_text, as_utc
from app.services.airtable_sync.runs_service import _like_pattern
from app.services.airtable_sync.scopes_step import SYNC_EDGE, name_key
from app.services.airtable_sync.settings_service import AirtableSyncError
from app.services.airtable_sync.r2 import R2Config
from app.services.airtable_sync.users_step import (
    APPLY_EMAIL,
    EMAIL_CHANGE,
    EMAIL_TAKEN,
    FIELD_CONFLICT,
    PERSON_KINDS,
    SYNC_SOURCE,
    UserWriter,
    answered_match,
    field_item_specs,
    match_person,
)
from app.services.rbac_graph_service import RbacGraphError, delete_scope_edge

if TYPE_CHECKING:
    from app.dependencies import CurrentHubUser

# What each kind offers. The payload's own `choices` narrows it further.
CHOICES: dict[str, tuple[str, ...]] = {
    "scope_name_conflict": ("accept_airtable", "keep_current"),
    # `create_new` dropped (owner, 2026-10-22): the candidates already own
    # the name case-insensitively, so a new scope's name is always taken.
    "scope_ambiguous_match": ("link", "skip"),
    "scope_deleted": ("delete_scope", "keep_hub_only"),
    "cluster_edge_removed": ("remove_edge", "keep"),
    "possible_match": ("link", "create_new", "skip"),
    "ambiguous_match": ("link", "create_new", "skip"),
    "no_program_roles": ("assign_default", "skip"),
    "user_deleted": ("remove_synced_access", "keep", "delete_user"),
    "assignment_conflict": ("accept_airtable", "keep_current"),
    "assignment_removed": ("remove", "keep"),
    "unmapped_designation": ("skip",),
    "field_conflict": ("accept_airtable", "keep_current"),
    # "Apply + keep the old address as Alias Email" is offered only when the
    # Hub has no alias (the payload's choices say so).
    "work_email_change_signed_in": ("apply_keep_alias", "apply_drop_old", "skip"),
}
# "—" items: they close by themselves once the data is fixed.
NO_CHOICES = frozenset(
    {
        "scope_name_taken",
        "unknown_cluster",
        "scope_type_mismatch",
        "no_work_email",
        "duplicate_work_email",
        "email_taken",
        "duplicate_program_role",
        "missing_program",
    }
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def allowed_choices(item: AirtableSyncItemV2) -> list[str]:
    choices = CHOICES.get(item.kind, ())
    offered = (item.payload or {}).get("choices")
    if offered is not None:
        choices = tuple(c for c in choices if c in offered)
    return list(choices)


# ---------------------------------------------------------
# Listing
# ---------------------------------------------------------


def serialize_item(item: AirtableSyncItemV2) -> dict:
    return {
        "id": item.id,
        "entity": item.entity,
        "airtable_record_id": item.airtable_record_id,
        "hub_id": item.hub_id,
        "subject": item.subject,
        "kind": item.kind,
        "field": item.field or None,
        "payload": item.payload,
        "choices": allowed_choices(item) if item.status == "open" else [],
        "status": item.status,
        "resolution": item.resolution,
        "resolution_data": item.resolution_data,
        "first_run_id": item.first_run_id,
        "last_run_id": item.last_run_id,
        "resolved_by_email": item.resolved_by_email,
        "resolved_at": as_utc(item.resolved_at),
        "created_at": as_utc(item.created_at),
    }


def list_items(
    db: Session,
    *,
    status: str | None,
    kind: str | None,
    entity: str | None,
    q: str | None,
    cursor: int | None,
    limit: int,
) -> dict:
    """Items in id order, `limit` at a time, keyset-paginated like
    `runs_service.list_log`. `status=None` lists every status. `q` matches,
    case-insensitively, the subject, the Airtable record id and the field."""
    query = db.query(AirtableSyncItemV2)
    if status:
        query = query.filter(AirtableSyncItemV2.status == status)
    if kind:
        query = query.filter(AirtableSyncItemV2.kind == kind)
    if entity:
        query = query.filter(AirtableSyncItemV2.entity == entity)
    if q and q.strip():
        pattern = _like_pattern(q.strip())
        query = query.filter(
            or_(
                *(
                    column.ilike(pattern, escape="\\")
                    for column in (AirtableSyncItemV2.subject, AirtableSyncItemV2.airtable_record_id, AirtableSyncItemV2.field)
                )
            )
        )
    if cursor is not None:
        query = query.filter(AirtableSyncItemV2.id > cursor)
    rows = query.order_by(AirtableSyncItemV2.id.asc()).limit(limit + 1).all()
    more = len(rows) > limit
    rows = rows[:limit]
    return {"items": [serialize_item(r) for r in rows], "next_cursor": rows[-1].id if more and rows else None}


# ---------------------------------------------------------
# Resolving
# ---------------------------------------------------------


class _ResolutionLog:
    """`ctx.log`'s signature, for changes a resolution makes: `run_id`
    NULL, `resolution_item_id` and `actor_email` set."""

    def __init__(self, db: Session, item: AirtableSyncItemV2, actor_email: str | None) -> None:
        self.db, self.item, self.actor_email = db, item, actor_email

    def __call__(self, entity: str, action: str, **kw: Any) -> None:
        self.db.add(
            AirtableSyncLogV2(
                run_id=None,
                resolution_item_id=self.item.id,
                actor_email=self.actor_email,
                entity=entity,
                action=action,
                airtable_record_id=kw.get("airtable_record_id"),
                hub_id=kw.get("hub_id"),
                subject=kw.get("subject"),
                field=kw.get("field"),
                old_value=_as_text(kw.get("old_value")),
                new_value=_as_text(kw.get("new_value")),
            )
        )


@dataclass
class _Res:
    db: Session
    item: AirtableSyncItemV2
    choice: str
    data: dict
    log: _ResolutionLog
    current: CurrentHubUser
    # Writers whose old headshots are deleted once the router has committed.
    after_commit: list[UserWriter] = field(default_factory=list)

    @property
    def rid(self) -> str:
        return self.item.airtable_record_id

    def record(self, entity: str | None = None) -> AirtableSyncRecordV2 | None:
        return (
            self.db.query(AirtableSyncRecordV2)
            .filter_by(entity=entity or self.item.entity, airtable_record_id=self.rid)
            .one_or_none()
        )

    def write(self, action: str, **kw: Any) -> None:
        kw.setdefault("airtable_record_id", self.rid)
        kw.setdefault("subject", self.item.subject)
        self.log(self.item.entity, action, **kw)


def _stale(what: str) -> AirtableSyncError:
    return AirtableSyncError(
        "item_stale",
        f"The {what} changed since this item was raised. The next run refreshes or closes it.",
    )


# ── scopes ──────────────────────────────────────────────────────────────


def _linked_scope(res: _Res) -> ScopeV2:
    scope = res.db.get(ScopeV2, res.item.hub_id) if res.item.hub_id is not None else None
    if scope is None or scope.airtable_record_id != res.rid:
        raise _stale("scope")
    return scope


def _airtable_name(res: _Res, record: AirtableSyncRecordV2 | None) -> str:
    name = ((record.latest_airtable or {}).get("name") if record else None) or None
    if not name:
        raise _stale("Airtable record")
    return name


def _name_taken(db: Session, name: str, *, entity: str, exclude_id: int | None) -> dict | None:
    """Phase 3's rule: names are taken case-insensitively, and a program may
    never take a cluster's name (A-2)."""
    key = name_key(name)
    for sid, sname in db.query(ScopeV2.id, ScopeV2.name):
        if sid != exclude_id and name_key(sname) == key:
            return {"kind": "scope", "scope_id": sid, "name": sname}
    if entity == "program":
        for crid, latest in db.query(AirtableSyncRecordV2.airtable_record_id, AirtableSyncRecordV2.latest_airtable).filter(
            AirtableSyncRecordV2.entity == "cluster"
        ):
            if name_key((latest or {}).get("name")) == key:
                return {"kind": "cluster", "cluster_record_id": crid}
    return None


def _scope_name_conflict(res: _Res) -> dict:
    scope = _linked_scope(res)
    record = res.record()
    if record is None:
        raise _stale("sync record")
    airtable = _airtable_name(res, record)
    if res.choice == "accept_airtable" and scope.name != airtable:
        taken = _name_taken(res.db, airtable, entity=res.item.entity, exclude_id=scope.id)
        if taken is not None:
            raise AirtableSyncError("scope_name_taken", f"{airtable!r} is already taken.", taken_by=taken)
        old = scope.name
        rbac_service.update_scope(res.db, scope.id, name=airtable, description=None, description_provided=False)
        res.write("scope_renamed", hub_id=scope.id, field="name", old_value=old, new_value=airtable)
    # Both choices move the base to Airtable's value (Keep: M3).
    record.base = {**(record.base or {}), "name": airtable}
    return {"applied": True, "scope_id": scope.id}


def _scope_ambiguous_match(res: _Res) -> dict:
    if res.choice == "skip":
        return {"applied": True}
    candidates = {c.get("scope_id") for c in (res.item.payload or {}).get("candidates", [])}
    sid = res.data.get("scope_id")
    if not isinstance(sid, int) or isinstance(sid, bool) or sid not in candidates:
        raise AirtableSyncError("invalid_data", "Pick one of the candidate scopes (data.scope_id).", candidates=sorted(c for c in candidates if c is not None))
    db, entity = res.db, res.item.entity
    scope = db.get(ScopeV2, sid)
    taken = db.query(ScopeV2.id).filter(ScopeV2.airtable_record_id == res.rid).first()
    if (
        scope is None
        or scope.airtable_record_id is not None
        or scope.is_universal
        or scope.is_public
        or scope.is_system
        or scope.type not in (None, entity)
        or taken is not None
    ):
        raise AirtableSyncError("scope_unavailable", "That scope can no longer be linked.", scope_id=sid)
    record = res.record()
    airtable = _airtable_name(res, record)
    scope.airtable_record_id = res.rid
    scope.type = entity
    scope.updated_at = _utc_now()
    record.hub_id = scope.id
    record.base = {"name": airtable}
    res.write("linked", hub_id=scope.id, old_value=scope.name, new_value=res.rid)
    if scope.name != airtable:
        res.write("hub_differs", hub_id=scope.id, field="name", old_value=airtable, new_value=scope.name)
    res.data = {"scope_id": sid}
    return {"applied": True, "scope_id": sid}


def _scope_deleted(res: _Res) -> dict:
    scope = _linked_scope(res)
    record = res.record()
    if res.choice == "delete_scope":
        name = scope.name
        rbac_service.delete_scope(res.db, scope.id)  # refuses while in use
        res.write("scope_deleted", hub_id=scope.id, old_value=name)
        if record is not None:
            record.hub_id = None
        return {"applied": True, "scope_id": scope.id}
    # Keep as Hub-only: the Record ID goes, `type` stays (v2 §4.1), and the
    # sync record is retired (a returning record is a first link again).
    scope.airtable_record_id = None
    scope.updated_at = _utc_now()
    if record is not None:
        record.hub_id = None
        record.base = None
    res.write("scope_unlinked", hub_id=scope.id, old_value=res.rid)
    return {"applied": True, "scope_id": scope.id}


def _cluster_edge_removed(res: _Res) -> dict:
    db, payload, crid = res.db, res.item.payload or {}, res.item.field
    program_id = payload.get("program_scope_id") or res.item.hub_id
    cluster_id = payload.get("cluster_scope_id")
    if cluster_id is None:
        row = db.query(ScopeV2.id).filter(ScopeV2.airtable_record_id == crid).first()
        cluster_id = row[0] if row else None
    edge = (
        db.query(ScopeEdgeV2).filter_by(parent_scope_id=cluster_id, child_scope_id=program_id).one_or_none()
        if cluster_id is not None and program_id is not None
        else None
    )
    log = {"hub_id": program_id, "field": "cluster", "old_value": {"cluster": crid, "cluster_scope_id": cluster_id}}
    if edge is not None and edge.source == SYNC_EDGE:
        if res.choice == "remove_edge":
            delete_scope_edge(db, cluster_id, program_id)
            res.write("edge_removed", **log)
        else:
            edge.source = "manual"
            res.write("edge_kept_manual", **log)
    # Either way the cluster leaves the agreed set.
    record = res.record()
    if record is not None and record.base:
        record.base = {**record.base, "clusters": sorted(c for c in record.base.get("clusters") or [] if c != crid)}
    return {"applied": True, "edge_found": edge is not None}


# ── users ───────────────────────────────────────────────────────────────


def _answers_in_force(res: _Res, record: AirtableSyncRecordV2) -> dict[tuple[str, str], tuple[str, Any, dict]]:
    """The person's answered items that still applied in their last run
    (the one being resolved included): (kind, field) → (resolution, data,
    payload)."""
    rows = (
        res.db.query(AirtableSyncItemV2)
        .filter(
            AirtableSyncItemV2.entity == "user",
            AirtableSyncItemV2.airtable_record_id == res.rid,
            AirtableSyncItemV2.kind.in_(PERSON_KINDS),
            AirtableSyncItemV2.status == "resolved",
            AirtableSyncItemV2.resolution.notin_(NOT_REMEMBERED),
        )
        .order_by(AirtableSyncItemV2.id)
    )
    out: dict[tuple[str, str], tuple[str, Any, dict]] = {}
    for it in rows:
        if it.id == res.item.id:  # its data is stored after the applier ran
            out[(it.kind, it.field or "")] = (res.choice, res.data, it.payload or {})
        elif record.last_seen_run_id is not None and it.last_run_id == record.last_seen_run_id:
            out[(it.kind, it.field or "")] = (it.resolution, it.resolution_data, it.payload or {})
    return out


def _unanswered(plan: Plan, answers: dict[tuple[str, str], tuple[str, Any, dict]]) -> list[str]:
    """What the HR merge now finds that no answer in force covers: the Hub
    changed since the run (the next run raises it)."""
    out: list[str] = []
    for c in plan.conflicts:
        if answers.get((FIELD_CONFLICT, c.field), ("",))[0] not in ("accept_airtable", "keep_current"):
            out.append(FIELD_CONFLICT)
    if plan.email_change is not None:
        choice = answers.get((EMAIL_CHANGE, ""), ("",))[0]
        if choice not in APPLY_EMAIL or (choice == "apply_keep_alias" and not plan.alias_offer):
            out.append(EMAIL_CHANGE)
    if plan.taken:
        out.append(EMAIL_TAKEN)
    return sorted(set(out))


def _raise_now(res: _Res, record: AirtableSyncRecordV2, plan: Plan, m: Any, answers: dict) -> None:
    """Open the merge's unanswered items exactly as the step would (same
    inputs, so the next run refreshes them), dated to the person's last run
    so an answer to them counts as in force (`_answers_in_force`)."""
    ctx = SimpleNamespace(db=res.db, dry_run=False, run_id=record.last_seen_run_id, log=res.log)
    book = ItemBook(ctx, "user")
    for spec in field_item_specs(plan, m.user):
        if (spec.kind, spec.field) in answers:
            continue
        book.raise_item(
            res.rid, spec.kind, inputs=spec.inputs, payload=spec.payload, subject=res.item.subject, hub_id=spec.hub_id, field=spec.field
        )
    res.db.flush()


def _apply_person(res: _Res) -> dict:
    """C4: with the person's last open item answered, link or create them,
    apply their HR fields (with every answer in force) and give the default
    pair if answered, now, from `latest_airtable`. Otherwise the answer waits
    for the next run."""
    db, rid = res.db, res.rid
    waiting = sorted(
        k
        for (k,) in db.query(AirtableSyncItemV2.kind).filter(
            AirtableSyncItemV2.entity == "user",
            AirtableSyncItemV2.airtable_record_id == rid,
            AirtableSyncItemV2.status == "open",
            AirtableSyncItemV2.kind.in_(PERSON_KINDS),
        )
    )
    if waiting:
        return {"applied": False, "waiting_on": waiting}
    record = res.record("user")
    if record is None or not record.latest_airtable:
        return {"applied": False, "waiting_on": ["next_run"]}
    answers = _answers_in_force(res, record)
    skipped = sorted({k for (k, _f), (resolution, _d, _p) in answers.items() if resolution == "skip"})
    if skipped:
        return {"applied": False, "waiting_on": skipped}

    fields = dict(record.latest_airtable)
    writer = UserWriter(db, res.log, dry_run=False, run_id=None, r2=R2Config.from_settings(get_settings()))
    m = match_person(writer.index, rid, fields)
    if m.outcome == "item" and (m.kind, "") in answers:
        resolution, data, payload = answers[(m.kind, "")]
        m = answered_match(writer.index, rid, m.kind, resolution, data, payload) or m
    if m.outcome in ("item", "deleted"):
        # The Hub changed since the run: the next run re-checks the person.
        return {"applied": False, "waiting_on": [m.kind or "skipped_deleted_in_hub"]}
    plan = writer.plan_for(rid, m, fields)
    unanswered = _unanswered(plan, answers) if plan is not None else []
    if unanswered:
        # The merge found items no run has raised yet (the person was only
        # just matched, by this answer): open them now, so they can be
        # answered without waiting for a run.
        _raise_now(res, record, plan, m, answers)
        return {"applied": False, "waiting_on": unanswered}
    want_default = answers.get(("no_program_roles", ""), ("",))[0] == "assign_default"
    step_answers = {key: (resolution, data) for key, (resolution, data, _p) in answers.items()}
    hu = writer.apply(rid, fields, m, want_default=want_default, plan=plan, answers=step_answers)
    res.after_commit.append(writer)  # its old headshots, once the Hub has switched
    return {"applied": True, "user_id": hu.id}


def _match_item(res: _Res) -> dict:
    if res.choice == "skip":
        return {"applied": True}
    if res.choice == "link":
        payload = res.item.payload or {}
        if res.item.kind == "possible_match":
            target = (payload.get("candidate") or {}).get("user_id")
        else:
            ids = [c.get("user_id") for c in payload.get("candidates", [])]
            target = res.data.get("user_id")
            if not isinstance(target, int) or isinstance(target, bool) or target not in ids:
                raise AirtableSyncError("invalid_data", "Pick one of the candidates (data.user_id).", candidates=ids)
        user = res.db.get(UserV2, target) if target is not None else None
        other = res.db.query(UserV2.id).filter(UserV2.airtable_record_id == res.rid, UserV2.id != target).first()
        if user is None or user.is_deleted or user.airtable_record_id not in (None, res.rid) or other is not None:
            raise AirtableSyncError("candidate_unavailable", "That user can no longer be linked.", user_id=target)
        res.data = {"user_id": target}
    return _apply_person(res)


def _no_program_roles(res: _Res) -> dict:
    if res.choice == "skip":
        return {"applied": True}
    return _apply_person(res)


def _field_conflict(res: _Res) -> dict:
    # Accept Airtable and Keep current both move the base (M3); the person's
    # writes wait for their last open item (C4), like every person answer.
    return _apply_person(res)


def _work_email_change(res: _Res) -> dict:
    if res.choice == "skip":  # remembered: the person stays held (v1 §5.4)
        return {"applied": True}
    return _apply_person(res)


def _user_deleted(res: _Res) -> dict:
    db = res.db
    user = db.get(UserV2, res.item.hub_id) if res.item.hub_id is not None else None
    if user is None or user.is_deleted or user.airtable_record_id != res.rid:
        raise _stale("user")
    if res.choice == "delete_user":
        user_admin_service.delete_user(db, user.id, res.current)  # its guards → 409
        res.write("user_soft_deleted", hub_id=user.id)
        return {"applied": True, "user_id": user.id}
    record = res.record("user")
    if res.choice == "remove_synced_access":
        rows = db.query(RoleAssignmentV2).filter_by(user_id=user.id, source=SYNC_SOURCE).order_by(RoleAssignmentV2.id).all()
        for a in rows:
            removed = {"assignment_id": a.id, "role_id": a.role_id, "scope_id": a.scope_id}
            rbac_assignment_service.delete_assignment(db, a.id)  # last-admin guard
            res.write("assignment_removed", hub_id=user.id, old_value=removed)
        if record is not None:
            record.managed_assignment_id = None
    # Both Remove and Keep unlink the row: it becomes a plain Hub row. Its
    # Program Roles pairs stop being agreed, so a return links them afresh.
    user.airtable_record_id = None
    user.updated_at = _utc_now()
    if record is not None:
        record.hub_id = None
    retire_person_pairs(db, res.rid)
    res.write("unlinked", hub_id=user.id, old_value=res.rid)
    return {"applied": True, "user_id": user.id}


# ── program roles (pairs) ───────────────────────────────────────────────


def _pair_person(res: _Res, record: AirtableSyncRecordV2 | None) -> tuple[UserV2 | None, list[str]]:
    """The pair's person, when a pair answer can apply now: a live, linked
    Hub user, the record's own, with no open person-level item (v2 §3.5).
    Otherwise (None, what it waits on) and the next run applies the answer."""
    db = res.db
    employee, _program = split_key(res.rid)
    if record is None:
        raise _stale("sync record")
    user = db.query(UserV2).filter(UserV2.airtable_record_id == employee).one_or_none()
    if user is None:
        return None, ["user_not_synced"]
    if user.is_deleted:
        return None, ["skipped_deleted_in_hub"]
    if record.hub_id != user.id:
        raise _stale("user")
    held = sorted(
        {
            k
            for (k,) in db.query(AirtableSyncItemV2.kind).filter(
                AirtableSyncItemV2.entity == "user",
                AirtableSyncItemV2.airtable_record_id == employee,
                AirtableSyncItemV2.status == "open",
            )
        }
    )
    return (None, held) if held else (user, [])


def _pair_log(res: _Res, user: UserV2) -> dict:
    return {"airtable_record_id": res.rid, "hub_id": user.id, "subject": res.item.subject}


def _assignment_conflict(res: _Res) -> dict:
    db, record = res.db, res.record()
    target = Target.from_dict((record.latest_airtable or {}).get("target")) if record is not None else None
    if target is None:
        raise _stale("Airtable record")
    if res.choice == "keep_current":
        record.base = target.as_base()  # M3: the Hub is untouched
        return {"applied": True}
    user, waiting = _pair_person(res, record)
    if user is None:
        return {"applied": False, "waiting_on": waiting}
    others = sorted(
        k
        for (k,) in db.query(AirtableSyncItemV2.kind).filter(
            AirtableSyncItemV2.entity == res.item.entity,
            AirtableSyncItemV2.airtable_record_id == res.rid,
            AirtableSyncItemV2.status == "open",
        )
    )
    if others:  # the pair is held by another item
        return {"applied": False, "waiting_on": others}
    if db.get(RoleV2, target.role_id) is None or db.get(ScopeV2, target.scope_id) is None:
        raise _stale("mapped role or program scope")
    writer = PairWriter(db, res.log, dry_run=False, run_id=None)
    writer.apply_target(
        res.rid, user.id, target, record.latest_airtable, log=_pair_log(res, user), replaced=writer.managed_row(res.rid, user.id)
    )
    return {"applied": True, "assignment_id": record.managed_assignment_id}


def _assignment_removed(res: _Res) -> dict:
    record = res.record()
    user, waiting = _pair_person(res, record)
    if user is None:
        return {"applied": False, "waiting_on": waiting}
    writer = PairWriter(res.db, res.log, dry_run=False, run_id=None)
    row = writer.managed_row(res.rid, user.id)
    log = _pair_log(res, user)
    if row is None or row.source != SYNC_SOURCE:  # an admin removed it meanwhile
        writer.retire(res.rid, hub_id=user.id, subject=res.item.subject)
        return {"applied": True, "assignment_found": False}
    if res.choice == "remove":
        writer.remove(res.rid, row, log=log)  # last-admin guard → 409
    else:
        writer.keep_as_manual(res.rid, row, log=log)
    return {"applied": True, "assignment_id": row.id}


def _unmapped_designation(res: _Res) -> dict:
    # Skip only: remembered by fingerprint, it holds the pair until the
    # Designation changes; mapping the Designation closes the item.
    return {"applied": True}


APPLIERS = {
    "scope_name_conflict": _scope_name_conflict,
    "scope_ambiguous_match": _scope_ambiguous_match,
    "scope_deleted": _scope_deleted,
    "cluster_edge_removed": _cluster_edge_removed,
    "possible_match": _match_item,
    "ambiguous_match": _match_item,
    "no_program_roles": _no_program_roles,
    "user_deleted": _user_deleted,
    "assignment_conflict": _assignment_conflict,
    "assignment_removed": _assignment_removed,
    "unmapped_designation": _unmapped_designation,
    "field_conflict": _field_conflict,
    "work_email_change_signed_in": _work_email_change,
}


def _check_choice(item: AirtableSyncItemV2, choice: str) -> None:
    if item.status != "open":
        raise AirtableSyncError("item_not_open", "This item is no longer open.", item_id=item.id, status=item.status)
    if item.kind in NO_CHOICES:
        raise AirtableSyncError(
            "item_has_no_choices", "This item has no choices: it closes by itself once the data is fixed.", kind=item.kind
        )
    if item.kind not in APPLIERS:
        raise AirtableSyncError("unsupported_kind", f"Items of kind {item.kind!r} can't be resolved yet.", kind=item.kind)
    if choice in allowed_choices(item):
        return
    if item.kind == "scope_ambiguous_match" and choice == "create_new":
        raise AirtableSyncError(
            "scope_name_taken", "A new scope can't take this name: the candidates already have it. Link one, or Skip."
        )
    if item.kind == "ambiguous_match" and choice == "create_new":
        raise AirtableSyncError(
            "email_taken", "A new user can't have these emails: the candidates already have them. Link one, or Skip."
        )
    raise AirtableSyncError("invalid_choice", f"{choice!r} is not a choice for this item.", choices=allowed_choices(item))


class PostCommit:
    """What a resolution leaves for after its commit (A-5, owner
    2026-10-23): delete the old headshot objects it switched away from, then
    sweep the edit locks (lock_sweep.py). The router calls `run` after it
    has committed; a rollback simply drops this object."""

    def __init__(self) -> None:
        self.writers: list[UserWriter] = []

    def run(self, db: Session, *, sweep: bool = True) -> None:
        for writer in self.writers:
            if writer.pending_deletes:
                writer.delete_old_headshots()
        self.writers.clear()
        db.commit()  # the delete log rows
        if sweep:
            release_unauthorized_locks(db)


def resolve_item(
    db: Session,
    item_id: int,
    *,
    choice: str,
    data: dict | None,
    current: CurrentHubUser,
    post_commit: PostCommit | None = None,
    bulk_job_id: int | None = None,
) -> dict | None:
    """None when there is no such item (404). Flushes; the router commits,
    or rolls back on an `AirtableSyncError`, then runs `post_commit`.
    `bulk_job_id`: the background job making this call, which its own
    `bulk_in_progress` check lets through."""
    refuse_if_busy(db, own_job_id=bulk_job_id)
    item = db.query(AirtableSyncItemV2).filter(AirtableSyncItemV2.id == item_id).with_for_update().one_or_none()
    if item is None:
        return None
    _check_choice(item, choice)

    actor = (current.email or "").strip().lower() or None
    res = _Res(db=db, item=item, choice=choice, data=dict(data or {}), log=_ResolutionLog(db, item, actor), current=current)
    if post_commit is not None:
        res.after_commit = post_commit.writers
    # Resolved first: a person-level answer looks at what is still open.
    item.status = "resolved"
    item.resolution = choice
    item.resolved_by_email = actor
    item.resolved_at = _utc_now()
    db.flush()
    try:
        outcome = APPLIERS[item.kind](res)
        item.resolution_data = res.data or None
        res.write("item_resolved", hub_id=item.hub_id, field=item.kind, new_value={"choice": choice, "data": res.data or None, **outcome})
        db.flush()
    except RbacGraphError as exc:
        raise AirtableSyncError(exc.code, exc.message, **exc.details) from exc
    except IntegrityError as exc:
        raise AirtableSyncError("write_refused", "The database refused the change (a unique value is already taken).") from exc

    open_items = (
        db.query(AirtableSyncItemV2)
        .filter_by(entity=item.entity, airtable_record_id=item.airtable_record_id, status="open")
        .order_by(AirtableSyncItemV2.id)
        .all()
    )
    return {"item": serialize_item(item), "outcome": outcome, "open_items": [serialize_item(i) for i in open_items]}


def bulk_targets(db: Session, *, item_ids: list[int] | None, run_id: int | None, kind: str | None, choice: str) -> list[int]:
    """The items a bulk resolve works through, in id order. `item_ids` are
    taken as given (each must offer the choice, or the loop stops there);
    `run_id` picks the items still open from that run that offer it."""
    if item_ids is not None:
        return sorted(set(item_ids))
    query = db.query(AirtableSyncItemV2).filter(AirtableSyncItemV2.status == "open", AirtableSyncItemV2.last_run_id == run_id)
    if kind:
        query = query.filter(AirtableSyncItemV2.kind == kind)
    return [i.id for i in query.order_by(AirtableSyncItemV2.id) if i.kind in APPLIERS and choice in allowed_choices(i)]


def bulk_resolve(
    db: Session,
    *,
    item_ids: list[int] | None,
    run_id: int | None,
    kind: str | None,
    choice: str,
    current: CurrentHubUser,
) -> dict:
    """v1 §5.4's bulk actions ("Accept all from Airtable" / "Keep all"):
    `resolve_item` in a loop, one transaction per item, stopping at the first
    error (that item rolled back, the earlier ones kept). Items are taken in
    id order, so a person's last item is the one whose answer applies all of
    theirs (C4). Owns its commits; sweeps the locks once at the end. A live
    run → `SyncRunInProgress` before anything is touched (409), as is a
    live background job (`bulk_in_progress`)."""
    refuse_if_busy(db)
    ids = bulk_targets(db, item_ids=item_ids, run_id=run_id, kind=kind, choice=choice)
    db.rollback()  # the targets' read is not part of any item's transaction
    resolved: list[dict] = []
    failed: dict | None = None
    not_attempted: list[int] = []
    for n, item_id in enumerate(ids):
        post = PostCommit()
        try:
            out = resolve_item(db, item_id, choice=choice, data=None, current=current, post_commit=post)
            if out is None:
                raise AirtableSyncError("item_not_found", "No such item.", item_id=item_id)
        except AirtableSyncError as exc:
            db.rollback()
            failed = {"item_id": item_id, "code": exc.code, "message": exc.message, **exc.details}
            not_attempted = ids[n + 1 :]
            break
        db.commit()
        post.run(db, sweep=False)
        resolved.append({"item_id": item_id, "outcome": out["outcome"]})
    if resolved:
        release_unauthorized_locks(db)
    return {"resolved": resolved, "failed": failed, "not_attempted": not_attempted}
