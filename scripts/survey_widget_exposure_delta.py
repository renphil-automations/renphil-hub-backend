"""
READ-ONLY survey: **the widget exposure delta** — the cutover safety check for
`AI Docs/plan_access_control_algorithm_2026-08-27.md` §10.

It answers one question:

    Which widgets are gated server-side today, who can see them, and what
    would change if that gate stopped being consulted?

This is NOT an authoring estimate. Per-widget `access_control` is the one half
of the old model that is genuinely enforced on the server, so whatever
eventually replaces it changes what real people see. This script enumerates
that blast radius so the change can be made deliberately.

WHY IT CALLS THE REAL PREDICATE. Every "who can see this" answer below comes
from `tab_service._user_can_view_widget` itself, imported and invoked, never
from a reimplementation of its rules. A reimplementation that disagreed with
production would UNDERSTATE exposure — the dangerous direction — and the rules
are counterintuitive enough that disagreeing is the likely outcome:

  1. Only `viewers` is ever read. `admins` is never consulted by the view
     predicate, so an AC with a populated `admins` and an empty `viewers`
     gates NOTHING.
  2. An empty `viewers` group means OPEN TO EVERYONE, not closed.
  3. Roles scoped "Program" or "Function" are SKIPPED (`_SCOPED_ROLE_SCOPES`)
     because they cannot be checked without an Airtable round trip — so such
     an entry matches NOBODY. It reads like a grant and behaves like a
     comment.

THE HUB ADMIN BYPASS IS UPSTREAM OF THE PREDICATE and is therefore reported
as its own column rather than folded into the persona sweep.
`_user_can_view_widget` knows nothing about it; `filter_widget_content_for_user`
returns early for Hub Admins BEFORE calling it. Simulating a Hub Admin through
the predicate alone would show them as RESTRICTED on some widgets, which is the
exact opposite of the truth.

`automations@renphil.org` IS REPORTED SEPARATELY. It appears in every
`DEFAULT_ACCESS_CONTROL` viewers group, and D5's per-user grants exist
specifically to keep it working (§7). It is a service account, not a person,
and quietly counting it as "a user who can see this" would misstate the delta.

WHAT IT DOES NOT COVER, said out loud rather than omitted. The hub, nav tabs
and tabs are NOT gated server-side — their `access_control` is read by the
frontend only (§8.3). They therefore have no server-side exposure to delta,
and the report says so explicitly so a reader cannot mistake absence for
"nothing there".

Emits **no writes of any kind** — SELECTs only, no commit, no flush. It
touches no `resource_grants` and creates no scopes. Safe against the live DB.

Run:
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.survey_widget_exposure_delta
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# This report uses the same typographic characters as the design documents it
# cross-references (section marks, arrows, the set-union sign). A Windows
# console defaults to cp1252, which cannot encode several of them, and the
# failure mode is a UnicodeEncodeError that kills the run PART-WAY THROUGH —
# after the caller has already seen a screen of plausible-looking output.
# Forcing UTF-8 here makes the encoding a property of the script rather than
# of whichever terminal happens to run it; `errors="replace"` means a console
# that still cannot render a glyph degrades to a substitution character
# instead of losing the rest of the report.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.db_v2.database import SessionLocalV2  # noqa: E402
from app.db_v2.models.component import ComponentV2  # noqa: E402
from app.db_v2.models.hub import HubV2  # noqa: E402
from app.db_v2.models.nav_tab import NavTabV2  # noqa: E402
from app.db_v2.models.role import RoleV2  # noqa: E402
from app.db_v2.models.tab import TabV2  # noqa: E402
from app.db_v2.models.thread import ThreadV2  # noqa: E402
from app.services.access_visibility_service import build_node_tree  # noqa: E402
from app.services.tab_service import (  # noqa: E402
    DEFAULT_ACCESS_CONTROL,
    HUB_ADMIN_ROLE,
    _SCOPED_ROLE_SCOPES,
    _user_can_view_widget,
)

# The service account `DEFAULT_ACCESS_CONTROL` names in every group. Not a
# person; see the module docstring.
SERVICE_ACCOUNT = "automations@renphil.org"

# Types excluded from `_serialize_gridstack_content`, and therefore from the
# canvas payload `filter_widget_content_for_user` runs over. Duplicated as
# literals rather than imported from `gridstack_service` for the same reason
# `access_visibility_service` duplicates them — importing that module pulls in
# the entire content stack. `_check_serializer_filter_still_matches` below
# pins them against the real definitions at runtime, so the copies cannot
# drift silently.
GRIDSTACK_REPRESENTATION_TYPES = ("gridstack", "super_gridstack")
MIRROR_WIDGET_TYPE = "mirror"
AIRTABLE_LIKE_WIDGET_TYPES = ("airtable", "airtable_metric", "chart")

# An email that matches nothing, used to drive the role half of the predicate
# without a user-list hit contaminating the answer.
_NOBODY_EMAIL = "nobody.matches.this@example.invalid"


# ---------------------------------------------------------
# Canonical comparison against DEFAULT_ACCESS_CONTROL
#
# Extracted and unit-tested (tests/test_widget_exposure_delta.py) because it
# is the one piece of non-trivial logic here: a naive `stored == DEFAULT`
# compares Python dicts whose LIST members are order-sensitive, so whether a
# stamped-at-creation AC is recognised as a default would depend on the order
# Postgres happened to serialise the JSON in. Getting it wrong would
# misclassify authored ACs as defaults and vice versa — the difference
# between "13 rows nobody chose" and "13 rows somebody meant".
# ---------------------------------------------------------

def canonical_access_control(ac: Any) -> Any:
    """A form of `ac` in which two ACLs that grant the same thing compare
    equal, regardless of role/user ordering or JSON key order.

    Roles and users are sorted by their full identifying tuple rather than by
    name alone: two entries can share a `name` and differ in `scope`,
    `program` or `function`, and collapsing those would call two genuinely
    different ACLs identical.

    Emails are lower-cased and stripped, matching `_user_can_view_widget`'s
    own comparison — an AC differing from the default only in the CASE of an
    email grants exactly what the default grants, so it is a default.

    Non-dict input (SQL NULL, JSON `null`, a stray list) is returned
    unchanged, so a caller can compare anything without a type check first.
    """
    if not isinstance(ac, dict):
        return ac

    out: dict[str, Any] = {}
    for group_name in sorted(ac):
        group = ac[group_name]
        if not isinstance(group, dict):
            out[group_name] = group
            continue

        roles = [r for r in (group.get("roles") or []) if isinstance(r, dict)]
        users = [u for u in (group.get("users") or []) if isinstance(u, dict)]

        out[group_name] = {
            "roles": sorted(
                (
                    {
                        "name": r.get("name", ""),
                        "scope": (r.get("scope") or "").strip(),
                        "program": (r.get("program") or "").strip(),
                        "function": (r.get("function") or "").strip(),
                    }
                    for r in roles
                ),
                key=lambda r: (r["name"], r["scope"], r["program"], r["function"]),
            ),
            "users": sorted(
                ({"email": (u.get("email") or "").strip().lower()} for u in users),
                key=lambda u: u["email"],
            ),
        }
    return out


def is_default_access_control(ac: Any) -> bool:
    """True when `ac` grants exactly what `DEFAULT_ACCESS_CONTROL` grants —
    i.e. it was stamped at creation rather than authored by anyone."""
    return canonical_access_control(ac) == canonical_access_control(DEFAULT_ACCESS_CONTROL)


# ---------------------------------------------------------
# Reading one ACL
# ---------------------------------------------------------

def viewer_entries(ac: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The `(roles, users)` of `ac`'s viewers group — the ONLY group the view
    predicate reads. `admins` is deliberately not returned: including it here
    is exactly the mistake that makes a reader believe a populated `admins`
    grants something."""
    viewers = ac.get("viewers") or {}
    roles = [r for r in (viewers.get("roles") or []) if isinstance(r, dict)]
    users = [u for u in (viewers.get("users") or []) if isinstance(u, dict)]
    return roles, users


