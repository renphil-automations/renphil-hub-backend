"""Thread widget service layer (plan_thread_widget_2026-08-17.md) — Phase 1.

Threads + comments + votes: access-checked, `link`-addressed reads/writes,
keyset pagination, and recompute-from-source counters. Mentions (plan §5)
and notifications (plan §3.4, §4.5, §7) are Phase 3/4 — nothing here writes
a `mentions` array or a `notifications` row (see app/schemas/thread.py's
module docstring for why the request models don't even accept one yet).

Every public function here is a thin, synchronous, DB-session-bound unit —
each is called from the router via a single `asyncio.to_thread(...)` per
request (plan §4.6: "every DB call through asyncio.to_thread" — today this
only saves the caller from blocking on its own request, but once Fluid
compute lands for Phase 5 (§13.2) a blocking call in an `async def` handler
stalls every OTHER concurrent request on that instance, so it is written
correctly from the start rather than retrofitted later). Raising
`HTTPException` directly from a service function (not just from the router)
matches this codebase's existing precedent in `user_db_service.py`; it
propagates through `await asyncio.to_thread(...)` exactly like any other
exception.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, tuple_
from sqlalchemy.orm import Session

from app.db_v2.models.component import ComponentV2
from app.db_v2.models.notification import NotificationV2
from app.db_v2.models.thread import (
    THREAD_WIDGET_TYPE,
    ThreadCommentV2,
    ThreadV2,
    ThreadVoteV2,
)
from app.models.auth import UserInfo
from app.schemas.thread import (
    CommentListResponse,
    CommentSummary,
    ThreadDetail,
    ThreadListResponse,
    ThreadSummary,
    VoteResponse,
)
from app.services.tab_service import HUB_ADMIN_ROLE, _user_can_view_widget

logger = logging.getLogger(__name__)

THREADS_PAGE_SIZE = 20
COMMENTS_PAGE_SIZE = 50

# Thread status values (D5). Only 'approved' is ever written in Phase 1 —
# the moderation workflow that would write anything else is deferred,
# pending the access-control rework.
THREAD_STATUS_APPROVED = "approved"


# ---------------------------------------------------------
# Small internals
# ---------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def _encode_cursor(created_at: datetime, row_id: int) -> str:
    """Opaque base64 of the last row's (created_at, id) — plan §4.3. Treat
    the wire value as meaningless; only `_decode_cursor` below interprets
    it."""
    payload = json.dumps([created_at.isoformat(), row_id])
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        iso, row_id = json.loads(raw)
        parsed = datetime.fromisoformat(iso)
        return parsed, int(row_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid cursor"
        ) from exc


# ---------------------------------------------------------
# Component / row resolution
# ---------------------------------------------------------


def _get_thread_widget_component(db: Session, link: str) -> ComponentV2 | None:
    """Resolves a `link` to its ComponentV2 row, but only when that
    component actually IS a thread widget — mirrors
    `gridstack_service._airtable_component_by_link`'s "wrong type reads as
    not found" convention, so pointing a thread request at, say, an
    Airtable widget's link 404s instead of quietly creating orphaned rows
    under it."""
    link = (link or "").strip()
    if not link:
        return None
    component = db.query(ComponentV2).filter(ComponentV2.link == link).first()
    if component is None or component.type != THREAD_WIDGET_TYPE:
        return None
    return component


def _get_component(db: Session, component_id: int) -> ComponentV2 | None:
    return db.query(ComponentV2).filter(ComponentV2.id == component_id).first()


def _get_thread(db: Session, thread_id: int) -> ThreadV2 | None:
    return db.query(ThreadV2).filter(ThreadV2.id == thread_id).first()


def _get_comment(db: Session, comment_id: int) -> ThreadCommentV2 | None:
    return db.query(ThreadCommentV2).filter(ThreadCommentV2.id == comment_id).first()


def _require_thread_and_component(
    db: Session, thread_id: int
) -> tuple[ThreadV2, ComponentV2]:
    thread = _get_thread(db, thread_id)
    if thread is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found")
    component = _get_component(db, thread.component_id)
    if component is None:
        # The FK is NOT NULL — this would mean the component was deleted
        # without the cascade running (should not happen; defensive only).
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found")
    return thread, component


def _require_comment_thread_and_component(
    db: Session, comment_id: int
) -> tuple[ThreadCommentV2, ThreadV2, ComponentV2]:
    comment = _get_comment(db, comment_id)
    if comment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Comment not found")
    thread, component = _require_thread_and_component(db, comment.thread_id)
    return comment, thread, component


# ---------------------------------------------------------
# Access control — plan §4.1: view gates read+post, authorship gates edit,
# Hub Admin gates delete. Same check shape as airtable.py:417-430 (plan
# §1.4): Hub Admins bypass; otherwise a widget with no explicit
# access_control is open to any authenticated caller (the inherited,
# documented gap — tab-level AC isn't enforced server-side anywhere yet).
# ---------------------------------------------------------


def _check_view_access(component: ComponentV2, user: UserInfo) -> None:
    roles = list(user.roles)
    widget_ac = component.access_control
    if widget_ac and HUB_ADMIN_ROLE not in roles:
        if not _user_can_view_widget(widget_ac, user.email, roles):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "You do not have access to this discussion",
            )


def _require_hub_admin(user: UserInfo) -> None:
    if HUB_ADMIN_ROLE not in list(user.roles):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Hub Admin access required")


def _require_author(item_author_email: str, user: UserInfo) -> None:
    """D4: authors may edit their own thread/comment. Deliberately no Hub
    Admin bypass here — D3/D4 split edit (author-only) from delete
    (admin-only) cleanly, and the plan never says an admin may edit
    someone else's post. If that turns out to be wanted, it's a one-line
    change here, called out in the Phase 1 handoff."""
    if _norm_email(item_author_email) != _norm_email(user.email):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only the author may edit this"
        )


# ---------------------------------------------------------
# Vote lookups — batched for list endpoints, single for detail responses.
# ---------------------------------------------------------


def _my_votes_for_threads(
    db: Session, thread_ids: list[int], caller_email: str
) -> dict[int, int]:
    if not thread_ids:
        return {}
    email = _norm_email(caller_email)
    rows = (
        db.query(ThreadVoteV2.thread_id, ThreadVoteV2.value)
        .filter(ThreadVoteV2.thread_id.in_(thread_ids), ThreadVoteV2.voter_email == email)
        .all()
    )
    return {tid: value for tid, value in rows}


def _my_vote_for_thread(db: Session, thread_id: int, caller_email: str) -> int:
    return _my_votes_for_threads(db, [thread_id], caller_email).get(thread_id, 0)


def _my_votes_for_comments(
    db: Session, comment_ids: list[int], caller_email: str
) -> dict[int, int]:
    if not comment_ids:
        return {}
    email = _norm_email(caller_email)
    rows = (
        db.query(ThreadVoteV2.comment_id, ThreadVoteV2.value)
        .filter(
            ThreadVoteV2.comment_id.in_(comment_ids), ThreadVoteV2.voter_email == email
        )
        .all()
    )
    return {cid: value for cid, value in rows}


def _my_vote_for_comment(db: Session, comment_id: int, caller_email: str) -> int:
    return _my_votes_for_comments(db, [comment_id], caller_email).get(comment_id, 0)


# ---------------------------------------------------------
# Response builders
# ---------------------------------------------------------


def _to_thread_summary(thread: ThreadV2, my_vote: int) -> ThreadSummary:
    return ThreadSummary(
        id=thread.id,
        component_id=thread.component_id,
        title=thread.title,
        author_email=thread.author_email,
        author_name=thread.author_name,
        status=thread.status,
        up_count=thread.up_count,
        down_count=thread.down_count,
        comment_count=thread.comment_count,
        created_at=thread.created_at,
        edited_at=thread.edited_at,
        my_vote=my_vote,
    )


def _to_thread_detail(thread: ThreadV2, my_vote: int) -> ThreadDetail:
    return ThreadDetail(
        **_to_thread_summary(thread, my_vote).model_dump(),
        content=thread.content,
    )


def _to_comment_summary(comment: ThreadCommentV2, my_vote: int) -> CommentSummary:
    return CommentSummary(
        id=comment.id,
        thread_id=comment.thread_id,
        content=comment.content,
        author_email=comment.author_email,
        author_name=comment.author_name,
        up_count=comment.up_count,
        down_count=comment.down_count,
        created_at=comment.created_at,
        edited_at=comment.edited_at,
        my_vote=my_vote,
    )


# ---------------------------------------------------------
# Threads
# ---------------------------------------------------------


def list_threads_for_link(
    db: Session, link: str, cursor: str | None, user: UserInfo
) -> ThreadListResponse:
    component = _get_thread_widget_component(db, link)
    if component is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread widget not found")
    _check_view_access(component, user)

    query = db.query(ThreadV2).filter(
        ThreadV2.component_id == component.id,
        ThreadV2.status == THREAD_STATUS_APPROVED,
    )
    if cursor:
        after_created_at, after_id = _decode_cursor(cursor)
        # A row comparison, not two ANDed predicates (plan §4.3, finding
        # E10) — `created_at <= X AND id < Y` silently drops any row with an
        # earlier timestamp but a higher id, which tied-timestamp inserts
        # (every row from one transaction — plan §4.3) make a real case, not
        # a theoretical one.
        query = query.filter(
            tuple_(ThreadV2.created_at, ThreadV2.id) < (after_created_at, after_id)
        )

    rows = (
        query.order_by(ThreadV2.created_at.desc(), ThreadV2.id.desc())
        .limit(THREADS_PAGE_SIZE + 1)
        .all()
    )
    has_more = len(rows) > THREADS_PAGE_SIZE
    page = rows[:THREADS_PAGE_SIZE]

    my_votes = _my_votes_for_threads(db, [t.id for t in page], user.email)
    items = [_to_thread_summary(t, my_votes.get(t.id, 0)) for t in page]
    next_cursor = _encode_cursor(page[-1].created_at, page[-1].id) if has_more and page else None

    return ThreadListResponse(items=items, next_cursor=next_cursor)


def create_thread_for_link(
    db: Session, link: str, user: UserInfo, title: str, content: str
) -> ThreadDetail:
    component = _get_thread_widget_component(db, link)
    if component is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread widget not found")
    _check_view_access(component, user)

    now = _utc_now()
    thread = ThreadV2(
        component_id=component.id,
        title=title,
        content=content,
        author_email=_norm_email(user.email),
        author_name=user.name,
        mentions=[],
        status=THREAD_STATUS_APPROVED,
        up_count=0,
        down_count=0,
        comment_count=0,
        created_at=now,
    )
    db.add(thread)
    db.commit()
    db.refresh(thread)
    return _to_thread_detail(thread, my_vote=0)


def update_thread_by_id(
    db: Session, thread_id: int, user: UserInfo, title: str | None, content: str | None
) -> ThreadDetail:
    thread, component = _require_thread_and_component(db, thread_id)
    # A caller who has lost view access to the widget since posting must
    # not still be able to edit through this endpoint (defense in depth —
    # not spelled out explicitly in the plan, called out in the handoff).
    _check_view_access(component, user)
    _require_author(thread.author_email, user)

    # Only stamp "edited" when something was actually supplied to change —
    # an empty PATCH ({} — both fields omitted) must not show an "edited"
    # marker for an edit that never happened.
    changed = False
    if title is not None:
        thread.title = title
        changed = True
    if content is not None:
        thread.content = content
        changed = True
    if changed:
        thread.edited_at = _utc_now()

    db.commit()
    db.refresh(thread)
    my_vote = _my_vote_for_thread(db, thread.id, user.email)
    return _to_thread_detail(thread, my_vote)


def delete_thread_by_id(db: Session, thread_id: int, user: UserInfo) -> None:
    _require_hub_admin(user)
    thread = _get_thread(db, thread_id)
    if thread is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found")
    # Plain ORM delete — cascading to comments/votes/notifications is the
    # database's job via ON DELETE CASCADE (see thread.py's module
    # docstring), not SQLAlchemy relationship cascade.
    db.delete(thread)
    db.commit()


# ---------------------------------------------------------
# Comments
# ---------------------------------------------------------


def list_comments_for_thread(
    db: Session, thread_id: int, cursor: str | None, user: UserInfo
) -> CommentListResponse:
    _thread, component = _require_thread_and_component(db, thread_id)
    _check_view_access(component, user)

    query = db.query(ThreadCommentV2).filter(ThreadCommentV2.thread_id == thread_id)
    if cursor:
        after_created_at, after_id = _decode_cursor(cursor)
        # Oldest-first, so the comparison direction flips relative to the
        # threads list (plan §4.3).
        query = query.filter(
            tuple_(ThreadCommentV2.created_at, ThreadCommentV2.id)
            > (after_created_at, after_id)
        )

    rows = (
        query.order_by(ThreadCommentV2.created_at.asc(), ThreadCommentV2.id.asc())
        .limit(COMMENTS_PAGE_SIZE + 1)
        .all()
    )
    has_more = len(rows) > COMMENTS_PAGE_SIZE
    page = rows[:COMMENTS_PAGE_SIZE]

    my_votes = _my_votes_for_comments(db, [c.id for c in page], user.email)
    items = [_to_comment_summary(c, my_votes.get(c.id, 0)) for c in page]
    next_cursor = _encode_cursor(page[-1].created_at, page[-1].id) if has_more and page else None

    return CommentListResponse(items=items, next_cursor=next_cursor)


def _recompute_comment_count(db: Session, thread_id: int) -> int:
    return (
        db.query(func.count(ThreadCommentV2.id))
        .filter(ThreadCommentV2.thread_id == thread_id)
        .scalar()
        or 0
    )


def create_comment_for_thread(
    db: Session, thread_id: int, user: UserInfo, content: str
) -> CommentSummary:
    thread, component = _require_thread_and_component(db, thread_id)
    _check_view_access(component, user)

    # Lock the parent thread before the recompute below, same reasoning as
    # the vote recipe (plan §4.4): the recompute's snapshot must be taken
    # after any concurrent comment-create/delete on this same thread has
    # committed, or the two can race to the same (wrong) count.
    db.query(ThreadV2).filter(ThreadV2.id == thread_id).with_for_update().first()

    now = _utc_now()
    comment = ThreadCommentV2(
        thread_id=thread_id,
        content=content,
        author_email=_norm_email(user.email),
        author_name=user.name,
        mentions=[],
        up_count=0,
        down_count=0,
        created_at=now,
    )
    db.add(comment)
    db.flush()

    thread.comment_count = _recompute_comment_count(db, thread_id)

    db.commit()
    db.refresh(comment)
    return _to_comment_summary(comment, my_vote=0)


def update_comment_by_id(
    db: Session, comment_id: int, user: UserInfo, content: str | None
) -> CommentSummary:
    comment, _thread, component = _require_comment_thread_and_component(db, comment_id)
    _check_view_access(component, user)
    _require_author(comment.author_email, user)

    # Same "only stamp edited when something changed" rule as
    # update_thread_by_id.
    if content is not None:
        comment.content = content
        comment.edited_at = _utc_now()

    db.commit()
    db.refresh(comment)
    my_vote = _my_vote_for_comment(db, comment.id, user.email)
    return _to_comment_summary(comment, my_vote)


def delete_comment_by_id(db: Session, comment_id: int, user: UserInfo) -> None:
    _require_hub_admin(user)
    comment = _get_comment(db, comment_id)
    if comment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Comment not found")
    thread_id = comment.thread_id

    thread = db.query(ThreadV2).filter(ThreadV2.id == thread_id).with_for_update().first()

    db.delete(comment)
    db.flush()

    if thread is not None:
        thread.comment_count = _recompute_comment_count(db, thread_id)

    db.commit()


# ---------------------------------------------------------
# Votes — recomputed, never incremented (plan §4.4, landmine §11.9).
# ---------------------------------------------------------


def _recompute_vote_counts(db: Session, *, thread_id: int | None, comment_id: int | None) -> tuple[int, int]:
    """FILTER-based single-pass aggregate over `thread_votes` (plan §4.4):
    one query, one index scan, both counts. Idempotent by construction —
    recomputing from source rather than applying a delta is what makes a
    retried or double-clicked vote write leave the same result as a single
    write (finding E2)."""
    query = db.query(
        func.count(ThreadVoteV2.id).filter(ThreadVoteV2.value == 1),
        func.count(ThreadVoteV2.id).filter(ThreadVoteV2.value == -1),
    )
    if thread_id is not None:
        query = query.filter(ThreadVoteV2.thread_id == thread_id)
    else:
        query = query.filter(ThreadVoteV2.comment_id == comment_id)
    up_count, down_count = query.one()
    return int(up_count or 0), int(down_count or 0)


def _cast_vote(
    db: Session,
    *,
    target: ThreadV2 | ThreadCommentV2,
    thread_id: int | None,
    comment_id: int | None,
    user: UserInfo,
    value: int,
) -> VoteResponse:
    if _norm_email(target.author_email) == _norm_email(user.email):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "You cannot vote on your own post"
        )

    email = _norm_email(user.email)
    vote_query = db.query(ThreadVoteV2).filter(ThreadVoteV2.voter_email == email)
    vote_query = (
        vote_query.filter(ThreadVoteV2.thread_id == thread_id)
        if thread_id is not None
        else vote_query.filter(ThreadVoteV2.comment_id == comment_id)
    )
    existing_vote = vote_query.first()

    if value == 0:
        if existing_vote is not None:
            db.delete(existing_vote)
            db.flush()
    else:
        now = _utc_now()
        if existing_vote is not None:
            existing_vote.value = value
            existing_vote.updated_at = now
        else:
            db.add(
                ThreadVoteV2(
                    thread_id=thread_id,
                    comment_id=comment_id,
                    voter_email=email,
                    value=value,
                    created_at=now,
                    updated_at=now,
                )
            )
        db.flush()

    up_count, down_count = _recompute_vote_counts(
        db, thread_id=thread_id, comment_id=comment_id
    )
    # Mutate the already-loaded ORM object directly rather than a bulk
    # `Query.update()` — matches this codebase's convention elsewhere
    # (access_control_service.write_ac, gridstack_service) and sidesteps
    # `synchronize_session` entirely: the row `target` refers to is the
    # SAME identity-mapped object the FOR UPDATE query above resolved to,
    # so this write is guaranteed visible to anything reading `target`
    # later in this session.
    target.up_count = up_count
    target.down_count = down_count

    db.commit()

    return VoteResponse(
        up_count=up_count,
        down_count=down_count,
        my_vote=value,
    )


def vote_on_thread(db: Session, thread_id: int, user: UserInfo, value: int) -> VoteResponse:
    thread, component = _require_thread_and_component(db, thread_id)
    _check_view_access(component, user)

    # Serialize competing writers on this thread BEFORE the recompute runs
    # (plan §4.4) — under READ COMMITTED each statement takes a fresh
    # snapshot, so acquiring this lock first guarantees the recompute's
    # snapshot is taken after any concurrent voter's write has committed.
    db.query(ThreadV2).filter(ThreadV2.id == thread_id).with_for_update().first()

    return _cast_vote(
        db,
        target=thread,
        thread_id=thread_id,
        comment_id=None,
        user=user,
        value=value,
    )


def vote_on_comment(
    db: Session, comment_id: int, user: UserInfo, value: int
) -> VoteResponse:
    comment, _thread, component = _require_comment_thread_and_component(db, comment_id)
    _check_view_access(component, user)

    db.query(ThreadCommentV2).filter(ThreadCommentV2.id == comment_id).with_for_update().first()

    return _cast_vote(
        db,
        target=comment,
        thread_id=None,
        comment_id=comment_id,
        user=user,
        value=value,
    )


# ---------------------------------------------------------
# Repair support (plan §4.4 "this is also the repair path", §3.6's second
# mode) — thin logic used by scripts/repair_thread_counters.py. Kept here,
# not in the script, so it's covered by the same test suite as everything
# else in this module.
# ---------------------------------------------------------


def recompute_all_thread_counters(db: Session) -> list[dict[str, Any]]:
    """Recomputes up_count/down_count/comment_count for every thread from
    source. Returns one entry per thread whose stored value was wrong,
    {thread_id, field, before, after} — never writes when nothing changed,
    so a full re-run against an already-correct table reports an empty
    list (plan §4.4: "the report is the only way a silent miscount ever
    becomes visible")."""
    changes: list[dict[str, Any]] = []
    threads = db.query(ThreadV2).order_by(ThreadV2.id).all()
    for thread in threads:
        up_count, down_count = _recompute_vote_counts(
            db, thread_id=thread.id, comment_id=None
        )
        comment_count = _recompute_comment_count(db, thread.id)

        for field, before, after in (
            ("up_count", thread.up_count, up_count),
            ("down_count", thread.down_count, down_count),
            ("comment_count", thread.comment_count, comment_count),
        ):
            if before != after:
                changes.append(
                    {
                        "thread_id": thread.id,
                        "field": field,
                        "before": before,
                        "after": after,
                    }
                )

        thread.up_count = up_count
        thread.down_count = down_count
        thread.comment_count = comment_count

    comments = db.query(ThreadCommentV2).order_by(ThreadCommentV2.id).all()
    for comment in comments:
        up_count, down_count = _recompute_vote_counts(
            db, thread_id=None, comment_id=comment.id
        )
        for field, before, after in (
            ("up_count", comment.up_count, up_count),
            ("down_count", comment.down_count, down_count),
        ):
            if before != after:
                changes.append(
                    {
                        "comment_id": comment.id,
                        "field": field,
                        "before": before,
                        "after": after,
                    }
                )
        comment.up_count = up_count
        comment.down_count = down_count

    return changes


def rekey_email(
    db: Session, *, old_email: str, new_email: str
) -> dict[str, int]:
    """Identity reconciliation (plan §3.6, §4.4's second mode): rewrite every
    row keyed on `old_email` to `new_email`.

    **Rewritten 2026-08-19.** This took an Airtable USERS record id and
    matched on `author_user_id`/`recipient_user_id`. That anchor is gone
    (plan §3.6): the Postgres `users` table cannot carry the record id, so
    the only thing that could ever have populated the column was a live
    Airtable fetch on every post — and the anchor was redundant here anyway,
    since this operation already requires the old email and every affected
    row is reachable by it.

    What that costs, stated plainly: a row whose email was edited by hand to
    some third value is no longer reachable. Nothing in the codebase edits
    these columns, so that path only exists through direct DB access.

    `mentions` entries whose `email` is `old_email` are rewritten wherever
    they appear, regardless of whose row the mention sits on. Returns counts
    per table, for the script's report."""
    old_norm = _norm_email(old_email)
    new_norm = _norm_email(new_email)
    counts = {"threads": 0, "thread_comments": 0, "notifications": 0, "mentions_rewritten": 0}

    threads = (
        db.query(ThreadV2).filter(ThreadV2.author_email == old_norm).all()
    )
    for thread in threads:
        thread.author_email = new_norm
        counts["threads"] += 1

    comments = (
        db.query(ThreadCommentV2)
        .filter(ThreadCommentV2.author_email == old_norm)
        .all()
    )
    for comment in comments:
        comment.author_email = new_norm
        counts["thread_comments"] += 1

    notifications = (
        db.query(NotificationV2)
        .filter(NotificationV2.recipient_email == old_norm)
        .all()
    )
    for notification in notifications:
        notification.recipient_email = new_norm
        counts["notifications"] += 1

    # Rewrite `mentions` entries wherever the old address appears, across
    # both tables — a mention doesn't require the mentioning row's OWN author
    # to be the rekeyed user, so this is a separate scan from the ones above.
    #
    # Deliberately a full scan rather than a JSONB containment filter: `@>`
    # would push this into the database, but it is Postgres-only and the
    # SQLite test harness could then not exercise this path at all. This is a
    # manual, dry-run-by-default repair script over tables measured in
    # thousands of rows; portability is worth more than the scan here. If
    # these tables ever grow enough for that to stop being true, add a
    # dialect branch rather than dropping the SQLite path.
    # Each entry is a {email, name, token} object (plan D13, §5.3). Only
    # `email` is identity; `name` and `token` are display snapshots of how the
    # mention read when it was posted, so they are deliberately left alone —
    # an address change is not a name change, and rewriting the snapshot would
    # retroactively alter what an existing post renders (plan §5.6).
    #
    # Entries that aren't objects are passed through untouched rather than
    # guessed at. Nothing in this codebase writes any other shape; the guard
    # is here so malformed data degrades to "not rewritten" instead of raising
    # halfway through a repair run.
    for model in (ThreadV2, ThreadCommentV2):
        rows = db.query(model).all()
        for row in rows:
            mentions = row.mentions or []
            if not isinstance(mentions, list):
                continue
            changed = False
            rewritten = []
            for entry in mentions:
                if (
                    isinstance(entry, dict)
                    and _norm_email(entry.get("email", "")) == old_norm
                ):
                    rewritten.append({**entry, "email": new_norm})
                    changed = True
                else:
                    rewritten.append(entry)
            if changed:
                row.mentions = rewritten
                counts["mentions_rewritten"] += 1

    return counts
