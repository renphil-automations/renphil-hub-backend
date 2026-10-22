"""The Clusters and Programs steps (plan v2 §3.3 C1–C3, §4.1, §8 steps 3–4;
v1 plan §5.2, K3, S1–S6).

Both steps run the same per-record scope logic (`_ScopeStep.sync_scope`):

  match    K3: by Record ID (`scopes.airtable_record_id`), else by a unique
           name (trimmed, case-insensitive) among scopes with NO Record ID
           that are not the universal, public or a system scope. A linked
           scope is never matched by name, so one is never stolen.
  create   no match → `rbac_service.create_scope` (not universal, no
           parents), then `type` and `airtable_record_id` on the new row in
           the same transaction. `key` = slug of the name, `_2`… on a clash;
           made once and never changed (S2).
  link     first match by name → store the Record ID, set `type`.
  rename   three-way on `name` (S3): Airtable changed, Hub didn't → rename;
           Hub changed only → keep, log `hub_differs`; both → item.
  deleted  a linked scope of this step's type whose Record ID is gone from
           Airtable → `scope_deleted` item with what uses it (S4).

`type` is written only on create and link, and never flipped: a matched
scope of the other type is a `scope_type_mismatch` item (v2 §4.1).

Programs, on top: a row with no Cluster Record Id named like a cluster IS
that cluster (C2: an alias record, no scope); a row WITH one named like a
cluster → `scope_name_taken` (A-2); its parent set is three-way merged
against the base and the Hub's `airtable_sync` edges (C3); the text and the
"Program Cluster" link disagreeing is a log line only (D-6).

Names count as TAKEN case-insensitively (a scope "space" blocks a cluster
"Space"), the same comparison K3 matches with, although the column itself
is unique case-sensitively: two scopes differing only in case would be
indistinguishable in every picker.

One transaction per scope (and per added edge) through `ctx.commit()`. A
data problem the rbac services refuse (`RbacGraphError`) rolls back that
scope only, is logged `scope_error` / `edge_refused`, and the step goes on
(R3); anything else is a code error and fails the step.

Preview computes everything and writes only log rows: would-be scopes live
in an in-memory index, handed from the Clusters step to the Programs step
through `ctx.state[VIRTUAL_SCOPES]`, so a full preview shows the edges a
real run would add.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import func

from app.db_v2.models.airtable_sync import AirtableSyncRecordV2
from app.db_v2.models.resource_grant import ResourceGrantV2
from app.db_v2.models.role import RoleV2
from app.db_v2.models.role_assignment import RoleAssignmentV2
from app.db_v2.models.scope import ScopeEdgeV2, ScopeV2
from app.db_v2.models.user import UserV2
from app.services import rbac_service
from app.services.airtable_sync.items import ItemBook
from app.services.rbac_graph_service import RbacGraphError, create_scope_edge

if TYPE_CHECKING:
    from app.services.airtable_sync.airtable_reader import SnapshotRecord
    from app.services.airtable_sync.runner import RunContext

# ctx.state key: the Clusters step's would-be scopes, for a preview's
# Programs step.
VIRTUAL_SCOPES = "virtual_scopes"

SCOPE_KINDS = (
    "scope_type_mismatch",
    "scope_ambiguous_match",
    "scope_name_taken",
    "scope_name_conflict",
    "scope_deleted",
)
EDGE_KINDS = ("unknown_cluster", "cluster_edge_removed")

SYNC_EDGE = "airtable_sync"
KEY_MAX = 64  # scopes.key
PEOPLE_LISTED = 50  # in a scope_deleted payload


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def name_key(name: str) -> str:
    """K3's comparison: trimmed, case-insensitive."""
    return name.strip().casefold()


