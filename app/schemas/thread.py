"""Pydantic request/response models for the Thread widget
(plan_thread_widget_2026-08-17.md) — Phase 1 (threads/comments/votes).

No `mentions` field on any request model yet. The plan's own wire shape
(§4.1) is `{title, content, mentions[]}`, but trusting a client-supplied
`mentions` array requires the validation pass in plan §5.3 (roster lookup +
literal-token check), which doesn't exist until Phase 3. Accepting the field
now and silently discarding it would look like a real API contract that
isn't; accepting it and trusting it would violate §5.3's explicit "the
server does not trust that array". Phase 3 adds the field to both request
models alongside the validation that makes it safe to store.
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

    @field_validator("title")
    @classmethod
    def _check_title(cls, value: str) -> str:
        return _validate_title(value)

    @field_validator("content")
    @classmethod
    def _check_content(cls, value: str) -> str:
        return _validate_content(value)


class ThreadUpdateRequest(BaseModel):
    """Both fields optional — a PATCH may touch either independently."""

    title: str | None = None
    content: str | None = None

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

    @field_validator("content")
    @classmethod
    def _check_content(cls, value: str) -> str:
        return _validate_content(value)


class CommentUpdateRequest(BaseModel):
    content: str | None = None

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


class CommentListResponse(BaseModel):
    items: list[CommentSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class VoteResponse(BaseModel):
    """Both fresh counts and the caller's new vote state, so the UI
    reconciles rather than guesses (plan §4.4). No net score (D10)."""

    up_count: int
    down_count: int
    my_vote: Literal[1, -1, 0]
