"""The Program Roles step (plan v2 §3.4 A1–A4, §3.5, §8 step 6; A-1).

A row's identity is the PAIR "<Employee Record ID>|<Original Program Record
ID>" (A1), kept as an `airtable_sync_records` row (`entity =
'program_role'`, `hub_id` = the person's `users.id`) whose
`managed_assignment_id` points at the sync's `role_assignments` row and whose
`base` is the agreed `{role_id, scope_id, designation}`.

Per pair, one transaction, in this order (A3, then A2):

  employee not in the Users snapshot → `employee_not_in_users`, counted, no
           item (D-2; the person, if linked, gets `user_deleted`)
  person   soft-deleted in the Hub → `skipped_deleted_in_hub` (M7: never
           given, changed or stripped); held by a person-level item, or not
           a Hub user yet → `user_not_synced`: the pair waits, no item, and
           its items are carried
  duplicate the pair appears twice → `duplicate_program_role` ("—")
  mapping  "Don't sync" → skipped quietly (`skipped_dont_sync`); if the sync
           already gave the pair → `assignment_removed` (owner, 2026-10-23:
           losing access is never silent, v1 M5)
  program  not in the Public snapshot → `missing_program` ("—", A-1); known
           but its scope (or its C2 alias's cluster scope) doesn't exist yet
           → `scope_not_synced`, waits
  mapping  unmapped, or its role was deleted → `unmapped_designation` (Skip)
  3-way    A = the mapped (role, program scope), B = `base`, H = the managed
           row (gone when an admin deleted it):
             no base       adopt the person's identical row of ANY source
                           (`assignment_adopted`), else insert
             A≠B, H=B      replace (`assignment_replaced`); an identical row
                           already held is adopted, the old sync row removed
             A=B, H≠B      keep the Hub (`hub_differs`)
             A≠B, H≠B      `assignment_conflict` (Accept Airtable / Keep
                           current); H=A just moves the base

Afterwards, every recorded pair not in this run whose sync row still exists,
for a person in Users who is linked, live and not held → `assignment_removed`
(Remove, through `delete_assignment` so the last-admin guard applies / Keep:
the row becomes `manual`). A pair whose row an admin already removed is
retired quietly (`pair_retired`).

Pair-level items hold only their pair (v2 §3.5). A remembered Skip holds; a
remembered Accept Airtable / Keep current / Remove / Keep is applied here.
Only `airtable_sync` rows are ever modified or removed; the only other write
is A4's adopt, which flips an identical row's `source`. The default (User,
Hub) pair is never touched: the mapping can't target the public role.

Preview computes everything and writes only log rows. People and scopes are
resolved through the earlier steps' overlays (`users_step.VIRTUAL_USERS`,
`scopes_step.VIRTUAL_SCOPES` / `PROGRAM_ALIASES`) first, then the DB.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db_v2.models.airtable_sync import (
    AirtableDesignationMappingV2,
    AirtableSyncItemV2,
    AirtableSyncRecordV2,
)
from app.db_v2.models.role import RoleV2
from app.db_v2.models.role_assignment import RoleAssignmentV2
from app.db_v2.models.scope import ScopeV2
from app.db_v2.models.user import UserV2
from app.services import rbac_assignment_service
from app.services.airtable_sync.items import ItemBook
from app.services.airtable_sync.lock_sweep import ACCESS_REMOVED
from app.services.airtable_sync.scopes_step import PROGRAM_ALIASES, VIRTUAL_SCOPES
from app.services.airtable_sync.settings_service import normalize_designation
from app.services.airtable_sync.users_step import DELETED_PEOPLE, HELD_PEOPLE, SYNC_SOURCE, VIRTUAL_USERS
from app.services.rbac_graph_service import RbacGraphError

if TYPE_CHECKING:
    from app.services.airtable_sync.airtable_reader import SnapshotRecord
    from app.services.airtable_sync.runner import RunContext

ENTITY = "program_role"
# Pair-level kinds (v2 §3.5): each holds only its pair.
PAIR_KINDS = (
    "assignment_conflict",
    "assignment_removed",
    "unmapped_designation",
    "duplicate_program_role",
    "missing_program",
)

LogFn = Callable[..., Any]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def pair_key(employee_record_id: str, program_record_id: str) -> str:
    return f"{employee_record_id}|{program_record_id}"


def split_key(key: str) -> tuple[str, str]:
    employee, _, program = key.partition("|")
    return employee, program


def held_people(db: Session) -> set[str]:
    """Users Record IDs with an open person-level item: their pairs wait.
    What the Users step leaves in `ctx.state[HELD_PEOPLE]`, read from the DB
    for a run (or a resolution) without that step."""
    return {
        rid
        for (rid,) in db.query(AirtableSyncItemV2.airtable_record_id).filter(
            AirtableSyncItemV2.entity == "user", AirtableSyncItemV2.status == "open"
        )
    }


def retire_person_pairs(db: Session, employee_record_id: str) -> None:
    """The person was unlinked (`user_deleted` Remove / Keep): their pairs
    stop being agreed, so a return links them afresh (A4: adopt or insert)
    instead of reading an admin removal into the old base."""
    prefix = pair_key(employee_record_id, "")
    for record in db.query(AirtableSyncRecordV2).filter(
        AirtableSyncRecordV2.entity == ENTITY, AirtableSyncRecordV2.airtable_record_id.startswith(prefix, autoescape=True)
    ):
        record.hub_id = None
        record.base = None
        record.managed_assignment_id = None


# ---------------------------------------------------------
# The pair's two sides
# ---------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """The Airtable side: the mapped role on the program's scope. In a
    preview the scope (or the person) may be one that doesn't exist yet
    (`scope_id` / user id None)."""

    role_id: int
    scope_id: int | None
    designation: str
    scope_name: str | None = None

    @property
    def pair(self) -> tuple[int, int | None]:
        return self.role_id, self.scope_id

    def as_base(self) -> dict:
        return {"role_id": self.role_id, "scope_id": self.scope_id, "designation": self.designation}

    @classmethod
    def from_dict(cls, raw: dict | None) -> Target | None:
        if not raw or raw.get("role_id") is None or raw.get("scope_id") is None:
            return None
        return cls(role_id=raw["role_id"], scope_id=raw["scope_id"], designation=raw.get("designation") or "")


@dataclass(frozen=True)
class Person:
    # ok / deleted / held / missing
    status: str
    user_id: int | None = None
    name: str | None = None


class PairWriter:
    """The writes a pair makes — insert, adopt, replace, remove, keep, the
    sync record — for the step (`ctx.log`) and for resolutions (a log with
    `resolution_item_id`). In a preview only log rows are written."""

    _KEEP = object()

    def __init__(self, db: Session, log: LogFn, *, dry_run: bool, run_id: int | None) -> None:
        self.db = db
        self._log = log
        self.dry_run = dry_run
        self.run_id = run_id
        self.counts: Counter[str] = Counter()
        self.roles = {i: n for i, n in db.query(RoleV2.id, RoleV2.name)}
        self.scope_names = {i: n for i, n in db.query(ScopeV2.id, ScopeV2.name)}
        self.load_records()

    def load_records(self) -> None:
        self.records: dict[str, AirtableSyncRecordV2] = {
            r.airtable_record_id: r for r in self.db.query(AirtableSyncRecordV2).filter(AirtableSyncRecordV2.entity == ENTITY)
        }

    def log(self, action: str, **kw: Any) -> None:
        self._log(ENTITY, action, **kw)

    def describe(self, role_id: int | None, scope_id: int | None, scope_name: str | None = None) -> dict:
        return {
            "role_id": role_id,
            "role": self.roles.get(role_id),
            "scope_id": scope_id,
            "scope": scope_name or self.scope_names.get(scope_id),
        }

    def describe_row(self, row: RoleAssignmentV2 | None) -> dict | None:
        if row is None:
            return None
        return {"assignment_id": row.id, **self.describe(row.role_id, row.scope_id), "source": row.source}

    def describe_target(self, t: Target) -> dict:
        return {**self.describe(t.role_id, t.scope_id, t.scope_name), "designation": t.designation}

    # ── sync records ────────────────────────────────────────────────────

    def base_of(self, key: str, user_id: int | None) -> Target | None:
        """The agreed pair, or None on a first link (no record, a retired
        one, or one that belonged to another Hub user)."""
        record = self.records.get(key)
        if record is None or user_id is None or record.hub_id != user_id:
            return None
        return Target.from_dict(record.base)

    def managed_row(self, key: str, user_id: int | None) -> RoleAssignmentV2 | None:
        """The sync's row for this pair, if it still exists and is still the
        person's. None = "removed" on the Hub side."""
        record = self.records.get(key)
        if record is None or user_id is None or record.hub_id != user_id or record.managed_assignment_id is None:
            return None
        row = self.db.get(RoleAssignmentV2, record.managed_assignment_id)
        return row if row is not None and row.user_id == user_id else None

    def save_record(self, key: str, latest: dict, *, hub_id: int | None, base: Any = _KEEP, managed: Any = _KEEP) -> None:
        if self.dry_run:
            return
        record = self.records.get(key)
        if record is None:
            record = AirtableSyncRecordV2(entity=ENTITY, airtable_record_id=key)
            self.db.add(record)
            self.records[key] = record
        record.hub_id = hub_id
        record.latest_airtable = dict(latest)
        if self.run_id is not None:
            record.last_seen_run_id = self.run_id
        if base is not self._KEEP:
            record.base = base
        if managed is not self._KEEP:
            record.managed_assignment_id = managed

    def retire(self, key: str, **log: Any) -> None:
        """The pair is no longer agreed (removed, kept as manual, or its row
        already gone): a return is a first link again."""
        record = self.records.get(key)
        if record is None:
            return
        self.counts["retired"] += 1
        self.log("pair_retired", airtable_record_id=key, old_value=record.base, **log)
        if not self.dry_run:
            record.base = None
            record.managed_assignment_id = None

    # ── assignments ─────────────────────────────────────────────────────

    def _held(self, user_id: int | None, t: Target) -> RoleAssignmentV2 | None:
        if user_id is None or t.scope_id is None:
            return None
        return (
            self.db.query(RoleAssignmentV2)
            .filter_by(user_id=user_id, role_id=t.role_id, scope_id=t.scope_id)
            .one_or_none()
        )

    def ensure(self, key: str, user_id: int | None, t: Target, *, log: dict, replaced: RoleAssignmentV2 | None = None) -> int | None:
        """Give the person the target pair: adopt their identical row of any
        source (A4), else insert one. Then remove `replaced` (the old sync
        row), through the service so the last-admin guard applies. Returns
        the managed row's id (None in a preview)."""
        held = self._held(user_id, t)
        if held is not None and held is replaced:  # the old row already is the target
            replaced = None
        new = self.describe_target(t)
        if held is not None:
            old_source = held.source
            if not self.dry_run and held.source != SYNC_SOURCE:
                held.source = SYNC_SOURCE
            row_id = held.id
            if replaced is None:
                self.counts["adopted"] += 1
                self.log("assignment_adopted", **log, old_value=old_source, new_value={"assignment_id": held.id, **new})
        else:
            row_id = None
            if not self.dry_run:
                row = RoleAssignmentV2(user_id=user_id, role_id=t.role_id, scope_id=t.scope_id, source=SYNC_SOURCE, created_at=_utc_now())
                self.db.add(row)
                self.db.flush()
                row_id = row.id
            if replaced is None:
                self.counts["created"] += 1
                self.log("assignment_created", **log, new_value={"assignment_id": row_id, **new})
        if replaced is not None:
            old = self.describe_row(replaced)
            if replaced.source == SYNC_SOURCE and not self.dry_run:
                rbac_assignment_service.delete_assignment(self.db, replaced.id)  # last-admin guard
            self.counts["replaced"] += 1
            self.log(
                "assignment_replaced",
                **log,
                old_value=old,
                new_value={"assignment_id": row_id, **new, "adopted": held is not None},
            )
        return row_id

    def remove(self, key: str, row: RoleAssignmentV2, *, log: dict) -> None:
        old = self.describe_row(row)
        if not self.dry_run:
            rbac_assignment_service.delete_assignment(self.db, row.id)  # last-admin guard
        self.counts["removed"] += 1
        self.log("assignment_removed", **log, old_value=old)
        self.retire(key, **{k: v for k, v in log.items() if k != "airtable_record_id"})

    def keep_as_manual(self, key: str, row: RoleAssignmentV2, *, log: dict) -> None:
        if not self.dry_run:
            row.source = "manual"
        self.counts["kept_manual"] += 1
        self.log("assignment_kept_manual", **log, old_value=self.describe_row(row))
        self.retire(key, **{k: v for k, v in log.items() if k != "airtable_record_id"})

    def apply_target(self, key: str, user_id: int | None, t: Target, latest: dict, *, log: dict, replaced: RoleAssignmentV2 | None) -> None:
        """Give `t` (replacing the old sync row if any) and agree on it."""
        row_id = self.ensure(key, user_id, t, log=log, replaced=replaced)
        self.save_record(key, latest, hub_id=user_id, base=t.as_base(), managed=row_id)


