"""
READ-ONLY survey: the Hub Admin PARITY REPORT for
plan_access_control_algorithm_2026-08-27.md §10 item 6, phase 2 ("parallel
run") — session_handoff_2026-09-03-hub-admin-resolver.md §0.2.

    Who is Hub Admin by AIRTABLE (what a fresh JWT would carry) versus by
    `role_assignments` (what `app.dependencies.is_hub_admin`'s new branch
    resolves)? Every divergence is either a MISSING ASSIGNMENT (admin by
    Airtable, no assignment yet — expected and harmless during population)
    or an UNEXPECTED ADMIN (admin by assignment, not by Airtable — needs
    answering: is this deliberate, or a stale/incorrect grant?). §10 says
    both need answering before phase 3 (assignments-only) can happen.

────────────────────────────────────────────────────────────────────────────
§0.2's DATA SOURCE DECISION (owner-confirmed 2026-09-03)
────────────────────────────────────────────────────────────────────────────
ONE bulk Airtable read — `AirtableService.list_access_control_records()` —
rather than one `is_hub_admin(email)` call per candidate. This sidesteps the
`users`-table-vs-`hub_users`-table "candidate population" question the
original task brief raised entirely: there is no population to check
membership against, because the bulk read already IS the complete set of
Airtable Hub Admins. Records are grouped by (normalized) email and their role
names UNIONED per email — mirroring `AirtableService.get_user_roles`'s own
logic exactly, because that is what actually runs at JWT-mint time, and a
report that disagreed with it would be reporting on a different mechanism
than the one this task changed.

────────────────────────────────────────────────────────────────────────────
§0.1's DECISION, REUSED HERE (owner-confirmed 2026-09-03)
────────────────────────────────────────────────────────────────────────────
The "by assignments" column uses CLOSURE membership — the exact same
`effective_pairs` computation `app.dependencies.is_hub_admin` uses for its
second `OR` branch, restricted to that branch alone (this report is not
asking "would this person's JWT admit them", it is asking "does the
assignments SIDE alone admit them" — the two are deliberately kept apart so
a divergence report of "JWT says X, assignments say Y" cannot be muddied by
one column secretly containing information from the other). See that
function's docstring for the full reasoning on why closure, not a literal
row.

Emits NO WRITES of any kind — one Airtable list call, and SELECT-only
queries against DATABASE_URL_V2. Touches no `role_assignments` row, creates
nothing. Safe against both the live Neon DB and the live Airtable base.

Run:
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.survey_hub_admin_parity
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Same reasoning as scripts/survey_widget_exposure_delta.py and
# scripts/create_bottom_role_and_scope.py: this report uses non-ASCII
# section marks, and a Windows console defaults to cp1252, which cannot
# encode them. Forcing UTF-8 here makes the encoding a property of the
# script, not of whichever terminal happens to run it.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import get_settings  # noqa: E402
from app.db_v2.database import SessionLocalV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.db_v2.models.role import RoleV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.db_v2.models.scope import ScopeV2  # noqa: E402
from app.services.airtable_service import AirtableService  # noqa: E402
from app.services.rbac_graph_service import RbacClosures, effective_pairs  # noqa: E402

HUB_ADMIN_ROLE_KEY = "hub_admin"  # roles.key — see app.dependencies.is_hub_admin
HUB_ADMIN_JWT_ROLE_NAME = "Hub Admin"  # Airtable display name — tab_service.HUB_ADMIN_ROLE


async def _airtable_admin_emails(settings) -> tuple[set[str], dict[str, list[str]]]:
    """`(admin_emails, roles_by_email)` from ONE bulk Airtable read.

    Groups by normalized email and unions role names per email, exactly as
    `AirtableService.get_user_roles` does per-email at JWT-mint time — the
    de-dup-while-preserving-order there does not matter for a set-membership
    question, but the UNION ACROSS RECORDS for one email does: a person with
    more than one Access Control row (e.g. two programs) must not be
    under-counted.
    """
    airtable = AirtableService(settings)
    records = await airtable.list_access_control_records()

    roles_by_email: dict[str, list[str]] = {}
    for rec in records:
        if not rec.user_email:
            continue  # unattached AC rows exist; not a person to report on
        email = rec.user_email.strip().lower()
        seen = roles_by_email.setdefault(email, [])
        for role in rec.roles:
            if role.name and role.name not in seen:
                seen.append(role.name)

    admin_emails = {
        email for email, roles in roles_by_email.items() if HUB_ADMIN_JWT_ROLE_NAME in roles
    }
    return admin_emails, roles_by_email


def _assignment_admin_emails(db) -> tuple[set[str], dict[str, list[tuple[str, str]]]]:
    """`(admin_emails, reaching_pairs_by_email)` from `role_assignments`
    alone, via CLOSURE membership (§0.1) — deliberately NOT via
    `app.dependencies.is_hub_admin`, which also consults the JWT. Calling
    that function here would let a JWT-side signal leak into what is meant
    to be the pure assignments-side column.
    """
    hub_admin_role_row = db.query(RoleV2.id).filter(RoleV2.key == HUB_ADMIN_ROLE_KEY).first()
    universal_scope_ids = {
        row[0] for row in db.query(ScopeV2.id).filter(ScopeV2.is_universal.is_(True)).all()
    }
    if hub_admin_role_row is None or not universal_scope_ids:
        return set(), {}
    hub_admin_role_id = hub_admin_role_row[0]

    # Names for the "reaching pairs" detail column, not used in the
    # membership test itself.
    role_names = {row[0]: row[1] for row in db.query(RoleV2.id, RoleV2.name).all()}
    scope_names = {row[0]: row[1] for row in db.query(ScopeV2.id, ScopeV2.name).all()}

    closures = RbacClosures(db)  # one snapshot, shared across every user below
    admin_emails: set[str] = set()
    reaching_pairs: dict[str, list[tuple[str, str]]] = {}

    hub_users_with_assignments = (
        db.query(UserV2)
        .join(RoleAssignmentV2, RoleAssignmentV2.user_id == UserV2.id)
        .distinct()
        .all()
    )
    for hub_user in hub_users_with_assignments:
        pairs = effective_pairs(db, hub_user.id, closures=closures)
        if any((hub_admin_role_id, sid) in pairs for sid in universal_scope_ids):
            email = hub_user.email.strip().lower()
            admin_emails.add(email)
            # The RAW rows this person holds (not the expanded closure) —
            # what actually explains the "why" in the report.
            raw = (
                db.query(RoleAssignmentV2.role_id, RoleAssignmentV2.scope_id)
                .filter(RoleAssignmentV2.user_id == hub_user.id)
                .all()
            )
            reaching_pairs[email] = [
                (role_names.get(r, f"role#{r}"), scope_names.get(s, f"scope#{s}")) for r, s in raw
            ]
    return admin_emails, reaching_pairs


def _print_section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


async def main() -> None:
    settings = get_settings()
    db = SessionLocalV2()
    try:
        _print_section("HUB ADMIN PARITY REPORT (read-only)")
        print("Airtable side: ONE bulk list_access_control_records() call.")
        print("Assignments side: role_assignments, CLOSURE membership (§0.1).")

        airtable_admins, airtable_roles = await _airtable_admin_emails(settings)
        assignment_admins, assignment_pairs = _assignment_admin_emails(db)

        _print_section(f"Admin by AIRTABLE: {len(airtable_admins)}")
        for email in sorted(airtable_admins):
            print(f"  - {email}  (roles: {', '.join(airtable_roles.get(email, []))})")

        _print_section(f"Admin by ASSIGNMENTS (closure): {len(assignment_admins)}")
        for email in sorted(assignment_admins):
            pairs = ", ".join(f"({r}, {s})" for r, s in assignment_pairs.get(email, []))
            print(f"  - {email}  (reaching via: {pairs})")

        missing_assignment = airtable_admins - assignment_admins
        unexpected_admin = assignment_admins - airtable_admins
        agree = airtable_admins & assignment_admins

        _print_section(f"AGREE on both sides: {len(agree)}")
        for email in sorted(agree):
            print(f"  - {email}")

        _print_section(
            f"MISSING ASSIGNMENT — admin by Airtable, no assignment yet: {len(missing_assignment)}"
        )
        print("(Expected during population — each of these needs a")
        print(" (hub_admin, a universal scope) assignment written before phase 3.)")
        for email in sorted(missing_assignment):
            print(f"  - {email}")

        _print_section(
            f"UNEXPECTED ADMIN — admin by assignment, NOT by Airtable: {len(unexpected_admin)}"
        )
        print("(Needs a human answer: deliberate (e.g. a role added above hub_admin,")
        print(" §0.1), or a stale/incorrect assignment that should be revoked?)")
        for email in sorted(unexpected_admin):
            pairs = ", ".join(f"({r}, {s})" for r, s in assignment_pairs.get(email, []))
            print(f"  - {email}  (reaching via: {pairs})")

        _print_section("Summary")
        print(f"  Airtable admins:     {len(airtable_admins)}")
        print(f"  Assignment admins:   {len(assignment_admins)}")
        print(f"  Agree:               {len(agree)}")
        print(f"  Missing assignment:  {len(missing_assignment)}")
        print(f"  Unexpected admin:    {len(unexpected_admin)}")
        if not missing_assignment and not unexpected_admin:
            print("\n  CLEAN RUN — both sides agree completely.")
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
