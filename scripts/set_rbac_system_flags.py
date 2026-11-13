"""
One-shot DATA migration: set `is_system = true` on the RBAC rows the
application cannot function without, in the database referenced by
DATABASE_URL_V2 / PG_V2_* env vars. Closes audit finding §2.1
(session_handoff_2026-08-25-rank-to-depth.md).

  roles  : key = 'hub_admin'   -> is_system = true
  scopes : is_universal = true -> is_system = true   (in practice 'all_scopes')

Owner-approved 2026-08-31, including the decision about WHICH rows: exactly
these two and no others. See "WHAT IS DELIBERATELY NOT MARKED" below.

────────────────────────────────────────────────────────────────────────────
WHY THE FLAG IS INERT TODAY, AND WHAT SETTING IT BUYS
────────────────────────────────────────────────────────────────────────────
`is_system` is a delete-guard, and it has been enforced since it was written:
`rbac_service.delete_role` raises `system_role` and `delete_scope` raises
`system_scope` before doing anything else. What has never existed is a way to
SET it — `create_role` and `create_scope` both hardcode `is_system=False`, and
the field is absent from all four request schemas. So the guard has protected
nothing since the day it landed, and `hub_admin` has been deletable through
the Access Management UI the whole time.

Verified on the live database on 2026-08-31 before writing this: all nine
roles and all three scopes have `is_system = false`.

That is recoverable TODAY only because `require_hub_admin` still reads
Airtable JWT role names rather than these tables. At the §10 item 6 cutover it
becomes an unrecoverable lockout, which is why this finding is the one with a
deadline on it.

────────────────────────────────────────────────────────────────────────────
⚠ THIS DOES NOT REPLACE `BOOTSTRAP_ADMIN_EMAILS` (plan §6.5, condition 3)
────────────────────────────────────────────────────────────────────────────
Read this before deciding §6.5 is satisfied. It is not, and running this
script does not make it so.

`is_system` protects the role DEFINITION row. The lockout comes from the
ASSIGNMENT row. Post-cutover `is_hub_admin` resolves from `role_assignments`
(§6.8), so if no row names `(hub_admin, universal_scope)` then nobody is a Hub
Admin — and an undeletable `hub_admin` role changes nothing about that.
Recovery through `can_delegate` is impossible by construction: it needs a role
strictly ABOVE `hub_admin`, and nothing is above it.

That is not hypothetical. **`role_assignments` has 0 rows as of 2026-08-31.**

The companion protection on the assignment side is the last-admin guard in
`rbac_assignment_service._assert_not_the_last_hub_admin`. Between them they
cover the two ordinary delete paths — and neither survives a bad migration, a
restore to the wrong snapshot, an empty table, or direct database access.
§6.5 specifies a settings-level `BOOTSTRAP_ADMIN_EMAILS` list as PERMANENT
precisely because it lives outside the database. It stays required.

────────────────────────────────────────────────────────────────────────────
WHAT IS DELIBERATELY NOT MARKED
────────────────────────────────────────────────────────────────────────────
  - THE TWO ⊥ ROWS (`any_role` / `any_scope`, created by
    `scripts/create_bottom_role_and_scope.py` and present in live Neon as of
    2026-08-31). §4.4 calls them immutable after creation, which is about the
    FLAG rather than the row's existence, and the owner chose not to extend
    `is_system` to them. They are not defenceless: `resource_grants.role_id`
    and `.scope_id` are ON DELETE RESTRICT, so once a single "open to
    everyone" grant exists, neither ⊥ row can be deleted. The window is while
    `resource_grants` is empty — which is now.

  - `hub_member`. Nothing in the code keys on it, §11.1 has not settled
    whether it is auto-assigned at first login, and marking a row undeletable
    is much harder to undo than to do (see the next section). It stays an
    ordinary role the org may restructure.

  - EVERY OTHER SCOPE. Only `is_universal` rows are marked, per §2.1's
    suggested fix. `ai_for_math` and anything created after it are ordinary
    content scopes.

────────────────────────────────────────────────────────────────────────────
THIS IS EFFECTIVELY IRREVERSIBLE THROUGH THE APPLICATION
────────────────────────────────────────────────────────────────────────────
`update_role` writes only name/description/depth and `update_scope` only
name/description, so once `is_system` is true there is NO API path that can
clear it — and that omission is the point, not a bug (owner decision
2026-08-31: the flag stays script-only). It is what makes the protection
durable, and `tests/test_admin_floor.TestIsSystemCannotBeCleared` pins it.

The corollary is that a row marked here can only be unmarked by hand, in SQL.
Marking the wrong row produces a permanently undeletable role. That is the
reason this script names its targets explicitly rather than taking them as
arguments.

SAFE TO RUN MULTIPLE TIMES. Each target is probed first and only the rows that
need changing are updated, so a second run reports "already up to date". Both
updates run in ONE transaction.

It REFUSES rather than guessing when the shape is wrong: no `hub_admin` role,
or no universal scope. Either means this is not the database the script was
written for, and marking whatever happens to be there instead is exactly the
mistake the section above says is hard to undo.

POSTGRES ONLY, matching the other migration scripts. The test suite builds its
schema from the models via `create_all` on in-memory SQLite and never runs
this.

Run from renphil-hub-backend/, with the venv python:

  Dry run (default, no writes):
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.set_rbac_system_flags

  Real run (writes, against the live Neon renphil-hub-db — DATABASE_URL_V2):
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.set_rbac_system_flags --commit
"""

