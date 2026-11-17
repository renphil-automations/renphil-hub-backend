"""
READ-ONLY helper: lists root gridstacks by sub-tab (child) count and
top-level canvas widget count — to find a good document_id for
profile_tab_workspace.py's N+1 hypothesis (does query count scale with
#sub-tabs / #widgets, as opposed to da34ffe6's 0-sub-tab case). Never writes.

Run: PYTHONPATH=. ./.venv/Scripts/python.exe scripts/find_heavy_gridstack.py
"""

from app.db_v2.database import SessionLocalV2
from app.db_v2.models.tab import TabV2  # noqa: F401
from app.db_v2.models.gridstack import GridstackV2
from app.db_v2.models.component import ComponentV2


def main() -> None:
    db = SessionLocalV2()
    try:
        roots = db.query(GridstackV2).filter(GridstackV2.parent_id.is_(None)).all()
        counts = []
        for r in roots:
            child_count = (
                db.query(GridstackV2.id).filter(GridstackV2.parent_id == r.id).count()
            )
            widget_count = (
                db.query(ComponentV2.id)
                .filter(ComponentV2.gridstack_id == r.id, ComponentV2.super_blocknote_id.is_(None))
                .count()
            )
            counts.append((child_count, widget_count, r.document_id, r.name))
        counts.sort(key=lambda t: (t[0], t[1]), reverse=True)
        print(f"{'children':>8}  {'widgets':>7}  document_id                            name")
        print(f"{'-'*8}  {'-'*7}  {'-'*36}  {'-'*30}")
        for child_count, widget_count, doc_id, name in counts[:15]:
            print(f"{child_count:>8}  {widget_count:>7}  {doc_id}  {name}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
