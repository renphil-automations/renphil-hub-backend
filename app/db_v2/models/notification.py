"""The ``notifications`` table (plan_thread_widget_2026-08-17.md §3.4).

Split from ``thread.py`` so a caller that only needs the discussion-board
shape isn't forced to import the notification model too.

**Phase 1 note:** this table is created now (the plan's own creation script,
§3.5, bundles all four tables together, and §9 explicitly allows it: "Table
already exists from Phase 1 if you prefer"), but nothing in Phase 1 writes
to it — no endpoint, no fan-out. Mention/comment notifications are Phase 3/4
work (plan §5.5, §7). Having the table exist early costs nothing and saves
Phase 4 a migration.
"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String

from app.db_v2.database import BaseV2


class NotificationV2(BaseV2):
    """One fan-out event: someone was @-mentioned, or someone commented on
    their thread (D6 — the only two triggers, no others planned)."""

    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)

    # The only identity key (plan §3.6) — same terms as
    # ThreadV2.author_email, and no FK to `users` for the same reasons. An
    # email change strands this row's unread notifications until
    # `scripts/repair_thread_counters.py --rekey-email` rewrites it; that
    # cost is accepted deliberately (plan §3.6, §12.10).
    recipient_email = Column(String(320), nullable=False, index=True)

    # 'mention' | 'thread_comment' (D6).
    type = Column(String(32), nullable=False)

    actor_email = Column(String(320), nullable=True)
    actor_name = Column(String(255), nullable=True)

    # Navigation target resolves through this to the widget's `link`
    # (plan §1.5, §7.3).
    component_id = Column(
        Integer, ForeignKey("components.id", ondelete="CASCADE"), nullable=False
    )
    thread_id = Column(
        Integer, ForeignKey("threads.id", ondelete="CASCADE"), nullable=False
    )
    # Set only when the mention/comment that produced this notification was
    # itself a comment, not the thread's own body.
    comment_id = Column(
        Integer, ForeignKey("thread_comments.id", ondelete="CASCADE"), nullable=True
    )

    # Plain-text snippet for the panel row — produced by the bounded
    # stripper (plan §4.8), never by a markdown parser. Phase 1 writes
    # nothing here.
    excerpt = Column(String(200), nullable=True)

    # NULL => unread. Never DECRemented/deleted — see plan §4.5 on why the
    # unread count is derived (COUNT(*) WHERE read_at IS NULL) rather than
    # cached.
    read_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # The panel list (plan §3.4).
        Index(
            "ix_notifications_recipient_read_created",
            "recipient_email",
            "read_at",
            created_at.desc(),
        ),
        # PARTIAL — holds only unread rows, so it stays small regardless of
        # table growth (plan §3.4, §4.5). `sqlite_where` mirrors
        # `postgresql_where` for the same reason as ThreadVoteV2's partial
        # unique indexes: real enforcement/behaviour in the test harness too.
        Index(
            "ix_notifications_recipient_unread",
            "recipient_email",
            postgresql_where=read_at.is_(None),
            sqlite_where=read_at.is_(None),
        ),
    )
