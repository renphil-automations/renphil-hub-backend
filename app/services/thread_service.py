"""Thread widget service layer (plan_thread_widget_2026-08-17.md) — Phase 1
(threads/comments/votes) + Phase 3 (mentions) + Phase 4 (notifications).

Threads + comments + votes: access-checked, `link`-addressed reads/writes,
keyset pagination, and recompute-from-source counters. Mentions (plan §5)
are validated and stored — see `derive_mention_token`,
`list_mentionable_users` and `_validate_and_resolve_mentions` below.
Notifications (plan §3.4, §4.5, §5.7, §7) are now live end to end: the
"Notifications" section below fans rows out on thread/comment create and
edit (`_notify_mentions`, plan §5.7's exact trigger rules), generates the
panel excerpt via a bounded, non-backtracking stripper (`
generate_notification_excerpt`, plan §4.8), and serves the read side
(`list_notifications_for_user`, `get_unread_notification_count` — derived
every time, never cached, plan §4.5 — `mark_notification_read`,
`mark_all_notifications_read`). Per-user write rate limiting (plan §4.7
control 2) lives one layer up, in `helpers/rate_limit.py`'s
`@rate_limited(...)` decorator on the router handlers — it gates a request
before it ever reaches this module, so there is nothing to enforce here.

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
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, or_, tuple_
from sqlalchemy.orm import Session

from app.db_v2.models.component import ComponentV2
from app.db_v2.models.notification import NotificationV2
from app.db_v2.models.thread import (
    THREAD_WIDGET_TYPE,
    ThreadCommentV2,
    ThreadV2,
    ThreadVoteV2,
)
from app.db_v2.models.user import UserV2
from app.models.auth import UserInfo
from app.schemas.thread import (
    MAX_MENTIONS_PER_POST,
    CommentListResponse,
    CommentSummary,
    MentionableUser,
    MentionInput,
    NotificationEntry,
    NotificationListResponse,
    ThreadDetail,
    ThreadListResponse,
    ThreadSummary,
    VoteResponse,
)
from app.services.tab_service import HUB_ADMIN_ROLE, _user_can_view_widget

logger = logging.getLogger(__name__)

THREADS_PAGE_SIZE = 20
COMMENTS_PAGE_SIZE = 50
NOTIFICATIONS_PAGE_SIZE = 20

# D6 — the only two notification triggers.
NOTIFICATION_TYPE_MENTION = "mention"
NOTIFICATION_TYPE_THREAD_COMMENT = "thread_comment"

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
# Mentions (plan §5, D13) — directory + server-side validation. The token
# derivation (§5.5) is the ONE function both this module's validation and
# the directory endpoint call — a second, independent implementation
# (e.g. in TypeScript) is exactly the bug §5.5 exists to prevent, which is
# also why the frontend never derives one itself (plan §8 item 16).
# ---------------------------------------------------------


def derive_mention_token(name: str) -> str:
    """plan §5.5: the person's name with everything that isn't a Unicode
    letter or digit removed — `Roy Abdelnour` -> `RoyAbdelnour`. Keeps
    accented letters as-is (no transliteration); drops apostrophes, hyphens,
    periods and spaces, which is what makes the result a single "word" the
    caret regex (frontend) and the word-boundary check (`_token_occurs_in_content`
    below) can both bound. A name that strips to '' derives to '' — callers
    treat that as "not mentionable" (plan §5.5's own note), never as an
    error."""
    return "".join(ch for ch in (name or "") if ch.isalnum())


def _token_occurs_in_content(content: str, token: str) -> bool:
    """plan §5.3 check 2 — "the token ... occurs in content, as `@<token>`
    at a word boundary." The trailing negative lookahead is the boundary:
    without it, a token that is a PREFIX of a longer run of letters/digits
    (`@RoyAbdelnour` inside `@RoyAbdelnourite`) would count as a match.
    `\\w` under Python's default Unicode `re` matches non-ASCII letters
    too, so this holds for the 3 of 148 eligible names with accented
    characters (plan D13's own note) without any extra handling."""
    if not token:
        return False
    return re.search(rf"@{re.escape(token)}(?!\w)", content) is not None


def _fold_for_search(value: str) -> str:
    """Accent-insensitive fold (plan §5.1): NFKD-normalize, drop combining
    marks, casefold. Deliberately Python-side rather than the Postgres
    `unaccent` extension — avoids depending on that extension being
    installed on Neon (plan §5.1's own reasoning)."""
    normalized = unicodedata.normalize("NFKD", value or "")
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return stripped.casefold()


MENTIONABLE_USERS_LIMIT = 8


def _eligible_mentionable_users_query(db: Session):
    """The one eligibility rule (plan D7 as amended by D13, §5.1):
    `EndDate IS NULL OR EndDate > current_date`, `status` deliberately
    ignored, plus the structural exclusion of rows with no `work_email`
    (mentions are email-keyed). Shared by the directory endpoint AND
    mention validation below so the two definitions can never drift apart —
    the plan's own test list (§10) explicitly checks the directory and
    validation agree on who's eligible."""
    today = func.current_date()
    return db.query(UserV2).filter(
        or_(UserV2.end_date.is_(None), UserV2.end_date > today),
        UserV2.work_email.isnot(None),
        UserV2.work_email != "",
    )


def list_mentionable_users(db: Session, q: str | None) -> list[MentionableUser]:
    """`GET /threads/mentionable-users` (plan §5.1). Eligibility filter runs
    in SQL; the accent-insensitive substring/prefix match runs in Python
    over that (small, ≤175-row) result set, per §5.1's own reasoning for why
    that split is the simplest correct thing at this table size."""
    rows = _eligible_mentionable_users_query(db).all()
    q_folded = _fold_for_search(q) if q else ""

    results: list[MentionableUser] = []
    for row in rows:
        token = derive_mention_token(row.name)
        if not token:
            # A name that strips to '' isn't mentionable (plan §5.5) —
            # excluded from the directory rather than shown with a token
            # that could never actually be typed or matched.
            continue
        if q_folded:
            name_hit = q_folded in _fold_for_search(row.name or "")
            token_hit = _fold_for_search(token).startswith(q_folded)
            if not (name_hit or token_hit):
                continue
        results.append(
            MentionableUser(
                token=token,
                name=row.name,
                email=_norm_email(row.work_email),
                headshot_url=row.headshot or None,
            )
        )

    results.sort(key=lambda r: r.name.lower())
    return results[:MENTIONABLE_USERS_LIMIT]


def _validate_and_resolve_mentions(
    db: Session, claimed: list[MentionInput], content: str
) -> list[dict[str, str]]:
    """plan §5.3 — the server does not trust the client's `mentions` array.
    For each claimed email: (1) it must belong to an eligible `users` row
    (the SAME eligibility rule the directory applies — `_eligible_
    mentionable_users_query`), and (2) the token the SERVER derives from
    that row's name must actually occur in `content` as `@<token>` at a
    word boundary. Anything failing either check is silently dropped
    (never a 4xx for the whole request — plan §5.3: "this closes the
    obvious hole"). `name`/`token` on the returned entries always come from
    the matched `users` row, never from `claimed` — a client cannot make a
    chip render someone else's name.

    De-duplicates by normalized email, preserving first-occurrence order
    (plan §5.2: rendering resolves a token collision by "array order,
    first wins", which only means something if this array's order is the
    order the client actually claimed them in, not an arbitrary one a
    `set()` would produce).

    Raises 400 if more than `MAX_MENTIONS_PER_POST` mentions SURVIVE both
    checks (plan §4.7 control 1 / §5.3 item 3) — deliberately counts
    survivors, not the raw claimed count, so a client can't be blocked by
    padding a request with mentions that would be dropped anyway."""
    if not claimed:
        return []

    ordered_emails: list[str] = []
    seen: set[str] = set()
    for entry in claimed:
        email = _norm_email(entry.email)
        if email and email not in seen:
            seen.add(email)
            ordered_emails.append(email)
    if not ordered_emails:
        return []

    eligible_rows = (
        _eligible_mentionable_users_query(db)
        .filter(func.lower(UserV2.work_email).in_(ordered_emails))
        .all()
    )
    eligible_by_email = {_norm_email(row.work_email): row for row in eligible_rows}

    resolved: list[dict[str, str]] = []
    for email in ordered_emails:
        row = eligible_by_email.get(email)
        if row is None:
            continue  # check 1 failed — not an eligible users row
        token = derive_mention_token(row.name)
        if not _token_occurs_in_content(content, token):
            continue  # check 2 failed — token isn't actually in the text
        resolved.append({"email": email, "name": row.name, "token": token})

    if len(resolved) > MAX_MENTIONS_PER_POST:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"A post may mention at most {MAX_MENTIONS_PER_POST} people",
        )
    return resolved


# ---------------------------------------------------------
# Notifications (plan §3.4, §4.5, §4.8, §5.7, §7 — Phase 4). Fan-out on
# write (`_notify_mentions`, called from thread/comment create and edit
# below), the bounded excerpt stripper, and the read side (list, derived
# unread count, mark-read).
# ---------------------------------------------------------

# plan §4.8 rule 1 — bound the input BEFORE any pattern runs. This is the
# step that actually makes the rest safe: every regex below only ever sees
# up to this many code points, regardless of the post's real length (up to
# MAX_CONTENT_LENGTH = 5000), so none of them can be driven quadratic by
# input size even if a pattern were badly chosen.
_EXCERPT_INPUT_BOUND = 400
_EXCERPT_OUTPUT_LENGTH = 200

# plan §4.8 rule 2 — linear, non-backtracking patterns only: character-class
# deletion and anchored line-leading forms. No nested quantifiers, no
# backreferences, no alternation over `.*`. This deliberately does NOT parse
# markdown — a `[text](url)` link strips to `texturl`, and that is accepted
# cosmetic loss ("losing the nuance of link-title syntax costs nothing" —
# plan §4.8), not a bug to fix here.
_EXCERPT_LINE_LEADING_RE = re.compile(r"^[ \t]*(?:[-+*]|\d+\.)[ \t]+", re.MULTILINE)
_EXCERPT_MARKDOWN_CHARS_RE = re.compile(r"[*_`~#>\[\]()]+")
_EXCERPT_WHITESPACE_RE = re.compile(r"\s+")


def generate_notification_excerpt(content: str) -> str:
    """plan §4.8 — bound first, then strip, for a plain-text panel preview
    (never rendered as markdown — the risk this guards against is
    catastrophic backtracking on the write path, not XSS). Python string
    slicing already operates on code points, not bytes or UTF-16 units
    (same unit as §4.2 throughout), so bounding and the final trim are both
    "on a code-point boundary" for free — no separate handling needed to
    avoid corrupting a multi-code-point sequence."""
    bounded = (content or "")[:_EXCERPT_INPUT_BOUND]
    stripped = _EXCERPT_LINE_LEADING_RE.sub("", bounded)
    stripped = _EXCERPT_MARKDOWN_CHARS_RE.sub("", stripped)
    collapsed = _EXCERPT_WHITESPACE_RE.sub(" ", stripped).strip()
    if len(collapsed) <= _EXCERPT_OUTPUT_LENGTH:
        return collapsed
    return collapsed[:_EXCERPT_OUTPUT_LENGTH].rstrip() + "…"


def _notify_mentions(
    db: Session,
    resolved_mentions: list[dict[str, str]],
    *,
    actor: UserInfo,
    component_id: int,
    thread_id: int,
    comment_id: int | None,
    content: str,
) -> set[str]:
    """plan §5.7 — "notify every validated mention except the author (no
    self-pings)". Returns the set of emails actually notified (excluding
    the actor), so a caller creating a comment can tell whether the
    thread's author already received a 'mention' notification on this same
    comment before deciding whether to also send a 'thread_comment' one —
    "one notification per person per event, mention wins" (plan §5.7).

    Does not commit — caller controls the transaction, same convention as
    every other write function in this module."""
    actor_email = _norm_email(actor.email)
    excerpt = generate_notification_excerpt(content)
    notified: set[str] = set()
    for entry in resolved_mentions:
        recipient = _norm_email(entry["email"])
        if recipient == actor_email or recipient in notified:
            # No self-pings; `resolved_mentions` is already de-duped by
            # email (plan §5.3), but a second guard here costs nothing and
            # protects a future caller that passes an un-de-duped list.
            continue
        db.add(
            NotificationV2(
                recipient_email=recipient,
                type=NOTIFICATION_TYPE_MENTION,
                actor_email=actor_email,
                actor_name=actor.name,
                component_id=component_id,
                thread_id=thread_id,
                comment_id=comment_id,
                excerpt=excerpt,
                read_at=None,
                created_at=_utc_now(),
            )
        )
        notified.add(recipient)
    return notified


def _notify_new_thread_comment(
    db: Session,
    *,
    thread_author_email: str,
    actor: UserInfo,
    component_id: int,
    thread_id: int,
    comment_id: int,
    content: str,
    already_notified: set[str],
) -> None:
    """plan §5.7 — "on comment create, additionally notify the thread's
    author — unless they are the commenter, or they were already notified
    by a mention on the same comment". `already_notified` is the return
    value of `_notify_mentions` for this same comment; self-exclusion
    covers the commenter-is-the-author case the same way `_notify_mentions`
    covers self-mentions."""
    thread_author = _norm_email(thread_author_email)
    actor_email = _norm_email(actor.email)
    if thread_author == actor_email or thread_author in already_notified:
        return
    db.add(
        NotificationV2(
            recipient_email=thread_author,
            type=NOTIFICATION_TYPE_THREAD_COMMENT,
            actor_email=actor_email,
            actor_name=actor.name,
            component_id=component_id,
            thread_id=thread_id,
            comment_id=comment_id,
            excerpt=generate_notification_excerpt(content),
            read_at=None,
            created_at=_utc_now(),
        )
    )


def _mention_emails(mentions: list[dict[str, Any]] | None) -> set[str]:
    """Normalized email set of a stored `mentions` JSONB array, tolerant of
    malformed entries (same defensive posture as `rekey_email`'s own scan
    below) — used to diff an edit's mentions against what was already
    there (plan §5.7's "on edit: notify only NEWLY added mentions")."""
    result: set[str] = set()
    for entry in mentions or []:
        if isinstance(entry, dict):
            email = _norm_email(entry.get("email", ""))
            if email:
                result.add(email)
    return result


def _notify_newly_added_mentions(
    db: Session,
    *,
    previous_mentions: list[dict[str, Any]] | None,
    resolved_mentions: list[dict[str, str]],
    actor: UserInfo,
    component_id: int,
    thread_id: int,
    comment_id: int | None,
    content: str,
) -> None:
    """plan §5.7 — "on edit: notify only newly added mentions — diff the
    stored mentions against the incoming set. Editing a post must not
    re-ping everyone already in it." `resolved_mentions` is already the
    validated set for the post's CURRENT (effective) content; this only
    changes which subset of it gets a notification row."""
    previously_notified = _mention_emails(previous_mentions)
    newly_added = [
        entry
        for entry in resolved_mentions
        if _norm_email(entry["email"]) not in previously_notified
    ]
    if newly_added:
        _notify_mentions(
            db,
            newly_added,
            actor=actor,
            component_id=component_id,
            thread_id=thread_id,
            comment_id=comment_id,
            content=content,
        )


def _to_notification_entry(
    notification: NotificationV2, thread_title: str, component_link: str
) -> NotificationEntry:
    return NotificationEntry(
        id=notification.id,
        type=notification.type,
        actor_email=notification.actor_email,
        actor_name=notification.actor_name,
        thread_id=notification.thread_id,
        thread_title=thread_title,
        component_link=component_link,
        excerpt=notification.excerpt,
        read_at=notification.read_at,
        created_at=notification.created_at,
    )


def list_notifications_for_user(
    db: Session, user: UserInfo, cursor: str | None, unread_only: bool
) -> NotificationListResponse:
    """`GET /notifications` (plan §4.1) — self only, newest first, pages of
    `NOTIFICATIONS_PAGE_SIZE`. `thread_title` / `component_link` are joined
    live rather than stored (see `NotificationEntry`'s own docstring for
    why) — safe unconditionally because a notification cannot outlive its
    thread or component (`ON DELETE CASCADE`, plan §3.4)."""
    email = _norm_email(user.email)
    query = (
        db.query(NotificationV2, ThreadV2.title, ComponentV2.link)
        .join(ThreadV2, ThreadV2.id == NotificationV2.thread_id)
        .join(ComponentV2, ComponentV2.id == NotificationV2.component_id)
        .filter(NotificationV2.recipient_email == email)
    )
    if unread_only:
        query = query.filter(NotificationV2.read_at.is_(None))
    if cursor:
        after_created_at, after_id = _decode_cursor(cursor)
        # Same row-comparison rule as threads/comments pagination (plan
        # §4.3, finding E10) — not two ANDed predicates.
        query = query.filter(
            tuple_(NotificationV2.created_at, NotificationV2.id)
            < (after_created_at, after_id)
        )

    rows = (
        query.order_by(NotificationV2.created_at.desc(), NotificationV2.id.desc())
        .limit(NOTIFICATIONS_PAGE_SIZE + 1)
        .all()
    )
    has_more = len(rows) > NOTIFICATIONS_PAGE_SIZE
    page = rows[:NOTIFICATIONS_PAGE_SIZE]

    items = [
        _to_notification_entry(notification, title, link)
        for notification, title, link in page
    ]
    next_cursor = (
        _encode_cursor(page[-1][0].created_at, page[-1][0].id)
        if has_more and page
        else None
    )
    return NotificationListResponse(items=items, next_cursor=next_cursor)


def get_unread_notification_count(db: Session, user: UserInfo) -> tuple[int, str]:
    """`GET /notifications/unread-count` (plan §4.5) — "one query, both
    values", derived every time, never cached. Returns `(count, etag)`; the
    router honours `If-None-Match` against the etag and 304s on a hit. The
    partial index on `(recipient_email) WHERE read_at IS NULL` (plan §3.4)
    is what keeps this an index-only scan over a handful of rows regardless
    of table growth."""
    email = _norm_email(user.email)
    count, max_id = (
        db.query(func.count(NotificationV2.id), func.max(NotificationV2.id))
        .filter(NotificationV2.recipient_email == email, NotificationV2.read_at.is_(None))
        .one()
    )
    count = int(count or 0)
    etag = f'"{count}-{int(max_id) if max_id is not None else 0}"'
    return count, etag


def mark_notification_read(db: Session, notification_id: int, user: UserInfo) -> None:
    """`POST /notifications/{id}/read` (plan §4.5) — idempotent (the
    `read_at IS NULL` predicate makes a repeat call a no-op: 0 rows match,
    nothing changes) and scoped to the recipient (the IDOR guard plan §4.5
    calls out explicitly — without the `recipient_email` predicate, any
    authenticated caller could mark another user's notification read by
    id). Deliberately does not distinguish "already read", "not yours" and
    "does not exist" in its response — all three look identical from the
    caller's side, which is the point of the IDOR guard, not an
    oversight."""
    email = _norm_email(user.email)
    (
        db.query(NotificationV2)
        .filter(
            NotificationV2.id == notification_id,
            NotificationV2.recipient_email == email,
            NotificationV2.read_at.is_(None),
        )
        .update({NotificationV2.read_at: _utc_now()}, synchronize_session=False)
    )
    db.commit()


def mark_all_notifications_read(db: Session, user: UserInfo) -> int:
    """`POST /notifications/read-all` (plan §4.1). Returns the number of
    rows actually flipped, for callers that want it; the endpoint itself
    doesn't need to expose it."""
    email = _norm_email(user.email)
    updated = (
        db.query(NotificationV2)
        .filter(NotificationV2.recipient_email == email, NotificationV2.read_at.is_(None))
        .update({NotificationV2.read_at: _utc_now()}, synchronize_session=False)
    )
    db.commit()
    return int(updated or 0)


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
        mentions=thread.mentions or [],
        # Same generator notifications already use (plan §4.8) — `thread.content`
        # is already loaded on this row regardless (ThreadV2 has no deferred
        # columns), so this costs no extra query, only a few CPU cycles.
        content_excerpt=generate_notification_excerpt(thread.content),
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
        mentions=comment.mentions or [],
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
    db: Session,
    link: str,
    user: UserInfo,
    title: str,
    content: str,
    mentions: list[MentionInput] | None = None,
) -> ThreadDetail:
    component = _get_thread_widget_component(db, link)
    if component is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread widget not found")
    _check_view_access(component, user)

    # Resolve mentions BEFORE constructing the row — a 400 from the cap
    # (plan §4.7) must not leave a half-built thread behind (nothing is
    # `db.add`ed yet at this point).
    resolved = _validate_and_resolve_mentions(db, mentions or [], content)

    now = _utc_now()
    thread = ThreadV2(
        component_id=component.id,
        title=title,
        content=content,
        author_email=_norm_email(user.email),
        author_name=user.name,
        mentions=resolved,
        status=THREAD_STATUS_APPROVED,
        up_count=0,
        down_count=0,
        comment_count=0,
        created_at=now,
    )
    db.add(thread)
    db.flush()  # assigns thread.id — the notifications below FK to it

    # plan §5.7 — "on create: notify every validated mention except the
    # author". Same transaction as the thread insert: a notification must
    # never survive a thread that doesn't (and vice versa).
    _notify_mentions(
        db,
        resolved,
        actor=user,
        component_id=component.id,
        thread_id=thread.id,
        comment_id=None,
        content=content,
    )

    db.commit()
    db.refresh(thread)
    return _to_thread_detail(thread, my_vote=0)