# ---------------------------------------------------------
# Lookups: people, scopes, the mapping
# ---------------------------------------------------------


class People:
    """Users Record ID → the Hub user the pair belongs to, as this run sees
    it: the Users step's held / M7 sets and (in a preview) its would-be
    users, else the DB (open person-level items hold; `users.airtable_record_id`)."""

    def __init__(self, ctx: RunContext) -> None:
        db, state = ctx.db, ctx.state
        self.in_users: dict[str, str | None] = {}
        for rec in ctx.snapshot.users:
            if rec.record_id and rec.record_id not in self.in_users:
                self.in_users[rec.record_id] = rec.fields.get("name")
        if HELD_PEOPLE in state:
            self.held = set(state[HELD_PEOPLE])
            self.deleted = set(state.get(DELETED_PEOPLE, ()))
        else:
            self.held = held_people(db)
            self.deleted = set()
        self.virtual = dict(state.get(VIRTUAL_USERS, {})) if ctx.dry_run else {}
        self.linked: dict[str, tuple[int, str, bool]] = {
            rid: (uid, name, bool(deleted))
            for uid, name, rid, deleted in db.query(UserV2.id, UserV2.name, UserV2.airtable_record_id, UserV2.is_deleted).filter(
                UserV2.airtable_record_id.isnot(None)
            )
        }

    def name(self, rid: str) -> str | None:
        return self.in_users.get(rid)

    def get(self, rid: str) -> Person:
        row = self.linked.get(rid)
        if rid in self.deleted or (row is not None and row[2]):
            return Person("deleted", row[0] if row else None)
        if rid in self.held:
            return Person("held")
        hu = self.virtual.get(rid)
        if hu is not None:
            return Person("ok", hu.id, hu.name)
        if row is not None:
            return Person("ok", row[0], row[1])
        return Person("missing")


