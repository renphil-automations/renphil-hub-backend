"""Pydantic request/response models for the Thread widget
(plan_thread_widget_2026-08-17.md) — Phase 1 (threads/comments/votes) +
Phase 3 (mentions) + Phase 4 (notifications).

`mentions` now appears on the create/update request models (a
`list[MentionInput]`) and on `ThreadSummary`/`CommentSummary` (a
`list[MentionEntry]`, inherited by `ThreadDetail`). The request shape mirrors
the stored/response shape (`{email, name, token}`, plan D13) rather than a
bare email list, because the composer's own mention-tracking state is
already `{token -> email, name}` (plan §5.4) and serializes directly into
this array — but `name`/`token` on the way IN are never trusted (plan §5.3):
`thread_service._validate_and_resolve_mentions` re-derives both from the
`users` row it matches on `email` and discards whatever the client sent.

`NotificationEntry` / `NotificationListResponse` / `UnreadCountResponse`
(bottom of this file) back `GET /notifications`, `GET
/notifications/unread-count`, `POST /notifications/{id}/read` and `POST
/notifications/read-all` (plan §4.1, §4.5, §7).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Unicode code points, not bytes and not UTF-16 code units — Python's
# `len()` on a `str` already counts code points, so no special handling is
# needed here (plan §4.2, finding E8). This is the whole reason code points
# were chosen as the shared unit: both Python and JS can compute them with
# no extra machinery, and they agree.
MAX_TITLE_LENGTH = 200
MAX_CONTENT_LENGTH = 5000

# plan §4.7 control (1) / §5.3 item 3 — counts SURVIVING (validated)
# mentions, not the raw claimed count. Over the cap is a 400 naming the
# limit, never a silent truncation.
MAX_MENTIONS_PER_POST = 25


class MentionInput(BaseModel):
    """One entry of a client-submitted `mentions` array (plan §5.3/§5.4).
    Only `email` is ever trusted — it's the key the server looks up in
    `users` — `name`/`token` are accepted so the wire shape matches what the
    composer already tracks, but the server overwrites both from the matched
    row before anything is stored (plan §5.3: "the server overwrites both
    from the users row it just matched, so a client cannot make a chip
    render someone else's name")."""

    email: str
    name: str | None = None
    token: str | None = None


class MentionEntry(BaseModel):
    """A validated, resolved mention (plan D13) — `email` is the only
    identity; `name` and `token` are display snapshots of how the mention
    read when it was posted (plan §3.6's "display snapshot is not an
    anchor"). Mirrors the JSONB shape stored in `threads.mentions` /
    `thread_comments.mentions`."""

    email: str
    name: str
    token: str


class MentionableUser(BaseModel):
    """One row of `GET /threads/mentionable-users` (plan §5.1)."""

    token: str
    name: str
    email: str
    headshot_url: str | None = None


def _validate_title(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Title is required")
    if len(value) > MAX_TITLE_LENGTH:
        raise ValueError(f"Title must be at most {MAX_TITLE_LENGTH} characters")
    return value


def _validate_content(value: str) -> str:
    # Stored as raw markdown, untrimmed (plan §4.2 — "No sanitizing on
    # write"). The emptiness check still strips, purely to reject
    # whitespace-only posts; the stored value itself is `value` unchanged.
    if not value.strip():
        raise ValueError("Content is required")
    if len(value) > MAX_CONTENT_LENGTH:
        raise ValueError(f"Content must be at most {MAX_CONTENT_LENGTH} characters")
    return value


class ThreadCreateRequest(BaseModel):
    title: str
    content: str
    mentions: list[MentionInput] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def _check_title(cls, value: str) -> str:
        return _validate_title(value)

    @field_validator("content")
    @classmethod
    def _check_content(cls, value: str) -> str:
        return _validate_content(value)


class ThreadUpdateRequest(BaseModel):
    """Fields optional — a PATCH may touch any subset independently.
    `mentions: None` (the default — omitted from the JSON body) means
    "leave the stored mentions alone"; an explicit array, even `[]`,
    means "replace them with this validated set" — the same tri-state
    convention `title`/`content` already use, extended to `mentions` so a
    PATCH that only touches the title can't silently wipe existing
    mentions by omission."""

    title: str | None = None
    content: str | None = None
    mentions: list[MentionInput] | None = None

    @field_validator("title")
    @classmethod
    def _check_title(cls, value: str | None) -> str | None:
        return _validate_title(value) if value is not None else None

    @field_validator("content")
    @classmethod
    def _check_content(cls, value: str | None) -> str | None:
        return _validate_content(value) if value is not None else None


class CommentCreateRequest(BaseModel):
    content: str
    mentions: list[MentionInput] = Field(default_factory=list)

    @field_validator("content")
    @classmethod
    def _check_content(cls, value: str) -> str:
        return _validate_content(value)


class CommentUpdateRequest(BaseModel):
    """`mentions` follows the same None-means-don't-touch convention as
    `ThreadUpdateRequest` (see there). Plan §4.1's endpoint table doesn't
    list `mentions[]` in this one request's notes column, unlike every
    other create/update endpoint — treated as an incomplete table entry
    rather than a deliberate asymmetry (plan §12 item 2: "comments support
    markdown and mentions, same rules ... as threads"), flagged in the
    session handoff rather than silently resolved either way."""

    content: str | None = None
    mentions: list[MentionInput] | None = None

    @field_validator("content")
    @classmethod
    def _check_content(cls, value: str | None) -> str | None:
        return _validate_content(value) if value is not None else None


class VoteRequest(BaseModel):
    # 0 clears the caller's vote (plan §4.1).
    value: Literal[1, -1, 0]


class ThreadSummary(BaseModel):
    """One row of the list endpoint (plan §4.1 — "counts + the caller's own
    vote per row")."""

    id: int
    component_id: int
    title: str
    author_email: str
    author_name: str | None
    status: str
    # Two counts, rendered separately — never collapsed into a net score
    # (D10, amended 2026-08-19). There is deliberately no `score` field: the
    # UI shows up and down as independent like/dislike controls, and a
    # derived total that nothing renders is a field a future implementer
    # renders by accident.
    up_count: int
    down_count: int
    comment_count: int
    created_at: datetime
    edited_at: datetime | None
    my_vote: Literal[1, -1, 0]
    # Resolved, validated entries (plan D13/§5.3) — the list view doesn't
    # render markdown bodies so it never needs these for the chip plugin,
    # but they ride along here (rather than only on ThreadDetail) so every
    # response shape that DOES render markdown (ThreadDetail, via
    # inheritance) gets them without a second field declaration.
    mentions: list[MentionEntry] = Field(default_factory=list)
    # Bounded, markdown-stripped preview for the list row's "read without
    # opening the thread" card (2026-08-20 UI pass). Built the SAME way as a
    # notification's own `excerpt` — `thread_service.generate_notification_excerpt`,
    # reused rather than duplicated — deliberately NOT the full `content`
    # ThreadDetail carries: the list endpoint still never sends a page of
    # full markdown bodies (plan §4.1), it just now also sends a short,
    # already-plain-text snippet of each one.
    content_excerpt: str = ""


class ThreadDetail(ThreadSummary):
    """Adds the body — returned by create/update, which the list endpoint
    deliberately omits (plan §4.1 lists no `content` field on the list
    response; a 20-row page of full markdown bodies is not what the list
    view needs)."""

    content: str


class ThreadListResponse(BaseModel):
    items: list[ThreadSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class CommentSummary(BaseModel):
    id: int
    thread_id: int
    content: str
    author_email: str
    author_name: str | None
    # Same terms as ThreadSummary — two counts, no net score (D10).
    up_count: int
    down_count: int
    created_at: datetime
    edited_at: datetime | None
    my_vote: Literal[1, -1, 0]
    mentions: list[MentionEntry] = Field(default_factory=list)


class CommentListResponse(BaseModel):
    items: list[CommentSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class VoteResponse(BaseModel):
    """Both fresh counts and the caller's new vote state, so the UI
    reconciles rather than guesses (plan §4.4). No net score (D10)."""

    up_count: int
    down_count: int
    my_vote: Literal[1, -1, 0]


# ---------------------------------------------------------
# Notifications (plan §3.4, §4.5, §7 — Phase 4)
# ---------------------------------------------------------


class NotificationEntry(BaseModel):
    """One row of `GET /notifications` (plan §7.2's panel row content:
    actor name, a type-keyed phrase the frontend derives from `type`, the
    thread title, the excerpt, and a relative timestamp computed
    client-side from `created_at`).

    `thread_title` and `component_link` are NOT stored on the
    `notifications` row itself (plan §3.4 lists no such columns) — they are
    joined live from the thread/component at read time. This is
    deliberately unlike `mentions`' `name`/`token` snapshots (plan §3.6's
    "display snapshot is not an anchor"): a snapshot exists to survive an
    identity change (an email, a name); a thread's title has no such
    problem; a live join is simply the current, correct title, and it
    costs nothing extra since the notification can only exist while its
    thread does (`ON DELETE CASCADE`, plan §3.4 — "no orphan state and no
    tombstone")."""

    id: int
    type: str
    actor_email: str | None
    actor_name: str | None
    thread_id: int
    thread_title: str
    component_link: str
    excerpt: str | None
    read_at: datetime | None
    created_at: datetime


class NotificationListResponse(BaseModel):
    items: list[NotificationEntry] = Field(default_factory=list)
    next_cursor: str | None = None


class UnreadCountResponse(BaseModel):
    """plan §4.5 — derived every time via `COUNT(*)`, never cached. The
    router also returns this value baked into an `ETag`
    (`"{count}-{max_id}"`) so an unchanged poll can 304 — see
    `thread_service.get_unread_notification_count`."""

    count: int