def gates_anything(ac: dict[str, Any]) -> bool:
    """False when the viewers group is empty — which means OPEN TO EVERYONE,
    not closed (`_user_can_view_widget` returns True early)."""
    roles, users = viewer_entries(ac)
    return bool(roles or users)


def inert_role_entries(ac: dict[str, Any]) -> list[str]:
    """Viewer role entries that match NOBODY because they are Program- or
    Function-scoped and the predicate skips those outright."""
    roles, _ = viewer_entries(ac)
    out = []
    for r in roles:
        scope = (r.get("scope") or "").strip()
        if scope in _SCOPED_ROLE_SCOPES:
            detail = (r.get("program") or "").strip() or (r.get("function") or "").strip()
            out.append(f"{r.get('name', '')} ({scope}{'/' + detail if detail else ''})")
    return out


def _label(r: dict[str, Any]) -> str:
    scope = (r.get("scope") or "").strip()
    detail = (r.get("program") or "").strip() or (r.get("function") or "").strip()
    return f"{r.get('name', '')} [{scope}{'/' + detail if detail else ''}]"


# ---------------------------------------------------------
# Node addressing
# ---------------------------------------------------------

def node_path(tree: Any, titles: dict[tuple[str, int], str], component_id: int) -> str:
    """Root-first `hub > nav tab > tab > …` path for one component, so the
    owner can see which node would need a grant to reproduce today's access."""
    ref = ("component", component_id)
    if tree.is_orphan(ref):
        return "<ORPHAN — parent chain does not reach the hub (§3.3: fails closed)>"
    chain = list(reversed(tree.ancestors(ref)))
    return " > ".join(titles.get(a, f"{a[0]}:{a[1]}") for a in chain) or "<root>"


