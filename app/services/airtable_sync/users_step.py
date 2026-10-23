"""The Users step, without HR fields (plan v2 §3.1 K2/M7, §3.4 A5, §3.5,
§8 step 5; v1 plan §5.3.1, §5.3.2, §5.3.4, §5.3.8). HR fields, Work Email
changes (E1) and M2's first-run field conflicts are phase 6.

Per Airtable Users record, one transaction:

  M7       the Record ID, or either email, hits a SOFT-DELETED `users` row
           → skipped quietly (`skipped_deleted_in_hub`): no item, no write.
           The sync reads `users.is_deleted` and never writes it.
  validate `no_work_email`; `duplicate_work_email` (every record sharing the
           address is held). "—" items, no buttons.
  match    K2 among non-deleted rows: Record ID → Work Email → Alias Email
           (each checked against both email columns) → a unique exact name
           (trimmed, case-insensitive) among rows with no Record ID.
             several rows by email → `ambiguous_match` (Link to X / Skip:
               Create new would collide with their emails; owner 2026-10-22)
             several rows by name  → `ambiguous_match` (Link to X / Create
               new / Skip; owner 2026-10-22)
             the email's row is linked to ANOTHER Record ID → `email_taken`
               (owner 2026-10-22)
             one name match, no email on the row → link; with another email
               → `possible_match` (Link / Create as new / Skip)
  A5       zero usable Program Roles rows (from the snapshot, R2: "Don't
           sync" Designations excluded; rows on programs missing from the
           Public table count) and no sync-given default pair →
           `no_program_roles` (Assign default (User, Hub) / Skip).
  hold     any of these items holds the whole person (v2 §3.5): no create,
           no link, no default. A remembered answer is APPLIED instead
           (Link, Create new, Assign default), except Skip, which holds.
  apply    link (store the Record ID) or create the `users` row (name,
           emails, Record ID, has_signed_in false; `add_default_assignment`
           is NOT called); the sync-given default pair when answered.

Afterwards, every non-deleted row with a Record ID that is not in this run
→ `user_deleted` (Remove synced access / Keep / Delete user).

The sync record (`entity = 'user'`) keeps `latest_airtable` (resolutions
apply from it, C4), `hub_id`, and as `base` only the identity fields
(name, work_email, alias_email) on which Airtable and the Hub AGREE. A field
that differs is left out, so phase 6's three-way treats it as a first link
(M2: a conflict); a base value, once set, is never moved here.
`managed_assignment_id` is set only for a sync-given default pair (A5).

Preview computes everything and writes only log rows; would-be users live
in the step's in-memory index and are handed to the Program Roles step in
`ctx.state[VIRTUAL_USERS]`, with `ctx.state[HELD_PEOPLE]`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db_v2.models.airtable_sync import AirtableDesignationMappingV2, AirtableSyncRecordV2
from app.db_v2.models.role import RoleV2
from app.db_v2.models.role_assignment import RoleAssignmentV2
from app.db_v2.models.scope import ScopeV2
from app.db_v2.models.user import UserV2
from app.services.airtable_sync.items import ItemBook
from app.services.airtable_sync.settings_service import normalize_designation
from app.services.rbac_assignment_service import public_pair_ids
from app.services.rbac_graph_service import RbacGraphError

if TYPE_CHECKING:
    from app.services.airtable_sync.airtable_reader import SnapshotRecord
    from app.services.airtable_sync.runner import RunContext

ENTITY = "user"
SYNC_SOURCE = "airtable_sync"

# Person-level kinds this phase raises (v2 §3.5). Phase 6 adds
# `work_email_change_signed_in` and `field_conflict`.
PERSON_KINDS = (
    "no_work_email",
    "duplicate_work_email",
    "possible_match",
    "ambiguous_match",
    "email_taken",
    "no_program_roles",
)
USER_DELETED = "user_deleted"
IDENTITY_FIELDS = ("name", "work_email", "alias_email")

# ctx.state keys, for the Program Roles step (phase 5).
HELD_PEOPLE = "users_held"  # Users Record IDs held (or skipped, M7) this run
VIRTUAL_USERS = "virtual_users"  # preview: Record ID → the would-be HubUser

LogFn = Callable[..., Any]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def name_key(name: str | None) -> str:
    return (name or "").strip().casefold()


def _email(value: str | None) -> str | None:
    return (value or "").strip().lower() or None


def airtable_identity(fields: dict) -> dict:
    return {f: fields.get(f) for f in IDENTITY_FIELDS}


# ---------------------------------------------------------
# The Hub's users, as the step sees them
# ---------------------------------------------------------


@dataclass
class HubUser:
    id: int | None  # None: a user a preview would create
    name: str
    work_email: str | None
    alias_email: str | None
    record_id: str | None
    is_deleted: bool = False
    virtual: bool = False

    @property
    def emails(self) -> list[str]:
        return [e for e in (self.work_email, self.alias_email) if e]

    def identity(self) -> dict:
        return {"name": (self.name or "").strip() or None, "work_email": self.work_email, "alias_email": self.alias_email}

    def describe(self) -> dict:
        return {
            "user_id": self.id,
            "name": self.name,
            "work_email": self.work_email,
            "alias_email": self.alias_email,
            "airtable_record_id": self.record_id,
        }


class UserIndex:
    """Every `users` row (soft-deleted ones too: M7 needs to see them),
    indexed by Record ID, email (both columns) and name. Kept current as the
    step links and creates."""

    def __init__(self, users: Iterable[HubUser]) -> None:
        self.users: list[HubUser] = []
        self.by_id: dict[int, HubUser] = {}
        self.by_record_id: dict[str, HubUser] = {}
        self._by_email: dict[str, list[HubUser]] = {}
        self._by_name: dict[str, list[HubUser]] = {}
        for hu in users:
            self.add(hu)

    @classmethod
    def load(cls, db: Session) -> UserIndex:
        rows = db.query(
            UserV2.id, UserV2.name, UserV2.work_email, UserV2.alias_email, UserV2.airtable_record_id, UserV2.is_deleted
        ).all()
        return cls(
            HubUser(id=r[0], name=r[1] or "", work_email=_email(r[2]), alias_email=_email(r[3]), record_id=r[4], is_deleted=bool(r[5]))
            for r in rows
        )

    def add(self, hu: HubUser) -> None:
        self.users.append(hu)
        if hu.id is not None:
            self.by_id[hu.id] = hu
        if hu.record_id:
            self.by_record_id[hu.record_id] = hu
        for e in hu.emails:
            self._by_email.setdefault(e, []).append(hu)
        self._by_name.setdefault(name_key(hu.name), []).append(hu)

    def link(self, hu: HubUser, rid: str, *, virtual: bool = False) -> None:
        hu.record_id = rid
        self.by_record_id[rid] = hu
        hu.virtual = hu.virtual or virtual

    def email_hits(self, work: str | None, alias: str | None) -> list[tuple[HubUser, str]]:
        """Distinct rows holding either Airtable email in either column,
        with the Airtable field that hit, Work Email first (K2 steps 2–3)."""
        out: list[tuple[HubUser, str]] = []
        for via, e in (("work_email", work), ("alias_email", alias)):
            for hu in self._by_email.get(e, ()) if e else ():
                if all(hu is not seen for seen, _ in out):
                    out.append((hu, via))
        return out

    def unlinked_named(self, name: str) -> list[HubUser]:
        """K2 step 4's candidates: same name, no Record ID, not deleted."""
        return [hu for hu in self._by_name.get(name_key(name), ()) if hu.record_id is None and not hu.is_deleted]


