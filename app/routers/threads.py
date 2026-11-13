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

REJECTION REASONS + RESUBMISSION (plan_thread_reject_resubmit_2026-11-07.md
§5). Both reject routes REQUIRE a `RejectRequest` body (`{reason}`, trimmed,
1..1000) — a bodiless reject is a 422. The pending queue orders by
`submitted_at` (a resubmission joins the back); History is grouped by
thread, with any-match filters over its decided rounds. The thread's AUTHOR
may `POST /threads/{id}/resubmit` (changed text only — 409
`unchanged_resubmission`; 409 `closed` once closed) or `.../close` (final,
no reopen — plan §12) a declined thread; `GET /threads/{id}/submission-
history` is its round timeline (author or editor);
`GET /threads/component/{link}/declined` is the author's inbox (`items` =
open, `closed` = closed). The revision twins
(`.../revisions/{rid}/resubmit|close|chain`) do the same for a declined edit
(a rework is a linked new revision). A declined edit also closes itself
(`superseded`) when a newer version of its thread goes live; the publisher
confirms through the `earlier_revisions_pending` 409 (`declined_open`) and
`overwrite: true`, and the edit's author gets a `thread_edit_closed` bell.

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
    ViewAsAccess,
    get_current_hub_user,
    get_current_user,
    get_view_as_access,
    get_view_as_anonymous,
    get_view_as_selection,
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
    DeclinedItemsResponse,
    MentionableUser,
    MyRevisionsResponse,
    MyRevisionsSummary,
    NotificationListResponse,
    RejectRequest,
    RevisionDecisionRequest,
    ThreadDecisionRequest,
    RevisionResubmitRequest,
    ThreadActivityFacets,
    ThreadActivityListResponse,
    ThreadCreateRequest,
    ThreadDetail,
    ThreadDetailViewAsResponse,
    ThreadHistoryFacets,
    ThreadListResponse,
    ThreadHistoryListResponse,
    ThreadModerationListResponse,
    ThreadModerationRow,
    ThreadModerationSummary,
    ThreadRevisionDetail,
    ThreadRevisionHistoryFacets,
    ThreadRevisionModerationListResponse,
    ThreadRevisionModerationSummary,
    ThreadResubmitRequest,
    ThreadSubmissionRound,
    ThreadTimelineResponse,
    ThreadUpdateRequest,
    ThreadUpdateResult,
    ThreadWidgetCounts,
    UnreadCountResponse,
    VoteRequest,
    VoteResponse,
)
from app.services import thread_service, view_as_service
from app.services.access_visibility_service import ViewerAccess, granted_single_node
from app.services.rbac_graph_service import RbacClosures

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["Threads"])

VIEW_AS_ERROR_RESPONSES = {
    400: {"description": "View As: a malformed `view_as` pair, an unknown key or user, or too many pairs"},
    403: {"description": "View As refused: the caller may not adopt one or more of the given pairs"},
}

THREAD_VIEW_AS_RESPONSES = {
    400: {
        "description": "View As: a malformed `view_as` pair, an unknown key or user, or too many pairs"
    },
    403: {"description": "Caller does not satisfy the widget's access control, or View As refused"},
}

def _thread_subject(user: UserInfo, anonymous: bool) -> UserInfo | None:
    """Whose authorship, votes and authored edits the thread read endpoints
    report: the caller's, whatever `view_as` pairs or `view_as_user` are
    selected, or nobody's (`None`) under `anonymous`. A selected user's own
    threads, votes and edits are never reported."""
    return None if anonymous else user



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


