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
from sqlalchemy import and_, case, func, or_, tuple_
from sqlalchemy.orm import Session

from app.db_v2.models.component import ComponentV2
from app.db_v2.models.notification import NotificationV2
from app.db_v2.models.page_content import PageContentV2
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
    ThreadModerationListResponse,
    ThreadModerationRow,
    ThreadModerationSummary,
    ThreadSummary,
    ThreadWidgetCounts,
    VoteResponse,
)
from app.services.access_visibility_service import (
    NodeRef,
    ViewerAccess,
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
# rejects it. Same `notifications` row shape for all four; `type` is a
# `String(32)` with no CHECK, so the new values needed no migration.
NOTIFICATION_TYPE_MENTION = "mention"
NOTIFICATION_TYPE_THREAD_COMMENT = "thread_comment"
NOTIFICATION_TYPE_THREAD_APPROVED = "thread_approved"
NOTIFICATION_TYPE_THREAD_REJECTED = "thread_rejected"

# Thread status values (D5, lifecycle in plan_thread_moderation_2026-09-18.md
# §2). An editor's own post, or a decided pending one, is 'approved'; a
# viewer's post lands 'pending' until an editor decides; 'rejected' is
# terminal (no resubmit-on-edit — a PATCH on a rejected thread is a 409).
THREAD_STATUS_APPROVED = "approved"
THREAD_STATUS_PENDING = "pending"
THREAD_STATUS_REJECTED = "rejected"


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


def _notify_thread_decision(
    db: Session,
    thread: ThreadV2,
    *,
    reviewer: UserInfo,
    approved: bool,
    component_id: int,
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

    Does not commit — the caller (`decide_thread`) owns the transaction,
    same convention as every other write function in this module."""
    recipient = _norm_email(thread.author_email)
    reviewer_email = _norm_email(reviewer.email)
    if recipient == reviewer_email:
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
            excerpt=generate_notification_excerpt(thread.content),
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
    thread or component (`ON DELETE CASCADE`, plan §3.4).

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
    next_cursor = (
        _encode_cursor(page[-1][0].created_at, page[-1][0].id)
        if has_more and page
        else None
    )

    if access is not None:
        page = [
            row for row in page if access.is_granted(("component", row[0].component_id))
        ]

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
        if access.is_granted(("component", component_id))
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
    if access is not None and not access.is_granted(("component", notification.component_id)):
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


def _to_thread_summary(thread: ThreadV2, my_vote: int) -> ThreadSummary:
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


# ---------------------------------------------------------
# Moderation page support — widget title / location label, batched per
# DISTINCT component (plan §5.6), and the ThreadModerationRow builders.
# ---------------------------------------------------------


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
        title: str | None = None
        if component.page_content_id is not None:
            content = preloaded_content.get(component.page_content_id)
            if isinstance(content, dict):
                raw_title = content.get("title")
                title = raw_title if isinstance(raw_title, str) and raw_title.strip() else None
        if not title:
            title = component.title or None
        if not title:
            title = "Discussion"

        location_refs = [ref for ref in chains[component_id] if ref[0] in ("tab", "nav_tab")]
        # `walk_ancestors` is nearest-first; a location label reads root-first.
        location_label = " › ".join(
            labels[ref] for ref in reversed(location_refs) if ref in labels
        )
        result[component_id] = (title, location_label)
    return result


def _to_moderation_rows(db: Session, threads: list[ThreadV2]) -> list[ThreadModerationRow]:
    """`ThreadV2` rows (already the caller's page, already ordered) ->
    `ThreadModerationRow`s, with `widget_title`/`location_label` computed
    once per distinct component (see above), not once per thread."""
    if not threads:
        return []

    component_ids = {t.component_id for t in threads}
    components = {
        c.id: c
        for c in db.query(ComponentV2).filter(ComponentV2.id.in_(component_ids)).all()
    }
    info_by_component = _widget_title_and_location_for_components(db, components)

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
    user: UserInfo,
    *,
    access: ViewerAccess | None = None,
) -> ThreadListResponse:
    component = _get_thread_widget_component(db, link)
    if component is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread widget not found")
    _check_view_access(component, access)

    # plan §5.5 — every approved thread, plus the CALLER'S OWN pending/
    # rejected ones (M3: an author sees their own non-approved threads with
    # a status badge; nobody else's pending/rejected rows appear in the
    # widget's list at all).
    query = db.query(ThreadV2).filter(
        ThreadV2.component_id == component.id,
        or_(
            ThreadV2.status == THREAD_STATUS_APPROVED,
            and_(
                ThreadV2.author_email == _norm_email(user.email),
                ThreadV2.status.in_((THREAD_STATUS_PENDING, THREAD_STATUS_REJECTED)),
            ),
        ),
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
    )
    db.add(thread)
    db.flush()  # assigns thread.id — the notifications below FK to it

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
    return _to_thread_detail(thread, my_vote=0)


def get_thread_by_id(
    db: Session, thread_id: int, user: UserInfo, *, access: ViewerAccess | None = None
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
    access to the widget at all keeps getting 403, never 404."""
    thread, component = _require_thread_and_component(db, thread_id)
    # 403 BEFORE the status check (landmine 2) — a caller with no view
    # access to the widget at all must keep getting the existing 403, never
    # a 404 that would leak "this id exists but isn't approved/yours".
    _check_view_access(component, access)
    if thread.status != THREAD_STATUS_APPROVED:
        is_author = _norm_email(thread.author_email) == _norm_email(user.email)
        if not (is_author or _is_component_editor(component, access)):
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
    *,
    access: ViewerAccess | None = None,
) -> ThreadDetail:
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

    # Validate against the EFFECTIVE content — the new content if this PATCH
    # touches it, otherwise the thread's current content — since a mention's
    # token must occur in whatever the stored body ends up being, not
    # necessarily what this specific request's `content` field carried
    # (e.g. a title-only edit that also resends the same mentions).
    if mentions is not None:
        effective_content = content if content is not None else thread.content
        previous_mentions = thread.mentions
        resolved = _validate_and_resolve_mentions(
            db, mentions, effective_content, component=component
        )
        thread.mentions = resolved
        # plan §5.4 — a PENDING thread's mentions are validated and stored
        # but NOT fanned out: nobody has read it yet, so there is no "newly
        # added" to notify — the whole stored array is notified once, at
        # approval time (`decide_thread`). An APPROVED thread keeps the
        # unchanged "only newly added" behaviour.
        if thread.status == THREAD_STATUS_APPROVED:
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
    access: ViewerAccess | None = None,
) -> ThreadModerationRow:
    """`POST /threads/{id}/approve` or `.../reject` — gated on `edit(n)`
    (plan §5.1, landmine 8: NOT `full_access`/Hub-Admin-only, by analogy
    with delete — a root-tab editor with no admin role must be able to
    approve). `FOR UPDATE` serializes two editors deciding at once; the
    loser re-reads a non-pending row and gets the winner's decision back in
    the 409 body."""
    thread, component = _require_thread_and_component(db, thread_id)
    _require_component_editor(component, access)

    # Re-fetch under lock — plan §5.3's recipe, same shape as the vote/
    # comment-count recompute locks elsewhere in this module. The unlocked
    # `thread` above already proved the caller is an editor of the right
    # component; this second read is what makes the pending-check and the
    # write atomic against a concurrent decision on the same row.
    thread = db.query(ThreadV2).filter(ThreadV2.id == thread_id).with_for_update().first()
    if thread.status != THREAD_STATUS_PENDING:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "message": "This thread has already been reviewed",
                "thread": _to_moderation_row(db, thread, component).model_dump(mode="json"),
            },
        )

    thread.status = THREAD_STATUS_APPROVED if approve else THREAD_STATUS_REJECTED
    thread.reviewed_by_email = _norm_email(user.email)
    thread.reviewed_by_name = user.name
    thread.reviewed_at = _utc_now()

    if approve:
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
        db, thread, reviewer=user, approved=approve, component_id=component.id
    )

    db.commit()
    db.refresh(thread)
    return _to_moderation_row(db, thread, component)


