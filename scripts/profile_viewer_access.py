"""
READ-ONLY profiler for `compute_visibility` (access_visibility_service.py) —
plan_ac_enforcement_closeout_2026-09-09.md §5.3's "measure first" instruction
for step 3 (Airtable component data). Times the fold against live Neon for a
real, non-admin `hub_user_id`, the same way scripts/profile_tab_workspace.py
already does for get_tab_workspace_v2. Decides Option A (reuse get_viewer_
access, moved off the loop) vs. Option B (a single-node `granted` fast path)
per that section: "if it is tens of milliseconds, ship A and stop."

Never writes. Only SELECTs against DATABASE_URL_V2.

Run (from renphil-hub-backend/, Windows venv — never a bare `python`):

    PYTHONPATH=. ./.venv/Scripts/python.exe scripts/profile_viewer_access.py [hub_user_id]

Omit `hub_user_id` to auto-pick the first non-admin hub_users row that holds
at least one role_assignments row (falls back to any hub_users row with an
assignment if every assigned user turns out to be admin).
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

# Import every model this walk touches so SQLAlchemy resolves the
# string-based FKs before any query is built (same requirement as every
# other script in this repo).
from app.db_v2.models.hub import HubV2  # noqa: F401
from app.db_v2.models.nav_tab import NavTabV2  # noqa: F401
from app.db_v2.models.tab import TabV2  # noqa: F401
from app.db_v2.models.gridstack import GridstackV2  # noqa: F401
from app.db_v2.models.component import ComponentV2  # noqa: F401
from app.db_v2.models.user import UserV2  # noqa: F401
from app.db_v2.models.role import RoleV2  # noqa: F401
from app.db_v2.models.scope import ScopeV2  # noqa: F401
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: F401
from app.db_v2.models.resource_grant import ResourceGrantV2  # noqa: F401

from app.dependencies import is_hub_admin, CurrentHubUser  # noqa: E402
from app.models.auth import UserInfo  # noqa: E402
from app.services.access_visibility_service import (  # noqa: E402
    compute_visibility,
    resolve_viewer_access,
)
from app.services.rbac_graph_service import RbacClosures  # noqa: E402


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
        key = " ".join(statement.split())
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


def _pick_hub_user_id(db) -> tuple[int, str, bool]:
    """A real (hub_user_id, email, is_admin) from live data — the first
    hub_users row with at least one role_assignments row, preferring a
    non-admin so the measurement reflects the common gated case (§5.3's
    own note: the admin short-circuit already skips the fold entirely, so
    profiling an admin would measure nothing new)."""
    closures = RbacClosures(db)
    rows = (
        db.query(UserV2)
        .join(RoleAssignmentV2, RoleAssignmentV2.user_id == UserV2.id)
        .distinct()
        .all()
    )
    if not rows:
        raise SystemExit("No hub_users row holds a role_assignments row — nothing to profile.")

    fallback = None
    for hub_user in rows:
        current = CurrentHubUser(
            info=UserInfo(email=hub_user.email, name=hub_user.name or "", roles=[]),
            hub_user_id=hub_user.id,
            email=hub_user.email,
        )
        admin = is_hub_admin(db, current, closures=closures)
        if not admin:
            return hub_user.id, hub_user.email, False
        if fallback is None:
            fallback = (hub_user.id, hub_user.email, True)
    return fallback


def measure(label: str, fn, profiler: QueryProfiler, show_table: bool = False):
    profiler.reset()
    db = SessionLocalV2()
    try:
        t0 = time.perf_counter()
        result = fn(db)
        dt = (time.perf_counter() - t0) * 1000
    finally:
        db.close()
    print(f"\n[{label}] {dt:8.1f} ms wall, {profiler.total:3d} queries")
    if show_table:
        profiler.report()
    return result


def main() -> None:
    profiler = QueryProfiler()
    event.listen(engine_v2, "before_cursor_execute", profiler.before)
    event.listen(engine_v2, "after_cursor_execute", profiler.after)
    try:
        db = SessionLocalV2()
        try:
            if len(sys.argv) > 1:
                hub_user_id = int(sys.argv[1])
                row = db.query(UserV2).filter(UserV2.id == hub_user_id).first()
                email = row.email if row else "?"
                is_admin = False
            else:
                hub_user_id, email, is_admin = _pick_hub_user_id(db)
        finally:
            db.close()

        print(f"Profiling hub_user_id = {hub_user_id}  ({email})  is_admin={is_admin}")

        # Warm the pool first (the first statement pays TLS/connect to Neon).
        measure("warmup (ignore)", lambda db: db.query(HubV2.id).all(), profiler)

        measure(
            "compute_visibility (cold RbacClosures + fresh NodeTree)",
            lambda db: compute_visibility(db, hub_user_id),
            profiler,
            show_table=True,
        )

        measure(
            "resolve_viewer_access (full get_viewer_access body, is_admin known)",
            lambda db: resolve_viewer_access(db, hub_user_id, is_admin=is_admin),
            profiler,
            show_table=True,
        )
    finally:
        event.remove(engine_v2, "before_cursor_execute", profiler.before)
        event.remove(engine_v2, "after_cursor_execute", profiler.after)


if __name__ == "__main__":
    main()
