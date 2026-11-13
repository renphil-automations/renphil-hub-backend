"""Thread widget service layer (plan_thread_widget_2026-08-17.md) — Phase 1
(threads/comments/votes) + Phase 3 (mentions) + Phase 4 (notifications).

Threads + comments + votes: access-checked, `link`-addressed reads/writes,
keyset pagination, and recompute-from-source counters. Mentions (plan §5)
are validated and stored — see `derive_mention_token`,
`list_mentionable_users` and `_validate_and_resolve_mentions` below — and,
as of plan_thread_moderation_2026-09-18.md phase 2m (M9), SCOPED to the
people who can open the thread: the directory is per thread widget and
both it and the validator intersect the roster with
`_granted_emails_on_component`, the read-time fold's `granted_view`
inverted for that one node (`access_visibility_service.users_granted_on_node`).
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

Moderation (plan_thread_moderation_2026-09-18.md phase 2a): a viewer's new
thread lands `pending` until an editor of the widget or any of its
ancestors (`_is_component_editor`, `edit(n)` — plan §0.1(2)) approves or
rejects it (`decide_thread`); an editor's own post auto-approves. Comments
and votes gate on `_require_approved` — nothing hangs off a non-approved
thread, for anyone, author included. The "Moderation" section below also
serves the "Threads Management" page's pending queue, history list and
polled summary (`list_pending_threads`, `list_thread_history`,
`moderation_summary`), each scoped to `_moderated_component_ids` — the set
of thread widgets the caller can moderate, a Hub Admin's being every one.

Thread versioning (plan_thread_edit_versioning_2026-09-22.md, phase A, with
the 2026-09-23 amendments): every PUBLISHED version of a thread is a
`thread_revisions` row, and `threads` caches the row at `threads.version`.
v1 (`origin='original'`) is written when a thread is published
(`_write_original_revision` — at create for an editor's post, in
`decide_thread` on approval for a viewer's). A NON-editor author's edit of
an approved thread is staged as a pending `origin='author'` row
(`_stage_thread_edit`) while the thread stays `approved` and readable; an
editor decides each one (`decide_thread_revision`), in version order or
with an explicit `overwrite`. An editor-author's edit goes live at once as
an auto-approved `origin='editor'` row (`_apply_editor_edit`), refused while
author edits are pending unless `overwrite` is set. Revisions have their
OWN management section — `list_pending_revisions`, `list_revision_history`,
`revision_history_facets`, `revision_moderation_summary` — and never appear
in the Threads Management functions above them. Who may even learn that a
thread has staged edits is ONE predicate, `_can_see_thread_revisions`
(author or component editor).

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
import hashlib
import json
import logging
import re
import unicodedata
from datetime import datetime, timezone
from types import SimpleNamespace
from collections.abc import Callable, Collection
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import (
    Integer,
    String,
    and_,
    case,
    false,
    func,
    literal_column,
    not_,
    or_,
    select,
    tuple_,
    union_all,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased, object_session

from app.db_v2.models.component import ComponentV2
from app.db_v2.models.notification import NotificationV2
from app.db_v2.models.page_content import PageContentV2
from app.db_v2.models.thread import (
    THREAD_WIDGET_TYPE,
    ThreadCommentV2,
    ThreadRevisionV2,
    ThreadSubmissionV2,
    ThreadV2,
    ThreadVoteV2,
)
from app.db_v2.models.user import UserV2
from app.models.auth import UserInfo
from app.schemas.thread import (
    MAX_MENTIONS_PER_POST,
    CommentListResponse,
    CommentSummary,
    DeclinedEditItem,
    DeclinedItemsResponse,
    DeclinedThreadItem,
    MentionableUser,
    MentionInput,
    MyRevisionsResponse,
    MyRevisionsSummary,
    NotificationEntry,
    NotificationListResponse,
    PendingEditItem,
    PendingThreadItem,
    PreviousRejection,
    RevisionPerson,
    ThreadDetail,
    ThreadHistoryDecision,
    ThreadHistoryFacets,
    ThreadHistoryListResponse,
    ThreadHistoryRow,
    ThreadListResponse,
    ThreadModerationListResponse,
    ThreadModerationRow,
    ThreadModerationSummary,
    ThreadRevisionDetail,
    ThreadRevisionHistoryFacets,
    ThreadRevisionModerationListResponse,
    ThreadRevisionModerationRow,
    ThreadRevisionModerationSummary,
    ThreadRevisionSummary,
    ThreadRoundDecision,
    ThreadActivityFacets,
    ThreadActivityListResponse,
    ThreadActivityRow,
    ThreadActivityWidget,
    ThreadSubmissionRound,
    ThreadSummary,
    ThreadTimelineResponse,
    ThreadUpdateResult,
    ThreadWidgetCounts,
    VoteResponse,
)
from app.services.access_visibility_service import (
    NodeRef,
    ViewerAccess,
    granted_single_node,
    users_granted_on_node,
    walk_ancestors,
)
from app.services.rbac_graph_service import RbacClosures
from app.services.resource_grant_service import _hub_user_email_map, resolve_node_labels

logger = logging.getLogger(__name__)

THREADS_PAGE_SIZE = 20
COMMENTS_PAGE_SIZE = 50
NOTIFICATIONS_PAGE_SIZE = 20

# Notification triggers. D6 (plan_thread_widget_2026-08-17.md) fixed the
# first two as "the only two"; plan_thread_moderation_2026-09-18.md §3.2 +
# M1 (owner-approved 2026-09-19, phase 2d) added the two decision types —
# the AUTHOR of a moderated thread is told when an editor approves or
# rejects it. plan_thread_edit_versioning_2026-09-22.md §4.6/§8 (phase D)
# added the two EDIT decision types — the SUBMITTER of a staged edit to an
# approved thread is told when an editor approves or declines that edit.
# Same `notifications` row shape for all six; `type` is a `String(32)` with
# no CHECK, so the new values needed no migration.
NOTIFICATION_TYPE_MENTION = "mention"
NOTIFICATION_TYPE_THREAD_COMMENT = "thread_comment"
NOTIFICATION_TYPE_THREAD_APPROVED = "thread_approved"
NOTIFICATION_TYPE_THREAD_REJECTED = "thread_rejected"
NOTIFICATION_TYPE_THREAD_EDIT_APPROVED = "thread_edit_approved"
NOTIFICATION_TYPE_THREAD_EDIT_REJECTED = "thread_edit_rejected"
# plan_thread_reject_resubmit_2026-11-07.md §12.6 (owner decision R4): an
# OPEN declined edit was closed automatically because a newer version of its
# thread went live (§12.4). Recipient = the edit's submitter, actor = whoever
# published that version; excerpt "Superseded by v{K}". No migration (same
# `String(32)`, no CHECK).
NOTIFICATION_TYPE_THREAD_EDIT_CLOSED = "thread_edit_closed"
# The Airtable sync's two (plan_airtable_sync v1 §9, C3), written by
# `airtable_sync/notifications.py`. They carry no thread or component:
# `component_id` / `thread_id` are NULL and `link` says where to go.
NOTIFICATION_TYPE_AIRTABLE_SYNC_STARTED = "airtable_sync_started"
NOTIFICATION_TYPE_AIRTABLE_SYNC_FINISHED = "airtable_sync_finished"

# Thread status values (D5, lifecycle in plan_thread_moderation_2026-09-18.md
# §2). An editor's own post, or a decided pending one, is 'approved'; a
# viewer's post lands 'pending' until an editor decides. A PATCH on a
# 'rejected' thread is still a 409 (no resubmit-on-edit), but since
# plan_thread_reject_resubmit_2026-11-07.md the author can resubmit it
# through the explicit `resubmit_thread` (back to 'pending') or close it for
# good (`close_rejected_thread`, plan §12).
THREAD_STATUS_APPROVED = "approved"
THREAD_STATUS_PENDING = "pending"
THREAD_STATUS_REJECTED = "rejected"

# Revision status values (plan_thread_edit_versioning_2026-09-22.md §1.1) —
# a staged edit's OWN lifecycle, separate from the thread's above: the
# thread stays 'approved' throughout. 'overwritten' marks a pending revision
# displaced by an explicit overwrite-approve of a NEWER one (owner decision
# 4); it is terminal, like 'approved'/'rejected'.
REVISION_STATUS_PENDING = "pending"
REVISION_STATUS_APPROVED = "approved"
REVISION_STATUS_REJECTED = "rejected"
REVISION_STATUS_OVERWRITTEN = "overwritten"
REVISION_DECIDED_STATUSES = (
    REVISION_STATUS_APPROVED,
    REVISION_STATUS_REJECTED,
    REVISION_STATUS_OVERWRITTEN,
)

# Where a revision came from (amendment A3, 2026-09-23) — see
# `ThreadRevisionV2`'s docstring. Only 'author' rows are ever pending.
REVISION_ORIGIN_ORIGINAL = "original"
REVISION_ORIGIN_AUTHOR = "author"
REVISION_ORIGIN_EDITOR = "editor"
REVISION_ORIGINS = (REVISION_ORIGIN_ORIGINAL, REVISION_ORIGIN_AUTHOR, REVISION_ORIGIN_EDITOR)

# plan_thread_reject_resubmit §12.3 — why a declined item was closed. A
# thread is only ever closed by its author; an edit also closes itself when
# a newer version of its thread goes live (§12.4).
CLOSED_KIND_AUTHOR = "author"
CLOSED_KIND_SUPERSEDED = "superseded"

# plan §12.8 G3 — the status of a SYNTHESIZED submission round (never a
# `thread_submissions` row): an editor-author's resubmission that went live
# at once, without a decision.
SUBMISSION_STATUS_PUBLISHED = "published"


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


def resolve_thread_widget_component(db: Session, link: str) -> ComponentV2:
    """`_get_thread_widget_component` with the 404 every `link` route in
    this module raises on a miss — public so the mention-directory route,
    which gates the caller in the router rather than in here (moderation
    plan §5.9.3), resolves the widget through the same rule instead of
    reaching into a private helper."""
    component = _get_thread_widget_component(db, link)
    if component is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread widget not found")
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


def _lock_thread(db: Session, thread_id: int) -> ThreadV2 | None:
    """Re-read the threads row `FOR UPDATE` and REFRESH the session's copy.

    Callers have usually loaded `thread` already (unlocked, through
    `_require_thread_and_component`). Without `populate_existing()` the ORM
    hands back that same identity-mapped object with its PRE-lock values: a
    decision that blocked on an author's in-flight PATCH would then check
    the old `edit_count` (U9) / `status` and act on text nobody read.
    Sessions here run `autoflush=False`, so call this before writing to the
    row — a refresh discards unflushed changes."""
    return (
        db.query(ThreadV2)
        .filter(ThreadV2.id == thread_id)
        .with_for_update()
        .populate_existing()
        .first()
    )


def _require_comment_thread_and_component(
    db: Session, comment_id: int
) -> tuple[ThreadCommentV2, ThreadV2, ComponentV2]:
    comment = _get_comment(db, comment_id)
    if comment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Comment not found")
    thread, component = _require_thread_and_component(db, comment.thread_id)
    return comment, thread, component


# ---------------------------------------------------------
# Access control — plan §4.1 (thread plan): view gates read+post, authorship
# gates edit, Hub Admin gates delete. RE-GATED by
# plan_ac_enforcement_closeout_2026-09-09.md §4: view/post now run through
# the read-time visibility fold (plan_access_control_algorithm_2026-08-27.md
# §5) via `ViewerAccess.is_granted` — the PAYLOAD gate, not the chrome one
# (§5.2: `granted` gates content, `visible` gates a shell's title/nav
# presence; a node that is merely REVEALED — visible only because some
# descendant is granted — must not serve its discussion, exactly as the
# canvas serializer already returns `content: None` for a revealed
# component). This REPLACES the legacy per-widget `access_control` JSONB
# blob this file used to read — that column is no longer consulted here at
# all. `airtable.py`'s six component endpoints have NOT been migrated to the
# fold as of this step (plan_ac_enforcement_closeout_2026-09-09.md §7 scopes
# that to a separate step) — the two surfaces' checks have diverged; do not
# assume they still match, and do not "fix" airtable.py to match this file
# outside its own step.
#
# `access: ViewerAccess | None` — `None` means "no check requested", the
# same convention `access_visibility_service.require_edit` already
# documents (an internal caller that never intended a gate keeps working
# unchanged). Every route in `threads.py` always supplies a real
# `ViewerAccess` from `Depends(get_viewer_access)`.
# ---------------------------------------------------------


def _check_view_access(component: ComponentV2, access: ViewerAccess | None) -> None:
    if access is not None and not access.is_granted(("component", component.id)):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You do not have access to this discussion",
        )


def _require_hub_admin(access: ViewerAccess | None) -> None:
    """`ViewerAccess.full_access` IS the answer to "is this caller a Hub
    Admin", by construction — `resolve_viewer_access` sets it from
    `dependencies.is_hub_admin`'s result, computed once per request in
    `get_viewer_access`. Routing through it here costs no second closure
    query (plan §6.8's "do not make it its own query"); importing
    `dependencies.is_hub_admin` directly would run backwards (a domain
    service depending on a FastAPI dependency module), the same reasoning
    `access_visibility_service` already gives for refusing that import.

    THE COUPLING IS REAL AND MUST STAY WATCHED: `full_access` means
    "bypasses the fold", which is TODAY true if and only if the caller is a
    Hub Admin. If a second reason to bypass the fold is ever added to
    `ViewerAccess.full_access`, this delete gate silently widens to cover it
    too, with nothing here changing. The warning belongs on
    `ViewerAccess.full_access`'s own definition
    (`access_visibility_service.py`) — see that field's docstring — not only
    at this call site."""
    if access is not None and not access.full_access:
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
# Moderation gates (plan_thread_moderation_2026-09-18.md §5.1, §5.2) — a
# viewer's new thread requires approval from an EDITOR of the widget or any
# of its ancestors (0.1(2)); an editor's own post auto-approves (M8's mirror
# case falls out of this definition for free — see `_moderated_component_ids`
# below).
# ---------------------------------------------------------


def _is_component_editor(component: ComponentV2, access: ViewerAccess | None) -> bool:
    """`access=None` deliberately does NOT keep this module's usual "no check
    requested" convention (`_check_view_access`'s) — an internal caller with
    no `ViewerAccess` is NOT an editor for auto-approval purposes, so its
    post lands `pending` rather than silently auto-approving (plan §5.1's
    own note, pinned by the `access=None` → `pending` test)."""
    return access is not None and access.verdict(("component", component.id)).edit


def _require_component_editor(component: ComponentV2, access: ViewerAccess | None) -> None:
    if not _is_component_editor(component, access):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only an editor of this discussion can approve or reject threads",
        )


def _require_approved(thread: ThreadV2) -> None:
    """Comments and votes can only ever hang off an APPROVED thread (plan
    §2's visibility table) — same wording `get_thread_by_id` already uses
    for a non-approved thread, so a pending/rejected thread's comments read
    identically to a thread that doesn't exist, for everyone including its
    own author (there is nothing to discuss until it's public)."""
    if thread.status != THREAD_STATUS_APPROVED:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found")


def _moderated_component_ids(db: Session, access: ViewerAccess | None) -> list[int]:
    """Every thread-widget component the caller can moderate — plan §5.2's
    "moderated set". A Hub Admin moderates every thread widget in the hub; a
    non-admin moderates exactly the ones `edit(n)` is true for. `access=None`
    → `[]`, matching every other internal-caller default in this module.

    A mirror-typed component (`type == 'mirror'`) is never in the query this
    filters (`WHERE type = 'thread'`), so M8 — "who approves a thread posted
    through a mirror? The target's editors" — falls OUT of this definition
    rather than needing a special case: a mirror is never itself a
    moderated node, and a thread posted "through" one is, by construction,
    stored against the TARGET component's own id (plan M8)."""
    rows = db.query(ComponentV2.id).filter(ComponentV2.type == THREAD_WIDGET_TYPE).all()
    if access is None:
        return []
    if access.full_access:
        return [row[0] for row in rows]
    return [row[0] for row in rows if access.verdict(("component", row[0])).edit]


def _can_see_thread_revisions(
    thread: ThreadV2, component: ComponentV2, user: UserInfo | None, access: ViewerAccess | None
) -> bool:
    """plan_thread_edit_versioning_2026-09-22.md §4.4 / landmine 3 — "may
    this caller know about this thread's staged edits": the thread's author
    (the only person who can submit one) or an editor of its widget or any
    ancestor (the people who decide them). Everyone else — a plain granted
    viewer included — must not learn that someone is editing a thread,
    which nothing in the product leaked before this feature.

    The ONE predicate behind `pending_revision_count`,
    `ThreadDetail.pending_revisions` and `GET .../revisions/{id}`, so the
    three can't drift apart. `access=None` → author only (the editor arm is
    `_is_component_editor`, which is False without a real `ViewerAccess`)."""
    is_author = user is not None and _norm_email(thread.author_email) == _norm_email(user.email)
    return is_author or _is_component_editor(component, access)


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
    """The ROSTER half of the eligibility rule (plan D7 as amended by D13,
    §5.1): `EndDate IS NULL OR EndDate > current_date`, `status`
    deliberately ignored, plus the structural exclusion of rows with no
    `work_email` (mentions are email-keyed). Shared by the directory endpoint
    AND mention validation below so the two definitions can never drift
    apart — the plan's own test list (§10) explicitly checks the directory
    and validation agree on who's eligible.

    The ACCESS half — M9, plan_thread_moderation_2026-09-18.md §5.9 — is
    `_granted_emails_on_component` below, applied by the same two callers
    for the same reason."""
    today = func.current_date()
    return db.query(UserV2).filter(
        or_(UserV2.end_date.is_(None), UserV2.end_date > today),
        UserV2.work_email.isnot(None),
        UserV2.work_email != "",
        # Soft-deleted people can't be mentioned (plan_admin_soft_delete_user §3.4).
        UserV2.not_deleted(),
    )


def _granted_emails_on_component(
    db: Session, component: ComponentV2, *, closures: RbacClosures | None = None
) -> set[str]:
    """M9 (plan_thread_moderation_2026-09-18.md §0.1(5), §5.9): the
    normalised emails of every `hub_users` row that is GRANTED on this thread
    widget — a direct user grant or a matching `(role, scope)` grant on the
    component or any ancestor, view or edit level, or a Hub Admin by
    assignment. This is the definition of "can open the thread", and it
    amends the original plan's D7 ("you may mention anyone in that list
    even without access to the tab") by owner decision — not a proposal.

    ONE HELPER, TWO CALLERS (landmine 15): the directory
    (`list_mentionable_users`) and the validator
    (`_validate_and_resolve_mentions`) both intersect the roster with THIS
    set. A validator looser than the menu would let a hand-typed `@Token`
    mention someone the menu wouldn't offer; a stricter one would make the
    menu offer people whose mention then silently vanishes on submit.

    The set comes from `access_visibility_service.users_granted_on_node` —
    the read-time fold's `granted_view` inverted for one node — so it agrees
    exactly with the `is_granted` gate every thread read already applies to
    the caller; its property test is what makes that claim safe. A mirror
    never reaches here: a mirrored ThreadWidget posts to its TARGET's link
    (M8), and every resolver in this module refuses a non-thread type, so
    the same rule is asserted rather than "handled".

    Ids become emails through `resource_grant_service._hub_user_email_map`
    (one query). A roster row with no `hub_users` row — someone who has
    never signed in — is simply absent from the map: not mentionable, which
    is correct, since with no `hub_users` row they can hold no assignment
    and no grant and therefore cannot open the thread either.

    KNOWN GAP, NOT A BUG HERE (M10, plan §11.12): a Hub Admin recognised
    only by the Airtable role in their own JWT is invisible to this set on
    any widget they hold no grant on — nothing server-side can read another
    user's JWT. `rbac_graph_service.hub_admin_user_ids` is the single seam
    where the phase-3 `BOOTSTRAP_ADMIN_EMAILS` backstop gets unioned in.
    """
    if component.type != THREAD_WIDGET_TYPE:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread widget not found")
    user_ids = users_granted_on_node(db, ("component", component.id), closures=closures)
    if not user_ids:
        return set()
    return {_norm_email(email) for email in _hub_user_email_map(db, user_ids).values()}


def list_mentionable_users(
    db: Session,
    component: ComponentV2,
    q: str | None,
    *,
    closures: RbacClosures | None = None,
) -> list[MentionableUser]:
    """`GET /threads/component/{link}/mentionable-users` (plan §5.1, scoped
    per M9 — moderation plan §4.1/§5.9.1). The roster eligibility filter
    runs in SQL; the access filter (`_granted_emails_on_component`) and the
    accent-insensitive substring/prefix match run in Python over that
    (small, ≤175-row) result set, per §5.1's own reasoning for why that
    split is the simplest correct thing at this table size. The cap of 8
    applies AFTER both filters, so an ungranted roster row can never eat a
    slot.

    The caller's OWN access to `component` is the router's job
    (`granted_single_node`, not `get_viewer_access` — moderation plan
    §5.9.3 / landmine 16); this function lists people, it does not gate the
    caller. `closures` is the request's shared `RbacClosures` (§8.2).
    """
    granted_emails = _granted_emails_on_component(db, component, closures=closures)
    if not granted_emails:
        return []
    rows = [
        row
        for row in _eligible_mentionable_users_query(db).all()
        if _norm_email(row.work_email) in granted_emails
    ]
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
    db: Session,
    claimed: list[MentionInput],
    content: str,
    *,
    component: ComponentV2,
    closures: RbacClosures | None = None,
) -> list[dict[str, str]]:
    """plan §5.3 — the server does not trust the client's `mentions` array.
    For each claimed email: (1) it must belong to an eligible `users` row
    (the SAME eligibility rule the directory applies — `_eligible_
    mentionable_users_query`), (1b) that person must be GRANTED on
    `component` (M9 — the SAME access rule the directory applies,
    `_granted_emails_on_component`; moderation plan §5.9.2), and (2) the
    token the SERVER derives from that row's name must actually occur in
    `content` as `@<token>` at a word boundary. Anything failing any check
    is silently dropped (never a 4xx for the whole request — plan §5.3:
    "this closes the obvious hole"); an ungranted roster user is dropped
    exactly as a non-roster email always has been. `name`/`token` on the
    returned entries always come from the matched `users` row, never from
    `claimed` — a client cannot make a chip render someone else's name.

    Consequence worth knowing (moderation plan §11.14): an author editing an
    OLD post resends its stored mentions, and anyone who has since lost
    access to the widget is dropped from the stored array on that edit —
    the literal `@Token` stays in the text, unstyled, and they are not
    re-notified. Correct under M9; surprising on first sight.

    `component` is the thread widget the post lives on — every caller has
    it in hand already. `closures` is optional: the four write paths hold a
    `ViewerAccess`, not an `RbacClosures`, so the reverse fold builds its
    own snapshot here (one extra closure load per WRITE, not per keystroke;
    the directory route is the hot path and shares its own).

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
    if not eligible_rows:
        return []  # nothing survived check 1 — no reason to run the fold
    # Check 1b (M9): the same access set the directory offers, computed
    # once per request rather than once per claimed email.
    granted_emails = _granted_emails_on_component(db, component, closures=closures)
    eligible_by_email = {
        _norm_email(row.work_email): row
        for row in eligible_rows
        if _norm_email(row.work_email) in granted_emails
    }

    resolved: list[dict[str, str]] = []
    for email in ordered_emails:
        row = eligible_by_email.get(email)
        if row is None:
            continue  # check 1 or 1b failed — not an eligible, granted users row
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
# deletion, negated-class spans and anchored line-leading forms. No nested
# quantifiers, no backreferences, no alternation over `.*`. This still does
# NOT parse markdown in general; the one construct it does recognize is an
# inline link `[text](url)` (and the `:color[text]{…}` directive, same
# shape), because stripping its brackets glued label and URL together
# (`websitehttps://…`, 2026-10-06 owner report). Every span below is a
# negated class bounded by its own closing delimiter, so it stays linear.
_EXCERPT_LINE_LEADING_RE = re.compile(r"^[ \t]*(?:[-+*]|\d+\.)[ \t]+", re.MULTILINE)
_EXCERPT_MARKDOWN_CHARS_RE = re.compile(r"[*_`~#>\[\]()]+")
_EXCERPT_WHITESPACE_RE = re.compile(r"\s+")
# Group 1 is the visible label of either form; the URL / directive
# attributes are never shown in a preview.
_EXCERPT_INLINE_LINK_RE = re.compile(
    r"(?::color)?\[([^\[\]\n]*)\](?:\([^()\s]*\)|\{[^{}\n]*\})"
)


def generate_notification_excerpt(content: str) -> str:
    """plan §4.8 — bound first, then strip, for a plain-text panel preview
    (never rendered as markdown — the risk this guards against is
    catastrophic backtracking on the write path, not XSS). Python string
    slicing already operates on code points, not bytes or UTF-16 units
    (same unit as §4.2 throughout), so bounding and the final trim are both
    "on a code-point boundary" for free — no separate handling needed to
    avoid corrupting a multi-code-point sequence.

    Links keep only their label (`[website](https://…)` -> `website`)."""
    bounded = (content or "")[:_EXCERPT_INPUT_BOUND]
    stripped = _EXCERPT_LINE_LEADING_RE.sub("", bounded)
    stripped = _EXCERPT_INLINE_LINK_RE.sub(r"\1", stripped)
    stripped = _EXCERPT_MARKDOWN_CHARS_RE.sub("", stripped)
    collapsed = _EXCERPT_WHITESPACE_RE.sub(" ", stripped).strip()
    if len(collapsed) <= _EXCERPT_OUTPUT_LENGTH:
        return collapsed
    return collapsed[:_EXCERPT_OUTPUT_LENGTH].rstrip() + "…"


# The thread-card preview (2026-10-06): unlike the panel excerpt above this
# one IS rendered as markdown (ThreadWidget / PendingThreadCard), so inline
# syntax — links, emphasis, code, `:color` — is KEPT and only block syntax
# is flattened into one line. Same safety rules (bound first, linear
# patterns only). The input bound is larger than the panel's because the
# URLs it now keeps don't count toward the visible length.
_PREVIEW_INPUT_BOUND = 1500
_PREVIEW_BLOCK_LINE_RE = re.compile(
    r"^[ \t]*(?:```|~~~)[^\n]*$|^[ \t]*(?:---+|\*\*\*+|___+)[ \t]*$", re.MULTILINE
)
_PREVIEW_LINE_LEADING_RE = re.compile(
    r"^[ \t]*(?:#{1,6}|>+|[-+*]|\d+\.)[ \t]+", re.MULTILINE
)
# Paired inline markers a cut can leave unclosed — an odd count would render
# the marker literally, so the last one is dropped.
_PREVIEW_PAIRED_MARKERS = ("**", "~~", "`")


def generate_preview_excerpt(content: str) -> str:
    """Markdown-preserving card preview: block syntax flattened, inline
    syntax kept, at most `_EXCERPT_OUTPUT_LENGTH` VISIBLE characters (a
    link counts only its label). A link is never cut in half: it is either
    kept whole or left out, and a trailing partial link left by the input
    bound is dropped rather than shown as raw `[label](ht…`."""
    raw = content or ""
    bounded = raw[:_PREVIEW_INPUT_BOUND]
    input_cut = len(raw) > _PREVIEW_INPUT_BOUND
    flat = _PREVIEW_BLOCK_LINE_RE.sub("", bounded)
    flat = _PREVIEW_LINE_LEADING_RE.sub("", flat)
    flat = _EXCERPT_WHITESPACE_RE.sub(" ", flat).strip()

    budget = _EXCERPT_OUTPUT_LENGTH
    parts: list[str] = []
    visible = 0
    pos = 0
    cut = False
    for match in _EXCERPT_INLINE_LINK_RE.finditer(flat):
        text = flat[pos:match.start()]
        if visible + len(text) >= budget:
            parts.append(text[: budget - visible])
            cut = True
            break
        parts.append(text)
        visible += len(text) + len(match.group(1))
        parts.append(match.group(0))
        pos = match.end()
    else:
        tail = flat[pos:]
        if input_cut and "[" in tail:
            # No complete link remains in `tail`, so a `[` here is most
            # likely a link the input bound sliced through.
            tail = tail[: tail.rfind("[")]
        if visible + len(tail) > budget:
            tail = tail[: budget - visible]
            cut = True
        parts.append(tail)
        cut = cut or input_cut

    excerpt = "".join(parts).rstrip()
    outside_links = _EXCERPT_INLINE_LINK_RE.sub("", excerpt)
    for marker in _PREVIEW_PAIRED_MARKERS:
        if outside_links.count(marker) % 2:
            idx = excerpt.rfind(marker)
            excerpt = excerpt[:idx] + excerpt[idx + len(marker):]
            outside_links = _EXCERPT_INLINE_LINK_RE.sub("", excerpt)
    excerpt = excerpt.rstrip()
    return excerpt + "…" if cut else excerpt


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


def _recipient_is_deleted(db: Session, email: str) -> bool:
    """A soft-deleted person gets no NEW notifications (plan_admin_soft_
    delete_user §3.4); their existing ones are kept. Mentions never reach
    here for them (the roster excludes deleted rows); this covers the
    author/submitter notifications, which are addressed by stored email."""
    return (
        db.query(UserV2.id)
        .filter(UserV2.matches_email(email), UserV2.is_deleted.is_(True))
        .first()
        is not None
    )


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
    if _recipient_is_deleted(db, thread_author):
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


REASON_EXCERPT_MAX_LENGTH = 200
_WHITESPACE_RUN = re.compile(r"\s+")


def _reason_excerpt(reason: str) -> str:
    """plan_thread_reject_resubmit_2026-11-07.md §4.8 / D9 — a decline's
    bell excerpt: `"Reason: " + reason`, whitespace runs collapsed to one
    space, truncated to 200 characters (an ellipsis replaces the overflow).

    Deliberately NOT `generate_notification_excerpt` (landmine 5): that is a
    MARKDOWN stripper and eats `()[]*_`, but a reason is plain text (D1) —
    `(see [doc])` must reach the bell verbatim."""
    text_ = "Reason: " + _WHITESPACE_RUN.sub(" ", reason or "").strip()
    if len(text_) <= REASON_EXCERPT_MAX_LENGTH:
        return text_
    return text_[: REASON_EXCERPT_MAX_LENGTH - 1].rstrip() + "…"


def _notify_thread_decision(
    db: Session,
    thread: ThreadV2,
    *,
    reviewer: UserInfo,
    approved: bool,
    component_id: int,
    reason: str | None = None,
) -> None:
    """plan_thread_moderation_2026-09-18.md §3.2 / M1 (owner-approved
    2026-09-19) — tell the AUTHOR that an editor decided their pending
    thread. One recipient (the author), one row, `type` chosen by
    `approved`; `actor_*` is the REVIEWER (they made the decision), the
    `excerpt` is the thread's own, `comment_id` is NULL because the event
    is about the thread body, not a comment.

    Self-exclusion copies `_notify_new_thread_comment`'s shape — "skip when
    the RECIPIENT is the actor" — NOT `_notify_mentions`' (which skips the
    actor inside a loop over recipients). Here the recipient is the author
    and the actor is the reviewer, so the comparison is written out
    explicitly against those two. The only way this fires is an editor
    deciding a thread they themselves posted back when they were a viewer
    (the create route auto-approves an editor's post, so an editor's own
    thread can only be `pending` if their grant arrived after they posted);
    the "no self-pings" rule both existing writers follow applies.

    A DECLINE's excerpt is the reviewer's reason (`_reason_excerpt`,
    plan_thread_reject_resubmit D9), not the thread's content; an approval's
    is unchanged.

    Does not commit — the caller (`decide_thread`) owns the transaction,
    same convention as every other write function in this module."""
    recipient = _norm_email(thread.author_email)
    reviewer_email = _norm_email(reviewer.email)
    if recipient == reviewer_email or _recipient_is_deleted(db, recipient):
        return
    db.add(
        NotificationV2(
            recipient_email=recipient,
            type=NOTIFICATION_TYPE_THREAD_APPROVED if approved else NOTIFICATION_TYPE_THREAD_REJECTED,
            actor_email=reviewer_email,
            actor_name=reviewer.name,
            component_id=component_id,
            thread_id=thread.id,
            comment_id=None,
            excerpt=(
                generate_notification_excerpt(thread.content)
                if approved or reason is None
                else _reason_excerpt(reason)
            ),
            read_at=None,
            created_at=_utc_now(),
        )
    )


def _notify_thread_edit_decision(
    db: Session,
    thread: ThreadV2,
    revision: ThreadRevisionV2,
    *,
    reviewer: UserInfo,
    approved: bool,
    component_id: int,
    reason: str | None = None,
) -> None:
    """plan_thread_edit_versioning_2026-09-22.md §4.6 — `_notify_thread_
    decision` transplanted onto a staged EDIT: tell the revision's SUBMITTER
    that an editor approved / declined it. Same shape (one recipient, one
    row, `actor_*` = the reviewer, `comment_id` NULL, explicit
    recipient-vs-reviewer self-exclusion), three differences:

    - the recipient is `revision.submitted_by_email`, not
      `thread.author_email` — equal today (PATCH is author-only), but the
      submitter is who is waiting on this decision;
    - `type` is the edit pair, so an author is never told "your thread was
      approved" about an edit to a thread that has been live for weeks;
    - `excerpt` is the REVISION's content — a declined edit's text never
      went live, so the thread's own content would describe the wrong thing.

    Called once per decision, for the decided revision only. Revisions an
    overwrite-approve displaces get NO row: one click, one bell row.

    A DECLINE's excerpt is the reviewer's reason (`_reason_excerpt`,
    plan_thread_reject_resubmit D9); an approval's is unchanged.

    Does not commit — the caller (`decide_thread_revision`) owns the
    transaction."""
    recipient = _norm_email(revision.submitted_by_email)
    reviewer_email = _norm_email(reviewer.email)
    if recipient == reviewer_email or _recipient_is_deleted(db, recipient):
        return
    db.add(
        NotificationV2(
            recipient_email=recipient,
            type=NOTIFICATION_TYPE_THREAD_EDIT_APPROVED if approved else NOTIFICATION_TYPE_THREAD_EDIT_REJECTED,
            actor_email=reviewer_email,
            actor_name=reviewer.name,
            component_id=component_id,
            thread_id=thread.id,
            comment_id=None,
            excerpt=(
                generate_notification_excerpt(revision.content)
                if approved or reason is None
                else _reason_excerpt(reason)
            ),
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


def _notification_visible(notification_component_id: int | None, access: ViewerAccess | None) -> bool:
    """The bell's access filter (plan_ac_enforcement_closeout §4.2). It
    applies only to rows that point at a widget: a row with no
    `component_id` (the Airtable sync's) has no widget to lose access to."""
    if access is None or notification_component_id is None:
        return True
    return access.is_granted(("component", notification_component_id))


def _to_notification_entry(
    notification: NotificationV2, thread_title: str | None, component_link: str | None
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
        link=notification.link,
        read_at=notification.read_at,
        created_at=notification.created_at,
    )


def list_notifications_for_user(
    db: Session,
    user: UserInfo,
    cursor: str | None,
    unread_only: bool,
    *,
    access: ViewerAccess | None = None,
) -> NotificationListResponse:
    """`GET /notifications` (plan §4.1) — self only, newest first, pages of
    `NOTIFICATIONS_PAGE_SIZE`. `thread_title` / `component_link` are joined
    live rather than stored (see `NotificationEntry`'s own docstring for
    why) — safe unconditionally because a notification cannot outlive its
    thread or component (`ON DELETE CASCADE`, plan §3.4). The joins are
    OUTER joins: the Airtable sync's notifications have neither (both ids
    NULL; v1 plan §9.1), and come back with `thread_title` /
    `component_link` None.

    plan_ac_enforcement_closeout_2026-09-09.md §4.2 — a third gap alongside
    the 9 `_check_view_access` sites: get mentioned in a thread, lose the
    grant that made it visible, and the notification (excerpt included)
    stayed in the bell indefinitely before this. Post-filtered by
    `access.is_granted(("component", n.component_id))` AFTER the page is
    already bounded by `NOTIFICATIONS_PAGE_SIZE` — a post-filter, not a
    subquery. This makes a page carrying an ungranted row SHORT (fewer than
    `NOTIFICATIONS_PAGE_SIZE` items), not WRONG — acceptable for a bell
    dropdown; do not "fix" this into a subquery/join later. `next_cursor` is
    computed from the UNFILTERED page (before this filter runs), so
    pagination continuity tracks where the DB scan actually left off, not
    what survives the filter."""
    email = _norm_email(user.email)
    query = (
        db.query(NotificationV2, ThreadV2.title, ComponentV2.link)
        .outerjoin(ThreadV2, ThreadV2.id == NotificationV2.thread_id)
        .outerjoin(ComponentV2, ComponentV2.id == NotificationV2.component_id)
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
    next_cursor = (
        _encode_cursor(page[-1][0].created_at, page[-1][0].id)
        if has_more and page
        else None
    )

    if access is not None:
        page = [row for row in page if _notification_visible(row[0].component_id, access)]

    items = [
        _to_notification_entry(notification, title, link)
        for notification, title, link in page
    ]
    return NotificationListResponse(items=items, next_cursor=next_cursor)


def get_unread_notification_count(
    db: Session, user: UserInfo, *, access: ViewerAccess | None = None
) -> tuple[int, str]:
    """`GET /notifications/unread-count` (plan §4.5) — "one query, both
    values", derived every time, never cached. Returns `(count, etag)`; the
    router honours `If-None-Match` against the etag and 304s on a hit.

    Without `access`, the partial index on `(recipient_email) WHERE
    read_at IS NULL` (plan §3.4) keeps this an index-only scan regardless of
    table growth. With `access` (every real caller, as of
    plan_ac_enforcement_closeout_2026-09-09.md §4.2), the count MUST agree
    with `list_notifications_for_user`'s own filter — otherwise the bell
    badge and the dropdown it opens visibly disagree — which costs the
    index-only-scan property: `component_id` has to be read per row to
    filter with, not just aggregated. Still bounded by how many
    notifications one person has unread, not by the whole table."""
    email = _norm_email(user.email)
    if access is None:
        count, max_id = (
            db.query(func.count(NotificationV2.id), func.max(NotificationV2.id))
            .filter(NotificationV2.recipient_email == email, NotificationV2.read_at.is_(None))
            .one()
        )
        count = int(count or 0)
        etag = f'"{count}-{int(max_id) if max_id is not None else 0}"'
        return count, etag

    rows = (
        db.query(NotificationV2.id, NotificationV2.component_id)
        .filter(NotificationV2.recipient_email == email, NotificationV2.read_at.is_(None))
        .all()
    )
    granted_ids = [
        notification_id
        for notification_id, component_id in rows
        if _notification_visible(component_id, access)
    ]
    count = len(granted_ids)
    max_id = max(granted_ids) if granted_ids else None
    etag = f'"{count}-{max_id if max_id is not None else 0}"'
    return count, etag


def mark_notification_read(
    db: Session, notification_id: int, user: UserInfo, *, access: ViewerAccess | None = None
) -> None:
    """`POST /notifications/{id}/read` (plan §4.5) — idempotent (the
    `read_at IS NULL` predicate makes a repeat call a no-op: 0 rows match,
    nothing changes) and scoped to the recipient (the IDOR guard plan §4.5
    calls out explicitly — without the `recipient_email` predicate, any
    authenticated caller could mark another user's notification read by
    id). Deliberately does not distinguish "already read", "not yours",
    "does not exist" — and, as of plan_ac_enforcement_closeout_2026-09-09.md
    §4.2, "no longer granted" — in its response: all four look identical
    from the caller's side, which is the point of the IDOR guard extended to
    cover access, not an oversight."""
    email = _norm_email(user.email)
    notification = (
        db.query(NotificationV2)
        .filter(
            NotificationV2.id == notification_id,
            NotificationV2.recipient_email == email,
            NotificationV2.read_at.is_(None),
        )
        .first()
    )
    if notification is None:
        return
    if not _notification_visible(notification.component_id, access):
        return
    notification.read_at = _utc_now()
    db.commit()


def mark_all_notifications_read(db: Session, user: UserInfo) -> int:
    """`POST /notifications/read-all` (plan §4.1). Returns the number of
    rows actually flipped, for callers that want it; the endpoint itself
    doesn't need to expose it.

    NOT access-gated. plan_ac_enforcement_closeout_2026-09-09.md §4.2 names
    exactly three functions needing `access` threaded for the notification
    gap (`list_notifications_for_user`, `get_unread_notification_count`,
    `mark_notification_read`) — this one is not among them. Marking an
    ungranted notification read flips a flag on the caller's own row, not a
    content read — no excerpt is served — so the harm the other three close
    (a stale excerpt sitting in the bell) does not apply here. Left as a
    known, deliberate asymmetry rather than silently extended to match; flag
    it to the owner if that turns out to be wrong."""
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


def _to_thread_summary(
    thread: ThreadV2, my_vote: int, *, pending_revision_count: int = 0
) -> ThreadSummary:
    """`pending_revision_count` is computed by the CALLER (it needs `user`
    and `access` for its scoping — `_can_see_thread_revisions` — which this
    serializer deliberately doesn't take); defaults to 0, the value every
    caller that can't or needn't scope it (moderation rows, create) sends."""
    return ThreadSummary(
        id=thread.id,
        component_id=thread.component_id,
        title=thread.title,
        author_email=thread.author_email,
        author_name=thread.author_name,
        status=thread.status,
        reviewed_by_email=thread.reviewed_by_email,
        reviewed_by_name=thread.reviewed_by_name,
        reviewed_at=thread.reviewed_at,
        up_count=thread.up_count,
        down_count=thread.down_count,
        comment_count=thread.comment_count,
        created_at=thread.created_at,
        edited_at=thread.edited_at,
        my_vote=my_vote,
        mentions=thread.mentions or [],
        # The markdown-preserving card preview (links keep their syntax so the
        # card renders them like the modal does) — NOT the plain-text panel
        # excerpt. `thread.content` is already loaded on this row regardless
        # (ThreadV2 has no deferred columns), so this costs no extra query.
        content_excerpt=generate_preview_excerpt(thread.content),
        pending_revision_count=pending_revision_count,
        resubmission_count=thread.resubmission_count or 0,
        submitted_at=thread.submitted_at or thread.created_at,
        edit_count=thread.edit_count or 0,
    )


def _open_declined_revision_of(
    db: Session, thread_id: int, email: str
) -> ThreadRevisionV2 | None:
    """plan_thread_pending_edit_update §3.2 (U5) — `email`'s newest OPEN
    declined edit of `thread_id` (`_open_declined_filter`), or None."""
    return (
        db.query(ThreadRevisionV2)
        .filter(
            ThreadRevisionV2.thread_id == thread_id,
            ThreadRevisionV2.submitted_by_email == _norm_email(email),
            _open_declined_filter(db),
        )
        .order_by(ThreadRevisionV2.version.desc())
        .first()
    )


def _to_thread_detail(
    thread: ThreadV2,
    my_vote: int,
    *,
    pending_revisions: list[ThreadRevisionSummary] | None = None,
    viewer_is_editor: bool = False,
    viewer: UserInfo | None = None,
) -> ThreadDetail:
    """`pending_revisions` comes from the caller for the same reason as
    `_to_thread_summary`'s count (`_pending_revision_summaries`); the
    inherited `pending_revision_count` is its length, so the two can never
    disagree on one response. `viewer_is_editor` likewise needs `access`,
    so every caller passes `_is_component_editor(component, access)`.

    `viewer` (plan_thread_pending_edit_update §3.6, U5): whose
    `open_declined_revision` to fill — only when they are the thread's
    author (one query through the row's own session); None for anyone
    else, and when no viewer is passed."""
    pending_revisions = pending_revisions or []
    open_declined: ThreadRevisionSummary | None = None
    if viewer is not None and _norm_email(viewer.email) == _norm_email(thread.author_email):
        session = object_session(thread)
        if session is not None:
            row = _open_declined_revision_of(session, thread.id, viewer.email)
            if row is not None:
                open_declined = _to_revision_summary(row)
    return ThreadDetail(
        **_to_thread_summary(
            thread, my_vote, pending_revision_count=len(pending_revisions)
        ).model_dump(),
        content=thread.content,
        pending_revisions=pending_revisions,
        viewer_is_editor=viewer_is_editor,
        open_declined_revision=open_declined,
        closed_at=thread.closed_at,
        closed_kind=thread.closed_kind,
    )


def _resubmission_of_version(revision: ThreadRevisionV2) -> int | None:
    """The version of the declined edit `revision` reworks ("Resubmission of
    v5"), or None. A PK read through the row's own session — reworks are
    rare, so this is not batched; a revision without a link costs nothing."""
    if revision.resubmission_of_id is None:
        return None
    session = object_session(revision)
    if session is None:
        return None
    return (
        session.query(ThreadRevisionV2.version)
        .filter(ThreadRevisionV2.id == revision.resubmission_of_id)
        .scalar()
    )


def _resubmission_root_version(revision: ThreadRevisionV2) -> int | None:
    """The version of the FIRST edit in `revision`'s rework chain — walk
    `resubmission_of_id` back until a row links to nothing — so every attempt
    reads "v4 (Revision 2 of v2)". None for a row that is not a rework. One
    PK read per step; chains are short (one link per resubmission) and
    reworks rare, the same reasoning as `_resubmission_of_version`. A
    dangling link (`ON DELETE SET NULL` already clears those; defensive
    only) stops the walk at the last row found; a cycle is impossible
    (each link points at an older row) but is guarded anyway."""
    if revision.resubmission_of_id is None:
        return None
    session = object_session(revision)
    if session is None:
        return None
    version: int | None = None
    next_id: int | None = revision.resubmission_of_id
    seen: set[int] = {revision.id}
    while next_id is not None and next_id not in seen:
        seen.add(next_id)
        row = (
            session.query(ThreadRevisionV2.version, ThreadRevisionV2.resubmission_of_id)
            .filter(ThreadRevisionV2.id == next_id)
            .first()
        )
        if row is None:
            break
        version, next_id = row
    return version


def _to_revision_summary(revision: ThreadRevisionV2) -> ThreadRevisionSummary:
    return ThreadRevisionSummary(
        id=revision.id,
        thread_id=revision.thread_id,
        version=revision.version,
        title=revision.title,
        status=revision.status,
        origin=revision.origin,
        submitted_by_email=revision.submitted_by_email,
        submitted_by_name=revision.submitted_by_name,
        submitted_at=revision.submitted_at,
        reviewed_by_email=revision.reviewed_by_email,
        reviewed_by_name=revision.reviewed_by_name,
        reviewed_at=revision.reviewed_at,
        content_excerpt=generate_notification_excerpt(revision.content),
        rejection_reason=revision.rejection_reason,
        resubmission_of_id=revision.resubmission_of_id,
        resubmission_of_version=_resubmission_of_version(revision),
        resubmission_root_version=_resubmission_root_version(revision),
        resubmission_number=revision.resubmission_number or 0,
        closed_at=revision.closed_at,
        closed_kind=revision.closed_kind,
        closed_by_version=revision.closed_by_version,
        edit_count=revision.edit_count or 0,
        updated_at=revision.updated_at,
    )


def _write_original_revision(
    db: Session, thread: ThreadV2, *, publisher: UserInfo, published_at: datetime
) -> ThreadRevisionV2:
    """Amendment A3 (2026-09-23): the published v1 (`origin='original'`),
    written the moment a thread becomes PUBLIC — at create for an editor's
    own post, in `decide_thread` on approval for a viewer's (so it holds the
    final approved text, including any edits made while pending). A pending
    or rejected thread has no rows at all. Snapshots the thread's CURRENT
    title/content/mentions; `submitted_*` is the author, `reviewed_*` whoever
    published it. No-op if the thread already has a v1 (defensive — the
    migration backfills existing approved threads). Does not commit."""
    existing = (
        db.query(ThreadRevisionV2)
        .filter(ThreadRevisionV2.thread_id == thread.id, ThreadRevisionV2.version == 1)
        .first()
    )
    if existing is not None:
        return existing
    revision = ThreadRevisionV2(
        thread_id=thread.id,
        version=1,
        title=thread.title,
        content=thread.content,
        mentions=list(thread.mentions or []),
        status=REVISION_STATUS_APPROVED,
        origin=REVISION_ORIGIN_ORIGINAL,
        submitted_by_email=_norm_email(thread.author_email),
        submitted_by_name=thread.author_name,
        submitted_at=thread.created_at,
        reviewed_by_email=_norm_email(publisher.email),
        reviewed_by_name=publisher.name,
        reviewed_at=published_at,
    )
    db.add(revision)
    thread.version = 1
    return revision


def _to_revision_detail(revision: ThreadRevisionV2) -> ThreadRevisionDetail:
    return ThreadRevisionDetail(
        **_to_revision_summary(revision).model_dump(),
        content=revision.content,
        mentions=revision.mentions or [],
    )


def _pending_revision_counts(
    db: Session,
    threads: list[ThreadV2],
    component: ComponentV2,
    user: UserInfo | None,
    *,
    access: ViewerAccess | None,
) -> dict[int, int]:
    """`thread_id -> pending revision count` for one page of ONE widget's
    threads (plan_thread_edit_versioning §4.4) — ONE grouped query, and only
    over the threads `_can_see_thread_revisions` lets this caller know
    about; any other thread is simply absent (reads as 0)."""
    visible_ids = [
        t.id for t in threads if _can_see_thread_revisions(t, component, user, access)
    ]
    if not visible_ids:
        return {}
    rows = (
        db.query(ThreadRevisionV2.thread_id, func.count(ThreadRevisionV2.id))
        .filter(
            ThreadRevisionV2.thread_id.in_(visible_ids),
            ThreadRevisionV2.status == REVISION_STATUS_PENDING,
        )
        .group_by(ThreadRevisionV2.thread_id)
        .all()
    )
    return {thread_id: int(count) for thread_id, count in rows}


def _pending_revision_summaries(
    db: Session,
    thread: ThreadV2,
    component: ComponentV2,
    user: UserInfo | None,
    *,
    access: ViewerAccess | None,
) -> list[ThreadRevisionSummary]:
    """`ThreadDetail.pending_revisions` — oldest version first, same scoping
    as the count (`_can_see_thread_revisions`); `[]` for everyone else."""
    if not _can_see_thread_revisions(thread, component, user, access):
        return []
    rows = (
        db.query(ThreadRevisionV2)
        .filter(
            ThreadRevisionV2.thread_id == thread.id,
            ThreadRevisionV2.status == REVISION_STATUS_PENDING,
        )
        .order_by(ThreadRevisionV2.version.asc())
        .all()
    )
    return [_to_revision_summary(r) for r in rows]


# ---------------------------------------------------------
# Moderation page support — widget title / location label, batched per
# DISTINCT component (plan §5.6), and the ThreadModerationRow builders.
# ---------------------------------------------------------


def _resolve_widget_title(component: ComponentV2, preloaded_content: dict[int, Any]) -> str:
    """A thread widget's display title: `PageContentV2.content["title"]`,
    then `component.title`, then `"Discussion"` (see below)."""
    title: str | None = None
    if component.page_content_id is not None:
        content = preloaded_content.get(component.page_content_id)
        if isinstance(content, dict):
            raw_title = content.get("title")
            title = raw_title if isinstance(raw_title, str) and raw_title.strip() else None
    return title or component.title or "Discussion"


def _widget_titles_for_component_ids(db: Session, component_ids: list[int]) -> dict[int, str]:
    """`component_id -> widget title` for a whole moderated set, in two
    queries and WITHOUT the location walk — History search matches widget
    titles, which live in JSON, so it resolves them here and turns the hits
    into a `component_id IN (...)` predicate."""
    if not component_ids:
        return {}
    components = db.query(ComponentV2).filter(ComponentV2.id.in_(component_ids)).all()
    page_content_ids = {c.page_content_id for c in components if c.page_content_id is not None}
    preloaded: dict[int, Any] = {}
    if page_content_ids:
        preloaded = {
            row.id: row.content
            for row in db.query(PageContentV2).filter(PageContentV2.id.in_(page_content_ids))
        }
    return {c.id: _resolve_widget_title(c, preloaded) for c in components}


def _history_search_predicate(
    db: Session,
    q: str,
    component_ids: list[int],
    title_columns: list[Any],
    *,
    component_column: Any = None,
):
    """History search (2026-11-09): a case-insensitive substring of any of
    `title_columns` OR of the widget title. `%`, `_` and `\\` in `q` are
    literal. Returns None for a blank `q` (no filter). `component_column`
    is the row's widget id (default `ThreadV2.component_id`; Thread Review's
    union passes its own labelled column)."""
    if component_column is None:
        component_column = ThreadV2.component_id
    needle = (q or "").strip()
    if not needle:
        return None
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    predicates = [col.ilike(pattern, escape="\\") for col in title_columns]
    folded = needle.casefold()
    widget_hits = [
        cid
        for cid, title in _widget_titles_for_component_ids(db, component_ids).items()
        if folded in title.casefold()
    ]
    if widget_hits:
        predicates.append(component_column.in_(widget_hits))
    return or_(*predicates)


HISTORY_GROUPS = ("thread", "widget")
HISTORY_ORDERS = ("newest", "oldest")


def _encode_keyset(values: list[Any]) -> str:
    """Opaque cursor for a History page: the last row's sort key, datetimes
    as ISO strings. Ungrouped = `(decided_at, id)` — the same wire format
    `_encode_cursor` writes; grouped = `(group_at, group_id, decided_at, id)`."""
    payload = json.dumps([v.isoformat() if isinstance(v, datetime) else v for v in values])
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_keyset(cursor: str, grouped: bool) -> tuple[Any, ...]:
    """Inverse of `_encode_keyset`; a cursor from the other mode (or junk)
    is a 400 — the caller must resend the same filters with its cursor."""
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        if grouped:
            g_at, g_id, at, row_id = raw
            return (_as_utc(datetime.fromisoformat(g_at)), int(g_id), _as_utc(datetime.fromisoformat(at)), int(row_id))
        at, row_id = raw
        return (_as_utc(datetime.fromisoformat(at)), int(row_id))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid cursor") from exc


def _keyset_page(
    db: Session,
    base,
    *,
    decided_at,
    row_id,
    group_column,
    order: str,
    cursor: str | None,
):
    """One History page over `base` (an ORM query whose single entity is
    the row model, already filtered). Sort: `decided_at, row_id`, newest or
    oldest first. With `group_column`, rows of one group come out together
    — groups sorted by their newest (or, oldest-first, their oldest)
    decision under the SAME filters, then rows inside — so a group never
    interleaves with another and may only continue on the next page.

    Returns `(rows, group_sizes, next_cursor)`; `group_sizes[i]` is None
    when ungrouped."""
    newest = order != "oldest"
    if group_column is not None:
        agg = func.max(decided_at) if newest else func.min(decided_at)
        groups = (
            base.with_entities(
                group_column.label("gk"), agg.label("g_at"), func.count(row_id).label("g_n")
            )
            .group_by(group_column)
            .subquery()
        )
        query = base.join(groups, groups.c.gk == group_column).add_columns(
            groups.c.g_at, groups.c.gk, groups.c.g_n
        )
        keys = (groups.c.g_at, groups.c.gk, decided_at, row_id)
    else:
        query = base
        keys = (decided_at, row_id)

    if cursor:
        after = _decode_keyset(cursor, grouped=group_column is not None)
        query = query.filter(tuple_(*keys) < after if newest else tuple_(*keys) > after)

    query = query.order_by(*[k.desc() if newest else k.asc() for k in keys])
    fetched = query.limit(THREADS_PAGE_SIZE + 1).all()
    has_more = len(fetched) > THREADS_PAGE_SIZE
    fetched = fetched[:THREADS_PAGE_SIZE]

    if group_column is not None:
        rows = [f[0] for f in fetched]
        sizes = [int(f[3]) for f in fetched]
        last_key = list(fetched[-1][1:3]) if fetched else []
    else:
        rows = list(fetched)
        sizes = [None] * len(rows)
        last_key = []
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = _encode_keyset(last_key + [getattr(last, "reviewed_at"), last.id])
    return rows, sizes, next_cursor


def _widget_title_and_location_for_components(
    db: Session, components: dict[int, ComponentV2]
) -> dict[int, tuple[str, str]]:
    """`component_id -> (widget_title, location_label)`, one entry per
    DISTINCT component in the page (plan §5.6) — a moderation list page can
    show many rows from the SAME widget, and this is computed once for all
    of them, not once per row.

    `widget_title`: `PageContentV2.content["title"]` via
    `component.page_content_id` (the same place
    `gridstack_service._serialize_gridstack_content` reads widget data),
    falling back to `component.title`, then `"Discussion"` (the widget's own
    frontend default). Batched into ONE query across every distinct
    `page_content_id`, the same "build once" shape
    `_serialize_gridstack_content` already uses for the canvas save path.

    `location_label`: `walk_ancestors` per component (3–5 PK reads each — NOT
    `build_node_tree`, which a Hub Admin request has no tree for at all —
    landmine 9), keeping only `tab`/`nav_tab` refs (a sub-grid representation
    or SBN root is a `component` ref and reads as part of the chain, not as a
    named location), labelled in ONE batched `resolve_node_labels` call
    across every component's chain, joined root-first with " › ". An orphan
    chain (§5.6, `walk_ancestors`'s own contract) or a widget with no
    tab/nav-tab ancestor at all yields `""`.
    """
    if not components:
        return {}

    page_content_ids = {
        c.page_content_id for c in components.values() if c.page_content_id is not None
    }
    preloaded_content: dict[int, Any] = {}
    if page_content_ids:
        preloaded_content = {
            row.id: row.content
            for row in db.query(PageContentV2)
            .filter(PageContentV2.id.in_(page_content_ids))
            .all()
        }

    chains: dict[int, list[NodeRef]] = {
        component_id: walk_ancestors(db, ("component", component_id))
        for component_id in components
    }
    all_location_refs: set[NodeRef] = set()
    for chain in chains.values():
        all_location_refs.update(ref for ref in chain if ref[0] in ("tab", "nav_tab"))
    labels = resolve_node_labels(db, all_location_refs) if all_location_refs else {}

    result: dict[int, tuple[str, str]] = {}
    for component_id, component in components.items():
        title = _resolve_widget_title(component, preloaded_content)

        location_refs = [ref for ref in chains[component_id] if ref[0] in ("tab", "nav_tab")]
        # `walk_ancestors` is nearest-first; a location label reads root-first.
        location_label = " › ".join(
            labels[ref] for ref in reversed(location_refs) if ref in labels
        )
        result[component_id] = (title, location_label)
    return result


def _is_self_published_resubmission(thread: ThreadV2, rounds: list[ThreadSubmissionV2]) -> bool:
    """plan §12.8 G3 — the thread is live and its LAST submission round has
    no stored decision: the editor-author resubmitted it and it published at
    once (D5), which writes no round (M5). Only a resubmission qualifies
    (`resubmission_count > 0`), so an editor's own post (round 0, never
    decided) never does."""
    count = thread.resubmission_count or 0
    if thread.status != THREAD_STATUS_APPROVED or count <= 0:
        return False
    highest = max((r.round for r in rounds), default=-1)
    return count > highest


def _published_round(thread: ThreadV2, v1: ThreadRevisionV2) -> SimpleNamespace:
    """The SYNTHESIZED G3 round (never stored): snapshot = v1 (the text that
    went live), reviewer = v1's publisher and instant, no reason. Shaped
    like a `ThreadSubmissionV2` row so both of its readers can treat it as
    one."""
    return SimpleNamespace(
        round=thread.resubmission_count or 0,
        status=SUBMISSION_STATUS_PUBLISHED,
        title=v1.title,
        content=v1.content,
        mentions=list(v1.mentions or []),
        reason=None,
        submitted_by_email=_norm_email(thread.author_email),
        submitted_by_name=thread.author_name,
        submitted_at=thread.submitted_at,
        reviewed_by_email=v1.reviewed_by_email,
        reviewed_by_name=v1.reviewed_by_name,
        reviewed_at=v1.reviewed_at,
    )


def _round_fields(
    db: Session,
    threads: list[ThreadV2],
    *,
    matched_rounds: dict[int, list[int]] | None = None,
) -> dict[int, dict[str, Any]]:
    """`thread_id -> {last_decision, decision_count, previous_rejection,
    matched_rounds}` for one page of moderation rows
    (plan_thread_reject_resubmit §3/§4.6) — ONE query over the page's
    `thread_submissions`, batched like `_pending_revision_counts`.

    `previous_rejection` is the newest REJECTED round, but only for a
    pending RESUBMISSION (`status == 'pending' and resubmission_count > 0`);
    None otherwise. `last_decision` is the highest-numbered decided round —
    or, when the thread was finally published by its editor-author without
    a decision (plan §12.8 G3), the synthesized `published` round (one more
    query, for the page's qualifying threads only). `decision_count` and
    `matched_rounds` count STORED (explicit) decisions only."""
    ids = [t.id for t in threads]
    by_thread: dict[int, list[ThreadSubmissionV2]] = {i: [] for i in ids}
    if ids:
        for row in (
            db.query(ThreadSubmissionV2)
            .filter(ThreadSubmissionV2.thread_id.in_(ids))
            .order_by(ThreadSubmissionV2.thread_id, ThreadSubmissionV2.round)
            .all()
        ):
            by_thread[row.thread_id].append(row)

    self_published = [t for t in threads if _is_self_published_resubmission(t, by_thread.get(t.id, []))]
    v1_by_thread: dict[int, ThreadRevisionV2] = {}
    if self_published:
        v1_by_thread = {
            r.thread_id: r
            for r in db.query(ThreadRevisionV2).filter(
                ThreadRevisionV2.thread_id.in_([t.id for t in self_published]),
                ThreadRevisionV2.version == 1,
            )
        }

    result: dict[int, dict[str, Any]] = {}
    for thread in threads:
        rounds = by_thread.get(thread.id, [])
        last: Any = rounds[-1] if rounds else None
        v1 = v1_by_thread.get(thread.id)
        if v1 is not None:
            last = _published_round(thread, v1)
        previous_rejection = None
        if thread.status == THREAD_STATUS_PENDING and (thread.resubmission_count or 0) > 0:
            rejected = [r for r in rounds if r.status == THREAD_STATUS_REJECTED]
            if rejected:
                r = rejected[-1]
                previous_rejection = PreviousRejection(
                    round=r.round,
                    reason=r.reason,
                    reviewed_by_email=r.reviewed_by_email,
                    reviewed_by_name=r.reviewed_by_name,
                    reviewed_at=r.reviewed_at,
                )
        result[thread.id] = {
            "closed_at": thread.closed_at,
            "closed_kind": thread.closed_kind,
            "previous_rejection": previous_rejection,
            "last_decision": (
                ThreadRoundDecision(
                    round=last.round,
                    status=last.status,
                    reason=last.reason,
                    reviewed_by_email=last.reviewed_by_email,
                    reviewed_by_name=last.reviewed_by_name,
                    reviewed_at=last.reviewed_at,
                )
                if last is not None
                else None
            ),
            "decision_count": len(rounds),
            "matched_rounds": list((matched_rounds or {}).get(thread.id, [])),
        }
    return result


def _to_moderation_rows(
    db: Session,
    threads: list[ThreadV2],
    *,
    matched_rounds: dict[int, list[int]] | None = None,
) -> list[ThreadModerationRow]:
    """`ThreadV2` rows (already the caller's page, already ordered) ->
    `ThreadModerationRow`s, with `widget_title`/`location_label` computed
    once per distinct component (see above), not once per thread, and the
    round fields (`_round_fields`) in one query for the page."""
    if not threads:
        return []

    component_ids = {t.component_id for t in threads}
    components = {
        c.id: c
        for c in db.query(ComponentV2).filter(ComponentV2.id.in_(component_ids)).all()
    }
    info_by_component = _widget_title_and_location_for_components(db, components)
    round_fields = _round_fields(db, threads, matched_rounds=matched_rounds)

    items: list[ThreadModerationRow] = []
    for thread in threads:
        component = components.get(thread.component_id)
        if component is None:
            # The FK is NOT NULL — this would mean the component was deleted
            # without the cascade running (defensive only, same posture as
            # `_require_thread_and_component`).
            continue
        widget_title, location_label = info_by_component.get(
            thread.component_id, ("Discussion", "")
        )
        items.append(
            ThreadModerationRow(
                **_to_thread_summary(thread, my_vote=0).model_dump(),
                component_link=component.link,
                widget_title=widget_title,
                location_label=location_label,
                **round_fields[thread.id],
            )
        )
    return items


def _to_moderation_row(db: Session, thread: ThreadV2, component: ComponentV2) -> ThreadModerationRow:
    """Single-row form — `decide_thread`'s return value and its 409 body
    (plan §5.3)."""
    widget_title, location_label = _widget_title_and_location_for_components(
        db, {component.id: component}
    )[component.id]
    return ThreadModerationRow(
        **_to_thread_summary(thread, my_vote=0).model_dump(),
        component_link=component.link,
        widget_title=widget_title,
        location_label=location_label,
        **_round_fields(db, [thread])[thread.id],
    )


def _author_as_user_info(thread: ThreadV2) -> UserInfo:
    """plan §5.3's approval-time fan-out actor: the AUTHOR (they did the
    mentioning), not the reviewing editor — built from the thread's own
    stored author columns since there is no request-scoped `UserInfo` for
    "the author" at decision time. `author_name` is nullable (plan §1.1);
    falls back to the email's local part, the same display fallback used
    when a thread is first created (`user.name` there; nothing stored here
    if the author never had a name)."""
    email = thread.author_email
    name = thread.author_name or email.split("@")[0]
    return UserInfo(email=email, name=name, roles=[])


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
    db: Session,
    link: str,
    cursor: str | None,
    user: UserInfo | None,
    *,
    access: ViewerAccess | None = None,
    anonymous: bool = False,
) -> ThreadListResponse:
    """Approved threads only. `user` is whose votes, authored-revision
    counts and own declined / pending COUNTS are computed — the caller
    normally, nobody (`None`) for `anonymous` or `view_as_user`, which
    therefore get `my_vote` 0 and zero counts (vote counts stay exact)."""
    component = _get_thread_widget_component(db, link)
    if component is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread widget not found")
    _check_view_access(component, access)

    # Approved threads only, for every caller (2026-11-13, owner request —
    # narrowing plan §5.5): the author's OWN pending threads no longer show
    # in the widget's list with a status badge. They are counted in
    # `pending_count` (and their pending edits in `pending_edit_count`) for
    # the widget's banner, and live on My Revisions → Awaiting review,
    # where the author updates them.
    #
    # plan_thread_reject_resubmit_2026-11-07.md §4.2 (owner decision 5)
    # DELIBERATELY narrows 2a's M3 rule: the author's REJECTED threads no
    # longer appear here — they live in the author's inbox
    # (`list_declined_items`), counted by `declined_count` below. A CLOSED
    # thread is always rejected (ck_threads_closed_shape), so this list
    # never needs to filter on `closed_at` — closed items never appear in
    # the threads list (owner decision R5).
    query = db.query(ThreadV2).filter(
        ThreadV2.component_id == component.id, ThreadV2.status == THREAD_STATUS_APPROVED
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

    my_votes = _my_votes_for_threads(db, [t.id for t in page], user.email) if user is not None else {}
    # Scoped (plan_thread_edit_versioning §4.4, landmine 3) — 0 for any row
    # this caller neither authored nor edits.
    revision_counts = _pending_revision_counts(db, page, component, user, access=access)
    items = [
        _to_thread_summary(
            t, my_votes.get(t.id, 0), pending_revision_count=revision_counts.get(t.id, 0)
        )
        for t in page
    ]
    next_cursor = _encode_cursor(page[-1].created_at, page[-1].id) if has_more and page else None

    # plan_thread_reject_resubmit §4.7 — the caller's OWN declined items on
    # this widget (two cheap COUNTs, the inbox's exact predicate). 0 for a
    # no-subject caller (`anonymous` / view-as with no user).
    declined_count = 0
    pending_count = 0
    pending_edit_count = 0
    if not anonymous and user is not None:
        declined_count = _declined_count(db, component.id, user.email)
        pending_count = int(_own_pending_threads_query(db, [component.id], user.email).count())
        pending_edit_count = int(
            _own_pending_revisions_query(db, [component.id], user.email).count()
        )

    return ThreadListResponse(
        items=items,
        next_cursor=next_cursor,
        declined_count=declined_count,
        pending_count=pending_count,
        pending_edit_count=pending_edit_count,
    )


def create_thread_for_link(
    db: Session,
    link: str,
    user: UserInfo,
    title: str,
    content: str,
    mentions: list[MentionInput] | None = None,
    *,
    access: ViewerAccess | None = None,
) -> ThreadDetail:
    component = _get_thread_widget_component(db, link)
    if component is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread widget not found")
    _check_view_access(component, access)

    # Resolve mentions BEFORE constructing the row — a 400 from the cap
    # (plan §4.7) must not leave a half-built thread behind (nothing is
    # `db.add`ed yet at this point).
    resolved = _validate_and_resolve_mentions(db, mentions or [], content, component=component)

    # plan_thread_moderation_2026-09-18.md §0.1(1)/(2), §4.1 — an editor of
    # the widget or any of its ancestors auto-approves; a granted VIEWER's
    # post lands `pending` until an editor decides.
    thread_status = (
        THREAD_STATUS_APPROVED if _is_component_editor(component, access) else THREAD_STATUS_PENDING
    )

    now = _utc_now()
    thread = ThreadV2(
        component_id=component.id,
        title=title,
        content=content,
        author_email=_norm_email(user.email),
        author_name=user.name,
        mentions=resolved,
        status=thread_status,
        up_count=0,
        down_count=0,
        comment_count=0,
        created_at=now,
        # plan_thread_reject_resubmit §2.1 — the first submission instant.
        submitted_at=now,
        resubmission_count=0,
    )
    db.add(thread)
    db.flush()  # assigns thread.id — the notifications below FK to it

    # Amendment A3: an editor's post is published at once, so its v1 is
    # written now, with the editor as its own publisher. A viewer's pending
    # post gets its v1 when `decide_thread` approves it.
    if thread_status == THREAD_STATUS_APPROVED:
        _write_original_revision(db, thread, publisher=user, published_at=now)

    # plan §5.4 (landmine 1) — fan out ONLY when the new row is approved. A
    # pending thread notifies nobody: its mentions were validated and stored
    # (so nothing has to be re-typed once approved), but every mentioned
    # user would otherwise get a bell entry to a thread that 404s for them
    # (`get_thread_by_id`'s non-approved branch) until an editor decides.
    if thread_status == THREAD_STATUS_APPROVED:
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
    return _to_thread_detail(
        thread, my_vote=0, viewer_is_editor=_is_component_editor(component, access)
    )


def get_thread_by_id(
    db: Session, thread_id: int, user: UserInfo | None, *, access: ViewerAccess | None = None
) -> ThreadDetail:
    """Fetch one thread's full content — any viewer with the widget's own
    view access, NOT author-only (session handoff 2026-08-20 §0/addendum:
    the Phase 2 frontend worked around this endpoint not existing by
    empty-PATCH'ing as the author, which only ever closed the gap for a
    thread's own author).

    plan_thread_moderation_2026-09-18.md §4.1: an `approved` thread reads
    for any granted viewer, unchanged. A `pending`/`rejected` thread reads
    for its author OR a component editor (moderation needs the full body,
    not just the queue row); anyone else gets the SAME 404 a nonexistent id
    would — "this id exists but isn't yours" is never leaked. The `_check_
    view_access` 403 still runs FIRST (landmine 2): a caller with no view
    access to the widget at all keeps getting 403, never 404.

    `user` is whose authorship, vote and authored revisions count — the
    caller normally (View As pairs included), and nobody (`None`) for
    `anonymous` or `view_as_user`.

    plan_thread_reject_resubmit §12.7: a CLOSED declined thread is NOT a
    404 for its author (that was the superseded soft-delete rule) — it
    reads like any other rejected thread, read-only, so old bell rows still
    open it."""
    thread, component = _require_thread_and_component(db, thread_id)
    # 403 BEFORE the status check (landmine 2) — a caller with no view
    # access to the widget at all must keep getting the existing 403, never
    # a 404 that would leak "this id exists but isn't approved/yours".
    _check_view_access(component, access)
    if thread.status != THREAD_STATUS_APPROVED:
        is_author = user is not None and _norm_email(thread.author_email) == _norm_email(user.email)
        is_editor = _is_component_editor(component, access)
        if not (is_author or is_editor):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found")
    my_vote = _my_vote_for_thread(db, thread.id, user.email) if user is not None else 0
    return _to_thread_detail(
        thread,
        my_vote,
        pending_revisions=_pending_revision_summaries(db, thread, component, user, access=access),
        viewer_is_editor=_is_component_editor(component, access), viewer=user,
    )


def update_thread_by_id(
    db: Session,
    thread_id: int,
    user: UserInfo,
    title: str | None,
    content: str | None,
    mentions: list[MentionInput] | None = None,
    *,
    overwrite: bool = False,
    access: ViewerAccess | None = None,
) -> ThreadUpdateResult:
    """PATCH /threads/{id}. Returns `ThreadUpdateResult`
    (plan_thread_edit_versioning_2026-09-22.md §3, amended 2026-09-23). Four
    paths, in this order:

    - `rejected` thread → 409 (terminal; unchanged).
    - `approved` thread, NON-editor author → STAGED as a pending
      `origin='author'` revision (`_stage_thread_edit`), `applied=False`.
    - `approved` thread, EDITOR author → live at once as an auto-approved
      `origin='editor'` revision (`_apply_editor_edit`), `applied=True`;
      409 `earlier_revisions_pending` while author edits are pending unless
      `overwrite` (amendment A2).
    - `pending` thread → written in place, no revision (the thread has no
      version history until it is published — amendment A3), `applied=True`.

    `overwrite` only means anything on the editor path."""
    thread, component = _require_thread_and_component(db, thread_id)
    # A caller who has lost view access to the widget since posting must
    # not still be able to edit through this endpoint (defense in depth —
    # not spelled out explicitly in the plan, called out in the handoff).
    _check_view_access(component, access)
    _require_author(thread.author_email, user)

    # plan §2 — rejected is terminal: no resubmit-on-edit. Checked after the
    # author gate (a non-author still gets the existing 403, not this 409).
    if thread.status == THREAD_STATUS_REJECTED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This thread was not approved and can no longer be edited",
        )

    if thread.status == THREAD_STATUS_APPROVED:
        # plan_thread_edit_versioning §4.2 (owner decision 2) — the SAME
        # `_is_component_editor(component, access)` predicate
        # `create_thread_for_link` uses to auto-approve (landmine 8), so
        # posting authority and editing authority can't drift apart.
        # `access=None` is not an editor there, so an internal caller
        # without a `ViewerAccess` stages rather than silently going live.
        if _is_component_editor(component, access):
            return _apply_editor_edit(
                db, thread_id, component, user, title, content, mentions,
                overwrite=overwrite, access=access,
            )
        return _stage_thread_edit(
            db, thread_id, component, user, title, content, mentions, access=access
        )

    # A PENDING thread (the only status left): edited in place, exactly as
    # before versioning. No revision — its v1 is written when an editor
    # approves it, and holds whatever the text is by then.
    #
    # Validate against the EFFECTIVE content — the new content if this PATCH
    # touches it, otherwise the thread's current content — since a mention's
    # token must occur in whatever the stored body ends up being, not
    # necessarily what this specific request's `content` field carried
    # (e.g. a title-only edit that also resends the same mentions).
    #
    # plan_thread_pending_edit_update §3.8/§3.10: lock the threads row — the
    # `edit_count` bump below and `decide_thread`'s stale check serialize on
    # it, and so does the U10 guard's read of the declined round.
    thread = _lock_thread(db, thread_id)
    effective_title = title if title is not None else thread.title
    effective_content = content if content is not None else thread.content
    if mentions is not None:
        # plan §5.4 — a PENDING thread's mentions are validated and stored
        # but NOT fanned out: nobody has read it yet, so there is no "newly
        # added" to notify — the whole stored array is notified once, at
        # approval time (`decide_thread`).
        effective_mentions = _validate_and_resolve_mentions(
            db, mentions, effective_content, component=component
        )
    else:
        effective_mentions = list(thread.mentions or [])

    # U10 — a pending RESUBMISSION may not be edited back to the text of the
    # attempt it reworks (the directly reworked round only: round
    # `resubmission_count - 1`; matching an older round is allowed — the
    # same rule as `resubmit_thread`).
    if (thread.resubmission_count or 0) > 0:
        reworked = (
            db.query(ThreadSubmissionV2)
            .filter(
                ThreadSubmissionV2.thread_id == thread.id,
                ThreadSubmissionV2.round == thread.resubmission_count - 1,
                # A round's status is the thread status it decided.
                ThreadSubmissionV2.status == THREAD_STATUS_REJECTED,
            )
            .first()
        )
        if reworked is not None and _is_unchanged(
            effective_title, effective_content, effective_mentions, reworked
        ):
            db.rollback()
            raise _conflict(
                "unchanged_resubmission",
                "This is the text that was declined — change it before it goes back for review",
            )

    # U9 — the stale-check token moves on every REAL change (title, content
    # or the set of mentioned emails), not on an unchanged resend: the
    # composer sends every field on each save, and a no-op must not make a
    # reviewer's decision fail as "changed while you were reviewing".
    really_changed = not _is_unchanged(
        effective_title, effective_content, effective_mentions, thread
    )
    if mentions is not None:
        thread.mentions = effective_mentions

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
    if really_changed:
        thread.edit_count = (thread.edit_count or 0) + 1

    db.commit()
    db.refresh(thread)
    my_vote = _my_vote_for_thread(db, thread.id, user.email)
    return ThreadUpdateResult(
        applied=True,
        thread=_to_thread_detail(
            thread,
            my_vote,
            pending_revisions=_pending_revision_summaries(db, thread, component, user, access=access),
            viewer_is_editor=_is_component_editor(component, access), viewer=user,
        ),
        revision=None,
    )


def _ensure_original_revision(db: Session, thread: ThreadV2) -> None:
    """Self-healing v1 for an APPROVED thread that has no version history
    yet — the same row `scripts/migrate_thread_revisions.py` backfills for
    every pre-versioning approved thread (publisher = the thread's own
    reviewer, else its author; published at its decision, else its
    creation). Called under the threads-row lock before any revision is
    appended, so a history never starts without its v1. After the migration
    has run this finds a revision every time and does nothing."""
    has_revisions = (
        db.query(ThreadRevisionV2.id).filter(ThreadRevisionV2.thread_id == thread.id).first()
    )
    if has_revisions is not None:
        return
    publisher_email = thread.reviewed_by_email or thread.author_email
    publisher_name = thread.reviewed_by_name if thread.reviewed_by_email else thread.author_name
    original = _write_original_revision(
        db,
        thread,
        publisher=UserInfo(
            email=publisher_email,
            name=publisher_name or publisher_email.split("@")[0],
            roles=[],
        ),
        published_at=thread.reviewed_at or thread.created_at,
    )
    # Store the name exactly as the migration's backfill does (possibly
    # NULL), not the display fallback `UserInfo` needed.
    original.reviewed_by_name = publisher_name


def _next_revision_version(db: Session, thread: ThreadV2) -> int:
    """COALESCE(MAX(version), threads.version) + 1 (plan §1.2) — call ONLY
    under the threads-row lock; `uq_thread_revisions_thread_version` is the
    hard backstop behind it."""
    max_version = (
        db.query(func.max(ThreadRevisionV2.version))
        .filter(ThreadRevisionV2.thread_id == thread.id)
        .scalar()
    )
    return (max_version if max_version is not None else thread.version) + 1


def _earlier_revisions_conflict(
    earlier: list[ThreadRevisionV2],
    revision: ThreadRevisionV2 | None,
    declined_open: list[ThreadRevisionV2] | None = None,
) -> HTTPException:
    """The 409 `earlier_revisions_pending` body, shared by an out-of-order
    approve (`revision` = the one being approved) and an editor's PATCH over
    pending author edits (`revision` = None — nothing was created).

    plan_thread_reject_resubmit §12.5 (owner decision R3): also carries
    `declined_open` — the OPEN declined edits that publishing would close as
    superseded (§12.4) — and is raised when EITHER list is non-empty, so the
    publisher confirms once for both. `earlier` may be `[]`."""
    declined_open = declined_open or []
    parts: list[str] = []
    if earlier:
        noun = "edit" if len(earlier) == 1 else "edits"
        verb = "is" if len(earlier) == 1 else "are"
        parts.append(f"There {verb} {len(earlier)} earlier {noun} still awaiting review")
    if declined_open:
        noun = "edit" if len(declined_open) == 1 else "edits"
        parts.append(
            f"{len(declined_open)} declined {noun} will be closed as superseded"
        )
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "code": "earlier_revisions_pending",
            "message": "; ".join(parts),
            "revision": _to_revision_summary(revision).model_dump(mode="json") if revision else None,
            "earlier": [_to_revision_summary(r).model_dump(mode="json") for r in earlier],
            "declined_open": [_to_revision_summary(r).model_dump(mode="json") for r in declined_open],
        },
    )


