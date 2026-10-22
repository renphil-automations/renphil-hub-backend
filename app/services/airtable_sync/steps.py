"""The four run steps (plan v2 R1, §8 steps 3–6), in their fixed order.

A step is `fn(ctx: RunContext) -> dict`:
  - it reads the run's snapshot from `ctx.snapshot` (all four tables, always
    complete, v2 R2) and the run's settings from `ctx.config`;
  - it logs each change with `ctx.log(entity, action, ...)`;
  - it commits with `ctx.commit()` (per person / per scope transactions),
    never `db.commit()` directly;
  - in a preview (`ctx.dry_run`) it computes everything but writes ONLY log
    rows; the runner's preview guard fails the run if it tries more;
  - it raises and closes its items through `items.ItemBook`;
  - it may leave something for a later step of the same run in `ctx.state`;
  - it returns its summary counts, stored under `summary.steps[<step>]`.
A step whose prerequisite is missing waits and logs; it never fails (R3).

Phase 3: Clusters and Programs (scopes_step.py). Users and Program Roles are
still stubs that only report what they read (phases 4 and 5).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.airtable_sync.scopes_step import clusters_step, programs_step

if TYPE_CHECKING:
    from app.services.airtable_sync.runner import RunContext


def users_step(ctx: RunContext) -> dict:
    return {"records": len(ctx.snapshot.users), "stub": True}


def program_roles_step(ctx: RunContext) -> dict:
    return {"records": len(ctx.snapshot.program_roles), "stub": True}


# Keyed by models.airtable_sync.RUN_STEPS.
STEP_FUNCTIONS = {
    "clusters": clusters_step,
    "programs": programs_step,
    "users": users_step,
    "program_roles": program_roles_step,
}