@router.get(
    "/threads/component/{link}/declined",
    response_model=DeclinedItemsResponse,
    summary="The caller's own declined items on one widget (reject/resubmit plan §4.7)",
    responses=THREAD_VIEW_AS_RESPONSES,
)
async def list_declined_items(
    link: str = Path(..., description="The thread widget component's stable `link`."),
    anonymous: bool = Depends(get_view_as_anonymous),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
    view_as_access: ViewAsAccess = Depends(get_view_as_access),
):
    """The author's "Revisions" inbox: declined threads and declined,
    not-yet-reworked edits, newest decline first, capped at 100 with
    `truncated`. Empty for a no-subject caller (`anonymous`), the same
    subject rule as the widget list."""
    subject = _thread_subject(user, anonymous)
    return await asyncio.to_thread(
        thread_service.list_declined_items, db, link, subject, access=view_as_access.access
    )


# ---------------------------------------------------------
# My Revisions (plan_my_revisions_2026-11-10.md §4.3) — the author's
# declined + pending items across every thread widget. Static `/threads/me/*`
# paths, declared ABOVE `/threads/{thread_id}` (otherwise `me` binds as a
# thread id and 422s — same convention as the moderation block).
#
# Gated like the mention directory, NOT with `Depends(get_viewer_access)`:
# the summary is polled by every signed-in user, and the whole-tree fold is
# ~2.4 s for a non-admin (landmine 16). `get_current_hub_user` + the View As
# selection only; the service gates per widget with `granted_single_node`
# and one shared `RbacClosures`, built only when the author has any rows.
# Under ANY View As selection, or `anonymous`, both answer empty with
# `view_as: true` (M7) — the section is personal, View As is read-only.
# ---------------------------------------------------------


def _admin_check(db: Session, current: CurrentHubUser):
    return lambda closures: is_hub_admin(db, current, closures=closures)


def _my_revisions_sync(db: Session, current: CurrentHubUser) -> MyRevisionsResponse:
    return thread_service.list_my_declined_items(
        db, current.info, hub_user_id=current.hub_user_id, is_admin=_admin_check(db, current)
    )


def _my_revisions_summary_sync(db: Session, current: CurrentHubUser) -> tuple[MyRevisionsSummary, str]:
    return thread_service.my_revisions_summary(
        db, current.info, hub_user_id=current.hub_user_id, is_admin=_admin_check(db, current)
    )


@router.get(
    "/threads/me/revisions",
    response_model=MyRevisionsResponse,
    summary="The caller's own declined and pending threads and edits, across every thread widget (My Revisions)",
    responses=VIEW_AS_ERROR_RESPONSES,
)
async def list_my_revisions(
    anonymous: bool = Depends(get_view_as_anonymous),
    db: Session = Depends(get_db_v2),
    current: CurrentHubUser = Depends(get_current_hub_user),
    selection: view_as_service.ViewAsSelection | None = Depends(get_view_as_selection),
):
    """Open items newest decline first (cap 100, `truncated`), closed ones
    newest close first (cap 50, `closed_truncated`), pending ones newest
    submission first (cap 100, `pending_truncated`). A widget the caller can
    no longer view drops out entirely."""
    if anonymous or selection is not None:
        return MyRevisionsResponse(view_as=True)
    return await asyncio.to_thread(_my_revisions_sync, db, current)


@router.get(
    "/threads/me/revisions/summary",
    response_model=MyRevisionsSummary,
    summary="My Revisions counts for the sidebar item and badge (polled; ETag/304)",
    responses=VIEW_AS_ERROR_RESPONSES,
)
async def get_my_revisions_summary(
    request: Request,
    response: Response,
    anonymous: bool = Depends(get_view_as_anonymous),
    db: Session = Depends(get_db_v2),
    current: CurrentHubUser = Depends(get_current_hub_user),
    selection: view_as_service.ViewAsSelection | None = Depends(get_view_as_selection),
):
    if anonymous or selection is not None:
        summary, etag = thread_service.my_revisions_view_as_answer()
    else:
        summary, etag = await asyncio.to_thread(_my_revisions_summary_sync, db, current)
    # The `/threads/moderation/summary` shape: `no-store` on the 200 AND the
    # 304, so the browser's URL-keyed HTTP cache never serves one user's
    # body to the next (followups plan §1).
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={"ETag": etag, "Cache-Control": "no-store"},
        )
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store"
    return summary


