"""
Thread widget counter repair + identity reconciliation
(plan_thread_widget_2026-08-17.md §4.4, §3.6).

Two independent modes, sharing this script per the plan's own reasoning
("reconcile stored values against the source of truth... one script is
easier to remember than two" — §4.4):

1. Default mode — recompute every thread's up_count/down_count/comment_count
   and every comment's up_count/down_count from source (thread_votes /
   thread_comments), and report what changed. Every ordinary vote/comment
   write already self-heals its own row (recompute-on-write, §4.4); this
   covers the residual case — a row that drifted (partial failure, manual DB
   fix, a bug) and never receives another vote/comment to trigger a repair.

2. --rekey-email --from <old_email> --to <new_email> — identity
   reconciliation (§3.6). Rewrites author_email / recipient_email / mentions
   entries on every row keyed on the old address. This is the scripted path
   that makes an email change (marriage, legal name change, the multi-domain
   migration §5.2 contemplates) survivable rather than unrecoverable.

   Rewritten 2026-08-19: this mode took an Airtable USERS record id and
   matched on the author_user_id/recipient_user_id "anchor" columns. Those
   columns are gone (plan §3.6) — the Postgres `users` table cannot carry the
   record id, and the anchor was redundant here regardless, since this
   operation already requires the old email and every affected row is
   reachable by it. Unlike the old version, this mode works today.

DRY RUN BY DEFAULT, same convention as scripts/migrate_hub_ac_propagation.py:
runs the real logic inside one transaction, then rolls back instead of
committing, so the printed report reflects the actual recompute rather than a
hand-simulated approximation. Pass --commit to keep the changes.

Run from renphil-hub-backend/, with the venv python:

    Dry run, counter repair:
        PYTHONPATH=. ./.venv/Scripts/python.exe scripts/repair_thread_counters.py

    Real run, counter repair:
        PYTHONPATH=. ./.venv/Scripts/python.exe scripts/repair_thread_counters.py --commit

    Dry run, identity reconciliation:
        PYTHONPATH=. ./.venv/Scripts/python.exe scripts/repair_thread_counters.py \\
            --rekey-email --from old@renphil.org --to new@renphil.org

    Real run, identity reconciliation:
        PYTHONPATH=. ./.venv/Scripts/python.exe scripts/repair_thread_counters.py \\
            --rekey-email --from old@renphil.org --to new@renphil.org --commit
"""

from __future__ import annotations

import argparse

from app.db_v2.database import SessionLocalV2
from app.services.thread_service import recompute_all_thread_counters, rekey_email


def _run_counter_repair(dry_run: bool) -> None:
    db = SessionLocalV2()
    try:
        changes = recompute_all_thread_counters(db)

        print("\n" + "=" * 70)
        print(f"Counter repair — {len(changes)} field(s) changed")
        print("=" * 70)
        for change in changes:
            target = (
                f"thread {change['thread_id']}"
                if "thread_id" in change
                else f"comment {change['comment_id']}"
            )
            print(f"  {target}.{change['field']}: {change['before']} -> {change['after']}")
        if not changes:
            print("  (nothing drifted)")

        if dry_run:
            db.rollback()
            print("\nThis was a DRY RUN — nothing was written. Re-run with --commit to apply.")
        else:
            db.commit()
            print("\nChanges committed.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _run_rekey(old_email: str, new_email: str, dry_run: bool) -> None:
    db = SessionLocalV2()
    try:
        counts = rekey_email(db, old_email=old_email, new_email=new_email)

        print("\n" + "=" * 70)
        print("Identity reconciliation")
        print(f"  {old_email} -> {new_email}")
        print("=" * 70)
        for key, value in counts.items():
            print(f"  {key}: {value}")

        if dry_run:
            db.rollback()
            print("\nThis was a DRY RUN — nothing was written. Re-run with --commit to apply.")
        else:
            db.commit()
            print("\nChanges committed.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write changes. Omit for a dry run (default) that runs the same "
        "logic but rolls back instead of committing.",
    )
    parser.add_argument(
        "--rekey-email",
        action="store_true",
        help="Switch to identity-reconciliation mode: rewrite every row keyed on "
        "--from so it is keyed on --to. Requires both.",
    )
    parser.add_argument("--from", dest="old_email", default=None, help="The user's old email.")
    parser.add_argument("--to", dest="new_email", default=None, help="The user's new email.")
    args = parser.parse_args()

    dry_run = not args.commit
    print("=" * 70)
    print(
        "DRY RUN — no writes will be kept (runs for real, then rolls back)"
        if dry_run
        else "COMMIT MODE — writes will be made"
    )
    print("=" * 70)

    if args.rekey_email:
        if not args.old_email or not args.new_email:
            parser.error("--rekey-email requires both --from and --to")
        _run_rekey(args.old_email, args.new_email, dry_run)
        return

    _run_counter_repair(dry_run)


if __name__ == "__main__":
    main()
