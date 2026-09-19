"""Thread widget router (plan_thread_widget_2026-08-17.md) — Phase 1
(threads/comments/votes) + Phase 3 (mentions) + Phase 4 (notifications).

`/threads/component/{link}/mentionable-users` (the directory endpoint —
per thread widget since plan_thread_moderation_2026-09-18.md phase 2m,
M9) lives here, and so does `/notifications*` (list, unread-count,
mark-read, read-all — plan §4.1). Thread/comment create and every vote
endpoint carry a
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

MODERATION (plan_thread_moderation_2026-09-18.md phase 2a). `POST
/threads/{id}/approve` and `.../reject` gate on `edit(n)` for the thread's
own component — an editor of the widget OR of any ancestor (a root-tab
editor approves every thread widget under that root), never Hub-Admin-only
by analogy with delete (landmine 8). The three static `/threads/moderation/
*` routes (`pending`, `history`, `summary`) are declared ABOVE
`/threads/{thread_id}`, same convention as the mention directory above.

ACCESS CONTROL, added plan_ac_enforcement_closeout_2026-09-09.md §4. Until
then every route below carried `Depends(get_current_user)` and nothing
else — `thread_service` gated view/post against the component's own legacy
`access_control` JSONB, independent of the fold gating the tab the widget
sits on. Every route that reaches `thread_service._check_view_access` or
`_require_hub_admin` now also takes
`access: ViewerAccess = Depends(get_viewer_access)` and passes it through —
same split as `tabs_v2.py` and `super_blocknote_v2.py`: routers thread,
services enforce. The mention directory is the one route gated
DIFFERENTLY, not left ungated: it is polled per keystroke, so it takes
`get_current_hub_user` + `granted_single_node` (the `airtable.py` fast-path
shape) instead of the whole-tree `ViewerAccess` — see
`_list_mentionable_users_sync` below and moderation plan §5.9.3.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.dependencies import (
    CurrentHubUser,
    get_current_hub_user,
    get_current_user,
    get_viewer_access,
    is_hub_admin,
)
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
    ThreadModerationListResponse,
    ThreadModerationRow,
    ThreadModerationSummary,
    ThreadUpdateRequest,
    UnreadCountResponse,
    VoteRequest,
    VoteResponse,
)
from app.services import thread_service
from app.services.access_visibility_service import ViewerAccess, granted_single_node
from app.services.rbac_graph_service import RbacClosures

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["Threads"])


# ---------------------------------------------------------
# Mentions (plan §5.1, scoped per plan_thread_moderation_2026-09-18.md M9 /
# §4.1 / §5.9). The directory is PER THREAD WIDGET now — the old un-scoped
# `/threads/mentionable-users` is REMOVED, not kept beside: a directory with
# no component is exactly the thing D7's amendment forbids. That static
# path now falls through to `GET /threads/{thread_id}` and fails its `int`
# validation (422), which the tests pin.
# ---------------------------------------------------------


def _list_mentionable_users_sync(
    db: Session, link: str, q: str | None, current: CurrentHubUser
) -> list[MentionableUser]:
    """Resolve + gate + list, in ONE `asyncio.to_thread` call — the
    `airtable.py` shape (`_resolve_airtable_bundle_and_access`), for the
    same reason: this route is hit once per debounced keystroke in the
    composer, and gating it with `Depends(get_viewer_access)` would put the
    ~2.4 s whole-tree fold on every one of those requests for a non-admin
    (moderation plan §5.9.3, landmine 16). `granted_single_node` is the
    O(depth) single-node fast path built for exactly this shape of call.

    ONE `RbacClosures`, shared by the caller's gate (`is_hub_admin` branch
    2 + `granted_single_node`) and the audience loop inside the service
    (§8.2's rule — build once per request). An admin caller short-circuits
    `is_hub_admin` on branch 1 with zero queries, but the reverse fold
    below needs the closures regardless, so a real snapshot (not
    airtable.py's lazy one) is the right shape here.

    404 for an unknown link or a non-thread component (the module's "wrong
    type reads as not found" rule); 403 for a caller not granted on the
    widget — the same wording `thread_service._check_view_access` uses, so
    the composer's error handling sees nothing new.
    """
    component = thread_service.resolve_thread_widget_component(db, link)
    closures = RbacClosures(db)
    if not is_hub_admin(db, current, closures=closures) and not granted_single_node(
        db, current.hub_user_id, ("component", component.id), is_admin=False, closures=closures
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have access to this discussion")
    return thread_service.list_mentionable_users(db, component, q, closures=closures)


@router.get(
    "/threads/component/{link}/mentionable-users",
    response_model=list[MentionableUser],
    summary="Search the mention directory for one thread widget (plan §5.1, scoped per M9)",
    responses={
        403: {"description": "Caller does not satisfy the widget's access control"},
        404: {"description": "No thread widget with this link"},
    },
)
async def list_mentionable_users(
    link: str = Path(..., description="The thread widget component's stable `link`."),
    q: str | None = Query(default=None, description="Search prefix/substring, matched accent-insensitively."),
    db: Session = Depends(get_db_v2),
    current: CurrentHubUser = Depends(get_current_hub_user),
):
    return await asyncio.to_thread(_list_mentionable_users_sync, db, link, q, current)


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
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.list_threads_for_link, db, link, cursor, user, access=access
    )


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
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.create_thread_for_link,
        db,
        link,
        user,
        payload.title,
        payload.content,
        payload.mentions,
        access=access,
    )


# ---------------------------------------------------------
# Moderation (plan_thread_moderation_2026-09-18.md §4.2) — approve/reject,
# the pending queue, the history list, and the sidebar's polled summary.
# The three static `/threads/moderation/*` paths are declared ABOVE
# `/threads/{thread_id}` below, same convention as the mention directory.
# ---------------------------------------------------------


@router.get(
    "/threads/moderation/pending",
    response_model=ThreadModerationListResponse,
    summary="The caller's pending-thread moderation queue, oldest first",
)
async def list_pending_threads(
    cursor: str | None = Query(default=None, description="Opaque next-page cursor."),
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(thread_service.list_pending_threads, db, cursor, access=access)


@router.get(
    "/threads/moderation/history",
    response_model=ThreadModerationListResponse,
    summary="Threads the caller moderates that have an explicit decision, most recent first",
)
async def list_thread_history(
    cursor: str | None = Query(default=None, description="Opaque next-page cursor."),
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(thread_service.list_thread_history, db, cursor, access=access)


@router.get(
    "/threads/moderation/summary",
    response_model=ThreadModerationSummary,
    summary="Whether the caller moderates anything, and how many threads are pending",
)
async def get_moderation_summary(
    request: Request,
    response: Response,
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    summary, etag = await asyncio.to_thread(thread_service.moderation_summary, db, access=access)
    # Same 304-on-unchanged shape as `/notifications/unread-count` above.
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return summary


@router.post(
    "/threads/{thread_id}/approve",
    response_model=ThreadModerationRow,
    summary="Approve a pending thread — an editor of its widget or any ancestor",
    responses={
        403: {"description": "Caller is not an editor of this discussion"},
        409: {"description": "Thread has already been reviewed"},
    },
)
async def approve_thread(
    thread_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.decide_thread, db, thread_id, user, approve=True, access=access
    )


@router.post(
    "/threads/{thread_id}/reject",
    response_model=ThreadModerationRow,
    summary="Reject a pending thread — an editor of its widget or any ancestor",
    responses={
        403: {"description": "Caller is not an editor of this discussion"},
        409: {"description": "Thread has already been reviewed"},
    },
)
async def reject_thread(
    thread_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.decide_thread, db, thread_id, user, approve=False, access=access
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
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.get_thread_by_id, db, thread_id, user, access=access
    )


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
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.update_thread_by_id,
        db,
        thread_id,
        user,
        payload.title,
        payload.content,
        payload.mentions,
        access=access,
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
    access: ViewerAccess = Depends(get_viewer_access),
):
    await asyncio.to_thread(thread_service.delete_thread_by_id, db, thread_id, access=access)


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
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.vote_on_thread, db, thread_id, user, payload.value, access=access
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
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.list_comments_for_thread, db, thread_id, cursor, user, access=access
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
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.create_comment_for_thread,
        db,
        thread_id,
        user,
        payload.content,
        payload.mentions,
        access=access,
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
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.update_comment_by_id,
        db,
        comment_id,
        user,
        payload.content,
        payload.mentions,
        access=access,
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
    access: ViewerAccess = Depends(get_viewer_access),
):
    await asyncio.to_thread(thread_service.delete_comment_by_id, db, comment_id, access=access)


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
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.vote_on_comment, db, comment_id, user, payload.value, access=access
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
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.list_notifications_for_user,
        db,
        user,
        cursor,
        unread_only,
        access=access,
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
    access: ViewerAccess = Depends(get_viewer_access),
):
    count, etag = await asyncio.to_thread(
        thread_service.get_unread_notification_count, db, user, access=access
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
    access: ViewerAccess = Depends(get_viewer_access),
):
    await asyncio.to_thread(
        thread_service.mark_notification_read, db, notification_id, user, access=access
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
