"""Object grants — what is granted, to whom, on which node
(plan_access_control_algorithm_2026-08-27.md §7, D5, D6).

One row means: *this principal reaches this node at this level*. The read
path (§8.1) intersects a node's bag of grants with the user's effective
pairs; nothing here is propagated at write time and nothing is derived into
storage, which is the design's one commitment (§1).

A NODE CARRIES A BAG OF GRANTS, NOT "AN ACL". There is no single
access-control object per node any more, and several grants on one node are
the normal case rather than an edge case. ``Root A`` holding
``(Program Member, X, view)``, ``(Program Lead, X, edit)`` and
``(Program Lead, Y, edit)`` is three ordinary rows, and ``seed(n)`` is a set
intersection where any one match suffices.

BOTH AXES RUN BACKWARDS ON AN OBJECT GRANT, and this is the single most
counterintuitive thing about this table (§4.3). A stored pair matches when it
sits AT OR BELOW a pair the user holds, so *the lower and narrower the stored
pair, the more people it reaches*. In particular ``All Scopes`` — the
universal scope, which means "every scope" in an ASSIGNMENT — is the
NARROWEST choice here: ``(Hub Member, All Scopes)`` stored on a node reaches
only users whose own assignment is hub-wide. It is a legitimate thing to
want (an org-leadership tab), so §9 says label it, never forbid it. Anything
that summarizes, sorts, validates or labels a grant must not "helpfully"
treat the universal scope as the widest option.

The mirror end is what ⊥ is for: ``(Any Role, Any Scope)`` reaches literally
everyone, now and after any role or scope is added, in ONE row — which is
why ``is_public`` is refused as an ASSIGNMENT but is the normal case here.
Each flag has a lane: ``is_universal`` belongs in assignments, ``is_public``
belongs on object grants.

WHY THE NODE ARC IS FOUR NULLABLE FK COLUMNS AND NOT A POLYMORPHIC
``(node_type, node_id)`` PAIR. The polymorphic shape is the obvious economy —
two columns instead of four, no CHECK, no eight-index uniqueness rule below —
and it was rejected because it cannot have foreign keys. A
``(node_type, node_id)`` pair is an integer the database cannot join or
validate, so:

  - **Deleting a tab would orphan its grants**, silently and invisibly. Ids
    are reused across tables, so a stale grant on tab 41 becomes a live grant
    on whatever tab 41 is next — an access hole created by a delete nobody
    thought was an access-control operation. With a real FK, ``ON DELETE
    CASCADE`` takes them with it.
  - **Nothing could enforce that the node exists at all.** A typo'd
    ``node_type`` writes a row that matches no node and is never read again.
  - The same argument ``role_assignments`` already makes for ``RESTRICT`` on
    ``role_id``/``scope_id`` applies here and is load-bearing in the other
    direction: deleting a role that is still granted somewhere must not
    silently revoke access. See ``rbac_service.delete_role``, which pre-counts
    BOTH assignments and grants so the caller gets a named 409 with a count
    rather than an opaque IntegrityError.

Four nullable columns plus one CHECK buys every one of those back. The cost
is that "which node is this?" is four columns to inspect instead of one, which
``NODE_COLUMNS`` below turns back into a single lookup for every caller.

GRIDSTACKS ARE DELIBERATELY ABSENT (§3.2). ``GridstackV2`` is a transparent
pass-through link in the parent chain, holds no grants, and both folds skip
it — a component's owner resolves THROUGH its gridstack to the tab or parent
component that does hold grants. Four node kinds, not five.

No relationship() here, matching every other model in db_v2/.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)

from app.db_v2.database import BaseV2

# Importing every FK target is load-bearing, not tidiness — exactly the note
# role_assignment.py carries. SQLAlchemy resolves ForeignKey("tabs.id") by
# TABLE NAME against BaseV2.metadata at DDL time, so any caller that imports
# this module and then runs create_all over the whole metadata (which is what
# tests/ does) fails with NoReferencedTableError unless the target tables were
# imported too.
#
# The list is longer than the four node kinds because it has to be
# TRANSITIVELY closed: ComponentV2 points at gridstacks and page_contents,
# GridstackV2 points at tabs, and TabV2 points at nav_tabs. Dropping any one
# of them breaks create_all for every test module that touches this table.
from app.db_v2.models.component import ComponentV2  # noqa: F401
from app.db_v2.models.gridstack import GridstackV2  # noqa: F401
from app.db_v2.models.hub import HubV2  # noqa: F401
from app.db_v2.models.hub_user import HubUserV2  # noqa: F401
from app.db_v2.models.nav_tab import NavTabV2  # noqa: F401
from app.db_v2.models.page_content import PageContentV2  # noqa: F401
from app.db_v2.models.role import RoleV2  # noqa: F401
from app.db_v2.models.scope import ScopeV2  # noqa: F401
from app.db_v2.models.tab import TabV2  # noqa: F401

# The two levels (§7). `level` is a ROW, not two boolean columns, so that
# "edit implies view" lives in the read predicate (§6.1) rather than in the
# data — a node granted edit does not also need a view row written beside it.
LEVEL_VIEW = "view"
LEVEL_EDIT = "edit"
GRANT_LEVELS = (LEVEL_VIEW, LEVEL_EDIT)

# node kind -> the column that carries it. ONE source of truth: the CHECK
# below, the eight uniqueness indexes, the migration script and the service's
# node addressing all derive from this, so adding a fifth node kind is one
# entry here plus a column, and cannot half-happen.
NODE_COLUMNS: dict[str, str] = {
    "hub": "hub_id",
    "nav_tab": "nav_tab_id",
    "tab": "tab_id",
    "component": "component_id",
}

NODE_KINDS = tuple(NODE_COLUMNS)

# "Exactly one of the four node columns is non-null", written portably.
#
# The Postgres-idiomatic `(x IS NOT NULL)::int + ...` is not usable: the test
# suite builds its schema from these models via create_all on in-memory
# SQLite, where `::int` is a syntax error, so the CHECK would exist only in
# production and every test asserting it would pass vacuously. CASE WHEN is
# the portable spelling and runs identically on both.
_EXACTLY_ONE_NODE = (
    " + ".join(f"CASE WHEN {c} IS NULL THEN 0 ELSE 1 END" for c in NODE_COLUMNS.values()) + " = 1"
)

# "Either the (role, scope) pair, or a user — never both, never neither."
#
# Written as two explicit conjunctions rather than a CASE sum, because a sum
# cannot say it: the pair form has two non-null principal columns and the user
# form has one, so no single total distinguishes "role + scope" from
# "role + user". Spelling both shapes out also states the rule in the same
# words the service and the schema use.
_EXACTLY_ONE_PRINCIPAL = (
    "(role_id IS NOT NULL AND scope_id IS NOT NULL AND user_id IS NULL)"
    " OR "
    "(role_id IS NULL AND scope_id IS NULL AND user_id IS NOT NULL)"
)


def _uniqueness_indexes() -> list[Index]:
    """The uniqueness rule: one partial unique index per node kind × principal
    form, eight in total.

    WHY NOT THE PLAIN `UNIQUE` §7 SPECIFIES. As written there —
    ``UNIQUE (hub_id, nav_tab_id, tab_id, component_id, role_id, scope_id,
    user_id, level)`` — it enforces NOTHING AT ALL. Every row in this table
    has at least three NULLs among those columns (three of the four node
    columns, plus either ``user_id`` or the ``(role_id, scope_id)`` pair), and
    NULLs are DISTINCT in a unique constraint in both Postgres and SQLite. Two
    byte-identical grants therefore both insert. This was verified by building
    it and inserting duplicates, not by reading the standard.

    WHY EIGHT INDEXES RATHER THAN THE ALTERNATIVES:

      - ``NULLS NOT DISTINCT`` (PG 15+) expresses the intent in one line and
        Neon would take it, but SQLAlchemy silently drops the clause on
        SQLite and emits a plain UNIQUE — so under test the constraint is
        exactly as inert as the version above, and the duplicate-insert test
        could never be written. An unverifiable constraint on the revocation
        path is worse than a verbose one.
      - COALESCE sentinel expression indexes (two instead of eight) work on
        both dialects, but key on ``coalesce(tab_id, 0)`` rather than on
        ``tab_id``, so they cannot serve "which grants are on this node?" —
        the query grant CRUD, §6.2's revoke-time confirmation and §9's
        "who can access this node" panel all run. They would need four more
        plain indexes beside them.

    THE COUNT IS COSMETIC, WHICH IS THE POINT THAT SETTLES IT. The eight
    predicates are mutually exclusive — every row has exactly one non-null
    node column and exactly one principal form — so each row lands in exactly
    ONE of these indexes. Total storage and per-insert write amplification are
    identical to a single index over the whole table; there is no eight-fold
    anything. And because each one leads with its node column, the pair
    covering a node kind also serves every "grants on this node" lookup and
    the FK's own ON DELETE CASCADE scan, so no separate node indexes are
    carried.

    BOTH DIALECT PREDICATES ARE REQUIRED on every one of them, for the reason
    spelled out at length above ``uq_scopes_single_universal``: these kwargs
    are dialect-prefixed, so a dialect with no matching one silently loses the
    WHERE. Here that does not fail loudly the way it does on a boolean column
    — it degrades to a plain index over nullable columns, which is inert
    again. They are generated from one loop rather than written out sixteen
    times precisely so a predicate cannot drift or go missing on one line.
    """
    indexes: list[Index] = []
    for kind, column in NODE_COLUMNS.items():
        # Pair form: (node, role, scope, level). No NULLs in the key for any
        # row the predicate admits, which is what makes it bite.
        pair_where = f"{column} IS NOT NULL AND user_id IS NULL"
        indexes.append(
            Index(
                f"uq_resource_grants_{kind}_pair",
                column,
                "role_id",
                "scope_id",
                "level",
                unique=True,
                postgresql_where=text(pair_where),
                sqlite_where=text(pair_where),
            )
        )
        # User form: (node, user, level). Likewise NULL-free.
        user_where = f"{column} IS NOT NULL AND user_id IS NOT NULL"
        indexes.append(
            Index(
                f"uq_resource_grants_{kind}_user",
                column,
                "user_id",
                "level",
                unique=True,
                postgresql_where=text(user_where),
                sqlite_where=text(user_where),
            )
        )
    return indexes


class ResourceGrantV2(BaseV2):
    """One grant: a principal, a node, a level."""

    __tablename__ = "resource_grants"

    id = Column(Integer, primary_key=True, index=True)

    # ---- the node arc: exactly one non-null -------------------------
    #
    # CASCADE on all four: deleting a node takes its grants with it. A grant
    # naming a node that no longer exists is meaningless, and leaving it
    # behind is the id-reuse hole described in the module docstring.
    #
    # Not indexed individually — the two partial unique indexes for each node
    # kind lead with its column and between them cover every row where it is
    # non-null, which is exactly the set a `WHERE tab_id = X` lookup or a
    # cascade scan wants. Grants are far fewer than nodes (§8.1), so a third
    # index per column would be paid for on every write and chosen over those
    # two by nothing.
    hub_id = Column(Integer, ForeignKey("hub.id", ondelete="CASCADE"), nullable=True)
    nav_tab_id = Column(Integer, ForeignKey("nav_tabs.id", ondelete="CASCADE"), nullable=True)
    tab_id = Column(Integer, ForeignKey("tabs.id", ondelete="CASCADE"), nullable=True)
    component_id = Column(Integer, ForeignKey("components.id", ondelete="CASCADE"), nullable=True)

    # ---- the principal arc: the pair, or a user ---------------------
    #
    # RESTRICT on both halves of the pair, the same discipline and for the
    # same reason as role_assignments: deleting a role or scope that is still
    # granted on a node must not silently revoke access. RESTRICT forces the
    # caller to look at what would be lost. `rbac_service.delete_role` /
    # `delete_scope` pre-count these so the refusal is a named 409 with a
    # count rather than an opaque IntegrityError from the FK.
    #
    # Not indexed individually: `ix_resource_grants_role_scope` below leads
    # with role_id and serves both the read path and the RESTRICT check.
    # scope_id gets its own, because that composite cannot serve it.
    role_id = Column(Integer, ForeignKey("roles.id", ondelete="RESTRICT"), nullable=True)
    scope_id = Column(
        Integer, ForeignKey("scopes.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    # CASCADE, not RESTRICT — the mirror of the pair above, and deliberately
    # the opposite. Deleting a PERSON should take their personal grants with
    # them, exactly as it takes their assignments; there is nothing for an
    # admin to review, because the principal itself is gone. D5 keeps this
    # form: `automations@renphil.org` depends on it today through
    # DEFAULT_ACCESS_CONTROL.
    user_id = Column(
        Integer, ForeignKey("hub_users.id", ondelete="CASCADE"), nullable=True, index=True
    )

    # 'view' | 'edit'. See GRANT_LEVELS above for why this is a row rather
    # than two columns.
    level = Column(String(16), nullable=False)

    # ---- provenance -------------------------------------------------
    #
    # Two columns rather than one, the same pattern and the same reasoning as
    # role_assignments: the FK is joinable and aggregable ("show me everything
    # Alice granted"), and the email snapshot beside it is what survives the
    # SET NULL when that admin's row is deleted. §6.2's revoke-time
    # confirmation reads both — "granted directly by Sam, 3 Feb" is the whole
    # point of the affordance, and it has to keep working after Sam leaves.
    granted_by_user_id = Column(
        Integer, ForeignKey("hub_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Immutable snapshot of the granter's email as it read at grant time.
    # Never updated, never an FK — it has to outlive the row above.
    granted_by_email = Column(String(320), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(_EXACTLY_ONE_NODE, name="ck_resource_grants_one_node"),
        CheckConstraint(_EXACTLY_ONE_PRINCIPAL, name="ck_resource_grants_one_principal"),
        # Keeps `level` to the two the predicate knows how to read. A third
        # value would not error anywhere — it would simply never match, which
        # is a grant that silently does nothing.
        CheckConstraint(
            "level IN ('{}')".format("', '".join(GRANT_LEVELS)),
            name="ck_resource_grants_level",
        ),
        # §7's read-path index: "what can this principal reach?". Leads with
        # role_id, so it also serves the role half of the RESTRICT pre-count.
        # The read path's wide SQL filter (§8.1 step 2) is the hot query here.
        Index("ix_resource_grants_role_scope", "role_id", "scope_id"),
        *_uniqueness_indexes(),
    )
