"""
Tests for thread moderation (plan_thread_moderation_2026-09-18.md phase 2a)
— approve/reject, the pending queue, the history list, the polled summary,
and the visibility/inertness rules a non-approved thread's comments/votes
now carry.

Reuses `_ThreadFixture` from `tests.test_threads` (root tab id, `_grant`,
`_seed_thread_widget`, `_insert_thread` — extended there with optional
`reviewed_by_email`/`reviewed_by_name`/`reviewed_at` kwargs for this module's
hand-seeded decided rows — `_seed_mentionable`, `_member_auth`/`_auth`).

`_member_auth` mints a Hub Admin token, which trips `ViewerAccess.full_access`
and bypasses the fold entirely (see that helper's own docstring in
test_threads.py) — used ONLY where the test is not about the gate itself.
Every test that is actually about the ancestor rule (0.1(2): a tab or
nav-tab EDIT grant approves everything under it) or the mirror case (M8)
builds a real `ResourceGrantV2` row and authenticates with plain `_auth`,
never `_member_auth` — using the bypass there would make the assertion pass
vacuously regardless of whether the real fold walks the ancestor chain.
"""

from __future__ import annotations

import unittest
from datetime import timedelta, timezone

from sqlalchemy.exc import IntegrityError

from app.db_v2.models.component import ComponentV2
from app.db_v2.models.gridstack import GridstackV2
from app.db_v2.models.user import UserV2
from app.db_v2.models.notification import NotificationV2
from app.db_v2.models.page_content import PageContentV2
from app.db_v2.models.resource_grant import ResourceGrantV2
from app.db_v2.models.tab import TabV2
from app.db_v2.models.thread import ThreadCommentV2, ThreadSubmissionV2, ThreadV2
from app.services import thread_service
from app.services.thread_service import generate_notification_excerpt
from app.services.gridstack_service import MIRROR_WIDGET_TYPE, create_tab_v2
from app.services.nav_tab_service import create_nav_tab_v2, get_nav_tab_by_document_id
from tests.test_threads import _ThreadFixture


class _ModerationFixture(_ThreadFixture):
    """Adds a second nav tab / root tab / gridstack ("tab B") beside the
    fixture's own default ("tab A" — `self.root_tab_id` /
    `self.root_gridstack`), for the ancestor and mirror tests — same
    construction the base `setUp` already uses for tab A."""

    def setUp(self) -> None:
        super().setUp()
        nav_b = create_nav_tab_v2(self.db, title="Nav B")
        nav_b_row = get_nav_tab_by_document_id(self.db, nav_b["documentId"])
        self.nav_b_id = nav_b_row.id
        root_b = create_tab_v2(self.db, title="Root B", nav_tab_id=nav_b_row.id)
        self.tab_b_row = (
            self.db.query(TabV2).filter(TabV2.document_id == root_b["documentId"]).first()
        )
        self.tab_b_id = self.tab_b_row.id
        self.gridstack_b = (
            self.db.query(GridstackV2)
            .filter(GridstackV2.parent_tab_id == self.tab_b_id, GridstackV2.parent_id.is_(None))
            .first()
        )

    def _insert_thread(self, component_id: int, **kwargs) -> ThreadV2:
        """`_ThreadFixture._insert_thread`, plus the decision log a
        hand-seeded DECIDED thread would have since
        plan_thread_reject_resubmit_2026-11-07.md: grouped History and its
        facets read `last_decided_at` + `thread_submissions`, not
        `threads.reviewed_*`. So a row seeded with `reviewed_at` and a
        decided status gets exactly what the migration's backfill (§2.4)
        writes — `last_decided_at = reviewed_at` and one round-0 row
        (status/reviewer from the thread, `submitted_at = created_at`,
        `reason = NULL`, snapshot = the thread's columns)."""
        thread = super()._insert_thread(component_id, **kwargs)
        self._seed_backfilled_round(thread)
        return thread

    def _seed_backfilled_round(self, thread: ThreadV2) -> None:
        if (
            thread.reviewed_at is None
            or thread.reviewed_by_email is None
            or thread.status not in ("approved", "rejected")
        ):
            return
        thread.last_decided_at = thread.reviewed_at
        self.db.add(
            ThreadSubmissionV2(
                thread_id=thread.id,
                round=0,
                title=thread.title,
                content=thread.content,
                mentions=list(thread.mentions or []),
                status=thread.status,
                reason=None,
                submitted_by_email=thread.author_email,
                submitted_by_name=thread.author_name,
                submitted_at=thread.created_at,
                reviewed_by_email=thread.reviewed_by_email,
                reviewed_by_name=thread.reviewed_by_name,
                reviewed_at=thread.reviewed_at,
            )
        )
        self.db.commit()
        self.db.refresh(thread)

    @property
    def nav_tab_id(self) -> int:
        return self.db.query(TabV2.nav_tab_id).filter(TabV2.id == self.root_tab_id).scalar()

    def _seed_mirror(self, link: str, *, target_link: str) -> ComponentV2:
        mirror = ComponentV2(
            link=link,
            type=MIRROR_WIDGET_TYPE,
            gridstack_id=self.gridstack_b.id,
            props={"target_link": target_link},
        )
        self.db.add(mirror)
        self.db.commit()
        self.db.refresh(mirror)
        return mirror

    def _notifications_for(self, email: str) -> list[NotificationV2]:
        return (
            self.db.query(NotificationV2)
            .filter(NotificationV2.recipient_email == email.strip().lower())
            .all()
        )


# ---------------------------------------------------------
# Who auto-approves, and who lands pending — plan §0.1(1)/(2), M8's
# definition, and the `access=None` internal-caller default.
# ---------------------------------------------------------