def list_pending_threads(
    db: Session, cursor: str | None, *, access: ViewerAccess | None = None
) -> ThreadModerationListResponse:
    """`GET /threads/moderation/pending` — scoped to the caller's moderated
    set (plan §5.2); an empty set is a `200` with an empty page, never a
    403 (a non-moderator deep-linking the management page sees an empty
    state, not an error). Oldest-first (FIFO) — the queue drains in the
    order people waited, not newest-first like the widget's own list."""
    component_ids = _moderated_component_ids(db, access)
    if not component_ids:
        return ThreadModerationListResponse(items=[], next_cursor=None)

    query = db.query(ThreadV2).filter(
        ThreadV2.component_id.in_(component_ids),
        ThreadV2.status == THREAD_STATUS_PENDING,
    )
    if cursor:
        after_created_at, after_id = _decode_cursor(cursor)
        # Ascending order flips the keyset comparison to `>` (plan §4.3's
        # row-comparison rule, same shape as the comment list's oldest-first
        # pagination).
        query = query.filter(
            tuple_(ThreadV2.created_at, ThreadV2.id) > (after_created_at, after_id)
        )

    rows = (
        query.order_by(ThreadV2.created_at.asc(), ThreadV2.id.asc())
        .limit(THREADS_PAGE_SIZE + 1)
        .all()
    )
    has_more = len(rows) > THREADS_PAGE_SIZE
    page = rows[:THREADS_PAGE_SIZE]
    next_cursor = _encode_cursor(page[-1].created_at, page[-1].id) if has_more and page else None

    return ThreadModerationListResponse(items=_to_moderation_rows(db, page), next_cursor=next_cursor)