def _not_reworked(db: Session):
    """`NOT EXISTS (a revision whose resubmission_of_id = this one)`,
    correlated to the outer `ThreadRevisionV2` (D4's anti-join)."""
    rework = aliased(ThreadRevisionV2)
    return ~db.query(rework.id).filter(rework.resubmission_of_id == ThreadRevisionV2.id).exists()


def _open_declined_filter(db: Session):
    """The "OPEN declined edit" predicate (plan §12.3): an author's edit that
    was declined, is not closed, and has not been reworked (no revision
    links back to it). Shared by the auto-close and the inbox / count."""
    return and_(
        ThreadRevisionV2.status == REVISION_STATUS_REJECTED,
        ThreadRevisionV2.origin == REVISION_ORIGIN_AUTHOR,
        ThreadRevisionV2.closed_at.is_(None),
        _not_reworked(db),
    )


def _open_declined_edits_below(
    db: Session, thread_id: int, version: int, *, exclude_id: int | None = None
) -> list[ThreadRevisionV2]:
    """plan §12.4 — the OPEN declined edits of `thread_id` that version
    `version` going live would close: `version < K` only (a declined v8
    built on a then-pending v7 is still current when v7 goes live).
    `exclude_id` drops a rework's own source while the 409 is computed
    (before the new linked row exists). Call under the threads-row lock."""
    query = db.query(ThreadRevisionV2).filter(
        ThreadRevisionV2.thread_id == thread_id,
        ThreadRevisionV2.version < version,
        _open_declined_filter(db),
    )
    if exclude_id is not None:
        query = query.filter(ThreadRevisionV2.id != exclude_id)
    return query.order_by(ThreadRevisionV2.version.asc()).all()