# ---------------------------------------------------------
# Threads
# ---------------------------------------------------------


@router.get(
    "/threads/component/{link}",
    response_model=ThreadListResponse,
    summary="One page of threads for a thread widget, newest first",
    responses=THREAD_VIEW_AS_RESPONSES,
)
async def list_threads(
    link: str = Path(..., description="The thread widget component's stable `link`."),
    cursor: str | None = Query(default=None, description="Opaque next-page cursor."),
    anonymous: bool = Depends(get_view_as_anonymous),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
    view_as_access: ViewAsAccess = Depends(get_view_as_access),
):
    """Own pending/rejected threads, votes and pending edits are the
    caller's, whatever `view_as` pairs or `view_as_user` are selected, and
    nobody's with `anonymous` (approved threads only, `my_vote` 0)."""
    subject = _thread_subject(user, anonymous)
    result = await asyncio.to_thread(
        thread_service.list_threads_for_link,
        db,
        link,
        cursor,
        subject,
        access=view_as_access.access,
        anonymous=anonymous,
    )
    return result.model_copy(
        update={"view_as": view_as_access.view_as, "view_as_user": view_as_access.view_as_user}
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
    cursor: str | None = Query(default=None, description="Opaque next-page cursor; it names its order."),
    order: Literal["oldest", "newest"] = Query(
        default="oldest", description="Submission order. A cursor issued under the other order is a 400."
    ),
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.list_pending_threads, db, cursor, order=order, access=access
    )