def get_thread_by_id(db: Session, thread_id: int, user: UserInfo) -> ThreadDetail:
    """Fetch one thread's full content — any viewer with the widget's own
    view access, NOT author-only (session handoff 2026-08-20 §0/addendum:
    the Phase 2 frontend worked around this endpoint not existing by
    empty-PATCH'ing as the author, which only ever closed the gap for a
    thread's own author). Gated the same way `list_threads_for_link` gates
    the list — including the same `status == approved` filter, so a future
    moderation queue (D5) can't be read around by id once it exists; today
    every thread is 'approved' by construction, so this is a no-op filter,
    not a behavior change."""
    thread, component = _require_thread_and_component(db, thread_id)
    _check_view_access(component, user)
    if thread.status != THREAD_STATUS_APPROVED:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found")
    my_vote = _my_vote_for_thread(db, thread.id, user.email)
    return _to_thread_detail(thread, my_vote)


def update_thread_by_id(
    db: Session,
    thread_id: int,
    user: UserInfo,
    title: str | None,
    content: str | None,
    mentions: list[MentionInput] | None = None,
) -> ThreadDetail:
    thread, component = _require_thread_and_component(db, thread_id)
    # A caller who has lost view access to the widget since posting must
    # not still be able to edit through this endpoint (defense in depth —
    # not spelled out explicitly in the plan, called out in the handoff).
    _check_view_access(component, user)
    _require_author(thread.author_email, user)

    # Validate against the EFFECTIVE content — the new content if this PATCH
    # touches it, otherwise the thread's current content — since a mention's
    # token must occur in whatever the stored body ends up being, not
    # necessarily what this specific request's `content` field carried
    # (e.g. a title-only edit that also resends the same mentions).
    if mentions is not None:
        effective_content = content if content is not None else thread.content
        previous_mentions = thread.mentions
        resolved = _validate_and_resolve_mentions(db, mentions, effective_content)
        thread.mentions = resolved
        # plan §5.7 — "on edit: notify only newly added mentions". Read
        # BEFORE the reassignment above overwrites it.
        _notify_newly_added_mentions(
            db,
            previous_mentions=previous_mentions,
            resolved_mentions=resolved,
            actor=user,
            component_id=component.id,
            thread_id=thread.id,
            comment_id=None,
            content=effective_content,
        )

    # Only stamp "edited" when title/content were actually supplied to
    # change — an empty PATCH ({} — both omitted) must not show an "edited"
    # marker for an edit that never happened. A mentions-only resend (no
    # title/content change) is deliberately NOT treated as an edit either,
    # for the same reason.
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
    db: Session,
    thread_id: int,
    user: UserInfo,
    content: str,
    mentions: list[MentionInput] | None = None,
) -> CommentSummary:
    thread, component = _require_thread_and_component(db, thread_id)
    _check_view_access(component, user)

    # Resolve BEFORE the lock/recompute below — a 400 from the cap must not
    # leave a half-built comment or a bumped comment_count behind.
    resolved = _validate_and_resolve_mentions(db, mentions or [], content)

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
        mentions=resolved,
        up_count=0,
        down_count=0,
        created_at=now,
    )
    db.add(comment)
    db.flush()  # assigns comment.id — both the notifications below and
    # the comment_count recompute need it to already exist.

    thread.comment_count = _recompute_comment_count(db, thread_id)

    # plan §5.7 — mentions in the comment notify first ("mention wins"),
    # then the thread's author is notified for the comment itself unless
    # already covered by a mention on this same comment, or unless they are
    # the commenter.
    mention_notified = _notify_mentions(
        db,
        resolved,
        actor=user,
        component_id=component.id,
        thread_id=thread_id,
        comment_id=comment.id,
        content=content,
    )
    _notify_new_thread_comment(
        db,
        thread_author_email=thread.author_email,
        actor=user,
        component_id=component.id,
        thread_id=thread_id,
        comment_id=comment.id,
        content=content,
        already_notified=mention_notified,
    )

    db.commit()
    db.refresh(comment)
    return _to_comment_summary(comment, my_vote=0)


def update_comment_by_id(
    db: Session,
    comment_id: int,
    user: UserInfo,
    content: str | None,
    mentions: list[MentionInput] | None = None,
) -> CommentSummary:
    comment, _thread, component = _require_comment_thread_and_component(db, comment_id)
    _check_view_access(component, user)
    _require_author(comment.author_email, user)

    # Same effective-content reasoning as update_thread_by_id.
    if mentions is not None:
        effective_content = content if content is not None else comment.content
        previous_mentions = comment.mentions
        resolved = _validate_and_resolve_mentions(db, mentions, effective_content)
        comment.mentions = resolved
        # plan §5.7 — same "only newly added mentions" rule as thread edits.
        _notify_newly_added_mentions(
            db,
            previous_mentions=previous_mentions,
            resolved_mentions=resolved,
            actor=user,
            component_id=component.id,
            thread_id=comment.thread_id,
            comment_id=comment.id,
            content=effective_content,
        )

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
