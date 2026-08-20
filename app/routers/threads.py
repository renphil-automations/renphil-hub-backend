"""Thread widget router (plan_thread_widget_2026-08-17.md) — Phase 1
(threads/comments/votes) + Phase 3 (mentions).

`/threads/mentionable-users` (the directory endpoint) now lives here.
Notifications (`/notifications*`) are still Phase 4 and are not defined —
see app/services/thread_service.py's module docstring.

Every handler is a thin async wrapper: resolve + access-check + the actual
DB work all happen inside ONE `asyncio.to_thread(...)` call in
thread_service, so a single request costs one hop off the event loop, not
one per internal query (plan §4.6).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.dependencies import get_current_user
from app.models.auth import UserInfo
from app.schemas.thread import (
    CommentCreateRequest,
    CommentListResponse,
    CommentSummary,
    CommentUpdateRequest,
    MentionableUser,
    ThreadCreateRequest,
    ThreadDetail,
    ThreadListResponse,
    ThreadUpdateRequest,
    VoteRequest,
    VoteResponse,
)
from app.services import thread_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["Threads"])


# ---------------------------------------------------------
# Mentions (plan §5.1) — a static path segment ("mentionable-users"),
# declared ahead of the dynamic `/threads/{thread_id}` GET below. FastAPI/
# Starlette would still fall through to this route even if it came second
# (an `int` path-converter failure on "mentionable-users" just skips that
# route rather than erroring), but ordering it first avoids relying on that.
# ---------------------------------------------------------


@router.get(
    "/threads/mentionable-users",
    response_model=list[MentionableUser],
    summary="Search the mention directory (plan §5.1) — any authenticated caller",
)
async def list_mentionable_users(
    q: str | None = Query(default=None, description="Search prefix/substring, matched accent-insensitively."),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
):
    return await asyncio.to_thread(thread_service.list_mentionable_users, db, q)


# ---------------------------------------------------------
# Threads
# ---------------------------------------------------------


@router.get(
    "/threads/component/{link}",
    response_model=ThreadListResponse,
    summary="One page of threads for a thread widget, newest first",
    responses={403: {"description": "Caller does not satisfy the widget's access control"}},
)
async def list_threads(
    link: str = Path(..., description="The thread widget component's stable `link`."),
    cursor: str | None = Query(default=None, description="Opaque next-page cursor."),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
):
    return await asyncio.to_thread(thread_service.list_threads_for_link, db, link, cursor, user)


@router.post(
    "/threads/component/{link}",
    response_model=ThreadDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a thread on a thread widget",
    responses={403: {"description": "Caller does not satisfy the widget's access control"}},
)
async def create_thread(
    payload: ThreadCreateRequest,
    link: str = Path(..., description="The thread widget component's stable `link`."),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
):
    return await asyncio.to_thread(
        thread_service.create_thread_for_link,
        db,
        link,
        user,
        payload.title,
        payload.content,
        payload.mentions,
    )


@router.get(
    "/threads/{thread_id}",
    response_model=ThreadDetail,
    summary="Fetch one thread's full content — any viewer with widget access",
    responses={403: {"description": "Caller does not satisfy the widget's access control"}},
)
async def get_thread(
    thread_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
):
    return await asyncio.to_thread(thread_service.get_thread_by_id, db, thread_id, user)


@router.patch(
    "/threads/{thread_id}",
    response_model=ThreadDetail,
    summary="Edit a thread — author only",
    responses={403: {"description": "Caller is not this thread's author"}},
)
async def update_thread(
    payload: ThreadUpdateRequest,
    thread_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
):
    return await asyncio.to_thread(
        thread_service.update_thread_by_id,
        db,
        thread_id,
        user,
        payload.title,
        payload.content,
        payload.mentions,
    )


@router.delete(
    "/threads/{thread_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a thread — Hub Admin only, cascades to comments/votes",
    responses={403: {"description": "Caller is not a Hub Admin"}},
)
async def delete_thread(
    thread_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
):
    await asyncio.to_thread(thread_service.delete_thread_by_id, db, thread_id, user)


@router.put(
    "/threads/{thread_id}/vote",
    response_model=VoteResponse,
    summary="Cast, switch, or clear a vote on a thread",
    responses={403: {"description": "Caller is the thread's author, or lacks widget access"}},
)
async def vote_on_thread(
    payload: VoteRequest,
    thread_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
):
    return await asyncio.to_thread(
        thread_service.vote_on_thread, db, thread_id, user, payload.value
    )


# ---------------------------------------------------------
# Comments
# ---------------------------------------------------------


@router.get(
    "/threads/{thread_id}/comments",
    response_model=CommentListResponse,
    summary="One page of comments on a thread, oldest first",
    responses={403: {"description": "Caller does not satisfy the widget's access control"}},
)
async def list_comments(
    thread_id: int = Path(...),
    cursor: str | None = Query(default=None, description="Opaque next-page cursor."),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
):
    return await asyncio.to_thread(
        thread_service.list_comments_for_thread, db, thread_id, cursor, user
    )


@router.post(
    "/threads/{thread_id}/comments",
    response_model=CommentSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Add a comment to a thread",
    responses={403: {"description": "Caller does not satisfy the widget's access control"}},
)
async def create_comment(
    payload: CommentCreateRequest,
    thread_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
):
    return await asyncio.to_thread(
        thread_service.create_comment_for_thread,
        db,
        thread_id,
        user,
        payload.content,
        payload.mentions,
    )


@router.patch(
    "/threads/comments/{comment_id}",
    response_model=CommentSummary,
    summary="Edit a comment — author only",
    responses={403: {"description": "Caller is not this comment's author"}},
)
async def update_comment(
    payload: CommentUpdateRequest,
    comment_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
):
    return await asyncio.to_thread(
        thread_service.update_comment_by_id,
        db,
        comment_id,
        user,
        payload.content,
        payload.mentions,
    )


@router.delete(
    "/threads/comments/{comment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a comment — Hub Admin only, cascades to votes",
    responses={403: {"description": "Caller is not a Hub Admin"}},
)
async def delete_comment(
    comment_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
):
    await asyncio.to_thread(thread_service.delete_comment_by_id, db, comment_id, user)


@router.put(
    "/threads/comments/{comment_id}/vote",
    response_model=VoteResponse,
    summary="Cast, switch, or clear a vote on a comment",
    responses={403: {"description": "Caller is the comment's author, or lacks widget access"}},
)
async def vote_on_comment(
    payload: VoteRequest,
    comment_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
):
    return await asyncio.to_thread(
        thread_service.vote_on_comment, db, comment_id, user, payload.value
    )
