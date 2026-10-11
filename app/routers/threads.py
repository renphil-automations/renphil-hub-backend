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
by analogy with delete (landmine 8). The static `/threads/moderation/*`
routes (`pending`, `history` + facets, `summary`) are declared ABOVE
`/threads/{thread_id}`, same convention as the mention directory above.

THREAD VERSIONING (plan_thread_edit_versioning_2026-09-22.md, phase A, with
the 2026-09-23 amendments). Every published version of a thread is a
revision. A non-editor author's PATCH of an approved thread is staged as a
pending revision; an editor-author's goes live as an auto-approved one (409
`earlier_revisions_pending` while author edits are pending, unless the body
carries `overwrite: true`). PATCH returns `ThreadUpdateResult` (`applied`
says which happened). Editors decide staged revisions one at a time through
`POST /threads/{id}/revisions/{rid}/approve` (body `{overwrite}` — an
out-of-order approve is the same 409 unless `overwrite: true`) and
`.../reject`, gated exactly like the thread approve/reject above; `GET
/threads/{id}/revisions/{rid}` reads one body (author or editor). Revisions
have their OWN management section, `/threads/revision-moderation/*`
(pending, history + facets, summary); the Threads Management routes above
list new threads only, exactly as before versioning.

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
from datetime import datetime
from typing import Literal

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
    RevisionDecisionRequest,
    ThreadCreateRequest,
    ThreadDetail,
    ThreadHistoryFacets,
    ThreadListResponse,
    ThreadModerationListResponse,
    ThreadModerationRow,
    ThreadModerationSummary,
    ThreadRevisionDetail,
    ThreadRevisionHistoryFacets,
    ThreadRevisionModerationListResponse,
    ThreadRevisionModerationSummary,
    ThreadUpdateRequest,
    ThreadUpdateResult,
    ThreadWidgetCounts,
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


@router.get(
    "/threads/component/{link}/counts",
    response_model=ThreadWidgetCounts,
    summary="All-status thread/comment totals for one widget (followups plan §4.2)",
    responses={
        403: {"description": "Caller is not an editor of this discussion"},
        404: {"description": "No thread widget with this link"},
    },
)
async def get_thread_widget_counts(
    link: str = Path(..., description="The thread widget component's stable `link`."),
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(thread_service.thread_widget_counts, db, link, access=access)


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
    summary="Threads the caller moderates that have an explicit decision, most recent first, filterable",
    responses={400: {"description": "Malformed cursor"}},
)
async def list_thread_history(
    cursor: str | None = Query(default=None, description="Opaque next-page cursor; resend the same filters."),
    status_filter: list[Literal["approved", "rejected"]] | None = Query(
        default=None, alias="status", description="Repeatable. Default: both."
    ),
    date_from: datetime | None = Query(
        default=None, description="Decision instant, inclusive (ISO-8601; naive = UTC)."
    ),
    date_to: datetime | None = Query(
        default=None, description="Decision instant, exclusive (ISO-8601; naive = UTC)."
    ),
    author: list[str] | None = Query(default=None, description="Thread author email. Repeatable."),
    reviewer: list[str] | None = Query(default=None, description="Decider email. Repeatable."),
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.list_thread_history,
        db,
        cursor,
        statuses=status_filter,
        date_from=date_from,
        date_to=date_to,
        authors=author,
        reviewers=reviewer,
        access=access,
    )


@router.get(
    "/threads/moderation/history/facets",
    response_model=ThreadHistoryFacets,
    summary="The History tab's author and reviewer filter options",
)
async def get_thread_history_facets(
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(thread_service.thread_history_facets, db, access=access)


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
    # `Cache-Control: no-store` on both the 200 and the 304 (followups plan
    # §1) so the browser's own HTTP cache never stores this body — that
    # cache is keyed by URL, not by bearer token, so without `no-store` a
    # user switch in one browser could revalidate against the *previous*
    # user's cached response. With nothing stored, the only 304s left are
    # the ones JS asks for with its own `If-None-Match`.
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={"ETag": etag, "Cache-Control": "no-store"},
        )
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store"
    return summary


# ---------------------------------------------------------
# Revision Management (plan_thread_edit_versioning amendment A4) — its own
# section, separate from Threads Management above. Static paths under
# `/threads/revision-moderation/...`, NOT `/threads/moderation/revisions/...`:
# the latter is depth 4 with `revisions` in position 3, which is exactly the
# shape of `/threads/{thread_id}/revisions/{revision_id}` — `moderation`
# would bind as a thread id and fail int validation (422) whenever that
# route happened to match first. `revision-moderation` can't collide with
# any `/threads/{thread_id}/...` pattern. Declared above
# `/threads/{thread_id}` anyway, same convention as the rest of this file.
# ---------------------------------------------------------