@router.get(
    "/threads/moderation/history",
    response_model=ThreadHistoryListResponse,
    summary="Every explicit decision on a thread the caller moderates — one row per decision — filterable, searchable, groupable",
    responses={400: {"description": "Malformed cursor"}},
)
async def list_thread_history(
    cursor: str | None = Query(default=None, description="Opaque next-page cursor; resend the same filters."),
    status_filter: list[Literal["approved", "rejected"]] | None = Query(
        default=None, alias="status", description="Repeatable. Default: both."
    ),
    type_filter: list[Literal["new", "resubmission"]] | None = Query(
        default=None, alias="type", description="Round 0 (new) or a resubmission. Repeatable. Default: both."
    ),
    q: str | None = Query(
        default=None, max_length=200, description="Substring of the decided title, current title or widget title."
    ),
    group: Literal["thread", "widget"] | None = Query(
        default=None, description="Keep each thread's / widget's rows together."
    ),
    order: Literal["newest", "oldest"] = Query(default="newest"),
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
        types=type_filter,
        q=q,
        group=group,
        order=order,
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


# plan_thread_review_merge_2026-11-11.md §5 B2 — Thread Review's History:
# both decision tables in one deduplicated log. Static, so declared here
# with the other `/threads/moderation/*` routes, above `/threads/{thread_id}`.
@router.get(
    "/threads/moderation/activity",
    response_model=ThreadActivityListResponse,
    summary="Every decision on threads and edits the caller moderates — one row per decision — filterable, searchable, groupable",
    responses={400: {"description": "Malformed cursor, or a cursor from another group / order"}},
)
async def list_thread_activity(
    cursor: str | None = Query(default=None, description="Opaque next-page cursor; resend the same filters, group and order."),
    kind: list[Literal["new", "resubmission", "edit", "editor_edit", "published_directly"]] | None = Query(
        default=None, description="Repeatable. Default: all five."
    ),
    outcome: list[Literal["approved", "rejected", "overwritten", "published_directly"]] | None = Query(
        default=None, description="Repeatable. Default: all four."
    ),
    widget: list[str] | None = Query(
        default=None, description="Widget `component_link`. Repeatable. Narrows within the moderated set."
    ),
    q: str | None = Query(
        default=None,
        max_length=200,
        description="Substring of the decided title, current title, widget title or reason.",
    ),
    group: Literal["thread", "widget"] | None = Query(
        default=None, description="Keep each thread's / widget's rows together."
    ),
    order: Literal["newest", "oldest"] = Query(default="newest"),
    date_from: datetime | None = Query(
        default=None, description="Decision instant, inclusive (ISO-8601; naive = UTC)."
    ),
    date_to: datetime | None = Query(
        default=None, description="Decision instant, exclusive (ISO-8601; naive = UTC)."
    ),
    author: list[str] | None = Query(default=None, description="Submitter email. Repeatable."),
    reviewer: list[str] | None = Query(default=None, description="Decider email. Repeatable."),
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.list_thread_activity,
        db,
        cursor,
        kinds=kind,
        outcomes=outcome,
        widgets=widget,
        date_from=date_from,
        date_to=date_to,
        authors=author,
        reviewers=reviewer,
        q=q,
        group=group,
        order=order,
        access=access,
    )


@router.get(
    "/threads/moderation/activity/facets",
    response_model=ThreadActivityFacets,
    summary="Thread Review History's author, reviewer and widget filter options",
)
async def get_thread_activity_facets(
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(thread_service.thread_activity_facets, db, access=access)


@router.get(
    "/threads/moderation/summary",
    response_model=ThreadModerationSummary,
    summary="Whether the caller moderates anything, and how many threads are pending",
    responses=VIEW_AS_ERROR_RESPONSES,
)
async def get_moderation_summary(
    request: Request,
    response: Response,
    db: Session = Depends(get_db_v2),
    view_as_access: ViewAsAccess = Depends(get_view_as_access),
):
    summary, etag = await asyncio.to_thread(
        thread_service.moderation_summary,
        db,
        access=view_as_access.access,
        view_as=view_as_access.view_as,
        view_as_user=view_as_access.view_as_user,
    )
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
    cursor: str | None = Query(default=None, description="Opaque next-page cursor; it names its order."),
    order: Literal["oldest", "newest"] = Query(
        default="newest", description="Submission order. A cursor issued under the other order is a 400."
    ),
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.list_pending_revisions, db, cursor, order=order, access=access
    )


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
    q: str | None = Query(
        default=None, max_length=200, description="Substring of the revision title, current title or widget title."
    ),
    group: Literal["thread", "widget"] | None = Query(
        default=None, description="Keep each thread's / widget's rows together."
    ),
    order: Literal["newest", "oldest"] = Query(default="newest"),
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
        q=q,
        group=group,
        order=order,
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
    responses=VIEW_AS_ERROR_RESPONSES,
)
async def get_revision_moderation_summary(
    request: Request,
    response: Response,
    db: Session = Depends(get_db_v2),
    view_as_access: ViewAsAccess = Depends(get_view_as_access),
):
    summary, etag = await asyncio.to_thread(
        thread_service.revision_moderation_summary,
        db,
        access=view_as_access.access,
        view_as=view_as_access.view_as,
        view_as_user=view_as_access.view_as_user,
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
        409: {
            "description": "Thread has already been reviewed, or `detail.code` is "
            "`thread_changed` (`expected_edit_count` no longer matches)"
        },
    },
)
async def approve_thread(
    payload: ThreadDecisionRequest | None = None,
    thread_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.decide_thread,
        db,
        thread_id,
        user,
        approve=True,
        expected_edit_count=payload.expected_edit_count if payload is not None else None,
        access=access,
    )


@router.post(
    "/threads/{thread_id}/reject",
    response_model=ThreadModerationRow,
    summary="Reject a pending thread, with a required reason — an editor of its widget or any ancestor",
    responses={
        403: {"description": "Caller is not an editor of this discussion"},
        409: {
            "description": "Thread has already been reviewed, or `detail.code` is "
            "`thread_changed` (`expected_edit_count` no longer matches)"
        },
        422: {"description": "Missing body, or `reason` empty/whitespace or over 1000 characters"},
    },
)
async def reject_thread(
    payload: RejectRequest,
    thread_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.decide_thread,
        db,
        thread_id,
        user,
        approve=False,
        reason=payload.reason,
        expected_edit_count=payload.expected_edit_count,
        access=access,
    )


