"""
One-time migration, Phase 1 of the navigation-tabs feature (see
AI Docs/plan_nav_tabs_2026-07-28.md for the full design).

Goal: introduce the `nav_tabs` table (a new parent layer above root tabs)
and the nullable `tabs.nav_tab_id` FK that links every root TabV2 (and its
variants) to one. Then:

  1. CREATE the `nav_tabs` table if it doesn't already exist.
  2. ALTER `tabs` to add the `nav_tab_id` column + index, if missing.
  3. INSERT the Dashboard nav tab row if absent — slug='dashboard',
     protected=True. This is what makes today's unassigned roots become
     "the Dashboard nav tab" with no permanent special case.
  4. BACKFILL: every TabV2 (root or variant) with nav_tab_id IS NULL gets
     the Dashboard row's id. This is a flat UPDATE, not a walk — variants
     get the same treatment as roots, matching create_tab_variant_v2's
     "a variant carries its parent's nav_tab_id" rule for everything created
     from this point forward.
  5. Print before/after counts and flag any remaining NULLs (would indicate
     a row this script's queries didn't reach — should never happen).

Idempotent / safe to re-run: a second run finds the table/column already
present, the Dashboard row already inserted, and zero rows left to backfill
— reported as "already up to date".

Safety:
  - DRY RUN BY DEFAULT. Prints the plan and does not write anything unless
    invoked with --commit.
  - Single transaction: the Dashboard-row insert and the backfill UPDATE
    happen in one DB session, committed once at the end; any exception
    rolls back everything. The CREATE TABLE / ALTER TABLE DDL runs outside
    that transaction (Postgres DDL auto-commits per statement) but is itself
    idempotent (IF NOT EXISTS / checkfirst) and safe to re-run.

Run from renphil-hub-backend/, with the venv python:
  Dry run (default, no writes):
    PYTHONPATH=. ./.venv/Scripts/python.exe scripts/migrate_nav_tabs.py

  Real run (writes, single commit at the end):
    PYTHONPATH=. ./.venv/Scripts/python.exe scripts/migrate_nav_tabs.py --commit
"""

import argparse
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import text

from app.db_v2.database import BaseV2, SessionLocalV2, engine_v2
from app.db_v2.models.nav_tab import NavTabV2
# Importing every db_v2 model registers them all on the shared declarative
# base before create_all/queries run — same convention as
# migrate_subtab_access_control_to_components.py.
from app.db_v2.models.component import ComponentV2  # noqa: F401
from app.db_v2.models.gridstack import GridstackV2  # noqa: F401
from app.db_v2.models.page_content import PageContentV2  # noqa: F401
from app.db_v2.models.tab import TabV2  # noqa: F401

from app.services.tab_service import DEFAULT_ACCESS_CONTROL

DASHBOARD_SLUG = "dashboard"
DASHBOARD_TITLE = "Dashboard"


def _ensure_schema(dry_run: bool) -> tuple[bool, bool]:
    """Returns (nav_tabs_table_created, nav_tab_id_column_added)."""
    inspector_sql = text(
        "SELECT to_regclass('public.nav_tabs') IS NOT NULL AS table_exists"
    )
    with engine_v2.connect() as conn:
        table_exists = conn.execute(inspector_sql).scalar()
        column_exists = conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'tabs' AND column_name = 'nav_tab_id')"
            )
        ).scalar()

    if dry_run:
        return (not table_exists, not column_exists)

    BaseV2.metadata.create_all(engine_v2, tables=[NavTabV2.__table__], checkfirst=True)
    with engine_v2.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE tabs ADD COLUMN IF NOT EXISTS nav_tab_id INTEGER "
                "REFERENCES nav_tabs(id)"
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_tabs_nav_tab_id ON tabs (nav_tab_id)")
        )
    return (not table_exists, not column_exists)


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

    will_create_table, will_add_column = _ensure_schema(dry_run)
    print(f"\nnav_tabs table: {'to CREATE' if will_create_table else 'already exists'}")
    print(f"tabs.nav_tab_id column: {'to ADD' if will_add_column else 'already exists'}")

    if dry_run and will_create_table:
        print(
            "\nnav_tabs doesn't exist yet, so the Dashboard-row / backfill "
            "plan below can't be inspected until the table is created. "
            "Re-run with --commit, then dry-run again to see the rest of the plan."
        )
        print("=" * 70)
        return

    db = SessionLocalV2()
    try:
        dashboard = (
            db.query(NavTabV2).filter(NavTabV2.slug == DASHBOARD_SLUG).first()
        )
        will_insert_dashboard = dashboard is None

        if will_insert_dashboard:
            print(f"\nDashboard nav tab (slug={DASHBOARD_SLUG!r}): to INSERT")
            if not dry_run:
                now = datetime.now(timezone.utc)
                dashboard = NavTabV2(
                    document_id=str(uuid4()),
                    slug=DASHBOARD_SLUG,
                    title=DASHBOARD_TITLE,
                    order=0,
                    access_control=DEFAULT_ACCESS_CONTROL,
                    protected=True,
                    created_at=now,
                    updated_at=now,
                )
                db.add(dashboard)
                db.flush()
        else:
            print(
                f"\nDashboard nav tab (slug={DASHBOARD_SLUG!r}): already exists "
                f"(id={dashboard.id}, document_id={dashboard.document_id})"
            )

        total_tabs = db.query(TabV2).count()
        before_null = db.query(TabV2).filter(TabV2.nav_tab_id.is_(None)).count()
        print(f"\nTabs total: {total_tabs}")
        print(f"Tabs with nav_tab_id IS NULL (before backfill): {before_null}")

        if before_null > 0:
            if dry_run:
                print(
                    f"Would backfill {before_null} tab(s) -> nav_tab_id="
                    f"{dashboard.id if dashboard else '<new Dashboard row>'}"
                )
            else:
                dashboard_id = dashboard.id
                db.query(TabV2).filter(TabV2.nav_tab_id.is_(None)).update(
                    {TabV2.nav_tab_id: dashboard_id}, synchronize_session=False
                )
        else:
            print("Nothing to backfill.")

        if dry_run:
            print(f"Tabs with nav_tab_id IS NULL (dry run — unchanged): {before_null}")
        else:
            db.commit()
            after_null = db.query(TabV2).filter(TabV2.nav_tab_id.is_(None)).count()
            print(f"Tabs with nav_tab_id IS NULL (after backfill): {after_null}")

        if not dry_run and after_null > 0:
            stragglers = (
                db.query(TabV2.id, TabV2.document_id)
                .filter(TabV2.nav_tab_id.is_(None))
                .all()
            )
            print(f"\n⚠ {after_null} tab(s) still have nav_tab_id IS NULL — needs manual review:")
            for tab_id, document_id in stragglers:
                print(f"  id={tab_id} document_id={document_id}")

        print("\n" + "=" * 70)
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