def _check_serializer_filter_still_matches() -> list[str]:
    """Pin the duplicated widget-type literals against `gridstack_service`'s
    real definitions. Imported lazily and inside a try, so a heavyweight
    import failure downgrades this report to a warning rather than killing a
    read-only survey the owner is running for other reasons."""
    problems: list[str] = []
    try:
        from app.services import gridstack_service as gs
    except Exception as exc:  # noqa: BLE001
        return [f"could not import gridstack_service to verify constants: {exc!r}"]

    if tuple(gs.GRIDSTACK_REPRESENTATION_TYPES) != GRIDSTACK_REPRESENTATION_TYPES:
        problems.append(
            f"GRIDSTACK_REPRESENTATION_TYPES drifted: {gs.GRIDSTACK_REPRESENTATION_TYPES!r}"
        )
    if gs.MIRROR_WIDGET_TYPE != MIRROR_WIDGET_TYPE:
        problems.append(f"MIRROR_WIDGET_TYPE drifted: {gs.MIRROR_WIDGET_TYPE!r}")
    if tuple(gs.AIRTABLE_LIKE_WIDGET_TYPES) != AIRTABLE_LIKE_WIDGET_TYPES:
        problems.append(
            f"AIRTABLE_LIKE_WIDGET_TYPES drifted: {gs.AIRTABLE_LIKE_WIDGET_TYPES!r}"
        )
    return problems