def _close_superseded(
    db: Session,
    thread: ThreadV2,
    revisions: list[ThreadRevisionV2],
    *,
    live_version: int,
    publisher: UserInfo,
    component_id: int,
    now: datetime,
) -> None:
    """plan §12.4 / §12.6 — close each OPEN declined edit as `superseded` by
    `live_version`, and bell its submitter (`thread_edit_closed`, actor =
    the publisher, excerpt "Superseded by v{K}"). The existing self-
    exclusion applies: an editor-author who just confirmed closing their own
    declined edit gets no bell. Does not commit."""
    publisher_email = _norm_email(publisher.email)
    for revision in revisions:
        revision.closed_at = now
        revision.closed_kind = CLOSED_KIND_SUPERSEDED
        revision.closed_by_version = live_version
        recipient = _norm_email(revision.submitted_by_email)
        if recipient == publisher_email or _recipient_is_deleted(db, recipient):
            continue
        db.add(
            NotificationV2(
                recipient_email=recipient,
                type=NOTIFICATION_TYPE_THREAD_EDIT_CLOSED,
                actor_email=publisher_email,
                actor_name=publisher.name,
                component_id=component_id,
                thread_id=thread.id,
                comment_id=None,
                excerpt=f"Superseded by v{live_version}",
                read_at=None,
                created_at=now,
            )
        )


