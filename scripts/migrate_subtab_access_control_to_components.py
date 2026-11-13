"""
One-time migration, Phase 1 of the sub-tab access-control relocation
(see session_handoff_2026-07-20.md for the full design + verified prod
counts this script's expectations are based on).

Goal: every Super GridStack sub-tab's access_control currently lives in
`gridstacks.settings["access_control"]`. It is moving onto the sub-tab's
own representation component (`components` row where
`current_grid_id == gridstack.id`, see ComponentV2.current_grid_id) so the
access-control system can read/write `components.access_control` uniformly
instead of splitting storage between `tabs` (roots/variants) and
`gridstacks.settings` (sub-tabs).

This script does two things, per the user's explicit scope decision:

  1. BACKFILL: create a representation component for every gridstack that
     doesn't already have one (root, variant, or sub-tab — 42/46 were
     missing one as of 2026-07-20, since current_grid_id population only
     started applying to newly-created gridstacks). Root/variant components
     get access_control=None (unchanged from _create_gridstack_component's
     normal behavior — they don't store AC here, they read from `tabs`).

  2. MIGRATE: for every NON-ROOT gridstack whose settings carries
     "access_control", copy that value onto its representation component's
     access_control column.

This is a COPY, not a MOVE: `gridstacks.settings["access_control"]` is left
untouched. The read/write code change (Phase 2, not part of this script)
will read the component first and fall back to settings, so this script's
ordering relative to the code deploy does not matter and there is no window
where a restriction silently reads as open. A later cleanup pass (Phase 4)
will strip the dead settings key once Phase 2 is deployed and verified.

Idempotent / safe to re-run: gridstacks that already have a correctly-set
component are left alone (reported as "already up to date"). Re-running
after a partial `--commit` run (e.g. interrupted) will only touch what
still needs it.

Safety:
  - DRY RUN BY DEFAULT. Prints every planned change and does not write
    anything unless invoked with --commit.
  - Single transaction: all writes for one run happen in one DB session,
    committed once at the end; any exception rolls back everything.
  - Never touches `gridstacks.settings`, `tabs.access_control`, or any
    component field other than access_control (for updates) / the normal
    _create_gridstack_component fields (for creates) — reuses that exact
    helper from gridstack_service.py rather than reimplementing it, so the
    created rows are byte-for-byte what the live code path would create.
  - Anomaly guard: if a gridstack is ever found with MORE than one
    representation component (shouldn't happen — verified 0 cases in prod
    on 2026-07-20), that gridstack is skipped and flagged, never guessed at.

Run from renphil-hub-backend/, with the venv python:
  Dry run (default, no writes):
    PYTHONPATH=. ./.venv/Scripts/python.exe scripts/migrate_subtab_access_control_to_components.py

  Real run (writes, single commit at the end):
    PYTHONPATH=. ./.venv/Scripts/python.exe scripts/migrate_subtab_access_control_to_components.py --commit
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

from app.services.gridstack_service import (
    GRIDSTACK_WIDGET_TYPE,
    SUPER_GRIDSTACK_WIDGET_TYPE,
    _create_gridstack_component,
)


def _predicted_type(gridstack: GridstackV2) -> str:
    """Mirrors _create_gridstack_component's own type derivation, for
    dry-run display only — never used to actually decide what gets
    written (the real helper is called for that in --commit mode)."""
    sgs = (gridstack.settings or {}).get("sgs")
    return SUPER_GRIDSTACK_WIDGET_TYPE if sgs else GRIDSTACK_WIDGET_TYPE


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
    created_with_ac: list[dict] = []
    created_without_ac: list[dict] = []
    updated_ac: list[dict] = []
    already_ok: list[dict] = []
    anomalies: list[dict] = []

    try:
        gridstacks = db.query(GridstackV2).order_by(GridstackV2.id).all()
        print(f"\nFound {len(gridstacks)} gridstack(s) total.\n")

        for gridstack in gridstacks:
            is_root = gridstack.parent_id is None
            settings = gridstack.settings or {}
            ac_in_settings = settings.get("access_control") if not is_root else None

            existing = (
                db.query(ComponentV2)
                .filter(ComponentV2.current_grid_id == gridstack.id)
                .all()
            )

            if len(existing) > 1:
                anomalies.append(
                    {
                        "grid_id": gridstack.id,
                        "name": gridstack.name,
                        "reason": f"{len(existing)} representation components found (expected 0 or 1)",
                        "component_ids": [c.id for c in existing],
                    }
                )
                continue

            if existing:
                component = existing[0]
                if ac_in_settings and component.access_control != ac_in_settings:
                    updated_ac.append(
                        {
                            "grid_id": gridstack.id,
                            "name": gridstack.name,
                            "component_id": component.id,
                            "old_ac": component.access_control,
                            "new_ac": ac_in_settings,
                        }
                    )
                    if not dry_run:
                        component.access_control = ac_in_settings
                else:
                    already_ok.append(
                        {
                            "grid_id": gridstack.id,
                            "name": gridstack.name,
                            "component_id": component.id,
                            "is_root": is_root,
                        }
                    )
            else:
                record = {
                    "grid_id": gridstack.id,
                    "name": gridstack.name,
                    "is_root": is_root,
                    "predicted_type": _predicted_type(gridstack),
                    "will_set_ac": bool(ac_in_settings),
                }
                if ac_in_settings:
                    created_with_ac.append(record)
                else:
                    created_without_ac.append(record)

                if not dry_run:
                    component = _create_gridstack_component(db, gridstack)
                    if ac_in_settings:
                        component.access_control = ac_in_settings
                    record["component_id"] = component.id

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
            "Representation components to CREATE, WITH access_control set (sub-tabs)"
            if dry_run else
            "Representation components CREATED, WITH access_control set (sub-tabs)",
            created_with_ac,
        )
        _print_group(
            "Representation components to CREATE, no access_control (root/variant/AC-less sub-tab)"
            if dry_run else
            "Representation components CREATED, no access_control (root/variant/AC-less sub-tab)",
            created_without_ac,
        )
        _print_group(
            "Existing components to UPDATE with access_control"
            if dry_run else
            "Existing components UPDATED with access_control",
            updated_ac,
        )
        _print_group("Already up to date (no action)", already_ok)
        _print_group("ANOMALIES — skipped, needs manual review", anomalies)

        print("\n" + "=" * 70)
        print(
            f"Summary: {len(created_with_ac)} create+AC, {len(created_without_ac)} create (no AC), "
            f"{len(updated_ac)} update AC, {len(already_ok)} already OK, {len(anomalies)} anomalies "
            f"skipped, out of {len(gridstacks)} gridstacks total."
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
