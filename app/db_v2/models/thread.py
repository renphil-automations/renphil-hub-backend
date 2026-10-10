"""Thread widget data model (plan_thread_widget_2026-08-17.md §3) — Phase 1.

Three of the plan's four new tables: ``threads``, ``thread_comments`` and
``thread_votes``. The fourth (``notifications``) lives in ``notification.py``
so a caller that only needs the discussion-board shape doesn't have to import
the notification model too. ``thread_revisions`` (a published thread's
version log, including staged edits awaiting review —
plan_thread_edit_versioning_2026-09-22.md §2, amended 2026-09-23) belongs to
the thread and lives here beside it.

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
    text,
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
    ``status`` gained a real moderation lifecycle in
    ``plan_thread_moderation_2026-09-18.md`` §2 (phase 2a): ``'pending'`` for
    a viewer's post awaiting an editor's decision, ``'approved'`` for an
    editor's own post or a decided pending one, ``'rejected'`` for a
    declined one — never any other value (``ck_threads_status``).
    ``reviewed_by_email``/``reviewed_by_name``/``reviewed_at`` are NULL for
    every pre-phase-2a row and for an auto-approved editor post, and set
    together the moment an editor decides (plan §5.3) — a thread's own
    identity columns follow the same email-is-identity /
    name-is-a-display-snapshot split as ``author_email``/``author_name``
    (plan §3.6).
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

    # D5's moderation hook. 'pending' for a viewer's post, 'approved' for an
    # editor's own post or a decided pending one, 'rejected' for a declined
    # one (plan_thread_moderation_2026-09-18.md §2, §3.1). The CHECK below
    # is new; the column itself is unchanged from Phase 1.
    status = Column(String(16), nullable=False, default="approved")

    # Moderation decision (plan_thread_moderation_2026-09-18.md §3.1). All
    # three NULL for an auto-approved editor post and for every pre-phase-2a
    # row — M5: History lists only threads with an EXPLICIT decision. Email
    # is identity, name is a display snapshot — same terms as
    # author_email/author_name above (plan §3.6).
    reviewed_by_email = Column(String(320), nullable=True)
    reviewed_by_name = Column(String(255), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)

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

    # The version of the content currently live in title/content/mentions
    # (plan_thread_edit_versioning_2026-09-22.md §1.2, amended 2026-09-23).
    # 1 for every new thread and every pre-versioning row; once the thread
    # is published, the thread_revisions row at this version holds exactly
    # the live content. Only ever moves forward — approving an author's
    # revision or an editor's direct edit sets this to that revision's
    # version, and every still-pending revision below it is `overwritten`
    # first (explicitly, never silently), so a pending revision always has
    # version > this. `server_default` so existing rows and the migration
    # agree; `default` so the ORM sets it on insert without a refresh.
    version = Column(Integer, nullable=False, default=1, server_default=text("1"))

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
        # The status lifecycle (plan §2) — no other value is ever written.
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_threads_status",
        ),
        # The moderation queue: WHERE component_id IN (...) AND status =
        # 'pending' ORDER BY created_at, id. PARTIAL — only pending rows, so
        # it stays tiny however large the table grows (same reasoning as
        # notifications' own unread-only index). sqlite_where mirrors
        # postgresql_where so the SQLite test harness exercises the same
        # shape the live Postgres index does.
        Index(
            "ix_threads_pending_created_id",
            "component_id",
            "created_at",
            "id",
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        # History: WHERE reviewed_at IS NOT NULL ORDER BY reviewed_at DESC,
        # id DESC. PARTIAL on reviewed rows only — a legacy or editor
        # auto-approved thread never appears in history (M5) and never
        # bloats this index either.
        Index(
            "ix_threads_reviewed_at_id",
            reviewed_at.desc(),
            id.desc(),
            postgresql_where=text("reviewed_at IS NOT NULL"),
            sqlite_where=text("reviewed_at IS NOT NULL"),
        ),
    )


class ThreadRevisionV2(BaseV2):
    """One version of a PUBLISHED thread (plan_thread_edit_versioning_
    2026-09-22.md §2.1, amended 2026-09-23). The table is the thread's full
    version log; ``threads.title``/``content``/``mentions`` is a cache of the
    row at ``version = threads.version``.

    ``origin`` says where a row came from: ``'original'`` is v1, written
    when the thread is PUBLISHED (at create for an editor's post, inside
    ``decide_thread`` on approval for a viewer's — a pending or rejected
    thread has no rows at all); ``'author'`` is a non-editor author's staged
    edit, the only origin ever ``pending``; ``'editor'`` is an editor-
    author's direct edit, auto-approved.

    ``status`` is its own four-value lifecycle — ``'pending'`` /
    ``'approved'`` / ``'rejected'`` / ``'overwritten'`` — deliberately NOT
    ``ck_threads_status``: the thread's own state machine and an edit's are
    two separate machines that share vocabulary and nothing else, and
    ``threads.status`` never moves while a revision is decided (plan §1.1).
    Many pending revisions per thread are legal (owner decision 1) — there
    is NO "at most one pending" index.

    Same cascade rule as every other table in this module (see the module
    docstring): the FK's ``ON DELETE CASCADE`` removes a thread's revisions
    with it — never application code.
    """

    __tablename__ = "thread_revisions"

    id = Column(Integer, primary_key=True, index=True)
    # No `index=True` — uq_thread_revisions_thread_version below leads with
    # thread_id and covers every per-thread lookup, same reasoning as
    # ThreadV2.component_id.
    thread_id = Column(
        Integer,
        ForeignKey("threads.id", ondelete="CASCADE"),
        nullable=False,
    )
    version = Column(Integer, nullable=False)

    # A COMPLETE snapshot, never a diff — simpler to store, simpler to
    # render, and the only representation `threads` itself has.
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    mentions = Column(JSONB, nullable=False, default=list)

    status = Column(String(16), nullable=False)
    origin = Column(String(16), nullable=False)

    # Only the thread's own author can submit (`_require_author`), so this
    # is technically derivable — stored anyway, because author_email on
    # `threads` is a plain column for the same reason (email is identity,
    # name is a display snapshot; plan_thread_widget §3.6).
    submitted_by_email = Column(String(320), nullable=False)
    submitted_by_name = Column(String(255), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=False)

    # Set together on EVERY non-pending row. The deciding editor for an
    # author edit; on an `overwritten` row, the editor whose approval (or
    # direct edit) displaced it, and that instant; on an auto-approved row,
    # whoever published it — the approving editor for a viewer's v1, the
    # editor themselves for their own post or direct edit.
    reviewed_by_email = Column(String(320), nullable=True)
    reviewed_by_name = Column(String(255), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'overwritten')",
            name="ck_thread_revisions_status",
        ),
        CheckConstraint(
            "origin IN ('original', 'author', 'editor')",
            name="ck_thread_revisions_origin",
        ),
        # Only an author's staged edit is ever decided; v1 and an editor's
        # edit are published the moment they are written. And v1 is exactly
        # the original — no other row can claim version 1.
        CheckConstraint(
            "(origin = 'author' OR status = 'approved') "
            "AND ((origin = 'original') = (version = 1))",
            name="ck_thread_revisions_origin_shape",
        ),
        # Hard backstop for the version race (the allocation itself runs
        # under a FOR UPDATE on the threads row). Also the per-thread lookup
        # index and what MAX(version) reads.
        Index("uq_thread_revisions_thread_version", "thread_id", "version", unique=True),
        # The merged pending queue's own ordering half. PARTIAL — pending
        # revisions are few by nature however large the table grows.
        Index(
            "ix_thread_revisions_pending_submitted_id",
            "submitted_at",
            "id",
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        # History's half, mirroring ix_threads_reviewed_at_id exactly.
        Index(
            "ix_thread_revisions_reviewed_at_id",
            reviewed_at.desc(),
            id.desc(),
            postgresql_where=text("reviewed_at IS NOT NULL"),
            sqlite_where=text("reviewed_at IS NOT NULL"),
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
