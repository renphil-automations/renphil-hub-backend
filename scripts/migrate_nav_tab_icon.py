"""
One-time migration for the nav-tab icon feature (see
AI Docs/plan_ui_features_2026-08-04.md §3 for the full design).

Goal: add the nullable `nav_tabs.icon` column. NULL means "no icon chosen",
which the frontend renders as today's default (`LayoutGrid`) — there is no
backfill and no uniqueness constraint, matching the feature's locked design.

  1. ALTER `nav_tabs` to add the `icon VARCHAR(64)` column, if missing.

On Postgres a nullable ADD COLUMN with no default is a catalog-only change —
no table rewrite, no lock of consequence.

Idempotent / safe to re-run: a second run finds the column already present
and reports "already up to date".

Landmine: `icon` stores a lucide-react icon name as a plain lookup key, never
validated against the icon library itself. Names get renamed/deprecated
between lucide-react minor versions — a stored name that disappears degrades
to the fallback icon client-side (not a crash), but a version bump could
silently reset someone's chosen icon to the default. Worth a spot-check after
any lucide-react upgrade.

Safety:
  - DRY RUN BY DEFAULT. Prints the plan and does not write anything unless
    invoked with --commit.
  - The ALTER TABLE DDL is itself idempotent (IF NOT EXISTS) and safe to
    re-run; Postgres auto-commits DDL per statement, so there is no separate
    transaction to roll back.

Run from renphil-hub-backend/, with the venv python:
  Dry run (default, no writes):
    PYTHONPATH=. ./.venv/Scripts/python.exe scripts/migrate_nav_tab_icon.py

  Real run (writes, against the live Neon renphil-hub-db — DATABASE_URL_V2):
    PYTHONPATH=. ./.venv/Scripts/python.exe scripts/migrate_nav_tab_icon.py --commit
"""

import argparse

from sqlalchemy import text

from app.db_v2.database import engine_v2


def _column_exists() -> bool:
    with engine_v2.connect() as conn:
        return bool(
            conn.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'nav_tabs' AND column_name = 'icon')"
                )
            ).scalar()
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write changes. Omit for a dry run (default) that only prints the plan.",
    )
    args = parser.parse_args()
    dry_run = not args.commit

    print("=" * 70)
    print("DRY RUN — no writes will be made" if dry_run else "COMMIT MODE — writes will be made")
    print("=" * 70)

    already_present = _column_exists()
    print(f"\nnav_tabs.icon column: {'already exists' if already_present else 'to ADD'}")

    if already_present:
        print("\nNothing to do — already up to date.")
    elif dry_run:
        print("\nWould run: ALTER TABLE nav_tabs ADD COLUMN IF NOT EXISTS icon VARCHAR(64);")
    else:
        with engine_v2.begin() as conn:
            conn.execute(text("ALTER TABLE nav_tabs ADD COLUMN IF NOT EXISTS icon VARCHAR(64)"))
        print("\nColumn added.")

    print("\n" + "=" * 70)
    if dry_run:
        print("This was a DRY RUN — nothing was written. Re-run with --commit to apply.")
    else:
        print("Changes committed.")
    print("=" * 70)


if __name__ == "__main__":
    main()
