"""
READ-ONLY profiler for get_tab_workspace_v2 / get_tab_children_v2.

Counts and times every SQL statement each one issues against DATABASE_URL_V2
(live Neon), grouped by statement, to confirm and quantify the N+1 pattern
behind a slow /workspace load. It only calls the read functions — it never
writes, commits, or mutates anything.

Run (from renphil-hub-backend/, Windows venv — never a bare `python`):

    PYTHONPATH=. ./.venv/Scripts/python.exe scripts/profile_tab_workspace.py <document_id>

<document_id> defaults to the host tab we've been profiling. Pass the
document_id of whichever slow tab you want to measure.

What to read off the output:
  * "[get_tab_workspace_v2] N ms wall, K queries" — K is the round-trip count.
    If K scales with (#sub-tabs) and (#canvas widgets), that's the N+1.
  * The per-statement table (sorted by total time) shows which query is run
    the most times — those are the batching targets.
  * "[get_tab_children_v2]" is the lighter list-only path the frontend now
    uses; compare its query count to the workspace one.
"""

import sys
import time
from collections import defaultdict

try:  # match how the app resolves DATABASE_URL_V2
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001 — dotenv is optional; env may already be set
    pass

from sqlalchemy import event

from app.db_v2.database import SessionLocalV2, engine_v2

# Import all four models so SQLAlchemy resolves the string-based FKs before any
# query is built (same requirement as every other script in this repo).
from app.db_v2.models.tab import TabV2  # noqa: F401
from app.db_v2.models.gridstack import GridstackV2  # noqa: F401
from app.db_v2.models.component import ComponentV2  # noqa: F401
from app.db_v2.models.page_content import PageContentV2  # noqa: F401
from app.services.gridstack_service import (  # noqa: E402
    get_tab_children_v2,
    get_tab_workspace_v2,
)

DEFAULT_DOCUMENT_ID = "da34ffe6-7f14-419a-96e3-c2fc81465204"


class QueryProfiler:
    def __init__(self) -> None:
        self.stats: dict[str, dict[str, float]] = defaultdict(
            lambda: {"count": 0, "total_ms": 0.0}
        )
        self.total = 0

    def reset(self) -> None:
        self.stats.clear()
        self.total = 0

    def before(self, conn, cursor, statement, params, context, executemany):
        context._prof_t0 = time.perf_counter()

    def after(self, conn, cursor, statement, params, context, executemany):
        dt = (time.perf_counter() - getattr(context, "_prof_t0", time.perf_counter())) * 1000
        key = " ".join(statement.split())  # collapse whitespace so like-queries group
        s = self.stats[key]
        s["count"] += 1
        s["total_ms"] += dt
        self.total += 1

    def report(self) -> None:
        rows = sorted(self.stats.items(), key=lambda kv: kv[1]["total_ms"], reverse=True)
        print(f"\n  {'cnt':>4}  {'total_ms':>9}  {'avg_ms':>7}  statement")
        print(f"  {'-'*4}  {'-'*9}  {'-'*7}  {'-'*80}")
        for stmt, s in rows:
            avg = s["total_ms"] / s["count"] if s["count"] else 0.0
            print(f"  {int(s['count']):>4}  {s['total_ms']:>9.1f}  {avg:>7.1f}  {stmt[:90]}")


def measure(label: str, fn, profiler: QueryProfiler, show_table: bool = False):
    """Run `fn(db)` in a FRESH session (empty identity map, so the query count
    reflects a cold load), timing the whole call and every SQL statement."""
    profiler.reset()
    db = SessionLocalV2()
    try:
        t0 = time.perf_counter()
        result = fn(db)
        dt = (time.perf_counter() - t0) * 1000
    finally:
        db.close()
    status = "None (not found)" if result is None else "ok"
    print(f"\n[{label}] {dt:8.1f} ms wall, {profiler.total:3d} queries  ->  {status}")
    if show_table:
        profiler.report()
    return result


def main() -> None:
    document_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DOCUMENT_ID
    profiler = QueryProfiler()
    event.listen(engine_v2, "before_cursor_execute", profiler.before)
    event.listen(engine_v2, "after_cursor_execute", profiler.after)
    try:
        print(f"Profiling document_id = {document_id}")
        # Warm the pool first (the very first statement pays TLS/connect to Neon,
        # which would otherwise be blamed on whichever call ran first).
        measure("warmup (ignore)", lambda db: get_tab_children_v2(db, document_id), profiler)

        measure("get_tab_workspace_v2", lambda db: get_tab_workspace_v2(db, document_id),
                profiler, show_table=True)
        measure("get_tab_children_v2", lambda db: get_tab_children_v2(db, document_id),
                profiler, show_table=True)
    finally:
        event.remove(engine_v2, "before_cursor_execute", profiler.before)
        event.remove(engine_v2, "after_cursor_execute", profiler.after)


if __name__ == "__main__":
    main()