# ---------------------------------------------------------
# Author rework of a declined thread
# (plan_thread_reject_resubmit_2026-11-07.md §4.3, §4.5, §4.6). Author-only;
# the gates live in the service.
# ---------------------------------------------------------


@router.post(
    "/threads/{thread_id}/resubmit",
    response_model=ThreadDetail,
    summary="Resubmit your own declined thread with changes",
    responses={
        403: {"description": "No access to the widget, or caller is not the thread's author"},
        404: {"description": "Thread not found"},
        409: {"description": "`detail.code` is `not_rejected`, `closed` or `unchanged_resubmission`"},
    },
)
async def resubmit_thread(
    payload: ThreadResubmitRequest,
    thread_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.resubmit_thread,
        db,
        thread_id,
        user,
        payload.title,
        payload.content,
        payload.mentions,
        access=access,
    )


@router.post(
    "/threads/{thread_id}/close",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Close your own declined thread (final; it stays readable under Closed)",
    responses={
        403: {"description": "No access to the widget, or caller is not the thread's author"},
        404: {"description": "Thread not found"},
        409: {"description": "`detail.code` is `not_rejected`"},
    },
)
async def close_thread(
    thread_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
    access: ViewerAccess = Depends(get_viewer_access),
):
    await asyncio.to_thread(
        thread_service.close_rejected_thread, db, thread_id, user, access=access
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/threads/{thread_id}/submission-history",
    response_model=list[ThreadSubmissionRound],
    summary="Every decided submission round of a thread, plus the pending one — author or editor",
    responses={
        403: {"description": "Caller does not satisfy the widget's access control"},
        404: {"description": "Thread not found, or caller may not see it"},
    },
)
async def get_thread_submission_history(
    thread_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.get_thread_submission_history, db, thread_id, user, access=access
    )


@router.get(
    "/threads/{thread_id}/timeline",
    response_model=ThreadTimelineResponse,
    summary="One thread's whole lifecycle — every decision plus its pending items — editors of its widget",
    responses={
        403: {"description": "Caller does not satisfy the widget's access control"},
        404: {"description": "Thread not found, or caller is not an editor of its widget"},
    },
)
async def get_thread_timeline(
    thread_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(thread_service.get_thread_timeline, db, thread_id, access=access)


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
            "or `earlier_revisions_pending` (older pending edits exist, and/or open declined "
            "edits below this version would be closed as superseded — `detail.earlier` / "
            "`detail.declined_open` — and `overwrite` was not set) or `revision_changed` "
            "(`expected_edit_count` no longer matches the edit)"
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
        expected_edit_count=payload.expected_edit_count if payload is not None else None,
        access=access,
    )


@router.post(
    "/threads/{thread_id}/revisions/{revision_id}/reject",
    response_model=ThreadRevisionDetail,
    summary="Reject a staged edit, with a required reason — an editor of its widget or any ancestor",
    responses={
        403: {"description": "Caller is not an editor of this discussion"},
        404: {"description": "No such revision on this thread"},
        409: {"description": "`detail.code` is `already_decided` or `revision_changed`"},
        422: {"description": "Missing body, or `reason` empty/whitespace or over 1000 characters"},
    },
)
async def reject_thread_revision(
    payload: RejectRequest,
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
        reason=payload.reason,
        expected_edit_count=payload.expected_edit_count,
        access=access,
    )


@router.post(
    "/threads/{thread_id}/revisions/{revision_id}/resubmit",
    response_model=ThreadUpdateResult,
    summary="Rework your own declined edit into a linked new revision",
    responses={
        403: {"description": "No access to the widget, or caller did not submit this edit"},
        404: {"description": "No such revision on this thread"},
        409: {
            "description": "`detail.code` is `not_rejected`, `closed` (with `closed_kind` / "
            "`closed_by_version`), `already_resubmitted`, `pending_edit_exists` "
            "(non-editor: the thread already has a pending author edit), "
            "`unchanged_resubmission`, `no_change` or `earlier_revisions_pending`"
        },
    },
)
async def resubmit_thread_revision(
    payload: RevisionResubmitRequest,
    thread_id: int = Path(...),
    revision_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.resubmit_revision,
        db,
        thread_id,
        revision_id,
        user,
        payload.title,
        payload.content,
        payload.mentions,
        overwrite=payload.overwrite,
        access=access,
    )