from __future__ import annotations

import argparse
import sys

from app.db_v2.database import SessionLocalV2, engine_v2
from app.db_v2.models.role import RoleV2
from app.db_v2.models.scope import ScopeV2

if hasattr(sys.stdout, "reconfigure"):
    # Same reason scripts/create_bottom_role_and_scope.py does it: the output
    # uses the design docs' typographic characters, a Windows console is
    # cp1252, and the failure mode is a UnicodeEncodeError part-way through —
    # after the caller has already seen a screenful of plausible output.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HUB_ADMIN_ROLE_KEY = "hub_admin"


def _plan(db) -> tuple[list[tuple[str, object]], list[str], list[str]]:
    """Decide what to change.

    Returns `(to_update, lines, problems)`. A non-empty `problems` means
    REFUSE — the database is not the shape this script was written for, and
    marking something else instead is not a recovery.
    """
    to_update: list[tuple[str, object]] = []
    lines: list[str] = []
    problems: list[str] = []

    role = db.query(RoleV2).filter(RoleV2.key == HUB_ADMIN_ROLE_KEY).one_or_none()
    if role is None:
        problems.append(
            f"roles: no row with key={HUB_ADMIN_ROLE_KEY!r}. That role is both the "
            f"admin gate and the root every delegated grant descends from "
            f"(plan §6), so its absence is not something to work around. Create "
            f"it first, or check this is the right database."
        )
    else:
        if role.is_system:
            lines.append(f"  - roles  id={role.id} {role.key!r}: already is_system")
        else:
            lines.append(f"  - roles  id={role.id} {role.key!r}: will be marked is_system")
            to_update.append(("role", role))

    universal = db.query(ScopeV2).filter(ScopeV2.is_universal.is_(True)).all()
    if not universal:
        problems.append(
            "scopes: no row has is_universal. §6.5 condition 1 requires Hub "
            "Admins to be assigned on the universal scope, so a database "
            "without one cannot express an admin at all. Check this is the "
            "right database."
        )
    for scope in universal:
        if scope.is_system:
            lines.append(f"  - scopes id={scope.id} {scope.key!r}: already is_system")
        else:
            lines.append(f"  - scopes id={scope.id} {scope.key!r}: will be marked is_system")
            to_update.append(("scope", scope))

    return to_update, lines, problems


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Set is_system on the hub_admin role and the universal scope (§2.1)."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write changes. Omit for a dry run (default) that only prints the plan.",
    )
    args = parser.parse_args()
    dry_run = not args.commit

    print("=" * 74)
    print("DRY RUN — no writes will be made" if dry_run else "COMMIT MODE — writes will be made")
    print("=" * 74)
    print(f"Connecting to: {engine_v2.url.render_as_string(hide_password=True)}")

    if engine_v2.dialect.name != "postgresql":
        print(f"  REFUSING: dialect is {engine_v2.dialect.name!r}, not 'postgresql'.")
        raise SystemExit(1)

    db = SessionLocalV2()
    try:
        with engine_v2.connect() as conn:
            for table in ("roles", "scopes"):
                if not engine_v2.dialect.has_table(conn, table):
                    print(f"  REFUSING: table {table!r} does not exist. Is this the right database?")
                    raise SystemExit(1)

        to_update, lines, problems = _plan(db)

        if problems:
            print("\nREFUSING — the database is not the shape this script expects:")
            for problem in problems:
                print(f"  * {problem}")
            raise SystemExit(1)

        print("\nPlan:")
        for line in lines:
            print(line)

        print("\nNOT marked, deliberately (see the module docstring):")
        print("  - the two ⊥ rows 'any_role' / 'any_scope' — owner decision")
        print("  - 'hub_member' — §11.1 has not settled its role")
        print("  - every non-universal scope")

        if not to_update:
            print("\nNothing to do — already up to date.")
            return

        print(
            "\n⚠ REMINDER: is_system cannot be cleared through the API "
            "(update_role/update_scope\n"
            "  do not write it). Undoing this needs hand-written SQL."
        )
        print(
            "⚠ REMINDER: this protects the role/scope DEFINITION rows only. It does "
            "NOT make\n  §6.5's BOOTSTRAP_ADMIN_EMAILS unnecessary — the lockout "
            "comes from the\n  ASSIGNMENT side, and role_assignments is empty."
        )

        if dry_run:
            print("\nWould run:")
            for kind, row in to_update:
                table = "roles" if kind == "role" else "scopes"
                print(f"    UPDATE {table} SET is_system = true WHERE id = {row.id};")
            print("\n" + "=" * 74)
            print("This was a DRY RUN — nothing was written. Re-run with --commit to apply.")
            print("=" * 74)
            return

        # One transaction for both. A marked role beside an unmarked universal
        # scope is a half-built floor, and the scope is the half §6.5
        # condition 1 depends on.
        for _kind, row in to_update:
            row.is_system = True
        db.commit()

        role = db.query(RoleV2).filter(RoleV2.key == HUB_ADMIN_ROLE_KEY).one()
        print(f"\nVerified: roles  id={role.id} key={role.key!r} is_system={role.is_system}")
        for scope in db.query(ScopeV2).filter(ScopeV2.is_universal.is_(True)).all():
            print(
                f"Verified: scopes id={scope.id} key={scope.key!r} "
                f"is_system={scope.is_system}"
            )

        print("\n" + "=" * 74)
        print("Changes committed.")
        print("=" * 74)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