# ---------------------------------------------------------
# K2 matching (pure)
# ---------------------------------------------------------


@dataclass
class Match:
    # linked   the row already holds this Record ID
    # match    a row to link (email / name)
    # create   no row: a new one
    # deleted  M7: skip quietly
    # item     a person-level item stands in the way
    outcome: str
    user: HubUser | None = None
    via: str | None = None
    kind: str | None = None
    payload: dict = field(default_factory=dict)


def match_person(index: UserIndex, rid: str, fields: dict) -> Match:
    name, work, alias = fields.get("name"), fields.get("work_email"), fields.get("alias_email")
    airtable = airtable_identity(fields)
    hu = index.by_record_id.get(rid)
    if hu is not None:
        return Match("deleted" if hu.is_deleted else "linked", hu, "record_id")

    hits = index.email_hits(work, alias)
    if hits:
        for hu, via in hits:
            if hu.is_deleted:
                return Match("deleted", hu, via)
        if len(hits) > 1:
            return Match(
                "item",
                kind="ambiguous_match",
                payload={
                    "reason": "email",
                    "airtable": airtable,
                    "candidates": [{**hu.describe(), "matched_by": via} for hu, via in hits],
                    # Create new would collide with the candidates' emails.
                    "choices": ["link", "skip"],
                },
            )
        hu, via = hits[0]
        if hu.record_id:  # linked to ANOTHER Airtable record (ours missed above)
            return Match(
                "item",
                user=hu,
                kind="email_taken",
                payload={"airtable": airtable, "email": work if via == "work_email" else alias, "matched_by": via, "taken_by": hu.describe()},
            )
        return Match("match", hu, via)

    candidates = index.unlinked_named(name)
    if len(candidates) > 1:
        return Match(
            "item",
            kind="ambiguous_match",
            payload={
                "reason": "name",
                "airtable": airtable,
                "candidates": [{**c.describe(), "matched_by": "name"} for c in candidates],
                "choices": ["link", "create_new", "skip"],
            },
        )
    if candidates:
        c = candidates[0]
        if not c.emails:
            return Match("match", c, "name")
        return Match(
            "item",
            user=c,
            kind="possible_match",
            payload={"airtable": airtable, "candidate": c.describe(), "choices": ["link", "create_new", "skip"]},
        )
    return Match("create")


