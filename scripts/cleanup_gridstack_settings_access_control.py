"""
Phase 4 (final) cleanup for the sub-tab access-control relocation — see
session_handoff_2026-07-20.md for the full design history.

Recap of the prior phases, both already committed and verified as of this
script's writing:
  - Phase 1 (migrate_subtab_access_control_to_components.py): copied every
    sub-tab's access_control from gridstacks.settings onto its own
    representation component (components.access_control, where
    current_grid_id == gridstack.id), backfilling a representation component
    for any gridstack that lacked one.
  - Phase 2 (gridstack_service.py): switched the read path
    (_safe_access_control_for_gridstack) and both write paths (create_tab_v2,
    update_tab_by_document_id_v2) over to components.access_control
    exclusively. gridstacks.settings["access_control"] has not been read or
    written by any live code path since this deployed.

This script is the cleanup: gridstacks.settings["access_control"] is now
dead weight. It removes that key from every gridstack's settings, leaving
any other settings keys (e.g. "sgs") untouched.

Safety check per row: only strips the key when the gridstack's own
representation component already carries a (truthy) access_control — i.e.
only once there is proof Phase 1 actually migrated that row. A gridstack
with the settings key but no matching component data is left alone and
reported as an ANOMALY instead of having its only copy of that data deleted.

Idempotent: a gridstack with no "access_control" key in settings is a no-op.

Safety:
  - DRY RUN BY DEFAULT. Prints every planned change; writes nothing unless
    invoked with --commit.
  - Single transaction: committed once at the end; any exception rolls back
    everything.
  - Never touches components.access_control, tabs.access_control, or any
    settings key other than "access_control" (e.g. "sgs" is preserved as-is).

Run from renphil-hub-backend/, with the venv python:
  Dry run (default, no writes):
    PYTHONPATH=. ./.venv/Scripts/python.exe scripts/cleanup_gridstack_settings_access_control.py

  Real run (writes, single commit at the end):
    PYTHONPATH=. ./.venv/Scripts/python.exe scripts/cleanup_gridstack_settings_access_control.py --commit
"""

import argparse

from app.db_v2.database import SessionLocalV2
from app.db_v2.models.component import ComponentV2
# Importing every db_v2 model registers them all on the shared declarative
# base before any query runs — ComponentV2's FK to page_contents otherwise
# fails to resolve (NoReferencedTableError) since that table's own model
# class is never imported by this script's own import graph.
from app.db_v2.models.tab import TabV2  # noqa: F401
from app.db_v2.models.gridstack import GridstackV2  # noqa: F401
from app.db_v2.models.page_content import PageContentV2  # noqa: F401


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

    db = SessionLocalV2()
    cleaned: list[dict] = []
    anomalies: list[dict] = []
    untouched_count = 0

    try:
        gridstacks = db.query(GridstackV2).order_by(GridstackV2.id).all()
        print(f"\nFound {len(gridstacks)} gridstack(s) total.\n")

        for gridstack in gridstacks:
            settings = gridstack.settings or {}
            if "access_control" not in settings:
                untouched_count += 1
                continue

            is_root = gridstack.parent_id is None
            component = (
                db.query(ComponentV2)
                .filter(ComponentV2.current_grid_id == gridstack.id)
                .first()
            )
            component_ac = component.access_control if component else None

            if not component_ac:
                anomalies.append(
                    {
                        "grid_id": gridstack.id,
                        "name": gridstack.name,
                        "is_root": is_root,
                        "reason": (
                            "no representation component"
                            if component is None
                            else "component.access_control is empty"
                        ),
                        "settings_ac": settings.get("access_control"),
                    }
                )
                continue

            cleaned.append(
                {
                    "grid_id": gridstack.id,
                    "name": gridstack.name,
                    "is_root": is_root,
                    "component_id": component.id,
                    "removed_ac": settings.get("access_control"),
                    "remaining_settings_keys": [k for k in settings.keys() if k != "access_control"],
                }
            )
            if not dry_run:
                new_settings = dict(settings)
                new_settings.pop("access_control", None)
                gridstack.settings = new_settings

        if not dry_run:
            db.commit()

        def _print_group(title: str, rows: list[dict]) -> None:
            print(f"\n--- {title} ({len(rows)}) ---")
            if not rows:
                print("  (none)")
                return
            for r in rows:
                print(f"  {r}")

        _print_group(
            "settings.access_control to REMOVE (component already has it)"
            if dry_run else
            "settings.access_control REMOVED (component already had it)",
            cleaned,
        )
        _print_group("ANOMALIES — settings key present but component unmigrated, skipped", anomalies)

        print("\n" + "=" * 70)
        print(
            f"Summary: {len(cleaned)} cleaned, {len(anomalies)} anomalies skipped, "
            f"{untouched_count} had no access_control key (already clean), "
            f"out of {len(gridstacks)} gridstacks total."
        )
        if anomalies:
            print(
                "ANOMALIES were left untouched — investigate before re-running; likely means "
                "Phase 1 (migrate_subtab_access_control_to_components.py) hasn't been run/committed "
                "for that row, or it was created after Phase 1 by code older than Phase 2."
            )
        if dry_run:
            print("This was a DRY RUN — nothing was written. Re-run with --commit to apply.")
        else:
            print("Changes committed.")
        print("=" * 70)

    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
