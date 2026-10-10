"""Who may write or revoke a grant on a node
(plan_access_control_algorithm_2026-08-27.md §6.3, and §12, which left this
undesigned — the owner decided it on 2026-08-31; see below).

Kept separate from ``resource_grant_service`` for the same split reason
``rbac_delegation_service`` is kept separate from ``rbac_graph_service``:
that module owns ``resource_grants`` rows and the read-time matching
primitive; this one owns the one RULE built on top of them — who may create
or delete a row. This module writes nothing.

THIS IS NOT ``rbac_delegation_service``, AND IT MUST NOT BECOME IT. The two
answer different questions on opposite sides of §4.2's arrow, and the
obvious consolidation — "grants are delegation, reuse ``assert_can_delegate``"
— is wrong in a way that is invisible until it is measured. That measurement
is recorded in full below rather than summarised, because the reuse is the
intuitive move and someone will propose it again.

═══════════════════════════════════════════════════════════════════════════
WHY ``can_delegate`` IS THE WRONG RULE HERE (measured 2026-08-31)
═══════════════════════════════════════════════════════════════════════════

``can_delegate`` requires the target role to be a PROPER descendant of a
role the granter holds. On the ASSIGNMENT side that is exactly right: it
stops you cloning your own authority onto another person.

On the OBJECT side the direction inverts (§4.3 — "the lower and narrower the
stored pair, the MORE people it reaches"), so the same rule reads backwards.
Measured against a granter holding exactly ``(program_lead, program_a)``,
over a five-person population on the live role DAG:

    candidate grant                can_delegate    audience
    (program_lead,   all_scopes)      REFUSED         1
    (program_lead,   program_a )      REFUSED         2
    (program_member, program_a )      permitted       3
    (⊥role,          program_a )      permitted       4
    (⊥role,          ⊥scope    )      permitted       5   ← everyone

It refuses the two NARROWEST grants and permits the WIDEST. A program lead
could not share a page with their own peers, but could share it with
everyone junior to those peers — strictly more people — and with the entire
hub. That is not a leak at one end of the lattice, it is the rule pointing
the wrong way along its whole length.

THE RULE THAT REPLACED IT, decided by the owner on 2026-08-31:

    A grant's pair must lie in ``effective_pairs(granter)``.

Or, as the sentence to keep:

    **You may only write a grant that you yourself would match.**
    Nobody hands out access to an audience they are not part of.

Three properties make it the right shape rather than merely a better one:

1. **It is §4.2's own predicate**, run against the granter instead of
   against a reader. No new concept is introduced, and the gate cannot drift
   away from the read path it is gating, because it IS the read path's
   question.
2. **It is monotone.** Everything a grantee can subsequently grant is a
   subset of what the granter could, since their effective pairs are a
   subset. Authority cannot be laundered upward through a chain of grants.
3. **It restores the correlation with audience size.** Under it the granter
   above may write ``(program_lead, program_a)`` — their own peers, audience
   2 — and may not write ``(hub_admin, all_scopes)``.

``rbac_delegation_service.can_delegate`` IS UNCHANGED AND STAYS THAT WAY.
Its docstring's warning — that it deliberately does not call
``effective_pairs``, because §6.1 needs PROPER descent and flattening
destroys the per-row fact properness depends on — remains true of
assignments and is pinned by
``test_can_delegate_is_not_membership_in_effective_pairs``. This module
calling ``effective_pairs`` is not that mistake made anyway; it is a
different question whose correct answer happens to be the flat set. The two
rules are supposed to differ.

═══════════════════════════════════════════════════════════════════════════
⊥ ("Any Role" / "Any Scope") — NO CARVE-OUT, AND IT IS DELIBERATE
═══════════════════════════════════════════════════════════════════════════

READ THIS BEFORE ADDING A GUARD. The table above ends at audience 5 for a
reason: **both ⊥ rows sit in every descendant closure by construction**, so
``(⊥role, ⊥scope)`` is in the effective pairs of ANY user holding ANY
assignment. Under the rule above, that means:

    any user with any assignment, who also holds edit(n),
    may publish n to the entire hub.

That consequence was measured, tabled, and put to the owner with three
carve-outs to choose from (⊥scope requires ``edit(hub)``; ⊥ on either axis
requires ``edit(hub)``; ⊥ requires Hub Admin identity). **The owner chose no
carve-out on 2026-08-31.** It is a decision, not an oversight, and it is
consistent: §4.4 built the ⊥ rows precisely so that "open to everyone" is
one row instead of one per scope, and gating them behind the hub would make
the ordinary case — *"everyone on Program A"*, written durably so it does
not silently narrow the day a junior role is added — unwritable by the
program lead it exists for.

Two things follow, and neither is optional:

- **⊥ IS A LEGAL GRANT PRINCIPAL. Do not copy
  ``routers/rbac_assignments.py::_assert_not_public`` onto this path.** That
  refusal is a security control on the ASSIGNMENT side, where holding ⊥
  confers only what everyone already reaches and would blow the delegation
  rule open. On an object grant the flag means the opposite — it is the
  whole point of it. Each flag has a lane: ``is_universal`` for assignments,
  ``is_public`` for object grants.
  ``test_the_bottom_is_not_refused_on_the_grants_path`` goes red if someone
  copies the refusal across.

- **The restraint on ⊥ is ``edit(n)``, and it is the only one.** Publishing
  a node to the hub requires controlling that node. That is a real bound —
  it is not "any signed-in user", it is "someone the node was entrusted to"
  — and it is the bound the owner accepted. If it is ever revisited, the
  thing to change is this module's ⊥ handling, not ``effective_pairs`` and
  not the ⊥ flags themselves; changing those would silently rewrite what
  every existing grant reaches (§4.4, "immutable after creation").

═══════════════════════════════════════════════════════════════════════════
THE BOOTSTRAP TRAP — the Hub Admin branch stays first and unconditional
═══════════════════════════════════════════════════════════════════════════

``role_assignments`` and ``resource_grants`` are both EMPTY in production.
So ``effective(U) = ∅``, every seed set is empty, ``edit(n)`` is FALSE for
everyone, and BOTH halves of the gate below refuse every caller. Without a
bypass nobody can write the first grant, and the system locks itself out of
its own bootstrap with no way back in except direct database access.

This is the same trap ``assert_can_delegate`` documents, and it has the same
answer: **the Hub Admin branch is first, and it is unconditional.** §6.5
requires a hardcoded backstop beneath the grant PERMANENTLY, not just during
cutover — because if the only path in is data, someone can delete their way
to a hub nobody can administer. Today that backstop is the JWT role check;
§10 item 6 replaces it with a settings-level ``BOOTSTRAP_ADMIN_EMAILS`` list
so the floor outlives Airtable rather than depending on it.

``test_the_bootstrap_is_not_deadlocked`` runs the whole gate against
completely empty tables. It is the one test in the module that would still
matter if every other one were deleted.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.access_visibility_service import (
    NodeTree,
    VisibilityResult,
    compute_visibility,
)
from app.services.rbac_graph_service import RbacClosures, RbacGraphError, effective_pairs


def assert_can_administer_node(
    db: Session,
    *,
    hub_user_id: int,
    is_hub_admin: bool,
    node_kind: str,
    node_id: int,
    closures: RbacClosures | None = None,
    tree: NodeTree | None = None,
    visibility: VisibilityResult | None = None,
) -> None:
    """Step 1 and step 2 of the gate on their own: Hub Admin, or ``edit`` on
    the node.

    Split out because the grants surface has READS as well as writes — "who
    can access this node?" and §6.2's "what would they retain?" — and those
    have a node to check but no principal to check. The alternative shape,
    calling ``assert_can_grant`` with a principal invented to make the pair
    rule short-circuit, works and is unreadable: it puts the CALLER's id in
    a parameter that means the GRANT's target, and the next person to read
    it has to prove to themselves that it was a trick rather than a bug.

    Gating reads on ``edit`` rather than on ``view`` is a judgement call §9
    does not settle; ``routers/resource_grants.py``'s module docstring
    records the reasoning.
    """
    # FIRST AND UNCONDITIONAL — see the module docstring's bootstrap section.
    # Do not move this below the check, do not make it depend on a
    # role_assignments row, and do not fold it into the expression below.
    # With both tables empty it is the only branch that can pass, and it is
    # what makes the first grant writable.
    if is_hub_admin:
        return

    graph = closures if closures is not None else RbacClosures(db)
    verdict = (
        visibility
        if visibility is not None
        else compute_visibility(db, hub_user_id, closures=graph, tree=tree)
    )
    if verdict.verdict(node_kind, node_id).edit:
        return

    # Deliberately the same message whether the node is uneditable,
    # invisible, an orphan, or absent. ``verdict()`` already returns
    # INVISIBLE for all four, and distinguishing them here would let a
    # caller probe for the existence of nodes they cannot see (§9: "a deep
    # link to a hidden node should 404 rather than 403 so it does not
    # confirm the node exists").
    raise RbacGraphError(
        "node_not_editable",
        (
            "You need edit access on this item to change who can see it. "
            "Grants are written by the people a node is entrusted to."
        ),
        node_kind=node_kind,
        node_id=node_id,
    )


def assert_can_grant(
    db: Session,
    *,
    granter_hub_user_id: int,
    is_hub_admin: bool,
    node_kind: str,
    node_id: int,
    role_id: int | None,
    scope_id: int | None,
    user_id: int | None,
    closures: RbacClosures | None = None,
    tree: NodeTree | None = None,
    visibility: VisibilityResult | None = None,
) -> None:
    """The full write gate for creating OR revoking a grant on a node.

    Three steps, in this order, and the order is load-bearing:

    1. **Hub Admin bypasses, unconditionally** — and bypasses BOTH remaining
       steps, not just the first. The bootstrap backstop; see the module
       docstring. Nothing may run before it.
    2. **``edit(node)`` is required.** This is what makes a grant an act on a
       resource the caller controls rather than a free-floating capability.
       Without it, step 3 alone would let anyone holding an assignment write
       grants on every node in the hub, including nodes they cannot see.
       §6.3's table requires it; the owner confirmed it on 2026-08-31 as an
       independent requirement rather than a consequence of step 3.
    3. **The pair must be in ``effective_pairs(granter)``** — "you may only
       write a grant you would match yourself". See the module docstring for
       why this is not ``can_delegate``.

    STEP 1 SKIPS STEP 3 EXPLICITLY, and the explicit line is the point. A Hub
    Admin in production holds no ``role_assignments`` row, so their
    ``effective_pairs`` is empty and step 3 would refuse them every pair
    there is. Letting the bypass fall through to it would re-close the
    bootstrap that step 1 exists to open — the deadlock reappearing one
    branch further down.

    A **user-form** grant stops after step 2, and that is deliberate: there
    is no pair to test, and naming one person is the NARROWEST audience the
    table can express — narrower than any pair, ⊥ or otherwise. Gating it on
    the target's own org position would restrict who you may share a node
    with based on where they sit, which has no analogue anywhere in this
    design and would break D5's ``automations@renphil.org`` case. Controlling
    the node is the whole rule.

    SYMMETRIC ON CREATE AND REVOKE, the same way
    ``rbac_assignments.delete_assignment`` re-runs its gate against the
    assignment's own role/scope rather than checking who granted it. There is
    deliberately no "I granted it, so I may remove it" shortcut, and equally
    no ownership requirement — revocation is decided by what the revoker
    holds now.

    Note what symmetry does NOT mean: ``resource_grant_service.delete_grant``
    still validates nothing about the row's CONTENT, deliberately (hazard:
    "refuse the way in, never the way out"). A ⊥ grant stays revocable — it
    is in every user's effective pairs, so step 3 passes for anyone who
    passes step 2, and a Hub Admin passes regardless. The widest grant in the
    system must never become the one thing nobody can take away.

    Pass ``closures`` / ``tree`` / ``visibility`` to share one snapshot per
    request (§8.2, §8.4). **Never cache any of them across requests** — an
    edge or a grant written in between would not be seen, and all three are
    authorization inputs.

    Raises ``RbacGraphError``; the router maps it to 409, matching every
    other gate in the ``/v2/rbac`` family (see ``rbac.py::_conflict``).
    """
    # -- 1 and 2. the bootstrap backstop, then edit(node) -----------
    #
    # Returns for a Hub Admin BEFORE touching the database, which is what
    # makes passing `closures`/`tree` optional rather than wasteful: on the
    # bootstrap path there is nothing to load.
    assert_can_administer_node(
        db,
        hub_user_id=granter_hub_user_id,
        is_hub_admin=is_hub_admin,
        node_kind=node_kind,
        node_id=node_id,
        closures=closures,
        tree=tree,
        visibility=visibility,
    )
    if is_hub_admin:
        # Step 3 does not apply to a Hub Admin either. Stated as its own
        # line rather than left to fall through the pair rule below, because
        # `effective_pairs` is empty for an admin with no assignments — the
        # production state — and reaching that test at all would refuse the
        # one caller the backstop exists to admit.
        return

    graph = closures if closures is not None else RbacClosures(db)

    # -- 3. the pair rule ------------------------------------------
    if user_id is not None:
        # A user-form grant. Step 2 was the whole gate; see the docstring.
        return

    if role_id is None or scope_id is None:
        # Shape error rather than an authorization one, but it must not fall
        # through to a membership test that a half-built pair would silently
        # fail — "(role, None) is not in your effective pairs" is a true
        # sentence and a useless error message.
        raise RbacGraphError(
            "incomplete_pair",
            "A principal pair needs both a role and a scope.",
            role_id=role_id,
            scope_id=scope_id,
        )

    if (role_id, scope_id) not in effective_pairs(db, granter_hub_user_id, closures=graph):
        raise RbacGraphError(
            "not_grantable",
            (
                "You can only grant access to an audience you are part of "
                "yourself. This role and scope combination is not one your "
                "own assignments reach."
            ),
            role_id=role_id,
            scope_id=scope_id,
        )