def _rework_link(resubmission_of: ThreadRevisionV2 | None) -> dict[str, Any]:
    """The link columns of a rework (plan_thread_reject_resubmit §2.3,
    owner decision 9): `resubmission_of_id` → the declined edit, and
    `resubmission_number` = the declined one's number + 1. `{}` for an
    ordinary edit (the columns' defaults: NULL / 0)."""
    if resubmission_of is None:
        return {}
    return {
        "resubmission_of_id": resubmission_of.id,
        "resubmission_number": (resubmission_of.resubmission_number or 0) + 1,
    }


def _apply_editor_edit(
    db: Session,
    thread_id: int,
    component: ComponentV2,
    user: UserInfo,
    title: str | None,
    content: str | None,
    mentions: list[MentionInput] | None,
    *,
    overwrite: bool,
    access: ViewerAccess | None,
    resubmission_of: ThreadRevisionV2 | None = None,
    resolved_mentions: list[dict[str, str]] | None = None,
) -> ThreadUpdateResult:
    """Amendment A2 (2026-09-23) — an EDITOR-author's edit of an approved
    thread goes live immediately AND is recorded as an auto-approved
    `origin='editor'` revision at the next version, so `threads.version`
    moves forward with every published change and the version log stays
    complete.

    While the thread has pending AUTHOR revisions (possible when the author
    was a non-editor when they submitted them), publishing over them would
    leave them below the live version — approving one later would move the
    thread BACKWARDS. So this is refused with 409 `earlier_revisions_pending`
    (listing them, `revision: null`) unless `overwrite=True`, which marks
    them `overwritten`, stamped with this editor and instant — the same
    opt-in-on-the-wire rule owner decision 4 set for an out-of-order
    approve.

    plan_thread_reject_resubmit §12.4/§12.5: the same 409 also fires when
    the edit would close OPEN declined edits (`declined_open`); with
    `overwrite=True` they are closed as `superseded` by the new version,
    and each submitter other than this editor gets a `thread_edit_closed`
    bell.

    A no-op (title, content and mention emails all equal the live thread)
    writes nothing and is never a conflict. Mentions keep the pre-
    versioning rule: validated, and only newly added ones notified, inline,
    with the editor as the actor.

    `resubmission_of` (plan_thread_reject_resubmit §4.4, D5): the rework of
    a declined edit by an author who is now an editor. The caller
    (`resubmit_revision`) has already locked the thread and the declined
    revision, resolved the mentions (`resolved_mentions`) and refused both
    "unchanged vs declined" and "unchanged vs live", so the values here are
    a full snapshot and the no-op branch below is skipped. The new row
    carries the link."""
    # Threads row first, then revisions — the order every writer here takes.
    thread = _lock_thread(db, thread_id)

    new_title = title if title is not None else thread.title
    new_content = content if content is not None else thread.content
    if resolved_mentions is not None:
        new_mentions = list(resolved_mentions)
    elif mentions is not None:
        new_mentions = _validate_and_resolve_mentions(db, mentions, new_content, component=component)
    else:
        new_mentions = list(thread.mentions or [])

    if resubmission_of is None and (
        new_title == thread.title
        and new_content == thread.content
        and _mention_emails(new_mentions) == _mention_emails(thread.mentions)
    ):
        db.commit()  # releases the row lock; nothing was written
        return ThreadUpdateResult(
            applied=True,
            thread=_to_thread_detail(
                thread,
                _my_vote_for_thread(db, thread.id, user.email),
                pending_revisions=_pending_revision_summaries(db, thread, component, user, access=access),
                viewer_is_editor=_is_component_editor(component, access), viewer=user,
            ),
            revision=None,
        )

    # Every pending revision has version > threads.version (plan §1.2), so
    # "pending at all" means "would end up below this edit".
    pending = (
        db.query(ThreadRevisionV2)
        .filter(
            ThreadRevisionV2.thread_id == thread.id,
            ThreadRevisionV2.status == REVISION_STATUS_PENDING,
        )
        .order_by(ThreadRevisionV2.version.asc())
        .all()
    )
    # plan §12.4/§12.5 — this edit goes live at MAX(version)+1, above every
    # existing row, so EVERY open declined edit would be superseded — except
    # a rework's own source, which the new row is about to link (it is
    # "reworked", not closed). Same confirm as the pending list.
    declined_open = _open_declined_edits_below(
        db,
        thread.id,
        _next_revision_version(db, thread),
        exclude_id=resubmission_of.id if resubmission_of is not None else None,
    )
    if (pending or declined_open) and not overwrite:
        conflict = _earlier_revisions_conflict(pending, None, declined_open)
        db.rollback()  # release the lock before raising
        raise conflict

    _ensure_original_revision(db, thread)
    now = _utc_now()
    editor_email = _norm_email(user.email)
    for displaced in pending:
        displaced.status = REVISION_STATUS_OVERWRITTEN
        displaced.reviewed_by_email = editor_email
        displaced.reviewed_by_name = user.name
        displaced.reviewed_at = now

    db.flush()  # the v1 above, if written, must count in MAX(version)
    next_version = _next_revision_version(db, thread)
    previous_mentions = list(thread.mentions or [])  # BEFORE the overwrite below
    thread.title = new_title
    thread.content = new_content
    thread.mentions = new_mentions
    thread.version = next_version
    # "Edited" means a version after v1 is live (owner, 2026-09-23) — a
    # mentions-only change included: it links or unlinks a chip and can
    # notify someone, and the marker must agree with the version history.
    thread.edited_at = now

    revision = ThreadRevisionV2(
        thread_id=thread.id,
        version=next_version,
        title=new_title,
        content=new_content,
        mentions=list(new_mentions),
        status=REVISION_STATUS_APPROVED,
        origin=REVISION_ORIGIN_EDITOR,
        submitted_by_email=editor_email,
        submitted_by_name=user.name,
        submitted_at=now,
        reviewed_by_email=editor_email,
        reviewed_by_name=user.name,
        reviewed_at=now,
        **_rework_link(resubmission_of),
    )
    db.add(revision)
    # Flush the (possibly linked) new row FIRST, so a rework's own source
    # reads as reworked, not open, in the close query (plan §12.4).
    db.flush()
    _close_superseded(
        db,
        thread,
        _open_declined_edits_below(db, thread.id, next_version),
        live_version=next_version,
        publisher=user,
        component_id=component.id,
        now=now,
    )

    # plan §5.7 — "on edit: notify only newly added mentions", unchanged.
    _notify_newly_added_mentions(
        db,
        previous_mentions=previous_mentions,
        resolved_mentions=new_mentions,
        actor=user,
        component_id=component.id,
        thread_id=thread.id,
        comment_id=None,
        content=new_content,
    )

    db.commit()
    db.refresh(thread)
    db.refresh(revision)
    return ThreadUpdateResult(
        applied=True,
        thread=_to_thread_detail(
            thread,
            _my_vote_for_thread(db, thread.id, user.email),
            pending_revisions=_pending_revision_summaries(db, thread, component, user, access=access),
            viewer_is_editor=_is_component_editor(component, access), viewer=user,
        ),
        revision=_to_revision_summary(revision),
    )


def _stage_thread_edit(
    db: Session,
    thread_id: int,
    component: ComponentV2,
    user: UserInfo,
    title: str | None,
    content: str | None,
    mentions: list[MentionInput] | None,
    *,
    access: ViewerAccess | None,
    resubmission_of: ThreadRevisionV2 | None = None,
    resolved_mentions: list[dict[str, str]] | None = None,
) -> ThreadUpdateResult:
    """plan_thread_edit_versioning_2026-09-22.md §4.2 — store a non-editor
    author's edit of an approved thread as a pending `thread_revisions`
    snapshot. `threads.title`/`content`/`mentions`/`edited_at`/`status` are
    NEVER touched here — that is the whole point (landmine 7: if
    `threads.status` moved, `_require_approved` would close comments and
    votes on a live thread). No notification is written at submit; the
    mention fan-out happens at approval (`decide_thread_revision`).

    `resubmission_of` (plan_thread_reject_resubmit §4.4, landmine 9): the
    rework of a declined edit. Its base is the DECLINED revision, not the
    "latest pending else live" rule below — so the caller
    (`resubmit_revision`) passes a full snapshot with already-resolved
    mentions, has already refused "unchanged vs declined" and "unchanged vs
    live", and the no-op branch (which compares against latest-pending/live)
    is skipped. The new row carries the link.

    plan_thread_pending_edit_update_2026-11-10 (ordinary PATCH path only):
    - a pending edit exists → it is UPDATED IN PLACE (`edit_count += 1`,
      `updated_at = now`; U1–U3), the newest one on a legacy thread with
      several (§3.5). A pending REWORK must still differ from the edit it
      reworks (409 `unchanged_resubmission`) and from live (409
      `no_change`) — U4. A plain edit may go back to live (U7).
    - no pending edit → a new row, unless the author has an OPEN declined
      edit of this thread: 409 `declined_edit_open` (U5)."""
    # Lock the THREADS row first — the same order `decide_thread_revision`
    # takes (threads, then revisions), so a submit and a decision on the
    # same thread can never deadlock on Postgres. This is also what
    # serializes the version allocation below; `uq_thread_revisions_thread_
    # version` is the hard backstop behind it.
    thread = _lock_thread(db, thread_id)

    # Base for omitted fields = the author's latest PENDING revision if one
    # exists, else the live thread (plan §4.2 step 2) — a PATCH carrying only
    # `content` keeps the title the author last PROPOSED, not the published
    # one. Every revision on a thread is its author's (`_require_author`
    # gates submission), so "the thread's latest pending" is "the author's".
    #
    # plan_thread_pending_edit_update §3.2 step 1: locked too (threads row
    # first, then the revision — the decision path's order), so an in-place
    # update and a decision on the same edit serialize.
    latest_pending = (
        db.query(ThreadRevisionV2)
        .filter(
            ThreadRevisionV2.thread_id == thread.id,
            ThreadRevisionV2.status == REVISION_STATUS_PENDING,
        )
        .order_by(ThreadRevisionV2.version.desc())
        .with_for_update()
        .populate_existing()  # see `_lock_thread`
        .first()
    )
    base = latest_pending if latest_pending is not None else thread

    new_title = title if title is not None else base.title
    new_content = content if content is not None else base.content
    # Validation happens at SUBMIT, against the effective content (same rule
    # as the write-through path); only the fan-out moves to approval.
    # Omitted mentions carry over from the base, unvalidated — exactly what
    # an omitted `mentions` means on the write-through path too.
    if resolved_mentions is not None:
        new_mentions = list(resolved_mentions)
    elif mentions is not None:
        new_mentions = _validate_and_resolve_mentions(db, mentions, new_content, component=component)
    else:
        new_mentions = list(base.mentions or [])

    # No-op (owner-approved 2026-09-23): an edit identical to its base
    # writes nothing — no do-nothing revision lands in an editor's queue
    # (an unchanged Save in the composer sends all three fields).
    if resubmission_of is None and (
        new_title == base.title
        and new_content == base.content
        and _mention_emails(new_mentions) == _mention_emails(base.mentions)
    ):
        db.commit()  # releases the row lock; nothing was written
        pending_revisions = _pending_revision_summaries(db, thread, component, user, access=access)
        detail = _to_thread_detail(
            thread,
            _my_vote_for_thread(db, thread.id, user.email),
            pending_revisions=pending_revisions,
            viewer_is_editor=_is_component_editor(component, access), viewer=user,
        )
        if latest_pending is None:
            # Unchanged vs the LIVE thread — the same no-op a pre-versioning
            # empty PATCH was.
            return ThreadUpdateResult(applied=True, thread=detail, revision=None)
        # Unchanged vs the author's own pending draft — that draft is still
        # what is awaiting review.
        return ThreadUpdateResult(
            applied=False, thread=detail, revision=_to_revision_summary(latest_pending)
        )

    if resubmission_of is None and latest_pending is not None:
        # plan_thread_pending_edit_update §3.2 step 3 (U1–U3): UPDATE the
        # pending edit in place — same row, version, status, `submitted_at`
        # and link columns (a rework keeps its "Revision N", U2/U3).
        if latest_pending.resubmission_of_id is not None:
            # U4 — a rework must still differ from the edit it reworks (the
            # DIRECTLY declined one only, as `resubmit_revision` checks) and
            # from the live text. Codes/messages are `resubmit_revision`'s.
            declined = (
                db.query(ThreadRevisionV2)
                .filter(ThreadRevisionV2.id == latest_pending.resubmission_of_id)
                .first()
            )
            if declined is not None and _is_unchanged(new_title, new_content, new_mentions, declined):
                db.rollback()
                raise _conflict("unchanged_resubmission", "Change the edit before resubmitting")
            if _is_unchanged(new_title, new_content, new_mentions, thread):
                db.rollback()
                raise _conflict("no_change", "This is already the published version of the thread")
        # A PLAIN edit updated back to the live text stays pending (U7).
        latest_pending.title = new_title
        latest_pending.content = new_content
        latest_pending.mentions = list(new_mentions)
        latest_pending.edit_count = (latest_pending.edit_count or 0) + 1
        latest_pending.updated_at = _utc_now()
        db.commit()
        db.refresh(latest_pending)
        db.refresh(thread)
        return ThreadUpdateResult(
            applied=False,
            thread=_to_thread_detail(
                thread,
                _my_vote_for_thread(db, thread.id, user.email),
                pending_revisions=_pending_revision_summaries(db, thread, component, user, access=access),
                viewer_is_editor=_is_component_editor(component, access), viewer=user,
            ),
            revision=_to_revision_summary(latest_pending),
        )

    if resubmission_of is None:
        # U5 (plan §3.2 step 2) — no pending edit, so this save would CREATE
        # one: refused while the author has an OPEN declined edit on this
        # thread. Under the threads-row lock, so a concurrent reject can't
        # slip past it. A no-op never reaches here (it writes nothing).
        open_declined = _open_declined_revision_of(db, thread.id, user.email)
        if open_declined is not None:
            conflict = _conflict(
                "declined_edit_open",
                f"Resubmit or close your declined edit (v{open_declined.version}) "
                "before making a new one",
                revision=_to_revision_summary(open_declined).model_dump(mode="json"),
            )
            db.rollback()
            raise conflict

    # A pre-versioning thread gets its v1 first, so the history never
    # starts at v2 (a no-op after the migration's backfill).
    _ensure_original_revision(db, thread)
    db.flush()
    next_version = _next_revision_version(db, thread)

    revision = ThreadRevisionV2(
        thread_id=thread.id,
        version=next_version,
        title=new_title,
        content=new_content,
        mentions=new_mentions,
        status=REVISION_STATUS_PENDING,
        origin=REVISION_ORIGIN_AUTHOR,
        submitted_by_email=_norm_email(user.email),
        submitted_by_name=user.name,
        submitted_at=_utc_now(),
        **_rework_link(resubmission_of),
    )
    db.add(revision)
    db.commit()
    db.refresh(revision)
    db.refresh(thread)

    return ThreadUpdateResult(
        applied=False,
        thread=_to_thread_detail(
            thread,
            _my_vote_for_thread(db, thread.id, user.email),
            pending_revisions=_pending_revision_summaries(db, thread, component, user, access=access),
            viewer_is_editor=_is_component_editor(component, access), viewer=user,
        ),
        revision=_to_revision_summary(revision),
    )


def delete_thread_by_id(
    db: Session, thread_id: int, *, access: ViewerAccess | None = None
) -> None:
    _require_hub_admin(access)
    thread = _get_thread(db, thread_id)
    if thread is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found")
    # Plain ORM delete — cascading to comments/votes/notifications is the
    # database's job via ON DELETE CASCADE (see thread.py's module
    # docstring), not SQLAlchemy relationship cascade.
    db.delete(thread)
    db.commit()


# ---------------------------------------------------------
# Rejection reasons, resubmission and author delete
# (plan_thread_reject_resubmit_2026-11-07.md §4.1, §4.3–§4.7).
#
# Every writer here is AUTHOR-ONLY (`_require_author`, landmine 6): an editor
# or Hub Admin who is not the author gets 403 — never `_require_component_
# editor`, never the admin bypass. Lock order is the threads row FIRST, then
# the revision (landmine 8), the order `_stage_thread_edit` and
# `decide_thread_revision` take. 409 bodies are `{"code", "message", ...}`,
# the versioning 409s' shape.
# ---------------------------------------------------------

# plan §4.7 — the inbox is not paginated; capped, with `truncated`.
DECLINED_ITEMS_CAP = 100
# plan §12.7 — the inbox's read-only "Closed" list; capped, with
# `closed_truncated`.
CLOSED_ITEMS_CAP = 50


def _require_reason(approve: bool, reason: str | None) -> str | None:
    """Defense in depth behind `RejectRequest` (plan §4.1): a reject
    without a reason is a 400 even from an internal caller. Approve ignores
    any reason (`None` is stored)."""
    if approve:
        return None
    reason = (reason or "").strip()
    if not reason:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A reason is required to reject")
    return reason


def _conflict(code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT, detail={"code": code, "message": message, **extra}
    )


def _is_unchanged(
    title: str, content: str, mentions: list[dict[str, Any]] | None, base: Any
) -> bool:
    """Owner decisions 6/7 — "unchanged" means title, content AND the SET of
    mentioned emails (`_mention_emails`) all equal `base`'s. Mention order is
    irrelevant; whitespace is NOT normalized (a trailing-space change counts
    as a change) — the same comparison the existing no-op rule uses."""
    return (
        title == base.title
        and content == base.content
        and _mention_emails(mentions) == _mention_emails(base.mentions)
    )