class Scopes:
    """Program Record ID → its scope: the C2 alias's cluster scope, or the
    scope carrying the Record ID (plus a preview's would-be scopes)."""

    def __init__(self, ctx: RunContext) -> None:
        db = ctx.db
        self.programs: dict[str, str | None] = {}
        for rec in ctx.snapshot.programs:
            if rec.record_id and rec.record_id not in self.programs:
                self.programs[rec.record_id] = rec.fields.get("name")
        self.by_record_id: dict[str, tuple[int | None, str]] = {
            rid: (sid, name)
            for sid, name, rid in db.query(ScopeV2.id, ScopeV2.name, ScopeV2.airtable_record_id).filter(
                ScopeV2.airtable_record_id.isnot(None)
            )
        }
        if ctx.dry_run:
            for hs in ctx.state.get(VIRTUAL_SCOPES, ()):
                if hs.record_id:
                    self.by_record_id[hs.record_id] = (hs.id, hs.name)
        if PROGRAM_ALIASES in ctx.state:  # the Programs step ran this run
            self.aliases = dict(ctx.state[PROGRAM_ALIASES])
        else:
            self.aliases = {
                rid: (base or {}).get("cluster_record_id")
                for rid, base in db.query(AirtableSyncRecordV2.airtable_record_id, AirtableSyncRecordV2.base).filter(
                    AirtableSyncRecordV2.entity == "program"
                )
                if (base or {}).get("is_cluster")
            }

    def resolve(self, program_rid: str) -> tuple[str, tuple[int | None, str] | None]:
        """('missing', None): not in the Public snapshot (A-1); ('waiting',
        None): known, its scope not synced yet; ('ok', (scope id, name))."""
        if program_rid not in self.programs:
            return "missing", None
        found = self.by_record_id.get(self.aliases.get(program_rid) or program_rid)
        return ("ok", found) if found is not None else ("waiting", None)