def answered_match(index: UserIndex, rid: str, kind: str, resolution: str, data: Any, payload: dict) -> Match | None:
    """What a Link / Create new answer to a match item means now; None when
    it no longer can be applied (the row went away, was deleted or linked to
    someone else) — then the person waits."""
    if resolution == "create_new":
        return Match("create")
    if resolution != "link":
        return None
    target_id = (data or {}).get("user_id")
    if target_id is None and kind == "possible_match":
        target_id = (payload.get("candidate") or {}).get("user_id")
    hu = index.by_id.get(target_id) if target_id is not None else None
    if hu is None or hu.is_deleted or (hu.record_id not in (None, rid)):
        return None
    return Match("linked" if hu.record_id == rid else "match", hu, "resolution")


# ---------------------------------------------------------
# Writes, shared by the step and by resolutions
# ---------------------------------------------------------


class UserWriter:
    """The writes a person's pipeline makes — link, create, the default
    pair, the sync record — for the step (`ctx.log`) and for resolutions (a
    log with `resolution_item_id`). In a preview only the index changes and
    log rows are written."""

    _KEEP = object()

    def __init__(self, db: Session, log: LogFn, *, dry_run: bool, run_id: int | None) -> None:
        self.db = db
        self._log = log
        self.dry_run = dry_run
        self.run_id = run_id
        self.index = UserIndex.load(db)
        self.counts: Counter[str] = Counter()
        self.load_records()

    def load_records(self) -> None:
        self.records: dict[str, AirtableSyncRecordV2] = {
            r.airtable_record_id: r for r in self.db.query(AirtableSyncRecordV2).filter(AirtableSyncRecordV2.entity == ENTITY)
        }

    def reload(self) -> None:
        """After a rollback: the index and records are re-read from the DB."""
        self.index = UserIndex.load(self.db)
        self.load_records()

    def log(self, action: str, **kw: Any) -> None:
        self._log(ENTITY, action, **kw)

    # ── sync records ────────────────────────────────────────────────────

    def base_of(self, rid: str, hu: HubUser | None) -> dict:
        record = self.records.get(rid)
        if record is None or hu is None or hu.id is None or record.hub_id != hu.id or not record.base:
            return {}
        return dict(record.base)

    def agreed_base(self, rid: str, hu: HubUser, fields: dict) -> dict:
        """The kept base plus every identity field on which Airtable and the
        Hub agree now. A base value is never moved here (phase 6's job)."""
        base = self.base_of(rid, hu)
        hub = hu.identity()
        for f in IDENTITY_FIELDS:
            if f not in base and fields.get(f) == hub[f]:
                base[f] = fields.get(f)
        return base

    def save_record(self, rid: str, fields: dict, *, hub_id: int | None, base: Any = _KEEP, managed: Any = _KEEP) -> None:
        if self.dry_run:
            return
        record = self.records.get(rid)
        if record is None:
            record = AirtableSyncRecordV2(entity=ENTITY, airtable_record_id=rid)
            self.db.add(record)
            self.records[rid] = record
        record.hub_id = hub_id
        record.latest_airtable = dict(fields)
        if self.run_id is not None:
            record.last_seen_run_id = self.run_id
        if base is not self._KEEP:
            record.base = base
        if managed is not self._KEEP:
            record.managed_assignment_id = managed

    # ── link / create ───────────────────────────────────────────────────

    def link(self, hu: HubUser, rid: str, *, via: str, subject: str | None) -> None:
        if not self.dry_run:
            row = self.db.get(UserV2, hu.id)
            row.airtable_record_id = rid
            row.updated_at = _utc_now()
            self.db.flush()
        self.index.link(hu, rid, virtual=self.dry_run)
        self.counts["linked"] += 1
        self.log("linked", airtable_record_id=rid, hub_id=hu.id, subject=subject, field=via, old_value=hu.name, new_value=rid)

    def create(self, rid: str, fields: dict) -> HubUser:
        name, work, alias = fields.get("name"), fields.get("work_email"), fields.get("alias_email")
        if self.dry_run:
            hu = HubUser(id=None, name=name, work_email=work, alias_email=alias, record_id=rid, virtual=True)
        else:
            now = _utc_now()
            row = UserV2(
                name=name,
                work_email=work,
                alias_email=alias,
                airtable_record_id=rid,
                has_signed_in=False,
                created_at=now,
                updated_at=now,
            )
            self.db.add(row)
            self.db.flush()
            hu = HubUser(id=row.id, name=name, work_email=work, alias_email=alias, record_id=rid)
        self.index.add(hu)
        self.counts["created"] += 1
        self.log("user_created", airtable_record_id=rid, hub_id=hu.id, subject=name, new_value=airtable_identity(fields))
        return hu

    # ── the sync-given default pair (A5, D3) ────────────────────────────

    def sync_default(self, rid: str, hu: HubUser | None) -> RoleAssignmentV2 | None:
        """The person's sync-given (User, Hub) row, if it still exists."""
        record = self.records.get(rid)
        if hu is None or hu.id is None or record is None or record.hub_id != hu.id or record.managed_assignment_id is None:
            return None
        row = self.db.get(RoleAssignmentV2, record.managed_assignment_id)
        return row if row is not None and row.user_id == hu.id else None

    def default_removed_by_hub(self, rid: str, hu: HubUser | None) -> bool:
        """The sync gave the default pair once (`base.default`) and an admin
        has since removed it: the Hub's choice stands (M1), so it is neither
        given again nor asked about."""
        return bool(self.base_of(rid, hu).get("default")) and self.sync_default(rid, hu) is None

    def ensure_default(self, rid: str, hu: HubUser) -> int | None:
        """Give the sync-given default pair (never stacked: nothing is
        written when the person already holds (User, Hub), whatever its
        source). Returns the managed assignment id to record, or None."""
        existing = self.sync_default(rid, hu)
        if existing is not None:
            return existing.id
        if self.default_removed_by_hub(rid, hu):
            return None
        pair = public_pair_ids(self.db)
        if pair is None:
            self.log("default_unavailable", airtable_record_id=rid, hub_id=hu.id, subject=hu.name)
            return None
        role_id, scope_id = pair
        if hu.id is not None:
            held = (
                self.db.query(RoleAssignmentV2.id)
                .filter_by(user_id=hu.id, role_id=role_id, scope_id=scope_id)
                .first()
            )
            if held is not None:
                self.log("default_already_held", airtable_record_id=rid, hub_id=hu.id, subject=hu.name, new_value=held[0])
                return None
        assignment_id = None
        if not self.dry_run:
            row = RoleAssignmentV2(
                user_id=hu.id, role_id=role_id, scope_id=scope_id, source=SYNC_SOURCE, created_at=_utc_now()
            )
            self.db.add(row)
            self.db.flush()
            assignment_id = row.id
        self.counts["defaults_assigned"] += 1
        self.log(
            "assignment_created",
            airtable_record_id=rid,
            hub_id=hu.id,
            subject=hu.name,
            field="default",
            new_value={"assignment_id": assignment_id, "role_id": role_id, "scope_id": scope_id},
        )
        return assignment_id

    # ── applying one person ─────────────────────────────────────────────

    def apply(self, rid: str, fields: dict, m: Match, *, want_default: bool) -> HubUser:
        """Link or create, then the default pair if answered, then the
        record. The caller commits (or rolls back on a refusal)."""
        name = fields.get("name")
        if m.outcome == "create":
            hu = self.create(rid, fields)
        else:
            hu = m.user
            if m.outcome == "match":
                self.link(hu, rid, via=m.via, subject=name)
            else:
                self.counts["unchanged"] += 1
        managed: Any = self._KEEP
        base = self.agreed_base(rid, hu, fields)
        if want_default:
            managed = self.ensure_default(rid, hu)
            if managed is not None:
                base["default"] = True  # the sync gave it (D3: never silently removed)
        self.save_record(rid, fields, hub_id=hu.id, base=base, managed=managed)
        return hu


