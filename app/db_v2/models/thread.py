"""Thread widget data model (plan_thread_widget_2026-08-17.md §3) — Phase 1.

Three of the plan's four new tables: ``threads``, ``thread_comments`` and
``thread_votes``. The fourth (``notifications``) lives in ``notification.py``
so a caller that only needs the discussion-board shape doesn't have to import
the notification model too.

None of these rows are reachable through an ORM ``relationship()`` — every
other model in ``db_v2`` follows the same rule (see ``ComponentV2``'s class
docstring): traversal is always a plain query, and cross-row cleanup on
delete is handled by the database via ``ON DELETE CASCADE``, never by
SQLAlchemy's own cascade machinery. That is load-bearing here specifically:
``update_tab_content_v2`` deletes a removed widget's ``ComponentV2`` row with
a plain ``db.delete(component)`` and has no idea threads exist (plan §1.2,
§11.1) — the FK's ``ondelete="CASCADE"`` is the ONLY thing that cleans up a
thread widget's discussion when the widget itself is deleted.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.db_v2.database import BaseV2

# The ComponentV2.type string a thread widget is saved under. Phase 2 (the
# widget itself) must register the frontend's widget-type union with this
# exact string — see the Phase 1 handoff for why this isn't yet enforced by
# a shared constant on the frontend side.
THREAD_WIDGET_TYPE = "thread"


class ThreadV2(BaseV2):
    """A top-level discussion topic on one thread widget (plan §3.1).

    ``mentions`` is populated starting Phase 3 (plan §5.3) — always ``[]``
    here in Phase 1, since nothing yet validates or resolves an ``@mention``
    token. Each entry is a ``{email, name, token}`` object (plan D13): the
    email is the identity, and the name and token are *display snapshots* of
    how the mention read when it was posted, so a chip renders without a
    lookup. That is not the ``{email, user_id}`` pair dropped 2026-08-19 —
    ``user_id`` was an identity anchor nothing could populate; see plan §3.6.
    ``status`` is always ``'approved'`` in Phase 1 (plan D5) — the
    column exists now, and the list endpoint already filters on it, purely
    so the future moderation queue is a write-path change only, never a
    migration.
    """

    __tablename__ = "threads"

    id = Column(Integer, primary_key=True, index=True)

    # See the module docstring — this FK, not application code, is what
    # deletes a thread widget's discussion when the widget itself is removed.
    #
    # Deliberately NO `index=True`: the composite index in __table_args__
    # leads with component_id, so a single-column index here would be a
    # redundant leftmost prefix — paid for on every insert, never chosen over
    # the composite. Postgres does not require an index on the referencing
    # side of an FK, so the cascade is unaffected.
    component_id = Column(
        Integer,
        ForeignKey("components.id", ondelete="CASCADE"),
        nullable=False,
    )

    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)

    # Lowercased on write (app.services.thread_service). The ONLY identity
    # key — "is this caller the author" is this column against the JWT's
    # email, and nothing else (plan §3.6, rewritten 2026-08-19).
    #
    # Deliberately NOT a foreign key to `users.work_email`, even though
    # emails are unique in practice: the author set is strictly wider than
    # that roster (login gates on email domain only, so a signed-in user with
    # no `users` row is a legitimate author), and the roster is maintained by
    # a sync this repo does not contain — which makes every available FK
    # delete behaviour unacceptable. Plan §3.6 records the full argument,
    # including why ON UPDATE CASCADE does not rescue it.
    author_email = Column(String(320), nullable=False, index=True)
    author_name = Column(String(255), nullable=True)

    # Resolved, validated {email, name, token} entries (plan D13, §5.3).
    # Always [] until Phase 3 exists to populate it.
    mentions = Column(JSONB, nullable=False, default=list)

    # D5's moderation hook. Always 'approved' until the deferred approval
    # workflow (gated on the access-control rework) exists to write anything
    # else.
    status = Column(String(16), nullable=False, default="approved")

    # Two separate columns, never a single net score — see the plan's own
    # note on why (losing "controversial" to a collapsed net number is a
    # render change with two columns but a schema change with one).
    # Recomputed from thread_votes on every vote write (plan §4.4) — never
    # incremented. Same for down_count.
    up_count = Column(Integer, nullable=False, default=0)
    down_count = Column(Integer, nullable=False, default=0)
    # Recomputed from thread_comments on every comment create/delete — same
    # non-incremental discipline as the vote counters.
    comment_count = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), nullable=False)
    # Non-null => render an "edited" marker (D4). Never set on creation.
    edited_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # (component_id, created_at DESC, id DESC) — the one query the list
        # endpoint runs (plan §3.1). The trailing `id` is not decorative: it
        # is what makes the keyset comparison in thread_service.list_threads
        # a pure index range scan even when many rows share one created_at
        # (plan §4.3 — every row inserted in one transaction ties exactly).
        Index(
            "ix_threads_component_created_id",
            "component_id",
            created_at.desc(),
            id.desc(),
        ),
    )


class ThreadCommentV2(BaseV2):
    """A comment on one ``ThreadV2`` (plan §3.2). Same shape as a thread
    minus ``title``, ``comment_count`` and ``status`` — comments have no
    title (D8), no nested replies to count, and no moderation queue of
    their own (only threads gate visibility)."""

    __tablename__ = "thread_comments"

    id = Column(Integer, primary_key=True, index=True)

    # No `index=True`, for the same reason as ThreadV2.component_id: the
    # composite in __table_args__ leads with thread_id and covers it.
    thread_id = Column(
        Integer,
        ForeignKey("threads.id", ondelete="CASCADE"),
        nullable=False,
    )

    content = Column(Text, nullable=False)

    # Same terms as ThreadV2 — email is the only identity key, no FK.
    author_email = Column(String(320), nullable=False, index=True)
    author_name = Column(String(255), nullable=True)

    mentions = Column(JSONB, nullable=False, default=list)

    up_count = Column(Integer, nullable=False, default=0)
    down_count = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), nullable=False)
    edited_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # (thread_id, created_at ASC, id ASC) — comments read oldest-first
        # inside a thread (plan §3.2), the mirror image of ThreadV2's own
        # index; same tied-timestamp reasoning applies.
        Index(
            "ix_thread_comments_thread_created_id",
            "thread_id",
            "created_at",
            "id",
        ),
    )


class ThreadVoteV2(BaseV2):
    """One user's vote on either a thread or a comment (plan §3.3).

    A single table for both targets via two nullable FKs plus a check
    constraint, rather than a polymorphic ``target_type`` string — this
    keeps real referential integrity (each FK's own ``ON DELETE CASCADE``
    does the cleanup) and needs no application-level "which table does this
    row belong to" branch anywhere except the two callers that already know
    which one they're voting on.
    """

    __tablename__ = "thread_votes"

    id = Column(Integer, primary_key=True, index=True)

    thread_id = Column(
        Integer, ForeignKey("threads.id", ondelete="CASCADE"), nullable=True
    )
    comment_id = Column(
        Integer, ForeignKey("thread_comments.id", ondelete="CASCADE"), nullable=True
    )

    voter_email = Column(String(320), nullable=False)
    # +1 or -1 only (check constraint below). A cleared vote DELETEs this
    # row — there is no stored 0 (plan §3.3).
    value = Column(SmallInteger, nullable=False)

    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "(thread_id IS NOT NULL) <> (comment_id IS NOT NULL)",
            name="ck_thread_votes_exactly_one_target",
        ),
        CheckConstraint("value IN (-1, 1)", name="ck_thread_votes_value"),
        # Two PARTIAL unique indexes, not one plain multi-column unique
        # index — NULLs are distinct in Postgres (and in SQLite), so a plain
        # unique index on (thread_id, comment_id, voter_email) would let the
        # same voter insert unlimited rows as long as the other target stays
        # NULL. `sqlite_where` mirrors `postgresql_where` so the constraint
        # is real (and testable) in both the production DB and the SQLite
        # test harness, not just in production.
        Index(
            "uq_thread_votes_thread_voter",
            "thread_id",
            "voter_email",
            unique=True,
            postgresql_where=thread_id.isnot(None),
            sqlite_where=thread_id.isnot(None),
        ),
        Index(
            "uq_thread_votes_comment_voter",
            "comment_id",
            "voter_email",
            unique=True,
            postgresql_where=comment_id.isnot(None),
            sqlite_where=comment_id.isnot(None),
        ),
        # Backs the recompute-from-source scan in thread_service.cast_vote
        # (plan §4.4) — one index per target column, since a thread's
        # recompute filters on thread_id and a comment's on comment_id.
        Index("ix_thread_votes_thread_value", "thread_id", "value"),
        Index("ix_thread_votes_comment_value", "comment_id", "value"),
    )
