"""
One-shot population: seed `hub_users` from the existing `users` table.

Both tables live in the SAME database (DATABASE_URL_V2 / PG_V2_* env vars —
the live Neon `renphil-hub-db`, confirmed via information_schema on
2026-09-05): `users` is `app/db_v2/models/user.py`'s `UserV2`, the Airtable
HR roster mirror (primary key `name`); `hub_users` is
`app/db_v2/models/hub_user.py`'s `HubUserV2`, the RBAC identity table that
`app.dependencies` normally auto-provisions one row into on first login.

This script pre-provisions `hub_users` rows for the existing roster instead
of waiting for each person's first login — closing the gap
`app/services/rbac_assignment_service.py` calls `target_not_provisioned`:
today an admin cannot grant a role to someone who has never signed in,
because there is no `hub_users` row yet to attach the assignment to.

Mapping (owner-confirmed 2026-09-05):
    users.name        -> hub_users.name    (whitespace-stripped)
    users.work_email  -> hub_users.email    (lower-cased + stripped, the same
                          normalization `HubUserV2.email`'s own comment says
                          the service layer applies everywhere else)
    (literal True)    -> hub_users.is_active
    NOW()             -> hub_users.created_at (insert only) and
                          hub_users.updated_at (insert, and every update)

Filter — which `users` rows are candidates (owner-confirmed):
    - EXCLUDE is_deleted = true (NULL is treated as false; as of 2026-09-05
      every row is still NULL here, so this excludes nothing yet)
    - EXCLUDE "terminated": "EndDate" is not null AND "EndDate" <= CURRENT_DATE
    - Do NOT filter on `status` — "Onboarding" rows are included on purpose;
      pre-provisioning before a first login is the whole point of this script
    - SKIP rows with a null/blank work_email (reported, not inserted)

In-source duplicate work_email (owner-confirmed): two clusters exist today
(3 rows for one Pedro Mendonça hire, 2 for one Zachary Braunstein hire —
duplicate/misspelled name entries, same work_email). When 2+ candidate rows
resolve to the same normalized email, the script STOPS and asks
interactively which single row should become that email's hub_users row
(or none). Only one row per email can ever exist in hub_users regardless —
ix_hub_users_email is a real UNIQUE index in the live database.

Conflict with an existing hub_users row (owner-confirmed): asked
interactively PER ROW, offering update or skip only. "Duplicate" is not
offered — a second row sharing an email is not something this database will
ever allow to be written, so there is nothing for that choice to do.

DRY RUN BY DEFAULT, same convention as scripts/migrate_tab_locked_at.py and
scripts/repair_thread_counters.py: the whole plan runs inside one
transaction, then rolls back instead of committing, so the printed report
reflects what would actually happen rather than a hand-simulated
approximation. The interactive prompts (duplicate-cluster picks, per-row
conflict update/skip) still happen in dry-run mode — those are decisions,
not writes, and re-running for real would ask them again anyway. Pass
--commit to keep the changes.

Run from renphil-hub-backend/, with the venv python:

    Dry run:
        PYTHONPATH=. ./.venv/Scripts/python.exe scripts/populate_hub_users_from_users.py

    Real run:
        PYTHONPATH=. ./.venv/Scripts/python.exe scripts/populate_hub_users_from_users.py --commit
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Several names in `users` use non-ASCII characters (e.g. "Mendonça"), and a
# Windows console defaults to cp1252, which cannot encode them. Forcing
# UTF-8 here makes the encoding a property of the script, not of whichever
# terminal happens to run it — same reasoning as scripts/survey_hub_admin_parity.py.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text  # noqa: E402

from app.db_v2.database import SessionLocalV2  # noqa: E402


def _normalize_email(raw: str | None) -> str | None:
    if raw is None:
        return None
    cleaned = raw.strip().lower()
    return cleaned or None


def _fetch_candidates(db) -> tuple[list[dict], list[str]]:
    """Rows from `users` passing the is_deleted/terminated filter, still
    possibly sharing a duplicated email — `_resolve_duplicates` handles that.
    Returns (candidates, names_skipped_for_blank_email).
    """
    rows = db.execute(
        text(
            """
            SELECT name, work_email, status, start_date
            FROM users
            WHERE COALESCE(is_deleted, false) = false
              AND NOT ("EndDate" IS NOT NULL AND "EndDate" <= CURRENT_DATE)
            ORDER BY name
            """
        )
    ).mappings().all()

    candidates: list[dict] = []
    skipped_blank_email: list[str] = []
    for row in rows:
        email = _normalize_email(row["work_email"])
        if not email:
            skipped_blank_email.append(row["name"])
            continue
        candidates.append({**row, "email": email, "name": row["name"].strip()})
    return candidates, skipped_blank_email


def _resolve_duplicates(candidates: list[dict]) -> list[dict]:
    """Interactively collapse rows that share a normalized email down to one
    row per email (owner-confirmed 2026-09-05: prompt per cluster)."""
    by_email: dict[str, list[dict]] = {}
    for c in candidates:
        by_email.setdefault(c["email"], []).append(c)

    resolved: list[dict] = []
    for email, rows in sorted(by_email.items()):
        if len(rows) == 1:
            resolved.append(rows[0])
            continue

        print(f"\n{'=' * 78}\nDuplicate work_email in `users`: {email}\n{'=' * 78}")
        for i, r in enumerate(rows, start=1):
            print(f"  [{i}] name={r['name']!r}  status={r['status']!r}  start_date={r['start_date']}")
        print("  [0] skip this email entirely (insert nothing for it)")

        while True:
            choice = input(f"Which row should become hub_users' row for {email}? ").strip()
            if choice == "0":
                print(f"  -> skipping {email} entirely")
                break
            if choice.isdigit() and 1 <= int(choice) <= len(rows):
                picked = rows[int(choice) - 1]
                resolved.append(picked)
                print(f"  -> keeping {picked['name']!r}")
                break
            print("  Enter a number from the list above, or 0 to skip.")
    return resolved


def _process(db, candidates: list[dict]) -> dict:
    stats = {"inserted": 0, "updated": 0, "skipped_conflict": 0}
    now = datetime.now(timezone.utc)

    for c in sorted(candidates, key=lambda r: r["email"]):
        existing = db.execute(
            text("SELECT id, name, is_active, created_at FROM hub_users WHERE lower(trim(email)) = :email"),
            {"email": c["email"]},
        ).mappings().first()

        if existing is None:
            db.execute(
                text(
                    """
                    INSERT INTO hub_users (email, name, is_active, created_at, updated_at)
                    VALUES (:email, :name, true, :now, :now)
                    """
                ),
                {"email": c["email"], "name": c["name"], "now": now},
            )
            stats["inserted"] += 1
            print(f"  INSERT  {c['email']:<40} name={c['name']!r}")
            continue

        print(f"\n{'=' * 78}\nConflict: {c['email']} already exists in hub_users (id={existing['id']})\n{'=' * 78}")
        print(f"  existing: name={existing['name']!r}  is_active={existing['is_active']}  created_at={existing['created_at']}")
        print(f"  incoming: name={c['name']!r}  is_active=True")

        while True:
            choice = input("  [u]pdate existing row / [s]kip this row? ").strip().lower()
            if choice in ("u", "update"):
                db.execute(
                    text(
                        """
                        UPDATE hub_users SET name = :name, is_active = true, updated_at = :now
                        WHERE id = :id
                        """
                    ),
                    {"name": c["name"], "now": now, "id": existing["id"]},
                )
                stats["updated"] += 1
                print(f"  -> updated id={existing['id']}")
                break
            if choice in ("s", "skip"):
                stats["skipped_conflict"] += 1
                print("  -> skipped")
                break
            print("  Enter u or s.")

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit",
        action="store_true",
        help=(
            "Actually commit the changes. Without this flag the script runs the "
            "full interactive plan and then rolls back, so nothing is written."
        ),
    )
    args = parser.parse_args()

    db = SessionLocalV2()
    try:
        candidates, skipped_blank = _fetch_candidates(db)
        print(f"Candidates after is_deleted/terminated filter: {len(candidates) + len(skipped_blank)}")
        if skipped_blank:
            print(f"Skipping {len(skipped_blank)} row(s) with no work_email:")
            for name in skipped_blank:
                print(f"  - {name!r}")

        resolved = _resolve_duplicates(candidates)
        print(f"\n{len(resolved)} row(s) will be considered for insert/update.")

        stats = _process(db, resolved)

        print(f"\n{'=' * 78}\nSummary\n{'=' * 78}")
        print(f"  inserted:           {stats['inserted']}")
        print(f"  updated:            {stats['updated']}")
        print(f"  skipped (conflict): {stats['skipped_conflict']}")
        print(f"  skipped (no email): {len(skipped_blank)}")

        if args.commit:
            db.commit()
            print("\nCOMMITTED.")
        else:
            db.rollback()
            print("\nDRY RUN — rolled back, nothing was written. Re-run with --commit to apply.")
    except KeyboardInterrupt:
        db.rollback()
        print("\nAborted by user — rolled back, nothing was written.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
