"""Thread widget router (plan_thread_widget_2026-08-17.md) — Phase 1
(threads/comments/votes) + Phase 3 (mentions) + Phase 4 (notifications).

`/threads/mentionable-users` (the directory endpoint) lives here, and so
now does `/notifications*` (list, unread-count, mark-read, read-all — plan
§4.1). Thread/comment create and every vote endpoint carry a
`@rate_limited(...)` decorator (`helpers/rate_limit.py`, plan §4.7 control
2) — it runs BEFORE the handler body, so a rate-limited caller never
reaches the DB at all.

Every handler is a thin async wrapper: resolve + access-check + the actual
DB work all happen inside ONE `asyncio.to_thread(...)` call in
thread_service, so a single request costs one hop off the event loop, not
one per internal query (plan §4.6). `/notifications/unread-count` is the
one exception — it also needs the raw `Request`/`Response` for the
`If-None-Match` / `ETag` pair (plan §4.5), which have no place in
thread_service's plain-value return.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.dependencies import get_current_user
from app.helpers.rate_limit import rate_limited
from app.models.auth import UserInfo
from app.schemas.thread import (
    CommentCreateRequest,
    CommentListResponse,
    CommentSummary,
    CommentUpdateRequest,
    MentionableUser,
    NotificationListResponse,
    ThreadCreateRequest,
    ThreadDetail,
    ThreadListResponse,
    ThreadUpdateRequest,
    UnreadCountResponse,
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
@rate_limited("thread")
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
@rate_limited("vote")
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
@rate_limited("comment")
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
@rate_limited("vote")
async def vote_on_comment(
    payload: VoteRequest,
    comment_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
):
    return await asyncio.to_thread(
        thread_service.vote_on_comment, db, comment_id, user, payload.value
    )


# ---------------------------------------------------------
# Notifications (plan §3.4, §4.5, §7 — Phase 4). Self only throughout — none
# of these take an `{id}`-addressed OTHER user, by design.
# ---------------------------------------------------------


@router.get(
    "/notifications",
    response_model=NotificationListResponse,
    summary="One page of the caller's own notifications, newest first",
)
async def list_notifications(
    cursor: str | None = Query(default=None, description="Opaque next-page cursor."),
    unread_only: bool = Query(default=False),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
):
    return await asyncio.to_thread(
        thread_service.list_notifications_for_user, db, user, cursor, unread_only
    )


@router.get(
    "/notifications/unread-count",
    response_model=UnreadCountResponse,
    summary="The caller's unread notification count — derived every time, never cached (plan §4.5)",
)
async def get_unread_notification_count(
    request: Request,
    response: Response,
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
):
    count, etag = await asyncio.to_thread(
        thread_service.get_unread_notification_count, db, user
    )
    # plan §4.5 — "does not save a request, but it drops the body in the
    # overwhelmingly common unchanged case." A 304 carries no body by HTTP
    # definition, so the bell's poll saves the transfer even though it
    # still costs the same DB round trip.
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return UnreadCountResponse(count=count)


@router.post(
    "/notifications/{notification_id}/read",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark one of the caller's own notifications read — idempotent",
)
async def mark_notification_read(
    notification_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
):
    await asyncio.to_thread(
        thread_service.mark_notification_read, db, notification_id, user
    )


@router.post(
    "/notifications/read-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark all of the caller's own notifications read",
)
async def mark_all_notifications_read(
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
):
    await asyncio.to_thread(thread_service.mark_all_notifications_read, db, user)
