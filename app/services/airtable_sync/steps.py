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

Phase 3: Clusters and Programs (scopes_step.py). Phase 4: Users without HR
fields (users_step.py). Phase 5: Program Roles (program_roles_step.py).
"""

from __future__ import annotations

from app.services.airtable_sync.program_roles_step import program_roles_step
from app.services.airtable_sync.scopes_step import clusters_step, programs_step
from app.services.airtable_sync.users_step import users_step

# Keyed by models.airtable_sync.RUN_STEPS.
STEP_FUNCTIONS = {
    "clusters": clusters_step,
    "programs": programs_step,
    "users": users_step,
    "program_roles": program_roles_step,
}