def list_thread_history(
    db: Session, cursor: str | None, *, access: ViewerAccess | None = None
) -> ThreadModerationListResponse:
    """`GET /threads/moderation/history` — same scoping as the pending
    queue. `reviewed_at IS NOT NULL` is the whole filter (M5): a legacy or
    editor-auto-approved thread was never explicitly decided and does not
    appear here. Newest decision first."""
    component_ids = _moderated_component_ids(db, access)
    if not component_ids:
        return ThreadModerationListResponse(items=[], next_cursor=None)

    query = db.query(ThreadV2).filter(
        ThreadV2.component_id.in_(component_ids),
        ThreadV2.reviewed_at.isnot(None),
    )
    if cursor:
        after_reviewed_at, after_id = _decode_cursor(cursor)
        query = query.filter(
            tuple_(ThreadV2.reviewed_at, ThreadV2.id) < (after_reviewed_at, after_id)
        )

    rows = (
        query.order_by(ThreadV2.reviewed_at.desc(), ThreadV2.id.desc())
        .limit(THREADS_PAGE_SIZE + 1)
        .all()
    )
    has_more = len(rows) > THREADS_PAGE_SIZE
    page = rows[:THREADS_PAGE_SIZE]
    next_cursor = (
        _encode_cursor(page[-1].reviewed_at, page[-1].id) if has_more and page else None
    )

    return ThreadModerationListResponse(items=_to_moderation_rows(db, page), next_cursor=next_cursor)


def moderation_summary(
    db: Session, *, access: ViewerAccess | None = None
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
    regardless of whether any widget currently has a thread on it."""
    component_ids = _moderated_component_ids(db, access)
    can_moderate = (access is not None and access.full_access) or bool(component_ids)
    can_moderate_flag = 1 if can_moderate else 0

    if not component_ids:
        return (
            ThreadModerationSummary(
                can_moderate=can_moderate, moderated_component_count=0, pending_count=0
            ),
            f'"{can_moderate_flag}-0-0-0"',
        )

    count, max_id = (
        db.query(func.count(ThreadV2.id), func.max(ThreadV2.id))
        .filter(ThreadV2.component_id.in_(component_ids), ThreadV2.status == THREAD_STATUS_PENDING)
        .one()
    )
    count = int(count or 0)
    etag = (
        f'"{can_moderate_flag}-{len(component_ids)}-{count}-'
        f'{int(max_id) if max_id is not None else 0}"'
    )
    return (
        ThreadModerationSummary(
            can_moderate=can_moderate,
            moderated_component_count=len(component_ids),
            pending_count=count,
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
    user: UserInfo,
    *,
    access: ViewerAccess | None = None,
) -> CommentListResponse:
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

    thread = db.query(ThreadV2).filter(ThreadV2.id == thread_id).with_for_update().first()
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