def resubmit_thread(
    db: Session,
    thread_id: int,
    user: UserInfo,
    title: str,
    content: str,
    mentions: list[MentionInput],
    *,
    access: ViewerAccess | None = None,
) -> ThreadDetail:
    """`POST /threads/{id}/resubmit` (plan §4.3) — the author reworks their
    DECLINED thread. Fail-fast order (pinned by a test; plan §12.7): 404
    missing → 403 no view → 403 not the author → 409 `not_rejected` → 409
    `closed` (the author closed it — final, R1) → mention validation (400
    over the cap) → 409 `unchanged_resubmission`.

    Effects: the thread takes the new title/content/mentions,
    `resubmission_count += 1`, `submitted_at = now` (the back of the
    Pending queue, decision 14), and `reviewed_*` is CLEARED (landmine 3 —
    the declined round's reviewer lives on in its `thread_submissions` row;
    `last_decided_at` is untouched so grouped History keeps the thread).

    An author who is NOW an editor of the widget (D5) publishes at once —
    `create_thread_for_link`'s published branch: `approved`, v1 written
    with them as publisher, mentions fanned out ONCE, and no decision round
    (M5). Otherwise the thread is `pending` again and nobody is notified
    (D6; mention fan-out stays at publication — landmine 10). `access=None`
    is never an editor, so an internal caller always lands pending."""
    thread, component = _require_thread_and_component(db, thread_id)
    _check_view_access(component, access)
    _require_author(thread.author_email, user)

    thread = _lock_thread(db, thread_id)
    if thread.status != THREAD_STATUS_REJECTED:
        db.rollback()
        raise _conflict("not_rejected", "Only a declined thread can be resubmitted")
    if thread.closed_at is not None:
        conflict = _closed_conflict(thread.closed_kind, None)
        db.rollback()
        raise conflict

    resolved = _validate_and_resolve_mentions(db, mentions, content, component=component)
    # A rejected thread is frozen (PATCH is a 409), so its live columns ARE
    # the declined snapshot.
    if _is_unchanged(title, content, resolved, thread):
        db.rollback()
        raise _conflict("unchanged_resubmission", "Change the thread before resubmitting")

    now = _utc_now()
    thread.title = title
    thread.content = content
    thread.mentions = resolved
    thread.resubmission_count = (thread.resubmission_count or 0) + 1
    thread.submitted_at = now
    thread.reviewed_by_email = None
    thread.reviewed_by_name = None
    thread.reviewed_at = None

    is_editor = _is_component_editor(component, access)
    if is_editor:
        thread.status = THREAD_STATUS_APPROVED
        _write_original_revision(db, thread, publisher=user, published_at=now)
        _notify_mentions(
            db,
            resolved,
            actor=user,
            component_id=component.id,
            thread_id=thread.id,
            comment_id=None,
            content=content,
        )
    else:
        thread.status = THREAD_STATUS_PENDING

    db.commit()
    db.refresh(thread)
    return _to_thread_detail(
        thread,
        _my_vote_for_thread(db, thread.id, user.email),
        pending_revisions=_pending_revision_summaries(db, thread, component, user, access=access),
        viewer_is_editor=is_editor, viewer=user,
    )


def _closed_conflict(closed_kind: str | None, closed_by_version: int | None) -> HTTPException:
    """plan §12.7 — resubmitting a CLOSED item: 409 `closed`, with why
    (`closed_kind`: 'author' | 'superseded') and, when superseded, which
    live version closed it — the client's G4 copy depends on both."""
    if closed_kind == CLOSED_KIND_SUPERSEDED:
        message = "The thread was updated since this edit was declined"
    else:
        message = "This item was closed"
    return _conflict(
        "closed",
        message,
        closed_kind=closed_kind,
        closed_by_version=closed_by_version,
    )


def close_rejected_thread(
    db: Session, thread_id: int, user: UserInfo, *, access: ViewerAccess | None = None
) -> None:
    """`POST /threads/{id}/close` (plan §12.7, owner decision R1) — the
    author CLOSES their declined thread: `closed_at = now`, `closed_kind =
    'author'`. Final — nothing reopens it; it stays readable (read-only) and
    moves to the inbox's `closed` list. Order: 404 → 403 view → 403 author
    → (lock) → already closed → 204 (idempotent) → 409 `not_rejected`. The
    admin hard delete (`delete_thread_by_id`) is unchanged (R6)."""
    thread, component = _require_thread_and_component(db, thread_id)
    _check_view_access(component, access)
    _require_author(thread.author_email, user)

    thread = _lock_thread(db, thread_id)
    if thread.closed_at is not None:
        db.rollback()
        return
    if thread.status != THREAD_STATUS_REJECTED:
        db.rollback()
        raise _conflict("not_rejected", "Only a declined thread can be closed")
    thread.closed_at = _utc_now()
    thread.closed_kind = CLOSED_KIND_AUTHOR
    db.commit()


def _require_own_revision(
    db: Session, thread_id: int, revision_id: int, user: UserInfo, access: ViewerAccess | None
) -> tuple[ThreadV2, ComponentV2, ThreadRevisionV2]:
    """Gates shared by the two revision writers below: 404 thread → 403 view
    → 404 revision (absent or on another thread) → 403 not its submitter.
    Then the locks, threads row FIRST. Returns the LOCKED rows."""
    thread, component = _require_thread_and_component(db, thread_id)
    _check_view_access(component, access)
    revision = (
        db.query(ThreadRevisionV2)
        .filter(ThreadRevisionV2.id == revision_id, ThreadRevisionV2.thread_id == thread_id)
        .first()
    )
    if revision is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Revision not found")
    _require_author(revision.submitted_by_email, user)

    thread = _lock_thread(db, thread_id)
    revision = (
        db.query(ThreadRevisionV2)
        .filter(ThreadRevisionV2.id == revision_id)
        .with_for_update()
        .populate_existing()  # loaded unlocked above — see `_lock_thread`
        .first()
    )
    return thread, component, revision


def _existing_rework(db: Session, revision: ThreadRevisionV2) -> ThreadRevisionV2 | None:
    return (
        db.query(ThreadRevisionV2)
        .filter(ThreadRevisionV2.resubmission_of_id == revision.id)
        .first()
    )


def _already_resubmitted(rework: ThreadRevisionV2) -> HTTPException:
    """D4's 409 — carries the rework so the client can point at it."""
    return _conflict(
        "already_resubmitted",
        "This declined edit was already reworked",
        resubmission=_to_revision_summary(rework).model_dump(mode="json"),
    )


def resubmit_revision(
    db: Session,
    thread_id: int,
    revision_id: int,
    user: UserInfo,
    title: str,
    content: str,
    mentions: list[MentionInput],
    *,
    overwrite: bool = False,
    access: ViewerAccess | None = None,
) -> ThreadUpdateResult:
    """`POST /threads/{id}/revisions/{rid}/resubmit` (plan §4.4) — the
    submitter reworks their DECLINED edit into a linked new revision
    (owner decision 9): `resubmission_of_id` → the declined one,
    `resubmission_number` = its number + 1; the declined row stays
    `rejected`.

    Order (plan §12.7): 404 thread → 403 view → 404 revision → 403 not the
    submitter → (locks: threads, then revision) → 409 `not_rejected` (not
    rejected, or not an author edit) → 409 `closed` (closed by the author,
    or superseded by a newer live version — §12.4; `closed_kind` and
    `closed_by_version` in the body) → 409
    `already_resubmitted` (D4 — reworked once; the partial UNIQUE
    `uq_thread_revisions_resubmission_of` is the race backstop) → 409
    `pending_edit_exists` (non-editor only: the thread already has a pending
    author edit — plan_thread_pending_edit_update §3.2) → mention
    validation → 409 `unchanged_resubmission` vs the DECLINED snapshot
    (landmine 9: the base is the declined revision, never "latest pending
    else live") → 409 `no_change` vs the LIVE thread (D7).

    Then the existing paths, with an explicit full snapshot: an editor
    (D5) goes live through `_apply_editor_edit` (its 409
    `earlier_revisions_pending` / `overwrite` rule included); anyone else is
    staged by `_stage_thread_edit`. Returns `ThreadUpdateResult`, the PATCH
    shape."""
    thread, component, revision = _require_own_revision(db, thread_id, revision_id, user, access)
    if revision.status != REVISION_STATUS_REJECTED or revision.origin != REVISION_ORIGIN_AUTHOR:
        db.rollback()
        raise _conflict("not_rejected", "Only a declined edit can be resubmitted")
    if revision.closed_at is not None:
        conflict = _closed_conflict(revision.closed_kind, revision.closed_by_version)
        db.rollback()
        raise conflict
    rework = _existing_rework(db, revision)
    if rework is not None:
        conflict = _already_resubmitted(rework)
        db.rollback()
        raise conflict
    # plan_thread_pending_edit_update §3.2 (U1/U5): the staged path must never
    # create a SECOND pending edit. With U5 in place a pending edit next to an
    # open declined one only comes from legacy data; the author can still
    # close the declined edit. An editor-author's rework goes live instead
    # (D5) and keeps its `earlier_revisions_pending` / `overwrite` rule.
    if not _is_component_editor(component, access):
        pending = (
            db.query(ThreadRevisionV2)
            .filter(
                ThreadRevisionV2.thread_id == thread.id,
                ThreadRevisionV2.status == REVISION_STATUS_PENDING,
                ThreadRevisionV2.origin == REVISION_ORIGIN_AUTHOR,
            )
            .order_by(ThreadRevisionV2.version.desc())
            .first()
        )
        if pending is not None:
            conflict = _conflict(
                "pending_edit_exists",
                "You already have an edit awaiting review",
                revision=_to_revision_summary(pending).model_dump(mode="json"),
            )
            db.rollback()
            raise conflict

    resolved = _validate_and_resolve_mentions(db, mentions, content, component=component)
    if _is_unchanged(title, content, resolved, revision):
        db.rollback()
        raise _conflict("unchanged_resubmission", "Change the edit before resubmitting")
    if _is_unchanged(title, content, resolved, thread):
        db.rollback()
        raise _conflict("no_change", "This is already the published version of the thread")

    try:
        if _is_component_editor(component, access):
            return _apply_editor_edit(
                db, thread_id, component, user, title, content, None,
                overwrite=overwrite, access=access,
                resubmission_of=revision, resolved_mentions=resolved,
            )
        return _stage_thread_edit(
            db, thread_id, component, user, title, content, None,
            access=access, resubmission_of=revision, resolved_mentions=resolved,
        )
    except IntegrityError:
        # A concurrent rework of the same declined edit won the partial
        # UNIQUE (`FOR UPDATE` already serializes this on Postgres).
        db.rollback()
        rework = _existing_rework(db, revision)
        if rework is None:
            raise
        raise _already_resubmitted(rework)


def close_rejected_revision(
    db: Session,
    thread_id: int,
    revision_id: int,
    user: UserInfo,
    *,
    access: ViewerAccess | None = None,
) -> None:
    """`POST /threads/{id}/revisions/{rid}/close` (plan §12.7, R1) — the
    submitter CLOSES their declined edit (`closed_kind = 'author'`). Final.
    Gates as `resubmit_revision`. Order: … (locks) → already closed → 204
    (idempotent, whatever the kind — an edit already superseded is just as
    closed) → 409 `not_rejected` (anything but a declined author edit) →
    409 `already_resubmitted` (reworked — it left the inbox already)."""
    thread, component, revision = _require_own_revision(db, thread_id, revision_id, user, access)
    if revision.closed_at is not None:
        db.rollback()
        return
    if revision.status != REVISION_STATUS_REJECTED or revision.origin != REVISION_ORIGIN_AUTHOR:
        db.rollback()
        raise _conflict("not_rejected", "Only a declined edit can be closed")
    rework = _existing_rework(db, revision)
    if rework is not None:
        conflict = _already_resubmitted(rework)
        db.rollback()
        raise conflict
    revision.closed_at = _utc_now()
    revision.closed_kind = CLOSED_KIND_AUTHOR
    db.commit()


def _component_filter(query, component_ids: Collection[int] | None):
    """plan_my_revisions §4.2 — `None` = every widget (filter by author
    only); otherwise `threads.component_id IN (...)`. An empty collection
    matches nothing (an explicit FALSE, never an unfiltered query)."""
    if component_ids is None:
        return query
    ids = list(component_ids)
    if not ids:
        return query.filter(false())
    return query.filter(ThreadV2.component_id.in_(ids))


def _own_declined_threads_query(db: Session, component_ids: Collection[int] | None, email: str):
    """The caller's own declined threads on the given widgets (`None` =
    all), open AND closed — the inbox splits them on `closed_at`."""
    return _component_filter(
        db.query(ThreadV2).filter(
            ThreadV2.author_email == _norm_email(email),
            ThreadV2.status == THREAD_STATUS_REJECTED,
        ),
        component_ids,
    )


def _own_declined_revisions_query(db: Session, component_ids: Collection[int] | None, email: str):
    """The caller's own declined AUTHOR edits of the given widgets' threads
    (`None` = all) that were never reworked (a reworked one has left the
    inbox for good), open AND closed — the inbox splits them on
    `closed_at`."""
    return _component_filter(
        db.query(ThreadRevisionV2)
        .join(ThreadV2, ThreadV2.id == ThreadRevisionV2.thread_id)
        .filter(
            ThreadRevisionV2.origin == REVISION_ORIGIN_AUTHOR,
            ThreadRevisionV2.status == REVISION_STATUS_REJECTED,
            ThreadRevisionV2.submitted_by_email == _norm_email(email),
            _not_reworked(db),
        ),
        component_ids,
    )


def _declined_threads_query(db: Session, component_ids: Collection[int] | None, email: str):
    """The inbox's OPEN thread predicate (plan §4.7, §12.3): the caller's
    own declined, not closed threads on the given widgets."""
    return _own_declined_threads_query(db, component_ids, email).filter(ThreadV2.closed_at.is_(None))


def _declined_revisions_query(db: Session, component_ids: Collection[int] | None, email: str):
    """The inbox's OPEN edit predicate (plan §4.7, §12.3): the caller's own
    declined author edits of the given widgets' threads, not closed, not yet
    reworked (no revision has `resubmission_of_id` = it)."""
    return _own_declined_revisions_query(db, component_ids, email).filter(
        ThreadRevisionV2.closed_at.is_(None)
    )


def _own_pending_threads_query(db: Session, component_ids: Collection[int] | None, email: str):
    """plan_my_revisions M5 — the caller's own NEW threads awaiting review."""
    return _component_filter(
        db.query(ThreadV2).filter(
            ThreadV2.author_email == _norm_email(email),
            ThreadV2.status == THREAD_STATUS_PENDING,
        ),
        component_ids,
    )


def _own_pending_revisions_query(db: Session, component_ids: Collection[int] | None, email: str):
    """plan_my_revisions M5 — the caller's own author EDITS awaiting review."""
    return _component_filter(
        db.query(ThreadRevisionV2)
        .join(ThreadV2, ThreadV2.id == ThreadRevisionV2.thread_id)
        .filter(
            ThreadRevisionV2.origin == REVISION_ORIGIN_AUTHOR,
            ThreadRevisionV2.status == REVISION_STATUS_PENDING,
            ThreadRevisionV2.submitted_by_email == _norm_email(email),
        ),
        component_ids,
    )


def _declined_count(db: Session, component_id: int, email: str) -> int:
    """`ThreadListResponse.declined_count` — the inbox's OPEN predicate as
    two COUNTs; closed items never count (R5). Only ever called for the
    caller's OWN email."""
    return int(_declined_threads_query(db, [component_id], email).count()) + int(
        _declined_revisions_query(db, [component_id], email).count()
    )


def list_declined_items(
    db: Session,
    link: str,
    user: UserInfo | None,
    *,
    access: ViewerAccess | None = None,
) -> DeclinedItemsResponse:
    """`GET /threads/component/{link}/declined` (plan §4.7, amended §12.7) —
    the caller's own declined items on one widget, in two lists:

    - `items` — the OPEN ones that await their changes: declined threads
      (reason = that thread's latest REJECTED round's, None for a
      pre-feature decline — owner decision 12) and declined, not-yet-
      reworked edits. Newest `declined_at` first (`threads.reviewed_at` for
      a thread — still set while it is rejected; `thread_revisions.
      reviewed_at` for an edit). Capped at `DECLINED_ITEMS_CAP` with
      `truncated`.
    - `closed` — the CLOSED ones (by the author, or an edit superseded by a
      newer live version), read-only, newest `closed_at` first, capped at
      `CLOSED_ITEMS_CAP` with `closed_truncated` (R5: only ever shown in
      the Revisions view's "Closed (k)" section).

    A reworked edit is in neither list. 404 (widget) → 403 (view). A
    no-subject caller (`user=None`: anonymous / view-as-user) gets empty
    lists. Every item carries its widget's fields (plan_my_revisions §4.1 —
    the same item shape as `/threads/me/revisions`)."""
    component = resolve_thread_widget_component(db, link)
    _check_view_access(component, access)
    if user is None:
        return DeclinedItemsResponse()
    return _inbox_lists(db, [component.id], user.email, _widget_fields(db, [component.id]))


# A widget's display fields on an inbox item: (link, title, location_label).
_WidgetFields = dict[int, tuple[str, str, str | None]]


def _widget_fields(db: Session, component_ids: Collection[int]) -> _WidgetFields:
    """plan_my_revisions §4.2 step 4 — `component_id -> (link, widget_title,
    location_label)` for every distinct widget, in ONE batched
    `_widget_title_and_location_for_components` call."""
    ids = list(set(component_ids))
    if not ids:
        return {}
    components = {c.id: c for c in db.query(ComponentV2).filter(ComponentV2.id.in_(ids)).all()}
    labels = _widget_title_and_location_for_components(db, components)
    return {
        cid: (components[cid].link or "", labels[cid][0], labels[cid][1] or None)
        for cid in components
    }


def _inbox_lists(
    db: Session,
    component_ids: Collection[int] | None,
    email: str,
    widgets: _WidgetFields,
) -> DeclinedItemsResponse:
    """The two inbox lists (open + closed) over `component_ids` — shared by
    the per-widget inbox and My Revisions."""
    items, truncated = _declined_items(
        db,
        _declined_threads_query(db, component_ids, email)
        .order_by(ThreadV2.reviewed_at.desc(), ThreadV2.id.desc())
        .limit(DECLINED_ITEMS_CAP + 1)
        .all(),
        _declined_revisions_query(db, component_ids, email)
        .order_by(ThreadRevisionV2.reviewed_at.desc(), ThreadRevisionV2.id.desc())
        .limit(DECLINED_ITEMS_CAP + 1)
        .all(),
        cap=DECLINED_ITEMS_CAP,
        sort_attr="declined_at",
        widgets=widgets,
    )
    closed, closed_truncated = _declined_items(
        db,
        _own_declined_threads_query(db, component_ids, email)
        .filter(ThreadV2.closed_at.isnot(None))
        .order_by(ThreadV2.closed_at.desc(), ThreadV2.id.desc())
        .limit(CLOSED_ITEMS_CAP + 1)
        .all(),
        _own_declined_revisions_query(db, component_ids, email)
        .filter(ThreadRevisionV2.closed_at.isnot(None))
        .order_by(ThreadRevisionV2.closed_at.desc(), ThreadRevisionV2.id.desc())
        .limit(CLOSED_ITEMS_CAP + 1)
        .all(),
        cap=CLOSED_ITEMS_CAP,
        sort_attr="closed_at",
        widgets=widgets,
    )
    return DeclinedItemsResponse(
        items=items, truncated=truncated, closed=closed, closed_truncated=closed_truncated
    )


def _thread_meta(db: Session, thread_ids: Collection[int]) -> dict[int, tuple[str, int]]:
    """`thread_id -> (live title, component_id)` in one query — an edit
    item names its live thread and its widget."""
    ids = list(set(thread_ids))
    if not ids:
        return {}
    return {
        tid: (title, cid)
        for tid, title, cid in db.query(ThreadV2.id, ThreadV2.title, ThreadV2.component_id)
        .filter(ThreadV2.id.in_(ids))
        .all()
    }


def _widget_kwargs(widgets: _WidgetFields, component_id: int) -> dict[str, Any]:
    link, title, location = widgets.get(component_id, ("", "", None))
    return {
        "component_id": component_id,
        "component_link": link,
        "widget_title": title,
        "location_label": location,
    }


def _declined_items(
    db: Session,
    threads: list[ThreadV2],
    revisions: list[ThreadRevisionV2],
    *,
    cap: int,
    sort_attr: str,
    widgets: _WidgetFields | None = None,
) -> tuple[list[DeclinedThreadItem | DeclinedEditItem], bool]:
    """Build one inbox list from its two already-capped (`cap + 1`) halves:
    merge, sort by `sort_attr` DESC (tie-break on id), cut to `cap`, and
    report whether anything was cut. `widgets` fills each item's widget
    fields (plan_my_revisions §4.1)."""
    widgets = widgets or {}
    reasons: dict[int, str | None] = {}
    if threads:
        for row in (
            db.query(ThreadSubmissionV2)
            .filter(
                ThreadSubmissionV2.thread_id.in_([t.id for t in threads]),
                ThreadSubmissionV2.status == THREAD_STATUS_REJECTED,
            )
            .order_by(ThreadSubmissionV2.thread_id, ThreadSubmissionV2.round)
            .all()
        ):
            reasons[row.thread_id] = row.reason  # ascending → the latest wins
    meta = _thread_meta(db, {r.thread_id for r in revisions})

    items: list[DeclinedThreadItem | DeclinedEditItem] = [
        DeclinedThreadItem(
            thread_id=t.id,
            title=t.title,
            content_excerpt=generate_notification_excerpt(t.content),
            resubmission_count=t.resubmission_count or 0,
            reason=reasons.get(t.id),
            declined_by_email=t.reviewed_by_email,
            declined_by_name=t.reviewed_by_name,
            declined_at=t.reviewed_at,
            closed_at=t.closed_at,
            closed_kind=t.closed_kind,
            **_widget_kwargs(widgets, t.component_id),
        )
        for t in threads
    ] + [
        DeclinedEditItem(
            thread_id=r.thread_id,
            revision_id=r.id,
            version=r.version,
            title=r.title,
            content_excerpt=generate_notification_excerpt(r.content),
            thread_title=meta.get(r.thread_id, ("", 0))[0],
            resubmission_number=r.resubmission_number or 0,
            reason=r.rejection_reason,
            declined_by_email=r.reviewed_by_email,
            declined_by_name=r.reviewed_by_name,
            declined_at=r.reviewed_at,
            closed_at=r.closed_at,
            closed_kind=r.closed_kind,
            closed_by_version=r.closed_by_version,
            **_widget_kwargs(widgets, meta.get(r.thread_id, ("", 0))[1]),
        )
        for r in revisions
    ]
    _epoch = datetime.min.replace(tzinfo=timezone.utc)

    def _key(item: DeclinedThreadItem | DeclinedEditItem):
        instant = getattr(item, sort_attr)
        return (
            _as_utc(instant) if instant else _epoch,
            item.revision_id if isinstance(item, DeclinedEditItem) else item.thread_id,
        )

    items.sort(key=_key, reverse=True)
    return items[:cap], len(items) > cap


# ---------------------------------------------------------
# My Revisions (plan_my_revisions_2026-11-10.md §4.2) — the author's
# declined + pending items across EVERY thread widget, for the sidebar
# section and its page. Polled (the summary) by every signed-in user, so:
# - one cheap EXISTS by author email first (`ix_thread_revisions_author_open`
#   + the `threads.author_email` index) — no rows, no RBAC work at all;
# - then the per-widget view gate on the author's DISTINCT widgets only,
#   via `granted_single_node` + ONE shared `RbacClosures` (the mention-
#   directory pattern), never the whole-tree `get_viewer_access` fold
#   (landmine 16). A Hub Admin short-circuits.
# A widget the author can no longer view drops out of every list and count
# (today's per-widget inbox 403s for it).
# ---------------------------------------------------------

PENDING_ITEMS_CAP = 100

# `is_admin(closures)` — supplied by the router (`dependencies.is_hub_admin`
# with the shared closures); a domain service must not import the FastAPI
# dependency module.
IsAdminFn = Callable[[RbacClosures], bool]


def _has_my_revision_rows(db: Session, email: str) -> bool:
    """The summary's short-circuit: does the author have ANY declined or
    pending thread or author edit at all? Two indexed EXISTS probes."""
    norm = _norm_email(email)
    thread_exists = (
        db.query(ThreadV2.id)
        .filter(
            ThreadV2.author_email == norm,
            ThreadV2.status.in_([THREAD_STATUS_REJECTED, THREAD_STATUS_PENDING]),
        )
        .exists()
    )
    revision_exists = (
        db.query(ThreadRevisionV2.id)
        .filter(
            ThreadRevisionV2.submitted_by_email == norm,
            ThreadRevisionV2.origin == REVISION_ORIGIN_AUTHOR,
            ThreadRevisionV2.status.in_([REVISION_STATUS_REJECTED, REVISION_STATUS_PENDING]),
        )
        .exists()
    )
    return bool(db.query(or_(thread_exists, revision_exists)).scalar())


def _my_revision_component_ids(db: Session, email: str) -> set[int]:
    """Every DISTINCT widget the author has a declined or pending thread or
    author edit on — the set the view gate runs over. Gating this whole set
    BEFORE fetching (rather than the cap-limited rows after) keeps every cap
    exact: a lost widget's rows can never crowd an allowed widget's out."""
    norm = _norm_email(email)
    ids = {
        cid
        for (cid,) in db.query(ThreadV2.component_id)
        .filter(
            ThreadV2.author_email == norm,
            ThreadV2.status.in_([THREAD_STATUS_REJECTED, THREAD_STATUS_PENDING]),
        )
        .distinct()
        .all()
    }
    ids.update(
        cid
        for (cid,) in db.query(ThreadV2.component_id)
        .join(ThreadRevisionV2, ThreadRevisionV2.thread_id == ThreadV2.id)
        .filter(
            ThreadRevisionV2.submitted_by_email == norm,
            ThreadRevisionV2.origin == REVISION_ORIGIN_AUTHOR,
            ThreadRevisionV2.status.in_([REVISION_STATUS_REJECTED, REVISION_STATUS_PENDING]),
        )
        .distinct()
        .all()
    )
    return ids


def _viewable_component_ids(
    db: Session, component_ids: Collection[int], *, hub_user_id: int, is_admin: IsAdminFn
) -> set[int]:
    """plan_my_revisions §4.2 step 2 — the widgets in `component_ids` the
    caller can still VIEW, by the same `is_granted` rule
    `_check_view_access` applies (`granted_single_node` is its O(depth)
    single-node twin). ONE `RbacClosures` for the admin check and every
    widget; a Hub Admin keeps them all."""
    closures = RbacClosures(db)
    if is_admin(closures):
        return set(component_ids)
    return {
        cid
        for cid in sorted(component_ids)
        if granted_single_node(db, hub_user_id, ("component", cid), is_admin=False, closures=closures)
    }