@router.get(
    "/threads/revision-moderation/pending",
    response_model=ThreadRevisionModerationListResponse,
    summary="Pending thread revisions the caller can decide, newest first",
)
async def list_pending_revisions(
    cursor: str | None = Query(default=None, description="Opaque next-page cursor."),
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(thread_service.list_pending_revisions, db, cursor, access=access)


@router.get(
    "/threads/revision-moderation/history",
    response_model=ThreadRevisionModerationListResponse,
    summary="Decided and auto-approved thread revisions, most recent decision first, filterable",
    responses={400: {"description": "Malformed cursor"}},
)
async def list_revision_history(
    cursor: str | None = Query(default=None, description="Opaque next-page cursor; resend the same filters."),
    status_filter: list[Literal["approved", "rejected", "overwritten"]] | None = Query(
        default=None, alias="status", description="Repeatable. Default: all three."
    ),
    origin: list[Literal["original", "author", "editor"]] | None = Query(
        default=None, description="Repeatable. Default: all three."
    ),
    date_from: datetime | None = Query(
        default=None, description="Decision instant, inclusive (ISO-8601; naive = UTC)."
    ),
    date_to: datetime | None = Query(
        default=None, description="Decision instant, exclusive (ISO-8601; naive = UTC)."
    ),
    author: list[str] | None = Query(default=None, description="Submitter email. Repeatable."),
    reviewer: list[str] | None = Query(default=None, description="Decider email. Repeatable."),
    thread_id: list[int] | None = Query(
        default=None, description="Thread id. Repeatable. Narrows within the moderated set."
    ),
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.list_revision_history,
        db,
        cursor,
        statuses=status_filter,
        origins=origin,
        date_from=date_from,
        date_to=date_to,
        authors=author,
        reviewers=reviewer,
        thread_ids=thread_id,
        access=access,
    )


@router.get(
    "/threads/revision-moderation/history/facets",
    response_model=ThreadRevisionHistoryFacets,
    summary="The History tab's author and reviewer filter options",
)
async def get_revision_history_facets(
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(thread_service.revision_history_facets, db, access=access)


@router.get(
    "/threads/revision-moderation/summary",
    response_model=ThreadRevisionModerationSummary,
    summary="Whether the caller moderates anything, and how many revisions are pending",
)
async def get_revision_moderation_summary(
    request: Request,
    response: Response,
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    summary, etag = await asyncio.to_thread(
        thread_service.revision_moderation_summary, db, access=access
    )
    # Identical discipline to `/threads/moderation/summary` above: no-store
    # on both the 200 and the 304, 304 only for JS's own If-None-Match.
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={"ETag": etag, "Cache-Control": "no-store"},
        )
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store"
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


# ---------------------------------------------------------
# Staged edits (plan_thread_edit_versioning_2026-09-22.md §5). Depth-4 paths
# under `/threads/{thread_id}/revisions/...` — they can't collide with the
# depth-2 `/threads/{thread_id}` pattern, so their position above it is
# convention, kept to match the file's grouping. The gate lives in the
# service (the same split `decide_thread` uses).
# ---------------------------------------------------------


@router.get(
    "/threads/{thread_id}/revisions/{revision_id}",
    response_model=ThreadRevisionDetail,
    summary="One staged edit's full body — the thread's author or an editor",
    responses={
        403: {"description": "Caller does not satisfy the widget's access control"},
        404: {"description": "No such revision on this thread, or caller may not see it"},
    },
)
async def get_thread_revision(
    thread_id: int = Path(...),
    revision_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.get_thread_revision, db, thread_id, revision_id, user, access=access
    )


@router.post(
    "/threads/{thread_id}/revisions/{revision_id}/approve",
    response_model=ThreadRevisionDetail,
    summary="Approve a staged edit — an editor of its widget or any ancestor",
    responses={
        403: {"description": "Caller is not an editor of this discussion"},
        404: {"description": "No such revision on this thread"},
        409: {
            "description": "`detail.code` is `already_decided` (the revision is no longer pending) "
            "or `earlier_revisions_pending` (older pending edits exist and `overwrite` was not set)"
        },
    },
)
async def approve_thread_revision(
    payload: RevisionDecisionRequest | None = None,
    thread_id: int = Path(...),
    revision_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
    access: ViewerAccess = Depends(get_viewer_access),
):
    overwrite = payload.overwrite if payload is not None else False
    return await asyncio.to_thread(
        thread_service.decide_thread_revision,
        db,
        thread_id,
        revision_id,
        user,
        approve=True,
        overwrite=overwrite,
        access=access,
    )


@router.post(
    "/threads/{thread_id}/revisions/{revision_id}/reject",
    response_model=ThreadRevisionDetail,
    summary="Reject a staged edit — an editor of its widget or any ancestor",
    responses={
        403: {"description": "Caller is not an editor of this discussion"},
        404: {"description": "No such revision on this thread"},
        409: {"description": "`detail.code` is `already_decided`"},
    },
)
async def reject_thread_revision(
    thread_id: int = Path(...),
    revision_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.decide_thread_revision,
        db,
        thread_id,
        revision_id,
        user,
        approve=False,
        access=access,
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
    response_model=ThreadUpdateResult,
    summary="Edit a thread — author only; a non-editor's edit of an approved thread is staged for review",
    responses={
        403: {"description": "Caller is not this thread's author"},
        409: {
            "description": "Thread was rejected and can no longer be edited; or (editor, "
            "`detail.code` = `earlier_revisions_pending`) the thread has pending author "
            "edits and `overwrite` was not set"
        },
    },
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
        overwrite=payload.overwrite,
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
    # `Cache-Control: no-store` on both responses — same reasoning as
    # `get_moderation_summary` above (followups plan §1).
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={"ETag": etag, "Cache-Control": "no-store"},
        )
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store"
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
