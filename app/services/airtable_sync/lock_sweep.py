"""Edit locks after the sync removes access (owner, 2026-10-23).

Locks and access are separate systems (edit_lock_service.py, "THE LOCK TREE
IS NOT THE ACCESS-CONTROL TREE"): the `/v2/rbac` revoke routes run
`edit_lock_service.release_locks_now_unauthorized` after their own commit,
so a holder who just lost `edit()` doesn't keep their lock until its TTL.
The sync's removals go through `delete_assignment` too, so they need the same
sweep:
  - the resolve and bulk-resolve endpoints, after their commit;
  - a real run, once at its end, when a step removed or replaced a sync row
    (`ctx.state[ACCESS_REMOVED]`). Never a preview.

Advisory, like every release path: a failure here never fails the
resolution or the run that triggered it.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ctx.state key: a step of this real run removed or replaced a sync row.
ACCESS_REMOVED = "access_removed"


def release_unauthorized_locks(db: Session) -> int | None:
    """Run the sweep (it commits its own releases). Returns how many locks it
    released, or None if it failed. The sweep is `async` only by its
    signature, so it runs on a fresh event loop: callers are the run's thread
    and FastAPI's threadpool, neither of which has a running loop."""
    from app.services import edit_lock_service

    try:
        released = asyncio.run(edit_lock_service.release_locks_now_unauthorized(db))
    except Exception:
        logger.warning("Airtable sync: the lock sweep after a removal failed", exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return None
    return len(released)