def _pending_items(
    db: Session, component_ids: Collection[int], email: str, widgets: _WidgetFields
) -> tuple[list[PendingThreadItem | PendingEditItem], bool]:
    """plan_my_revisions M5 — the author's own pending threads and pending
    author edits, newest submission first, capped at `PENDING_ITEMS_CAP`."""
    threads = (
        _own_pending_threads_query(db, component_ids, email)
        .order_by(ThreadV2.submitted_at.desc(), ThreadV2.id.desc())
        .limit(PENDING_ITEMS_CAP + 1)
        .all()
    )
    revisions = (
        _own_pending_revisions_query(db, component_ids, email)
        .order_by(ThreadRevisionV2.submitted_at.desc(), ThreadRevisionV2.id.desc())
        .limit(PENDING_ITEMS_CAP + 1)
        .all()
    )
    meta = _thread_meta(db, {r.thread_id for r in revisions})
    items: list[PendingThreadItem | PendingEditItem] = [
        PendingThreadItem(
            thread_id=t.id,
            title=t.title,
            content_excerpt=generate_notification_excerpt(t.content),
            resubmission_count=t.resubmission_count or 0,
            submitted_at=t.submitted_at or t.created_at,
            edited_at=t.edited_at,
            **_widget_kwargs(widgets, t.component_id),
        )
        for t in threads
    ] + [
        PendingEditItem(
            thread_id=r.thread_id,
            revision_id=r.id,
            version=r.version,
            title=r.title,
            content_excerpt=generate_notification_excerpt(r.content),
            thread_title=meta.get(r.thread_id, ("", 0))[0],
            resubmission_number=r.resubmission_number or 0,
            resubmission_of_version=_resubmission_of_version(r),
            submitted_at=r.submitted_at,
            updated_at=r.updated_at,
            edit_count=r.edit_count or 0,
            **_widget_kwargs(widgets, meta.get(r.thread_id, ("", 0))[1]),
        )
        for r in revisions
    ]
    _epoch = datetime.min.replace(tzinfo=timezone.utc)

    def _key(item: PendingThreadItem | PendingEditItem):
        return (
            _as_utc(item.submitted_at) if item.submitted_at else _epoch,
            item.revision_id if isinstance(item, PendingEditItem) else item.thread_id,
        )

    items.sort(key=_key, reverse=True)
    return items[:PENDING_ITEMS_CAP], len(items) > PENDING_ITEMS_CAP


def list_my_declined_items(
    db: Session,
    user: UserInfo,
    *,
    hub_user_id: int,
    is_admin: IsAdminFn,
) -> MyRevisionsResponse:
    """`GET /threads/me/revisions` (plan_my_revisions §4.2) — the caller's
    own declined items (open + closed, the per-widget inbox's exact lists
    and caps) and pending items (M5), across every thread widget they can
    still view. The router answers View As / `anonymous` itself (empty,
    `view_as: true`) and never reaches here for them."""
    component_ids = _my_revision_component_ids(db, user.email)
    if not component_ids:
        return MyRevisionsResponse()
    allowed = _viewable_component_ids(db, component_ids, hub_user_id=hub_user_id, is_admin=is_admin)
    if not allowed:
        return MyRevisionsResponse()
    widgets = _widget_fields(db, allowed)
    inbox = _inbox_lists(db, allowed, user.email, widgets)
    pending, pending_truncated = _pending_items(db, allowed, user.email, widgets)
    return MyRevisionsResponse(
        **inbox.model_dump(),
        pending=pending,
        pending_truncated=pending_truncated,
    )


def _max_id(query, column) -> int:
    value = query.with_entities(func.max(column)).scalar()
    return int(value) if value is not None else 0


def my_revisions_summary(
    db: Session,
    user: UserInfo,
    *,
    hub_user_id: int,
    is_admin: IsAdminFn,
) -> tuple[MyRevisionsSummary, str]:
    """`GET /threads/me/revisions/summary` (plan_my_revisions §4.2). The
    EXISTS short-circuit comes FIRST: an author with no rows (almost every
    user on almost every poll) costs two indexed probes and no RBAC work.
    Otherwise the same per-widget gate as the list, then COUNTs.

    ETag `"{open}-{closed}-{pending}-{max_id_t}-{max_id_r}"` — every body
    field is a function of the three counts, so the tag covers the body
    (the `moderation_summary` discipline, followups §1); the max ids make a
    decline-then-close that leaves the counts equal still change it."""
    if not _has_my_revision_rows(db, user.email):
        return MyRevisionsSummary(), '"0-0-0-0-0"'
    component_ids = _my_revision_component_ids(db, user.email)
    allowed = _viewable_component_ids(db, component_ids, hub_user_id=hub_user_id, is_admin=is_admin)
    email = user.email
    open_t = _declined_threads_query(db, allowed, email)
    open_r = _declined_revisions_query(db, allowed, email)
    closed_t = _own_declined_threads_query(db, allowed, email).filter(ThreadV2.closed_at.isnot(None))
    closed_r = _own_declined_revisions_query(db, allowed, email).filter(
        ThreadRevisionV2.closed_at.isnot(None)
    )
    pending_t = _own_pending_threads_query(db, allowed, email)
    pending_r = _own_pending_revisions_query(db, allowed, email)
    open_count = int(open_t.count()) + int(open_r.count())
    closed_count = int(closed_t.count()) + int(closed_r.count())
    pending_count = int(pending_t.count()) + int(pending_r.count())
    max_t = max(_max_id(q, ThreadV2.id) for q in (open_t, closed_t, pending_t))
    max_r = max(_max_id(q, ThreadRevisionV2.id) for q in (open_r, closed_r, pending_r))
    summary = MyRevisionsSummary(
        open_count=open_count,
        closed_count=closed_count,
        pending_count=pending_count,
        has_any=(open_count + closed_count + pending_count) > 0,
    )
    return summary, f'"{open_count}-{closed_count}-{pending_count}-{max_t}-{max_r}"'


def my_revisions_view_as_answer() -> tuple[MyRevisionsSummary, str]:
    """M7 — the section is personal and View As is read-only: any View As
    selection (pairs, a user) or `anonymous` gets the empty summary."""
    return MyRevisionsSummary(view_as=True), '"0-0-0-0-0-va"'


def get_thread_submission_history(
    db: Session,
    thread_id: int,
    user: UserInfo | None,
    *,
    access: ViewerAccess | None = None,
) -> list[ThreadSubmissionRound]:
    """`GET /threads/{id}/submission-history` (plan §4.6) — every decided
    round, oldest first, with full snapshots, plus a SYNTHESIZED `pending`
    round (round = `resubmission_count`, snapshot = the live columns,
    `reviewed_*`/`reason` None) when the thread is pending.

    403 (view) BEFORE 404. Then the author or a component editor
    (`_can_see_thread_revisions` — D2, who may see a reason); anyone else
    gets the 404 a nonexistent id gives. A CLOSED thread reads for its
    author like any other (plan §12.7).

    plan §12.8 G3: a resubmission the editor-author published themselves
    (D5) wrote no round, so the timeline ends with a SYNTHESIZED
    `published` round (snapshot = v1, reviewer = v1's publisher)."""
    thread, component = _require_thread_and_component(db, thread_id)
    _check_view_access(component, access)
    if not _can_see_thread_revisions(thread, component, user, access):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found")

    stored = (
        db.query(ThreadSubmissionV2)
        .filter(ThreadSubmissionV2.thread_id == thread.id)
        .order_by(ThreadSubmissionV2.round.asc())
        .all()
    )
    rows: list[Any] = list(stored)
    if _is_self_published_resubmission(thread, stored):
        v1 = (
            db.query(ThreadRevisionV2)
            .filter(ThreadRevisionV2.thread_id == thread.id, ThreadRevisionV2.version == 1)
            .first()
        )
        if v1 is not None:
            rows.append(_published_round(thread, v1))

    rounds = [
        ThreadSubmissionRound(
            round=r.round,
            status=r.status,
            title=r.title,
            content=r.content,
            mentions=r.mentions or [],
            reason=r.reason,
            submitted_by_email=r.submitted_by_email,
            submitted_by_name=r.submitted_by_name,
            submitted_at=r.submitted_at,
            reviewed_by_email=r.reviewed_by_email,
            reviewed_by_name=r.reviewed_by_name,
            reviewed_at=r.reviewed_at,
        )
        for r in rows
    ]
    if thread.status == THREAD_STATUS_PENDING:
        rounds.append(
            ThreadSubmissionRound(
                round=thread.resubmission_count or 0,
                status="pending",
                title=thread.title,
                content=thread.content,
                mentions=thread.mentions or [],
                reason=None,
                submitted_by_email=thread.author_email,
                submitted_by_name=thread.author_name,
                submitted_at=thread.submitted_at or thread.created_at,
            )
        )
    return rounds


def get_revision_chain(
    db: Session,
    thread_id: int,
    revision_id: int,
    user: UserInfo | None,
    *,
    access: ViewerAccess | None = None,
) -> list[ThreadRevisionDetail]:
    """`GET /threads/{id}/revisions/{rid}/chain` (plan §5) — the rework
    chain `revision_id` belongs to (every declined edit and its reworks,
    via `resubmission_of_id`), oldest first, each with its reason. Same
    gate as `get_thread_revision`. A chain is linear (D4: one rework per
    declined edit), so this walks back to the root and then forward."""
    thread, component = _require_thread_and_component(db, thread_id)
    _check_view_access(component, access)
    if not _can_see_thread_revisions(thread, component, user, access):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Revision not found")

    def _get(rid: int | None) -> ThreadRevisionV2 | None:
        if rid is None:
            return None
        return (
            db.query(ThreadRevisionV2)
            .filter(ThreadRevisionV2.id == rid, ThreadRevisionV2.thread_id == thread_id)
            .first()
        )

    current = _get(revision_id)
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Revision not found")
    chain = [current]
    seen = {current.id}
    back = _get(current.resubmission_of_id)
    while back is not None and back.id not in seen:
        chain.insert(0, back)
        seen.add(back.id)
        back = _get(back.resubmission_of_id)
    forward = _existing_rework(db, current)
    while forward is not None and forward.id not in seen:
        chain.append(forward)
        seen.add(forward.id)
        forward = _existing_rework(db, forward)
    return [_to_revision_detail(r) for r in chain]


# ---------------------------------------------------------
# Moderation (plan_thread_moderation_2026-09-18.md §4.2, §5.3, §5.7) —
# approve/reject, the pending queue, the history list, and the sidebar's
# polled summary. No edit-lock involvement anywhere here (plan §5.8): these
# write `threads`, never `page_content`, so `edit_lock_service` is never
# consulted and a locked tab does not block moderation.
# ---------------------------------------------------------


def decide_thread(
    db: Session,
    thread_id: int,
    user: UserInfo,
    *,
    approve: bool,
    reason: str | None = None,
    expected_edit_count: int | None = None,
    access: ViewerAccess | None = None,
) -> ThreadModerationRow:
    """`POST /threads/{id}/approve` or `.../reject` — gated on `edit(n)`
    (plan §5.1, landmine 8: NOT `full_access`/Hub-Admin-only, by analogy
    with delete — a root-tab editor with no admin role must be able to
    approve). `FOR UPDATE` serializes two editors deciding at once; the
    loser re-reads a non-pending row and gets the winner's decision back in
    the 409 body.

    plan_thread_reject_resubmit_2026-11-07.md §4.1: a reject REQUIRES
    `reason` (the router validates `RejectRequest`; this asserts it too —
    400). BOTH outcomes write one `thread_submissions` round — round =
    `resubmission_count`, snapshot = what was decided — and stamp
    `last_decided_at`; without the approve round grouped History would
    lose approvals."""
    thread, component = _require_thread_and_component(db, thread_id)
    _require_component_editor(component, access)
    reason = _require_reason(approve, reason)

    # Re-fetch under lock — plan §5.3's recipe, same shape as the vote/
    # comment-count recompute locks elsewhere in this module. The unlocked
    # `thread` above already proved the caller is an editor of the right
    # component; this second read is what makes the pending-check and the
    # write atomic against a concurrent decision on the same row.
    thread = _lock_thread(db, thread_id)
    if thread.status != THREAD_STATUS_PENDING:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "message": "This thread has already been reviewed",
                "thread": _to_moderation_row(db, thread, component).model_dump(mode="json"),
            },
        )
    # plan_thread_pending_edit_update §3.8 (U9) — the decision must apply to
    # the text the reviewer read. Checked AFTER "already reviewed"; None
    # (scripts, older clients) skips it. Nothing is written on a mismatch.
    if expected_edit_count is not None and expected_edit_count != (thread.edit_count or 0):
        conflict = _conflict(
            "thread_changed",
            "This thread changed while you were reviewing",
            thread=_to_moderation_row(db, thread, component).model_dump(mode="json"),
        )
        db.rollback()
        raise conflict

    thread.status = THREAD_STATUS_APPROVED if approve else THREAD_STATUS_REJECTED
    thread.reviewed_by_email = _norm_email(user.email)
    thread.reviewed_by_name = user.name
    thread.reviewed_at = _utc_now()
    thread.last_decided_at = thread.reviewed_at

    # plan_thread_reject_resubmit §2.2/§4.1 — the round log. Snapshot of
    # WHAT WAS DECIDED (edits made while pending included), mentions as
    # stored, never re-resolved. `uq_thread_submissions_thread_round` is the
    # backstop; the FOR UPDATE above already serializes deciders.
    db.add(
        ThreadSubmissionV2(
            thread_id=thread.id,
            round=thread.resubmission_count or 0,
            title=thread.title,
            content=thread.content,
            mentions=list(thread.mentions or []),
            status=thread.status,
            reason=None if approve else reason,
            submitted_by_email=_norm_email(thread.author_email),
            submitted_by_name=thread.author_name,
            submitted_at=thread.submitted_at or thread.created_at,
            reviewed_by_email=thread.reviewed_by_email,
            reviewed_by_name=thread.reviewed_by_name,
            reviewed_at=thread.reviewed_at,
        )
    )

    if approve:
        # plan_thread_edit_versioning amendment A3: publication writes the
        # thread's v1 — its text as approved (edits made while pending
        # included), published by this editor at this instant. A rejected
        # thread was never published and gets no version history.
        _write_original_revision(db, thread, publisher=user, published_at=thread.reviewed_at)
        # plan §5.4 (landmine 1, deferred fan-out): the mentions were
        # validated and stored at post time but nobody was pinged, because
        # the thread wasn't readable yet. Actor = the AUTHOR (they did the
        # mentioning), not the reviewing editor.
        _notify_mentions(
            db,
            thread.mentions or [],
            actor=_author_as_user_info(thread),
            component_id=component.id,
            thread_id=thread.id,
            comment_id=None,
            content=thread.content,
        )
    # M1 (plan §3.2, owner-approved 2026-09-19): tell the author, for BOTH
    # outcomes — the helper picks the type. Sits AFTER the pending check
    # above so a 409 (already reviewed) writes no second row, after the
    # mention fan-out so the two kinds of row land in the order the events
    # happened, and before the commit so it shares the transaction.
    _notify_thread_decision(
        db,
        thread,
        reviewer=user,
        approved=approve,
        component_id=component.id,
        reason=reason,
    )

    db.commit()
    db.refresh(thread)
    return _to_moderation_row(db, thread, component)


def decide_thread_revision(
    db: Session,
    thread_id: int,
    revision_id: int,
    user: UserInfo,
    *,
    approve: bool,
    overwrite: bool = False,
    reason: str | None = None,
    expected_edit_count: int | None = None,
    access: ViewerAccess | None = None,
) -> ThreadRevisionDetail:
    """`POST /threads/{id}/revisions/{rid}/approve` or `.../reject`
    (plan_thread_edit_versioning_2026-09-22.md §4.3) — `decide_thread`'s
    shape, operating on one staged edit instead of the thread's own post.

    Same gate (`_require_component_editor` — an editor of the widget or any
    ancestor, never Hub-Admin-only). Same lock recipe, extended: the
    `threads` row FIRST, then the revision — the order `_stage_thread_edit`
    takes too, so a submit and a decision can never deadlock.

    Approve applies the revision's full snapshot to the live thread and
    sets `threads.version` to its version. Earlier still-pending revisions
    block a plain approve (409 `earlier_revisions_pending`, owner decision
    4); with `overwrite=True` they are marked `overwritten`, stamped with
    THIS decision's reviewer and instant. Reject touches only this one
    revision — never another revision, never the live content (owner
    decision 6). `threads.status` never moves on either path.

    plan_thread_reject_resubmit §12.4/§12.5: approving version K also
    closes every OPEN declined edit below K as `superseded` (R2), behind
    the SAME 409 (now also raised when only `declined_open` is non-empty)
    and the same `overwrite` (R3); each closed edit's submitter gets a
    `thread_edit_closed` bell with this reviewer as actor (R4).

    plan_thread_reject_resubmit §4.1: a reject REQUIRES `reason` (the router
    validates `RejectRequest`; this asserts it too — 400), stored on
    `revision.rejection_reason` and used as the bell excerpt."""
    thread, component = _require_thread_and_component(db, thread_id)
    _require_component_editor(component, access)
    reason = _require_reason(approve, reason)

    thread = _lock_thread(db, thread_id)
    revision = (
        db.query(ThreadRevisionV2)
        .filter(ThreadRevisionV2.id == revision_id, ThreadRevisionV2.thread_id == thread_id)
        .with_for_update()
        .populate_existing()  # the U9-style stale check reads it — see `_lock_thread`
        .first()
    )
    if revision is None:
        # Absent, or belonging to another thread — indistinguishable.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Revision not found")
    if revision.status != REVISION_STATUS_PENDING:
        # Includes a revision a newer approval has already `overwritten` —
        # by §1.2's invariant there is no separate "too old" case.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "already_decided",
                "message": "This edit has already been reviewed",
                "revision": _to_revision_summary(revision).model_dump(mode="json"),
            },
        )
    # plan_thread_pending_edit_update §3.3 — the author may have updated this
    # edit in place since the reviewer opened it. Checked after
    # `already_decided` and BEFORE the overwrite 409 (only the approved
    # revision is checked: an overwritten earlier one is never applied).
    # None skips it. Nothing is written on a mismatch.
    if expected_edit_count is not None and expected_edit_count != (revision.edit_count or 0):
        conflict = _conflict(
            "revision_changed",
            "This edit changed while you were reviewing",
            revision=_to_revision_summary(revision).model_dump(mode="json"),
        )
        db.rollback()
        raise conflict

    earlier: list[ThreadRevisionV2] = []
    declined_open: list[ThreadRevisionV2] = []
    if approve:
        earlier = (
            db.query(ThreadRevisionV2)
            .filter(
                ThreadRevisionV2.thread_id == thread_id,
                ThreadRevisionV2.status == REVISION_STATUS_PENDING,
                ThreadRevisionV2.version < revision.version,
            )
            .order_by(ThreadRevisionV2.version.asc())
            .all()
        )
        # plan §12.4/§12.5 — the OPEN declined edits below this version,
        # which going live will close as superseded. Not this revision's own
        # source when it is a rework: that one is linked, so not open.
        declined_open = _open_declined_edits_below(db, thread_id, revision.version)
        if (earlier or declined_open) and not overwrite:
            # The warning modal's payload. The destructive path is opt-in
            # ON THE WIRE (`overwrite: true`), so no client — or future
            # script — overwrites by accident.
            raise _earlier_revisions_conflict(earlier, revision, declined_open)

    now = _utc_now()
    reviewer_email = _norm_email(user.email)

    if approve:
        for displaced in earlier:
            displaced.status = REVISION_STATUS_OVERWRITTEN
            displaced.reviewed_by_email = reviewer_email
            displaced.reviewed_by_name = user.name
            displaced.reviewed_at = now
        # Read BEFORE the overwrite below (landmine 4) — and it is the LIVE
        # thread's mentions, not the previous revision's: that is what makes
        # an @mention present in both an overwritten v2 and the approved v3
        # notify exactly once.
        previous_mentions = list(thread.mentions or [])
        thread.title = revision.title
        thread.content = revision.content
        thread.mentions = list(revision.mentions or [])
        thread.version = revision.version
        # The approval instant, not `submitted_at` — the "edited" marker
        # appears when the change becomes public, not when it was drafted.
        # Stamped for EVERY version that goes live, a mentions-only one
        # included (owner, 2026-09-23 — same rule as `_apply_editor_edit`).
        thread.edited_at = now
        # plan §12.4 — R2: close the open declined edits this version
        # supersedes; the reviewer is the publisher (bell actor).
        _close_superseded(
            db,
            thread,
            declined_open,
            live_version=revision.version,
            publisher=user,
            component_id=component.id,
            now=now,
        )

    revision.status = REVISION_STATUS_APPROVED if approve else REVISION_STATUS_REJECTED
    revision.reviewed_by_email = reviewer_email
    revision.reviewed_by_name = user.name
    revision.reviewed_at = now
    if not approve:
        revision.rejection_reason = reason

    if approve:
        # plan §4.5 — the fan-out the pre-versioning PATCH ran inline moves
        # here. Actor = the AUTHOR (they did the mentioning), not the
        # reviewer — the same rule `decide_thread` applies. Mentions are
        # not re-scoped at approval (2m's standing posture; plan §11).
        _notify_newly_added_mentions(
            db,
            previous_mentions=previous_mentions,
            resolved_mentions=revision.mentions or [],
            actor=_author_as_user_info(thread),
            component_id=component.id,
            thread_id=thread.id,
            comment_id=None,
            content=revision.content,
        )
    # plan §4.6 — tell the submitter, for BOTH outcomes. ONE call, for THIS
    # revision only: the `earlier` rows an overwrite displaced get no row.
    _notify_thread_edit_decision(
        db,
        thread,
        revision,
        reviewer=user,
        approved=approve,
        component_id=component.id,
        reason=reason,
    )

    db.commit()
    db.refresh(revision)
    return _to_revision_detail(revision)


def get_thread_revision(
    db: Session,
    thread_id: int,
    revision_id: int,
    user: UserInfo,
    *,
    access: ViewerAccess | None = None,
) -> ThreadRevisionDetail:
    """`GET /threads/{id}/revisions/{rid}` — one revision's full body, any
    status. `_check_view_access` runs FIRST (landmine 2): a caller with no
    view access to the widget keeps getting 403, never a 404 that would
    leak existence. Then `_can_see_thread_revisions` (author or component
    editor); anyone else gets the SAME 404 a nonexistent id gives."""
    thread, component = _require_thread_and_component(db, thread_id)
    _check_view_access(component, access)
    if not _can_see_thread_revisions(thread, component, user, access):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Revision not found")
    revision = (
        db.query(ThreadRevisionV2)
        .filter(ThreadRevisionV2.id == revision_id, ThreadRevisionV2.thread_id == thread_id)
        .first()
    )
    if revision is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Revision not found")
    return _to_revision_detail(revision)


PENDING_ORDERS = ("oldest", "newest")


def _encode_pending_cursor(order: str, at: datetime, row_id: int) -> str:
    """A pending-queue cursor that names its order (plan_thread_review_merge
    B1): `[order, at, id]`, so a cursor resent with the OTHER order is a 400
    rather than a silently wrong page."""
    return _encode_keyset([order, at, row_id])


def _decode_pending_cursor(cursor: str, order: str, default_order: str) -> tuple[datetime, int]:
    """Inverse of `_encode_pending_cursor`. A pre-B1 two-element cursor
    (`_encode_cursor`'s `[at, id]`) still decodes, but only under the
    route's DEFAULT order — the only order it could have come from."""
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        if len(raw) == 2:
            cursor_order, (iso, row_id) = default_order, raw
        else:
            cursor_order, iso, row_id = raw
        parsed = datetime.fromisoformat(iso)
        row_id = int(row_id)
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid cursor") from exc
    if cursor_order != order:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cursor belongs to another order")
    return parsed, row_id


def _pending_page(query, at_column, id_column, *, order: str, default_order: str, cursor: str | None):
    """One page of a pending queue, keyset `(submitted_at, id)` in `order`."""
    if order not in PENDING_ORDERS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid order")
    newest = order == "newest"
    keys = tuple_(at_column, id_column)
    if cursor:
        after = _decode_pending_cursor(cursor, order, default_order)
        query = query.filter(keys < after if newest else keys > after)
    direction = (lambda c: c.desc()) if newest else (lambda c: c.asc())
    rows = query.order_by(direction(at_column), direction(id_column)).limit(THREADS_PAGE_SIZE + 1).all()
    has_more = len(rows) > THREADS_PAGE_SIZE
    page = rows[:THREADS_PAGE_SIZE]
    next_cursor = (
        _encode_pending_cursor(order, page[-1].submitted_at, page[-1].id) if has_more and page else None
    )
    return page, next_cursor


def list_pending_threads(
    db: Session,
    cursor: str | None,
    *,
    order: str = "oldest",
    access: ViewerAccess | None = None,
) -> ThreadModerationListResponse:
    """`GET /threads/moderation/pending` — scoped to the caller's moderated
    set (plan §5.2); an empty set is a `200` with an empty page, never a
    403 (a non-moderator deep-linking the management page sees an empty
    state, not an error). Oldest-first (FIFO) — the queue drains in the
    order people waited, not newest-first like the widget's own list.

    plan_thread_reject_resubmit §4.6 (owner decision 14): ordered and
    keyset on `(submitted_at, id)`, not `(created_at, id)` — a resubmission
    joins the BACK of the queue. The cursor keeps `_encode_cursor`'s format;
    its timestamp is now `submitted_at` (landmine 4: a `created_at` cursor
    from a page opened before deploy decodes to a valid-looking position —
    harmless, one odd page). Rows carry `resubmission_count` and
    `previous_rejection` (`_round_fields`, one batched query).

    plan_thread_review_merge B1: `order` = oldest (default, unchanged) |
    newest, so Thread Review can merge this queue with the revision queue
    in ONE order. The cursor names its order (`_encode_pending_cursor`)."""
    if order not in PENDING_ORDERS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid order")
    component_ids = _moderated_component_ids(db, access)
    if not component_ids:
        return ThreadModerationListResponse(items=[], next_cursor=None)

    query = db.query(ThreadV2).filter(
        ThreadV2.component_id.in_(component_ids),
        ThreadV2.status == THREAD_STATUS_PENDING,
    )
    page, next_cursor = _pending_page(
        query, ThreadV2.submitted_at, ThreadV2.id, order=order, default_order="oldest", cursor=cursor
    )
    return ThreadModerationListResponse(items=_to_moderation_rows(db, page), next_cursor=next_cursor)


