"""
One-shot data migration: create the two ⊥ rows — the BOTTOM of each lattice —
in the database referenced by DATABASE_URL_V2 / PG_V2_* env vars. See
plan_access_control_algorithm_2026-08-27.md §4.4.

  roles  : key='any_role',  name='Any Role',  is_public=True
  scopes : key='any_scope', name='Any Scope', is_public=True

Owner-approved 2026-08-31 (both the decision to create them and the two
key/name pairs above).

WHAT ⊥ IS FOR, because the direction is the opposite of the intuitive one.
On the OBJECT side both axes run backwards (§4.3): a stored grant must sit AT
OR BELOW a pair the user holds, so the LOWER and NARROWER the stored pair, the
MORE people it reaches. `(Hub Member, All Scopes)` on a node is therefore the
NARROWEST scope available — only users whose own assignment is hub-wide — and
`(Any Role, Any Scope)` is the widest thing expressible. These two rows are
what make "open to everyone" a SINGLE grant row, independent of how anyone is
scoped: Alice on `(Program Member, A)` and Bob on `(Function Member, B)` share
no ordinary pair, and both match `(⊥role, ⊥scope)`.

WHY BOTH AXES AND NOT JUST SCOPES. `(Hub Member, ⊥scope)` reaches everyone at
Hub Member OR ABOVE. Roles below Hub Member are anticipated, and the day one is
added, every grant written as `(Hub Member, ⊥scope)` MEANING "everyone"
silently narrows — holders of the new junior role stop matching and nothing
errors. `(⊥role, ⊥scope)` never narrows. One extra row now against an
audit-and-rewrite later, on a trigger already expected.

────────────────────────────────────────────────────────────────────────────
THE FIVE §4.4 CONSTRAINTS, AND WHERE EACH IS ACTUALLY ENFORCED
────────────────────────────────────────────────────────────────────────────
This script states them and respects them, but it is NOT where any of them
lives — every one is enforced beneath it, which is the point. All five were
re-verified against the code and the live catalog on 2026-08-31 rather than
taken from the plan:

  1. `is_public = True`, AT MOST ONE PER TABLE.
     Enforced by the partial unique indexes `uq_roles_single_public` and
     `uq_scopes_single_public` (present in live Neon — confirmed by
     scripts/migrate_rbac_is_public.py reporting "already exists"). The
     WHERE clause is load-bearing: a plain unique index on a boolean permits
     one `true` row AND one `false` row, i.e. the second ordinary role
     anybody creates would fail. This script's own idempotence check is a
     convenience so a second run prints "nothing to do" instead of raising —
     it is not the guarantee.

  2. NOT (is_universal AND is_public).
     Enforced by the CHECK `ck_scopes_not_universal_and_public` (present in
     live Neon). A row that was both would return every scope (the
     is_universal short-circuit) AND be appended to every walk (the
     is_public union). This script writes `is_universal=False` explicitly
     rather than relying on the column default, so the intent is visible at
     the insert.

  3. NEVER AN ENDPOINT IN `parent_child_roles` / `parent_child_scopes`.
     Membership is implicit; an explicit edge is redundant at best and
     contradictory at worst. Enforced by `rbac_graph_service.validate_role_edge`
     (the `public_role_edge` guard) and `validate_scope_edge` (`public_scope_edge`).
     Both guards sit BEFORE their cycle check, which is required rather than
     stylistic: ⊥ is in every descendant set, so `parent in descendants(child)`
     is trivially true for any edge with ⊥ as parent and the cycle check would
     reject it first, blaming a cycle that does not exist.
     **This script writes NO edges. That is the whole of its compliance.**

  4. IMMUTABLE AFTER CREATION.
     Flipping the flag later silently rewrites what every existing grant
     reaches. Enforced by absence: `is_public` is not a field on the role or
     scope UPDATE schemas, which are `extra="forbid"`, so there is no request
     that can change it. Verified in app/schemas/rbac.py.
     Consequence worth stating before you run this with --commit: THE
     KEY/NAME PAIRS BELOW ARE EFFECTIVELY PERMANENT. `name` is renameable;
     the flag and the row's identity as ⊥ are not.

  5. NEVER VALID AS AN ASSIGNMENT.
     Enforced by `routers/rbac_assignments._assert_not_public`, which runs
     BEFORE `assert_can_delegate` in `create_assignment` — re-read on
     2026-08-31 and still in place, still first. The ordering is the check:
     ⊥ sits in every descendant set, so the delegation gate is precisely
     what a ⊥ target walks through, and running the refusal afterwards would
     be running it never.

`is_system` IS LEFT AT ITS DEFAULT (False), DELIBERATELY. It is inert today
and it has its own separate prompt (`prompt_admin_floor_2026-08-31.md`).
Setting it here would pre-empt a design decision that belongs to that work —
whether ⊥ counts as a system row is exactly the kind of thing that prompt is
for. If it should be True, that is a one-line follow-up there, not a guess
here.

────────────────────────────────────────────────────────────────────────────
WHAT RUNNING THIS CHANGES FOR USERS: NOTHING, TODAY.
Both `role_assignments` and `resource_grants` are empty, no endpoint reads the
`granted` / `visible` folds, and creating a principal nobody has written a
grant with grants nothing. What it unblocks is AUTHORING: until these rows
exist, §4.4's one-row "open to everyone" cannot be written in production at
all.

SAFE TO RUN MULTIPLE TIMES. Each row is probed by `key` first and only the
missing ones are inserted, so a partially-applied run converges rather than
erroring. Both inserts run in ONE transaction, so a failure part-way leaves
neither row behind — you never end up with a public role and no public scope.

It also refuses rather than "fixing" a conflict it did not create: an existing
row with the same `key` but `is_public = False`, or a DIFFERENT row already
holding `is_public` on that table, stops the run with an explanation. Both
would need a human decision, and silently updating either is precisely the
immutability violation constraint 4 exists to prevent.

POSTGRES ONLY, matching the other migration scripts. The test suite builds its
schema from the models via `create_all` on in-memory SQLite and never runs
this.

Run from renphil-hub-backend/, with the venv python:

  Dry run (default, no writes):
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.create_bottom_role_and_scope

  Real run (writes, against the live Neon renphil-hub-db — DATABASE_URL_V2):
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.create_bottom_role_and_scope --commit
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app.db_v2.database import SessionLocalV2, engine_v2
from app.db_v2.models.role import RoleV2
from app.db_v2.models.scope import ScopeV2

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROLE_KEY = "any_role"
ROLE_NAME = "Any Role"
ROLE_DESCRIPTION = (
    "The bottom of the role lattice. Every role implicitly inherits it, so a "
    "grant written on content with this role is reachable from whatever role "
    "anyone holds. Never assignable to a person."
)

SCOPE_KEY = "any_scope"
SCOPE_NAME = "Any Scope"
SCOPE_DESCRIPTION = (
    "The bottom of the scope lattice. Every scope implicitly contains it, so a "
    "grant written on content with this scope is reachable from whatever scope "
    "anyone holds. Never assignable to a person. Not to be confused with "
    "'All Scopes', which on an OBJECT grant is the most RESTRICTIVE scope "
    "available — only users whose own assignment is hub-wide."
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _plan_one(db, model, *, key: str, name: str, label: str) -> tuple[str, list[str]]:
    """Decide what to do about one ⊥ row.

    Returns `(status, problems)` where status is 'exists' | 'create', and a
    non-empty `problems` list means REFUSE — a conflicting row a human has to
    resolve, never something to overwrite.
    """
    problems: list[str] = []

    by_key = db.query(model).filter(model.key == key).one_or_none()
    by_name = db.query(model).filter(model.name == name).one_or_none()
    incumbent = db.query(model).filter(model.is_public.is_(True)).one_or_none()

    if by_key is not None:
        if not by_key.is_public:
            problems.append(
                f"{label}: a row already has key={key!r} (id={by_key.id}, "
                f"name={by_key.name!r}) but is_public is FALSE. `is_public` is "
                f"immutable by design (§4.4) and there is no endpoint that can "
                f"flip it, so this script will not either. Resolve by hand."
            )
        elif incumbent is not None and incumbent.id != by_key.id:
            problems.append(
                f"{label}: key={key!r} is public (id={by_key.id}) but so is "
                f"id={incumbent.id} ({incumbent.name!r}). Two public rows should "
                f"be impossible — the partial unique index prevents it. Check "
                f"the index exists."
            )
        return "exists", problems

    # No row with our key. Anything else already holding the flag, or the
    # name, is a conflict rather than a thing to work around.
    if incumbent is not None:
        problems.append(
            f"{label}: no row has key={key!r}, but id={incumbent.id} "
            f"({incumbent.key!r} / {incumbent.name!r}) already holds is_public. "
            f"At most one per table (§4.4). Creating another would violate the "
            f"partial unique index. Decide which row is ⊥ before re-running."
        )
    if by_name is not None:
        problems.append(
            f"{label}: name={name!r} is already taken by id={by_name.id} "
            f"(key={by_name.key!r}), and `name` is UNIQUE. Rename that row or "
            f"pick a different display name for ⊥."
        )
    return "create", problems


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the two ⊥ rows — 'Any Role' and 'Any Scope' (§4.4)."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write the rows. Omit for a dry run (default) that only prints the plan.",
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

    print("\nConstraints respected (all enforced BELOW this script — see the docstring):")
    print("  - is_public = True, at most one per table   -> partial unique indexes")
    print("  - NOT (is_universal AND is_public)          -> CHECK on scopes")
    print("  - never an endpoint in either edge table    -> this script writes NO edges")
    print("  - immutable after creation                  -> absent from the UPDATE schemas")
    print("  - never valid as an assignment              -> _assert_not_public, runs first")

    db = SessionLocalV2()
    try:
        with engine_v2.connect() as conn:
            for table in ("roles", "scopes"):
                if not engine_v2.dialect.has_table(conn, table):
                    print(f"  REFUSING: table {table!r} does not exist. Is this the right database?")
                    raise SystemExit(1)

        role_status, role_problems = _plan_one(
            db, RoleV2, key=ROLE_KEY, name=ROLE_NAME, label="roles"
        )
        scope_status, scope_problems = _plan_one(
            db, ScopeV2, key=SCOPE_KEY, name=SCOPE_NAME, label="scopes"
        )

        problems = role_problems + scope_problems
        if problems:
            print("\nREFUSING — a conflicting row needs a human decision:")
            for problem in problems:
                print(f"  * {problem}")
            raise SystemExit(1)

        print("\nPlan:")
        print(f"  - roles  {ROLE_KEY!r} / {ROLE_NAME!r}: "
              f"{'already exists' if role_status == 'exists' else 'will be created'}")
        print(f"  - scopes {SCOPE_KEY!r} / {SCOPE_NAME!r}: "
              f"{'already exists' if scope_status == 'exists' else 'will be created'}")

        if role_status == "exists" and scope_status == "exists":
            print("\nNothing to do — already up to date.")
            return

        if dry_run:
            print("\nWould insert:")
            if role_status == "create":
                print(f"    roles  (key={ROLE_KEY!r}, name={ROLE_NAME!r}, "
                      f"is_public=True, is_system=False, depth=NULL)")
            if scope_status == "create":
                print(f"    scopes (key={SCOPE_KEY!r}, name={SCOPE_NAME!r}, "
                      f"is_public=True, is_universal=False, is_system=False)")
            print("\n" + "=" * 74)
            print("This was a DRY RUN — nothing was written. Re-run with --commit to apply.")
            print("=" * 74)
            return

        now = _utc_now()
        # ONE transaction for both. A public role with no public scope is a
        # half-built bottom: `(⊥role, S)` is writable and `(⊥role, ⊥scope)` —
        # the row this whole feature exists for — is not, with nothing to
        # indicate why.
        if role_status == "create":
            db.add(
                RoleV2(
                    key=ROLE_KEY,
                    name=ROLE_NAME,
                    description=ROLE_DESCRIPTION,
                    # NULL, not 0. `depth` is inert metadata today (the rank
                    # ordering rule that read it is commented out), and a
                    # number here would be a claim about where ⊥ sits in an
                    # ordering nothing currently enforces.
                    depth=None,
                    is_public=True,
                    created_at=now,
                )
            )
        if scope_status == "create":
            db.add(
                ScopeV2(
                    key=SCOPE_KEY,
                    name=SCOPE_NAME,
                    description=SCOPE_DESCRIPTION,
                    # Explicit rather than defaulted: constraint 2 is the one
                    # a reader is most likely to doubt, and this is where the
                    # answer belongs.
                    is_universal=False,
                    is_public=True,
                    created_at=now,
                )
            )
        db.commit()

        role = db.query(RoleV2).filter(RoleV2.key == ROLE_KEY).one()
        scope = db.query(ScopeV2).filter(ScopeV2.key == SCOPE_KEY).one()
        print(f"\nVerified: roles  id={role.id} key={role.key!r} name={role.name!r} "
              f"is_public={role.is_public}")
        print(f"Verified: scopes id={scope.id} key={scope.key!r} name={scope.name!r} "
              f"is_public={scope.is_public} is_universal={scope.is_universal}")

        n_role_edges = _edge_count(db, "parent_child_roles", role.id)
        n_scope_edges = _edge_count(db, "parent_child_scopes", scope.id)
        print(f"Verified: edges referencing ⊥ role = {n_role_edges}, ⊥ scope = {n_scope_edges} "
              f"(both must be 0 — §4.4)")

        print("\n" + "=" * 74)
        print("Changes committed.")
        print("=" * 74)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _edge_count(db, table: str, node_id: int) -> int:
    """Belt-and-braces confirmation that constraint 3 held: no edge anywhere
    names the row we just created. The guards in `rbac_graph_service` cover
    the API, and a hand-written INSERT would not have gone through them."""
    from sqlalchemy import text

    parent_col, child_col = (
        ("parent_role_id", "child_role_id")
        if table == "parent_child_roles"
        else ("parent_scope_id", "child_scope_id")
    )
    return int(
        db.execute(
            text(
                f"SELECT count(*) FROM {table} "
                f" WHERE {parent_col} = :id OR {child_col} = :id"
            ),
            {"id": node_id},
        ).scalar()
        or 0
    )


if __name__ == "__main__":
    main()