def slugify(name: str) -> str:
    """Lowercase ASCII, one `_` for every run of anything else, at most 64
    characters. Accents fold to their letter (é → e); other non-ASCII drops."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "_", folded).strip("_")
    return slug[:KEY_MAX].rstrip("_") or "scope"


# ---------------------------------------------------------
# The Hub's scopes, as the steps see them
# ---------------------------------------------------------


@dataclass
class HubScope:
    id: int | None  # None: a scope a preview would create
    key: str
    name: str
    type: str | None
    record_id: str | None
    # The universal, public or a system scope: never matched by name.
    reserved: bool = False
    # Created, linked or renamed by a preview (exists only in memory).
    virtual: bool = False


class ScopeIndex:
    """Every scope, indexed by id, Record ID, name and key. Loaded fresh per
    step (plus a preview's virtual overlay) and kept current as the step
    creates, links and renames."""

    def __init__(self, scopes: Iterable[HubScope]) -> None:
        self.scopes: list[HubScope] = []
        self.by_id: dict[int, HubScope] = {}
        self.by_record_id: dict[str, HubScope] = {}
        self._by_name: dict[str, list[HubScope]] = {}
        self.keys: set[str] = set()
        for hs in scopes:
            self.add(hs)

    @classmethod
    def load(cls, db, overlay: Iterable[HubScope] = ()) -> ScopeIndex:
        rows = db.query(
            ScopeV2.id,
            ScopeV2.key,
            ScopeV2.name,
            ScopeV2.type,
            ScopeV2.airtable_record_id,
            ScopeV2.is_universal,
            ScopeV2.is_public,
            ScopeV2.is_system,
        ).all()
        index = cls(
            HubScope(id=r[0], key=r[1], name=r[2], type=r[3], record_id=r[4], reserved=bool(r[5] or r[6] or r[7]))
            for r in rows
        )
        for hs in overlay:
            current = index.by_id.get(hs.id) if hs.id is not None else None
            if current is None:
                index.add(replace(hs))
            else:
                index.update(current, name=hs.name, type=hs.type, record_id=hs.record_id, virtual=True)
        return index

    def add(self, hs: HubScope) -> None:
        self.scopes.append(hs)
        if hs.id is not None:
            self.by_id[hs.id] = hs
        if hs.record_id:
            self.by_record_id[hs.record_id] = hs
        self._by_name.setdefault(name_key(hs.name), []).append(hs)
        self.keys.add(hs.key)

    def update(self, hs: HubScope, *, name: str | None = None, type: str | None = None, record_id: str | None = None, virtual: bool = False) -> None:
        if name is not None and name != hs.name:
            self._by_name[name_key(hs.name)].remove(hs)
            hs.name = name
            self._by_name.setdefault(name_key(name), []).append(hs)
        if record_id is not None and record_id != hs.record_id:
            hs.record_id = record_id
            self.by_record_id[record_id] = hs
        if type is not None:
            hs.type = type
        hs.virtual = hs.virtual or virtual

    def named(self, name: str) -> list[HubScope]:
        return list(self._by_name.get(name_key(name), ()))

    def unlinked_named(self, name: str) -> list[HubScope]:
        """K3 step 2's candidates: same name, no Record ID, not reserved."""
        return [hs for hs in self.named(name) if hs.record_id is None and not hs.reserved]

    def unique_key(self, name: str) -> str:
        base = slugify(name)
        key, n = base, 1
        while key in self.keys:
            n += 1
            suffix = f"_{n}"
            key = base[: KEY_MAX - len(suffix)] + suffix
        return key


def _describe(hs: HubScope) -> dict:
    return {"scope_id": hs.id, "name": hs.name, "key": hs.key, "type": hs.type, "airtable_record_id": hs.record_id}


# ---------------------------------------------------------
# Shared per-step machinery
# ---------------------------------------------------------


class _ScopeStep:
    """One step's state: the scope index, this entity's sync records, its
    item book and its counts."""

    def __init__(self, ctx: RunContext, *, entity: str, scope_type: str, table: str, created_action: str) -> None:
        self.ctx = ctx
        self.db = ctx.db
        self.entity = entity
        self.type = scope_type
        self.table = table
        self.created_action = created_action
        self.counts: Counter[str] = Counter()
        self.book = ItemBook(ctx, entity)
        self.index = ScopeIndex.load(self.db, ctx.state.get(VIRTUAL_SCOPES, ()) if ctx.dry_run else ())
        self._load_records()
        # Names that block this step's scopes besides the Hub's own (the
        # Programs step adds the snapshot's cluster names, A-2).
        self.reserved_names: dict[str, str] = {}

    def _load_records(self) -> None:
        self.records: dict[str, AirtableSyncRecordV2] = {
            r.airtable_record_id: r
            for r in self.db.query(AirtableSyncRecordV2).filter(AirtableSyncRecordV2.entity == self.entity)
        }

    # ── transactions ────────────────────────────────────────────────────

    def commit(self) -> None:
        self.ctx.commit()
        self.book.committed()

    def refused(self, exc: RbacGraphError, action: str, **log: Any) -> None:
        """A service refused one scope's (or edge's) write: undo just that
        transaction, log why, carry on (R3)."""
        self.db.rollback()
        self.book.rolled_back()
        self.index = ScopeIndex.load(self.db)
        self._load_records()
        self.counts["errors"] += 1
        self.log(action, old_value=exc.code, new_value=exc.message, **log)
        self.commit()

    def log(self, action: str, **kw: Any) -> None:
        self.ctx.log(self.entity, action, **kw)

    # ── sync records ────────────────────────────────────────────────────

    def base_of(self, rid: str, hs: HubScope | None) -> dict | None:
        """The agreed base for this record's scope, or None on a first link
        (no record, or the record pointed at another scope)."""
        record = self.records.get(rid)
        if record is None or hs is None or hs.id is None or record.hub_id != hs.id or not record.base:
            return None
        return dict(record.base)

    _KEEP = object()

    def save_record(self, rid: str, rec: SnapshotRecord, *, hub_id: int | None, base: Any = _KEEP) -> None:
        """Real runs only: latest Airtable values, hub_id, last seen run and,
        when given, a new base."""
        if self.ctx.dry_run:
            return
        record = self.records.get(rid)
        if record is None:
            record = AirtableSyncRecordV2(entity=self.entity, airtable_record_id=rid)
            self.db.add(record)
            self.records[rid] = record
        record.hub_id = hub_id
        record.latest_airtable = dict(rec.fields)
        record.last_seen_run_id = self.ctx.run_id
        if base is not self._KEEP:
            record.base = base

    # ── names ───────────────────────────────────────────────────────────

    def taken_by(self, name: str, *, exclude: HubScope | None = None) -> dict | None:
        """Who already has this name (case-insensitively), or None."""
        for hs in self.index.named(name):
            if hs is not exclude:
                return {"kind": "scope", **_describe(hs)}
        reserved = self.reserved_names.get(name_key(name))
        if reserved is not None:
            return {"kind": "cluster", "cluster_record_id": reserved}
        return None

    def name_taken(self, rid: str, name: str, taken: dict, *, reason: str, hs: HubScope | None = None) -> None:
        self.counts["held"] += 1
        self.book.raise_item(
            rid,
            "scope_name_taken",
            inputs=name,
            subject=name,
            hub_id=hs.id if hs else None,
            payload={
                "airtable_name": name,
                "reason": reason,  # create / rename / cluster_name (A-2)
                "current_name": hs.name if hs else None,
                "taken_by": taken,
            },
        )

    # ── the per-record scope logic ──────────────────────────────────────

    def sync_scope(self, rec: SnapshotRecord, rid: str, name: str) -> HubScope | None:
        """Match, create, link or rename one record's scope. Returns the
        scope (virtual in a preview) when the record has one afterwards and
        it isn't held by a type mismatch; None otherwise (held, ambiguous,
        name taken). One transaction, committed here."""
        book = self.book
        book.evaluate(rid, ("scope_deleted", "scope_type_mismatch", "scope_ambiguous_match"))
        hs = self.index.by_record_id.get(rid)
        try:
            if hs is not None:
                return self._linked(rec, rid, name, hs)
            return self._unlinked(rec, rid, name)
        except RbacGraphError as exc:
            self.refused(exc, "scope_error", airtable_record_id=rid, subject=name)
            return None

    def _type_ok(self, rec: SnapshotRecord, rid: str, name: str, hs: HubScope) -> bool:
        """A scope of the other type never becomes this record's: the item
        holds the record (no link, no rename, no edges)."""
        if hs.type in (None, self.type):
            return True
        self.counts["held"] += 1
        self.book.raise_item(
            rid,
            "scope_type_mismatch",
            inputs={"table": self.entity},
            subject=name,
            hub_id=hs.id,
            payload={"airtable_table": self.table, "expected_type": self.type, "scope": _describe(hs)},
        )
        # Linked by Record ID: that IS the record's scope. Matched by name
        # only: it never became the record's.
        self.save_record(rid, rec, hub_id=hs.id if hs.record_id == rid else None)
        self.commit()
        return False

    def _linked(self, rec: SnapshotRecord, rid: str, name: str, hs: HubScope) -> HubScope | None:
        if not self._type_ok(rec, rid, name, hs):
            return None
        self.book.evaluate(rid, ("scope_name_taken", "scope_name_conflict"))
        if hs.type is None:  # Record ID set by hand: the sync's from now on
            self._set_link(hs, rid)
            self.log("linked", airtable_record_id=rid, hub_id=hs.id, subject=name, field="type", new_value=self.type)
            self.counts["linked"] += 1
        base = self.base_of(rid, hs)
        base_name = base.get("name") if base else None
        if base_name is None:
            # First link by Record ID: Airtable's name becomes the base.
            if hs.name != name:
                self._hub_differs(rid, hs, name, name)
            self.save_record(rid, rec, hub_id=hs.id, base={**(base or {}), "name": name})
        elif name == base_name:
            if hs.name != base_name:
                self._hub_differs(rid, hs, base_name, name)
            else:
                self.counts["unchanged"] += 1
            self.save_record(rid, rec, hub_id=hs.id)
        elif hs.name == base_name:
            taken = self.taken_by(name, exclude=hs)
            if taken is not None:
                self.name_taken(rid, name, taken, reason="rename", hs=hs)
                self.save_record(rid, rec, hub_id=hs.id)
            else:
                old = hs.name
                if not self.ctx.dry_run:
                    rbac_service.update_scope(self.db, hs.id, name=name, description=None, description_provided=False)
                self.index.update(hs, name=name, virtual=self.ctx.dry_run)
                self.log("scope_renamed", airtable_record_id=rid, hub_id=hs.id, subject=name, field="name", old_value=old, new_value=name)
                self.counts["renamed"] += 1
                self.save_record(rid, rec, hub_id=hs.id, base={**base, "name": name})
        elif hs.name == name:
            # Both sides made the same change: agree, move the base forward.
            self.counts["unchanged"] += 1
            self.save_record(rid, rec, hub_id=hs.id, base={**base, "name": name})
        else:
            self.counts["held"] += 1
            self.book.raise_item(
                rid,
                "scope_name_conflict",
                inputs=name,
                subject=name,
                hub_id=hs.id,
                payload={
                    "scope_id": hs.id,
                    "base": base_name,
                    "airtable": name,
                    "hub": hs.name,
                    "choices": ["accept_airtable", "keep_current"],
                },
            )
            self.save_record(rid, rec, hub_id=hs.id)
        self.commit()
        return hs

    def _unlinked(self, rec: SnapshotRecord, rid: str, name: str) -> HubScope | None:
        book = self.book
        candidates = self.index.unlinked_named(name)
        if len(candidates) > 1:
            self.counts["held"] += 1
            book.raise_item(
                rid,
                "scope_ambiguous_match",
                inputs=name,
                subject=name,
                payload={
                    "airtable_name": name,
                    "candidates": [_describe(c) for c in candidates],
                    "choices": ["link", "create_new", "skip"],
                },
            )
            self.save_record(rid, rec, hub_id=None)
            self.commit()
            return None
        if candidates:
            hs = candidates[0]
            if not self._type_ok(rec, rid, name, hs):
                return None
            book.evaluate(rid, ("scope_name_taken", "scope_name_conflict"))
            self._set_link(hs, rid)
            self.log("linked", airtable_record_id=rid, hub_id=hs.id, subject=name, old_value=hs.name, new_value=rid)
            self.counts["linked"] += 1
            if hs.name != name:
                self._hub_differs(rid, hs, name, name)
            self.save_record(rid, rec, hub_id=hs.id, base={"name": name})
            self.commit()
            return hs

        book.evaluate(rid, ("scope_name_taken", "scope_name_conflict"))
        taken = self.taken_by(name)
        if taken is not None:
            self.name_taken(rid, name, taken, reason="cluster_name" if taken["kind"] == "cluster" else "create")
            self.save_record(rid, rec, hub_id=None)
            self.commit()
            return None
        return self._create(rec, rid, name)

    def _create(self, rec: SnapshotRecord, rid: str, name: str) -> HubScope:
        key = self.index.unique_key(name)
        if self.ctx.dry_run:
            hs = HubScope(id=None, key=key, name=name, type=self.type, record_id=rid, virtual=True)
        else:
            created = rbac_service.create_scope(self.db, key=key, name=name, description=None, is_universal=False)
            row = self.db.get(ScopeV2, created["id"])
            row.type = self.type
            row.airtable_record_id = rid
            self.db.flush()
            hs = HubScope(id=row.id, key=key, name=name, type=self.type, record_id=rid)
        self.log(self.created_action, airtable_record_id=rid, hub_id=hs.id, subject=name, new_value={"key": key, "name": name})
        self.counts["created"] += 1
        self.save_record(rid, rec, hub_id=hs.id, base={"name": name})
        self.commit()
        self.index.add(hs)  # only once committed
        return hs

    def _set_link(self, hs: HubScope, rid: str) -> None:
        if not self.ctx.dry_run:
            row = self.db.get(ScopeV2, hs.id)
            row.airtable_record_id = rid
            row.type = self.type
            row.updated_at = _utc_now()
            self.db.flush()
        self.index.update(hs, record_id=rid, type=self.type, virtual=self.ctx.dry_run)

    def _hub_differs(self, rid: str, hs: HubScope, base_name: str, airtable_name: str) -> None:
        self.counts["hub_differs"] += 1
        self.log("hub_differs", airtable_record_id=rid, hub_id=hs.id, subject=airtable_name, field="name", old_value=base_name, new_value=hs.name)

    # ── deleted from Airtable (S4) ──────────────────────────────────────

    def belongs(self, hs: HubScope) -> bool:
        if hs.type is not None:
            return hs.type == self.type
        record = self.records.get(hs.record_id)
        return record is not None and record.hub_id == hs.id

    def detect_deleted(self, seen: set[str], *, item_kinds_for_gone: Iterable[str]) -> None:
        """Every linked scope of this step's type whose Record ID is not in
        Airtable any more → `scope_deleted`. Open items of records that are
        gone and own no scope are checked (and so closed) too."""
        deleted: set[str] = set()
        for hs in list(self.index.scopes):
            rid = hs.record_id
            if not rid or rid in seen or hs.id is None or not self.belongs(hs):
                continue
            deleted.add(rid)
            self.book.evaluate(rid, SCOPE_KINDS)
            self.book.raise_item(
                rid,
                "scope_deleted",
                inputs="deleted",
                subject=hs.name,
                hub_id=hs.id,
                payload=self._usage(hs),
            )
            self.counts["deleted"] += 1
        for rid in self.book.open_record_ids() - seen - deleted:
            self.book.evaluate(rid, item_kinds_for_gone)
        self.commit()

    def _usage(self, hs: HubScope) -> dict:
        db = self.db
        people = (
            db.query(UserV2.id, UserV2.name, UserV2.work_email, UserV2.is_deleted, RoleV2.name)
            .join(RoleAssignmentV2, RoleAssignmentV2.user_id == UserV2.id)
            .join(RoleV2, RoleV2.id == RoleAssignmentV2.role_id)
            .filter(RoleAssignmentV2.scope_id == hs.id)
            .order_by(UserV2.name, UserV2.id)
            .all()
        )
        grants = db.query(func.count(ResourceGrantV2.id)).filter(ResourceGrantV2.scope_id == hs.id).scalar()
        unused = not people and not grants
        return {
            "scope": _describe(hs),
            "airtable_table": self.table,
            "assignment_count": len(people),
            "resource_grant_count": grants,
            "people": [
                {"user_id": uid, "name": n, "email": e, "is_deleted": bool(d), "role": role}
                for uid, n, e, d, role in people[:PEOPLE_LISTED]
            ],
            "people_truncated": len(people) > PEOPLE_LISTED,
            # Delete only when nothing uses it (v1 §5.2); the delete itself
            # still goes through rbac_service.delete_scope, which refuses too.
            "choices": (["delete_scope"] if unused else []) + ["keep_hub_only"],
        }

    def summary(self) -> dict:
        out = dict(self.counts)
        out.update(self.book.finish())
        self.commit()
        return dict(sorted(out.items()))


def _valid(step: _ScopeStep, rec: SnapshotRecord, seen: set[str]) -> tuple[str, str] | None:
    """(record id, name), or None after logging why the record is skipped.
    A skipped record with a Record ID still counts as seen (not deleted)."""
    rid, name = rec.record_id, rec.fields.get("name")
    if not rid:
        step.counts["skipped"] += 1
        step.log("skipped_no_record_id", subject=name, new_value=rec.api_id)
        return None
    if rid in seen:
        step.counts["skipped"] += 1
        step.log("skipped_duplicate_record_id", airtable_record_id=rid, subject=name, new_value=rec.api_id)
        return None
    seen.add(rid)
    if not name:
        step.counts["skipped"] += 1
        step.log("skipped_no_name", airtable_record_id=rid, new_value=rec.api_id)
        return None
    return rid, name


# ---------------------------------------------------------
# Clusters (C1)
# ---------------------------------------------------------


def clusters_step(ctx: RunContext) -> dict:
    step = _ScopeStep(ctx, entity="cluster", scope_type="cluster", table="Cluster", created_action="cluster_created")
    seen: set[str] = set()
    for rec in ctx.snapshot.clusters:
        step.counts["records"] += 1
        valid = _valid(step, rec, seen)
        if valid is not None:
            step.sync_scope(rec, *valid)
    step.commit()
    step.detect_deleted(seen, item_kinds_for_gone=SCOPE_KINDS)
    if ctx.dry_run:
        ctx.state[VIRTUAL_SCOPES] = [replace(hs) for hs in step.index.scopes if hs.virtual]
    return step.summary()


# ---------------------------------------------------------
# Programs (S1–S6, C2, C3)
# ---------------------------------------------------------


class _ProgramsStep(_ScopeStep):
    def __init__(self, ctx: RunContext) -> None:
        super().__init__(ctx, entity="program", scope_type="program", table="Public incl. Finance Dashboard", created_action="scope_created")
        # The cluster lookups come from the SNAPSHOT, not from Hub scopes, so
        # a Programs-only run still knows every cluster (R2, trap 5).
        self.clusters: dict[str, SnapshotRecord] = {}
        self.cluster_by_api: dict[str, str] = {}
        names: dict[str, list[str]] = {}
        for c in ctx.snapshot.clusters:
            if not c.record_id:
                continue
            self.clusters.setdefault(c.record_id, c)
            self.cluster_by_api[c.api_id] = c.record_id
            if c.fields.get("name"):
                names.setdefault(name_key(c.fields["name"]), []).append(c.record_id)
        self.cluster_names = names
        # A-2: a program may never take a cluster's name.
        self.reserved_names = {k: rids[0] for k, rids in names.items()}

    def cluster_name(self, crid: str) -> str | None:
        c = self.clusters.get(crid)
        return c.fields.get("name") if c else None

    def run(self) -> dict:
        seen: set[str] = set()
        aliases: set[str] = set()
        for rec in self.ctx.snapshot.programs:
            self.counts["records"] += 1
            valid = _valid(self, rec, seen)
            if valid is None:
                continue
            rid, name = valid
            air = list(rec.fields.get("cluster_record_ids") or [])
            self._mismatch(rid, name, rec, air)
            same_named = self.cluster_names.get(name_key(name), [])
            if not air and len(same_named) == 1:
                aliases.add(rid)
                self._alias(rec, rid, name, same_named[0])
                continue
            if air and same_named and rid not in self.index.by_record_id:
                # A-2: named like a cluster but not the cluster itself.
                self.book.evaluate(rid, ("scope_deleted", "scope_type_mismatch", "scope_ambiguous_match", "scope_name_taken", "scope_name_conflict"))
                self.name_taken(rid, name, {"kind": "cluster", "cluster_record_id": same_named[0]}, reason="cluster_name")
                self.save_record(rid, rec, hub_id=None)
                self.commit()
                continue
            hs = self.sync_scope(rec, rid, name)
            if hs is not None:
                self._edges(rec, rid, name, hs, air)
        self.commit()
        # A program row that became a cluster alias no longer owns a program
        # scope: if one is still linked to its Record ID, that's S4 too.
        self.detect_deleted(seen - aliases, item_kinds_for_gone=SCOPE_KINDS + EDGE_KINDS)
        return self.summary()

    # ── C2 ──────────────────────────────────────────────────────────────

    def _alias(self, rec: SnapshotRecord, rid: str, name: str, cluster_rid: str) -> None:
        """The row IS the cluster: no scope of its own, a `program` record
        pointing at the cluster's scope (Program Roles on it land there)."""
        self.book.evaluate(rid, SCOPE_KINDS + EDGE_KINDS)
        cluster = self.index.by_record_id.get(cluster_rid)
        hub_id = cluster.id if cluster is not None else None
        self.counts["aliases"] += 1
        self.log("program_is_cluster", airtable_record_id=rid, hub_id=hub_id, subject=name, new_value=cluster_rid)
        if cluster is None:
            self.counts["waiting"] += 1
            self.log("scope_not_synced", airtable_record_id=rid, subject=name, field="cluster", new_value=cluster_rid)
        self.save_record(rid, rec, hub_id=hub_id, base={"name": name, "is_cluster": True, "cluster_record_id": cluster_rid})
        self.commit()

    # ── D-6: text vs link ───────────────────────────────────────────────

    def _mismatch(self, rid: str, name: str | None, rec: SnapshotRecord, air: list[str]) -> None:
        """The text is what counts; the link disagreeing is only logged.
        The link holds Cluster API ids: mapped through the snapshot to
        "Record Id" values before comparing."""
        linked = [self.cluster_by_api.get(api, f"unknown link {api}") for api in rec.fields.get("cluster_links") or []]
        if set(linked) != set(air):
            self.counts["cluster_mismatches"] += 1
            self.log("cluster_mismatch", airtable_record_id=rid, subject=name, field="cluster", old_value=linked, new_value=air)

    # ── C3: the parent set, three-way ───────────────────────────────────

    def _edges(self, rec: SnapshotRecord, rid: str, name: str, hs: HubScope, air: list[str]) -> None:
        """Airtable (the text set) vs base (`base.clusters`, the last agreed
        set) vs Hub (this program's `airtable_sync` edges), per cluster:

          Airtable adds          → add the edge (`edge_added`); a hand-made
                                   edge already there counts as present and
                                   STAYS manual (`edge_is_manual`)
          Airtable drops, sync
          edge still there       → `cluster_edge_removed` item (field = the
                                   cluster); the edge stays, base keeps it
          Hub dropped it only    → `hub_differs`, nothing re-added
          cluster scope missing  → waits (`scope_not_synced`), retried next run
        An id matching no cluster → `unknown_cluster`, which holds every edge
        of this program (its other items are carried, not re-checked)."""
        book = self.book
        book.evaluate(rid, ("unknown_cluster",))
        unknown = [c for c in air if c not in self.clusters]
        if unknown:
            self.counts["held"] += 1
            book.raise_item(
                rid,
                "unknown_cluster",
                inputs=sorted(unknown),
                subject=name,
                hub_id=hs.id,
                payload={"unknown_cluster_record_ids": unknown, "cluster_record_ids": air},
            )
            self.commit()
            return
        book.evaluate(rid, ("cluster_edge_removed",))

        base = self.base_of(rid, hs)
        agreed = base.get("clusters") if base else None
        sync_parents: set[int] = set()
        manual_parents: set[int] = set()
        if hs.id is not None:
            for parent_id, source in self.db.query(ScopeEdgeV2.parent_scope_id, ScopeEdgeV2.source).filter(
                ScopeEdgeV2.child_scope_id == hs.id
            ):
                (sync_parents if source == SYNC_EDGE else manual_parents).add(parent_id)

        def rids_of(parent_ids: set[int]) -> set[str]:
            # Only edges to a scope still linked to a cluster take part; a
            # sync edge to anything else is left alone.
            out = set()
            for pid in parent_ids:
                p = self.index.by_id.get(pid)
                if p is not None and p.record_id and p.type in (None, "cluster"):
                    out.add(p.record_id)
            return out

        a_set, b_set = set(air), set(agreed or [])
        h_set, m_set = rids_of(sync_parents), rids_of(manual_parents)
        new_base: set[str] = set()
        removed: list[str] = []
        for crid in sorted(a_set | b_set | h_set):
            in_a, in_b, in_h, in_m = crid in a_set, crid in b_set, crid in h_set, crid in m_set
            cname = self.cluster_name(crid)
            edge_log = {"airtable_record_id": rid, "hub_id": hs.id, "subject": name, "field": "cluster"}
            if not in_a:
                if in_h:
                    removed.append(crid)
                    if in_b:
                        new_base.add(crid)  # still agreed until an admin answers
                # else Airtable dropped it and the Hub has no sync edge: agreed.
                continue
            if in_h or in_m:
                new_base.add(crid)
                if in_m and not in_h and not in_b:
                    self.log("edge_is_manual", **edge_log, new_value={"cluster": crid, "name": cname})
                continue
            if in_b:
                # Airtable unchanged, an admin removed the edge: keep the Hub.
                new_base.add(crid)
                self.counts["hub_differs"] += 1
                self.log("hub_differs", **edge_log, old_value={"cluster": crid, "name": cname}, new_value=None)
                continue
            parent = self.index.by_record_id.get(crid)
            if parent is None or parent.type == "program":
                self.counts["waiting"] += 1
                self.log("scope_not_synced", **edge_log, new_value={"cluster": crid, "name": cname})
                continue
            if not self.ctx.dry_run:
                try:
                    create_scope_edge(self.db, parent.id, hs.id, source=SYNC_EDGE)
                except RbacGraphError as exc:
                    self.refused(exc, "edge_refused", **edge_log)
                    continue
            new_base.add(crid)
            self.counts["edges_added"] += 1
            self.log("edge_added", **edge_log, new_value={"cluster": crid, "name": cname})
            self.commit()

        for crid in removed:
            parent = self.index.by_record_id.get(crid)
            self.counts["edge_removals"] += 1
            book.raise_item(
                rid,
                "cluster_edge_removed",
                field=crid,
                inputs=crid,
                subject=name,
                hub_id=hs.id,
                payload={
                    "program_scope_id": hs.id,
                    "program_name": name,
                    "cluster_record_id": crid,
                    "cluster_scope_id": parent.id if parent else None,
                    "cluster_name": parent.name if parent else self.cluster_name(crid),
                    "cluster_record_ids": air,
                    "choices": ["remove_edge", "keep"],
                },
            )
        if not self.ctx.dry_run:
            record = self.records.get(rid)
            if record is not None:
                record.base = {**(record.base or {}), "clusters": sorted(new_base)}
        self.commit()


def programs_step(ctx: RunContext) -> dict:
    return _ProgramsStep(ctx).run()