def list_thread_history(
    db: Session,
    cursor: str | None,
    *,
    statuses: list[str] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    authors: list[str] | None = None,
    reviewers: list[str] | None = None,
    types: list[str] | None = None,
    q: str | None = None,
    group: str | None = None,
    order: str = "newest",
    access: ViewerAccess | None = None,
) -> ThreadHistoryListResponse:
    """`GET /threads/moderation/history` — ONE ROW PER DECISION (2026-11-09
    history-table rework, replacing owner decision 13's one row per thread):
    every stored `thread_submissions` round in the caller's moderated set,
    newest decision first (`reviewed_at DESC, id DESC`). A thread declined
    twice and then approved is three rows. The synthesized G3 `published`
    round is not a decision and never appears (its v1 is in Revision
    Management as an Original). A pending resubmission's earlier rounds stay
    listed; the pending round itself is the Pending tab's.

    Optional filters, all combinable, each on the ROW (no any-match):
    - `statuses` ⊆ approved/rejected — the decision;
    - `types` ⊆ new/resubmission — round 0 vs round > 0;
    - `date_from` (inclusive) / `date_to` (exclusive) — the decision instant;
    - `authors` — the submitter (always the thread author), `reviewers` —
      the decider (any-of, case-insensitive);
    - `q` — substring of the decided title, the thread's current title, or
      the widget title (`_history_search_predicate`).
    `group` = thread | widget keeps each group's rows together
    (`_keyset_page`) and sets `group_size`; `order` = newest | oldest.

    The cursor only encodes the position; the caller must resend the same
    filters, group and order with it."""
    if statuses and any(
        value not in (THREAD_STATUS_APPROVED, THREAD_STATUS_REJECTED) for value in statuses
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid status filter")
    if types and any(value not in ("new", "resubmission") for value in types):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid type filter")
    if group is not None and group not in HISTORY_GROUPS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid group")
    if order not in HISTORY_ORDERS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid order")

    component_ids = _moderated_component_ids(db, access)
    if not component_ids:
        return ThreadHistoryListResponse(items=[], next_cursor=None)

    base = (
        db.query(ThreadSubmissionV2)
        .join(ThreadV2, ThreadV2.id == ThreadSubmissionV2.thread_id)
        .filter(ThreadV2.component_id.in_(component_ids))
    )
    if statuses:
        base = base.filter(ThreadSubmissionV2.status.in_(statuses))
    if types and set(types) != {"new", "resubmission"}:
        base = base.filter(
            ThreadSubmissionV2.round == 0 if types[0] == "new" else ThreadSubmissionV2.round > 0
        )
    if date_from is not None:
        base = base.filter(ThreadSubmissionV2.reviewed_at >= _as_utc(date_from))
    if date_to is not None:
        base = base.filter(ThreadSubmissionV2.reviewed_at < _as_utc(date_to))
    if authors:
        base = base.filter(
            ThreadSubmissionV2.submitted_by_email.in_({_norm_email(e) for e in authors})
        )
    if reviewers:
        base = base.filter(
            ThreadSubmissionV2.reviewed_by_email.in_({_norm_email(e) for e in reviewers})
        )
    search = _history_search_predicate(
        db, q or "", component_ids, [ThreadSubmissionV2.title, ThreadV2.title]
    )
    if search is not None:
        base = base.filter(search)

    group_column = None
    if group == "thread":
        group_column = ThreadSubmissionV2.thread_id
    elif group == "widget":
        group_column = ThreadV2.component_id

    rounds, sizes, next_cursor = _keyset_page(
        db,
        base,
        decided_at=ThreadSubmissionV2.reviewed_at,
        row_id=ThreadSubmissionV2.id,
        group_column=group_column,
        order=order,
        cursor=cursor,
    )
    if not rounds:
        return ThreadHistoryListResponse(items=[], next_cursor=next_cursor)

    # The page's distinct threads, in one batch, built exactly like the
    # pending queue's rows (the review modal reads them).
    thread_ids = list(dict.fromkeys(r.thread_id for r in rounds))
    threads = {t.id: t for t in db.query(ThreadV2).filter(ThreadV2.id.in_(thread_ids)).all()}
    thread_rows = {
        row.id: row
        for row in _to_moderation_rows(db, [threads[i] for i in thread_ids if i in threads])
    }

    items: list[ThreadHistoryRow] = []
    for round_row, size in zip(rounds, sizes):
        thread_row = thread_rows.get(round_row.thread_id)
        if thread_row is None:
            continue
        items.append(
            ThreadHistoryRow(
                **thread_row.model_dump(),
                decision=ThreadHistoryDecision(
                    submission_id=round_row.id,
                    round=round_row.round,
                    status=round_row.status,
                    title=round_row.title,
                    reason=round_row.reason,
                    submitted_at=round_row.submitted_at,
                    reviewed_by_email=round_row.reviewed_by_email,
                    reviewed_by_name=round_row.reviewed_by_name,
                    reviewed_at=round_row.reviewed_at,
                ),
                group_size=size,
            )
        )
    return ThreadHistoryListResponse(items=items, next_cursor=next_cursor)


def thread_history_facets(
    db: Session, *, access: ViewerAccess | None = None
) -> ThreadHistoryFacets:
    """`GET /threads/moderation/history/facets` — the History tab's two
    person-filter option lists (owner, 2026-09-24), `revision_history_facets`'
    shape: the distinct submitters and the distinct deciders of every
    DECIDED ROUND (`thread_submissions`, plan_thread_reject_resubmit §4.6 —
    no longer `threads.reviewed_*`, which a resubmission clears) in the
    caller's moderated set, so a dropdown only offers people the caller can
    actually find. A round's submitter is always the thread's author.
    Emails are stored normalized, so grouping by email is grouping by
    person."""
    component_ids = _moderated_component_ids(db, access)
    if not component_ids:
        return ThreadHistoryFacets()

    def _people(email_col, name_col) -> list[RevisionPerson]:
        rows = (
            db.query(email_col, func.max(name_col))
            .join(ThreadV2, ThreadV2.id == ThreadSubmissionV2.thread_id)
            .filter(
                ThreadV2.component_id.in_(component_ids),
                email_col.isnot(None),
            )
            .group_by(email_col)
            .all()
        )
        people = [RevisionPerson(email=email, name=name) for email, name in rows]
        people.sort(key=lambda p: ((p.name or p.email).casefold(), p.email))
        return people

    return ThreadHistoryFacets(
        authors=_people(ThreadSubmissionV2.submitted_by_email, ThreadSubmissionV2.submitted_by_name),
        reviewers=_people(ThreadSubmissionV2.reviewed_by_email, ThreadSubmissionV2.reviewed_by_name),
    )


def _view_as_etag_suffix(view_as: bool, view_as_user: str | None) -> str:
    """`-va` for a View As body, plus a short hash of the selected user's
    email so two View As bodies that differ only by `view_as_user` never
    share a tag (the email itself stays out of the header)."""
    if not view_as:
        return ""
    if not view_as_user:
        return "-va"
    return "-va-" + hashlib.sha256(view_as_user.encode()).hexdigest()[:12]


def moderation_summary(
    db: Session,
    *,
    access: ViewerAccess | None = None,
    view_as: bool = False,
    view_as_user: str | None = None,
) -> tuple[ThreadModerationSummary, str]:
    """`GET /threads/moderation/summary` (plan §5.7) — one cheap query on
    top of the `ViewerAccess` the dependency already built; polled like the
    notification bell. The ETag covers every field the body carries —
    `"{1 if can_moderate else 0}-{len(component_ids)}-{count}-{max_id}"` —
    so a viewer and an editor with zero pending threads never collide on
    the same tag (followups plan §1: a shared `"0-0"` used to let the
    browser's own HTTP cache revalidate one user's request against
    another's cached body). A Hub Admin with zero thread widgets still gets
    `can_moderate=True` — the section is part of their admin surface
    regardless of whether any widget currently has a thread on it.

    `view_as=True` means `access` was built from the View As parameters
    (`dependencies.get_view_as_access`): its only direct grants are the
    caller's own, with pairs only and without `anonymous`. `view_as` and
    `view_as_user` are echoed in the body and suffix the ETag
    (`_view_as_etag_suffix`), keeping the tag covering every body field."""
    component_ids = _moderated_component_ids(db, access)
    can_moderate = (access is not None and access.full_access) or bool(component_ids)
    can_moderate_flag = 1 if can_moderate else 0
    etag_suffix = _view_as_etag_suffix(view_as, view_as_user)

    if not component_ids:
        return (
            ThreadModerationSummary(
                can_moderate=can_moderate, moderated_component_count=0, pending_count=0,
                view_as=view_as, view_as_user=view_as_user,
            ),
            f'"{can_moderate_flag}-0-0-0{etag_suffix}"',
        )

    count, max_id = (
        db.query(func.count(ThreadV2.id), func.max(ThreadV2.id))
        .filter(ThreadV2.component_id.in_(component_ids), ThreadV2.status == THREAD_STATUS_PENDING)
        .one()
    )
    count = int(count or 0)
    etag = (
        f'"{can_moderate_flag}-{len(component_ids)}-{count}-'
        f'{int(max_id) if max_id is not None else 0}{etag_suffix}"'
    )
    return (
        ThreadModerationSummary(
            can_moderate=can_moderate,
            moderated_component_count=len(component_ids),
            pending_count=count,
            view_as=view_as,
            view_as_user=view_as_user,
        ),
        etag,
    )


# ---------------------------------------------------------
# Revision Management (plan_thread_edit_versioning amendment A4,
# 2026-09-23) — its OWN section, separate from Threads Management above
# (which lists only NEW threads and is unchanged by versioning). Pending tab
# newest-first; History tab = every non-pending revision (decided author
# edits, overwritten ones, AND auto-approved originals/editor edits), newest
# decision first, filterable by status, origin, decision date, submitter and
# reviewer. Scoped to `_moderated_component_ids` exactly like the thread
# queue; the revision rows reach it through a PK join to `threads`.
# ---------------------------------------------------------


def _moderated_revisions_query(db: Session, component_ids: list[int]):
    return (
        db.query(ThreadRevisionV2)
        .join(ThreadV2, ThreadV2.id == ThreadRevisionV2.thread_id)
        .filter(ThreadV2.component_id.in_(component_ids))
    )


def _to_revision_moderation_rows(
    db: Session, revisions: list[ThreadRevisionV2]
) -> list[ThreadRevisionModerationRow]:
    """One page of revisions (already ordered) -> rows, with the thread's
    live title/version and `widget_title`/`location_label` computed once
    per distinct component, and `earlier_pending_count` from ONE query over
    the page's pending rows' threads — never once per row."""
    if not revisions:
        return []

    threads = {
        t.id: t
        for t in db.query(ThreadV2)
        .filter(ThreadV2.id.in_({r.thread_id for r in revisions}))
        .all()
    }
    components = {
        c.id: c
        for c in db.query(ComponentV2)
        .filter(ComponentV2.id.in_({t.component_id for t in threads.values()}))
        .all()
    }
    info_by_component = _widget_title_and_location_for_components(db, components)

    pending_thread_ids = {r.thread_id for r in revisions if r.status == REVISION_STATUS_PENDING}
    pending_versions: dict[int, list[int]] = {}
    if pending_thread_ids:
        for thread_id, version in (
            db.query(ThreadRevisionV2.thread_id, ThreadRevisionV2.version)
            .filter(
                ThreadRevisionV2.thread_id.in_(pending_thread_ids),
                ThreadRevisionV2.status == REVISION_STATUS_PENDING,
            )
            .all()
        ):
            pending_versions.setdefault(thread_id, []).append(version)

    items: list[ThreadRevisionModerationRow] = []
    for revision in revisions:
        thread = threads.get(revision.thread_id)
        component = components.get(thread.component_id) if thread is not None else None
        if component is None:
            continue  # FK cascade makes this unreachable; defensive only
        widget_title, location_label = info_by_component.get(component.id, ("Discussion", ""))
        items.append(
            ThreadRevisionModerationRow(
                **_to_revision_summary(revision).model_dump(),
                component_id=component.id,
                component_link=component.link,
                widget_title=widget_title,
                location_label=location_label,
                thread_title=thread.title,
                thread_version=thread.version,
                earlier_pending_count=(
                    sum(1 for v in pending_versions.get(revision.thread_id, []) if v < revision.version)
                    if revision.status == REVISION_STATUS_PENDING
                    else 0
                ),
            )
        )
    return items


def list_pending_revisions(
    db: Session,
    cursor: str | None,
    *,
    order: str = "newest",
    access: ViewerAccess | None = None,
) -> ThreadRevisionModerationListResponse:
    """`GET /threads/revision-moderation/pending` — every pending revision in
    the caller's moderated set, one row per revision (owner decision 5),
    NEWEST submission first by default (amendment A4 — unlike the thread
    queue's FIFO). An empty moderated set is a 200 with an empty page.
    plan_thread_review_merge B1: `order` = newest (default) | oldest, the
    cursor naming its order exactly like `list_pending_threads`."""
    if order not in PENDING_ORDERS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid order")
    component_ids = _moderated_component_ids(db, access)
    if not component_ids:
        return ThreadRevisionModerationListResponse(items=[], next_cursor=None)

    query = _moderated_revisions_query(db, component_ids).filter(
        ThreadRevisionV2.status == REVISION_STATUS_PENDING
    )
    page, next_cursor = _pending_page(
        query,
        ThreadRevisionV2.submitted_at,
        ThreadRevisionV2.id,
        order=order,
        default_order="newest",
        cursor=cursor,
    )
    return ThreadRevisionModerationListResponse(
        items=_to_revision_moderation_rows(db, page), next_cursor=next_cursor
    )


def _as_utc(value: datetime) -> datetime:
    """A filter bound as a UTC instant — a naive value is taken as UTC.
    Stored timestamps are UTC (`_utc_now`), and SQLite compares them as
    naive strings, so an offset-carrying bound must be converted, not just
    passed through."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def list_revision_history(
    db: Session,
    cursor: str | None,
    *,
    statuses: list[str] | None = None,
    origins: list[str] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    authors: list[str] | None = None,
    reviewers: list[str] | None = None,
    thread_ids: list[int] | None = None,
    q: str | None = None,
    group: str | None = None,
    order: str = "newest",
    access: ViewerAccess | None = None,
) -> ThreadRevisionModerationListResponse:
    """`GET /threads/revision-moderation/history` — every NON-pending
    revision in the moderated set: author edits an editor approved/rejected,
    `overwritten` ones, and the auto-approved `original` v1s and `editor`
    edits (owner, 2026-09-23: "everything published too"). Newest decision
    first (`reviewed_at DESC, id DESC` — overwritten rows share their
    displacing approval's instant, so `id` breaks the tie).

    Filters, all optional and combinable (owner, 2026-09-23):
    - `statuses` ⊆ approved/rejected/overwritten (default all three);
    - `origins` ⊆ original/author/editor (default all);
    - `date_from` (inclusive) / `date_to` (exclusive) on the DECISION date,
      `reviewed_at` — instants, so the client sends its own local-day bounds;
    - `authors` = submitter emails, `reviewers` = decider emails (any-of,
      case-insensitive);
    - `thread_ids` = one or more threads' own version logs (phase C: the
      review modal asks whether an earlier edit of THIS thread was rejected,
      plan §7.4). Narrows within the moderated set, never widens it — an
      unmoderated thread's id simply matches nothing;
    - `q` (2026-11-09) — substring of the revision's title, the thread's
      current title, or the widget title (`_history_search_predicate`).
    `group` = thread | widget keeps each group's rows together
    (`_keyset_page`) and sets `group_size`; `order` = newest | oldest.
    The cursor only encodes the position; the caller must resend the same
    filters, group and order with it."""
    statuses = list(statuses or REVISION_DECIDED_STATUSES)
    if any(value not in REVISION_DECIDED_STATUSES for value in statuses):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid status filter")
    if origins and any(value not in REVISION_ORIGINS for value in origins):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid origin filter")
    if group is not None and group not in HISTORY_GROUPS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid group")
    if order not in HISTORY_ORDERS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid order")

    component_ids = _moderated_component_ids(db, access)
    if not component_ids:
        return ThreadRevisionModerationListResponse(items=[], next_cursor=None)

    query = _moderated_revisions_query(db, component_ids).filter(
        ThreadRevisionV2.status.in_(statuses),
        ThreadRevisionV2.reviewed_at.isnot(None),
    )
    if origins:
        query = query.filter(ThreadRevisionV2.origin.in_(origins))
    if date_from is not None:
        query = query.filter(ThreadRevisionV2.reviewed_at >= _as_utc(date_from))
    if date_to is not None:
        query = query.filter(ThreadRevisionV2.reviewed_at < _as_utc(date_to))
    if authors:
        query = query.filter(
            ThreadRevisionV2.submitted_by_email.in_({_norm_email(e) for e in authors})
        )
    if reviewers:
        query = query.filter(
            ThreadRevisionV2.reviewed_by_email.in_({_norm_email(e) for e in reviewers})
        )
    if thread_ids:
        query = query.filter(ThreadRevisionV2.thread_id.in_(set(thread_ids)))
    search = _history_search_predicate(
        db, q or "", component_ids, [ThreadRevisionV2.title, ThreadV2.title]
    )
    if search is not None:
        query = query.filter(search)

    group_column = None
    if group == "thread":
        group_column = ThreadRevisionV2.thread_id
    elif group == "widget":
        group_column = ThreadV2.component_id

    page, sizes, next_cursor = _keyset_page(
        db,
        query,
        decided_at=ThreadRevisionV2.reviewed_at,
        row_id=ThreadRevisionV2.id,
        group_column=group_column,
        order=order,
        cursor=cursor,
    )
    items = _to_revision_moderation_rows(db, page)
    if group_column is not None:
        size_by_id = {r.id: s for r, s in zip(page, sizes)}
        for item in items:
            item.group_size = size_by_id.get(item.id)
    return ThreadRevisionModerationListResponse(items=items, next_cursor=next_cursor)


def revision_history_facets(
    db: Session, *, access: ViewerAccess | None = None
) -> ThreadRevisionHistoryFacets:
    """`GET /threads/revision-moderation/history/facets` — the History
    tab's two person-filter option lists (owner, 2026-09-23): the distinct
    submitters and the distinct deciders of every non-pending revision in
    the caller's moderated set, so a dropdown only offers people the caller
    can actually find. Emails are stored normalized, so grouping by email
    is grouping by person; the name shown is one of that person's stored
    display snapshots."""
    component_ids = _moderated_component_ids(db, access)
    if not component_ids:
        return ThreadRevisionHistoryFacets()

    decided = [ThreadRevisionV2.status.in_(REVISION_DECIDED_STATUSES)]

    def _people(email_col, name_col) -> list[RevisionPerson]:
        rows = (
            db.query(email_col, func.max(name_col))
            .select_from(ThreadRevisionV2)
            .join(ThreadV2, ThreadV2.id == ThreadRevisionV2.thread_id)
            .filter(ThreadV2.component_id.in_(component_ids), email_col.isnot(None), *decided)
            .group_by(email_col)
            .all()
        )
        people = [RevisionPerson(email=email, name=name) for email, name in rows]
        people.sort(key=lambda p: ((p.name or p.email).casefold(), p.email))
        return people

    return ThreadRevisionHistoryFacets(
        authors=_people(ThreadRevisionV2.submitted_by_email, ThreadRevisionV2.submitted_by_name),
        reviewers=_people(ThreadRevisionV2.reviewed_by_email, ThreadRevisionV2.reviewed_by_name),
    )


# ---------------------------------------------------------
# Thread Review (plan_thread_review_merge_2026-11-11.md §5 B2/B3) — ONE
# activity log over both decision tables. Source S = every stored
# `thread_submissions` round (`list_thread_history`'s rows); source R = every
# decided `thread_revisions` row (`list_revision_history`'s rows) MINUS the
# v1 `original` of a thread that has an approved round: approving a new
# thread writes both (`decide_thread` → `_write_original_revision`), and the
# round is the row ("New thread · Revision k → published as v1"). A v1 with
# no approved round (an editor's own post, a G3 self-publish) stays, as
# "Published directly". Read-only; the existing `reviewed_at, id` indexes
# cover both halves.
# ---------------------------------------------------------

ACTIVITY_KINDS = ("new", "resubmission", "edit", "editor_edit", "published_directly")
ACTIVITY_OUTCOMES = ("approved", "rejected", "overwritten", "published_directly")
ACTIVITY_OUTCOME_PUBLISHED_DIRECTLY = "published_directly"
ACTIVITY_SOURCE_SUBMISSION = 0
ACTIVITY_SOURCE_REVISION = 1
THREAD_TIMELINE_CAP = 200


def _sql_text(value: str):
    """A constant string in the union's select lists, rendered INLINE
    (`'edit'`) rather than as a bind parameter — Postgres cannot type an
    untyped parameter in a UNION's select list. Only ever called with this
    module's own constants."""
    return literal_column(f"'{value}'", String)


def _activity_union(db: Session, component_ids: Collection[int], *, thread_id: int | None = None):
    """The deduplicated `UNION ALL`, as a subquery with uniform labelled
    columns: `source_rank` (0 = submission, 1 = revision), `source_id`,
    `thread_id`, `component_id`, `kind`, `outcome`, `title` (decided
    snapshot), `thread_title` (now), `reason`, `submitted_by_*`,
    `decided_by_*`, `decided_at`. Callers filter, sort and page on it."""
    ids = list(component_ids)
    s_kind = case(
        (ThreadSubmissionV2.round == 0, _sql_text("new")), else_=_sql_text("resubmission")
    )
    submissions = (
        select(
            literal_column(str(ACTIVITY_SOURCE_SUBMISSION), Integer).label("source_rank"),
            ThreadSubmissionV2.id.label("source_id"),
            ThreadSubmissionV2.thread_id.label("thread_id"),
            ThreadV2.component_id.label("component_id"),
            s_kind.label("kind"),
            ThreadSubmissionV2.status.label("outcome"),
            ThreadSubmissionV2.title.label("title"),
            ThreadV2.title.label("thread_title"),
            ThreadSubmissionV2.reason.label("reason"),
            ThreadSubmissionV2.submitted_by_email.label("submitted_by_email"),
            ThreadSubmissionV2.submitted_by_name.label("submitted_by_name"),
            ThreadSubmissionV2.reviewed_by_email.label("decided_by_email"),
            ThreadSubmissionV2.reviewed_by_name.label("decided_by_name"),
            ThreadSubmissionV2.reviewed_at.label("decided_at"),
        )
        .join(ThreadV2, ThreadV2.id == ThreadSubmissionV2.thread_id)
        .where(ThreadV2.component_id.in_(ids))
    )

    approved_round = (
        select(ThreadSubmissionV2.id)
        .where(
            ThreadSubmissionV2.thread_id == ThreadRevisionV2.thread_id,
            ThreadSubmissionV2.status == THREAD_STATUS_APPROVED,
        )
        .exists()
    )
    r_kind = case(
        (ThreadRevisionV2.origin == REVISION_ORIGIN_AUTHOR, _sql_text("edit")),
        (ThreadRevisionV2.origin == REVISION_ORIGIN_EDITOR, _sql_text("editor_edit")),
        else_=_sql_text("published_directly"),
    )
    r_outcome = case(
        (ThreadRevisionV2.origin == REVISION_ORIGIN_AUTHOR, ThreadRevisionV2.status),
        else_=_sql_text(ACTIVITY_OUTCOME_PUBLISHED_DIRECTLY),
    )
    revisions = (
        select(
            literal_column(str(ACTIVITY_SOURCE_REVISION), Integer).label("source_rank"),
            ThreadRevisionV2.id.label("source_id"),
            ThreadRevisionV2.thread_id.label("thread_id"),
            ThreadV2.component_id.label("component_id"),
            r_kind.label("kind"),
            r_outcome.label("outcome"),
            ThreadRevisionV2.title.label("title"),
            ThreadV2.title.label("thread_title"),
            ThreadRevisionV2.rejection_reason.label("reason"),
            ThreadRevisionV2.submitted_by_email.label("submitted_by_email"),
            ThreadRevisionV2.submitted_by_name.label("submitted_by_name"),
            ThreadRevisionV2.reviewed_by_email.label("decided_by_email"),
            ThreadRevisionV2.reviewed_by_name.label("decided_by_name"),
            ThreadRevisionV2.reviewed_at.label("decided_at"),
        )
        .join(ThreadV2, ThreadV2.id == ThreadRevisionV2.thread_id)
        .where(
            ThreadV2.component_id.in_(ids),
            ThreadRevisionV2.status.in_(REVISION_DECIDED_STATUSES),
            ThreadRevisionV2.reviewed_at.isnot(None),
            # THE dedupe rule (plan B2).
            not_(and_(ThreadRevisionV2.origin == REVISION_ORIGIN_ORIGINAL, approved_round)),
        )
    )
    if thread_id is not None:
        submissions = submissions.where(ThreadSubmissionV2.thread_id == thread_id)
        revisions = revisions.where(ThreadRevisionV2.thread_id == thread_id)
    return union_all(submissions, revisions).subquery("thread_activity")


def _activity_filters(
    db: Session,
    union,
    component_ids: list[int],
    *,
    kinds: list[str] | None,
    outcomes: list[str] | None,
    widgets: list[str] | None,
    date_from: datetime | None,
    date_to: datetime | None,
    authors: list[str] | None,
    reviewers: list[str] | None,
    q: str | None,
) -> list[Any]:
    """The WHERE list for the activity union — every filter on the ROW."""
    where: list[Any] = []
    if kinds:
        where.append(union.c.kind.in_(list(dict.fromkeys(kinds))))
    if outcomes:
        where.append(union.c.outcome.in_(list(dict.fromkeys(outcomes))))
    if widgets:
        # Narrows within the moderated set, never widens it: an unknown or
        # unmoderated link matches nothing (the `thread_ids` rule).
        widget_ids = [
            row[0]
            for row in db.query(ComponentV2.id).filter(
                ComponentV2.link.in_({w.strip() for w in widgets}),
                ComponentV2.id.in_(component_ids),
            )
        ]
        where.append(union.c.component_id.in_(widget_ids) if widget_ids else false())
    if date_from is not None:
        where.append(union.c.decided_at >= _as_utc(date_from))
    if date_to is not None:
        where.append(union.c.decided_at < _as_utc(date_to))
    if authors:
        where.append(union.c.submitted_by_email.in_({_norm_email(e) for e in authors}))
    if reviewers:
        where.append(union.c.decided_by_email.in_({_norm_email(e) for e in reviewers}))
    search = _history_search_predicate(
        db,
        q or "",
        component_ids,
        [union.c.title, union.c.thread_title, union.c.reason],
        component_column=union.c.component_id,
    )
    if search is not None:
        where.append(search)
    return where


def _encode_activity_cursor(mode: str, order: str, values: list[Any]) -> str:
    return _encode_keyset([mode, order, *values])


def _decode_activity_cursor(cursor: str, mode: str, order: str) -> tuple[Any, ...]:
    """`[mode, order, ...key]` — flat key `(decided_at, source_rank,
    source_id)`, grouped `(g_at, gk, decided_at, source_rank, source_id)`.
    A cursor from another mode (flat / thread / widget) or order, or junk,
    is a 400: the caller must resend the filters, group and order it was
    issued under."""
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        cursor_mode, cursor_order, *key = raw
        if mode == "flat":
            at, rank, row_id = key
            values: tuple[Any, ...] = (_as_utc(datetime.fromisoformat(at)), int(rank), int(row_id))
        else:
            g_at, gk, at, rank, row_id = key
            values = (
                _as_utc(datetime.fromisoformat(g_at)),
                int(gk),
                _as_utc(datetime.fromisoformat(at)),
                int(rank),
                int(row_id),
            )
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid cursor") from exc
    if (cursor_mode, cursor_order) != (mode, order):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid cursor")
    return values


def _activity_page(
    db: Session,
    union,
    where: list[Any],
    *,
    group: str | None,
    order: str,
    cursor: str | None,
):
    """`_keyset_page`'s twin for the union (a Core subquery, not an ORM
    entity). Sort `(decided_at, source_rank, source_id)` newest or oldest
    first — `source_rank` keeps the key unique when a round and a revision
    share an instant AND an id. Grouped, each group's rows come out
    together: groups by their newest (oldest-first: oldest) decision under
    the SAME filters. Returns `(records, group_sizes, next_cursor)`."""
    newest = order != "oldest"
    mode = group or "flat"
    row_keys = (union.c.decided_at, union.c.source_rank, union.c.source_id)
    if group is not None:
        group_column = union.c.thread_id if group == "thread" else union.c.component_id
        agg = func.max(union.c.decided_at) if newest else func.min(union.c.decided_at)
        groups = (
            select(
                group_column.label("gk"), agg.label("g_at"), func.count().label("g_n")
            )
            .where(*where)
            .group_by(group_column)
            .subquery("thread_activity_groups")
        )
        stmt = select(union, groups.c.g_at, groups.c.gk, groups.c.g_n).select_from(
            union.join(groups, groups.c.gk == group_column)
        )
        keys = (groups.c.g_at, groups.c.gk, *row_keys)
    else:
        stmt = select(union)
        keys = row_keys
    stmt = stmt.where(*where)
    if cursor:
        after = _decode_activity_cursor(cursor, mode, order)
        stmt = stmt.where(tuple_(*keys) < after if newest else tuple_(*keys) > after)
    stmt = stmt.order_by(*[k.desc() if newest else k.asc() for k in keys])
    fetched = db.execute(stmt.limit(THREADS_PAGE_SIZE + 1)).all()
    has_more = len(fetched) > THREADS_PAGE_SIZE
    fetched = fetched[:THREADS_PAGE_SIZE]

    sizes = [int(r.g_n) for r in fetched] if group is not None else [None] * len(fetched)
    next_cursor = None
    if has_more and fetched:
        last = fetched[-1]
        values = [last.decided_at, int(last.source_rank), int(last.source_id)]
        if group is not None:
            values = [last.g_at, int(last.gk), *values]
        next_cursor = _encode_activity_cursor(mode, order, values)
    return fetched, sizes, next_cursor


def _can_open_in_hub(thread: ThreadV2, outcome: str) -> bool:
    """plan B2 (fixes M6): only a row whose event put text live, on a
    thread that is live now."""
    return thread.status == THREAD_STATUS_APPROVED and outcome in (
        REVISION_STATUS_APPROVED,
        ACTIVITY_OUTCOME_PUBLISHED_DIRECTLY,
    )


def _activity_context(db: Session, thread_ids: Collection[int]):
    """Threads, their components and widget labels for a set of activity
    rows — one batch each (`_widget_title_and_location_for_components`)."""
    threads = (
        {t.id: t for t in db.query(ThreadV2).filter(ThreadV2.id.in_(set(thread_ids)))}
        if thread_ids
        else {}
    )
    component_ids = {t.component_id for t in threads.values()}
    components = (
        {c.id: c for c in db.query(ComponentV2).filter(ComponentV2.id.in_(component_ids))}
        if component_ids
        else {}
    )
    info = _widget_title_and_location_for_components(db, components)
    return threads, components, info


def _activity_base(thread: ThreadV2, component: ComponentV2, info: dict[int, tuple[str, str]]) -> dict[str, Any]:
    widget_title, location_label = info.get(component.id, ("Discussion", ""))
    return {
        "thread_id": thread.id,
        "thread_title": thread.title,
        "thread_status": thread.status,
        "thread_version": thread.version or 1,
        "component_id": component.id,
        "component_link": component.link,
        "widget_title": widget_title,
        "location_label": location_label,
    }


def _submission_activity_row(
    sub: ThreadSubmissionV2, thread: ThreadV2, base: dict[str, Any], *, kind: str, outcome: str,
    carries_close: bool, group_size: int | None = None,
) -> ThreadActivityRow:
    return ThreadActivityRow(
        **base,
        key=f"s{sub.id}",
        source="submission",
        source_id=sub.id,
        kind=kind,
        outcome=outcome,
        round=sub.round,
        title=sub.title,
        submitted_by_email=sub.submitted_by_email,
        submitted_by_name=sub.submitted_by_name,
        submitted_at=sub.submitted_at,
        decided_by_email=sub.reviewed_by_email,
        decided_by_name=sub.reviewed_by_name,
        decided_at=sub.reviewed_at,
        reason=sub.reason,
        closed_at=thread.closed_at if carries_close else None,
        closed_kind=thread.closed_kind if carries_close else None,
        published_as_v1=outcome == THREAD_STATUS_APPROVED,
        can_open_in_hub=_can_open_in_hub(thread, outcome),
        group_size=group_size,
    )


def _revision_activity_row(
    rev: ThreadRevisionV2, thread: ThreadV2, base: dict[str, Any], *, kind: str, outcome: str,
    group_size: int | None = None,
) -> ThreadActivityRow:
    pending = outcome == REVISION_STATUS_PENDING
    return ThreadActivityRow(
        **base,
        key=f"r{rev.id}",
        source="revision",
        source_id=rev.id,
        kind=kind,
        outcome=outcome,
        version=rev.version,
        resubmission_number=rev.resubmission_number or 0,
        resubmission_of_version=_resubmission_of_version(rev),
        resubmission_root_version=_resubmission_root_version(rev),
        title=rev.title,
        submitted_by_email=rev.submitted_by_email,
        submitted_by_name=rev.submitted_by_name,
        submitted_at=rev.submitted_at,
        decided_by_email=rev.reviewed_by_email,
        decided_by_name=rev.reviewed_by_name,
        decided_at=rev.reviewed_at,
        reason=rev.rejection_reason,
        closed_at=rev.closed_at,
        closed_kind=rev.closed_kind,
        closed_by_version=rev.closed_by_version,
        can_open_in_hub=_can_open_in_hub(thread, outcome),
        group_size=group_size,
        edit_count=(rev.edit_count or 0) if pending else None,
        updated_at=rev.updated_at if pending else None,
    )


def _hydrate_activity(db: Session, records: list[Any], sizes: list[int | None]) -> list[ThreadActivityRow]:
    """Union records (one page, already ordered) -> `ThreadActivityRow`s.
    `kind` / `outcome` come from the union itself, so the SQL and the row
    can never disagree. Batched: one read per table plus the widget labels.
    A submission row carries the thread's Close only on the thread's LAST
    declined round (the round the author closed)."""
    if not records:
        return []
    s_ids = [r.source_id for r in records if r.source_rank == ACTIVITY_SOURCE_SUBMISSION]
    r_ids = [r.source_id for r in records if r.source_rank == ACTIVITY_SOURCE_REVISION]
    subs = (
        {s.id: s for s in db.query(ThreadSubmissionV2).filter(ThreadSubmissionV2.id.in_(s_ids))}
        if s_ids
        else {}
    )
    revs = (
        {r.id: r for r in db.query(ThreadRevisionV2).filter(ThreadRevisionV2.id.in_(r_ids))}
        if r_ids
        else {}
    )
    threads, components, info = _activity_context(db, {r.thread_id for r in records})

    closed_ids = [t.id for t in threads.values() if t.closed_at is not None]
    last_declined: dict[int, int] = {}
    if closed_ids and s_ids:
        last_declined = {
            thread_id: max_round
            for thread_id, max_round in db.query(
                ThreadSubmissionV2.thread_id, func.max(ThreadSubmissionV2.round)
            )
            .filter(
                ThreadSubmissionV2.thread_id.in_(closed_ids),
                ThreadSubmissionV2.status == THREAD_STATUS_REJECTED,
            )
            .group_by(ThreadSubmissionV2.thread_id)
        }

    items: list[ThreadActivityRow] = []
    for record, size in zip(records, sizes):
        thread = threads.get(record.thread_id)
        component = components.get(thread.component_id) if thread is not None else None
        if component is None:
            continue  # FK cascade makes this unreachable; defensive only
        base = _activity_base(thread, component, info)
        if record.source_rank == ACTIVITY_SOURCE_SUBMISSION:
            sub = subs.get(record.source_id)
            if sub is None:
                continue
            items.append(
                _submission_activity_row(
                    sub, thread, base, kind=record.kind, outcome=record.outcome,
                    carries_close=(
                        sub.status == THREAD_STATUS_REJECTED
                        and last_declined.get(thread.id) == sub.round
                    ),
                    group_size=size,
                )
            )
        else:
            rev = revs.get(record.source_id)
            if rev is None:
                continue
            items.append(
                _revision_activity_row(
                    rev, thread, base, kind=record.kind, outcome=record.outcome, group_size=size
                )
            )
    return items


def list_thread_activity(
    db: Session,
    cursor: str | None,
    *,
    kinds: list[str] | None = None,
    outcomes: list[str] | None = None,
    widgets: list[str] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    authors: list[str] | None = None,
    reviewers: list[str] | None = None,
    q: str | None = None,
    group: str | None = None,
    order: str = "newest",
    access: ViewerAccess | None = None,
) -> ThreadActivityListResponse:
    """`GET /threads/moderation/activity` (plan B2) — Thread Review's
    History: one row per decision from BOTH tables, deduplicated
    (`_activity_union`), over the caller's moderated set — the same
    audience and gate as `list_thread_history` / `list_revision_history`.

    Filters, all optional and combinable, each on the ROW: `kinds`,
    `outcomes`, `widgets` (component links), `date_from` (inclusive) /
    `date_to` (exclusive) on the decision instant, `authors` / `reviewers`
    (any-of, case-insensitive), `q` — substring of the decided title, the
    current title, the widget title OR the reason. `group` = thread |
    widget, `order` = newest | oldest. The cursor names its mode and order;
    resend the same filters with it."""
    if kinds and any(k not in ACTIVITY_KINDS for k in kinds):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid kind filter")
    if outcomes and any(o not in ACTIVITY_OUTCOMES for o in outcomes):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid outcome filter")
    if group is not None and group not in HISTORY_GROUPS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid group")
    if order not in HISTORY_ORDERS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid order")

    component_ids = _moderated_component_ids(db, access)
    if not component_ids:
        return ThreadActivityListResponse(items=[], next_cursor=None)

    union = _activity_union(db, component_ids)
    where = _activity_filters(
        db, union, component_ids, kinds=kinds, outcomes=outcomes, widgets=widgets,
        date_from=date_from, date_to=date_to, authors=authors, reviewers=reviewers, q=q,
    )
    records, sizes, next_cursor = _activity_page(
        db, union, where, group=group, order=order, cursor=cursor
    )
    return ThreadActivityListResponse(
        items=_hydrate_activity(db, records, sizes), next_cursor=next_cursor
    )


def thread_activity_facets(
    db: Session, *, access: ViewerAccess | None = None
) -> ThreadActivityFacets:
    """`GET /threads/moderation/activity/facets` — the distinct authors,
    deciders and widgets of the DEDUPLICATED activity union in the caller's
    moderated set (a kept v1's publisher is a decider; a dropped v1's is
    not, unless they decided something else). People sorted by name then
    email; widgets by title, location, link."""
    component_ids = _moderated_component_ids(db, access)
    if not component_ids:
        return ThreadActivityFacets()
    union = _activity_union(db, component_ids)

    def _people(email_col, name_col) -> list[RevisionPerson]:
        rows = db.execute(
            select(email_col, func.max(name_col)).where(email_col.isnot(None)).group_by(email_col)
        ).all()
        people = [RevisionPerson(email=email, name=name) for email, name in rows]
        people.sort(key=lambda p: ((p.name or p.email).casefold(), p.email))
        return people

    used = [row[0] for row in db.execute(select(union.c.component_id).distinct()).all()]
    components = (
        {c.id: c for c in db.query(ComponentV2).filter(ComponentV2.id.in_(used))} if used else {}
    )
    info = _widget_title_and_location_for_components(db, components)
    widgets = [
        ThreadActivityWidget(
            component_link=c.link,
            widget_title=info.get(c.id, ("Discussion", ""))[0],
            location_label=info.get(c.id, ("Discussion", ""))[1],
        )
        for c in components.values()
    ]
    widgets.sort(key=lambda w: (w.widget_title.casefold(), w.location_label.casefold(), w.component_link))
    return ThreadActivityFacets(
        authors=_people(union.c.submitted_by_email, union.c.submitted_by_name),
        reviewers=_people(union.c.decided_by_email, union.c.decided_by_name),
        widgets=widgets,
    )


def get_thread_timeline(
    db: Session, thread_id: int, *, access: ViewerAccess | None = None
) -> ThreadTimelineResponse:
    """`GET /threads/{id}/timeline` (plan B3) — the reviewer drawer's
    lifecycle: every activity row of ONE thread (the same deduplicated
    union), oldest first, then its pending entries — the pending round
    (synthesized, `outcome='pending'`, `edit_count` = the thread's, and
    `updated_at` = `edited_at` when the pending text was edited after it was
    submitted) and every pending author edit by version (`edit_count`,
    `updated_at`). No bodies: the drawer reads text through
    `submission-history` / `revisions/{rid}`.

    Unpaged, capped at `THREAD_TIMELINE_CAP` decided rows — the NEWEST
    ones, with `truncated`. Gate: 404 (no thread) → 403 (no view) → 404
    unless the caller is an editor of the thread's widget — the reviewer's
    drawer; the author-side timeline is out of scope."""
    thread, component = _require_thread_and_component(db, thread_id)
    _check_view_access(component, access)
    if not _is_component_editor(component, access):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found")

    union = _activity_union(db, [component.id], thread_id=thread.id)
    keys = (union.c.decided_at, union.c.source_rank, union.c.source_id)
    records = db.execute(
        select(union).order_by(*[k.desc() for k in keys]).limit(THREAD_TIMELINE_CAP + 1)
    ).all()
    truncated = len(records) > THREAD_TIMELINE_CAP
    records = list(reversed(records[:THREAD_TIMELINE_CAP]))
    items = _hydrate_activity(db, records, [None] * len(records))

    info = _widget_title_and_location_for_components(db, {component.id: component})
    base = _activity_base(thread, component, info)
    if thread.status == THREAD_STATUS_PENDING:
        submitted_at = thread.submitted_at or thread.created_at
        edited_after = (
            thread.edited_at is not None
            and submitted_at is not None
            and _as_utc(thread.edited_at) > _as_utc(submitted_at)
        )
        count = thread.resubmission_count or 0
        items.append(
            ThreadActivityRow(
                **base,
                key=f"sp{thread.id}",
                source="submission",
                source_id=None,
                kind="resubmission" if count > 0 else "new",
                outcome="pending",
                round=count,
                title=thread.title,
                submitted_by_email=_norm_email(thread.author_email),
                submitted_by_name=thread.author_name,
                submitted_at=submitted_at,
                edit_count=thread.edit_count or 0,
                updated_at=thread.edited_at if edited_after else None,
            )
        )
    for rev in (
        db.query(ThreadRevisionV2)
        .filter(
            ThreadRevisionV2.thread_id == thread.id,
            ThreadRevisionV2.status == REVISION_STATUS_PENDING,
        )
        .order_by(ThreadRevisionV2.version.asc())
    ):
        items.append(_revision_activity_row(rev, thread, base, kind="edit", outcome="pending"))
    return ThreadTimelineResponse(thread_id=thread.id, items=items, truncated=truncated)


def revision_moderation_summary(
    db: Session,
    *,
    access: ViewerAccess | None = None,
    view_as: bool = False,
    view_as_user: str | None = None,
) -> tuple[ThreadRevisionModerationSummary, str]:
    """`GET /threads/revision-moderation/summary` — Revision Management's
    own sidebar badge (amendment A4), the same shape and ETag discipline as
    `moderation_summary`: the tag covers every field the body carries —
    `"{1 if can_moderate else 0}-{n_components}-{pending}-{max_pending_id}"`
    — so two callers with different bodies never share a tag (followups
    §1). Counts pending REVISIONS only; new threads are Threads
    Management's. `view_as` behaves exactly as in `moderation_summary`."""
    component_ids = _moderated_component_ids(db, access)
    can_moderate = (access is not None and access.full_access) or bool(component_ids)
    can_moderate_flag = 1 if can_moderate else 0
    etag_suffix = _view_as_etag_suffix(view_as, view_as_user)

    if not component_ids:
        return (
            ThreadRevisionModerationSummary(
                can_moderate=can_moderate, moderated_component_count=0, pending_count=0,
                view_as=view_as, view_as_user=view_as_user,
            ),
            f'"{can_moderate_flag}-0-0-0{etag_suffix}"',
        )

    count, max_id = (
        db.query(func.count(ThreadRevisionV2.id), func.max(ThreadRevisionV2.id))
        .join(ThreadV2, ThreadV2.id == ThreadRevisionV2.thread_id)
        .filter(
            ThreadV2.component_id.in_(component_ids),
            ThreadRevisionV2.status == REVISION_STATUS_PENDING,
        )
        .one()
    )
    count = int(count or 0)
    etag = (
        f'"{can_moderate_flag}-{len(component_ids)}-{count}-'
        f'{int(max_id) if max_id is not None else 0}{etag_suffix}"'
    )
    return (
        ThreadRevisionModerationSummary(
            can_moderate=can_moderate,
            moderated_component_count=len(component_ids),
            pending_count=count,
            view_as=view_as,
            view_as_user=view_as_user,
        ),
        etag,
    )


def thread_widget_counts(
    db: Session, link: str, *, access: ViewerAccess | None = None
) -> ThreadWidgetCounts:
    """`GET /threads/component/{link}/counts` (followups plan §4.2) — the
    widget-removal confirmation's true-totals check. Gated `resolve` (404)
    → `_check_view_access` (403) → editor-only (403), same order as every
    other gated read in this module. Deliberately NOT `_require_component_
    editor`: that helper's message ("...can approve or reject threads") is
    about a different action, and would be a wrong description of a 403 on
    a read-only counts request — the check (`_is_component_editor`) is
    reused, the wording is not.

    ONE query, across ALL statuses — unlike every list endpoint here, which
    scopes to `approved OR (own AND pending/rejected)` (2a's deliberate
    widget/moderation authority split). The gate is what makes that safe:
    only a component editor (or an ancestor tab/nav-tab editor, via
    `_is_component_editor`'s `verdict(...).edit` fold) can see the true
    total, including other authors' pending/rejected rows."""
    component = resolve_thread_widget_component(db, link)
    _check_view_access(component, access)
    if not _is_component_editor(component, access):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only an editor of this discussion can view its thread counts",
        )

    thread_count, comment_total, pending_total = (
        db.query(
            func.count(ThreadV2.id),
            func.coalesce(func.sum(ThreadV2.comment_count), 0),
            func.coalesce(func.sum(case((ThreadV2.status == THREAD_STATUS_PENDING, 1), else_=0)), 0),
        )
        .filter(ThreadV2.component_id == component.id)
        .one()
    )
    return ThreadWidgetCounts(
        thread_count=int(thread_count or 0),
        comment_count=int(comment_total or 0),
        pending_count=int(pending_total or 0),
    )


