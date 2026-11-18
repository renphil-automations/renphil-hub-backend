"""
One-off repair: reconcile ComponentV2.gridstack_id across every Super Block
Note tree so it matches the tree's own root.

Root cause (fixed going forward in gridstack_service.py's
update_tab_content_v2): when an SBN root widget got re-parented into a
different gridstack (e.g. SgsCanvasHost migrating a normal canvas's content
into a freshly-created SGS sub-tab), only the root's own gridstack_id was
updated — its descendants (linked via super_blocknote_id) kept their old
gridstack_id. delete_tab_subtree_by_document_id_v2 finds an SBN tree to
delete by querying gridstack_id == gid, so a split tree causes a foreign key
violation on delete (children not found alongside their moved root).

This script only ever writes to ComponentV2.gridstack_id, wrapped in one
transaction, and is idempotent — safe to re-run.
"""

from app.db_v2.database import SessionLocalV2
from app.db_v2.models.component import ComponentV2
# Importing every db_v2 model registers them all on the shared declarative
# base before any query runs — ComponentV2's FK to page_contents otherwise
# fails to resolve (NoReferencedTableError) since that table's own model
# class is never imported by this script's own import graph.
from app.db_v2.models.tab import TabV2  # noqa: F401
from app.db_v2.models.gridstack import GridstackV2  # noqa: F401
from app.db_v2.models.page_content import PageContentV2  # noqa: F401

SBN_ROOT_TYPE = "super_block_note"


def cascade(db, component_id: int, new_gridstack_id: int, changed: list[dict]) -> None:
    children = db.query(ComponentV2).filter(ComponentV2.super_blocknote_id == component_id).all()
    for child in children:
        if child.gridstack_id != new_gridstack_id:
            changed.append(
                {
                    "id": child.id,
                    "link": child.link,
                    "title": child.title,
                    "old_gridstack_id": child.gridstack_id,
                    "new_gridstack_id": new_gridstack_id,
                }
            )
            child.gridstack_id = new_gridstack_id
        cascade(db, child.id, new_gridstack_id, changed)


def main() -> None:
    db = SessionLocalV2()
    changed: list[dict] = []
    try:
        roots = (
            db.query(ComponentV2)
            .filter(ComponentV2.type == SBN_ROOT_TYPE, ComponentV2.super_blocknote_id.is_(None))
            .all()
        )
        print(f"Found {len(roots)} Super Block Note root(s) to check.")
        for root in roots:
            cascade(db, root.id, root.gridstack_id, changed)

        if changed:
            db.commit()
        print(f"Reconciled {len(changed)} component(s):")
        for c in changed:
            print(
                f"  id={c['id']} link={c['link']} title={c['title']!r}: "
                f"gridstack_id {c['old_gridstack_id']} -> {c['new_gridstack_id']}"
            )
        if not changed:
            print("  (nothing to fix — every SBN tree was already consistent)")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