@router.post(
    "/threads/{thread_id}/revisions/{revision_id}/close",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Close your own declined edit (final; it stays readable under Closed)",
    responses={
        403: {"description": "No access to the widget, or caller did not submit this edit"},
        404: {"description": "No such revision on this thread"},
        409: {"description": "`detail.code` is `not_rejected` or `already_resubmitted`"},
    },
)
async def close_thread_revision(
    thread_id: int = Path(...),
    revision_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
    access: ViewerAccess = Depends(get_viewer_access),
):
    await asyncio.to_thread(
        thread_service.close_rejected_revision, db, thread_id, revision_id, user, access=access
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/threads/{thread_id}/revisions/{revision_id}/chain",
    response_model=list[ThreadRevisionDetail],
    summary="The rework chain a revision belongs to, oldest first — author or editor",
    responses={
        403: {"description": "Caller does not satisfy the widget's access control"},
        404: {"description": "No such revision on this thread, or caller may not see it"},
    },
)
async def get_thread_revision_chain(
    thread_id: int = Path(...),
    revision_id: int = Path(...),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
    access: ViewerAccess = Depends(get_viewer_access),
):
    return await asyncio.to_thread(
        thread_service.get_revision_chain, db, thread_id, revision_id, user, access=access
    )


@router.get(
    "/threads/{thread_id}",
    response_model=ThreadDetailViewAsResponse,
    summary="Fetch one thread's full content — any viewer with widget access",
    responses={**THREAD_VIEW_AS_RESPONSES, 404: {"description": "Thread not found"}},
)
async def get_thread(
    thread_id: int = Path(...),
    anonymous: bool = Depends(get_view_as_anonymous),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
    view_as_access: ViewAsAccess = Depends(get_view_as_access),
):
    """Access, `my_vote`, authorship (who may open a pending/rejected
    thread) and the authored pending edits follow `_thread_subject`; edit
    rights (`viewer_is_editor`) follow the View As access."""
    subject = _thread_subject(user, anonymous)
    detail = await asyncio.to_thread(
        thread_service.get_thread_by_id, db, thread_id, subject, access=view_as_access.access
    )
    return ThreadDetailViewAsResponse(
        **detail.model_dump(), view_as=view_as_access.view_as, view_as_user=view_as_access.view_as_user
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
            "edits and/or open declined edits this would close (`detail.declined_open`) "
            "and `overwrite` was not set; or (non-editor) `declined_edit_open` (an open "
            "declined edit blocks a NEW edit — resubmit or close it), `unchanged_resubmission` "
            "(a pending rework, or a pending resubmitted thread, edited back to the declined "
            "text) or `no_change` (a pending rework edited back to the live text). A save "
            "while a pending edit exists updates that edit in place"
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
    responses={**THREAD_VIEW_AS_RESPONSES, 404: {"description": "Thread not found"}},
)
async def list_comments(
    thread_id: int = Path(...),
    cursor: str | None = Query(default=None, description="Opaque next-page cursor."),
    anonymous: bool = Depends(get_view_as_anonymous),
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
    view_as_access: ViewAsAccess = Depends(get_view_as_access),
):
    """Access follows the View As access; each comment's `my_vote` follows
    `_thread_subject`."""
    subject = _thread_subject(user, anonymous)
    result = await asyncio.to_thread(
        thread_service.list_comments_for_thread, db, thread_id, cursor, subject, access=view_as_access.access
    )
    return result.model_copy(
        update={"view_as": view_as_access.view_as, "view_as_user": view_as_access.view_as_user}
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