# ---------------------------------------------------------
# Comments
# ---------------------------------------------------------


def list_comments_for_thread(
    db: Session,
    thread_id: int,
    cursor: str | None,
    user: UserInfo | None,
    *,
    access: ViewerAccess | None = None,
) -> CommentListResponse:
    """`user` is whose `my_vote` is reported — see `get_thread_by_id`;
    `None` reports 0 on every comment while the counts stay exact."""
    thread, component = _require_thread_and_component(db, thread_id)
    _check_view_access(component, access)
    _require_approved(thread)

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

    my_votes = _my_votes_for_comments(db, [c.id for c in page], user.email) if user is not None else {}
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
    *,
    access: ViewerAccess | None = None,
) -> CommentSummary:
    thread, component = _require_thread_and_component(db, thread_id)
    _check_view_access(component, access)
    _require_approved(thread)

    # Resolve BEFORE the lock/recompute below — a 400 from the cap must not
    # leave a half-built comment or a bumped comment_count behind.
    resolved = _validate_and_resolve_mentions(db, mentions or [], content, component=component)

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
    *,
    access: ViewerAccess | None = None,
) -> CommentSummary:
    comment, thread, component = _require_comment_thread_and_component(db, comment_id)
    _check_view_access(component, access)
    _require_approved(thread)
    _require_author(comment.author_email, user)

    # Same effective-content reasoning as update_thread_by_id.
    if mentions is not None:
        effective_content = content if content is not None else comment.content
        previous_mentions = comment.mentions
        resolved = _validate_and_resolve_mentions(
            db, mentions, effective_content, component=component
        )
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


def delete_comment_by_id(
    db: Session, comment_id: int, *, access: ViewerAccess | None = None
) -> None:
    _require_hub_admin(access)
    comment = _get_comment(db, comment_id)
    if comment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Comment not found")
    thread_id = comment.thread_id

    thread = _lock_thread(db, thread_id)
    # plan §4.1's table lists this route explicitly, though it's unreachable
    # in practice: a comment can only ever exist on a thread that was
    # approved at the time it was created (`_require_approved` on create).
    # Cheap, and the table says so.
    if thread is not None:
        _require_approved(thread)

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
    # (gridstack_service) and sidesteps
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


def vote_on_thread(
    db: Session,
    thread_id: int,
    user: UserInfo,
    value: int,
    *,
    access: ViewerAccess | None = None,
) -> VoteResponse:
    thread, component = _require_thread_and_component(db, thread_id)
    _check_view_access(component, access)
    _require_approved(thread)

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
    db: Session,
    comment_id: int,
    user: UserInfo,
    value: int,
    *,
    access: ViewerAccess | None = None,
) -> VoteResponse:
    comment, thread, component = _require_comment_thread_and_component(db, comment_id)
    _check_view_access(component, access)
    _require_approved(thread)

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
    per table, for the script's report.

    plan_thread_reject_resubmit §4.9 (owner, 2026-11-07: "Also fix
    thread_revisions"): also rewrites `thread_revisions.submitted_by_email`
    / `reviewed_by_email` (a pre-existing gap since versioning) and
    `thread_submissions.submitted_by_email` / `reviewed_by_email`, and
    scans both tables' `mentions` snapshots. A row counts once per table
    however many of its columns matched. `threads.reviewed_by_email` is
    still not rewritten (pre-existing, out of this plan's scope)."""
    old_norm = _norm_email(old_email)
    new_norm = _norm_email(new_email)
    counts = {
        "threads": 0,
        "thread_comments": 0,
        "notifications": 0,
        "thread_revisions": 0,
        "thread_submissions": 0,
        "mentions_rewritten": 0,
    }

    threads = (
        db.query(ThreadV2).filter(ThreadV2.author_email == old_norm).all()
    )
    for thread in threads:
        thread.author_email = new_norm
        counts["threads"] += 1

    for model, key in ((ThreadRevisionV2, "thread_revisions"), (ThreadSubmissionV2, "thread_submissions")):
        rows = (
            db.query(model)
            .filter(or_(model.submitted_by_email == old_norm, model.reviewed_by_email == old_norm))
            .all()
        )
        for row in rows:
            if row.submitted_by_email == old_norm:
                row.submitted_by_email = new_norm
            if row.reviewed_by_email == old_norm:
                row.reviewed_by_email = new_norm
            counts[key] += 1

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
    for model in (ThreadV2, ThreadCommentV2, ThreadRevisionV2, ThreadSubmissionV2):
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
