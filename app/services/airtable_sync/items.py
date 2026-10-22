"""Conflicts and warnings ("items", plan v2 §6; v1 plan §3.2, §5.3.9, G1).

One `ItemBook` per step and entity. A step tells it, record by record, which
item kinds it CHECKED (`evaluate`) and which of them it found (`raise_item`);
`finish` then closes every open item whose condition was checked and not
found. Items whose condition was not checked this run (the record was held
by another item, or the step did not run) are left exactly as they are.

  raise_item, no open item          → open a new one
              open, same fingerprint → refresh it (payload, hub_id, subject,
                                       last_run_id)
              open, new fingerprint  → the old one becomes `superseded`, a
                                       new one opens (v1 §5.3.9)
              resolved earlier with the same fingerprint (an admin's Skip or
              Keep) → not reopened: "remembered" (G1)
  finish      open + checked + not raised → `resolved`, resolution
              `auto_closed` (v1: "—" items close when the problem is gone;
              items with buttons close the same way, so a stale choice such as
              "Delete scope" can never be applied to a record that came back)

The FINGERPRINT is a hash of the Airtable inputs that produced the item (v1
§3.2), never of Hub-side values: a Hub-side change refreshes the payload but
neither supersedes the item nor forgets a Skip. An `auto_closed` item is not
a remembered answer — the same problem coming back reopens.

In a preview nothing is written: each found item becomes a
`would_raise_<kind>` log row (`old_value` = what a real run would do with it:
`open`, `refresh`, `supersede` or `remembered`) and each would-be close an
`would_auto_close` row.

Whether an item HOLDS anything is the caller's business (v2 §3.5), and does
not depend on the outcome: a remembered Skip still holds.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.db_v2.models.airtable_sync import AirtableSyncItemV2

if TYPE_CHECKING:
    from app.services.airtable_sync.runner import RunContext

AUTO_CLOSED = "auto_closed"

# Item key within one entity: (airtable_record_id, kind, field).
_Key = tuple[str, str, str]


def fingerprint(kind: str, inputs: Any) -> str:
    """sha256 of the kind and the Airtable inputs, canonical JSON. 64 hex
    characters, the column's width."""
    raw = json.dumps([kind, inputs], sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ItemBook:
    def __init__(self, ctx: RunContext, entity: str) -> None:
        self.ctx = ctx
        self.entity = entity
        db = ctx.db
        self._open: dict[_Key, AirtableSyncItemV2] = {
            (i.airtable_record_id, i.kind, i.field): i
            for i in db.query(AirtableSyncItemV2).filter(
                AirtableSyncItemV2.entity == entity, AirtableSyncItemV2.status == "open"
            )
        }
        # Answers an admin gave (Skip, Keep…), by fingerprint (G1).
        self._answered: set[tuple[str, str, str, str]] = {
            (rid, kind, fld, fp)
            for rid, kind, fld, fp in db.query(
                AirtableSyncItemV2.airtable_record_id,
                AirtableSyncItemV2.kind,
                AirtableSyncItemV2.field,
                AirtableSyncItemV2.fingerprint,
            ).filter(
                AirtableSyncItemV2.entity == entity,
                AirtableSyncItemV2.status == "resolved",
                AirtableSyncItemV2.resolution != AUTO_CLOSED,
            )
        }
        # (rid, kind) pairs checked this run; a kind checked for a record
        # covers every `field` of that kind (e.g. one item per cluster edge).
        self._checked: set[tuple[str, str]] = set()
        self._raised: set[_Key] = set()
        # Open items replaced since the last commit, so a rolled-back scope
        # transaction can restore the in-memory index (`rolled_back`).
        self._journal: list[tuple[_Key, AirtableSyncItemV2 | None]] = []
        self.counts: dict[str, int] = {}

    def committed(self) -> None:
        self._journal.clear()

    def rolled_back(self) -> None:
        """The step rolled its transaction back: forget the items it opened
        since the last commit (the superseded ones are open again in the DB)."""
        for key, prior in reversed(self._journal):
            if prior is None:
                self._open.pop(key, None)
            else:
                self._open[key] = prior
        self._journal.clear()

    def _count(self, what: str) -> None:
        self.counts[what] = self.counts.get(what, 0) + 1

    def open_kinds(self, airtable_record_id: str) -> set[str]:
        """Kinds open for one record before this run touched them."""
        return {k for (rid, k, _f) in self._open if rid == airtable_record_id}

    def open_record_ids(self) -> set[str]:
        return {rid for (rid, _k, _f) in self._open}

    def evaluate(self, airtable_record_id: str, kinds: Iterable[str]) -> None:
        """Declare that these kinds were checked for this record this run."""
        for kind in kinds:
            self._checked.add((airtable_record_id, kind))

    def raise_item(
        self,
        airtable_record_id: str,
        kind: str,
        *,
        inputs: Any,
        payload: dict,
        subject: str | None,
        hub_id: int | None = None,
        field: str = "",
    ) -> str:
        """Record that the condition holds. Returns the outcome: `opened`,
        `refreshed`, `superseded` or `remembered` (in a preview, what a real
        run would have done). Implies `evaluate` for this kind."""
        ctx = self.ctx
        key = (airtable_record_id, kind, field)
        self._checked.add((airtable_record_id, kind))
        self._raised.add(key)
        fp = fingerprint(kind, inputs)
        current = self._open.get(key)
        if current is not None:
            outcome = "refreshed" if current.fingerprint == fp else "superseded"
        elif (airtable_record_id, kind, field, fp) in self._answered:
            outcome = "remembered"
        else:
            outcome = "opened"
        self._count(f"items_{outcome}")

        if ctx.dry_run:
            ctx.log(
                self.entity,
                f"would_raise_{kind}",
                airtable_record_id=airtable_record_id,
                hub_id=hub_id,
                subject=subject,
                field=field or None,
                old_value={"opened": "open", "refreshed": "refresh", "superseded": "supersede"}.get(outcome, outcome),
                new_value=payload,
            )
            return outcome

        if outcome == "refreshed":
            current.payload = payload
            current.hub_id = hub_id
            current.subject = subject
            current.last_run_id = ctx.run_id
            return outcome
        if outcome == "remembered":
            return outcome
        if outcome == "superseded":
            current.status = "superseded"
            current.resolved_at = _utc_now()
            current.last_run_id = ctx.run_id
            ctx.db.flush()  # free the open-item index before the insert
        item = AirtableSyncItemV2(
            entity=self.entity,
            airtable_record_id=airtable_record_id,
            hub_id=hub_id,
            subject=subject,
            kind=kind,
            field=field,
            payload=payload,
            fingerprint=fp,
            status="open",
            first_run_id=ctx.run_id,
            last_run_id=ctx.run_id,
        )
        ctx.db.add(item)
        self._journal.append((key, current))
        self._open[key] = item
        return outcome

    def finish(self) -> dict[str, int]:
        """Close every open item whose condition was checked and not found.
        The caller commits."""
        ctx = self.ctx
        for key, item in list(self._open.items()):
            rid, kind, _field = key
            if key in self._raised or (rid, kind) not in self._checked:
                continue
            self._count("items_closed")
            if ctx.dry_run:
                ctx.log(
                    self.entity,
                    "would_auto_close",
                    airtable_record_id=rid,
                    hub_id=item.hub_id,
                    subject=item.subject,
                    field=kind,
                    old_value=item.field or None,
                )
                continue
            item.status = "resolved"
            item.resolution = AUTO_CLOSED
            item.resolved_at = _utc_now()
            item.last_run_id = ctx.run_id
            ctx.log(
                self.entity,
                "item_auto_closed",
                airtable_record_id=rid,
                hub_id=item.hub_id,
                subject=item.subject,
                field=kind,
                old_value=item.field or None,
            )
            del self._open[key]
        return dict(self.counts)