def load_mapping(db: Session) -> dict[str, tuple[str, int | None]]:
    return {
        normalize_designation(m.designation): (m.action, m.role_id)
        for m in db.query(AirtableDesignationMappingV2)
    }


# ---------------------------------------------------------
# The step
# ---------------------------------------------------------


class _ProgramRolesStep:
    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.db = ctx.db
        self.book = ItemBook(ctx, ENTITY)
        self.writer = PairWriter(ctx.db, ctx.log, dry_run=ctx.dry_run, run_id=ctx.run_id)
        self.counts = self.writer.counts
        self.people = People(ctx)
        self.scopes = Scopes(ctx)
        self.mapping = load_mapping(ctx.db)

    def commit(self) -> None:
        self.ctx.commit()
        self.book.committed()

    def refused(self, exc: Exception, key: str, subject: str, user_id: int | None) -> None:
        """A write the DB or a service refused (the last-admin guard, a
        concurrent edit): undo this pair only, log, carry on (R3)."""
        self.db.rollback()
        self.book.rolled_back()
        self.writer.load_records()
        self.counts["errors"] += 1
        code = getattr(exc, "code", type(exc).__name__)
        message = getattr(exc, "message", None) or str(getattr(exc, "orig", exc))
        self.writer.log("assignment_error", airtable_record_id=key, hub_id=user_id, subject=subject, old_value=code, new_value=message[:500])
        self.commit()

    def subject(self, employee: str, program: str) -> str:
        person = self.people.name(employee) or employee
        program_name = self.scopes.programs.get(program) or program
        return f"{person} · {program_name}"

    def _raise(self, key: str, kind: str, *, inputs: Any, payload: dict, subject: str, user_id: int | None) -> tuple[str, Any] | None:
        """Raise one pair item. Returns the remembered answer to apply, or
        None when the item holds the pair (opened, refreshed, superseded, or
        a remembered Skip)."""
        outcome = self.book.raise_item(key, kind, inputs=inputs, payload=payload, subject=subject, hub_id=user_id)
        answer = self.book.answer(key, kind) if outcome == "remembered" else None
        return None if answer is None or answer[0] == "skip" else answer

    # ── one pair ────────────────────────────────────────────────────────

    def sync_pair(self, key: str, rows: list[SnapshotRecord]) -> None:
        book, writer = self.book, self.writer
        employee, program = split_key(key)
        subject = self.subject(employee, program)
        if employee not in self.people.in_users:
            self.counts["employee_not_in_users"] += len(rows)
            writer.log("employee_not_in_users", airtable_record_id=key, subject=subject, new_value=len(rows) if len(rows) > 1 else None)
            book.evaluate(key, PAIR_KINDS)  # nothing left to ask about
            self.commit()
            return
        person = self.people.get(employee)
        uid = person.user_id
        base_log = {"airtable_record_id": key, "hub_id": uid, "subject": subject}
        if person.status == "deleted":  # M7
            self.counts["skipped_deleted_in_hub"] += 1
            writer.log("skipped_deleted_in_hub", **base_log)
            book.evaluate(key, PAIR_KINDS)
            self.commit()
            return
        if person.status != "ok":  # held, or not a Hub user (yet): waits
            self.counts["user_not_synced"] += 1
            writer.log("user_not_synced", **base_log, field=person.status)
            self.commit()
            return

        designations = [r.fields.get("designation") or "" for r in rows]
        designation = designations[0].strip()
        norm = normalize_designation(designation)
        latest = {"employee_record_id": employee, "program_record_id": program, "designation": designation, "target": None}
        book.evaluate(key, ("duplicate_program_role", "assignment_removed"))
        if len(rows) > 1:
            self.counts["held"] += 1
            self._raise(
                key,
                "duplicate_program_role",
                inputs={"designations": sorted(normalize_designation(d) for d in designations)},
                payload={
                    "employee_record_id": employee,
                    "program_record_id": program,
                    "program_name": self.scopes.programs.get(program),
                    "user_id": uid,
                    "designations": designations,
                },
                subject=subject,
                user_id=uid,
            )
            writer.log("held", **base_log, new_value=["duplicate_program_role"])
            writer.save_record(key, {**latest, "designation": None}, hub_id=uid)
            self.commit()
            return

        action, role_id = self.mapping.get(norm, (None, None))
        if action == "dont_sync":
            book.evaluate(key, PAIR_KINDS)
            self.counts["skipped_dont_sync"] += 1
            writer.log("skipped_dont_sync", **base_log, field="designation", new_value=designation)
            row = writer.managed_row(key, uid)
            try:
                if row is not None and row.source == SYNC_SOURCE:
                    self._removal(key, row, uid, subject, reason="dont_sync", inputs={"reason": "dont_sync", "designation": norm}, designation=designation)
                elif writer.base_of(key, uid) is not None:
                    writer.retire(key, hub_id=uid, subject=subject)
            except (IntegrityError, RbacGraphError) as exc:
                self.refused(exc, key, subject, uid)
                return
            writer.save_record(key, latest, hub_id=uid)
            self.commit()
            return

        book.evaluate(key, ("missing_program", "unmapped_designation"))
        hold: list[str] = []
        scope_status, scope = self.scopes.resolve(program)
        if scope_status == "missing":
            hold.append("missing_program")
            self._raise(
                key,
                "missing_program",
                inputs={"program_record_id": program},
                payload={"employee_record_id": employee, "program_record_id": program, "designation": designation, "user_id": uid},
                subject=subject,
                user_id=uid,
            )
        elif scope_status == "waiting":
            self.counts["scope_not_synced"] += 1
            writer.log("scope_not_synced", **base_log, field="program", new_value=program)
        if action != "role" or role_id is None:
            if self._raise(
                key,
                "unmapped_designation",
                inputs={"designation": norm},
                payload={
                    "employee_record_id": employee,
                    "program_record_id": program,
                    "program_name": self.scopes.programs.get(program),
                    "designation": designation,
                    "reason": "role_deleted" if action == "role" else "unmapped",
                    "user_id": uid,
                    "choices": ["skip"],
                },
                subject=subject,
                user_id=uid,
            ) is None:
                hold.append("unmapped_designation")
        if hold:
            self.counts["held"] += 1
            writer.log("held", **base_log, new_value=sorted(hold))
        if hold or scope is None:
            # Waiting or held: the record keeps what Airtable says (C4);
            # its conflict items are carried.
            writer.save_record(key, latest, hub_id=uid)
            self.commit()
            return

        target = Target(role_id=role_id, scope_id=scope[0], designation=designation, scope_name=scope[1])
        latest["target"] = target.as_base()
        book.evaluate(key, ("assignment_conflict",))
        try:
            self._three_way(key, uid, target, latest, subject)
        except (IntegrityError, RbacGraphError) as exc:
            self.refused(exc, key, subject, uid)
            return
        self.commit()

    def _three_way(self, key: str, uid: int | None, target: Target, latest: dict, subject: str) -> None:
        writer = self.writer
        log = {"airtable_record_id": key, "hub_id": uid, "subject": subject}
        base = writer.base_of(key, uid)
        row = writer.managed_row(key, uid)
        if base is None:  # first link (A4: adopt or insert)
            writer.apply_target(key, uid, target, latest, log=log, replaced=None)
            return
        hub = (row.role_id, row.scope_id) if row is not None else None
        a_changed, h_changed = target.pair != base.pair, hub != base.pair
        if not a_changed and not h_changed:
            self.counts["unchanged"] += 1
            writer.save_record(key, latest, hub_id=uid)
            return
        if a_changed and not h_changed:
            writer.apply_target(key, uid, target, latest, log=log, replaced=row)
            return
        if not a_changed:  # the Hub changed: keep it (M1)
            self.counts["hub_differs"] += 1
            writer.log("hub_differs", **log, field="assignment", old_value=writer.describe_target(base), new_value=writer.describe_row(row))
            writer.save_record(key, latest, hub_id=uid)
            return
        if hub == target.pair:  # both changed, to the same pair
            self.counts["unchanged"] += 1
            writer.save_record(key, latest, hub_id=uid, base=target.as_base())
            return
        answer = self._raise(
            key,
            "assignment_conflict",
            inputs={"designation": normalize_designation(target.designation), "role_id": target.role_id, "scope_id": target.scope_id},
            payload={
                "employee_record_id": latest["employee_record_id"],
                "program_record_id": latest["program_record_id"],
                "user_id": uid,
                "base": writer.describe_target(base),
                "airtable": writer.describe_target(target),
                "hub": writer.describe_row(row),
                "choices": ["accept_airtable", "keep_current"],
            },
            subject=subject,
            user_id=uid,
        )
        if answer is None:
            self.counts["held"] += 1
            writer.log("held", **log, new_value=["assignment_conflict"])
            writer.save_record(key, latest, hub_id=uid)
        elif answer[0] == "accept_airtable":
            writer.apply_target(key, uid, target, latest, log=log, replaced=row)
        else:  # keep_current: the base moves forward (M3)
            self.counts["hub_differs"] += 1
            writer.log("hub_differs", **log, field="assignment", old_value=writer.describe_target(target), new_value=writer.describe_row(row))
            writer.save_record(key, latest, hub_id=uid, base=target.as_base())

    # ── removals ────────────────────────────────────────────────────────

    def _removal(self, key: str, row: RoleAssignmentV2, uid: int, subject: str, *, reason: str, inputs: dict, designation: str | None = None) -> None:
        """`assignment_removed` for a sync row the sync no longer wants; a
        remembered Remove / Keep is applied."""
        writer = self.writer
        log = {"airtable_record_id": key, "hub_id": uid, "subject": subject}
        employee, program = split_key(key)
        answer = self._raise(
            key,
            "assignment_removed",
            inputs=inputs,
            payload={
                "reason": reason,  # gone / dont_sync
                "employee_record_id": employee,
                "program_record_id": program,
                "designation": designation,
                "user_id": uid,
                "assignment": writer.describe_row(row),
                "choices": ["remove", "keep"],
            },
            subject=subject,
            user_id=uid,
        )
        if answer is None:
            self.counts["removals"] += 1
            self.counts["held"] += 1
        elif answer[0] == "remove":
            writer.remove(key, row, log=log)
        else:
            writer.keep_as_manual(key, row, log=log)

    def detect_removed(self, seen: set[str]) -> None:
        """Recorded pairs (and pairs with open items) gone from Program
        Roles. People out of Users (`user_deleted`), soft-deleted or
        unlinked ones are never asked about; held people are carried."""
        writer, book = self.writer, self.book
        for key in sorted((set(writer.records) | book.open_record_ids()) - seen):
            employee, program = split_key(key)
            person = self.people.get(employee) if employee in self.people.in_users else Person("not_in_users")
            if person.status == "held":
                continue
            book.evaluate(key, PAIR_KINDS)
            record = writer.records.get(key)
            if person.status != "ok" or record is None or record.hub_id is None or record.hub_id != person.user_id:
                continue
            if record.base is None and record.managed_assignment_id is None:
                continue  # retired
            uid, subject = person.user_id, self.subject(employee, program)
            row = writer.managed_row(key, uid)
            try:
                if row is None or row.source != SYNC_SOURCE:
                    # An admin removed it already: agreed.
                    writer.retire(key, hub_id=uid, subject=subject)
                else:
                    self._removal(key, row, uid, subject, reason="gone", inputs={"reason": "gone", "last_seen_run_id": record.last_seen_run_id}, designation=(record.base or {}).get("designation"))
            except (IntegrityError, RbacGraphError) as exc:
                self.refused(exc, key, subject, uid)
                continue
            self.commit()
        self.commit()

    def run(self) -> dict:
        pairs: dict[str, list[SnapshotRecord]] = {}
        for rec in self.ctx.snapshot.program_roles:
            self.counts["records"] += 1
            employee = (rec.fields.get("employee_record_id") or "").strip()
            program = (rec.fields.get("program_record_id") or "").strip()
            if not employee or not program:
                self.counts["skipped"] += 1
                self.writer.log("skipped_no_record_id", airtable_record_id=pair_key(employee, program) if employee or program else None, new_value=rec.api_id)
                continue
            pairs.setdefault(pair_key(employee, program), []).append(rec)
        self.commit()
        for key, rows in pairs.items():
            self.sync_pair(key, rows)
        self.detect_removed(set(pairs))
        if not self.ctx.dry_run and (self.counts["replaced"] or self.counts["removed"]):
            self.ctx.state[ACCESS_REMOVED] = True  # the runner sweeps the locks once
        out = dict(self.counts)
        out.update(self.book.finish())
        self.commit()
        return dict(sorted(out.items()))


def program_roles_step(ctx: RunContext) -> dict:
    return _ProgramRolesStep(ctx).run()