def main() -> None:  # noqa: C901 — a report; the length is enumeration, not branching depth
    print("=" * 78)
    print("WIDGET EXPOSURE DELTA — READ-ONLY. No writes of any kind.")
    print("=" * 78)

    drift = _check_serializer_filter_still_matches()
    if drift:
        print("\n⚠ CONSTANT DRIFT — this report's reachability column may be wrong:")
        for line in drift:
            print(f"    {line}")

    db = SessionLocalV2()
    try:
        components = db.query(ComponentV2).order_by(ComponentV2.id.asc()).all()
        role_names_in_db = [r.name for r in db.query(RoleV2).order_by(RoleV2.id.asc()).all()]

        threaded_component_ids = {
            row[0] for row in db.query(ThreadV2.component_id).distinct().all()
        }

        # Mirror substitution (`_serialize_component`) puts the TARGET's
        # access_control on the mirror's widget entry, so a gated component
        # that is itself invisible to the serializer can still be gated
        # THROUGH a mirror sitting on an ordinary canvas. Resolved here so
        # that path is measured rather than assumed absent.
        mirror_target_links = {
            (c.props or {}).get("target_link")
            for c in components
            if c.type == MIRROR_WIDGET_TYPE
        }
        mirror_target_links.discard(None)

        tree = build_node_tree(db)
        titles: dict[tuple[str, int], str] = {}
        for h in db.query(HubV2).all():
            titles[("hub", h.id)] = "hub"
        for n in db.query(NavTabV2).all():
            titles[("nav_tab", n.id)] = f"nav:{n.title}"
        for t in db.query(TabV2).all():
            kind = "variant" if t.parent_tab_id is not None else "tab"
            titles[("tab", t.id)] = f"{kind}:{t.title}"
        for c in components:
            titles[("component", c.id)] = f"component:{c.title or c.type}#{c.id}"

        # ---- personas -------------------------------------------------
        # Every JWT role name that could matter: the ones the RBAC tables
        # define, PLUS every name any stored ACL actually mentions. The
        # second half is not redundant — "Studio Member" appears in
        # DEFAULT_ACCESS_CONTROL and has no `roles` row, so a sweep built
        # from the table alone would silently never test it.
        acl_role_names: set[str] = set()
        for c in components:
            ac = c.access_control
            if isinstance(ac, dict):
                roles, _ = viewer_entries(ac)
                acl_role_names.update((r.get("name") or "") for r in roles)
        acl_role_names.discard("")
        personas = sorted(set(role_names_in_db) | acl_role_names)

        # ---- partition ------------------------------------------------
        with_ac = [c for c in components if isinstance(c.access_control, dict) and c.access_control]
        gating = [c for c in with_ac if gates_anything(c.access_control)]
        open_to_all = [c for c in with_ac if not gates_anything(c.access_control)]

        print(f"\nComponents in the hub: {len(components)}")
        print(f"  carrying a real access_control dict : {len(with_ac)}")
        print(f"    …whose `viewers` GATES something  : {len(gating)}")
        print(f"    …whose `viewers` is EMPTY          : {len(open_to_all)}  "
              f"(open to everyone — an empty viewers group is not a closed one)")
        print(f"  no access_control at all             : {len(components) - len(with_ac)}")
        print(f"\nPersonas swept ({len(personas)}): {', '.join(personas)}")
        print(f"  (role names from `roles` ∪ every name any stored ACL mentions)")

        # ---- the gated ones -------------------------------------------
        print("\n" + "=" * 78)
        print(f"THE {len(gating)} GATED WIDGETS — who can see each one TODAY")
        print("=" * 78)

        default_count = 0
        handset: list[int] = []
        reach_tally: dict[str, list[int]] = {}
        for c in gating:
            ac = c.access_control
            roles, users = viewer_entries(ac)
            is_default = is_default_access_control(ac)
            if is_default:
                default_count += 1
            else:
                handset.append(c.id)

            can_see = [
                p for p in personas
                if _user_can_view_widget(ac, _NOBODY_EMAIL, [p])
            ]
            named_users = [(u.get("email") or "").strip().lower() for u in users]
            people = [e for e in named_users if e != SERVICE_ACCOUNT]
            service = [e for e in named_users if e == SERVICE_ACCOUNT]
            inert = inert_role_entries(ac)

            # Which enforcement paths can actually reach this row.
            reach: list[str] = []
            in_canvas = (
                c.super_blocknote_id is None
                and c.type not in GRIDSTACK_REPRESENTATION_TYPES
            )
            if in_canvas:
                reach.append("workspace redaction (tabs_v2.py:182)")
            if c.link and c.type != MIRROR_WIDGET_TYPE:
                reach.append("component deep link, 403 (tabs_v2.py:153)")
            if c.type in AIRTABLE_LIKE_WIDGET_TYPES:
                reach.append("airtable data endpoints (airtable.py ×6)")
            if c.id in threaded_component_ids:
                reach.append("threads (thread_service.py:191)")
            if c.link in mirror_target_links:
                reach.append("mirror substitution (gridstack_service.py:701)")
            for path in reach or ["<<nothing>>"]:
                reach_tally.setdefault(path, []).append(c.id)

            print(f"\n  component {c.id} — {c.title or '(untitled)'}   [type={c.type}]")
            print(f"    under        : {node_path(tree, titles, c.id)}")
            print(f"    ACL origin   : {'DEFAULT (stamped at creation)' if is_default else 'HAND-SET'}")
            print(f"    viewers.roles: {', '.join(_label(r) for r in roles) or '(none)'}")
            print(f"    CAN SEE TODAY: {', '.join(can_see) or '(no role matches — role entries grant nobody)'}")
            print(f"    + Hub Admins : YES — bypass upstream in filter_widget_content_for_user, "
                  f"never via the predicate")
            if people:
                print(f"    named people : {', '.join(people)}")
            if service:
                print(f"    service acct : {', '.join(service)}   "
                      f"⚠ NOT a person — D5 per-user grants exist to keep this working (§7)")
            if not people and not service:
                print(f"    named people : (none)")
            if inert:
                print(f"    ⚠ INERT      : {', '.join(inert)} — Program/Function scoped, so "
                      f"_SCOPED_ROLE_SCOPES SKIPS it. Matches NOBODY.")
            print(f"    reachable by : {'; '.join(reach) or '⚠ NOTHING — no live path consults this ACL'}")

        print("\n" + "-" * 78)
        print(f"ACL origin split: {default_count} byte-identical to DEFAULT_ACCESS_CONTROL "
              f"(canonically compared), {len(handset)} hand-set"
              + (f" — components {', '.join(str(i) for i in handset)}" if handset else ""))

        # ---- which gate actually fires -------------------------------
        # The headline number. It is easy to read "per-widget access_control
        # is enforced in nine places" as "nine places are gating these
        # widgets", and on today's data that is false: a call site only
        # gates a component it can actually be handed. The workspace
        # redactor runs over `_serialize_gridstack_content`'s output, which
        # EXCLUDES super-blocknote descendants and gridstack self-
        # representations — and on live data every gated component is one or
        # the other. So the redactor is never given one to redact.
        print("\n" + "=" * 78)
        print("WHICH ENFORCEMENT PATH ACTUALLY REACHES THESE ROWS")
        print("=" * 78)
        for path, ids in sorted(reach_tally.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            shown = ", ".join(str(i) for i in ids[:20]) + (" …" if len(ids) > 20 else "")
            label = "NO LIVE PATH" if path == "<<nothing>>" else path
            print(f"  {len(ids):3d}  {label}")
            print(f"       components: {shown}")
        in_canvas_count = sum(
            1 for c in gating
            if c.super_blocknote_id is None and c.type not in GRIDSTACK_REPRESENTATION_TYPES
        )
        print(f"\n  Gated components visible to the CANVAS SERIALIZER: {in_canvas_count} of {len(gating)}")
        print("  `filter_widget_content_for_user` can only redact what the serializer put in")
        print("  `content.widgets`, and that query excludes `super_blocknote_id IS NOT NULL`")
        print("  and the gridstack self-representation types. A component outside it is")
        print("  never offered to the redactor at all.")

        # ---- the open ones --------------------------------------------
        print("\n" + "=" * 78)
        print(f"THE {len(open_to_all)} WIDGETS WITH AN ACL THAT GATES NOTHING")
        print("=" * 78)
        print("  An empty `viewers` group returns True for everyone. Several of these")
        print("  carry a POPULATED `admins` group, which the view predicate never reads —")
        print("  they look restricted in the UI's data and are open in fact.")
        for c in open_to_all:
            ac = c.access_control
            admins = ac.get("admins") or {}
            admins_populated = bool((admins.get("roles") or []) or (admins.get("users") or []))
            print(f"    component {c.id:4d} {(c.title or '(untitled)'):42s} type={c.type:16s} "
                  f"admins_populated={admins_populated}")

        # ---- what is NOT gated ----------------------------------------
        print("\n" + "=" * 78)
        print("NODES WITH NO SERVER-SIDE EXPOSURE AT ALL — stated, not omitted")
        print("=" * 78)
        n_hub = db.query(HubV2).count()
        n_nav = db.query(NavTabV2).count()
        n_tabs = db.query(TabV2).count()
        print(f"  hub      : {n_hub} row(s)")
        print(f"  nav tabs : {n_nav}")
        print(f"  tabs     : {n_tabs}")
        print("\n  These carry `access_control` too, and NONE of it is enforced server-side.")
        print("  It is read by the frontend only (§8.3: `visibleTabs` is a pass-through and")
        print("  the API hands the whole tree to any signed-in user). They therefore have")
        print("  NO exposure to delta: turning the old path off changes nothing for them,")
        print("  because nothing was ever consulting it. Absence here is a measured")
        print("  finding, not an omission.")

        print("\n" + "=" * 78)
        print("WHAT A REPLACEMENT MUST REPRODUCE, AND WHAT IS BEING DROPPED")
        print("=" * 78)
        print(f"  • {len(gating)} components are gated. To reproduce today's access with")
        print("    resource_grants, each needs a grant reaching the roles listed above —")
        print("    on the component itself, or on an ancestor whose whole subtree may be")
        print("    granted (§5.5 has no break-glass: granting the ancestor grants ALL of it).")
        print(f"  • {default_count} of those ACLs were stamped at creation, not authored. Reproducing")
        print("    them faithfully would encode a default nobody chose. Decide per widget")
        print("    whether it was ever meant to be restricted.")
        print("  • Every Program/Function-scoped entry flagged INERT above is granting")
        print("    nothing today. Reproducing it as a real grant WIDENS access — that is a")
        print("    change in behaviour, not a migration.")
        print(f"  • {SERVICE_ACCOUNT} is named in the default ACLs. It is a service")
        print("    account; D5's per-user grants exist to keep it working (§7).")
        print("  • Hub Admins see everything today via the upstream bypass, and under the")
        print("    new design via an `edit` grant on the hub node (§6.5) plus a permanent")
        print("    hardcoded backstop.")
    finally:
        # Read-only, but the session is closed explicitly rather than left to
        # the garbage collector — a script run against live Neon should not
        # hold a pooled connection open past its last query.
        db.close()


if __name__ == "__main__":
    main()
