"""
Test-only: make field conflicts on a THROWAWAY Neon branch, so the
background "Accept all" / "Keep all" (bulk_jobs.py) can be tried on real
items (owner, 2026-10-28). NEVER for Dev, Sandbox or prod: it refuses them.

For each synced person (an `airtable_sync_records` user row linked to a
live `users` row) whose last run read FIELD, it:

  - sets the Hub value to "[branch test] <old value>", and
  - drops FIELD from the record's base (the last agreed value), and
  - sets aside an earlier answer to a conflict on FIELD for them (resolved
    -> superseded). Otherwise the sync "remembers" it (items.py, G1): the
    same Airtable value answered once is answered again silently, and no
    item opens. On Dev most people answered run 9's conflicts this way.

The next Import then sees a first link with Airtable ≠ Hub, which is a
`field_conflict` (hr_fields.py, M2) for each of them. "Accept Airtable"
writes Airtable's value back; "Keep current" keeps the test value. Only
text fields are offered: answering them writes a value and nothing else
(no headshot upload, no email checks, no sign-in effects).

Run it with the backend's DATABASE_URL_V2 pointed at the branch:

  .venv/Scripts/python.exe scripts/make_branch_field_conflicts.py --branch-host ep-xxxx-yyyy
  .venv/Scripts/python.exe scripts/make_branch_field_conflicts.py --branch-host ep-xxxx-yyyy --commit

`--branch-host` must equal the host the script connects to (its first
label, as printed), so a forgotten .env switch can't hit another database.
Optional: `--field` (default position), `--limit N` (default: everyone).

Undo: delete the branch.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import and_, select, update  # noqa: E402
from sqlalchemy.engine import Connection  # noqa: E402

from app.db_v2.database import engine_v2  # noqa: E402
from app.db_v2.models.airtable_sync import (  # noqa: E402
    AirtableSyncBulkJobV2,
    AirtableSyncItemV2,
    AirtableSyncRecordV2,
    AirtableSyncRunV2,
)
from app.db_v2.models.user import UserV2  # noqa: E402
from app.services.airtable_sync.items import NOT_REMEMBERED  # noqa: E402

# Databases this script must never touch (first label of the Neon host).
REFUSED_HOSTS = {
    "ep-raspy-silence": "Dev",
    "ep-lively-tooth": "Sandbox",
    "ep-dawn-fire": "a known shared database",
}

SAFE_FIELDS = ("position", "department", "office_location", "bio", "scope_of_work")
MARK = "[branch test] "


def make_conflicts(conn: Connection, field: str, limit: int | None = None) -> dict:
    """Prepare up to `limit` people for a `field_conflict` on `field`.
    Returns counts; writes inside the caller's transaction."""
    records = AirtableSyncRecordV2.__table__
    users = UserV2.__table__
    items = AirtableSyncItemV2.__table__
    col = users.c[field]

    busy_run = conn.execute(
        select(AirtableSyncRunV2.id).where(AirtableSyncRunV2.status == "running")
    ).first()
    busy_job = conn.execute(
        select(AirtableSyncBulkJobV2.id).where(AirtableSyncBulkJobV2.status == "running")
    ).first()
    if busy_run or busy_job:
        raise RuntimeError("A sync run or a bulk answer is in progress; try again once it has ended.")

    rows = conn.execute(
        select(records.c.id, records.c.airtable_record_id, records.c.base, records.c.latest_airtable, users.c.id.label("user_id"), col.label("hub"))
        .select_from(records.join(users, users.c.id == records.c.hub_id))
        .where(and_(records.c.entity == "user", users.c.deleted_at.is_(None)))
        .order_by(records.c.id)
    ).all()

    linked = len(rows)
    # The last run read the field (it is in latest_airtable) and the base
    # holds an agreed value for it: the only state a first link can follow.
    eligible = [
        r for r in rows
        if isinstance(r.base, dict) and field in r.base
        and isinstance(r.latest_airtable, dict) and field in r.latest_airtable
    ]
    chosen = eligible if limit is None else eligible[:limit]

    for r in chosen:
        old = r.hub or ""
        new = old if old.startswith(MARK) else MARK + old
        conn.execute(update(users).where(users.c.id == r.user_id).values({field: new.strip() or MARK.strip()}))
        base = {k: v for k, v in r.base.items() if k != field}
        conn.execute(update(records).where(records.c.id == r.id).values(base=base))

    set_aside = 0
    rids = [r.airtable_record_id for r in chosen]
    for i in range(0, len(rids), 500):
        set_aside += conn.execute(
            update(items)
            .where(
                and_(
                    items.c.entity == "user",
                    items.c.kind == "field_conflict",
                    items.c.field == field,
                    items.c.status == "resolved",
                    items.c.resolution.notin_(NOT_REMEMBERED),
                    items.c.airtable_record_id.in_(rids[i : i + 500]),
                )
            )
            .values(status="superseded")
        ).rowcount

    return {
        "linked": linked,
        "eligible": len(eligible),
        "changed": len(chosen),
        "answers_set_aside": set_aside,
        "user_ids": [r.user_id for r in chosen],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Make field conflicts on a throwaway Neon branch.")
    parser.add_argument("--branch-host", required=True, help="The branch's host, first label (e.g. ep-xxxx-yyyy).")
    parser.add_argument("--field", default="position", choices=SAFE_FIELDS)
    parser.add_argument("--limit", type=int, default=None, help="At most this many people (default: all).")
    parser.add_argument("--commit", action="store_true", help="Write. Omit for a dry run that rolls back.")
    args = parser.parse_args()

    print("=" * 70)
    print("COMMIT MODE — writes will be made" if args.commit else "DRY RUN — runs every step, then ROLLS BACK")
    print("=" * 70)
    host = (engine_v2.url.host or "").split(".")[0]
    print(f"Target host: {host or '(none)'}  database: {engine_v2.url.database}")

    if engine_v2.dialect.name != "postgresql":
        print(f"  REFUSING: dialect is {engine_v2.dialect.name!r}, not 'postgresql'.")
        raise SystemExit(1)
    base_host = host.removesuffix("-pooler")
    # A Neon host is "<name>-<id>" (e.g. ep-raspy-silence-a1b2c3d4): match the name.
    refused = next((k for k in REFUSED_HOSTS if base_host == k or base_host.startswith(k + "-")), None)
    if refused:
        print(f"  REFUSING: {host} is {REFUSED_HOSTS[refused]}, not a throwaway branch.")
        raise SystemExit(1)
    if host != args.branch_host and base_host != args.branch_host:
        print(f"  REFUSING: connected to {host!r}, but --branch-host is {args.branch_host!r}.")
        raise SystemExit(1)
    if args.limit is not None and args.limit < 1:
        print("  REFUSING: --limit must be at least 1.")
        raise SystemExit(1)

    with engine_v2.connect() as conn:
        trans = conn.begin()
        try:
            result = make_conflicts(conn, args.field, args.limit)
        except Exception as exc:
            trans.rollback()
            print(f"\n{exc}\nRolled back — nothing was written.")
            raise SystemExit(1) from exc

        print(f"\nField: {args.field}")
        print(f"  synced people linked to a live user: {result['linked']}")
        print(f"  eligible (last run read the field):  {result['eligible']}")
        print(f"  prepared for a conflict:             {result['changed']}")
        print(f"  earlier answers set aside:           {result['answers_set_aside']}")
        if result["user_ids"]:
            shown = ", ".join(str(i) for i in result["user_ids"][:10])
            more = " …" if len(result["user_ids"]) > 10 else ""
            print(f"  user ids: {shown}{more}")
        if not result["changed"]:
            trans.rollback()
            print("\nNothing to do.")
            return
        if args.commit:
            trans.commit()
            print("\n" + "=" * 70 + "\nCommitted. Run an Import from the tab: expect about this many field conflicts.\n" + "=" * 70)
        else:
            trans.rollback()
            print("\n" + "=" * 70 + "\nDRY RUN — rolled back, nothing was written. Re-run with --commit to apply.\n" + "=" * 70)


if __name__ == "__main__":
    main()