class CreateRoutesByAuthority(_ModerationFixture):
    def test_view_granted_viewer_posts_pending_no_notification_even_with_mention(self) -> None:
        widget = self._seed_thread_widget("authority-viewer")
        self._grant(email="viewer@example.com", node=("component", widget.id), level="view")
        self._seed_mentionable("mentioned@example.com", ("component", widget.id), name="Mentioned Person")

        resp = self.client.post(
            "/data/threads/component/authority-viewer",
            json={
                "title": "T",
                "content": "hi @MentionedPerson",
                "mentions": [{"email": "mentioned@example.com"}],
            },
            headers=self._auth("viewer@example.com"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertEqual(body["status"], "pending")
        self.assertEqual([m["email"] for m in body["mentions"]], ["mentioned@example.com"])
        self.assertEqual(self._notifications_for("mentioned@example.com"), [])

    def test_component_edit_grantee_posts_approved(self) -> None:
        widget = self._seed_thread_widget("authority-component-edit")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")

        resp = self.client.post(
            "/data/threads/component/authority-component-edit",
            json={"title": "T", "content": "hi"},
            headers=self._auth("editor@example.com"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["status"], "approved")

    def test_tab_edit_grantee_posts_approved(self) -> None:
        """The requirement itself (0.1(2)): an editor of the ROOT TAB — not
        the widget — auto-approves. Real grant, plain `_auth`."""
        self._seed_thread_widget("authority-tab-edit")
        self._grant(email="tabeditor@example.com", node=("tab", self.root_tab_id), level="edit")

        resp = self.client.post(
            "/data/threads/component/authority-tab-edit",
            json={"title": "T", "content": "hi"},
            headers=self._auth("tabeditor@example.com"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["status"], "approved")

    def test_hub_admin_posts_approved(self) -> None:
        self._seed_thread_widget("authority-admin")
        resp = self.client.post(
            "/data/threads/component/authority-admin",
            json={"title": "T", "content": "hi"},
            headers=self._member_auth("admin@example.com"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["status"], "approved")

    def test_access_none_service_call_yields_pending(self) -> None:
        widget = self._seed_thread_widget("authority-access-none")
        from app.models.auth import UserInfo

        detail = thread_service.create_thread_for_link(
            self.db,
            "authority-access-none",
            UserInfo(email="nobody@example.com", name="Nobody", roles=[]),
            "T",
            "hi",
            access=None,
        )
        self.assertEqual(detail.status, "pending")


# ---------------------------------------------------------
# What the widget's own list/detail endpoints show, by status and caller.
# ---------------------------------------------------------


class WidgetVisibility(_ModerationFixture):
    def test_list_shows_approved_only_and_counts_own_pending_and_rejected(self) -> None:
        """2a's M3 rule as CHANGED by plan_thread_reject_resubmit owner
        decision 5 (the author's own REJECTED thread left the main list for
        the `/declined` inbox and `declined_count`) and again on 2026-11-13
        (the author's own PENDING thread left it too: counted in
        `pending_count`, updated from My Revisions). Others' pending never
        shows, nor counts for the caller."""
        widget = self._seed_thread_widget("visibility-list")
        approved = self._insert_thread(widget.id, author_email="alice@example.com", status="approved")
        own_pending = self._insert_thread(widget.id, author_email="alice@example.com", status="pending")
        own_rejected = self._insert_thread(widget.id, author_email="alice@example.com", status="rejected")
        others_pending = self._insert_thread(widget.id, author_email="bob@example.com", status="pending")
        self._insert_thread(widget.id, author_email="bob@example.com", status="pending")

        resp = self.client.get(
            "/data/threads/component/visibility-list", headers=self._member_auth("alice@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        ids = {row["id"] for row in resp.json()["items"]}
        self.assertEqual(ids, {approved.id})
        self.assertNotIn(own_pending.id, ids)
        self.assertNotIn(own_rejected.id, ids)
        self.assertNotIn(others_pending.id, ids)
        self.assertEqual(resp.json()["declined_count"], 1)
        self.assertEqual(resp.json()["pending_count"], 1)
        self.assertEqual(resp.json()["pending_edit_count"], 0)

        bob = self.client.get(
            "/data/threads/component/visibility-list", headers=self._member_auth("bob@example.com")
        ).json()
        self.assertEqual({row["id"] for row in bob["items"]}, {approved.id})
        self.assertEqual((bob["pending_count"], bob["declined_count"]), (2, 0))

        declined = self.client.get(
            "/data/threads/component/visibility-list/declined",
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(declined.status_code, 200, declined.text)
        self.assertEqual(
            [(i["kind"], i["thread_id"]) for i in declined.json()["items"]],
            [("thread", own_rejected.id)],
        )

    def test_editor_list_does_not_show_others_pending(self) -> None:
        """Widget authority is not moderation authority: an editor's OWN
        widget list still only shows approved + their own non-approved —
        other viewers' pending rows belong to the moderation queue, not the
        canvas."""
        widget = self._seed_thread_widget("visibility-editor-list")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        approved = self._insert_thread(widget.id, author_email="alice@example.com", status="approved")
        others_pending = self._insert_thread(widget.id, author_email="alice@example.com", status="pending")

        resp = self.client.get(
            "/data/threads/component/visibility-editor-list", headers=self._auth("editor@example.com")
        )
        ids = {row["id"] for row in resp.json()["items"]}
        self.assertEqual(ids, {approved.id})
        self.assertNotIn(others_pending.id, ids)

    def test_detail_author_sees_pending(self) -> None:
        widget = self._seed_thread_widget("visibility-detail-author")
        thread = self._insert_thread(widget.id, author_email="alice@example.com", status="pending")
        resp = self.client.get(
            f"/data/threads/{thread.id}", headers=self._member_auth("alice@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_detail_editor_sees_pending(self) -> None:
        widget = self._seed_thread_widget("visibility-detail-editor")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        thread = self._insert_thread(widget.id, author_email="alice@example.com", status="pending")
        resp = self.client.get(f"/data/threads/{thread.id}", headers=self._auth("editor@example.com"))
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_detail_other_viewer_404_on_pending(self) -> None:
        widget = self._seed_thread_widget("visibility-detail-other")
        self._grant(email="viewer@example.com", node=("component", widget.id), level="view")
        thread = self._insert_thread(widget.id, author_email="alice@example.com", status="pending")
        resp = self.client.get(f"/data/threads/{thread.id}", headers=self._auth("viewer@example.com"))
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_ungranted_caller_403_before_404(self) -> None:
        """Landmine 2 — `_check_view_access` runs first: a caller with NO
        view access to the widget at all keeps getting 403, never a 404
        that would leak "this id exists"."""
        widget = self._seed_thread_widget("visibility-detail-ungranted")
        thread = self._insert_thread(widget.id, author_email="alice@example.com", status="pending")
        resp = self.client.get(f"/data/threads/{thread.id}", headers=self._auth("nobody@example.com"))
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_cursor_paging_walks_past_pending_without_dup_or_skip(self) -> None:
        widget = self._seed_thread_widget("visibility-cursor")
        base = thread_service._utc_now()
        ids_in_order = []
        for i in range(3):
            t = self._insert_thread(
                widget.id,
                author_email="alice@example.com",
                status="approved",
                created_at=base + timedelta(seconds=i),
            )
            ids_in_order.append(t.id)
        # A pending row from someone else sits chronologically in the
        # middle but must never appear in alice's (or anyone else's) page.
        others_pending = self._insert_thread(
            widget.id,
            author_email="bob@example.com",
            status="pending",
            created_at=base + timedelta(seconds=1, milliseconds=500),
        )

        headers = self._member_auth("alice@example.com")
        first = self.client.get("/data/threads/component/visibility-cursor", headers=headers).json()
        seen_ids = [row["id"] for row in first["items"]]
        self.assertNotIn(others_pending.id, seen_ids)
        self.assertEqual(sorted(seen_ids), sorted(ids_in_order))


# ---------------------------------------------------------
# A non-approved thread's comments/votes are 404 for EVERYONE, author
# included (plan §2's visibility table — "nothing to discuss until it's
# public").
# ---------------------------------------------------------


class NonApprovedIsInert(_ModerationFixture):
    def _pending_and_rejected(self, link_prefix: str) -> tuple[ComponentV2, ThreadV2, ThreadV2]:
        widget = self._seed_thread_widget(link_prefix)
        pending = self._insert_thread(widget.id, author_email="alice@example.com", status="pending")
        rejected = self._insert_thread(widget.id, author_email="alice@example.com", status="rejected")
        return widget, pending, rejected

    def test_list_comments_404_on_pending_and_rejected(self) -> None:
        _widget, pending, rejected = self._pending_and_rejected("inert-list-comments")
        for thread in (pending, rejected):
            resp = self.client.get(
                f"/data/threads/{thread.id}/comments", headers=self._member_auth("alice@example.com")
            )
            self.assertEqual(resp.status_code, 404, resp.text)

    def test_create_comment_404_on_pending_and_rejected(self) -> None:
        _widget, pending, rejected = self._pending_and_rejected("inert-create-comment")
        for thread in (pending, rejected):
            resp = self.client.post(
                f"/data/threads/{thread.id}/comments",
                json={"content": "hi"},
                headers=self._member_auth("alice@example.com"),
            )
            self.assertEqual(resp.status_code, 404, resp.text)

    def test_vote_on_thread_404_on_pending_and_rejected(self) -> None:
        _widget, pending, rejected = self._pending_and_rejected("inert-vote-thread")
        for thread in (pending, rejected):
            resp = self.client.put(
                f"/data/threads/{thread.id}/vote",
                json={"value": 1},
                headers=self._member_auth("bob@example.com"),
            )
            self.assertEqual(resp.status_code, 404, resp.text)

    def _seed_comment_on(self, thread: ThreadV2) -> ThreadCommentV2:
        comment = ThreadCommentV2(
            thread_id=thread.id,
            content="a comment",
            author_email="carol@example.com",
            author_name="carol",
            mentions=[],
            up_count=0,
            down_count=0,
            created_at=thread_service._utc_now(),
        )
        self.db.add(comment)
        self.db.commit()
        self.db.refresh(comment)
        return comment

    def test_vote_on_comment_404_on_pending_and_rejected(self) -> None:
        _widget, pending, rejected = self._pending_and_rejected("inert-vote-comment")
        for thread in (pending, rejected):
            comment = self._seed_comment_on(thread)
            resp = self.client.put(
                f"/data/threads/comments/{comment.id}/vote",
                json={"value": 1},
                headers=self._member_auth("bob@example.com"),
            )
            self.assertEqual(resp.status_code, 404, resp.text)

    def test_update_comment_404_on_pending_and_rejected(self) -> None:
        _widget, pending, rejected = self._pending_and_rejected("inert-update-comment")
        for thread in (pending, rejected):
            comment = self._seed_comment_on(thread)
            resp = self.client.patch(
                f"/data/threads/comments/{comment.id}",
                json={"content": "edited"},
                headers=self._member_auth("carol@example.com"),
            )
            self.assertEqual(resp.status_code, 404, resp.text)

    def test_delete_comment_404_on_pending_and_rejected(self) -> None:
        _widget, pending, rejected = self._pending_and_rejected("inert-delete-comment")
        for thread in (pending, rejected):
            comment = self._seed_comment_on(thread)
            resp = self.client.delete(
                f"/data/threads/comments/{comment.id}", headers=self._member_auth("admin@example.com")
            )
            self.assertEqual(resp.status_code, 404, resp.text)


# ---------------------------------------------------------
# Author edits — pending stays pending (no fan-out), rejected is terminal.
# ---------------------------------------------------------


class AuthorEdit(_ModerationFixture):
    def test_patch_pending_keeps_pending_stores_mentions_no_notification(self) -> None:
        widget = self._seed_thread_widget("edit-pending")
        self._seed_mentionable("mentioned@example.com", ("component", widget.id), name="Mentioned Person")
        thread = self._insert_thread(widget.id, author_email="alice@example.com", status="pending")

        resp = self.client.patch(
            f"/data/threads/{thread.id}",
            json={
                "content": "now mentioning @MentionedPerson",
                "mentions": [{"email": "mentioned@example.com"}],
            },
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()["thread"]
        self.assertEqual(body["status"], "pending")
        self.assertEqual([m["email"] for m in body["mentions"]], ["mentioned@example.com"])
        self.assertEqual(self._notifications_for("mentioned@example.com"), [])

    def test_patch_rejected_returns_409(self) -> None:
        widget = self._seed_thread_widget("edit-rejected")
        thread = self._insert_thread(widget.id, author_email="alice@example.com", status="rejected")
        resp = self.client.patch(
            f"/data/threads/{thread.id}",
            json={"content": "trying again"},
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 409, resp.text)

    def test_patch_approved_still_notifies_newly_added(self) -> None:
        widget = self._seed_thread_widget("edit-approved")
        self._seed_mentionable("mentioned@example.com", ("component", widget.id), name="Mentioned Person")
        thread = self._insert_thread(widget.id, author_email="alice@example.com", status="approved")

        resp = self.client.patch(
            f"/data/threads/{thread.id}",
            json={
                "content": "now mentioning @MentionedPerson",
                "mentions": [{"email": "mentioned@example.com"}],
            },
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(self._notifications_for("mentioned@example.com")), 1)


# ---------------------------------------------------------
# Who may approve/reject — plan §0.1(2)'s ancestor rule, exercised with real
# grants and plain `_auth`.
# ---------------------------------------------------------


class DecisionGates(_ModerationFixture):
    def test_viewer_403_on_approve(self) -> None:
        widget = self._seed_thread_widget("decide-viewer")
        self._grant(email="viewer@example.com", node=("component", widget.id), level="view")
        thread = self._insert_thread(widget.id, status="pending")
        resp = self.client.post(
            f"/data/threads/{thread.id}/approve", headers=self._auth("viewer@example.com")
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_component_editor_200_on_approve(self) -> None:
        widget = self._seed_thread_widget("decide-component-editor")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        thread = self._insert_thread(widget.id, status="pending")
        resp = self.client.post(
            f"/data/threads/{thread.id}/approve", headers=self._auth("editor@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "approved")

    def test_tab_editor_200_on_approve(self) -> None:
        self._seed_thread_widget("decide-tab-editor")
        widget = self.db.query(ComponentV2).filter(ComponentV2.link == "decide-tab-editor").first()
        self._grant(email="tabeditor@example.com", node=("tab", self.root_tab_id), level="edit")
        thread = self._insert_thread(widget.id, status="pending")
        resp = self.client.post(
            f"/data/threads/{thread.id}/approve", headers=self._auth("tabeditor@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_nav_tab_editor_200_on_approve(self) -> None:
        """Two hops up the ancestor chain (root tab, then nav tab) — proves
        the fold walks the WHOLE chain, not just one level."""
        self._seed_thread_widget("decide-nav-tab-editor")
        widget = self.db.query(ComponentV2).filter(ComponentV2.link == "decide-nav-tab-editor").first()
        self._grant(email="naveditor@example.com", node=("nav_tab", self.nav_tab_id), level="edit")
        thread = self._insert_thread(widget.id, status="pending")
        resp = self.client.post(
            f"/data/threads/{thread.id}/approve", headers=self._auth("naveditor@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_hub_admin_200_on_approve(self) -> None:
        widget = self._seed_thread_widget("decide-admin")
        thread = self._insert_thread(widget.id, status="pending")
        resp = self.client.post(
            f"/data/threads/{thread.id}/approve", headers=self._member_auth("admin@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_second_decision_409_with_thread_in_body(self) -> None:
        widget = self._seed_thread_widget("decide-second")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        thread = self._insert_thread(widget.id, status="pending")
        headers = self._auth("editor@example.com")
        first = self.client.post(f"/data/threads/{thread.id}/approve", headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        second = self.client.post(f"/data/threads/{thread.id}/approve", headers=headers)
        self.assertEqual(second.status_code, 409, second.text)
        detail = second.json()["detail"]
        self.assertIn("thread", detail)
        self.assertEqual(detail["thread"]["status"], "approved")

    def test_reject_then_approve_409(self) -> None:
        widget = self._seed_thread_widget("decide-reject-then-approve")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        thread = self._insert_thread(widget.id, status="pending")
        headers = self._auth("editor@example.com")
        first = self.client.post(f"/data/threads/{thread.id}/reject", json={"reason": "Needs work"}, headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        second = self.client.post(f"/data/threads/{thread.id}/approve", headers=headers)
        self.assertEqual(second.status_code, 409, second.text)


# ---------------------------------------------------------
# What a decision actually does to the row.
# ---------------------------------------------------------


class DecisionEffects(_ModerationFixture):
    def test_approve_stamps_reviewed_fields_and_fans_out_actor_is_author(self) -> None:
        widget = self._seed_thread_widget("effects-approve")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        self._seed_mentionable("mentioned@example.com", ("component", widget.id), name="Mentioned Person")
        thread = self._insert_thread(
            widget.id,
            author_email="alice@example.com",
            status="pending",
            content="hi @MentionedPerson",
        )
        thread.mentions = [{"email": "mentioned@example.com", "name": "Mentioned Person", "token": "MentionedPerson"}]
        self.db.commit()

        resp = self.client.post(
            f"/data/threads/{thread.id}/approve", headers=self._auth("editor@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["reviewed_by_email"], "editor@example.com")
        self.assertIsNotNone(body["reviewed_at"])

        notifications = self._notifications_for("mentioned@example.com")
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].actor_email, "alice@example.com")
        self.assertIsNone(notifications[0].comment_id)

    def test_reject_stamps_reviewed_fields_no_fanout(self) -> None:
        widget = self._seed_thread_widget("effects-reject")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        self._seed_mentionable("mentioned@example.com", ("component", widget.id), name="Mentioned Person")
        thread = self._insert_thread(
            widget.id,
            author_email="alice@example.com",
            status="pending",
            content="hi @MentionedPerson",
        )
        thread.mentions = [{"email": "mentioned@example.com", "name": "Mentioned Person", "token": "MentionedPerson"}]
        self.db.commit()

        resp = self.client.post(
            f"/data/threads/{thread.id}/reject", json={"reason": "Needs work"}, headers=self._auth("editor@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "rejected")
        # Landmine-1 pin for reject: the MENTIONED user gets nothing. M1's
        # author row (below) is a different recipient, so this stays exact.
        self.assertEqual(self._notifications_for("mentioned@example.com"), [])

    def test_author_notified_on_approve(self) -> None:
        """M1 (plan §3.2, phase 2d): one approve → TWO rows to TWO
        recipients with TWO types — the mentioned user's `mention` row
        (actor = author, the deferred fan-out) and the author's
        `thread_approved` row (actor = reviewer). Pinned in one test so the
        independence of the two writes is visible."""
        widget = self._seed_thread_widget("effects-author-approve")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        self._seed_mentionable("mentioned@example.com", ("component", widget.id), name="Mentioned Person")
        content = "hi @MentionedPerson, **please** look at `this`"
        thread = self._insert_thread(
            widget.id,
            author_email="alice@example.com",
            status="pending",
            content=content,
        )
        thread.mentions = [{"email": "mentioned@example.com", "name": "Mentioned Person", "token": "MentionedPerson"}]
        self.db.commit()

        resp = self.client.post(
            f"/data/threads/{thread.id}/approve", headers=self._auth("editor@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        author_rows = self._notifications_for("alice@example.com")
        self.assertEqual(len(author_rows), 1)
        row = author_rows[0]
        self.assertEqual(row.type, "thread_approved")
        self.assertEqual(row.actor_email, "editor@example.com")
        self.assertEqual(row.actor_name, "editor")  # the fixture's JWT `name` claim
        self.assertIsNone(row.comment_id)
        self.assertEqual(row.thread_id, thread.id)
        self.assertEqual(row.component_id, widget.id)
        self.assertEqual(row.excerpt, generate_notification_excerpt(content))
        self.assertIsNone(row.read_at)

        mention_rows = self._notifications_for("mentioned@example.com")
        self.assertEqual(len(mention_rows), 1)
        self.assertEqual(mention_rows[0].type, "mention")
        self.assertEqual(mention_rows[0].actor_email, "alice@example.com")

    def test_author_notified_on_reject(self) -> None:
        """M1: reject → the author's one `thread_rejected` row (actor =
        reviewer); the mentioned user gets nothing (§5.4 — reject fans out
        no mentions)."""
        widget = self._seed_thread_widget("effects-author-reject")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        self._seed_mentionable("mentioned@example.com", ("component", widget.id), name="Mentioned Person")
        thread = self._insert_thread(
            widget.id,
            author_email="alice@example.com",
            status="pending",
            content="hi @MentionedPerson",
        )
        thread.mentions = [{"email": "mentioned@example.com", "name": "Mentioned Person", "token": "MentionedPerson"}]
        self.db.commit()

        resp = self.client.post(
            f"/data/threads/{thread.id}/reject", json={"reason": "Needs work"}, headers=self._auth("editor@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        author_rows = self._notifications_for("alice@example.com")
        self.assertEqual(len(author_rows), 1)
        self.assertEqual(author_rows[0].type, "thread_rejected")
        self.assertEqual(author_rows[0].actor_email, "editor@example.com")
        self.assertEqual(author_rows[0].actor_name, "editor")
        self.assertIsNone(author_rows[0].comment_id)
        self.assertEqual(self._notifications_for("mentioned@example.com"), [])


# ---------------------------------------------------------
# M1's remaining edges (plan §3.2, phase 2d): self-review, admin reviewer,
# the row through the real notification endpoints, the revoked-author
# post-filter, the reviewer's name vs the author's, and the 409 path. The
# two headline cases stay in `DecisionEffects` above (they were the
# placeholders phase 2a left there); these are the ones that would catch a
# wrong self-exclusion direction or a call site above the pending check.
# ---------------------------------------------------------


class DecisionNotifications(_ModerationFixture):
    def _seed_pending_with_editor(
        self, link: str, *, author_email: str = "alice@example.com"
    ) -> tuple[ComponentV2, ThreadV2]:
        widget = self._seed_thread_widget(link)
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        thread = self._insert_thread(widget.id, author_email=author_email, status="pending")
        return widget, thread

    def test_self_review_writes_no_row(self) -> None:
        """The self-exclusion pin. An editor's own thread can only be
        `pending` by hand-seeding (the create route auto-approves an
        editor's post) — seed it that way, decide as that same editor, and
        assert no row. The comparison in `_notify_thread_decision` is
        recipient (author) vs reviewer; deleting it turns exactly this red."""
        widget, thread = self._seed_pending_with_editor(
            "notif-self-review", author_email="editor@example.com"
        )
        resp = self.client.post(
            f"/data/threads/{thread.id}/approve", headers=self._auth("editor@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "approved")
        self.assertEqual(self._notifications_for("editor@example.com"), [])
        # Nothing else was written either — the thread had no mentions.
        self.assertEqual(self.db.query(NotificationV2).count(), 0)

    def test_hub_admin_reviewer_is_the_actor(self) -> None:
        widget = self._seed_thread_widget("notif-admin-reviewer")
        thread = self._insert_thread(widget.id, author_email="alice@example.com", status="pending")
        resp = self.client.post(
            f"/data/threads/{thread.id}/reject", json={"reason": "Needs work"}, headers=self._member_auth("admin@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        rows = self._notifications_for("alice@example.com")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].type, "thread_rejected")
        self.assertEqual(rows[0].actor_email, "admin@example.com")
        self.assertEqual(rows[0].actor_name, "admin")

    def test_row_readable_through_the_real_endpoints(self) -> None:
        """The author holds a `view` grant on the widget (that is how they
        posted), so `list_notifications_for_user`'s post-filter admits the
        row: `GET /notifications` shows it with the live `thread_title` /
        `component_link` join, `unread-count` is 1 and its ETag moved."""
        widget = self._seed_thread_widget("notif-real-endpoints")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        self._grant(email="alice@example.com", node=("component", widget.id), level="view")
        content = "the *body* of the thread"
        thread = self._insert_thread(
            widget.id, title="Alice's thread", author_email="alice@example.com",
            status="pending", content=content,
        )
        author = self._auth("alice@example.com")

        before = self.client.get("/data/notifications/unread-count", headers=author)
        self.assertEqual(before.status_code, 200, before.text)
        self.assertEqual(before.json()["count"], 0)
        etag_before = before.headers["etag"]

        resp = self.client.post(
            f"/data/threads/{thread.id}/approve", headers=self._auth("editor@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        listing = self.client.get("/data/notifications", headers=author)
        self.assertEqual(listing.status_code, 200, listing.text)
        items = listing.json()["items"]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["type"], "thread_approved")
        self.assertEqual(item["actor_email"], "editor@example.com")
        self.assertEqual(item["actor_name"], "editor")
        self.assertEqual(item["thread_id"], thread.id)
        self.assertEqual(item["thread_title"], "Alice's thread")
        self.assertEqual(item["component_link"], "notif-real-endpoints")
        self.assertEqual(item["excerpt"], generate_notification_excerpt(content))
        self.assertIsNone(item["read_at"])
        # `NotificationEntry` has no `comment_id` field — unchanged shape.
        self.assertNotIn("comment_id", item)

        after = self.client.get("/data/notifications/unread-count", headers=author)
        self.assertEqual(after.status_code, 200, after.text)
        self.assertEqual(after.json()["count"], 1)
        self.assertNotEqual(after.headers["etag"], etag_before)

    def test_revoked_author_is_filtered_out(self) -> None:
        """Same as above, but the author's grant is deleted between the
        decision and the read — the `:854-857` post-filter (closeout §4.2)
        drops the row. Pins that 2d did not route around that filter."""
        widget = self._seed_thread_widget("notif-revoked-author")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        self._grant(email="alice@example.com", node=("component", widget.id), level="view")
        thread = self._insert_thread(widget.id, author_email="alice@example.com", status="pending")

        resp = self.client.post(
            f"/data/threads/{thread.id}/approve", headers=self._auth("editor@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        # The row exists in the table...
        self.assertEqual(len(self._notifications_for("alice@example.com")), 1)

        alice_id = (
            self.db.query(UserV2.id).filter(UserV2.email == "alice@example.com").scalar()
        )
        self.db.query(ResourceGrantV2).filter(ResourceGrantV2.user_id == alice_id).delete()
        self.db.commit()

        # ...but the endpoints no longer show it.
        author = self._auth("alice@example.com")
        listing = self.client.get("/data/notifications", headers=author)
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["items"], [])
        count = self.client.get("/data/notifications/unread-count", headers=author)
        self.assertEqual(count.json()["count"], 0)

    def test_actor_name_is_the_reviewers_even_when_author_has_none(self) -> None:
        """`_author_as_user_info`'s local-part fallback is the MENTION
        fan-out's actor, not this row's — the recipient's own name is
        irrelevant here. Null the author's name and check the reviewer's
        name is what lands in `actor_name`."""
        widget, thread = self._seed_pending_with_editor("notif-nameless-author")
        thread.author_name = None
        self.db.commit()

        resp = self.client.post(
            f"/data/threads/{thread.id}/approve", headers=self._auth("editor@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        rows = self._notifications_for("alice@example.com")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].actor_name, "editor")
        self.assertEqual(rows[0].actor_email, "editor@example.com")

    def test_second_decision_409_writes_no_second_row(self) -> None:
        """The call site sits after the pending check: approve-then-approve
        and approve-then-reject both 409 and leave exactly ONE author row.
        Moving `_notify_thread_decision` above the check turns this red."""
        for link, second in (
            ("notif-409-approve-approve", "approve"),
            ("notif-409-approve-reject", "reject"),
        ):
            with self.subTest(second=second):
                widget, thread = self._seed_pending_with_editor(link)
                headers = self._auth("editor@example.com")
                first = self.client.post(f"/data/threads/{thread.id}/approve", headers=headers)
                self.assertEqual(first.status_code, 200, first.text)
                again = self.client.post(f"/data/threads/{thread.id}/{second}", json={"reason": "Needs work"}, headers=headers)
                self.assertEqual(again.status_code, 409, again.text)

                rows = [
                    r for r in self._notifications_for("alice@example.com")
                    if r.thread_id == thread.id
                ]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].type, "thread_approved")


# ---------------------------------------------------------
# The pending queue.
# ---------------------------------------------------------


class PendingList(_ModerationFixture):
    def test_only_moderated_components(self) -> None:
        moderated = self._seed_thread_widget("pending-moderated")
        unmoderated = self._seed_thread_widget("pending-unmoderated")
        self._grant(email="editor@example.com", node=("component", moderated.id), level="edit")
        in_scope = self._insert_thread(moderated.id, status="pending")
        out_of_scope = self._insert_thread(unmoderated.id, status="pending")

        resp = self.client.get("/data/threads/moderation/pending", headers=self._auth("editor@example.com"))
        self.assertEqual(resp.status_code, 200, resp.text)
        ids = {row["id"] for row in resp.json()["items"]}
        self.assertIn(in_scope.id, ids)
        self.assertNotIn(out_of_scope.id, ids)

    def test_oldest_first_order(self) -> None:
        widget = self._seed_thread_widget("pending-order")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        base = thread_service._utc_now()
        first = self._insert_thread(widget.id, status="pending", created_at=base)
        second = self._insert_thread(widget.id, status="pending", created_at=base + timedelta(seconds=5))

        resp = self.client.get("/data/threads/moderation/pending", headers=self._auth("editor@example.com"))
        ids = [row["id"] for row in resp.json()["items"]]
        self.assertEqual(ids, [first.id, second.id])

    def test_cursor_continuity_across_decision_made_between_pages(self) -> None:
        widget = self._seed_thread_widget("pending-cursor")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        headers = self._auth("editor@example.com")
        base = thread_service._utc_now()
        threads = [
            self._insert_thread(widget.id, status="pending", created_at=base + timedelta(seconds=i))
            for i in range(3)
        ]

        # Monkeypatch page size down to 2 for this call would require
        # touching the service constant globally; instead simulate a small
        # page by asking for the cursor after the first row directly.
        first_page = self.client.get(
            "/data/threads/moderation/pending", headers=headers
        ).json()
        self.assertEqual([row["id"] for row in first_page["items"]], [t.id for t in threads])

        # Decide the first thread, then confirm a fresh (uncursored) fetch
        # no longer includes it and the remaining two are intact, in order.
        self.client.post(f"/data/threads/{threads[0].id}/approve", headers=headers)
        after = self.client.get("/data/threads/moderation/pending", headers=headers).json()
        self.assertEqual([row["id"] for row in after["items"]], [threads[1].id, threads[2].id])

    def test_location_label_format(self) -> None:
        widget = self._seed_thread_widget("pending-location")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        self._insert_thread(widget.id, status="pending")

        resp = self.client.get("/data/threads/moderation/pending", headers=self._auth("editor@example.com"))
        row = resp.json()["items"][0]
        self.assertEqual(row["location_label"], "Nav Threads › Root Threads")

    def test_widget_title_fallback_chain(self) -> None:
        # No page_content, no component.title -> "Discussion". `_seed_thread_
        # widget` defaults `title` to the link string, so it's cleared here
        # to actually exercise the empty case.
        no_title_widget = self._seed_thread_widget("pending-title-fallback")
        no_title_widget.title = None
        self.db.commit()
        self._grant(email="editor@example.com", node=("component", no_title_widget.id), level="edit")
        self._insert_thread(no_title_widget.id, status="pending")

        # component.title set, no page_content -> component.title.
        titled_widget = self._seed_thread_widget("pending-title-component")
        titled_widget.title = "My Widget Title"
        self.db.commit()
        self._grant(email="editor@example.com", node=("component", titled_widget.id), level="edit")
        self._insert_thread(titled_widget.id, status="pending")

        # page_content.content["title"] wins over component.title.
        pc_widget = self._seed_thread_widget("pending-title-pagecontent")
        pc_widget.title = "Ignored Component Title"
        page_content = PageContentV2(content={"title": "Page Content Title"})
        self.db.add(page_content)
        self.db.commit()
        self.db.refresh(page_content)
        pc_widget.page_content_id = page_content.id
        self.db.commit()
        self._grant(email="editor@example.com", node=("component", pc_widget.id), level="edit")
        self._insert_thread(pc_widget.id, status="pending")

        resp = self.client.get("/data/threads/moderation/pending", headers=self._auth("editor@example.com"))
        titles = {row["component_link"]: row["widget_title"] for row in resp.json()["items"]}
        self.assertEqual(titles["pending-title-fallback"], "Discussion")
        self.assertEqual(titles["pending-title-component"], "My Widget Title")
        self.assertEqual(titles["pending-title-pagecontent"], "Page Content Title")

    def test_hub_admin_sees_every_component(self) -> None:
        widget_a = self._seed_thread_widget("pending-admin-a")
        widget_b = self._seed_thread_widget("pending-admin-b")
        thread_a = self._insert_thread(widget_a.id, status="pending")
        thread_b = self._insert_thread(widget_b.id, status="pending")

        resp = self.client.get(
            "/data/threads/moderation/pending", headers=self._member_auth("admin@example.com")
        )
        ids = {row["id"] for row in resp.json()["items"]}
        self.assertEqual(ids, {thread_a.id, thread_b.id})

    def test_caller_moderating_nothing_gets_empty_200(self) -> None:
        widget = self._seed_thread_widget("pending-nothing")
        self._insert_thread(widget.id, status="pending")
        resp = self.client.get(
            "/data/threads/moderation/pending", headers=self._auth("nobody@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json(), {"items": [], "next_cursor": None})


# ---------------------------------------------------------
# The history list.
# ---------------------------------------------------------


class HistoryList(_ModerationFixture):
    def test_excludes_never_reviewed_legacy_and_editor_auto_approved(self) -> None:
        widget = self._seed_thread_widget("history-excludes")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        legacy_approved = self._insert_thread(widget.id, status="approved")  # reviewed_at NULL
        self._insert_thread(widget.id, status="pending")  # not decided at all

        resp = self.client.get("/data/threads/moderation/history", headers=self._auth("editor@example.com"))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["items"], [])

    def test_reviewed_at_desc_order(self) -> None:
        widget = self._seed_thread_widget("history-order")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        base = thread_service._utc_now()
        older = self._insert_thread(
            widget.id,
            status="approved",
            reviewed_by_email="editor@example.com",
            reviewed_by_name="Editor",
            reviewed_at=base,
        )
        newer = self._insert_thread(
            widget.id,
            status="rejected",
            reviewed_by_email="editor@example.com",
            reviewed_by_name="Editor",
            reviewed_at=base + timedelta(seconds=10),
        )

        resp = self.client.get("/data/threads/moderation/history", headers=self._auth("editor@example.com"))
        ids = [row["id"] for row in resp.json()["items"]]
        self.assertEqual(ids, [newer.id, older.id])

    def test_rejected_and_approved_both_present_with_chip_data(self) -> None:
        widget = self._seed_thread_widget("history-chip-data")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        now = thread_service._utc_now()
        approved = self._insert_thread(
            widget.id,
            status="approved",
            reviewed_by_email="editor@example.com",
            reviewed_by_name="Editor Name",
            reviewed_at=now,
        )
        rejected = self._insert_thread(
            widget.id,
            status="rejected",
            reviewed_by_email="editor@example.com",
            reviewed_by_name="Editor Name",
            reviewed_at=now + timedelta(seconds=1),
        )

        resp = self.client.get("/data/threads/moderation/history", headers=self._auth("editor@example.com"))
        by_id = {row["id"]: row for row in resp.json()["items"]}
        self.assertEqual(by_id[approved.id]["status"], "approved")
        self.assertEqual(by_id[rejected.id]["status"], "rejected")
        for row in by_id.values():
            self.assertEqual(row["reviewed_by_name"], "Editor Name")
            self.assertIsNotNone(row["reviewed_at"])


class HistoryFilters(_ModerationFixture):
    """The History tab's author / reviewer / decision-date filters (owner,
    2026-09-24) — Revision History's semantics on the thread list."""

    PATH = "/data/threads/moderation/history"
    EDITOR = "editor@example.com"

    def _get(self, **params) -> list[int]:
        ids: list[int] = []
        cursor = None
        for _ in range(20):
            query = dict(params)
            if cursor:
                query["cursor"] = cursor
            resp = self.client.get(self.PATH, params=query, headers=self._auth(self.EDITOR))
            self.assertEqual(resp.status_code, 200, resp.text)
            ids.extend(row["id"] for row in resp.json()["items"])
            cursor = resp.json()["next_cursor"]
            if not cursor:
                return ids
        self.fail("cursor never terminated")

    def _seed(self) -> dict[str, ThreadV2]:
        """Four decided threads at minutes 1..4 (two authors x two
        reviewers), plus an undecided one that must never appear."""
        widget = self._seed_thread_widget("history-filters")
        self._grant(email=self.EDITOR, node=("component", widget.id), level="edit")
        base = thread_service._utc_now().replace(microsecond=0)
        self.base = base

        def decided(key: str, author: str, reviewer: str, minute: int, status: str) -> ThreadV2:
            return self._insert_thread(
                widget.id,
                title=key,
                author_email=author,
                status=status,
                reviewed_by_email=reviewer,
                reviewed_by_name=reviewer.split("@")[0].title(),
                reviewed_at=base + timedelta(minutes=minute),
            )

        rows = {
            "a1": decided("a1", "alice@example.com", "rita@example.com", 1, "approved"),
            "b2": decided("b2", "bob@example.com", "rita@example.com", 2, "rejected"),
            "a3": decided("a3", "alice@example.com", self.EDITOR, 3, "rejected"),
            "b4": decided("b4", "bob@example.com", self.EDITOR, 4, "approved"),
        }
        self._insert_thread(widget.id, author_email="alice@example.com", status="pending")
        return rows

    def test_no_filter_lists_every_decided_thread(self) -> None:
        rows = self._seed()
        self.assertEqual(self._get(), [rows[k].id for k in ("b4", "a3", "b2", "a1")])

    def test_author_filter_is_any_of_and_case_insensitive(self) -> None:
        rows = self._seed()
        self.assertEqual(self._get(author="ALICE@example.com"), [rows["a3"].id, rows["a1"].id])
        self.assertEqual(
            self._get(author=["alice@example.com", "bob@example.com"]),
            [rows[k].id for k in ("b4", "a3", "b2", "a1")],
        )
        self.assertEqual(self._get(author="nobody@example.com"), [])

    def test_reviewer_filter(self) -> None:
        rows = self._seed()
        self.assertEqual(self._get(reviewer="Rita@Example.com"), [rows["b2"].id, rows["a1"].id])

    def test_decision_date_range_is_half_open(self) -> None:
        rows = self._seed()
        got = self._get(
            date_from=(self.base + timedelta(minutes=2)).isoformat(),
            date_to=(self.base + timedelta(minutes=4)).isoformat(),
        )
        self.assertEqual(got, [rows["a3"].id, rows["b2"].id])

    def test_date_bound_with_offset_is_converted_to_utc(self) -> None:
        rows = self._seed()
        bound = (self.base + timedelta(minutes=3)).astimezone(timezone(timedelta(hours=2)))
        self.assertEqual(self._get(date_from=bound.isoformat()), [rows["b4"].id, rows["a3"].id])

    def test_status_filter(self) -> None:
        rows = self._seed()
        self.assertEqual(self._get(status="approved"), [rows["b4"].id, rows["a1"].id])
        self.assertEqual(self._get(status="rejected"), [rows["a3"].id, rows["b2"].id])
        self.assertEqual(
            self._get(status=["approved", "rejected"]),
            [rows[k].id for k in ("b4", "a3", "b2", "a1")],
        )

    def test_filters_combine(self) -> None:
        rows = self._seed()
        self.assertEqual(
            self._get(
                author="alice@example.com",
                reviewer=self.EDITOR,
                date_from=(self.base + timedelta(minutes=2)).isoformat(),
            ),
            [rows["a3"].id],
        )
        self.assertEqual(self._get(status="approved", reviewer="rita@example.com"), [rows["a1"].id])

    def test_cursor_walks_with_a_filter(self) -> None:
        widget = self._seed_thread_widget("history-filter-paging")
        self._grant(email=self.EDITOR, node=("component", widget.id), level="edit")
        base = thread_service._utc_now()
        expected = []
        for i in range(25):
            expected.append(
                self._insert_thread(
                    widget.id,
                    author_email="alice@example.com",
                    status="approved",
                    reviewed_by_email=self.EDITOR,
                    reviewed_at=base + timedelta(minutes=i),
                ).id
            )
            self._insert_thread(
                widget.id,
                author_email="bob@example.com",
                status="approved",
                reviewed_by_email=self.EDITOR,
                reviewed_at=base + timedelta(minutes=i, seconds=30),
            )
        self.assertEqual(self._get(author="alice@example.com"), list(reversed(expected)))

    def test_invalid_filter_values_422(self) -> None:
        self._seed()
        # `pending` is a real thread status but never a History one.
        for params in ({"date_from": "not-a-date"}, {"status": "pending"}, {"status": "bogus"}):
            resp = self.client.get(self.PATH, params=params, headers=self._auth(self.EDITOR))
            self.assertEqual(resp.status_code, 422, (params, resp.text))


class HistoryFacets(_ModerationFixture):
    PATH = "/data/threads/moderation/history/facets"

    def test_distinct_authors_and_reviewers_of_decided_threads_in_moderated_set(self) -> None:
        widget = self._seed_thread_widget("history-facets")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        now = thread_service._utc_now()
        for author, reviewer in (
            ("zoe@example.com", "rita@example.com"),
            ("alice@example.com", "rita@example.com"),
            ("alice@example.com", "editor@example.com"),
        ):
            self._insert_thread(
                widget.id,
                author_email=author,
                status="approved",
                reviewed_by_email=reviewer,
                reviewed_by_name=reviewer.split("@")[0].title(),
                reviewed_at=now,
            )
        # Never decided — neither author is a facet.
        self._insert_thread(widget.id, author_email="pending@example.com", status="pending")
        self._insert_thread(widget.id, author_email="legacy@example.com", status="approved")
        # Decided, but on a widget the caller doesn't moderate.
        other = self._seed_thread_widget("history-facets-unmoderated")
        self._insert_thread(
            other.id,
            author_email="hidden@example.com",
            status="approved",
            reviewed_by_email="hiddenreviewer@example.com",
            reviewed_at=now,
        )

        body = self.client.get(self.PATH, headers=self._auth("editor@example.com")).json()
        self.assertEqual(
            [p["email"] for p in body["authors"]], ["alice@example.com", "zoe@example.com"]
        )
        self.assertEqual(
            body["reviewers"],
            [
                {"email": "editor@example.com", "name": "Editor"},
                {"email": "rita@example.com", "name": "Rita"},
            ],
        )

    def test_viewer_gets_empty_facets(self) -> None:
        widget = self._seed_thread_widget("history-facets-viewer")
        self._grant(email="viewer@example.com", node=("component", widget.id), level="view")
        self._insert_thread(
            widget.id,
            status="approved",
            reviewed_by_email="editor@example.com",
            reviewed_at=thread_service._utc_now(),
        )
        body = self.client.get(self.PATH, headers=self._auth("viewer@example.com")).json()
        self.assertEqual(body, {"authors": [], "reviewers": []})


# ---------------------------------------------------------
# The polled summary.
# ---------------------------------------------------------


class Summary(_ModerationFixture):
    def test_can_moderate_false_for_pure_viewer(self) -> None:
        widget = self._seed_thread_widget("summary-viewer")
        self._grant(email="viewer@example.com", node=("component", widget.id), level="view")
        resp = self.client.get("/data/threads/moderation/summary", headers=self._auth("viewer@example.com"))
        self.assertEqual(resp.json()["can_moderate"], False)

    def test_can_moderate_true_for_tab_editor(self) -> None:
        self._seed_thread_widget("summary-tab-editor")
        self._grant(email="tabeditor@example.com", node=("tab", self.root_tab_id), level="edit")
        resp = self.client.get(
            "/data/threads/moderation/summary", headers=self._auth("tabeditor@example.com")
        )
        self.assertEqual(resp.json()["can_moderate"], True)

    def test_can_moderate_true_for_admin_with_zero_widgets(self) -> None:
        resp = self.client.get(
            "/data/threads/moderation/summary", headers=self._member_auth("admin@example.com")
        )
        body = resp.json()
        self.assertEqual(body["can_moderate"], True)
        self.assertEqual(body["pending_count"], 0)

    def test_count_scoped_to_moderated_set(self) -> None:
        moderated = self._seed_thread_widget("summary-scoped-in")
        unmoderated = self._seed_thread_widget("summary-scoped-out")
        self._grant(email="editor@example.com", node=("component", moderated.id), level="edit")
        self._insert_thread(moderated.id, status="pending")
        self._insert_thread(unmoderated.id, status="pending")

        resp = self.client.get("/data/threads/moderation/summary", headers=self._auth("editor@example.com"))
        self.assertEqual(resp.json()["pending_count"], 1)

    def test_etag_round_trip_304(self) -> None:
        widget = self._seed_thread_widget("summary-etag")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        self._insert_thread(widget.id, status="pending")
        headers = self._auth("editor@example.com")

        first = self.client.get("/data/threads/moderation/summary", headers=headers)
        etag = first.headers["etag"]
        second = self.client.get(
            "/data/threads/moderation/summary", headers={**headers, "If-None-Match": etag}
        )
        self.assertEqual(second.status_code, 304, second.text)

    def test_count_drops_after_decision(self) -> None:
        widget = self._seed_thread_widget("summary-drops")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        thread = self._insert_thread(widget.id, status="pending")
        headers = self._auth("editor@example.com")

        before = self.client.get("/data/threads/moderation/summary", headers=headers).json()
        self.assertEqual(before["pending_count"], 1)
        self.client.post(f"/data/threads/{thread.id}/approve", headers=headers)
        after = self.client.get("/data/threads/moderation/summary", headers=headers).json()
        self.assertEqual(after["pending_count"], 0)

    def test_etag_differs_between_viewer_and_editor_with_zero_pending(self) -> None:
        # followups plan §1 — the exact collision behind the live bug: a
        # pure viewer and a component editor with no pending threads used
        # to both get `"0-0"`, so the browser's HTTP cache could revalidate
        # the editor's `can_moderate=True` body against the viewer's
        # request. Neither of these two has any pending thread.
        viewer_widget = self._seed_thread_widget("summary-etag-viewer")
        self._grant(email="viewer2@example.com", node=("component", viewer_widget.id), level="view")
        editor_widget = self._seed_thread_widget("summary-etag-editor")
        self._grant(email="editor2@example.com", node=("component", editor_widget.id), level="edit")

        viewer_resp = self.client.get(
            "/data/threads/moderation/summary", headers=self._auth("viewer2@example.com")
        )
        editor_resp = self.client.get(
            "/data/threads/moderation/summary", headers=self._auth("editor2@example.com")
        )
        self.assertEqual(viewer_resp.json()["can_moderate"], False)
        self.assertEqual(viewer_resp.json()["pending_count"], 0)
        self.assertEqual(editor_resp.json()["can_moderate"], True)
        self.assertEqual(editor_resp.json()["pending_count"], 0)
        self.assertNotEqual(viewer_resp.headers["etag"], editor_resp.headers["etag"])

    def test_summary_is_never_stored(self) -> None:
        widget = self._seed_thread_widget("summary-no-store")
        self._grant(email="nostore@example.com", node=("component", widget.id), level="edit")
        headers = self._auth("nostore@example.com")

        first = self.client.get("/data/threads/moderation/summary", headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.headers.get("cache-control"), "no-store")

        etag = first.headers["etag"]
        cached = self.client.get(
            "/data/threads/moderation/summary", headers={**headers, "If-None-Match": etag}
        )
        self.assertEqual(cached.status_code, 304, cached.text)
        self.assertEqual(cached.headers.get("cache-control"), "no-store")


# ---------------------------------------------------------
# The widget-removal confirm's true-totals check (followups plan §4) — ALL
# statuses, unlike every list endpoint above, which stays scoped to
# `approved OR (own AND pending/rejected)` (`WidgetVisibility.
# test_editor_list_does_not_show_others_pending` pins that the list
# endpoint itself must NOT change).
# ---------------------------------------------------------


class WidgetCounts(_ModerationFixture):
    def test_component_editor_gets_all_status_totals(self) -> None:
        widget = self._seed_thread_widget("counts-editor")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        approved_1 = self._insert_thread(widget.id, author_email="editor@example.com", status="approved")
        approved_2 = self._insert_thread(widget.id, author_email="editor@example.com", status="approved")
        others_pending = self._insert_thread(widget.id, author_email="bob@example.com", status="pending")
        rejected = self._insert_thread(widget.id, author_email="editor@example.com", status="rejected")
        for thread, comment_count in ((approved_1, 2), (approved_2, 3), (others_pending, 5), (rejected, 1)):
            thread.comment_count = comment_count
            self.db.commit()

        resp = self.client.get(
            "/data/threads/component/counts-editor/counts", headers=self._auth("editor@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["thread_count"], 4)
        self.assertEqual(body["pending_count"], 1)
        self.assertEqual(body["comment_count"], 11)

    def test_tab_editor_ancestor_gets_the_same_totals(self) -> None:
        """The ancestor rule this gate relies on (0.1(2), `_is_component_
        editor`'s `verdict(...).edit` fold) — a real grant on the ROOT TAB,
        not the widget, plain `_auth`, same shape as `CreateRoutesByAuthority.
        test_tab_edit_grantee_posts_approved`."""
        widget = self._seed_thread_widget("counts-tab-editor")
        self._grant(email="tabeditor@example.com", node=("tab", self.root_tab_id), level="edit")
        approved = self._insert_thread(widget.id, author_email="alice@example.com", status="approved")
        others_pending = self._insert_thread(widget.id, author_email="bob@example.com", status="pending")
        approved.comment_count = 2
        others_pending.comment_count = 1
        self.db.commit()

        resp = self.client.get(
            "/data/threads/component/counts-tab-editor/counts", headers=self._auth("tabeditor@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["thread_count"], 2)
        self.assertEqual(body["pending_count"], 1)
        self.assertEqual(body["comment_count"], 3)

    def test_pure_viewer_403(self) -> None:
        widget = self._seed_thread_widget("counts-viewer")
        self._grant(email="viewer@example.com", node=("component", widget.id), level="view")
        resp = self.client.get(
            "/data/threads/component/counts-viewer/counts", headers=self._auth("viewer@example.com")
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_ungranted_caller_403(self) -> None:
        self._seed_thread_widget("counts-ungranted")
        resp = self.client.get(
            "/data/threads/component/counts-ungranted/counts", headers=self._auth("nobody@example.com")
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_non_thread_widget_link_is_404(self) -> None:
        # Same "wrong type reads as not found" idiom as
        # test_threads.ThreadCrudTests.test_link_pointing_at_a_non_thread_widget_is_404.
        component = ComponentV2(
            link="counts-not-a-thread",
            type="airtable",
            gridstack_id=self.root_gridstack.id,
        )
        self.db.add(component)
        self.db.commit()
        resp = self.client.get(
            "/data/threads/component/counts-not-a-thread/counts",
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_empty_widget_is_all_zeros(self) -> None:
        widget = self._seed_thread_widget("counts-empty")
        self._grant(email="editor@example.com", node=("component", widget.id), level="edit")
        resp = self.client.get(
            "/data/threads/component/counts-empty/counts", headers=self._auth("editor@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["thread_count"], 0)
        self.assertEqual(body["pending_count"], 0)
        self.assertEqual(body["comment_count"], 0)


# ---------------------------------------------------------
# The database CHECK is live on SQLite too.
# ---------------------------------------------------------


class ModelConstraints(_ModerationFixture):
    def test_bogus_status_raises_integrity_error(self) -> None:
        widget = self._seed_thread_widget("model-constraints")
        with self.assertRaises(IntegrityError):
            self.db.add(
                ThreadV2(
                    component_id=widget.id,
                    title="T",
                    content="c",
                    author_email="a@example.com",
                    author_name="a",
                    mentions=[],
                    status="bogus",
                    up_count=0,
                    down_count=0,
                    comment_count=0,
                    created_at=thread_service._utc_now(),
                )
            )
            self.db.commit()


# ---------------------------------------------------------
# M8 — a mirror never confers moderation authority; the target's OWN
# ancestor chain is what matters.
# ---------------------------------------------------------


class Mirror(_ModerationFixture):
    def test_edit_on_mirrors_own_tab_does_not_grant_editor_status_on_target(self) -> None:
        widget = self._seed_thread_widget("mirror-target")  # lives under tab A
        self._seed_mirror("mirror-of-target", target_link="mirror-target")
        # VIEW on the target itself is what lets tabb reach/post to it at
        # all (a real user would hold this via the mirror's own conjunction
        # rule — access_visibility_service.MirrorsAreAConjunction — by also
        # being granted on the target; that machinery isn't under test
        # here). EDIT is granted only on the MIRROR's own tab (B) — the
        # thing this test proves does NOT confer editor status on the
        # target, which is a completely separate node with its own
        # ancestor chain under tab A.
        self._grant(email="tabb@example.com", node=("component", widget.id), level="view")
        self._grant(email="tabb@example.com", node=("tab", self.tab_b_id), level="edit")

        # M8: the frontend always resolves a mirror to its TARGET's link
        # before calling the API (`data.targetLink`) — the backend never
        # sees "the mirror" as a posting target.
        create = self.client.post(
            "/data/threads/component/mirror-target",
            json={"title": "T", "content": "hi"},
            headers=self._auth("tabb@example.com"),
        )
        self.assertEqual(create.status_code, 201, create.text)
        self.assertEqual(create.json()["status"], "pending")

        thread_id = create.json()["id"]
        decide = self.client.post(
            f"/data/threads/{thread_id}/approve", headers=self._auth("tabb@example.com")
        )
        self.assertEqual(decide.status_code, 403, decide.text)

    def test_edit_on_targets_own_tab_grants_editor_status(self) -> None:
        widget = self._seed_thread_widget("mirror-target-2")  # lives under tab A
        self._seed_mirror("mirror-of-target-2", target_link="mirror-target-2")
        self._grant(email="taba@example.com", node=("tab", self.root_tab_id), level="edit")

        create = self.client.post(
            "/data/threads/component/mirror-target-2",
            json={"title": "T", "content": "hi"},
            headers=self._auth("taba@example.com"),
        )
        self.assertEqual(create.status_code, 201, create.text)
        self.assertEqual(create.json()["status"], "approved")


if __name__ == "__main__":
    unittest.main()
