"""The four run steps (plan v2 R1, §8 steps 3–6), in their fixed order.

A step is `fn(ctx: RunContext) -> dict`:
  - it reads the run's snapshot from `ctx.snapshot` (all four tables, always
    complete, v2 R2) and the run's settings from `ctx.config`;
  - it logs each change with `ctx.log(entity, action, ...)`;
  - it commits with `ctx.commit()` (per person / per scope transactions),
    never `db.commit()` directly;
  - in a preview (`ctx.dry_run`) it computes everything but writes ONLY log
    rows; the runner's preview guard fails the run if it tries more;
  - it returns its summary counts, stored under `summary.steps[<step>]`.
A step whose prerequisite is missing waits and logs; it never fails (R3).

Phase 2: every step is a stub that only reports what it read. Phases 3–5
replace them (clusters + programs → 3, users → 4, program_roles → 5).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.airtable_sync.runner import RunContext


def clusters_step(ctx: RunContext) -> dict:
    return {"records": len(ctx.snapshot.clusters), "stub": True}


def programs_step(ctx: RunContext) -> dict:
    return {"records": len(ctx.snapshot.programs), "stub": True}


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