# ---------------------------------------------------------
# The step
# ---------------------------------------------------------


def synced_assignments(db: Session, user_id: int) -> list[dict]:
    rows = (
        db.query(RoleAssignmentV2.id, RoleV2.name, ScopeV2.name)
        .join(RoleV2, RoleV2.id == RoleAssignmentV2.role_id)
        .join(ScopeV2, ScopeV2.id == RoleAssignmentV2.scope_id)
        .filter(RoleAssignmentV2.user_id == user_id, RoleAssignmentV2.source == SYNC_SOURCE)
        .order_by(RoleAssignmentV2.id)
        .all()
    )
    return [{"assignment_id": i, "role": r, "scope": s} for i, r, s in rows]


class _UsersStep:
    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.db = ctx.db
        self.book = ItemBook(ctx, ENTITY)
        self.writer = UserWriter(ctx.db, ctx.log, dry_run=ctx.dry_run, run_id=ctx.run_id)
        self.counts = self.writer.counts
        self.held: set[str] = set()
        self._program_roles()

    # ── A5 inputs, from the snapshot (R2) ───────────────────────────────

    def _program_roles(self) -> None:
        dont_sync = {
            normalize_designation(d)
            for (d,) in self.db.query(AirtableDesignationMappingV2.designation).filter(
                AirtableDesignationMappingV2.action == "dont_sync"
            )
        }
        self.usable: Counter[str] = Counter()
        self.dont_sync_rows: dict[str, list[list[str | None]]] = {}
        for rec in self.ctx.snapshot.program_roles:
            emp = rec.fields.get("employee_record_id")
            if not emp:
                continue
            designation = rec.fields.get("designation")
            if normalize_designation(designation or "") in dont_sync:
                self.dont_sync_rows.setdefault(emp, []).append([rec.fields.get("program_record_id"), designation])
            else:
                self.usable[emp] += 1

    def commit(self) -> None:
        self.ctx.commit()
        self.book.committed()

    def refused(self, exc: Exception, rid: str, name: str | None) -> None:
        """A write the DB or a service refused (an email taken by a
        concurrent sign-in, …): undo this person only, log, carry on (R3)."""
        self.db.rollback()
        self.book.rolled_back()
        self.writer.reload()
        self.counts["errors"] += 1
        code = getattr(exc, "code", type(exc).__name__)
        message = getattr(exc, "message", None) or str(getattr(exc, "orig", exc))
        self.writer.log("user_error", airtable_record_id=rid, subject=name, old_value=code, new_value=message[:500])
        self.held.add(rid)
        self.commit()

    # ── one person ──────────────────────────────────────────────────────

    def sync_person(self, rec: SnapshotRecord, rid: str, duplicates: dict[str, list[str]]) -> None:
        book, writer, fields = self.book, self.writer, rec.fields
        name, work = fields.get("name"), fields.get("work_email")
        airtable = airtable_identity(fields)
        # Every person-level kind is re-checked below, and a person in
        # Airtable is not deleted from it.
        book.evaluate(rid, PERSON_KINDS + (USER_DELETED,))
        m = match_person(writer.index, rid, fields)

        if m.outcome == "deleted":  # M7: never matched, updated or reported
            self.counts["skipped_deleted_in_hub"] += 1
            self.held.add(rid)
            writer.log("skipped_deleted_in_hub", airtable_record_id=rid, hub_id=m.user.id, subject=name, field=m.via)
            self.commit()
            return

        hu_linked = m.user if m.outcome == "linked" else None
        linked_id = hu_linked.id if hu_linked else None
        # The row the person would be (linked or a clean email/name match),
        # shown on the items that hold them.
        matched = m.user if m.outcome in ("linked", "match") else None
        matched_id = matched.id if matched else None
        hold: list[str] = []
        answers: dict[str, tuple[str, Any]] = {}

        def raise_(kind: str, *, inputs: Any, payload: dict, hub_id: int | None = None) -> None:
            outcome = book.raise_item(rid, kind, inputs=inputs, payload=payload, subject=name, hub_id=hub_id)
            answer = book.answer(rid, kind) if outcome == "remembered" else None
            if answer is None or answer[0] == "skip":
                hold.append(kind)
            else:
                answers[kind] = answer

        if not work:
            raise_("no_work_email", inputs="blank", payload={"airtable": airtable}, hub_id=matched_id)
        elif len(duplicates.get(work, ())) > 1:
            others = sorted(r for r in duplicates[work] if r != rid)
            raise_(
                "duplicate_work_email",
                inputs={"work_email": work, "records": sorted(duplicates[work])},
                payload={"airtable": airtable, "work_email": work, "other_records": others},
                hub_id=matched_id,
            )
        if m.outcome == "item":
            raise_(m.kind, inputs=airtable, payload=m.payload)
        if writer.default_removed_by_hub(rid, hu_linked):
            # The sync gave the default once and an admin removed it: keep
            # the Hub (M1), don't ask again.
            self.counts["hub_differs"] += 1
            writer.log("hub_differs", airtable_record_id=rid, hub_id=linked_id, subject=name, field="default", old_value="default", new_value=None)
        elif not self.usable[rid] and writer.sync_default(rid, hu_linked) is None:
            dont = sorted(self.dont_sync_rows.get(rid, []), key=lambda r: [x or "" for x in r])
            raise_(
                "no_program_roles",
                inputs={"dont_sync_rows": dont},
                payload={
                    "airtable": airtable,
                    "hub_user": matched.describe() if matched else None,
                    "dont_sync_rows": dont,
                    "choices": ["assign_default", "skip"],
                },
                hub_id=matched_id,
            )

        # A remembered Link / Create new decides the match.
        if m.outcome == "item" and m.kind in answers:
            resolution, data = answers[m.kind]
            answered = answered_match(writer.index, rid, m.kind, resolution, data, m.payload)
            if answered is None:
                hold.append(m.kind)
                writer.log("answer_not_applicable", airtable_record_id=rid, subject=name, field=m.kind, old_value=resolution, new_value=data)
            else:
                m = answered

        if hold:
            self.counts["held"] += 1
            self.held.add(rid)
            writer.log("held", airtable_record_id=rid, hub_id=linked_id, subject=name, new_value=sorted(set(hold)))
            writer.save_record(rid, fields, hub_id=linked_id)
            self.commit()
            return

        want_default = answers.get("no_program_roles", ("",))[0] == "assign_default"
        try:
            writer.apply(rid, fields, m, want_default=want_default)
        except (IntegrityError, RbacGraphError) as exc:
            self.refused(exc, rid, name)
            return
        self.commit()

    # ── deleted from Airtable (M6) ──────────────────────────────────────

    def detect_deleted(self, seen: set[str]) -> None:
        raised: set[str] = set()
        for hu in list(self.writer.index.users):
            rid = hu.record_id
            if not rid or rid in seen or hu.is_deleted or hu.id is None:
                continue
            raised.add(rid)
            record = self.writer.records.get(rid)
            self.book.evaluate(rid, PERSON_KINDS + (USER_DELETED,))
            self.book.raise_item(
                rid,
                USER_DELETED,
                # The disappearance itself: a person who comes back and
                # leaves again is asked again (a Keep is not remembered
                # across a return).
                inputs={"last_seen_run_id": record.last_seen_run_id if record else None},
                subject=hu.name,
                hub_id=hu.id,
                payload={
                    "user": hu.describe(),
                    "synced_assignments": synced_assignments(self.db, hu.id),
                    "choices": ["remove_synced_access", "keep", "delete_user"],
                },
            )
            self.counts["deleted"] += 1
        # Records gone from Airtable whose row was unlinked or soft-deleted
        # meanwhile: their items have nothing left to ask.
        for rid in self.book.open_record_ids() - seen - raised:
            self.book.evaluate(rid, PERSON_KINDS + (USER_DELETED,))
        self.commit()

    def run(self) -> dict:
        ctx = self.ctx
        valid: list[tuple[SnapshotRecord, str]] = []
        seen: set[str] = set()
        for rec in ctx.snapshot.users:
            self.counts["records"] += 1
            rid, name = rec.record_id, rec.fields.get("name")
            if not rid:
                self.counts["skipped"] += 1
                self.writer.log("skipped_no_record_id", subject=name, new_value=rec.api_id)
                continue
            if rid in seen:
                self.counts["skipped"] += 1
                self.writer.log("skipped_duplicate_record_id", airtable_record_id=rid, subject=name, new_value=rec.api_id)
                continue
            seen.add(rid)
            if not name:
                self.counts["skipped"] += 1
                self.held.add(rid)
                self.writer.log("skipped_no_name", airtable_record_id=rid, new_value=rec.api_id)
                continue
            valid.append((rec, rid))
        duplicates: dict[str, list[str]] = {}
        for rec, rid in valid:
            work = rec.fields.get("work_email")
            if work:
                duplicates.setdefault(work, []).append(rid)
        self.commit()
        for rec, rid in valid:
            self.sync_person(rec, rid, duplicates)
        self.detect_deleted(seen)

        ctx.state[HELD_PEOPLE] = set(self.held)
        if ctx.dry_run:
            ctx.state[VIRTUAL_USERS] = {hu.record_id: replace(hu) for hu in self.writer.index.users if hu.virtual and hu.record_id}
        out = dict(self.counts)
        out.update(self.book.finish())
        self.commit()
        return dict(sorted(out.items()))


def users_step(ctx: RunContext) -> dict:
    return _UsersStep(ctx).run()
