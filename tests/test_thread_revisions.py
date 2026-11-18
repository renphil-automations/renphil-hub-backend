"""
Tests for thread versioning (plan_thread_edit_versioning_2026-09-22.md,
phase A, with the owner's 2026-09-23 amendments):

  - every PUBLISHED version of a thread is a `thread_revisions` row — v1
    (`origin='original'`) written at publication, and `threads` caches the
    row at `threads.version`;
  - a non-editor author's PATCH of an approved thread is STAGED as a pending
    `origin='author'` row; editors decide them individually (in order, or
    with an explicit overwrite);
  - an editor-author's PATCH goes live as an auto-approved `origin='editor'`
    row that bumps the version — refused while author edits are pending
    unless `overwrite`;
  - revisions have their OWN management section (pending newest-first;
    history of every non-pending revision, filterable; facets; summary) and
    never appear in Threads Management.

Built on `_ModerationFixture` (tests.test_thread_moderation), itself on
`_ThreadFixture` (tests.test_threads).

THE TWO CASES THAT ARE EASY TO MAKE VACUOUS. `_member_auth` mints a Hub Admin
token, which trips `ViewerAccess.full_access` — a Hub Admin is an editor of
everything and never stages. So:
  - the author whose edit must STAGE holds a real `view` grant on the widget
    and authenticates with plain `_auth` (`AUTHOR` below) — never
    `_member_auth`, under which the staging assertions could never hold;
  - the ancestor-editor whose edit must go LIVE holds a real `edit` grant on
    `("tab", self.root_tab_id)` — NOT on the widget — and also authenticates
    with plain `_auth`, so the test proves the fold walks the ancestor chain
    rather than riding the admin bypass.

`FOR UPDATE` is invisible on SQLite: every race here is pinned with a plain
second call, never the lock. `UNIQUE(thread_id, version)` and the three
CHECKs ARE enforced on SQLite, so those constraint tests are real.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.db_v2.models.notification import NotificationV2
from app.db_v2.models.thread import ThreadRevisionV2, ThreadV2
from app.models.auth import UserInfo
from app.services import thread_service
from app.services.access_visibility_service import resolve_viewer_access
from tests.test_thread_moderation import _ModerationFixture

AUTHOR = "author@example.com"
EDITOR = "editor@example.com"
VIEWER = "viewer@example.com"

BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _RevisionFixture(_ModerationFixture):
    """`AUTHOR` (view grant — stages), `EDITOR` (edit grant — decides, and
    goes live when editing their own thread) and `VIEWER` (view grant —
    sees nothing of revisions) on each widget."""

    def _widget(self, link: str):
        widget = self._seed_thread_widget(link)
        self._grant(email=AUTHOR, node=("component", widget.id), level="view")
        self._grant(email=EDITOR, node=("component", widget.id), level="edit")
        self._grant(email=VIEWER, node=("component", widget.id), level="view")
        return widget

    def _approved(self, widget, *, mentions: list[dict] | None = None, **kwargs) -> ThreadV2:
        """An approved thread AND its v1 — the post-migration shape, so the
        head invariant holds from the first line of every test."""
        kwargs.setdefault("title", "Live title")
        kwargs.setdefault("content", "live body")
        kwargs.setdefault("author_email", AUTHOR)
        thread = self._insert_thread(widget.id, status="approved", **kwargs)
        if mentions is not None:
            thread.mentions = mentions
            self.db.commit()
        self._insert_revision(
            thread,
            version=1,
            status="approved",
            origin="original",
            title=thread.title,
            content=thread.content,
            mentions=list(thread.mentions or []),
            submitted_at=thread.created_at,
            reviewed_at=thread.created_at,
            reviewed_by_email=thread.author_email,
        )
        return thread

    def _insert_revision(
        self,
        thread: ThreadV2,
        *,
        version: int,
        status: str = "pending",
        origin: str = "author",
        title: str | None = None,
        content: str | None = None,
        mentions: list[dict] | None = None,
        submitted_by_email: str | None = None,
        submitted_at: datetime | None = None,
        reviewed_at: datetime | None = None,
        reviewed_by_email: str | None = None,
        reviewed_by_name: str | None = None,
    ) -> ThreadRevisionV2:
        revision = ThreadRevisionV2(
            thread_id=thread.id,
            version=version,
            title=title or f"Proposed v{version}",
            content=content or f"proposed body v{version}",
            mentions=mentions or [],
            status=status,
            origin=origin,
            submitted_by_email=submitted_by_email or thread.author_email,
            submitted_by_name=(submitted_by_email or thread.author_email).split("@")[0],
            submitted_at=submitted_at or thread_service._utc_now(),
            reviewed_at=reviewed_at,
            reviewed_by_email=reviewed_by_email,
            reviewed_by_name=reviewed_by_name,
        )
        self.db.add(revision)
        self.db.commit()
        self.db.refresh(revision)
        return revision

    def _patch(self, thread_id: int, body: dict, *, email: str = AUTHOR, headers: dict | None = None):
        return self.client.patch(
            f"/data/threads/{thread_id}", json=body, headers=headers or self._auth(email)
        )

    def _approve(self, thread_id: int, revision_id: int, *, overwrite: bool | None = None, email: str = EDITOR):
        return self.client.post(
            f"/data/threads/{thread_id}/revisions/{revision_id}/approve",
            json=None if overwrite is None else {"overwrite": overwrite},
            headers=self._auth(email),
        )

    def _reject(self, thread_id: int, revision_id: int, *, email: str = EDITOR):
        return self.client.post(
            f"/data/threads/{thread_id}/revisions/{revision_id}/reject",
            json={"reason": "Needs work"},
            headers=self._auth(email),
        )

    def _all_revisions(self, thread_id: int) -> list[ThreadRevisionV2]:
        self.db.expire_all()
        return (
            self.db.query(ThreadRevisionV2)
            .filter(ThreadRevisionV2.thread_id == thread_id)
            .order_by(ThreadRevisionV2.version)
            .all()
        )

    def _revisions(self, thread_id: int) -> list[ThreadRevisionV2]:
        """Every revision AFTER v1 — what most assertions are about."""
        return [r for r in self._all_revisions(thread_id) if r.version > 1]

    def _shape(self, thread_id: int) -> list[tuple[int, str, str]]:
        return [(r.version, r.status, r.origin) for r in self._all_revisions(thread_id)]

    def _live(self, thread_id: int) -> ThreadV2:
        self.db.expire_all()
        return self.db.query(ThreadV2).filter(ThreadV2.id == thread_id).one()

    def _assert_head_matches(self, thread_id: int) -> None:
        """THE invariant: the revision at `threads.version` is the live
        content, and it is approved."""
        live = self._live(thread_id)
        (head,) = [r for r in self._all_revisions(thread_id) if r.version == live.version]
        self.assertEqual(head.status, "approved")
        self.assertEqual((head.title, head.content), (live.title, live.content))
        self.assertEqual(
            [m["email"] for m in head.mentions or []], [m["email"] for m in live.mentions or []]
        )

    def _legacy_pending(self, thread: ThreadV2, contents: list[str], **kwargs) -> list[dict]:
        """Several PENDING author edits on one thread, seeded directly.
        Since plan_thread_pending_edit_update_2026-11-10 (U1) a second save
        UPDATES the pending edit in place, and U5 refuses a new edit next to
        an open declined one — so this shape only exists as LEGACY data
        (§3.5). Seeded at the next free versions with the live title, the
        shape the old saves produced. Returns `{"id", "version"}` dicts."""
        next_version = max(r.version for r in self._all_revisions(thread.id)) + 1
        out = []
        for offset, content in enumerate(contents):
            row = self._insert_revision(
                thread, version=next_version + offset, title=thread.title, content=content, **kwargs
            )
            out.append({"id": row.id, "version": row.version})
        return out

    def _stage(self, thread_id: int, body: dict) -> dict:
        """PATCH as AUTHOR and assert it staged; returns the revision summary."""
        resp = self._patch(thread_id, body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["applied"], resp.text)
        return resp.json()["revision"]


# ---------------------------------------------------------
# Who stages and who goes live (owner decision 2, amendment A2).
# ---------------------------------------------------------


class SubmitRoutesByAuthority(_RevisionFixture):
    def test_view_granted_author_stages(self) -> None:
        widget = self._widget("submit-viewer")
        thread = self._approved(widget)

        resp = self._patch(thread.id, {"title": "New title", "content": "new body"})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertFalse(body["applied"])
        # `thread` is the UNCHANGED live content.
        self.assertEqual(body["thread"]["title"], "Live title")
        self.assertEqual(body["thread"]["content"], "live body")
        self.assertIsNone(body["thread"]["edited_at"])
        self.assertEqual(body["thread"]["status"], "approved")
        self.assertEqual(body["revision"]["version"], 2)
        self.assertEqual(body["revision"]["status"], "pending")
        self.assertEqual(body["revision"]["origin"], "author")
        self.assertEqual(body["revision"]["title"], "New title")
        self.assertEqual(body["revision"]["submitted_by_email"], AUTHOR)
        self.assertIsNone(body["revision"]["reviewed_at"])
        # The author sees their own staged edit on the returned detail.
        self.assertEqual([r["version"] for r in body["thread"]["pending_revisions"]], [2])
        self.assertEqual(body["thread"]["pending_revision_count"], 1)

        live = self._live(thread.id)
        self.assertEqual((live.title, live.content, live.version), ("Live title", "live body", 1))
        self.assertIsNone(live.edited_at)
        self.assertEqual(live.status, "approved")
        self.assertEqual(
            self._shape(thread.id), [(1, "approved", "original"), (2, "pending", "author")]
        )
        self._assert_head_matches(thread.id)

    def test_component_editor_goes_live_as_editor_revision(self) -> None:
        widget = self._widget("submit-component-editor")
        thread = self._approved(widget, author_email=EDITOR)

        resp = self._patch(thread.id, {"content": "edited live"}, email=EDITOR)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["applied"])
        self.assertEqual(body["thread"]["content"], "edited live")
        self.assertEqual(
            (body["revision"]["version"], body["revision"]["status"], body["revision"]["origin"]),
            (2, "approved", "editor"),
        )
        self.assertEqual(body["revision"]["reviewed_by_email"], EDITOR)
        self.assertEqual(self._live(thread.id).version, 2)
        self.assertEqual(
            self._shape(thread.id), [(1, "approved", "original"), (2, "approved", "editor")]
        )
        self._assert_head_matches(thread.id)

    def test_tab_edit_grantee_goes_live(self) -> None:
        """The ancestor rule — a real `edit` grant on the ROOT TAB only (no
        grant on the widget itself), plain `_auth`."""
        widget = self._seed_thread_widget("submit-tab-editor")
        self._grant(email="tabeditor@example.com", node=("tab", self.root_tab_id), level="edit")
        thread = self._approved(widget, author_email="tabeditor@example.com")

        resp = self._patch(thread.id, {"content": "tab editor edit"}, email="tabeditor@example.com")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["applied"])
        self.assertEqual(resp.json()["revision"]["origin"], "editor")
        self.assertEqual(self._live(thread.id).content, "tab editor edit")
        self.assertEqual([r.status for r in self._revisions(thread.id)], ["approved"])

    def test_hub_admin_goes_live(self) -> None:
        widget = self._widget("submit-admin")
        thread = self._approved(widget, author_email="admin@example.com")

        resp = self._patch(thread.id, {"content": "admin edit"}, headers=self._member_auth("admin@example.com"))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["applied"])
        self.assertEqual(self._live(thread.id).content, "admin edit")
        self.assertEqual([r.origin for r in self._revisions(thread.id)], ["editor"])

    def test_non_author_403(self) -> None:
        widget = self._widget("submit-non-author")
        thread = self._approved(widget)

        resp = self._patch(thread.id, {"content": "hijack"}, email=VIEWER)
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(self._revisions(thread.id), [])

    def test_rejected_thread_409(self) -> None:
        widget = self._widget("submit-rejected")
        thread = self._insert_thread(widget.id, author_email=AUTHOR, status="rejected")

        resp = self._patch(thread.id, {"content": "again"})
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(self._all_revisions(thread.id), [])

    def test_pending_thread_edits_in_place_no_revision(self) -> None:
        widget = self._widget("submit-pending")
        thread = self._insert_thread(widget.id, author_email=AUTHOR, status="pending")

        resp = self._patch(thread.id, {"content": "still pending"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["applied"])
        self.assertIsNone(resp.json()["revision"])
        live = self._live(thread.id)
        self.assertEqual((live.content, live.status), ("still pending", "pending"))
        self.assertEqual(self._all_revisions(thread.id), [])

    def test_access_none_service_call_stages(self) -> None:
        """Direct service call: `access=None` is NOT an editor
        (`_is_component_editor`), so an internal caller stages rather than
        silently going live."""
        widget = self._widget("submit-access-none")
        thread = self._approved(widget)

        result = thread_service.update_thread_by_id(
            self.db,
            thread.id,
            UserInfo(email=AUTHOR, name="author", roles=[]),
            None,
            "service body",
            None,
            access=None,
        )
        self.assertFalse(result.applied)
        self.assertEqual(result.revision.version, 2)
        self.assertEqual(result.thread.content, "live body")
        self.assertEqual(self._live(thread.id).content, "live body")

    def test_noop_vs_live_writes_nothing(self) -> None:
        """Owner-approved 2026-09-23: an edit identical to the live thread
        writes nothing — for the staging author and the editor alike."""
        widget = self._widget("submit-noop-live")
        staged = self._approved(widget)
        direct = self._approved(widget, author_email=EDITOR)

        for thread, email in ((staged, AUTHOR), (direct, EDITOR)):
            for body in ({}, {"title": "Live title", "content": "live body", "mentions": []}):
                resp = self._patch(thread.id, body, email=email)
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertTrue(resp.json()["applied"])
                self.assertIsNone(resp.json()["revision"])
                self.assertIsNone(resp.json()["thread"]["edited_at"])
            self.assertEqual(self._revisions(thread.id), [])
            self.assertEqual(self._live(thread.id).version, 1)

    def test_noop_vs_pending_returns_the_existing_revision(self) -> None:
        widget = self._widget("submit-noop-pending")
        thread = self._approved(widget)
        first = self._stage(thread.id, {"content": "draft"})

        resp = self._patch(thread.id, {"title": "Live title", "content": "draft"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["applied"])
        self.assertEqual(resp.json()["revision"]["id"], first["id"])
        self.assertEqual([r.version for r in self._revisions(thread.id)], [2])

    def test_comments_and_votes_stay_open_across_submit(self) -> None:
        """Landmine 7 — `threads.status` never moves, so `_require_approved`
        never closes comments or votes on a thread with a staged edit."""
        widget = self._widget("submit-comments-open")
        thread = self._approved(widget)
        self._stage(thread.id, {"content": "pending edit"})

        self.assertEqual(self._live(thread.id).status, "approved")
        comment = self.client.post(
            f"/data/threads/{thread.id}/comments", json={"content": "c"}, headers=self._auth(VIEWER)
        )
        self.assertEqual(comment.status_code, 201, comment.text)
        listing = self.client.get(f"/data/threads/{thread.id}/comments", headers=self._auth(VIEWER))
        self.assertEqual(listing.status_code, 200, listing.text)
        vote = self.client.put(
            f"/data/threads/{thread.id}/vote", json={"value": 1}, headers=self._auth(VIEWER)
        )
        self.assertEqual(vote.status_code, 200, vote.text)


# ---------------------------------------------------------
# v1 — written at PUBLICATION (amendment A3).
# ---------------------------------------------------------


class OriginalVersion(_RevisionFixture):
    def test_editor_post_writes_v1_published_by_themselves(self) -> None:
        self._widget("orig-editor-post")
        resp = self.client.post(
            "/data/threads/component/orig-editor-post",
            json={"title": "T", "content": "c"},
            headers=self._auth(EDITOR),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        thread_id = resp.json()["id"]
        (v1,) = self._all_revisions(thread_id)
        self.assertEqual((v1.version, v1.status, v1.origin), (1, "approved", "original"))
        self.assertEqual((v1.title, v1.content), ("T", "c"))
        self.assertEqual((v1.submitted_by_email, v1.reviewed_by_email), (EDITOR, EDITOR))
        self.assertIsNotNone(v1.reviewed_at)
        self.assertEqual(self._live(thread_id).version, 1)
        self._assert_head_matches(thread_id)

    def test_viewer_post_has_no_revision_until_approved(self) -> None:
        self._widget("orig-viewer-post")
        resp = self.client.post(
            "/data/threads/component/orig-viewer-post",
            json={"title": "T", "content": "first draft"},
            headers=self._auth(AUTHOR),
        )
        thread_id = resp.json()["id"]
        self.assertEqual(resp.json()["status"], "pending")
        self.assertEqual(self._all_revisions(thread_id), [])

        # Edited while still pending — in place, still no revision.
        self._patch(thread_id, {"content": "final draft"})
        self.assertEqual(self._all_revisions(thread_id), [])

        approve = self.client.post(f"/data/threads/{thread_id}/approve", headers=self._auth(EDITOR))
        self.assertEqual(approve.status_code, 200, approve.text)
        (v1,) = self._all_revisions(thread_id)
        self.assertEqual((v1.origin, v1.status, v1.content), ("original", "approved", "final draft"))
        self.assertEqual(v1.submitted_by_email, AUTHOR)
        self.assertEqual(v1.reviewed_by_email, EDITOR)
        self.assertEqual(v1.reviewed_at, self._live(thread_id).reviewed_at)
        self._assert_head_matches(thread_id)

    def test_rejected_thread_gets_no_history(self) -> None:
        widget = self._widget("orig-rejected")
        thread = self._insert_thread(widget.id, author_email=AUTHOR, status="pending")
        resp = self.client.post(f"/data/threads/{thread.id}/reject", json={"reason": "Needs work"}, headers=self._auth(EDITOR))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._all_revisions(thread.id), [])

    def test_pre_versioning_thread_self_heals_v1_on_first_edit(self) -> None:
        """A thread with NO revision rows (pre-migration shape) gets the same
        v1 the migration's backfill writes, before its first appended
        revision — the history never starts at v2."""
        widget = self._widget("orig-self-heal")
        reviewed_at = BASE_TIME + timedelta(days=1)
        legacy = self._insert_thread(
            widget.id,
            author_email=AUTHOR,
            status="approved",
            content="legacy body",
            created_at=BASE_TIME,
            reviewed_by_email=EDITOR,
            reviewed_by_name="Ed",
            reviewed_at=reviewed_at,
        )
        self._stage(legacy.id, {"content": "first versioned edit"})

        v1, v2 = self._all_revisions(legacy.id)
        self.assertEqual((v1.version, v1.origin, v1.status, v1.content), (1, "original", "approved", "legacy body"))
        self.assertEqual((v1.submitted_by_email, v1.reviewed_by_email, v1.reviewed_by_name), (AUTHOR, EDITOR, "Ed"))
        self.assertEqual(v1.reviewed_at.replace(tzinfo=None), reviewed_at.replace(tzinfo=None))
        self.assertEqual((v2.version, v2.status), (2, "pending"))

    def test_pre_versioning_editor_post_self_heals_with_author_as_publisher(self) -> None:
        widget = self._widget("orig-self-heal-editor")
        legacy = self._insert_thread(
            widget.id, author_email=EDITOR, status="approved", created_at=BASE_TIME
        )
        self._patch(legacy.id, {"content": "edited"}, email=EDITOR)
        v1, v2 = self._all_revisions(legacy.id)
        self.assertEqual((v1.reviewed_by_email, v1.reviewed_by_name), (EDITOR, "editor"))
        self.assertEqual(v1.reviewed_at.replace(tzinfo=None), BASE_TIME.replace(tzinfo=None))
        self.assertEqual((v2.origin, v2.version), ("editor", 2))
        self._assert_head_matches(legacy.id)


# ---------------------------------------------------------
# An editor-author's edit is an auto-approved revision (amendment A2).
# ---------------------------------------------------------


class EditorEdits(_RevisionFixture):
    def test_each_editor_edit_bumps_the_version(self) -> None:
        widget = self._widget("editor-bumps")
        thread = self._approved(widget, author_email=EDITOR)
        for n, body in ((2, "second"), (3, "third")):
            resp = self._patch(thread.id, {"content": body}, email=EDITOR)
            self.assertEqual(resp.json()["revision"]["version"], n)
            self.assertEqual(self._live(thread.id).version, n)
            self._assert_head_matches(thread.id)

    def test_mentions_only_change_is_a_version_and_stamps_edited(self) -> None:
        """"Edited" means a version after v1 is live (owner, 2026-09-23) —
        a mentions-only change links a chip and notifies someone, so it
        counts."""
        widget = self._widget("editor-mentions-only")
        self._seed_mentionable("alice@example.com", ("component", widget.id), name="Alice Adams")
        thread = self._approved(widget, author_email=EDITOR, content="hi @AliceAdams")

        resp = self._patch(thread.id, {"mentions": [{"email": "alice@example.com"}]}, email=EDITOR)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["revision"]["version"], 2)
        self.assertIsNotNone(resp.json()["thread"]["edited_at"])
        self.assertIsNotNone(self._live(thread.id).edited_at)
        self._assert_head_matches(thread.id)

    def test_editor_edit_over_pending_author_edits_is_409(self) -> None:
        """The author submitted v2/v3 while a non-editor, then gained edit:
        their direct edit must not silently bury v2/v3 below the live
        version."""
        widget = self._widget("editor-over-pending")
        thread = self._approved(widget, author_email=EDITOR)
        self._insert_revision(thread, version=2)
        self._insert_revision(thread, version=3)

        resp = self._patch(thread.id, {"content": "direct"}, email=EDITOR)
        self.assertEqual(resp.status_code, 409, resp.text)
        detail = resp.json()["detail"]
        self.assertEqual(detail["code"], "earlier_revisions_pending")
        self.assertEqual([r["version"] for r in detail["earlier"]], [2, 3])
        self.assertIsNone(detail["revision"])
        # Nothing moved.
        self.assertEqual([r.status for r in self._revisions(thread.id)], ["pending", "pending"])
        self.assertEqual((self._live(thread.id).content, self._live(thread.id).version), ("live body", 1))

    def test_editor_edit_with_overwrite_displaces_pending(self) -> None:
        widget = self._widget("editor-overwrite")
        thread = self._approved(widget, author_email=EDITOR)
        self._insert_revision(thread, version=2)
        self._insert_revision(thread, version=3)

        resp = self._patch(thread.id, {"content": "direct", "overwrite": True}, email=EDITOR)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["revision"]["version"], 4)
        by_version = {r.version: r for r in self._revisions(thread.id)}
        for version in (2, 3):
            self.assertEqual(by_version[version].status, "overwritten")
            self.assertEqual(by_version[version].reviewed_by_email, EDITOR)
            self.assertEqual(by_version[version].reviewed_at, by_version[4].reviewed_at)
        self.assertEqual((by_version[4].origin, by_version[4].status), ("editor", "approved"))
        self._assert_head_matches(thread.id)

    def test_noop_over_pending_is_not_a_conflict(self) -> None:
        widget = self._widget("editor-noop-pending")
        thread = self._approved(widget, author_email=EDITOR)
        self._insert_revision(thread, version=2)
        resp = self._patch(thread.id, {"content": "live body"}, email=EDITOR)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(resp.json()["revision"])
        self.assertEqual(self._revisions(thread.id)[0].status, "pending")

    def test_overwrite_flag_is_ignored_when_staging(self) -> None:
        widget = self._widget("editor-overwrite-ignored")
        thread = self._approved(widget)
        v2 = self._stage(thread.id, {"content": "a"})
        resp = self._patch(thread.id, {"content": "b", "overwrite": True})
        self.assertFalse(resp.json()["applied"])
        # plan_thread_pending_edit_update U1 (rewritten 2026-11-10): the
        # second save UPDATES v2 in place — `overwrite` displaced nothing.
        self.assertEqual(
            [(r.id == v2["id"], r.status, r.content, r.edit_count) for r in self._revisions(thread.id)],
            [(True, "pending", "b", 1)],
        )

    def test_editor_edit_notifies_newly_added_mentions_with_editor_as_actor(self) -> None:
        widget = self._widget("editor-mention")
        self._seed_mentionable("alice@example.com", ("component", widget.id), name="Alice Adams")
        thread = self._approved(widget, author_email=EDITOR)
        self._patch(
            thread.id,
            {"content": "hi @AliceAdams", "mentions": [{"email": "alice@example.com"}]},
            email=EDITOR,
        )
        self.db.expire_all()
        (notification,) = self._notifications_for("alice@example.com")
        self.assertEqual(notification.actor_email, EDITOR)


# ---------------------------------------------------------
# Version numbers (plan §1.2).
# ---------------------------------------------------------


class VersionAllocation(_RevisionFixture):
    def test_first_edit_is_v2_a_second_save_updates_it(self) -> None:
        """Rewritten 2026-11-10 (plan_thread_pending_edit_update U1): was
        "first edit v2, second v3". A second save now updates v2 in place,
        so the next NEW version only comes after v2 is decided."""
        widget = self._widget("version-sequence")
        thread = self._approved(widget)
        first = self._stage(thread.id, {"content": "a"})
        self.assertEqual(first["version"], 2)
        # A plain second call pins the race FOR UPDATE would serialize.
        second = self._stage(thread.id, {"content": "b"})
        self.assertEqual((second["id"], second["version"]), (first["id"], 2))
        self.assertEqual(
            [(r.version, r.status, r.content) for r in self._revisions(thread.id)],
            [(2, "pending", "b")],
        )
        self.assertEqual(self._approve(thread.id, first["id"]).status_code, 200)
        self.assertEqual(self._stage(thread.id, {"content": "c"})["version"], 3)

    def test_after_approving_v3_the_next_is_v4(self) -> None:
        widget = self._widget("version-after-approve")
        thread = self._approved(widget)
        # Legacy shape (two pending), rewritten 2026-11-10: seeded, not saved.
        _v2, v3 = self._legacy_pending(thread, ["a", "b"])
        resp = self._approve(thread.id, v3["id"], overwrite=True)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._live(thread.id).version, 3)

        self.assertEqual(self._stage(thread.id, {"content": "c"})["version"], 4)

    def test_rejected_versions_are_never_reused(self) -> None:
        widget = self._widget("version-no-reuse")
        thread = self._approved(widget)
        v2 = self._stage(thread.id, {"content": "a"})
        self.assertEqual(self._reject(thread.id, v2["id"]).status_code, 200)
        # Rewritten 2026-11-10: U5 — the open declined v2 blocks a new edit
        # until the author closes it.
        close = self.client.post(
            f"/data/threads/{thread.id}/revisions/{v2['id']}/close", headers=self._auth(AUTHOR)
        )
        self.assertEqual(close.status_code, 204, close.text)

        self.assertEqual(self._stage(thread.id, {"content": "b"})["version"], 3)

    def test_editor_and_author_versions_share_one_sequence(self) -> None:
        """The author (view grant) stages v2 on their own thread and is
        later made an editor: after overwriting v2 their direct edit is v3,
        and a staged edit by a DIFFERENT path can never reuse either."""
        widget = self._widget("version-shared")
        thread = self._approved(widget)
        self._stage(thread.id, {"content": "a"})
        self._grant(email=AUTHOR, node=("component", widget.id), level="edit")
        resp = self._patch(thread.id, {"content": "b", "overwrite": True})
        self.assertEqual(resp.json()["revision"]["version"], 3)
        self.assertEqual(
            [(r.version, r.status, r.origin) for r in self._revisions(thread.id)],
            [(2, "overwritten", "author"), (3, "approved", "editor")],
        )

    def test_duplicate_thread_version_raises_integrity_error(self) -> None:
        widget = self._widget("version-duplicate")
        thread = self._approved(widget)
        self._insert_revision(thread, version=2)
        with self.assertRaises(IntegrityError):
            self._insert_revision(thread, version=2)
        self.db.rollback()

    def test_omitted_fields_come_from_the_latest_pending_revision(self) -> None:
        """Base rule (plan §4.2 step 2): a title-only PATCH after a
        content-only pending revision keeps THAT revision's content, not the
        live one."""
        widget = self._widget("version-base-rule")
        thread = self._approved(widget)
        self._stage(thread.id, {"content": "proposed content"})
        self._stage(thread.id, {"title": "Proposed title"})

        # Rewritten 2026-11-10 (U1): was a new v3; the second save now
        # updates v2 in place, with the same base rule.
        (v2,) = self._revisions(thread.id)
        self.assertEqual(
            (v2.version, v2.title, v2.content, v2.edit_count), (2, "Proposed title", "proposed content", 1)
        )


# ---------------------------------------------------------
# Who may even learn a thread has staged edits (plan §4.4, landmine 3).
# ---------------------------------------------------------


class RevisionVisibility(_RevisionFixture):
    def _setup(self, link: str) -> tuple[ThreadV2, dict]:
        widget = self._widget(link)
        thread = self._approved(widget)
        return thread, self._stage(thread.id, {"content": "secret edit"})

    def _list_count(self, email: str, link: str) -> int:
        resp = self.client.get(f"/data/threads/component/{link}", headers=self._auth(email))
        self.assertEqual(resp.status_code, 200, resp.text)
        (row,) = resp.json()["items"]
        return row["pending_revision_count"]

    def test_list_count_scoped(self) -> None:
        self._setup("vis-list")
        self.assertEqual(self._list_count(AUTHOR, "vis-list"), 1)
        self.assertEqual(self._list_count(EDITOR, "vis-list"), 1)
        self.assertEqual(self._list_count(VIEWER, "vis-list"), 0)

    def test_list_pending_edit_count_is_the_authors_own(self) -> None:
        """2026-11-13 — `ThreadListResponse.pending_edit_count` (the widget
        banner): the caller's OWN pending edits on this widget. The editor
        who reviews it and a plain viewer get 0; a decided edit stops
        counting."""
        _, revision = self._setup("vis-list-pending-edits")

        def counts(email: str) -> tuple[int, int]:
            body = self.client.get(
                "/data/threads/component/vis-list-pending-edits", headers=self._auth(email)
            ).json()
            return body["pending_edit_count"], body["pending_count"]

        self.assertEqual(counts(AUTHOR), (1, 0))
        self.assertEqual(counts(EDITOR), (0, 0))
        self.assertEqual(counts(VIEWER), (0, 0))
        thread_id = revision["thread_id"]
        self.assertEqual(self._reject(thread_id, revision["id"]).status_code, 200)
        self.assertEqual(counts(AUTHOR), (0, 0))

    def test_list_count_for_tab_editor(self) -> None:
        self._setup("vis-list-tab")
        self._grant(email="tabeditor@example.com", node=("tab", self.root_tab_id), level="edit")
        self.assertEqual(self._list_count("tabeditor@example.com", "vis-list-tab"), 1)

    def test_detail_pending_revisions_scoped(self) -> None:
        thread, _ = self._setup("vis-detail")
        for email, expected in ((AUTHOR, [2]), (EDITOR, [2]), (VIEWER, [])):
            resp = self.client.get(f"/data/threads/{thread.id}", headers=self._auth(email))
            self.assertEqual(resp.status_code, 200, resp.text)
            body = resp.json()
            self.assertEqual([r["version"] for r in body["pending_revisions"]], expected, email)
            self.assertEqual(body["pending_revision_count"], len(expected), email)
            # Live content for everyone; no body on the summaries.
            self.assertEqual(body["content"], "live body")
            for summary in body["pending_revisions"]:
                self.assertNotIn("content", summary)

    def test_get_revision_author_and_editor_200(self) -> None:
        thread, revision = self._setup("vis-get-ok")
        for email in (AUTHOR, EDITOR):
            resp = self.client.get(
                f"/data/threads/{thread.id}/revisions/{revision['id']}", headers=self._auth(email)
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json()["content"], "secret edit")
            self.assertEqual(resp.json()["version"], 2)

    def test_get_revision_other_viewer_404(self) -> None:
        thread, revision = self._setup("vis-get-viewer")
        resp = self.client.get(
            f"/data/threads/{thread.id}/revisions/{revision['id']}", headers=self._auth(VIEWER)
        )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_get_revision_ungranted_403_before_404(self) -> None:
        """Landmine 2: no view access → 403, for a real id AND a bogus one,
        so existence never leaks."""
        thread, revision = self._setup("vis-get-ungranted")
        for revision_id in (revision["id"], 999999):
            resp = self.client.get(
                f"/data/threads/{thread.id}/revisions/{revision_id}",
                headers=self._auth("nobody@example.com"),
            )
            self.assertEqual(resp.status_code, 403, resp.text)

    def test_get_revision_wrong_thread_or_unknown_id_404(self) -> None:
        thread, revision = self._setup("vis-get-mismatch")
        other = self._approved(self._widget("vis-get-mismatch-other"))
        # Revision exists, but on another thread: indistinguishable from absent.
        resp = self.client.get(
            f"/data/threads/{other.id}/revisions/{revision['id']}", headers=self._auth(AUTHOR)
        )
        self.assertEqual(resp.status_code, 404, resp.text)
        resp = self.client.get(f"/data/threads/{thread.id}/revisions/999999", headers=self._auth(AUTHOR))
        self.assertEqual(resp.status_code, 404, resp.text)


# ---------------------------------------------------------
# `ThreadDetail.viewer_is_editor` (phase B, owner-approved 2026-09-23) — the
# edit composer's base switch: must be exactly the predicate that routes a
# PATCH to the editor path, on every response that carries a ThreadDetail.
# ---------------------------------------------------------


class ViewerIsEditorFlag(_RevisionFixture):
    def test_get_detail_matches_edit_authority(self) -> None:
        widget = self._widget("vie-get")
        self._grant(email="tabeditor@example.com", node=("tab", self.root_tab_id), level="edit")
        thread = self._approved(widget)
        cases = (
            (self._auth(AUTHOR), False),
            (self._auth(VIEWER), False),
            (self._auth(EDITOR), True),
            # The ancestor rule — tab grant only, plain `_auth`.
            (self._auth("tabeditor@example.com"), True),
            (self._member_auth("admin@example.com"), True),
        )
        for headers, expected in cases:
            resp = self.client.get(f"/data/threads/{thread.id}", headers=headers)
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertIs(resp.json()["viewer_is_editor"], expected, headers)

    def test_patch_results_carry_the_flag(self) -> None:
        widget = self._widget("vie-patch")
        staged_thread = self._approved(widget)
        # Staged, then a no-op against the pending draft: both non-editor.
        resp = self._patch(staged_thread.id, {"content": "draft"})
        self.assertFalse(resp.json()["applied"])
        self.assertIs(resp.json()["thread"]["viewer_is_editor"], False)
        resp = self._patch(staged_thread.id, {"content": "draft"})
        self.assertFalse(resp.json()["applied"])
        self.assertIs(resp.json()["thread"]["viewer_is_editor"], False)

        # Editor path: a live edit, then a no-op.
        editor_thread = self._approved(widget, author_email=EDITOR)
        for content in ("editor edit", "editor edit"):
            resp = self._patch(editor_thread.id, {"content": content}, email=EDITOR)
            self.assertTrue(resp.json()["applied"], resp.text)
            self.assertIs(resp.json()["thread"]["viewer_is_editor"], True)

        # A pending thread edited in place by its (non-editor) author.
        pending = self._insert_thread(widget.id, author_email=AUTHOR, status="pending")
        resp = self._patch(pending.id, {"content": "pending edit"})
        self.assertTrue(resp.json()["applied"], resp.text)
        self.assertIs(resp.json()["thread"]["viewer_is_editor"], False)

    def test_create_carries_the_flag(self) -> None:
        self._widget("vie-create")
        for email, expected in ((AUTHOR, False), (EDITOR, True)):
            resp = self.client.post(
                "/data/threads/component/vie-create",
                json={"title": "T", "content": "c"},
                headers=self._auth(email),
            )
            self.assertEqual(resp.status_code, 201, resp.text)
            self.assertIs(resp.json()["viewer_is_editor"], expected, email)


# ---------------------------------------------------------
# Approve applies the snapshot; reject changes nothing live (plan §4.3).
# ---------------------------------------------------------


class ApproveApplies(_RevisionFixture):
    def test_approve_moves_everything_but_status(self) -> None:
        widget = self._widget("approve-applies")
        thread = self._approved(widget)
        revision = self._stage(thread.id, {"title": "New title", "content": "new body"})

        resp = self._approve(thread.id, revision["id"])
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["status"], "approved")
        self.assertEqual(body["reviewed_by_email"], EDITOR)
        self.assertEqual(body["reviewed_by_name"], "editor")
        self.assertIsNotNone(body["reviewed_at"])
        self.assertEqual(body["content"], "new body")

        live = self._live(thread.id)
        self.assertEqual((live.title, live.content, live.version), ("New title", "new body", 2))
        self.assertIsNotNone(live.edited_at)
        # The §1.1 guarantee, asserted: the THREAD's own status never moved.
        self.assertEqual(live.status, "approved")
        self.assertIsNone(live.reviewed_at)
        self._assert_head_matches(thread.id)

    def test_comments_and_votes_open_after_approve(self) -> None:
        widget = self._widget("approve-comments-open")
        thread = self._approved(widget)
        revision = self._stage(thread.id, {"content": "new"})
        self.assertEqual(self._approve(thread.id, revision["id"]).status_code, 200)

        comment = self.client.post(
            f"/data/threads/{thread.id}/comments", json={"content": "c"}, headers=self._auth(VIEWER)
        )
        self.assertEqual(comment.status_code, 201, comment.text)
        vote = self.client.put(
            f"/data/threads/{thread.id}/vote", json={"value": -1}, headers=self._auth(VIEWER)
        )
        self.assertEqual(vote.status_code, 200, vote.text)

    def test_reject_changes_nothing_live(self) -> None:
        widget = self._widget("approve-reject")
        thread = self._approved(widget)
        revision = self._stage(thread.id, {"content": "declined"})

        resp = self._reject(thread.id, revision["id"])
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "rejected")
        self.assertEqual(resp.json()["reviewed_by_email"], EDITOR)
        live = self._live(thread.id)
        self.assertEqual((live.content, live.version, live.status), ("live body", 1, "approved"))
        self.assertIsNone(live.edited_at)
        self._assert_head_matches(thread.id)

    def test_mentions_only_revision_stamps_edited_on_approve(self) -> None:
        widget = self._widget("approve-mentions-only")
        self._seed_mentionable("alice@example.com", ("component", widget.id), name="Alice Adams")
        thread = self._approved(widget, content="hi @AliceAdams")
        revision = self._stage(thread.id, {"mentions": [{"email": "alice@example.com"}]})
        # Staged: nothing live changed, so no marker yet.
        self.assertIsNone(self._live(thread.id).edited_at)
        self.assertEqual(self._approve(thread.id, revision["id"]).status_code, 200)
        self.assertIsNotNone(self._live(thread.id).edited_at)
        self.assertEqual(self._live(thread.id).version, 2)

    def test_viewer_and_author_cannot_decide(self) -> None:
        widget = self._widget("approve-gate")
        thread = self._approved(widget)
        revision = self._stage(thread.id, {"content": "x"})
        for email in (VIEWER, AUTHOR):
            self.assertEqual(self._approve(thread.id, revision["id"], email=email).status_code, 403)
            self.assertEqual(self._reject(thread.id, revision["id"], email=email).status_code, 403)
        self.assertEqual(self._revisions(thread.id)[0].status, "pending")

    def test_tab_editor_can_decide(self) -> None:
        widget = self._widget("approve-tab-editor")
        self._grant(email="tabeditor@example.com", node=("tab", self.root_tab_id), level="edit")
        thread = self._approved(widget)
        revision = self._stage(thread.id, {"content": "x"})
        resp = self._approve(thread.id, revision["id"], email="tabeditor@example.com")
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_second_decision_409_already_decided(self) -> None:
        widget = self._widget("approve-twice")
        thread = self._approved(widget)
        revision = self._stage(thread.id, {"content": "x"})
        self.assertEqual(self._approve(thread.id, revision["id"]).status_code, 200)

        for resp in (self._approve(thread.id, revision["id"]), self._reject(thread.id, revision["id"])):
            self.assertEqual(resp.status_code, 409, resp.text)
            detail = resp.json()["detail"]
            self.assertEqual(detail["code"], "already_decided")
            self.assertEqual(detail["revision"]["id"], revision["id"])
            self.assertEqual(detail["revision"]["status"], "approved")
            self.assertNotIn("earlier", detail)

    def test_auto_approved_rows_are_already_decided(self) -> None:
        """v1 and an editor's edit are approved on write — deciding them is
        the ordinary already-decided 409."""
        widget = self._widget("approve-auto")
        thread = self._approved(widget, author_email=EDITOR)
        editor_rev = self._patch(thread.id, {"content": "x"}, email=EDITOR).json()["revision"]
        v1 = self._all_revisions(thread.id)[0]
        for revision_id in (v1.id, editor_rev["id"]):
            resp = self._reject(thread.id, revision_id)
            self.assertEqual(resp.status_code, 409, resp.text)
            self.assertEqual(resp.json()["detail"]["code"], "already_decided")

    def test_revision_of_another_thread_404(self) -> None:
        widget = self._widget("approve-mismatch")
        thread = self._approved(widget)
        other = self._approved(widget)
        revision = self._stage(thread.id, {"content": "x"})
        self.assertEqual(self._approve(other.id, revision["id"]).status_code, 404)
        self.assertEqual(self._approve(thread.id, 999999).status_code, 404)

    def test_direct_service_call_on_hand_seeded_revision(self) -> None:
        """`decide_thread_revision` called directly, with a REAL
        `ViewerAccess` built by `resolve_viewer_access` for the edit
        grantee (not the router)."""
        widget = self._widget("approve-direct")
        thread = self._approved(widget)
        revision = self._insert_revision(thread, version=2, content="seeded body")
        hub_user = self._seed_hub_user(EDITOR)
        access = resolve_viewer_access(self.db, hub_user.id, is_admin=False)

        detail = thread_service.decide_thread_revision(
            self.db,
            thread.id,
            revision.id,
            UserInfo(email=EDITOR, name="Editor Person", roles=[]),
            approve=True,
            access=access,
        )
        self.assertEqual(detail.status, "approved")
        self.assertEqual(detail.reviewed_by_name, "Editor Person")
        live = self._live(thread.id)
        self.assertEqual((live.content, live.version), ("seeded body", 2))

    def test_direct_service_call_access_none_is_403(self) -> None:
        widget = self._widget("approve-direct-none")
        thread = self._approved(widget)
        revision = self._insert_revision(thread, version=2)

        with self.assertRaises(HTTPException) as ctx:
            thread_service.decide_thread_revision(
                self.db,
                thread.id,
                revision.id,
                UserInfo(email=EDITOR, name="e", roles=[]),
                approve=True,
                access=None,
            )
        self.assertEqual(ctx.exception.status_code, 403)


# ---------------------------------------------------------
# Out-of-order approve is refused unless `overwrite: true` (decision 4).
# ---------------------------------------------------------


class OutOfOrderGuard(_RevisionFixture):
    def _three(self, link: str) -> tuple[ThreadV2, list[dict]]:
        widget = self._widget(link)
        thread = self._approved(widget)
        # Legacy shape since 2026-11-10 (U1): seeded, not saved.
        return thread, self._legacy_pending(thread, [f"body {n}" for n in (2, 3, 4)])

    def test_plain_approve_of_v4_is_409_with_earlier(self) -> None:
        thread, (v2, v3, v4) = self._three("ooo-refused")

        resp = self._approve(thread.id, v4["id"])
        self.assertEqual(resp.status_code, 409, resp.text)
        detail = resp.json()["detail"]
        self.assertEqual(detail["code"], "earlier_revisions_pending")
        self.assertEqual([r["version"] for r in detail["earlier"]], [2, 3])
        self.assertEqual(detail["revision"]["version"], 4)
        self.assertIn("2 earlier edits", detail["message"])
        # Nothing moved.
        self.assertEqual([r.status for r in self._revisions(thread.id)], ["pending"] * 3)
        live = self._live(thread.id)
        self.assertEqual((live.content, live.version), ("live body", 1))

    def test_overwrite_applies_v4_and_marks_earlier_overwritten(self) -> None:
        thread, (v2, v3, v4) = self._three("ooo-overwrite")

        resp = self._approve(thread.id, v4["id"], overwrite=True)
        self.assertEqual(resp.status_code, 200, resp.text)
        live = self._live(thread.id)
        self.assertEqual((live.content, live.version, live.status), ("body 4", 4, "approved"))

        revisions = {r.version: r for r in self._revisions(thread.id)}
        self.assertEqual(revisions[4].status, "approved")
        for version in (2, 3):
            self.assertEqual(revisions[version].status, "overwritten")
            self.assertEqual(revisions[version].reviewed_by_email, EDITOR)
            self.assertEqual(revisions[version].reviewed_at, revisions[4].reviewed_at)

        # A later approve of an overwritten revision is an ordinary
        # already-decided 409 — no separate "too old" case (plan §1.2).
        late = self._approve(thread.id, v2["id"], overwrite=True)
        self.assertEqual(late.status_code, 409, late.text)
        self.assertEqual(late.json()["detail"]["code"], "already_decided")
        self.assertEqual(late.json()["detail"]["revision"]["status"], "overwritten")

    def test_reject_out_of_order_needs_no_overwrite(self) -> None:
        thread, (v2, v3, v4) = self._three("ooo-reject")
        resp = self._reject(thread.id, v4["id"])
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            [(r.version, r.status) for r in self._revisions(thread.id)],
            [(2, "pending"), (3, "pending"), (4, "rejected")],
        )


class InOrderFlow(_RevisionFixture):
    def test_approve_v2_then_v3(self) -> None:
        widget = self._widget("in-order")
        thread = self._approved(widget)
        # Legacy shape since 2026-11-10 (U1): seeded, not saved.
        v2, v3 = self._legacy_pending(thread, ["second", "third"])

        self.assertEqual(self._approve(thread.id, v2["id"]).status_code, 200)
        self.assertEqual(self._live(thread.id).content, "second")
        self._assert_head_matches(thread.id)
        self.assertEqual(self._approve(thread.id, v3["id"]).status_code, 200)

        live = self._live(thread.id)
        self.assertEqual((live.content, live.version), ("third", 3))
        self.assertEqual([r.status for r in self._revisions(thread.id)], ["approved", "approved"])
        self._assert_head_matches(thread.id)


class RejectIsSnapshotScoped(_RevisionFixture):
    def test_reject_v2_leaves_later_pending_and_v3_publishes_v2_words(self) -> None:
        """Decision 6's accepted consequence, PINNED: revisions are full
        snapshots, so v3 (written on top of v2's text) still carries v2's
        words after v2 is rejected, and approving v3 publishes them. Intended
        — do not "fix".

        2026-11-10: plan_thread_pending_edit_update U1 closes the way this
        shape arose (a second save now updates the pending edit), so it is
        seeded as LEGACY data (§3.5) — those threads still behave this way."""
        widget = self._widget("reject-scoped")
        thread = self._approved(widget)
        v2, v3, _v4 = self._legacy_pending(
            thread, ["v2 words", "v2 words plus v3 words", "v4 words"]
        )

        self.assertEqual(self._reject(thread.id, v2["id"]).status_code, 200)
        self.assertEqual(
            [(r.version, r.status) for r in self._revisions(thread.id)],
            [(2, "rejected"), (3, "pending"), (4, "pending")],
        )
        self.assertEqual(self._live(thread.id).content, "live body")

        # v2 is no longer pending, so v3 is now first in line among PENDING
        # edits. plan_thread_reject_resubmit §12.4/§12.5 (repointed
        # 2026-11-08): v2 is an OPEN declined edit below v3, so a plain
        # approve is the confirm 409 with `earlier` empty and v2 in
        # `declined_open`; the overwrite then publishes v3 and closes v2 as
        # superseded by v3.
        refused = self._approve(thread.id, v3["id"])
        self.assertEqual(refused.status_code, 409, refused.text)
        detail = refused.json()["detail"]
        self.assertEqual(detail["code"], "earlier_revisions_pending")
        self.assertEqual(detail["earlier"], [])
        self.assertEqual([r["id"] for r in detail["declined_open"]], [v2["id"]])
        resp = self._approve(thread.id, v3["id"], overwrite=True)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("v2 words", self._live(thread.id).content)
        first = self._revisions(thread.id)[0]
        self.assertEqual(
            (first.status, first.closed_kind, first.closed_by_version), ("rejected", "superseded", 3)
        )


# ---------------------------------------------------------
# Mentions: validated at submit, fanned out at approve (plan §4.5).
# ---------------------------------------------------------


class MentionFanout(_RevisionFixture):
    def _mention_widget(self, link: str):
        widget = self._widget(link)
        self._seed_mentionable("alice@example.com", ("component", widget.id), name="Alice Adams")
        return widget

    def _alice(self) -> list[NotificationV2]:
        self.db.expire_all()
        return self._notifications_for("alice@example.com")

    def test_no_notification_at_submit_one_at_approve_author_as_actor(self) -> None:
        widget = self._mention_widget("mention-approve")
        thread = self._approved(widget)
        revision = self._stage(
            thread.id, {"content": "hi @AliceAdams", "mentions": [{"email": "alice@example.com"}]}
        )
        self.assertEqual(self._alice(), [])
        self.assertEqual(self._revisions(thread.id)[0].mentions[0]["email"], "alice@example.com")

        self.assertEqual(self._approve(thread.id, revision["id"]).status_code, 200)
        (notification,) = self._alice()
        self.assertEqual(notification.type, "mention")
        self.assertEqual(notification.actor_email, AUTHOR)
        self.assertIsNone(notification.comment_id)
        self.assertEqual([m["email"] for m in self._live(thread.id).mentions], ["alice@example.com"])

    def test_rejected_revision_never_notifies(self) -> None:
        widget = self._mention_widget("mention-reject")
        thread = self._approved(widget)
        revision = self._stage(
            thread.id, {"content": "hi @AliceAdams", "mentions": [{"email": "alice@example.com"}]}
        )
        self.assertEqual(self._reject(thread.id, revision["id"]).status_code, 200)
        self.assertEqual(self._alice(), [])

    def test_mention_in_overwritten_v2_and_approved_v3_notifies_once(self) -> None:
        widget = self._mention_widget("mention-overwrite")
        thread = self._approved(widget)
        mentions = [{"email": "alice@example.com"}]
        self._stage(thread.id, {"content": "v2 @AliceAdams", "mentions": mentions})
        v3 = self._stage(thread.id, {"content": "v3 @AliceAdams", "mentions": mentions})

        self.assertEqual(self._approve(thread.id, v3["id"], overwrite=True).status_code, 200)
        self.assertEqual(len(self._alice()), 1)

    def test_mention_already_live_is_not_renotified(self) -> None:
        widget = self._mention_widget("mention-already-live")
        thread = self._approved(
            widget,
            content="hi @AliceAdams",
            mentions=[{"email": "alice@example.com", "name": "Alice Adams", "token": "AliceAdams"}],
        )
        revision = self._stage(thread.id, {"content": "still hi @AliceAdams"})
        self.assertEqual(self._approve(thread.id, revision["id"]).status_code, 200)
        self.assertEqual(self._alice(), [])

    def test_ungranted_mention_dropped_at_submit(self) -> None:
        widget = self._widget("mention-ungranted")
        self._seed_user("Carol Jones", work_email="carol@example.com")  # roster, no grant
        thread = self._approved(widget)
        revision = self._stage(
            thread.id, {"content": "hi @CarolJones", "mentions": [{"email": "carol@example.com"}]}
        )
        self.assertEqual(self._revisions(thread.id)[0].mentions, [])
        self.assertEqual(revision["version"], 2)


# ---------------------------------------------------------
# Threads Management is UNCHANGED — new threads only (amendment A4).
# ---------------------------------------------------------


class ThreadsManagementUnchanged(_RevisionFixture):
    def test_pending_queue_lists_new_threads_only(self) -> None:
        widget = self._widget("tm-pending")
        pending_thread = self._insert_thread(widget.id, status="pending")
        self._insert_revision(self._approved(widget), version=2)

        resp = self.client.get("/data/threads/moderation/pending", headers=self._auth(EDITOR))
        self.assertEqual(resp.status_code, 200, resp.text)
        items = resp.json()["items"]
        self.assertEqual([row["id"] for row in items], [pending_thread.id])
        # The row shape is the pre-versioning one — no revision fields.
        self.assertNotIn("kind", items[0])
        self.assertNotIn("revision_id", items[0])

    def test_history_lists_thread_decisions_only(self) -> None:
        widget = self._widget("tm-history")
        decided = self._insert_thread(
            widget.id, status="rejected", reviewed_at=BASE_TIME, reviewed_by_email=EDITOR
        )
        thread = self._approved(widget)
        revision = self._stage(thread.id, {"content": "x"})
        self.assertEqual(self._approve(thread.id, revision["id"]).status_code, 200)

        resp = self.client.get("/data/threads/moderation/history", headers=self._auth(EDITOR))
        self.assertEqual([row["id"] for row in resp.json()["items"]], [decided.id])

    def test_summary_counts_threads_only_with_four_field_etag(self) -> None:
        widget = self._widget("tm-summary")
        pending_thread = self._insert_thread(widget.id, status="pending")
        self._insert_revision(self._approved(widget), version=2)

        resp = self.client.get("/data/threads/moderation/summary", headers=self._auth(EDITOR))
        self.assertEqual(resp.json()["pending_count"], 1)
        self.assertEqual(resp.headers["etag"], f'"1-1-1-{pending_thread.id}"')


# ---------------------------------------------------------
# Revision Management — Pending tab (newest first).
# ---------------------------------------------------------


class RevisionPendingList(_RevisionFixture):
    PATH = "/data/threads/revision-moderation/pending"

    def _walk(self, email: str = EDITOR) -> list[dict]:
        items: list[dict] = []
        cursor = None
        for _ in range(20):
            params = {"cursor": cursor} if cursor else {}
            resp = self.client.get(self.PATH, params=params, headers=self._auth(email))
            self.assertEqual(resp.status_code, 200, resp.text)
            items.extend(resp.json()["items"])
            cursor = resp.json()["next_cursor"]
            if not cursor:
                return items
        self.fail("cursor never terminated")

    def _pending_edit(self, widget, minutes: float, *, version: int = 2) -> ThreadRevisionV2:
        return self._insert_revision(
            self._approved(widget), version=version, submitted_at=BASE_TIME + timedelta(minutes=minutes)
        )

    def test_newest_first(self) -> None:
        widget = self._widget("rp-order")
        older = self._pending_edit(widget, 1)
        newer = self._pending_edit(widget, 2)
        self.assertEqual([r["id"] for r in self._walk()], [newer.id, older.id])

    def test_row_shape(self) -> None:
        widget = self._widget("rp-shape")
        thread = self._approved(widget, title="Live T")
        revision = self._insert_revision(
            thread, version=2, title="Proposed", content="proposed **body**", submitted_at=BASE_TIME
        )
        (row,) = self._walk()
        self.assertEqual(row["id"], revision.id)
        self.assertEqual(row["thread_id"], thread.id)
        self.assertEqual((row["version"], row["status"], row["origin"]), (2, "pending", "author"))
        self.assertEqual((row["title"], row["content_excerpt"]), ("Proposed", "proposed body"))
        self.assertEqual((row["thread_title"], row["thread_version"]), ("Live T", 1))
        self.assertEqual((row["component_id"], row["component_link"]), (widget.id, "rp-shape"))
        self.assertEqual(row["location_label"], "Nav Threads › Root Threads")
        self.assertEqual(row["earlier_pending_count"], 0)

    def test_earlier_pending_count_per_row(self) -> None:
        widget = self._widget("rp-earlier")
        thread = self._approved(widget)
        for version in (2, 3, 4):
            self._insert_revision(thread, version=version, submitted_at=BASE_TIME + timedelta(minutes=version))
        rows = self._walk()
        self.assertEqual(
            [(r["version"], r["earlier_pending_count"]) for r in rows], [(4, 2), (3, 1), (2, 0)]
        )

    def test_cursor_walks_multiple_pages_without_dup_or_skip(self) -> None:
        widget = self._widget("rp-paging")
        expected = [self._pending_edit(widget, i).id for i in range(25)]
        self.assertEqual([r["id"] for r in self._walk()], list(reversed(expected)))

    def test_only_moderated_components(self) -> None:
        moderated = self._widget("rp-scope-in")
        unmoderated = self._seed_thread_widget("rp-scope-out")
        kept = self._pending_edit(moderated, 1)
        self._pending_edit(unmoderated, 2)
        self.assertEqual([r["id"] for r in self._walk()], [kept.id])

    def test_decided_and_auto_rows_never_listed(self) -> None:
        widget = self._widget("rp-decided")
        thread = self._approved(widget)
        revision = self._stage(thread.id, {"content": "x"})
        self.assertEqual(len(self._walk()), 1)
        self.assertEqual(self._approve(thread.id, revision["id"]).status_code, 200)
        self.assertEqual(self._walk(), [])

    def test_pure_viewer_sees_empty_queue(self) -> None:
        widget = self._widget("rp-viewer")
        self._pending_edit(widget, 1)
        resp = self.client.get(self.PATH, headers=self._auth(VIEWER))
        self.assertEqual(resp.json(), {"items": [], "next_cursor": None})

    def test_malformed_cursor_400(self) -> None:
        self._widget("rp-bad-cursor")
        resp = self.client.get(self.PATH, params={"cursor": "nope"}, headers=self._auth(EDITOR))
        self.assertEqual(resp.status_code, 400, resp.text)


# ---------------------------------------------------------
# Revision Management — History tab (every non-pending row, filterable).
# ---------------------------------------------------------


class RevisionHistoryList(_RevisionFixture):
    PATH = "/data/threads/revision-moderation/history"

    def _get(self, email: str = EDITOR, **params) -> list[dict]:
        items: list[dict] = []
        cursor = None
        for _ in range(20):
            query = dict(params)
            if cursor:
                query["cursor"] = cursor
            resp = self.client.get(self.PATH, params=query, headers=self._auth(email))
            self.assertEqual(resp.status_code, 200, resp.text)
            items.extend(resp.json()["items"])
            cursor = resp.json()["next_cursor"]
            if not cursor:
                return items
        self.fail("cursor never terminated")

    def _seed(self) -> dict[str, ThreadRevisionV2]:
        """One of each kind, decided at distinct instants (minutes 1..5):
        an original, an editor edit, an approved / rejected / overwritten
        author edit — plus a pending one that must never appear."""
        widget = self._widget("rh-seed")
        at = lambda m: BASE_TIME + timedelta(minutes=m)  # noqa: E731
        thread = self._insert_thread(widget.id, status="approved", author_email=AUTHOR, created_at=at(0))
        rows = {
            "original": self._insert_revision(
                thread, version=1, status="approved", origin="original",
                reviewed_at=at(1), reviewed_by_email=EDITOR, reviewed_by_name="Ed",
            ),
            "overwritten": self._insert_revision(
                thread, version=2, status="overwritten",
                reviewed_at=at(3), reviewed_by_email="rita@example.com", reviewed_by_name="Rita",
            ),
            "approved": self._insert_revision(
                thread, version=3, status="approved",
                reviewed_at=at(3), reviewed_by_email="rita@example.com", reviewed_by_name="Rita",
            ),
            "rejected": self._insert_revision(
                thread, version=4, status="rejected",
                reviewed_at=at(4), reviewed_by_email=EDITOR, reviewed_by_name="Ed",
            ),
            "editor": self._insert_revision(
                thread, version=5, status="approved", origin="editor",
                submitted_by_email=EDITOR, reviewed_at=at(5), reviewed_by_email=EDITOR, reviewed_by_name="Ed",
            ),
        }
        self._insert_revision(thread, version=6)  # pending — never history
        return rows

    def test_everything_non_pending_newest_decision_first(self) -> None:
        rows = self._seed()
        self.assertEqual(
            [r["id"] for r in self._get()],
            [
                rows["editor"].id,
                rows["rejected"].id,
                # Same reviewed_at (one overwrite decision) — id DESC.
                rows["approved"].id,
                rows["overwritten"].id,
                rows["original"].id,
            ],
        )

    def test_status_filter(self) -> None:
        rows = self._seed()
        self.assertEqual([r["id"] for r in self._get(status="rejected")], [rows["rejected"].id])
        self.assertEqual(
            {r["id"] for r in self._get(status=["overwritten", "rejected"])},
            {rows["rejected"].id, rows["overwritten"].id},
        )

    def test_origin_filter(self) -> None:
        rows = self._seed()
        self.assertEqual([r["id"] for r in self._get(origin="original")], [rows["original"].id])
        self.assertEqual([r["id"] for r in self._get(origin="editor")], [rows["editor"].id])
        self.assertEqual(len(self._get(origin="author")), 3)

    def test_decision_date_range_is_half_open(self) -> None:
        rows = self._seed()
        got = self._get(
            date_from=(BASE_TIME + timedelta(minutes=3)).isoformat(),
            date_to=(BASE_TIME + timedelta(minutes=5)).isoformat(),
        )
        self.assertEqual(
            {r["id"] for r in got},
            {rows["approved"].id, rows["overwritten"].id, rows["rejected"].id},
        )

    def test_date_bound_with_offset_is_converted_to_utc(self) -> None:
        rows = self._seed()
        # 02:04+02:00 == 00:04Z — only minute 4 and 5 are at or after it.
        bound = (BASE_TIME + timedelta(minutes=4)).astimezone(timezone(timedelta(hours=2)))
        got = self._get(date_from=bound.isoformat())
        self.assertEqual({r["id"] for r in got}, {rows["rejected"].id, rows["editor"].id})

    def test_author_and_reviewer_filters(self) -> None:
        rows = self._seed()
        self.assertEqual([r["id"] for r in self._get(author=EDITOR.upper())], [rows["editor"].id])
        self.assertEqual(
            {r["id"] for r in self._get(reviewer="rita@example.com")},
            {rows["approved"].id, rows["overwritten"].id},
        )
        self.assertEqual(
            [r["id"] for r in self._get(author=AUTHOR, reviewer=EDITOR, status="rejected")],
            [rows["rejected"].id],
        )

    def test_thread_filter(self) -> None:
        """Phase C: the review modal's "an earlier edit of THIS thread was
        rejected" query — one thread's own log, other threads excluded,
        repeatable, and combinable with the other filters."""
        rows = self._seed()
        thread_id = rows["original"].thread_id
        other = self._approved(self._widget("rh-thread-other"))
        other_rejected = self._insert_revision(
            other, version=2, status="rejected",
            reviewed_at=BASE_TIME + timedelta(minutes=6), reviewed_by_email=EDITOR,
        )
        self.assertEqual(
            {r["id"] for r in self._get(thread_id=thread_id)},
            {row.id for row in rows.values()},
        )
        self.assertEqual(
            [r["id"] for r in self._get(thread_id=thread_id, status="rejected")],
            [rows["rejected"].id],
        )
        self.assertEqual(
            {r["id"] for r in self._get(thread_id=[thread_id, other.id], status="rejected")},
            {rows["rejected"].id, other_rejected.id},
        )

    def test_thread_filter_never_widens_the_moderated_set(self) -> None:
        self._widget("rh-thread-scope")
        unmoderated = self._insert_thread(
            self._seed_thread_widget("rh-thread-scope-out").id, status="approved", author_email=AUTHOR
        )
        self._insert_revision(
            unmoderated, version=1, status="approved", origin="original",
            reviewed_at=BASE_TIME, reviewed_by_email=AUTHOR,
        )
        self.assertEqual(self._get(thread_id=unmoderated.id), [])

    def test_invalid_filter_values_422(self) -> None:
        self._widget("rh-invalid")
        for params in (
            {"status": "pending"}, {"origin": "bogus"}, {"date_from": "not-a-date"}, {"thread_id": "x"},
        ):
            resp = self.client.get(self.PATH, params=params, headers=self._auth(EDITOR))
            self.assertEqual(resp.status_code, 422, (params, resp.text))

    def test_cursor_walks_with_a_filter(self) -> None:
        widget = self._widget("rh-paging")
        expected = []
        for i in range(25):
            thread = self._approved(widget)
            expected.append(
                self._insert_revision(
                    thread, version=2, status="rejected",
                    reviewed_at=BASE_TIME + timedelta(minutes=10 + i), reviewed_by_email=EDITOR,
                ).id
            )
        got = self._get(status="rejected")
        self.assertEqual([r["id"] for r in got], list(reversed(expected)))

    def test_real_decisions_and_publications_land_in_history(self) -> None:
        """End to end: an editor's post (v1), an editor's direct edit, a
        viewer's post approved (v1), an author edit rejected."""
        self._widget("rh-real")
        editor_post = self.client.post(
            "/data/threads/component/rh-real", json={"title": "E", "content": "e"}, headers=self._auth(EDITOR)
        ).json()["id"]
        self._patch(editor_post, {"content": "e2"}, email=EDITOR)
        viewer_post = self.client.post(
            "/data/threads/component/rh-real", json={"title": "V", "content": "v"}, headers=self._auth(AUTHOR)
        ).json()["id"]
        self.client.post(f"/data/threads/{viewer_post}/approve", headers=self._auth(EDITOR))
        staged = self._stage(viewer_post, {"content": "v2"})
        self._reject(viewer_post, staged["id"])

        got = [(r["thread_id"], r["version"], r["origin"], r["status"]) for r in self._get()]
        self.assertEqual(
            sorted(got),
            sorted([
                (editor_post, 1, "original", "approved"),
                (editor_post, 2, "editor", "approved"),
                (viewer_post, 1, "original", "approved"),
                (viewer_post, 2, "author", "rejected"),
            ]),
        )

    def test_pure_viewer_sees_nothing(self) -> None:
        self._seed()
        self.assertEqual(self._get(email=VIEWER), [])


class RevisionHistorySearchGroupOrder(_RevisionFixture):
    """2026-11-09 history-table rework: `q`, `group`, `order` on Revision
    Management History."""

    PATH = "/data/threads/revision-moderation/history"

    def _page(self, **params) -> dict:
        resp = self.client.get(self.PATH, params=params, headers=self._auth(EDITOR))
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _walk(self, **params) -> list[int]:
        ids: list[int] = []
        cursor = None
        for _ in range(50):
            query = dict(params)
            if cursor:
                query["cursor"] = cursor
            body = self._page(**query)
            ids.extend(r["id"] for r in body["items"])
            cursor = body["next_cursor"]
            if not cursor:
                return ids
        self.fail("cursor never terminated")

    def _seed(self) -> dict[str, ThreadRevisionV2]:
        """Two threads on two widgets, decisions interleaved in time:
        A.v1 (m1), B.v1 (m2), A.v2 (m3), B.v2 (m4), A.v3 (m5)."""
        w1, w2 = self._widget("rh-sgo-1"), self._widget("rh-sgo-2")
        at = lambda m: BASE_TIME + timedelta(minutes=m)  # noqa: E731
        a = self._insert_thread(w1.id, status="approved", author_email=AUTHOR, title="Alpha live")
        b = self._insert_thread(w2.id, status="approved", author_email=AUTHOR, title="Bravo live")

        def rev(thread, version, minute, **kw):
            return self._insert_revision(
                thread, version=version, status=kw.pop("status", "approved"),
                origin="original" if version == 1 else "author",
                reviewed_at=at(minute), reviewed_by_email=EDITOR, **kw,
            )

        return {
            "a1": rev(a, 1, 1, title="Alpha live"),
            "b1": rev(b, 1, 2, title="Bravo live"),
            "a2": rev(a, 2, 3, title="Alpha 50% off", status="rejected"),
            "b2": rev(b, 2, 4, title="Bravo second"),
            "a3": rev(a, 3, 5, title="Alpha third"),
        }

    def test_search_matches_revision_title_current_title_and_widget(self) -> None:
        r = self._seed()
        self.assertEqual(self._walk(q="50%"), [r["a2"].id])
        self.assertEqual(self._walk(q="5_%"), [])
        self.assertEqual(self._walk(q="SECOND"), [r["b2"].id])
        # Thread's CURRENT title: every revision of Bravo.
        self.assertEqual(self._walk(q="bravo live"), [r["b2"].id, r["b1"].id])
        widget_title = next(i for i in self._page()["items"] if i["id"] == r["b1"].id)["widget_title"]
        expected = {i["id"] for i in self._page()["items"] if i["widget_title"] == widget_title}
        self.assertEqual(set(self._walk(q=widget_title.lower())), expected)

    def test_group_by_thread_and_widget_both_orders(self) -> None:
        r = self._seed()
        ids = lambda *keys: [r[k].id for k in keys]  # noqa: E731
        self.assertEqual(self._walk(), ids("a3", "b2", "a2", "b1", "a1"))
        self.assertEqual(self._walk(order="oldest"), ids("a1", "b1", "a2", "b2", "a3"))
        self.assertEqual(self._walk(group="thread"), ids("a3", "a2", "a1", "b2", "b1"))
        self.assertEqual(self._walk(group="widget"), ids("a3", "a2", "a1", "b2", "b1"))
        self.assertEqual(self._walk(group="thread", order="oldest"), ids("a1", "a2", "a3", "b1", "b2"))
        items = self._page(group="thread")["items"]
        self.assertEqual([i["group_size"] for i in items], [3, 3, 3, 2, 2])
        self.assertTrue(all(i["group_size"] is None for i in self._page()["items"]))
        # Grouping applies to the FILTERED rows.
        self.assertEqual(self._walk(group="thread", status="approved"), ids("a3", "a1", "b2", "b1"))

    def test_paged_walks_match_one_page(self) -> None:
        self._seed()
        for params in ({}, {"group": "thread"}, {"group": "widget", "order": "oldest"}, {"order": "oldest"}):
            with self.subTest(params=params):
                whole = [i["id"] for i in self._page(**params)["items"]]
                with mock.patch.object(thread_service, "THREADS_PAGE_SIZE", 2):
                    self.assertEqual(self._walk(**params), whole)

    def test_bad_group_order_cursor(self) -> None:
        self._seed()
        for params in ({"group": "author"}, {"order": "up"}):
            resp = self.client.get(self.PATH, params=params, headers=self._auth(EDITOR))
            self.assertEqual(resp.status_code, 422, resp.text)
        with mock.patch.object(thread_service, "THREADS_PAGE_SIZE", 1):
            flat = self._page()["next_cursor"]
        resp = self.client.get(self.PATH, params={"group": "widget", "cursor": flat}, headers=self._auth(EDITOR))
        self.assertEqual(resp.status_code, 400, resp.text)


class RevisionHistoryFacets(_RevisionFixture):
    PATH = "/data/threads/revision-moderation/history/facets"

    def test_distinct_authors_and_reviewers_in_moderated_set(self) -> None:
        widget = self._widget("rf-facets")
        thread = self._approved(widget)  # v1 by AUTHOR, published by AUTHOR
        self._insert_revision(
            thread, version=2, status="rejected", reviewed_at=BASE_TIME,
            reviewed_by_email="rita@example.com", reviewed_by_name="Rita",
        )
        self._insert_revision(thread, version=3)  # pending — not a facet source
        other = self._seed_thread_widget("rf-unmoderated")
        hidden = self._insert_thread(other.id, status="approved", author_email="hidden@example.com")
        self._insert_revision(
            hidden, version=1, status="approved", origin="original",
            reviewed_at=BASE_TIME, reviewed_by_email="hiddenreviewer@example.com",
        )

        body = self.client.get(self.PATH, headers=self._auth(EDITOR)).json()
        self.assertEqual([p["email"] for p in body["authors"]], [AUTHOR])
        self.assertEqual(
            [p["email"] for p in body["reviewers"]], [AUTHOR, "rita@example.com"]
        )

    def test_viewer_gets_empty_facets(self) -> None:
        self._approved(self._widget("rf-viewer"))
        body = self.client.get(self.PATH, headers=self._auth(VIEWER)).json()
        self.assertEqual(body, {"authors": [], "reviewers": []})


class RevisionSummary(_RevisionFixture):
    PATH = "/data/threads/revision-moderation/summary"

    def test_counts_pending_revisions_in_moderated_set_only(self) -> None:
        moderated = self._widget("rs-in")
        unmoderated = self._seed_thread_widget("rs-out")
        self._insert_thread(moderated.id, status="pending")  # a NEW thread — not counted here
        thread = self._approved(moderated)
        self._insert_revision(thread, version=2)
        self._insert_revision(thread, version=3)
        self._insert_revision(self._approved(unmoderated), version=2)

        resp = self.client.get(self.PATH, headers=self._auth(EDITOR))
        self.assertEqual(resp.json()["pending_count"], 2)
        self.assertTrue(resp.json()["can_moderate"])

    def test_etag_changes_when_a_revision_arrives(self) -> None:
        widget = self._widget("rs-etag")
        thread = self._approved(widget)
        before = self.client.get(self.PATH, headers=self._auth(EDITOR))
        self.assertEqual(before.headers["etag"], '"1-1-0-0"')
        revision = self._stage(thread.id, {"content": "x"})
        after = self.client.get(self.PATH, headers=self._auth(EDITOR))
        self.assertEqual(after.headers["etag"], f'"1-1-1-{revision["id"]}"')
        stale = self.client.get(
            self.PATH, headers={**self._auth(EDITOR), "If-None-Match": before.headers["etag"]}
        )
        self.assertEqual(stale.status_code, 200, stale.text)

    def test_round_trip_304_and_no_store(self) -> None:
        widget = self._widget("rs-304")
        self._insert_revision(self._approved(widget), version=2)
        first = self.client.get(self.PATH, headers=self._auth(EDITOR))
        self.assertEqual(first.headers.get("cache-control"), "no-store")
        cached = self.client.get(
            self.PATH, headers={**self._auth(EDITOR), "If-None-Match": first.headers["etag"]}
        )
        self.assertEqual(cached.status_code, 304, cached.text)
        self.assertEqual(cached.headers.get("cache-control"), "no-store")

    def test_empty_set_etags(self) -> None:
        self._insert_revision(self._approved(self._widget("rs-empty")), version=2)
        viewer = self.client.get(self.PATH, headers=self._auth(VIEWER))
        self.assertEqual(viewer.headers["etag"], '"0-0-0-0"')
        self.assertFalse(viewer.json()["can_moderate"])

    def test_count_drops_after_decision(self) -> None:
        widget = self._widget("rs-drops")
        thread = self._approved(widget)
        revision = self._stage(thread.id, {"content": "x"})
        self.assertEqual(self.client.get(self.PATH, headers=self._auth(EDITOR)).json()["pending_count"], 1)
        self.assertEqual(self._reject(thread.id, revision["id"]).status_code, 200)
        self.assertEqual(self.client.get(self.PATH, headers=self._auth(EDITOR)).json()["pending_count"], 0)


# ---------------------------------------------------------
# Database-level rules, live on SQLite too.
# ---------------------------------------------------------


class ModelConstraints(_RevisionFixture):
    def _expect_integrity_error(self, **kwargs) -> None:
        widget = self._widget(f"model-{len(self.id())}-{kwargs.get('version')}-{kwargs.get('origin')}")
        thread = self._approved(widget)
        with self.assertRaises(IntegrityError):
            self._insert_revision(thread, **kwargs)
        self.db.rollback()

    def test_bogus_status_raises(self) -> None:
        self._expect_integrity_error(version=2, status="withdrawn")

    def test_bogus_origin_raises(self) -> None:
        self._expect_integrity_error(version=2, origin="import")

    def test_only_author_rows_may_be_undecided(self) -> None:
        self._expect_integrity_error(version=2, origin="editor", status="pending")

    def test_version_1_is_exactly_the_original(self) -> None:
        widget = self._widget("model-v1")
        bare = self._insert_thread(widget.id, status="approved")
        with self.assertRaises(IntegrityError):
            self._insert_revision(bare, version=1, origin="author", status="approved")
        self.db.rollback()
        self._expect_integrity_error(version=2, origin="original", status="approved")

    def test_deleting_the_thread_deletes_its_revisions(self) -> None:
        """ON DELETE CASCADE at the DB layer (landmine 9) — through the real
        admin delete endpoint, which has no idea revisions exist."""
        widget = self._widget("model-cascade")
        thread = self._approved(widget)
        self._insert_revision(thread, version=2)
        self._insert_revision(thread, version=3, status="rejected")

        resp = self.client.delete(f"/data/threads/{thread.id}", headers=self._member_auth("admin@example.com"))
        self.assertEqual(resp.status_code, 204, resp.text)
        self.assertEqual(self._all_revisions(thread.id), [])


# ---------------------------------------------------------
# Phase D — revision decision notifications (plan §4.6/§8): the SUBMITTER
# is told when an editor approves / declines their staged edit. One click,
# one row; overwritten revisions produce none; an editor's own direct edit
# produces none (owner, 2026-09-23 — "No notification").
# ---------------------------------------------------------


class DecisionNotifications(_RevisionFixture):
    def _author_rows(self) -> list[NotificationV2]:
        self.db.expire_all()
        return self._notifications_for(AUTHOR)

    def _count(self) -> int:
        self.db.expire_all()
        return self.db.query(NotificationV2).count()

    def test_submitter_notified_on_revision_approve(self) -> None:
        widget = self._widget("dn-approve")
        thread = self._approved(widget)
        content = "the **proposed** body, see `this`"
        revision = self._stage(thread.id, {"content": content})
        self.assertEqual(self._author_rows(), [])  # nothing at submit

        self.assertEqual(self._approve(thread.id, revision["id"]).status_code, 200)
        (row,) = self._author_rows()
        self.assertEqual(row.type, "thread_edit_approved")
        self.assertEqual(row.actor_email, EDITOR)
        self.assertEqual(row.actor_name, "editor")  # the fixture's JWT `name` claim
        self.assertEqual(row.thread_id, thread.id)
        self.assertEqual(row.component_id, widget.id)
        self.assertIsNone(row.comment_id)
        self.assertIsNone(row.read_at)
        self.assertEqual(row.excerpt, thread_service.generate_notification_excerpt(content))
        self.assertEqual(self._notifications_for(EDITOR), [])

    def test_submitter_notified_on_revision_reject(self) -> None:
        """Since plan_thread_reject_resubmit D9 a decline's excerpt is the
        REVIEWER'S REASON (`"Reason: …"`) — neither the declined revision's
        text nor the thread's. (It was the revision's text under versioning
        §4.6; the reason replaced it.)"""
        widget = self._widget("dn-reject")
        thread = self._approved(widget)
        content = "a *declined* proposal"
        revision = self._stage(thread.id, {"content": content})

        self.assertEqual(self._reject(thread.id, revision["id"]).status_code, 200)
        (row,) = self._author_rows()
        self.assertEqual(row.type, "thread_edit_rejected")
        self.assertEqual((row.actor_email, row.actor_name), (EDITOR, "editor"))
        self.assertEqual((row.thread_id, row.component_id), (thread.id, widget.id))
        self.assertIsNone(row.comment_id)
        self.assertEqual(self._live(thread.id).content, "live body")
        self.assertEqual(row.excerpt, "Reason: Needs work")  # the `_reject` helper's reason
        self.assertNotEqual(row.excerpt, thread_service.generate_notification_excerpt(content))

    def test_overwrite_approve_writes_exactly_one_row(self) -> None:
        """One editor click = one bell row, the approved revision's. The
        displaced v2/v3 get nothing (plan §4.6)."""
        widget = self._widget("dn-overwrite")
        thread = self._approved(widget)
        # Legacy shape since 2026-11-10 (U1): seeded, not saved.
        v4 = self._legacy_pending(thread, [f"body {n}" for n in (2, 3, 4)])[-1]

        self.assertEqual(self._approve(thread.id, v4["id"], overwrite=True).status_code, 200)
        self.assertEqual(
            [r.status for r in self._revisions(thread.id)], ["overwritten", "overwritten", "approved"]
        )
        (row,) = self._author_rows()
        self.assertEqual(row.type, "thread_edit_approved")
        self.assertEqual(row.excerpt, thread_service.generate_notification_excerpt("body 4"))
        self.assertEqual(self._count(), 1)

    def test_self_decision_writes_no_row(self) -> None:
        """The self-exclusion pin: the author staged while a viewer, was then
        made an editor, and decides their own revisions — both outcomes."""
        widget = self._widget("dn-self")
        approve_thread = self._approved(widget, title="Approve mine")
        reject_thread = self._approved(widget, title="Reject mine")
        to_approve = self._stage(approve_thread.id, {"content": "mine, approved"})
        to_reject = self._stage(reject_thread.id, {"content": "mine, declined"})
        self._grant(email=AUTHOR, node=("component", widget.id), level="edit")

        self.assertEqual(self._approve(approve_thread.id, to_approve["id"], email=AUTHOR).status_code, 200)
        self.assertEqual(self._reject(reject_thread.id, to_reject["id"], email=AUTHOR).status_code, 200)
        self.assertEqual(self._count(), 0)

    def test_no_row_on_either_409(self) -> None:
        widget = self._widget("dn-409")
        thread = self._approved(widget)
        # Legacy shape since 2026-11-10 (U1): seeded, not saved.
        v2, v3 = self._legacy_pending(thread, ["second", "third"])

        out_of_order = self._approve(thread.id, v3["id"])
        self.assertEqual(out_of_order.status_code, 409, out_of_order.text)
        self.assertEqual(out_of_order.json()["detail"]["code"], "earlier_revisions_pending")
        self.assertEqual(self._count(), 0)

        self.assertEqual(self._approve(thread.id, v2["id"]).status_code, 200)
        self.assertEqual(self._count(), 1)
        for retry in (self._approve(thread.id, v2["id"]), self._reject(thread.id, v2["id"])):
            self.assertEqual(retry.status_code, 409, retry.text)
            self.assertEqual(retry.json()["detail"]["code"], "already_decided")
        self.assertEqual(self._count(), 1)

    def test_mention_actor_is_author_decision_actor_is_reviewer(self) -> None:
        """Two writers, two actors, one approve: the mention row keeps the
        AUTHOR (they did the mentioning), the decision row names the
        REVIEWER (they decided). Not unified."""
        widget = self._widget("dn-mention")
        self._seed_mentionable("alice@example.com", ("component", widget.id), name="Alice Adams")
        thread = self._approved(widget)
        revision = self._stage(
            thread.id, {"content": "hi @AliceAdams", "mentions": [{"email": "alice@example.com"}]}
        )

        self.assertEqual(self._approve(thread.id, revision["id"]).status_code, 200)
        self.db.expire_all()
        (mention,) = self._notifications_for("alice@example.com")
        self.assertEqual((mention.type, mention.actor_email), ("mention", AUTHOR))
        (decision,) = self._author_rows()
        self.assertEqual((decision.type, decision.actor_email), ("thread_edit_approved", EDITOR))

    def test_recipient_is_the_submitter_normalized(self) -> None:
        """Plan §4.6 names the revision's `submitted_by_email`, not the
        thread's author. Equal today (PATCH is author-only), so a seeded row
        is the only way to tell them apart."""
        widget = self._widget("dn-submitter")
        thread = self._approved(widget)
        revision = self._insert_revision(
            thread, version=2, submitted_by_email="Other.Submitter@Example.com"
        )

        self.assertEqual(self._approve(thread.id, revision.id).status_code, 200)
        self.db.expire_all()
        (row,) = self._notifications_for("other.submitter@example.com")
        self.assertEqual(row.recipient_email, "other.submitter@example.com")
        self.assertEqual(self._author_rows(), [])

    def test_row_readable_through_the_real_endpoints(self) -> None:
        widget = self._widget("dn-endpoints")
        thread = self._approved(widget, title="Author's thread")
        content = "proposed *edit* body"
        revision = self._stage(thread.id, {"content": content})
        author = self._auth(AUTHOR)

        before = self.client.get("/data/notifications/unread-count", headers=author)
        self.assertEqual(before.json()["count"], 0)

        self.assertEqual(self._approve(thread.id, revision["id"]).status_code, 200)
        listing = self.client.get("/data/notifications", headers=author)
        self.assertEqual(listing.status_code, 200, listing.text)
        (item,) = listing.json()["items"]
        self.assertEqual(item["type"], "thread_edit_approved")
        self.assertEqual((item["actor_email"], item["actor_name"]), (EDITOR, "editor"))
        self.assertEqual(item["thread_id"], thread.id)
        self.assertEqual(item["thread_title"], "Author's thread")
        self.assertEqual(item["component_link"], "dn-endpoints")
        self.assertEqual(item["excerpt"], thread_service.generate_notification_excerpt(content))
        self.assertIsNone(item["read_at"])

        after = self.client.get("/data/notifications/unread-count", headers=author)
        self.assertEqual(after.json()["count"], 1)
        self.assertNotEqual(after.headers["etag"], before.headers["etag"])

    def test_editor_direct_edit_writes_no_row(self) -> None:
        """Owner, 2026-09-23: an editor's own direct edit (auto-approved
        `origin='editor'`) notifies nobody — no reviewer, nobody waiting.
        Including the edge where their overwrite displaces their OWN earlier
        pending revisions: no row for `overwritten`, same as §4.6."""
        widget = self._widget("dn-editor-direct")
        thread = self._approved(widget)
        self._stage(thread.id, {"content": "drafted as a viewer"})
        self._grant(email=AUTHOR, node=("component", widget.id), level="edit")

        resp = self._patch(thread.id, {"content": "published directly", "overwrite": True})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["applied"])
        again = self._patch(thread.id, {"content": "and again"})
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(
            self._shape(thread.id),
            [(1, "approved", "original"), (2, "overwritten", "author"),
             (3, "approved", "editor"), (4, "approved", "editor")],
        )
        self.assertEqual(self._count(), 0)


if __name__ == "__main__":
    unittest.main()
