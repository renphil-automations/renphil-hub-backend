"""
Tests for rejection reasons, resubmission and the Close model
(plan_thread_reject_resubmit_2026-11-07.md, phase A — §2 model, §3 schemas,
§4 service, §5 router, §8 tests — as amended by §12, 2026-11-08: author
"Close" replaced the soft "Delete", declined edits auto-close when a newer
version goes live, G1 and G3).

Built on `_RevisionFixture` (tests.test_thread_revisions): every widget gets
`AUTHOR` (view grant — a non-editor, resubmits land PENDING), `EDITOR` (edit
grant — decides) and `VIEWER` (view grant — sees nothing of anyone's
declines).

THE FOUR CASES THAT ARE EASY TO MAKE VACUOUS (reported before any code):
  (a) the author whose resubmit must go `pending` is `AUTHOR` — a real
      `view` grant + plain `_auth`, NEVER `_member_auth` (a Hub Admin is an
      editor of everything and would auto-publish). Every such test also
      asserts no v1 was written.
  (b) the editor-author whose resubmit must AUTO-PUBLISH (D5) is
      `EDITOR_AUTHOR`: posted and was declined as a `view`-granted viewer,
      THEN given `edit` on `("tab", self.root_tab_id)` — not on the widget —
      and still plain `_auth`, so the test proves the editor check walks the
      ancestor chain rather than riding the admin bypass.
  (c) an editor who is NOT the author (`EDITOR`, plain `_auth`) and a real
      Hub Admin (`_member_auth`) both get 403 on resubmit/close.
  (d) a plain viewer's `declined_count` is 0 while the AUTHOR's is 2 on the
      same widget in the same test, so the 0 is not vacuous; `EDITOR` and a
      no-subject caller get 0 too.

`FOR UPDATE` is invisible on SQLite: every race is pinned with a plain
second call. The partial UNIQUE `uq_thread_revisions_resubmission_of` IS
enforced on SQLite (it has `sqlite_where`), so the D4 duplicate test is real.
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db_v2.models.notification import NotificationV2
from app.db_v2.models.thread import ThreadRevisionV2, ThreadSubmissionV2, ThreadV2
from app.models.auth import UserInfo
from app.services import thread_service
from app.services.access_visibility_service import resolve_viewer_access
from tests.test_thread_revisions import AUTHOR, BASE_TIME, EDITOR, VIEWER, _RevisionFixture

EDITOR_AUTHOR = "edauthor@example.com"
EDITOR2 = "editor2@example.com"
STRANGER = "stranger@example.com"
ADMIN = "admin@example.com"
MENTION_EMAIL = "mentioned@example.com"
OTHER_EMAIL = "other@example.com"


class _ResubmitFixture(_RevisionFixture):
    def _widget(self, link: str):
        widget = super()._widget(link)
        self._grant(email=EDITOR_AUTHOR, node=("component", widget.id), level="view")
        self._grant(email=EDITOR2, node=("component", widget.id), level="edit")
        return widget

    def _mentionables(self, widget) -> None:
        self._seed_mentionable(MENTION_EMAIL, ("component", widget.id), name="Mentioned Person")
        self._seed_mentionable(OTHER_EMAIL, ("component", widget.id), name="Other Person")

    # -- thread helpers ------------------------------------------------

    def _pending(self, widget, *, author: str = AUTHOR, **kwargs) -> ThreadV2:
        kwargs.setdefault("title", "Declined title")
        kwargs.setdefault("content", "declined body")
        return self._insert_thread(widget.id, author_email=author, status="pending", **kwargs)

    def _reject_thread(self, thread_id: int, reason: str = "Needs work", *, email: str = EDITOR):
        return self.client.post(
            f"/data/threads/{thread_id}/reject", json={"reason": reason}, headers=self._auth(email)
        )

    def _approve_thread(self, thread_id: int, *, email: str = EDITOR):
        return self.client.post(f"/data/threads/{thread_id}/approve", headers=self._auth(email))

    def _rejected(self, widget, *, author: str = AUTHOR, reason: str = "Needs work", **kwargs) -> ThreadV2:
        thread = self._pending(widget, author=author, **kwargs)
        resp = self._reject_thread(thread.id, reason)
        self.assertEqual(resp.status_code, 200, resp.text)
        return self._live(thread.id)

    def _resubmit(self, thread_id: int, body: dict, *, email: str = AUTHOR, headers: dict | None = None):
        return self.client.post(
            f"/data/threads/{thread_id}/resubmit", json=body, headers=headers or self._auth(email)
        )

    def _close(self, thread_id: int, *, email: str = AUTHOR, headers: dict | None = None):
        return self.client.post(
            f"/data/threads/{thread_id}/close", headers=headers or self._auth(email)
        )

    @staticmethod
    def _body(title: str = "Declined title", content: str = "declined body", mentions=None) -> dict:
        return {"title": title, "content": content, "mentions": mentions or []}

    def _rounds(self, thread_id: int) -> list[ThreadSubmissionV2]:
        self.db.expire_all()
        return (
            self.db.query(ThreadSubmissionV2)
            .filter(ThreadSubmissionV2.thread_id == thread_id)
            .order_by(ThreadSubmissionV2.round)
            .all()
        )

    # -- revision helpers ----------------------------------------------

    def _declined_edit(self, thread, body: dict, *, reason: str = "Needs work") -> dict:
        revision = self._stage(thread.id, body)
        resp = self.client.post(
            f"/data/threads/{thread.id}/revisions/{revision['id']}/reject",
            json={"reason": reason},
            headers=self._auth(EDITOR),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _rework(self, thread_id: int, revision_id: int, body: dict, *, email: str = AUTHOR, headers=None):
        return self.client.post(
            f"/data/threads/{thread_id}/revisions/{revision_id}/resubmit",
            json=body,
            headers=headers or self._auth(email),
        )

    def _close_revision(self, thread_id: int, revision_id: int, *, email: str = AUTHOR, headers=None):
        return self.client.post(
            f"/data/threads/{thread_id}/revisions/{revision_id}/close",
            headers=headers or self._auth(email),
        )

    # -- reads ------------------------------------------------------------

    def _list(self, link: str, *, email: str = AUTHOR) -> dict:
        resp = self.client.get(f"/data/threads/component/{link}", headers=self._auth(email))
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _declined(self, link: str, *, email: str = AUTHOR) -> dict:
        resp = self.client.get(f"/data/threads/component/{link}/declined", headers=self._auth(email))
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _history(self, params: dict | None = None, *, email: str = EDITOR) -> dict:
        resp = self.client.get(
            "/data/threads/moderation/history", params=params or {}, headers=self._auth(email)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _access(self, email: str):
        return resolve_viewer_access(self.db, self._seed_hub_user(email).id, is_admin=False)


# ---------------------------------------------------------
# §4.1 — a required reason, and a round for every decision.
# ---------------------------------------------------------


class RejectReason(_ResubmitFixture):
    BAD_BODIES = (None, {}, {"reason": ""}, {"reason": "   \n\t "}, {"reason": "x" * 1001})

    def test_thread_reject_422_without_a_valid_reason(self) -> None:
        widget = self._widget("rr-thread-422")
        thread = self._pending(widget)
        for body in self.BAD_BODIES:
            with self.subTest(body=str(body)[:30]):
                resp = self.client.post(
                    f"/data/threads/{thread.id}/reject", json=body, headers=self._auth(EDITOR)
                )
                self.assertEqual(resp.status_code, 422, resp.text)
        self.assertEqual(self._live(thread.id).status, "pending")
        self.assertEqual(self._rounds(thread.id), [])

    def test_revision_reject_422_without_a_valid_reason(self) -> None:
        widget = self._widget("rr-rev-422")
        thread = self._approved(widget)
        revision = self._stage(thread.id, {"content": "proposed"})
        for body in self.BAD_BODIES:
            with self.subTest(body=str(body)[:30]):
                resp = self.client.post(
                    f"/data/threads/{thread.id}/revisions/{revision['id']}/reject",
                    json=body,
                    headers=self._auth(EDITOR),
                )
                self.assertEqual(resp.status_code, 422, resp.text)
        (row,) = self._revisions(thread.id)
        self.assertEqual(row.status, "pending")

    def test_1000_characters_is_accepted(self) -> None:
        widget = self._widget("rr-1000")
        thread = self._pending(widget)
        self.assertEqual(self._reject_thread(thread.id, "y" * 1000).status_code, 200)

    def test_reject_stores_trimmed_reason_on_round_zero_and_stamps_last_decided(self) -> None:
        widget = self._widget("rr-store")
        thread = self._pending(widget, title="T", content="C")
        resp = self._reject_thread(thread.id, "  Please add a source  ")
        self.assertEqual(resp.status_code, 200, resp.text)
        live = self._live(thread.id)
        (round0,) = self._rounds(thread.id)
        self.assertEqual(
            (round0.round, round0.status, round0.reason, round0.title, round0.content),
            (0, "rejected", "Please add a source", "T", "C"),
        )
        self.assertEqual((round0.reviewed_by_email, round0.submitted_by_email), (EDITOR, AUTHOR))
        self.assertEqual(round0.reviewed_at, live.reviewed_at)
        self.assertEqual(live.last_decided_at, live.reviewed_at)
        self.assertEqual(resp.json()["last_decision"]["reason"], "Please add a source")
        self.assertEqual(resp.json()["decision_count"], 1)

    def test_approve_writes_a_round_too_with_no_reason(self) -> None:
        widget = self._widget("rr-approve")
        thread = self._pending(widget)
        self.assertEqual(self._approve_thread(thread.id).status_code, 200)
        (round0,) = self._rounds(thread.id)
        self.assertEqual((round0.round, round0.status, round0.reason), (0, "approved", None))
        self.assertIsNotNone(self._live(thread.id).last_decided_at)

    def test_revision_reason_is_stored(self) -> None:
        widget = self._widget("rr-rev-store")
        thread = self._approved(widget)
        detail = self._declined_edit(thread, {"content": "proposed"}, reason=" too long ")
        self.assertEqual(detail["rejection_reason"], "too long")
        (row,) = self._revisions(thread.id)
        self.assertEqual(row.rejection_reason, "too long")

    def test_service_asserts_the_reason_too(self) -> None:
        widget = self._widget("rr-service")
        thread = self._pending(widget)
        editor = UserInfo(email=EDITOR, name="editor", roles=[])
        for reason in (None, "", "   "):
            with self.subTest(reason=reason):
                with self.assertRaises(HTTPException) as ctx:
                    thread_service.decide_thread(
                        self.db, thread.id, editor, approve=False, reason=reason,
                        access=self._access(EDITOR),
                    )
                self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self._live(thread.id).status, "pending")


# ---------------------------------------------------------
# §4.2 / §4.7 — declined threads leave the list; the inbox and its count.
# ---------------------------------------------------------


class WidgetListMovesDeclined(_ResubmitFixture):
    def test_declined_count_and_inbox_are_scoped_to_the_caller(self) -> None:
        """Fixture (d): the author sees 2 (a thread + an edit); a plain
        viewer, an editor who is not the author, and a no-subject caller see
        0 and an empty inbox — on the same widget, in the same test."""
        link = "wl-scope"
        widget = self._widget(link)
        declined_thread = self._rejected(widget)
        live = self._approved(widget, title="Live one")
        declined_edit = self._declined_edit(live, {"content": "edit body"})

        author_list = self._list(link)
        self.assertEqual(author_list["declined_count"], 2)
        self.assertNotIn(declined_thread.id, {r["id"] for r in author_list["items"]})
        inbox = self._declined(link)
        self.assertEqual(
            {(i["kind"], i.get("revision_id") or i["thread_id"]) for i in inbox["items"]},
            {("thread", declined_thread.id), ("edit", declined_edit["id"])},
        )
        self.assertFalse(inbox["truncated"])

        for email in (VIEWER, EDITOR):
            with self.subTest(email=email):
                self.assertEqual(self._list(link, email=email)["declined_count"], 0)
                self.assertEqual(self._declined(link, email=email)["items"], [])

        # No-subject caller (anonymous / view-as-user): the service gets
        # `user=None`.
        self.assertEqual(
            thread_service.list_threads_for_link(self.db, link, None, None, access=None).declined_count, 0
        )
        self.assertEqual(
            thread_service.list_declined_items(self.db, link, None, access=None).items, []
        )
        anon = self.client.get(
            f"/data/threads/component/{link}", params={"anonymous": "true"}, headers=self._auth(AUTHOR)
        )
        if anon.status_code == 200:  # anonymous View As may be refused for a non-admin
            self.assertEqual(anon.json()["declined_count"], 0)

    def test_inbox_items_carry_reason_reviewer_and_live_title(self) -> None:
        link = "wl-items"
        widget = self._widget(link)
        thread = self._rejected(widget, reason="Add a source")
        live = self._approved(widget, title="Live title here")
        edit = self._declined_edit(live, {"title": "Proposed", "content": "edit body"}, reason="Off topic")

        items = {i["kind"]: i for i in self._declined(link)["items"]}
        self.assertEqual(items["thread"]["reason"], "Add a source")
        self.assertEqual(items["thread"]["declined_by_email"], EDITOR)
        self.assertEqual(items["thread"]["resubmission_count"], 0)
        self.assertEqual(items["thread"]["thread_id"], thread.id)
        self.assertEqual(items["edit"]["reason"], "Off topic")
        self.assertEqual(items["edit"]["thread_title"], "Live title here")
        self.assertEqual(items["edit"]["title"], "Proposed")
        self.assertEqual(items["edit"]["version"], edit["version"])
        self.assertEqual(items["edit"]["resubmission_number"], 0)

    def test_inbox_newest_decline_first(self) -> None:
        link = "wl-order"
        widget = self._widget(link)
        first = self._rejected(widget, title="first")
        second = self._rejected(widget, title="second")
        self.assertEqual(
            [i["thread_id"] for i in self._declined(link)["items"]], [second.id, first.id]
        )

    def test_pre_feature_decline_appears_with_no_reason(self) -> None:
        """Owner decision 12: a decline from before reasons existed (the
        migration's backfilled round has `reason = NULL`)."""
        link = "wl-legacy"
        widget = self._widget(link)
        legacy = self._insert_thread(
            widget.id, author_email=AUTHOR, status="rejected",
            reviewed_by_email=EDITOR, reviewed_by_name="editor", reviewed_at=BASE_TIME,
        )
        (item,) = self._declined(link)["items"]
        self.assertEqual((item["thread_id"], item["reason"]), (legacy.id, None))

    def test_inbox_is_capped_with_truncated(self) -> None:
        link = "wl-cap"
        widget = self._widget(link)
        for i in range(3):
            self._rejected(widget, title=f"t{i}")
        with mock.patch.object(thread_service, "DECLINED_ITEMS_CAP", 2):
            body = self._declined(link)
        self.assertEqual(len(body["items"]), 2)
        self.assertTrue(body["truncated"])


# ---------------------------------------------------------
# §4.3 — resubmitting a declined thread.
# ---------------------------------------------------------


class ResubmitThread(_ResubmitFixture):
    def test_unchanged_resubmission_409(self) -> None:
        widget = self._widget("rt-unchanged")
        thread = self._rejected(widget)
        resp = self._resubmit(thread.id, self._body())
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "unchanged_resubmission")
        self.assertEqual(self._live(thread.id).status, "rejected")

    def test_unchanged_with_equal_and_reordered_mentions_409(self) -> None:
        widget = self._widget("rt-unchanged-mentions")
        self._mentionables(widget)
        content = "hi @MentionedPerson and @OtherPerson"
        thread = self._pending(widget, content=content)
        thread.mentions = [
            {"email": MENTION_EMAIL, "name": "Mentioned Person", "token": "MentionedPerson"},
            {"email": OTHER_EMAIL, "name": "Other Person", "token": "OtherPerson"},
        ]
        self.db.commit()
        self.assertEqual(self._reject_thread(thread.id).status_code, 200)
        for order in ([MENTION_EMAIL, OTHER_EMAIL], [OTHER_EMAIL, MENTION_EMAIL]):
            with self.subTest(order=order):
                resp = self._resubmit(
                    thread.id, self._body(content=content, mentions=[{"email": e} for e in order])
                )
                self.assertEqual(resp.status_code, 409, resp.text)
                self.assertEqual(resp.json()["detail"]["code"], "unchanged_resubmission")

    def test_title_only_body_only_and_mentions_only_changes_are_accepted(self) -> None:
        widget = self._widget("rt-changes")
        self._mentionables(widget)
        content = "hi @MentionedPerson"
        cases = {
            "title": self._body(title="New title", content=content),
            "content": self._body(content=content + " (more)"),
            "mentions": self._body(content=content, mentions=[{"email": MENTION_EMAIL}]),
        }
        for name, body in cases.items():
            with self.subTest(change=name):
                thread = self._rejected(widget, content=content, title="Declined title")
                resp = self._resubmit(thread.id, body)
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(resp.json()["status"], "pending")
        # Whitespace is NOT normalized: a trailing space is a change.
        thread = self._rejected(widget)
        self.assertEqual(self._resubmit(thread.id, self._body(content="declined body ")).status_code, 200)

    def test_resubmit_effects(self) -> None:
        """Fixture (a): a view-granted author with plain `_auth` lands
        PENDING — no v1, nobody notified."""
        widget = self._widget("rt-effects")
        self._mentionables(widget)
        thread = self._rejected(widget, created_at=BASE_TIME)
        declined = self._live(thread.id)
        notifications_before = self.db.query(NotificationV2).count()

        resp = self._resubmit(
            thread.id, self._body(content="fixed @MentionedPerson", mentions=[{"email": MENTION_EMAIL}])
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual((body["status"], body["resubmission_count"]), ("pending", 1))
        self.assertIsNone(body["reviewed_at"])

        live = self._live(thread.id)
        self.assertEqual(live.status, "pending")
        self.assertEqual(live.resubmission_count, 1)
        self.assertGreater(
            thread_service._as_utc(live.submitted_at), thread_service._as_utc(live.created_at)
        )
        self.assertEqual(
            (live.reviewed_by_email, live.reviewed_by_name, live.reviewed_at), (None, None, None)
        )
        self.assertEqual(live.last_decided_at, declined.last_decided_at)
        self.assertEqual(live.content, "fixed @MentionedPerson")
        (round0,) = self._rounds(thread.id)
        self.assertEqual((round0.status, round0.content, round0.reason), ("rejected", "declined body", "Needs work"))
        self.assertEqual(self._all_revisions(thread.id), [])
        self.assertEqual(self.db.query(NotificationV2).count(), notifications_before)
        # Pending again: counted in the author's list (2026-11-13 — no longer
        # listed as a row), and out of the inbox.
        listed = self._list("rt-effects")
        self.assertNotIn(thread.id, {r["id"] for r in listed["items"]})
        self.assertEqual(listed["pending_count"], 1)
        self.assertEqual(self._declined("rt-effects")["items"], [])
        # Still in grouped History with its earlier decline (landmine 3).
        (card,) = [c for c in self._history()["items"] if c["id"] == thread.id]
        self.assertEqual(card["status"], "pending")
        self.assertIsNone(card["reviewed_at"])
        self.assertEqual(card["last_decision"]["status"], "rejected")
        self.assertEqual(card["decision_count"], 1)

    def test_non_author_editor_and_admin_get_403(self) -> None:
        """Fixture (c) — author-only (landmine 6)."""
        widget = self._widget("rt-403")
        thread = self._rejected(widget)
        for label, headers in (
            ("editor", self._auth(EDITOR)),
            ("viewer", self._auth(VIEWER)),
            ("hub admin", self._member_auth(ADMIN)),
        ):
            with self.subTest(caller=label):
                self.assertEqual(self._resubmit(thread.id, self._body(title="X"), headers=headers).status_code, 403)
                self.assertEqual(self._close(thread.id, headers=headers).status_code, 403)
        live = self._live(thread.id)
        self.assertEqual((live.status, live.closed_at), ("rejected", None))

    def test_pending_or_approved_409_not_rejected(self) -> None:
        widget = self._widget("rt-not-rejected")
        for status_ in ("pending", "approved"):
            with self.subTest(status=status_):
                thread = self._insert_thread(widget.id, author_email=AUTHOR, status=status_)
                resp = self._resubmit(thread.id, self._body(title="Changed"))
                self.assertEqual(resp.status_code, 409, resp.text)
                self.assertEqual(resp.json()["detail"]["code"], "not_rejected")

    def test_closed_thread_resubmit_409_closed(self) -> None:
        """§12.7 — replaced the soft-delete 404."""
        widget = self._widget("rt-closed")
        thread = self._rejected(widget)
        self.assertEqual(self._close(thread.id).status_code, 204)
        resp = self._resubmit(thread.id, self._body(title="Changed"))
        self.assertEqual(resp.status_code, 409, resp.text)
        detail = resp.json()["detail"]
        self.assertEqual(
            (detail["code"], detail["closed_kind"], detail["closed_by_version"]),
            ("closed", "author", None),
        )
        self.assertEqual(self._live(thread.id).status, "rejected")

    def test_fail_fast_order(self) -> None:
        """§12.7: 404 missing → 403 view → 403 author → 409 not_rejected →
        409 closed → 409 unchanged. Each step uses a request that would
        ALSO fail every later check it can."""
        widget = self._widget("rt-order")
        closed = self._rejected(widget)
        self.assertEqual(self._close(closed.id).status_code, 204)
        unchanged = self._body()  # equal to the declined text

        self.assertEqual(self._resubmit(999999, unchanged).status_code, 404)
        self.assertEqual(self._resubmit(closed.id, unchanged, email=STRANGER).status_code, 403)
        self.assertEqual(self._resubmit(closed.id, unchanged, email=EDITOR).status_code, 403)
        pending = self._pending(widget)
        resp = self._resubmit(pending.id, unchanged)
        self.assertEqual((resp.status_code, resp.json()["detail"]["code"]), (409, "not_rejected"))
        resp = self._resubmit(closed.id, unchanged)
        self.assertEqual((resp.status_code, resp.json()["detail"]["code"]), (409, "closed"))
        rejected = self._rejected(widget)
        resp = self._resubmit(rejected.id, unchanged)
        self.assertEqual((resp.status_code, resp.json()["detail"]["code"]), (409, "unchanged_resubmission"))

    def test_editor_author_auto_publishes(self) -> None:
        """Fixture (b) / D5: declined as a viewer, an editor (via a ROOT-TAB
        grant, plain `_auth`) by the time they resubmit → published at once:
        v1 written with them as publisher, mentions notified exactly once,
        NO new decision round."""
        widget = self._widget("rt-editor-author")
        self._mentionables(widget)
        thread = self._rejected(widget, author=EDITOR_AUTHOR)
        self._grant(email=EDITOR_AUTHOR, node=("tab", self.root_tab_id), level="edit")

        resp = self._resubmit(
            thread.id,
            self._body(content="now @MentionedPerson", mentions=[{"email": MENTION_EMAIL}]),
            email=EDITOR_AUTHOR,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "approved")
        live = self._live(thread.id)
        self.assertEqual((live.status, live.reviewed_at, live.resubmission_count), ("approved", None, 1))
        (v1,) = self._all_revisions(thread.id)
        self.assertEqual((v1.version, v1.origin, v1.reviewed_by_email), (1, "original", EDITOR_AUTHOR))
        self.assertEqual(v1.content, "now @MentionedPerson")
        mention_rows = [n for n in self._notifications_for(MENTION_EMAIL) if n.type == "mention"]
        self.assertEqual(len(mention_rows), 1)
        self.assertEqual([r.status for r in self._rounds(thread.id)], ["rejected"])
        # §12.8 G3 (repointed 2026-11-08): the card's latest line is the
        # synthesized `published` round; decision_count stays explicit-only.
        (card,) = [c for c in self._history()["items"] if c["id"] == thread.id]
        self.assertEqual((card["decision_count"], card["last_decision"]["status"]), (1, "published"))

    def test_second_decline_creates_round_one_with_its_own_reason(self) -> None:
        link = "rt-second"
        widget = self._widget(link)
        thread = self._rejected(widget, reason="first reason")
        self.assertEqual(self._resubmit(thread.id, self._body(title="Second try")).status_code, 200)
        self.assertEqual(self._reject_thread(thread.id, "second reason").status_code, 200)
        rounds = self._rounds(thread.id)
        self.assertEqual(
            [(r.round, r.status, r.reason, r.title) for r in rounds],
            [(0, "rejected", "first reason", "Declined title"), (1, "rejected", "second reason", "Second try")],
        )
        (item,) = self._declined(link)["items"]
        self.assertEqual((item["reason"], item["resubmission_count"]), ("second reason", 1))

    def test_service_with_access_none_lands_pending(self) -> None:
        """An `access=None` caller is never an editor — even an EDITOR."""
        widget = self._widget("rt-service")
        thread = self._rejected(widget, author=EDITOR)
        detail = thread_service.resubmit_thread(
            self.db, thread.id, UserInfo(email=EDITOR, name="editor", roles=[]),
            "Changed", "declined body", [], access=None,
        )
        self.assertEqual(detail.status, "pending")
        self.assertEqual(self._all_revisions(thread.id), [])


# ---------------------------------------------------------
# §4.6 / decision 14 — the Pending queue orders by submitted_at.
# ---------------------------------------------------------


class QueueOrder(_ResubmitFixture):
    PATH = "/data/threads/moderation/pending"

    def test_resubmitted_old_thread_sorts_after_a_newer_pending_one(self) -> None:
        widget = self._widget("qo-order")
        old = self._rejected(widget, created_at=BASE_TIME)
        newer = self._pending(widget, title="newer", created_at=BASE_TIME + timedelta(days=1))
        self.assertEqual(self._resubmit(old.id, self._body(title="Reworked")).status_code, 200)

        resp = self.client.get(self.PATH, headers=self._auth(EDITOR))
        self.assertEqual(resp.status_code, 200, resp.text)
        items = resp.json()["items"]
        self.assertEqual([i["id"] for i in items], [newer.id, old.id])
        by_id = {i["id"]: i for i in items}
        self.assertEqual(by_id[old.id]["resubmission_count"], 1)
        self.assertEqual(by_id[old.id]["previous_rejection"]["reason"], "Needs work")
        self.assertEqual(by_id[old.id]["previous_rejection"]["reviewed_by_email"], EDITOR)
        self.assertIsNone(by_id[newer.id]["previous_rejection"])

    def test_cursor_walk_on_submitted_at_has_no_skip_or_duplicate(self) -> None:
        widget = self._widget("qo-cursor")
        expected = [self._pending(widget, title=f"p{i}", created_at=BASE_TIME + timedelta(hours=i)).id for i in range(3)]
        for i in range(2):
            reworked = self._rejected(widget, title=f"r{i}", created_at=BASE_TIME - timedelta(days=1 + i))
            self.assertEqual(self._resubmit(reworked.id, self._body(title=f"r{i} again")).status_code, 200)
            expected.append(reworked.id)

        seen: list[int] = []
        cursor = None
        with mock.patch.object(thread_service, "THREADS_PAGE_SIZE", 2):
            for _ in range(10):
                params = {"cursor": cursor} if cursor else {}
                body = self.client.get(self.PATH, params=params, headers=self._auth(EDITOR)).json()
                seen.extend(i["id"] for i in body["items"])
                cursor = body["next_cursor"]
                if not cursor:
                    break
        self.assertEqual(seen, expected)


# ---------------------------------------------------------
# §4.4 — reworking a declined edit (owner decision 9).
# ---------------------------------------------------------


class ResubmitRevision(_ResubmitFixture):
    def test_rework_base_is_the_declined_snapshot(self) -> None:
        """Landmine 9 / mutation 4: resubmitting EXACTLY the declined text
        is refused even though it differs from the live thread.

        Rewritten 2026-11-10: the "newer pending edit next to it" can no
        longer be staged (U5), and a rework next to a pending edit is now
        409 `pending_edit_exists` (`test_rework_with_a_pending_edit_409_
        pending_edit_exists`)."""
        widget = self._widget("rr-base")
        thread = self._approved(widget)
        declined = self._declined_edit(thread, {"title": "Declined T", "content": "declined edit"})
        resp = self._rework(thread.id, declined["id"], self._body(title="Declined T", content="declined edit"))
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "unchanged_resubmission")

    def test_rework_is_a_linked_pending_revision(self) -> None:
        widget = self._widget("rr-link")
        thread = self._approved(widget)
        declined = self._declined_edit(thread, {"content": "declined edit"})
        resp = self._rework(thread.id, declined["id"], self._body(title="Live title", content="reworked edit"))
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertFalse(body["applied"])
        rev = body["revision"]
        self.assertEqual(rev["status"], "pending")
        self.assertEqual(rev["resubmission_of_id"], declined["id"])
        self.assertEqual(rev["resubmission_of_version"], declined["version"])
        self.assertEqual(rev["resubmission_number"], 1)
        self.assertEqual(rev["resubmission_root_version"], declined["version"])
        self.assertIsNone(declined["resubmission_root_version"])
        self.assertEqual(rev["version"], declined["version"] + 1)
        statuses = {r.id: r.status for r in self._revisions(thread.id)}
        self.assertEqual(statuses[declined["id"]], "rejected")
        self.assertEqual(self._live(thread.id).content, "live body")
        # A rework of the rework is Revision 2.
        reject = self.client.post(
            f"/data/threads/{thread.id}/revisions/{rev['id']}/reject",
            json={"reason": "again"}, headers=self._auth(EDITOR),
        )
        self.assertEqual(reject.status_code, 200)
        second = self._rework(thread.id, rev["id"], self._body(title="Live title", content="third try"))
        self.assertEqual(second.json()["revision"]["resubmission_number"], 2)
        # 2026-11-09 relabel: "v4 (Revision 2 of v2)" — the ROOT, not the parent.
        self.assertEqual(second.json()["revision"]["resubmission_of_version"], rev["version"])
        self.assertEqual(second.json()["revision"]["resubmission_root_version"], declined["version"])
        chain = self.client.get(
            f"/data/threads/{thread.id}/revisions/{rev['id']}/chain", headers=self._auth(AUTHOR)
        )
        self.assertEqual(chain.status_code, 200, chain.text)
        self.assertEqual(
            [(r["id"], r["rejection_reason"]) for r in chain.json()],
            [(declined["id"], "Needs work"), (rev["id"], "again"), (second.json()["revision"]["id"], None)],
        )

    def test_second_rework_409_already_resubmitted(self) -> None:
        widget = self._widget("rr-twice")
        thread = self._approved(widget)
        declined = self._declined_edit(thread, {"content": "declined edit"})
        first = self._rework(thread.id, declined["id"], self._body(title="Live title", content="one"))
        self.assertEqual(first.status_code, 200, first.text)
        again = self._rework(thread.id, declined["id"], self._body(title="Live title", content="two"))
        self.assertEqual(again.status_code, 409, again.text)
        self.assertEqual(again.json()["detail"]["code"], "already_resubmitted")
        self.assertEqual(again.json()["detail"]["resubmission"]["id"], first.json()["revision"]["id"])

    def test_partial_unique_backstops_d4(self) -> None:
        widget = self._widget("rr-unique")
        thread = self._approved(widget)
        declined = self._insert_revision(thread, version=2, status="rejected", reviewed_at=BASE_TIME, reviewed_by_email=EDITOR)
        for version in (3, 4):
            row = ThreadRevisionV2(
                thread_id=thread.id, version=version, title="t", content="c", mentions=[],
                status="pending", origin="author", submitted_by_email=AUTHOR,
                submitted_at=BASE_TIME, resubmission_of_id=declined.id, resubmission_number=1,
            )
            self.db.add(row)
            if version == 3:
                self.db.commit()
            else:
                with self.assertRaises(IntegrityError):
                    self.db.commit()
                self.db.rollback()

    def test_rework_identical_to_live_409_no_change(self) -> None:
        widget = self._widget("rr-no-change")
        thread = self._approved(widget)
        declined = self._declined_edit(thread, {"content": "declined edit"})
        resp = self._rework(thread.id, declined["id"], self._body(title="Live title", content="live body"))
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "no_change")

    def test_editor_path_honours_earlier_revisions_pending_and_overwrite(self) -> None:
        widget = self._widget("rr-editor")
        thread = self._approved(widget, author_email=EDITOR_AUTHOR)
        self._patch(thread.id, {"content": "v2 proposed"}, email=EDITOR_AUTHOR)
        (v2,) = self._revisions(thread.id)
        self.client.post(
            f"/data/threads/{thread.id}/revisions/{v2.id}/reject",
            json={"reason": "no"}, headers=self._auth(EDITOR),
        )
        # Rewritten 2026-11-10: U5 refuses a new edit next to the open
        # declined v2, so the pending v3 is LEGACY data, seeded.
        self._legacy_pending(thread, ["v3 proposed"])
        self._grant(email=EDITOR_AUTHOR, node=("tab", self.root_tab_id), level="edit")

        body = self._body(title="Live title", content="v2 reworked")
        refused = self._rework(thread.id, v2.id, body, email=EDITOR_AUTHOR)
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["detail"]["code"], "earlier_revisions_pending")
        body["overwrite"] = True
        applied = self._rework(thread.id, v2.id, body, email=EDITOR_AUTHOR)
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertTrue(applied.json()["applied"])
        rev = applied.json()["revision"]
        self.assertEqual((rev["origin"], rev["status"], rev["resubmission_of_id"]), ("editor", "approved", v2.id))
        self.assertEqual(self._live(thread.id).content, "v2 reworked")
        statuses = [r.status for r in self._revisions(thread.id)]
        self.assertEqual(statuses, ["rejected", "overwritten", "approved"])

    def test_in_order_rule_still_applies_to_a_rework(self) -> None:
        """Rewritten 2026-11-10: a rework next to a pending edit can no longer
        be CREATED (`pending_edit_exists`), so the legacy pair (pending v3 +
        pending rework v4 of the declined v2) is seeded; the in-order rule
        still holds for it."""
        widget = self._widget("rr-in-order")
        thread = self._approved(widget)
        declined = self._declined_edit(thread, {"content": "declined edit"})
        self._legacy_pending(thread, ["a pending edit"])
        rework = ThreadRevisionV2(
            thread_id=thread.id, version=4, title="Live title", content="reworked", mentions=[],
            status="pending", origin="author", submitted_by_email=AUTHOR,
            submitted_at=BASE_TIME, resubmission_of_id=declined["id"], resubmission_number=1,
        )
        self.db.add(rework)
        self.db.commit()
        resp = self._approve(thread.id, rework.id)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "earlier_revisions_pending")

    def test_rework_with_a_pending_edit_409_pending_edit_exists(self) -> None:
        """plan_thread_pending_edit_update §3.2: the staged rework path never
        creates a SECOND pending edit (legacy data only, since U5)."""
        widget = self._widget("rr-pending-exists")
        thread = self._approved(widget)
        declined = self._declined_edit(thread, {"content": "declined edit"})
        (pending,) = self._legacy_pending(thread, ["a pending edit"])
        before = self._shape(thread.id)
        resp = self._rework(thread.id, declined["id"], self._body(title="Live title", content="reworked"))
        self.assertEqual(resp.status_code, 409, resp.text)
        detail = resp.json()["detail"]
        self.assertEqual(
            (detail["code"], detail["message"], detail["revision"]["id"]),
            ("pending_edit_exists", "You already have an edit awaiting review", pending["id"]),
        )
        self.assertEqual(self._shape(thread.id), before)
        # The author can still close the declined edit.
        self.assertEqual(self._close_revision(thread.id, declined["id"]).status_code, 204)

    def test_gates(self) -> None:
        widget = self._widget("rr-gates")
        thread = self._approved(widget)
        declined = self._declined_edit(thread, {"content": "declined edit"})
        body = self._body(title="Live title", content="reworked")
        for label, headers in (("editor", self._auth(EDITOR)), ("hub admin", self._member_auth(ADMIN))):
            with self.subTest(caller=label):
                self.assertEqual(self._rework(thread.id, declined["id"], body, headers=headers).status_code, 403)
                self.assertEqual(self._close_revision(thread.id, declined["id"], headers=headers).status_code, 403)
        self.assertEqual(self._rework(thread.id, 999999, body).status_code, 404)
        # Rewritten 2026-11-10: U5 — seeded (legacy), not staged.
        (pending,) = self._legacy_pending(thread, ["still pending"])
        resp = self._rework(thread.id, pending["id"], body)
        self.assertEqual((resp.status_code, resp.json()["detail"]["code"]), (409, "not_rejected"))
        self.assertEqual(self._close_revision(thread.id, declined["id"]).status_code, 204)
        resp = self._rework(thread.id, declined["id"], body)
        self.assertEqual((resp.status_code, resp.json()["detail"]["code"]), (409, "closed"))


# ---------------------------------------------------------
# §12.3 / §12.7 — author Close (replaced the soft delete; final).
# ---------------------------------------------------------


class CloseModel(_ResubmitFixture):
    def test_thread_close(self) -> None:
        link = "cl-thread"
        widget = self._widget(link)
        thread = self._rejected(widget, reason="Add a source")
        self.assertEqual(self._close(thread.id).status_code, 204)
        self.assertEqual(self._close(thread.id).status_code, 204)  # idempotent
        live = self._live(thread.id)
        self.assertIsNotNone(live.closed_at)
        self.assertEqual((live.status, live.closed_kind), ("rejected", "author"))
        # Inbox: in `closed`, not `items`; not counted; never in the list (R5).
        inbox = self._declined(link)
        self.assertEqual(inbox["items"], [])
        (item,) = inbox["closed"]
        self.assertEqual(
            (item["kind"], item["thread_id"], item["closed_kind"], item["reason"]),
            ("thread", thread.id, "author", "Add a source"),
        )
        self.assertIsNotNone(item["closed_at"])
        self.assertFalse(inbox["closed_truncated"])
        listed = self._list(link)
        self.assertEqual(listed["declined_count"], 0)
        self.assertNotIn(thread.id, {r["id"] for r in listed["items"]})
        # The author can still READ it (no 404 any more), and so can editors.
        for email in (AUTHOR, EDITOR):
            with self.subTest(reader=email):
                resp = self.client.get(f"/data/threads/{thread.id}", headers=self._auth(email))
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(resp.json()["status"], "rejected")
                sub = self.client.get(f"/data/threads/{thread.id}/submission-history", headers=self._auth(email))
                self.assertEqual(sub.status_code, 200, sub.text)
        (card,) = [c for c in self._history()["items"] if c["id"] == thread.id]
        self.assertEqual(card["closed_kind"], "author")
        self.assertIsNotNone(card["closed_at"])

    def test_thread_close_refuses_non_rejected(self) -> None:
        widget = self._widget("cl-thread-409")
        thread = self._pending(widget)
        resp = self._close(thread.id)
        self.assertEqual((resp.status_code, resp.json()["detail"]["code"]), (409, "not_rejected"))

    def test_revision_close(self) -> None:
        link = "cl-rev"
        widget = self._widget(link)
        thread = self._approved(widget)
        declined = self._declined_edit(thread, {"content": "declined edit"})
        self.assertEqual(self._close_revision(thread.id, declined["id"]).status_code, 204)
        self.assertEqual(self._close_revision(thread.id, declined["id"]).status_code, 204)
        inbox = self._declined(link)
        self.assertEqual(inbox["items"], [])
        (item,) = inbox["closed"]
        self.assertEqual(
            (item["kind"], item["revision_id"], item["closed_kind"], item["closed_by_version"]),
            ("edit", declined["id"], "author", None),
        )
        self.assertEqual(self._list(link)["declined_count"], 0)
        (row,) = self._revisions(thread.id)
        self.assertEqual((row.status, row.closed_kind, row.closed_by_version), ("rejected", "author", None))
        self.assertIsNotNone(row.closed_at)
        # The revision row exposes the close too (Revision Management History).
        resp = self.client.get(
            f"/data/threads/{thread.id}/revisions/{declined['id']}", headers=self._auth(EDITOR)
        )
        self.assertEqual(resp.json()["closed_kind"], "author")

    def test_revision_close_refuses_a_reworked_edit(self) -> None:
        link = "cl-rev-reworked"
        widget = self._widget(link)
        thread = self._approved(widget)
        declined = self._declined_edit(thread, {"content": "declined edit"})
        self.assertEqual(
            self._rework(thread.id, declined["id"], self._body(title="Live title", content="re")).status_code, 200
        )
        resp = self._close_revision(thread.id, declined["id"])
        self.assertEqual((resp.status_code, resp.json()["detail"]["code"]), (409, "already_resubmitted"))
        # A reworked edit is in NEITHER inbox list.
        inbox = self._declined(link)
        self.assertEqual((inbox["items"], inbox["closed"]), ([], []))

    def test_closed_list_is_capped_with_closed_truncated(self) -> None:
        link = "cl-cap"
        widget = self._widget(link)
        for i in range(3):
            self.assertEqual(self._close(self._rejected(widget, title=f"t{i}").id).status_code, 204)
        open_one = self._rejected(widget, title="open")
        with mock.patch.object(thread_service, "CLOSED_ITEMS_CAP", 2):
            body = self._declined(link)
        self.assertEqual(len(body["closed"]), 2)
        self.assertTrue(body["closed_truncated"])
        self.assertEqual([i["thread_id"] for i in body["items"]], [open_one.id])
        self.assertFalse(body["truncated"])


# ---------------------------------------------------------
# §12.4–§12.6 — a declined edit closes itself when a newer version goes
# live, behind the publisher's confirm, with a bell to its author.
# ---------------------------------------------------------


class AutoClose(_ResubmitFixture):
    def _closed_bells(self, email: str = AUTHOR) -> list[NotificationV2]:
        self.db.expire_all()
        return [n for n in self._notifications_for(email) if n.type == "thread_edit_closed"]

    def _rev(self, revision_id: int) -> ThreadRevisionV2:
        self.db.expire_all()
        return self.db.query(ThreadRevisionV2).filter(ThreadRevisionV2.id == revision_id).one()

    def test_approve_v6_confirms_then_supersedes_open_declined_v5(self) -> None:
        """Mutations 1 and 3: approving a newer edit while an OPEN declined
        one sits below it is the confirm 409 (`earlier` empty,
        `declined_open` = it); the overwrite publishes, closes it as
        superseded by that version, and bells its author with the reviewer
        as actor."""
        link = "ac-approve"
        widget = self._widget(link)
        thread = self._approved(widget)
        v5 = self._declined_edit(thread, {"title": "Room B", "content": "Monday, Room B"})
        # Rewritten 2026-11-10: U5 refuses staging next to the open v5, so the
        # newer pending edit is LEGACY data, seeded.
        (v6,) = self._legacy_pending(thread, ["Tuesday"])

        refused = self._approve(thread.id, v6["id"])
        self.assertEqual(refused.status_code, 409, refused.text)
        detail = refused.json()["detail"]
        self.assertEqual(detail["code"], "earlier_revisions_pending")
        self.assertEqual(detail["earlier"], [])
        self.assertEqual([r["id"] for r in detail["declined_open"]], [v5["id"]])
        self.assertEqual(detail["revision"]["id"], v6["id"])
        self.assertIsNone(self._rev(v5["id"]).closed_at)
        self.assertEqual(self._live(thread.id).content, "live body")
        self.assertEqual(self._closed_bells(), [])

        applied = self._approve(thread.id, v6["id"], overwrite=True)
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertEqual(self._live(thread.id).content, "Tuesday")
        row = self._rev(v5["id"])
        self.assertEqual(
            (row.status, row.closed_kind, row.closed_by_version), ("rejected", "superseded", v6["version"])
        )
        self.assertIsNotNone(row.closed_at)
        (bell,) = self._closed_bells()
        self.assertEqual(
            (bell.actor_email, bell.component_id, bell.thread_id, bell.excerpt, bell.comment_id),
            (EDITOR, widget.id, thread.id, f"Superseded by v{v6['version']}", None),
        )
        # It moved to the inbox's closed list, uncounted.
        inbox = self._declined(link)
        self.assertEqual([i for i in inbox["items"] if i["kind"] == "edit"], [])
        (item,) = inbox["closed"]
        self.assertEqual(
            (item["revision_id"], item["closed_kind"], item["closed_by_version"]),
            (v5["id"], "superseded", v6["version"]),
        )
        self.assertEqual(self._list(link)["declined_count"], 0)

    def test_both_lists_in_one_confirm(self) -> None:
        widget = self._widget("ac-both")
        thread = self._approved(widget)
        v2 = self._declined_edit(thread, {"content": "declined v2"})
        # Legacy shape since 2026-11-10 (U1/U5): seeded, not saved.
        v3, v4 = self._legacy_pending(thread, ["pending v3", "pending v4"])
        refused = self._approve(thread.id, v4["id"])
        detail = refused.json()["detail"]
        self.assertEqual(
            ([r["id"] for r in detail["earlier"]], [r["id"] for r in detail["declined_open"]]),
            ([v3["id"]], [v2["id"]]),
        )
        self.assertEqual(self._approve(thread.id, v4["id"], overwrite=True).status_code, 200)
        self.assertEqual(self._rev(v3["id"]).status, "overwritten")
        self.assertEqual(self._rev(v2["id"]).closed_kind, "superseded")

    def test_declined_v8_stays_open_when_v7_goes_live(self) -> None:
        """Mutation 2: `version < K`, not "all open declined edits"."""
        link = "ac-v8"
        widget = self._widget(link)
        thread = self._approved(widget)
        v2 = self._stage(thread.id, {"content": "pending v2"})
        # Rewritten 2026-11-10: a second save would update v2 (U1), so the
        # v3 built on it is LEGACY data, seeded, then declined.
        (v3,) = self._legacy_pending(thread, ["declined v3, built on v2"])
        self.assertEqual(self._reject(thread.id, v3["id"]).status_code, 200)
        resp = self._approve(thread.id, v2["id"])
        self.assertEqual(resp.status_code, 200, resp.text)  # nothing open below v2 → no confirm
        row = self._rev(v3["id"])
        self.assertEqual((row.status, row.closed_at, row.closed_kind), ("rejected", None, None))
        self.assertEqual(self._closed_bells(), [])
        self.assertEqual(self._list(link)["declined_count"], 1)

    def test_a_newer_edit_only_pending_leaves_it_open(self) -> None:
        """E16 — nothing went live. Rewritten 2026-11-10: the newer edit is
        seeded (legacy; U5 now refuses to stage it — and that refusal must
        not close anything either)."""
        widget = self._widget("ac-pending-only")
        thread = self._approved(widget)
        declined = self._declined_edit(thread, {"content": "declined"})
        refused = self._patch(thread.id, {"content": "newer"})
        self.assertEqual(refused.status_code, 409, refused.text)
        self._legacy_pending(thread, ["newer, still pending"])
        self.assertIsNone(self._rev(declined["id"]).closed_at)

    def test_editor_author_direct_edit_confirms_closes_and_sends_no_bell(self) -> None:
        """Mutation 4: the publisher IS the declined edit's author, so the
        existing self-exclusion means no bell (they just confirmed it)."""
        widget = self._widget("ac-direct")
        thread = self._approved(widget, author_email=EDITOR_AUTHOR)
        self._patch(thread.id, {"content": "v2 proposed"}, email=EDITOR_AUTHOR)
        (v2,) = self._revisions(thread.id)
        self.assertEqual(self._reject(thread.id, v2.id).status_code, 200)
        self._grant(email=EDITOR_AUTHOR, node=("tab", self.root_tab_id), level="edit")

        refused = self._patch(thread.id, {"content": "direct edit"}, email=EDITOR_AUTHOR)
        self.assertEqual(refused.status_code, 409, refused.text)
        detail = refused.json()["detail"]
        self.assertEqual((detail["earlier"], [r["id"] for r in detail["declined_open"]]), ([], [v2.id]))
        self.assertIsNone(detail["revision"])
        self.assertIsNone(self._rev(v2.id).closed_at)

        applied = self._patch(thread.id, {"content": "direct edit", "overwrite": True}, email=EDITOR_AUTHOR)
        self.assertEqual(applied.status_code, 200, applied.text)
        new_version = applied.json()["revision"]["version"]
        row = self._rev(v2.id)
        self.assertEqual((row.closed_kind, row.closed_by_version), ("superseded", new_version))
        self.assertEqual(self._closed_bells(EDITOR_AUTHOR), [])

    def test_editor_path_rework_links_its_source_and_closes_an_older_one(self) -> None:
        """§12.4: a rework going live does not close its OWN source (it is
        linked — reworked, not open), but an older open declined edit is
        closed."""
        widget = self._widget("ac-rework")
        thread = self._approved(widget, author_email=EDITOR_AUTHOR)
        # Rewritten 2026-11-10: the second edit can't be staged next to the
        # open declined v2 (U5), so v3 is seeded (legacy), then declined.
        self._patch(thread.id, {"content": "v2 proposed"}, email=EDITOR_AUTHOR)
        v2_id = self._revisions(thread.id)[-1].id
        self.assertEqual(self._reject(thread.id, v2_id).status_code, 200)
        (v3,) = self._legacy_pending(thread, ["v3 proposed"])
        v3_id = v3["id"]
        self.assertEqual(self._reject(thread.id, v3_id).status_code, 200)
        self._grant(email=EDITOR_AUTHOR, node=("tab", self.root_tab_id), level="edit")

        body = self._body(title="Live title", content="v3 reworked")
        refused = self._rework(thread.id, v3_id, body, email=EDITOR_AUTHOR)
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual([r["id"] for r in refused.json()["detail"]["declined_open"]], [v2_id])
        body["overwrite"] = True
        applied = self._rework(thread.id, v3_id, body, email=EDITOR_AUTHOR)
        self.assertEqual(applied.status_code, 200, applied.text)
        new_version = applied.json()["revision"]["version"]
        self.assertEqual(applied.json()["revision"]["resubmission_of_id"], v3_id)
        source, older = self._rev(v3_id), self._rev(v2_id)
        self.assertEqual((source.closed_at, source.closed_kind), (None, None))
        self.assertEqual((older.closed_kind, older.closed_by_version), ("superseded", new_version))

    def test_resubmitting_a_superseded_edit_409_closed(self) -> None:
        """E15 — the composer was open when a newer version went live."""
        widget = self._widget("ac-stale")
        thread = self._approved(widget)
        v2 = self._declined_edit(thread, {"content": "declined"})
        # Rewritten 2026-11-10: U5 — seeded (legacy), not staged.
        (v3,) = self._legacy_pending(thread, ["Tuesday"])
        self.assertEqual(self._approve(thread.id, v3["id"], overwrite=True).status_code, 200)
        resp = self._rework(thread.id, v2["id"], self._body(title="Live title", content="Monday, Room C"))
        self.assertEqual(resp.status_code, 409, resp.text)
        detail = resp.json()["detail"]
        self.assertEqual(
            (detail["code"], detail["closed_kind"], detail["closed_by_version"]),
            ("closed", "superseded", v3["version"]),
        )
        self.assertEqual(self._live(thread.id).content, "Tuesday")
        # Closing an already-superseded edit is the idempotent 204, and keeps
        # the superseded kind.
        self.assertEqual(self._close_revision(thread.id, v2["id"]).status_code, 204)
        self.assertEqual(self._rev(v2["id"]).closed_kind, "superseded")

    def test_pending_rework_of_the_source_is_not_blocked_by_it(self) -> None:
        """A staged rework links its source, so approving the rework never
        lists that source in `declined_open`."""
        widget = self._widget("ac-own-source")
        thread = self._approved(widget)
        declined = self._declined_edit(thread, {"content": "declined"})
        rework = self._rework(thread.id, declined["id"], self._body(title="Live title", content="reworked"))
        self.assertEqual(rework.status_code, 200, rework.text)
        resp = self._approve(thread.id, rework.json()["revision"]["id"])
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(self._rev(declined["id"]).closed_at)
        self.assertEqual(self._closed_bells(), [])


# ---------------------------------------------------------
# §12.8 — G1 (rollback safety) and G3 (self-published resubmission).
# ---------------------------------------------------------


class GapFixes(_ResubmitFixture):
    def test_g1_raw_insert_without_submitted_at_gets_a_value(self) -> None:
        """What a rolled-back backend's create path does: INSERT without the
        new column. The DB default must fill it (the model's
        `server_default`, mirrored by the migration's `SET DEFAULT now()`)."""
        from sqlalchemy import text as sql_text

        widget = self._widget("g1-insert")
        self.db.execute(
            sql_text(
                "INSERT INTO threads (component_id, title, content, author_email, mentions, status, "
                "up_count, down_count, comment_count, created_at, version, resubmission_count) "
                "VALUES (:c, 't', 'c', :a, '[]', 'pending', 0, 0, 0, :now, 1, 0)"
            ),
            {"c": widget.id, "a": AUTHOR, "now": BASE_TIME},
        )
        self.db.commit()
        row = (
            self.db.query(ThreadV2)
            .filter(ThreadV2.component_id == widget.id)
            .one()
        )
        self.assertIsNotNone(row.submitted_at)

    def test_g3_self_published_resubmission_ends_with_a_published_round(self) -> None:
        widget = self._widget("g3-published")
        thread = self._rejected(widget, author=EDITOR_AUTHOR, reason="why")
        self._grant(email=EDITOR_AUTHOR, node=("tab", self.root_tab_id), level="edit")
        resp = self._resubmit(thread.id, self._body(title="Published now"), email=EDITOR_AUTHOR)
        self.assertEqual((resp.status_code, resp.json()["status"]), (200, "approved"))

        rounds = self.client.get(
            f"/data/threads/{thread.id}/submission-history", headers=self._auth(EDITOR)
        ).json()
        self.assertEqual(
            [(r["round"], r["status"], r["reason"], r["title"]) for r in rounds],
            [(0, "rejected", "why", "Declined title"), (1, "published", None, "Published now")],
        )
        (v1,) = self._all_revisions(thread.id)
        self.assertEqual(rounds[1]["reviewed_by_email"], EDITOR_AUTHOR)
        self.assertEqual(
            thread_service._as_utc(thread_service.datetime.fromisoformat(rounds[1]["reviewed_at"])),
            thread_service._as_utc(v1.reviewed_at),
        )
        # History (one row per decision): only the STORED round 0 is a row;
        # the thread part of that row still reports the published round.
        (row,) = [c for c in self._history()["items"] if c["id"] == thread.id]
        self.assertEqual((row["decision"]["round"], row["decision"]["status"]), (0, "rejected"))
        self.assertEqual(
            (row["last_decision"]["status"], row["last_decision"]["round"], row["decision_count"]),
            ("published", 1, 1),
        )
        self.assertEqual(row["last_decision"]["reviewed_by_email"], EDITOR_AUTHOR)

    def test_g3_no_published_round_for_an_ordinary_approval_or_an_editor_post(self) -> None:
        widget = self._widget("g3-none")
        approved = self._pending(widget)
        self.assertEqual(self._approve_thread(approved.id).status_code, 200)
        rounds = self.client.get(
            f"/data/threads/{approved.id}/submission-history", headers=self._auth(EDITOR)
        ).json()
        self.assertEqual([r["status"] for r in rounds], ["approved"])
        posted = self._approved(widget, author_email=EDITOR)  # editor's own post: never decided
        rounds = self.client.get(
            f"/data/threads/{posted.id}/submission-history", headers=self._auth(EDITOR)
        ).json()
        self.assertEqual(rounds, [])


# ---------------------------------------------------------
# §4.6 / decision 13 — grouped History.
# ---------------------------------------------------------


class DecisionHistory(_ResubmitFixture):
    """Threads Management History, 2026-11-09 rework: ONE ROW PER DECISION
    (replacing decision 13's one row per thread), row-level filters, type,
    search, group by thread / widget, newest / oldest."""

    def _declined_then_approved(self, widget, **kwargs) -> ThreadV2:
        """Round 0 rejected by EDITOR, round 1 approved by EDITOR2."""
        thread = self._rejected(widget, **kwargs)
        self.assertEqual(self._resubmit(thread.id, self._body(title="Second")).status_code, 200)
        self.assertEqual(self._approve_thread(thread.id, email=EDITOR2).status_code, 200)
        return thread

    @staticmethod
    def _keys(items: list[dict]) -> list[tuple[int, int]]:
        return [(i["id"], i["decision"]["round"]) for i in items]

    def _walk(self, params: dict | None = None) -> list[dict]:
        items: list[dict] = []
        cursor = None
        for _ in range(50):
            query = dict(params or {})
            if cursor:
                query["cursor"] = cursor
            body = self._history(query)
            items.extend(body["items"])
            cursor = body["next_cursor"]
            if not cursor:
                return items
        self.fail("cursor never terminated")

    def test_one_row_per_decision_newest_first(self) -> None:
        widget = self._widget("gh-rows")
        a = self._declined_then_approved(widget)
        b = self._rejected(widget, title="b", reason="b reason")
        items = self._history()["items"]
        self.assertEqual(self._keys(items), [(b.id, 0), (a.id, 1), (a.id, 0)])
        a1, a0 = items[1], items[2]
        self.assertEqual(
            (a0["decision"]["status"], a0["decision"]["title"], a0["decision"]["reason"],
             a0["decision"]["reviewed_by_email"]),
            ("rejected", "Declined title", "Needs work", EDITOR),
        )
        self.assertEqual(
            (a1["decision"]["status"], a1["decision"]["title"], a1["decision"]["reason"],
             a1["decision"]["reviewed_by_email"]),
            ("approved", "Second", None, EDITOR2),
        )
        self.assertNotEqual(a0["decision"]["submission_id"], a1["decision"]["submission_id"])
        # The thread half of both rows is the thread NOW.
        self.assertEqual(a0["status"], "approved")
        self.assertEqual(a0["decision_count"], 2)
        self.assertIsNone(a0["group_size"])

    def test_a_never_published_thread_shows_every_rejection(self) -> None:
        """The view-only fix: a new thread declined, resubmitted, declined
        again has a row for EACH decline."""
        widget = self._widget("gh-never")
        thread = self._rejected(widget, reason="first")
        self.assertEqual(self._resubmit(thread.id, self._body(title="Again")).status_code, 200)
        self.assertEqual(self._reject_thread(thread.id, "second").status_code, 200)
        rows = [i for i in self._history()["items"] if i["id"] == thread.id]
        self.assertEqual(
            [(r["decision"]["round"], r["decision"]["reason"]) for r in rows], [(1, "second"), (0, "first")]
        )

    def test_filters_are_per_row(self) -> None:
        widget = self._widget("gh-filters")
        a = self._declined_then_approved(widget)
        cases = (
            ({"status": "rejected"}, [(a.id, 0)]),
            ({"status": "approved"}, [(a.id, 1)]),
            ({"reviewer": EDITOR}, [(a.id, 0)]),
            ({"reviewer": EDITOR2}, [(a.id, 1)]),
            ({"author": AUTHOR}, [(a.id, 1), (a.id, 0)]),
            ({"type": "new"}, [(a.id, 0)]),
            ({"type": "resubmission"}, [(a.id, 1)]),
            ({"type": ["new", "resubmission"]}, [(a.id, 1), (a.id, 0)]),
            ({"status": "rejected", "reviewer": EDITOR2}, []),
        )
        for params, expected in cases:
            with self.subTest(params=params):
                self.assertEqual(self._keys(self._history(params)["items"]), expected)

    def test_search_matches_decided_title_current_title_and_widget(self) -> None:
        widget = self._widget("gh-search")
        a = self._declined_then_approved(widget, title="Budget 100% draft")
        widget_title = self._history()["items"][0]["widget_title"]
        cases = (
            ("budget", [(a.id, 0)]),             # only round 0's decided title
            ("SECOND", [(a.id, 1), (a.id, 0)]),  # current title: both rows
            ("100%", [(a.id, 0)]),               # `%` is literal
            ("1_0", []),                         # `_` is literal
            (widget_title.upper(), [(a.id, 1), (a.id, 0)]),
            ("no such text", []),
            ("   ", [(a.id, 1), (a.id, 0)]),     # blank = no filter
        )
        for q, expected in cases:
            with self.subTest(q=q):
                self.assertEqual(self._keys(self._history({"q": q})["items"]), expected)

    def test_group_by_thread_keeps_rows_together_both_orders(self) -> None:
        widget = self._widget("gh-group-thread")
        a = self._rejected(widget, title="a")           # t1: a r0
        b = self._rejected(widget, title="b")           # t2: b r0
        self.assertEqual(self._resubmit(a.id, self._body(title="a2")).status_code, 200)
        self.assertEqual(self._approve_thread(a.id).status_code, 200)  # t3: a r1
        self.assertEqual(self._keys(self._history()["items"]), [(a.id, 1), (b.id, 0), (a.id, 0)])

        grouped = self._history({"group": "thread"})["items"]
        self.assertEqual(self._keys(grouped), [(a.id, 1), (a.id, 0), (b.id, 0)])
        self.assertEqual([i["group_size"] for i in grouped], [2, 2, 1])
        oldest = self._history({"group": "thread", "order": "oldest"})["items"]
        self.assertEqual(self._keys(oldest), [(a.id, 0), (a.id, 1), (b.id, 0)])
        self.assertEqual(
            self._keys(self._history({"order": "oldest"})["items"]), [(a.id, 0), (b.id, 0), (a.id, 1)]
        )
        # group_size counts under the filters.
        filtered = self._history({"group": "thread", "status": "rejected"})["items"]
        self.assertEqual([(k, i["group_size"]) for k, i in zip(self._keys(filtered), filtered)],
                         [((b.id, 0), 1), ((a.id, 0), 1)])

    def test_group_by_widget(self) -> None:
        w1 = self._widget("gh-group-w1")
        w2 = self._widget("gh-group-w2")
        x = self._rejected(w1, title="x")
        y = self._rejected(w2, title="y")
        z = self._rejected(w1, title="z")
        keys = self._keys(self._history({"group": "widget"})["items"])
        self.assertEqual(keys, [(z.id, 0), (x.id, 0), (y.id, 0)])

    def test_grouped_and_ordered_cursor_walks_match_one_page(self) -> None:
        widget = self._widget("gh-group-cursor")
        a = self._rejected(widget, title="a")
        b = self._rejected(widget, title="b")
        self.assertEqual(self._resubmit(a.id, self._body(title="a2")).status_code, 200)
        self.assertEqual(self._reject_thread(a.id).status_code, 200)
        self._rejected(widget, title="c")
        for params in ({}, {"group": "thread"}, {"group": "widget"}, {"group": "thread", "order": "oldest"},
                       {"order": "oldest"}):
            with self.subTest(params=params):
                whole = self._keys(self._history(params)["items"])
                with mock.patch.object(thread_service, "THREADS_PAGE_SIZE", 1):
                    walked = self._keys(self._walk(params))
                self.assertEqual(walked, whole)
                self.assertEqual(len(walked), 4)
        del b

    def test_cursor_from_another_mode_is_400(self) -> None:
        widget = self._widget("gh-cursor-mode")
        for i in range(2):
            self._rejected(widget, title=f"t{i}")
        with mock.patch.object(thread_service, "THREADS_PAGE_SIZE", 1):
            flat = self._history()["next_cursor"]
            grouped = self._history({"group": "thread"})["next_cursor"]
        for params in ({"group": "thread", "cursor": flat}, {"cursor": grouped}, {"cursor": "junk"}):
            with self.subTest(params=params):
                resp = self.client.get(
                    "/data/threads/moderation/history", params=params, headers=self._auth(EDITOR)
                )
                self.assertEqual(resp.status_code, 400, resp.text)

    def test_invalid_type_group_order_422(self) -> None:
        for params in ({"type": "bogus"}, {"group": "author"}, {"order": "sideways"}, {"q": "x" * 201}):
            with self.subTest(params=params):
                resp = self.client.get(
                    "/data/threads/moderation/history", params=params, headers=self._auth(EDITOR)
                )
                self.assertEqual(resp.status_code, 422, resp.text)

    def test_facets_come_from_rounds(self) -> None:
        widget = self._widget("gh-facets")
        self._declined_then_approved(widget)
        resp = self.client.get("/data/threads/moderation/history/facets", headers=self._auth(EDITOR))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual({p["email"] for p in resp.json()["reviewers"]}, {EDITOR, EDITOR2})
        self.assertEqual({p["email"] for p in resp.json()["authors"]}, {AUTHOR})

    def test_cursor_walk(self) -> None:
        widget = self._widget("gh-cursor")
        ids = [self._rejected(widget, title=f"t{i}").id for i in range(3)]
        seen: list[int] = []
        cursor = None
        with mock.patch.object(thread_service, "THREADS_PAGE_SIZE", 1):
            for _ in range(10):
                body = self._history({"cursor": cursor} if cursor else {})
                seen.extend(i["id"] for i in body["items"])
                cursor = body["next_cursor"]
                if not cursor:
                    break
        self.assertEqual(seen, list(reversed(ids)))

    def test_scoped_to_the_moderated_set(self) -> None:
        widget = self._widget("gh-scope")
        thread = self._rejected(widget)
        self.assertNotIn(thread.id, [c["id"] for c in self._history(email=VIEWER)["items"]])

    def test_editor_auto_published_thread_never_appears(self) -> None:
        widget = self._widget("gh-m5")
        resp = self.client.post(
            "/data/threads/component/gh-m5", json=self._body(title="Mine"), headers=self._auth(EDITOR)
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["status"], "approved")
        self.assertNotIn(resp.json()["id"], [c["id"] for c in self._history()["items"]])
        self.assertEqual(self._rounds(resp.json()["id"]), [])


# ---------------------------------------------------------
# §4.6 — the round timeline endpoint.
# ---------------------------------------------------------


class SubmissionHistoryEndpoint(_ResubmitFixture):
    def _get(self, thread_id: int, email: str):
        return self.client.get(f"/data/threads/{thread_id}/submission-history", headers=self._auth(email))

    def test_gates(self) -> None:
        widget = self._widget("sh-gates")
        thread = self._rejected(widget)
        self.assertEqual(self._get(thread.id, STRANGER).status_code, 403)
        self.assertEqual(self._get(thread.id, VIEWER).status_code, 404)
        self.assertEqual(self._get(thread.id, AUTHOR).status_code, 200)
        self.assertEqual(self._get(thread.id, EDITOR).status_code, 200)

    def test_rounds_plus_synthesized_pending(self) -> None:
        widget = self._widget("sh-rounds")
        thread = self._rejected(widget, reason="why")
        self.assertEqual(self._resubmit(thread.id, self._body(title="Again")).status_code, 200)
        rounds = self._get(thread.id, EDITOR).json()
        self.assertEqual(
            [(r["round"], r["status"], r["reason"], r["title"]) for r in rounds],
            [(0, "rejected", "why", "Declined title"), (1, "pending", None, "Again")],
        )
        self.assertIsNone(rounds[1]["reviewed_at"])
        self.assertEqual(rounds[0]["reviewed_by_email"], EDITOR)


# ---------------------------------------------------------
# §4.8 / D9 — the decline excerpt is the reason, not the stripper's output.
# ---------------------------------------------------------


class Notifications(_ResubmitFixture):
    REASON = "Please fix  (see [doc])\n and *retry*  now"
    EXPECTED = "Reason: Please fix (see [doc]) and *retry* now"

    def test_thread_decline_excerpt_keeps_parentheses_and_brackets(self) -> None:
        widget = self._widget("nt-parens")
        self._rejected(widget, reason=self.REASON)
        (row,) = [n for n in self._notifications_for(AUTHOR) if n.type == "thread_rejected"]
        self.assertEqual(row.excerpt, self.EXPECTED)

    def test_edit_decline_excerpt_is_the_reason(self) -> None:
        widget = self._widget("nt-edit")
        thread = self._approved(widget)
        self._declined_edit(thread, {"content": "x"}, reason=self.REASON)
        (row,) = [n for n in self._notifications_for(AUTHOR) if n.type == "thread_edit_rejected"]
        self.assertEqual(row.excerpt, self.EXPECTED)

    def test_long_reason_is_truncated_to_200(self) -> None:
        widget = self._widget("nt-long")
        self._rejected(widget, reason="word " * 150)
        (row,) = [n for n in self._notifications_for(AUTHOR) if n.type == "thread_rejected"]
        self.assertEqual(len(row.excerpt), 200)
        self.assertTrue(row.excerpt.startswith("Reason: word word"))
        self.assertTrue(row.excerpt.endswith("…"))

    def test_approve_excerpt_unchanged(self) -> None:
        widget = self._widget("nt-approve")
        thread = self._pending(widget, content="a **bold** (body)")
        self.assertEqual(self._approve_thread(thread.id).status_code, 200)
        (row,) = [n for n in self._notifications_for(AUTHOR) if n.type == "thread_approved"]
        self.assertEqual(row.excerpt, thread_service.generate_notification_excerpt("a **bold** (body)"))


# ---------------------------------------------------------
# §2 — constraints and cascade.
# ---------------------------------------------------------


class ModelConstraints(_ResubmitFixture):
    def _round(self, thread, **kwargs) -> ThreadSubmissionV2:
        values = dict(
            thread_id=thread.id, round=0, title="t", content="c", mentions=[], status="rejected",
            reason=None, submitted_by_email=AUTHOR, submitted_at=BASE_TIME,
            reviewed_by_email=EDITOR, reviewed_at=BASE_TIME,
        )
        values.update(kwargs)
        return ThreadSubmissionV2(**values)

    def _expect_integrity_error(self, row) -> None:
        self.db.add(row)
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    def test_bad_round_status_raises(self) -> None:
        thread = self._pending(self._widget("mc-status"))
        self._expect_integrity_error(self._round(thread, status="pending"))

    def test_reason_on_an_approved_round_raises(self) -> None:
        thread = self._pending(self._widget("mc-reason"))
        self._expect_integrity_error(self._round(thread, status="approved", reason="no"))

    def test_duplicate_round_raises(self) -> None:
        thread = self._pending(self._widget("mc-dup"))
        self.db.add(self._round(thread))
        self.db.commit()
        self._expect_integrity_error(self._round(thread))

    def _expect_update_error(self, row, **values) -> None:
        for key, value in values.items():
            setattr(row, key, value)
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    def test_threads_closed_shape_each_arm(self) -> None:
        """ck_threads_closed_shape (§12.3), one arm per subtest; the legal
        shape commits."""
        widget = self._widget("mc-thread-closed")
        cases = {
            "closed_at without kind": ("rejected", dict(closed_at=BASE_TIME)),
            "kind without closed_at": ("rejected", dict(closed_kind="author")),
            "closed but not rejected": ("pending", dict(closed_at=BASE_TIME, closed_kind="author")),
            "superseded is edits-only": ("rejected", dict(closed_at=BASE_TIME, closed_kind="superseded")),
        }
        for label, (status_, values) in cases.items():
            with self.subTest(arm=label):
                thread = self._insert_thread(widget.id, author_email=AUTHOR, status=status_)
                self._expect_update_error(self._live(thread.id), **values)
        ok = self._insert_thread(widget.id, author_email=AUTHOR, status="rejected")
        row = self._live(ok.id)
        row.closed_at, row.closed_kind = BASE_TIME, "author"
        self.db.commit()

    def test_revision_reason_requires_rejected(self) -> None:
        thread = self._approved(self._widget("mc-rev-reason"))
        revision = self._insert_revision(thread, version=2)
        self._expect_update_error(revision, rejection_reason="x")

    def test_revisions_closed_shape_each_arm(self) -> None:
        """ck_thread_revisions_closed_shape (§12.3), one arm per subtest;
        both legal shapes commit."""
        thread = self._approved(self._widget("mc-rev-closed"))
        cases = {
            "closed_at without kind": ("rejected", dict(closed_at=BASE_TIME)),
            "kind without closed_at": ("rejected", dict(closed_kind="author")),
            "closed but not rejected": ("pending", dict(closed_at=BASE_TIME, closed_kind="author")),
            "unknown kind": ("rejected", dict(closed_at=BASE_TIME, closed_kind="deleted")),
            "superseded without version": ("rejected", dict(closed_at=BASE_TIME, closed_kind="superseded")),
            "author with a version": (
                "rejected", dict(closed_at=BASE_TIME, closed_kind="author", closed_by_version=4),
            ),
            "version on an open row": ("rejected", dict(closed_by_version=4)),
        }
        version = 2
        for label, (status_, values) in cases.items():
            with self.subTest(arm=label):
                reviewed = dict(reviewed_at=BASE_TIME, reviewed_by_email=EDITOR) if status_ == "rejected" else {}
                revision = self._insert_revision(thread, version=version, status=status_, **reviewed)
                version += 1
                self._expect_update_error(revision, **values)
        for values in (
            dict(closed_at=BASE_TIME, closed_kind="author"),
            dict(closed_at=BASE_TIME, closed_kind="superseded", closed_by_version=9),
        ):
            revision = self._insert_revision(
                thread, version=version, status="rejected", reviewed_at=BASE_TIME, reviewed_by_email=EDITOR
            )
            version += 1
            for key, value in values.items():
                setattr(revision, key, value)
            self.db.commit()

    def test_rounds_cascade_with_the_thread(self) -> None:
        widget = self._widget("mc-cascade")
        thread = self._rejected(widget)
        self.assertEqual(len(self._rounds(thread.id)), 1)
        self.assertEqual(self.client.delete(f"/data/threads/{thread.id}", headers=self._member_auth(ADMIN)).status_code, 204)
        self.assertEqual(self._rounds(thread.id), [])


# ---------------------------------------------------------
# §4.9 — rekey_email covers the new table and thread_revisions.
# ---------------------------------------------------------


class RekeyEmail(_ResubmitFixture):
    def _rekey(self, old: str, new: str) -> dict:
        """Through its OWN short-lived session, committed and closed — the
        `_seed_real_admin` pattern. Rekeying through `self.db` is
        order-dependent here: a leaked request session's rollback-on-return
        (they share the one StaticPool connection) can discard `self.db`'s
        autoflushed-but-uncommitted UPDATEs before `self.db.commit()`."""
        db = self._session_factory()
        try:
            counts = thread_service.rekey_email(db, old_email=old, new_email=new)
            db.commit()
            return counts
        finally:
            db.close()

    def test_rekey_rewrites_submissions_and_revisions(self) -> None:
        widget = self._widget("rk")
        thread = self._rejected(widget)
        live = self._approved(widget)
        self._declined_edit(live, {"content": "x"})
        counts = self._rekey(AUTHOR, "renamed@example.com")
        self.assertEqual(counts["thread_submissions"], 1)
        self.assertEqual(counts["thread_revisions"], 2)  # v1 (submitted_by) + the declined edit
        (round0,) = self._rounds(thread.id)
        self.assertEqual(round0.submitted_by_email, "renamed@example.com")
        self.assertTrue(all(r.submitted_by_email == "renamed@example.com" for r in self._all_revisions(live.id)))
        counts = self._rekey(EDITOR, "ed2@example.com")
        self.assertEqual(counts["thread_submissions"], 1)
        self.assertEqual(self._rounds(thread.id)[0].reviewed_by_email, "ed2@example.com")


# ---------------------------------------------------------
# plan_thread_pending_edit_update_2026-11-10 — an author's save updates
# their pending edit in place (U1–U4, U7), the open-declined gate (U5), the
# decision stale checks (§3.3, U9) and the pending-resubmission guard (U10).
# Every staging author is `AUTHOR` (view grant, plain `_auth`), so none of
# these can pass by riding the editor path.
# ---------------------------------------------------------


class PendingEditUpdate(_ResubmitFixture):
    PENDING_PATH = "/data/threads/revision-moderation/pending"

    def _row(self, revision_id: int) -> ThreadRevisionV2:
        self.db.expire_all()
        return self.db.query(ThreadRevisionV2).filter(ThreadRevisionV2.id == revision_id).one()

    def test_second_save_updates_the_same_row(self) -> None:
        widget = self._widget("peu-same-row")
        thread = self._approved(widget)
        first = self._stage(thread.id, {"title": "T2", "content": "first"})
        submitted_at = self._row(first["id"]).submitted_at
        self.assertEqual((first["edit_count"], first["updated_at"]), (0, None))

        second = self._stage(thread.id, {"title": "T3", "content": "second"})
        self.assertEqual((second["id"], second["version"], second["status"]), (first["id"], 2, "pending"))
        self.assertEqual(second["edit_count"], 1)
        self.assertIsNotNone(second["updated_at"])
        row = self._row(first["id"])
        self.assertEqual((row.title, row.content, row.edit_count), ("T3", "second", 1))
        self.assertEqual(row.submitted_at, submitted_at)
        self.assertEqual(len(self._revisions(thread.id)), 1)
        # Revision Management's queue: one item, carrying the token.
        queue = self.client.get(self.PENDING_PATH, headers=self._auth(EDITOR))
        self.assertEqual(queue.status_code, 200, queue.text)
        (item,) = queue.json()["items"]
        self.assertEqual((item["id"], item["edit_count"]), (first["id"], 1))
        self.assertIsNotNone(item["updated_at"])
        # Live text untouched.
        self.assertEqual(self._live(thread.id).content, "live body")

    def test_omitted_field_keeps_the_pending_value(self) -> None:
        widget = self._widget("peu-omitted")
        thread = self._approved(widget)
        rev = self._stage(thread.id, {"title": "Proposed T", "content": "c2"})
        self._stage(thread.id, {"content": "c3"})
        row = self._row(rev["id"])
        self.assertEqual((row.title, row.content), ("Proposed T", "c3"))

    def test_unchanged_save_is_a_noop(self) -> None:
        widget = self._widget("peu-noop")
        thread = self._approved(widget)
        rev = self._stage(thread.id, {"title": "T", "content": "c"})
        again = self._stage(thread.id, {"title": "T", "content": "c", "mentions": []})
        self.assertEqual((again["id"], again["edit_count"], again["updated_at"]), (rev["id"], 0, None))
        self.assertEqual(self._row(rev["id"]).edit_count, 0)

    def test_plain_edit_updated_back_to_live_stays_pending(self) -> None:
        """U7 — no auto-withdraw."""
        widget = self._widget("peu-back-to-live")
        thread = self._approved(widget)
        rev = self._stage(thread.id, {"content": "changed"})
        back = self._stage(thread.id, {"title": "Live title", "content": "live body"})
        self.assertEqual((back["id"], back["status"], back["edit_count"]), (rev["id"], "pending", 1))

    def test_updating_a_rework_keeps_its_link_and_number(self) -> None:
        """U2/U3 — still "v3 (Revision 1 of v2)" after the update."""
        widget = self._widget("peu-rework-link")
        thread = self._approved(widget)
        declined = self._declined_edit(thread, {"content": "declined edit"})
        rework = self._rework(thread.id, declined["id"], self._body(title="Live title", content="rework one"))
        self.assertEqual(rework.status_code, 200, rework.text)
        rev = rework.json()["revision"]

        updated = self._stage(thread.id, {"content": "rework two"})
        self.assertEqual(updated["id"], rev["id"])
        self.assertEqual(
            (updated["resubmission_of_id"], updated["resubmission_number"], updated["resubmission_root_version"]),
            (declined["id"], 1, declined["version"]),
        )
        self.assertEqual(updated["edit_count"], 1)
        self.assertEqual(self._row(rev["id"]).content, "rework two")

    def _rework_pending(self, link: str):
        widget = self._widget(link)
        thread = self._approved(widget)
        declined = self._declined_edit(thread, {"title": "Declined T", "content": "declined edit"})
        rework = self._rework(thread.id, declined["id"], self._body(title="Rework T", content="rework body"))
        self.assertEqual(rework.status_code, 200, rework.text)
        return thread, declined, rework.json()["revision"]

    def test_u4_rework_updated_back_to_the_declined_text_409(self) -> None:
        thread, _declined, rev = self._rework_pending("peu-u4-declined")
        resp = self._patch(thread.id, {"title": "Declined T", "content": "declined edit", "mentions": []})
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "unchanged_resubmission")
        row = self._row(rev["id"])
        self.assertEqual((row.title, row.content, row.edit_count, row.updated_at), ("Rework T", "rework body", 0, None))

    def test_u4_rework_updated_to_the_live_text_409_no_change(self) -> None:
        thread, _declined, rev = self._rework_pending("peu-u4-live")
        resp = self._patch(thread.id, {"title": "Live title", "content": "live body"})
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "no_change")
        self.assertEqual(self._row(rev["id"]).edit_count, 0)

    def test_u4_only_a_full_match_is_refused(self) -> None:
        thread, _declined, rev = self._rework_pending("peu-u4-partial")
        steps = (
            ("title only matches", {"title": "Declined T", "content": "something else"}),
            ("content only matches", {"title": "Another T", "content": "declined edit"}),
            # Same (empty) mention set as the declined edit, different text.
            ("mentions only match", {"title": "Third T", "content": "third body", "mentions": []}),
        )
        for count, (label, body) in enumerate(steps, start=1):
            with self.subTest(label):
                resp = self._patch(thread.id, body)
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(resp.json()["revision"]["edit_count"], count)

    def test_legacy_thread_updates_only_the_newest_pending(self) -> None:
        widget = self._widget("peu-legacy")
        thread = self._approved(widget)
        v2, v3 = self._legacy_pending(thread, ["legacy v2", "legacy v3"])
        updated = self._stage(thread.id, {"content": "v3 updated"})
        self.assertEqual(updated["id"], v3["id"])
        self.assertEqual((self._row(v2["id"]).content, self._row(v2["id"]).edit_count), ("legacy v2", 0))
        self.assertEqual(self._row(v3["id"]).content, "v3 updated")

    def test_editor_author_still_goes_live_and_overwrite_is_unchanged(self) -> None:
        widget = self._widget("peu-editor")
        thread = self._approved(widget, author_email=EDITOR_AUTHOR)
        (legacy,) = self._legacy_pending(thread, ["their old pending edit"])
        self._grant(email=EDITOR_AUTHOR, node=("tab", self.root_tab_id), level="edit")
        refused = self._patch(thread.id, {"content": "direct"}, email=EDITOR_AUTHOR)
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["detail"]["code"], "earlier_revisions_pending")
        applied = self._patch(thread.id, {"content": "direct", "overwrite": True}, email=EDITOR_AUTHOR)
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertTrue(applied.json()["applied"])
        self.assertEqual(self._row(legacy["id"]).status, "overwritten")
        self.assertEqual(self._live(thread.id).content, "direct")

    def test_non_author_403(self) -> None:
        widget = self._widget("peu-403")
        thread = self._approved(widget)
        rev = self._stage(thread.id, {"content": "mine"})
        for email in (VIEWER, EDITOR):
            with self.subTest(email=email):
                self.assertEqual(self._patch(thread.id, {"content": "theirs"}, email=email).status_code, 403)
        self.assertEqual(self._row(rev["id"]).edit_count, 0)


class OpenDeclinedGate(_ResubmitFixture):
    """U5 — an open declined edit blocks a NEW edit (not an update)."""

    def _detail(self, thread_id: int, email: str = AUTHOR) -> dict:
        resp = self.client.get(f"/data/threads/{thread_id}", headers=self._auth(email))
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def test_new_edit_with_an_open_declined_edit_409(self) -> None:
        widget = self._widget("u5-refused")
        thread = self._approved(widget)
        declined = self._declined_edit(thread, {"content": "declined edit"})
        before = self._shape(thread.id)
        resp = self._patch(thread.id, {"content": "a brand new edit"})
        self.assertEqual(resp.status_code, 409, resp.text)
        detail = resp.json()["detail"]
        self.assertEqual(detail["code"], "declined_edit_open")
        self.assertEqual(
            detail["message"],
            f"Resubmit or close your declined edit (v{declined['version']}) before making a new one",
        )
        self.assertEqual(detail["revision"]["id"], declined["id"])
        self.assertEqual(self._shape(thread.id), before)

    def test_an_unchanged_save_is_still_a_noop_not_a_409(self) -> None:
        """Judgement call: the gate guards row CREATION; a no-op creates none."""
        widget = self._widget("u5-noop")
        thread = self._approved(widget)
        self._declined_edit(thread, {"content": "declined edit"})
        resp = self._patch(thread.id, {"title": "Live title", "content": "live body"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["applied"])

    def test_allowed_after_the_author_closes_it(self) -> None:
        widget = self._widget("u5-closed")
        thread = self._approved(widget)
        declined = self._declined_edit(thread, {"content": "declined edit"})
        self.assertEqual(self._close_revision(thread.id, declined["id"]).status_code, 204)
        rev = self._stage(thread.id, {"content": "new edit"})
        self.assertEqual(rev["version"], declined["version"] + 1)

    def test_allowed_after_a_publish_supersedes_it(self) -> None:
        widget = self._widget("u5-superseded")
        thread = self._approved(widget)
        declined = self._declined_edit(thread, {"content": "declined edit"})
        (legacy,) = self._legacy_pending(thread, ["legacy pending"])
        self.assertEqual(self._approve(thread.id, legacy["id"], overwrite=True).status_code, 200)
        self.assertEqual(self._detail(thread.id)["open_declined_revision"], None)
        rev = self._stage(thread.id, {"content": "new edit"})
        self.assertEqual(rev["status"], "pending")
        self.assertNotEqual(rev["id"], declined["id"])

    def test_a_legacy_pending_edit_can_still_be_updated(self) -> None:
        widget = self._widget("u5-legacy-update")
        thread = self._approved(widget)
        declined = self._declined_edit(thread, {"content": "declined v2"})
        (v3,) = self._legacy_pending(thread, ["legacy v3"])
        updated = self._stage(thread.id, {"content": "v3 updated"})
        self.assertEqual((updated["id"], updated["edit_count"]), (v3["id"], 1))
        # …and the declined v2 still blocks a NEW one once v3 is decided.
        self.assertEqual(self._reject(thread.id, v3["id"]).status_code, 200)
        self.assertEqual(self._close_revision(thread.id, v3["id"]).status_code, 204)
        refused = self._patch(thread.id, {"content": "another"})
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["detail"]["revision"]["id"], declined["id"])

    def test_open_declined_revision_is_set_for_the_author_only(self) -> None:
        widget = self._widget("u5-detail")
        thread = self._approved(widget)
        self.assertIsNone(self._detail(thread.id)["open_declined_revision"])
        declined = self._declined_edit(thread, {"content": "declined edit"})
        self.assertEqual(self._detail(thread.id)["open_declined_revision"]["id"], declined["id"])
        for email in (EDITOR, VIEWER):
            with self.subTest(email=email):
                self.assertIsNone(self._detail(thread.id, email)["open_declined_revision"])
        # A rework makes it "reworked", not open.
        rework = self._rework(thread.id, declined["id"], self._body(title="Live title", content="reworked"))
        self.assertEqual(rework.status_code, 200, rework.text)
        self.assertIsNone(self._detail(thread.id)["open_declined_revision"])


class DecisionStaleCheck(_ResubmitFixture):
    """§3.3 (edits) and §3.8 / U9 (pending new threads)."""

    def _approve_rev(self, thread_id: int, revision_id: int, body: dict | None):
        return self.client.post(
            f"/data/threads/{thread_id}/revisions/{revision_id}/approve",
            json=body, headers=self._auth(EDITOR),
        )

    def _reject_rev(self, thread_id: int, revision_id: int, body: dict):
        return self.client.post(
            f"/data/threads/{thread_id}/revisions/{revision_id}/reject",
            json=body, headers=self._auth(EDITOR),
        )

    def _updated_edit(self, link: str):
        widget = self._widget(link)
        thread = self._approved(widget)
        rev = self._stage(thread.id, {"content": "as first read"})
        self._stage(thread.id, {"content": "updated while reviewing"})
        return thread, rev

    def _assert_revision_409(self, resp, revision_id: int) -> None:
        self.assertEqual(resp.status_code, 409, resp.text)
        detail = resp.json()["detail"]
        self.assertEqual(
            (detail["code"], detail["message"]),
            ("revision_changed", "This edit changed while you were reviewing"),
        )
        self.assertEqual((detail["revision"]["id"], detail["revision"]["edit_count"]), (revision_id, 1))

    def test_stale_approve_and_reject_refused_with_nothing_written(self) -> None:
        thread, rev = self._updated_edit("stale-rev")
        self._assert_revision_409(self._approve_rev(thread.id, rev["id"], {"expected_edit_count": 0}), rev["id"])
        self._assert_revision_409(
            self._reject_rev(thread.id, rev["id"], {"reason": "no", "expected_edit_count": 0}), rev["id"]
        )
        self.db.expire_all()
        row = self.db.query(ThreadRevisionV2).filter(ThreadRevisionV2.id == rev["id"]).one()
        self.assertEqual((row.status, row.rejection_reason, row.reviewed_at), ("pending", None, None))
        live = self._live(thread.id)
        self.assertEqual((live.content, live.version), ("live body", 1))
        self.assertEqual(self._notifications_for(AUTHOR), [])

    def test_matching_token_decides(self) -> None:
        thread, rev = self._updated_edit("stale-rev-ok")
        resp = self._approve_rev(thread.id, rev["id"], {"expected_edit_count": 1})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._live(thread.id).content, "updated while reviewing")
        thread2, rev2 = self._updated_edit("stale-rev-ok-reject")
        resp = self._reject_rev(thread2.id, rev2["id"], {"reason": "no", "expected_edit_count": 1})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_omitted_token_skips_the_check(self) -> None:
        thread, rev = self._updated_edit("stale-rev-none")
        self.assertEqual(self._reject_rev(thread.id, rev["id"], {"reason": "no"}).status_code, 200)
        thread2, rev2 = self._updated_edit("stale-rev-none-approve")
        self.assertEqual(self._approve_rev(thread2.id, rev2["id"], None).status_code, 200)
        thread3, rev3 = self._updated_edit("stale-rev-null")
        self.assertEqual(
            self._approve_rev(thread3.id, rev3["id"], {"expected_edit_count": None}).status_code, 200
        )

    def test_stale_overwrite_approve_leaves_the_overwrite_targets_alone(self) -> None:
        widget = self._widget("stale-rev-overwrite")
        thread = self._approved(widget)
        v2, v3 = self._legacy_pending(thread, ["legacy v2", "legacy v3"])
        self._stage(thread.id, {"content": "v3 updated"})  # updates v3 (the newest)
        resp = self._approve_rev(thread.id, v3["id"], {"overwrite": True, "expected_edit_count": 0})
        self._assert_revision_409(resp, v3["id"])
        self.assertEqual([r.status for r in self._revisions(thread.id)], ["pending", "pending"])

    def test_already_decided_wins_over_a_stale_token(self) -> None:
        thread, rev = self._updated_edit("stale-rev-decided")
        self.assertEqual(self._reject_rev(thread.id, rev["id"], {"reason": "no"}).status_code, 200)
        resp = self._approve_rev(thread.id, rev["id"], {"expected_edit_count": 0})
        self.assertEqual(resp.json()["detail"]["code"], "already_decided")

    # -- pending new threads (U9) ---------------------------------------

    def _edited_pending_thread(self, link: str) -> ThreadV2:
        widget = self._widget(link)
        thread = self._pending(widget)
        resp = self._patch(thread.id, {"content": "edited while pending"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["thread"]["edit_count"], 1)
        return thread

    def _assert_thread_409(self, resp, thread_id: int) -> None:
        self.assertEqual(resp.status_code, 409, resp.text)
        detail = resp.json()["detail"]
        self.assertEqual(
            (detail["code"], detail["message"]),
            ("thread_changed", "This thread changed while you were reviewing"),
        )
        self.assertEqual((detail["thread"]["id"], detail["thread"]["edit_count"]), (thread_id, 1))

    def test_an_authors_in_place_edit_bumps_the_count_only_on_a_real_change(self) -> None:
        widget = self._widget("stale-thread-bump")
        thread = self._pending(widget)
        self._patch(thread.id, {"content": "one"})
        self._patch(thread.id, {"title": "Declined title", "content": "one", "mentions": []})  # unchanged
        self.assertEqual(self._live(thread.id).edit_count, 1)
        self._patch(thread.id, {"title": "Two"})
        self.assertEqual(self._live(thread.id).edit_count, 2)

    def test_stale_thread_approve_and_reject_refused_with_nothing_written(self) -> None:
        thread = self._edited_pending_thread("stale-thread")
        approve = self.client.post(
            f"/data/threads/{thread.id}/approve", json={"expected_edit_count": 0}, headers=self._auth(EDITOR)
        )
        self._assert_thread_409(approve, thread.id)
        self._assert_thread_409(self._reject_thread_with(thread.id, {"reason": "no", "expected_edit_count": 0}), thread.id)
        live = self._live(thread.id)
        self.assertEqual((live.status, live.reviewed_at, live.last_decided_at), ("pending", None, None))
        self.assertEqual(self._rounds(thread.id), [])
        self.assertEqual(self._all_revisions(thread.id), [])
        self.assertEqual(self._notifications_for(AUTHOR), [])

    def _reject_thread_with(self, thread_id: int, body: dict):
        return self.client.post(f"/data/threads/{thread_id}/reject", json=body, headers=self._auth(EDITOR))

    def test_matching_or_omitted_thread_token_decides(self) -> None:
        thread = self._edited_pending_thread("stale-thread-ok")
        resp = self.client.post(
            f"/data/threads/{thread.id}/approve", json={"expected_edit_count": 1}, headers=self._auth(EDITOR)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["edit_count"], 1)
        thread2 = self._edited_pending_thread("stale-thread-ok-reject")
        self.assertEqual(
            self._reject_thread_with(thread2.id, {"reason": "no", "expected_edit_count": 1}).status_code, 200
        )
        thread3 = self._edited_pending_thread("stale-thread-none")
        self.assertEqual(self._approve_thread(thread3.id).status_code, 200)  # no body at all
        thread4 = self._edited_pending_thread("stale-thread-none-reject")
        self.assertEqual(self._reject_thread_with(thread4.id, {"reason": "no"}).status_code, 200)

    def test_pending_queue_rows_carry_the_thread_token(self) -> None:
        thread = self._edited_pending_thread("stale-thread-queue")
        resp = self.client.get("/data/threads/moderation/pending", headers=self._auth(EDITOR))
        self.assertEqual(resp.status_code, 200, resp.text)
        (row,) = [r for r in resp.json()["items"] if r["id"] == thread.id]
        self.assertEqual(row["edit_count"], 1)


class PendingResubmissionEditGuard(_ResubmitFixture):
    """U10 — a pending RESUBMITTED thread can't be edited back to the text of
    the attempt it reworks."""

    ROUND0 = {"title": "Declined title", "content": "declined body", "mentions": []}

    def _resubmitted(self, link: str, content: str = "changed body") -> ThreadV2:
        widget = self._widget(link)
        thread = self._rejected(widget)
        resp = self._resubmit(thread.id, self._body(content=content))
        self.assertEqual(resp.status_code, 200, resp.text)
        return self._live(thread.id)

    def _assert_guarded(self, resp) -> None:
        self.assertEqual(resp.status_code, 409, resp.text)
        detail = resp.json()["detail"]
        self.assertEqual(
            (detail["code"], detail["message"]),
            (
                "unchanged_resubmission",
                "This is the text that was declined — change it before it goes back for review",
            ),
        )

    def test_edit_back_to_round_zero_is_refused_and_nothing_changes(self) -> None:
        thread = self._resubmitted("u10-round0")
        before = self._live(thread.id)
        snapshot = (before.title, before.content, before.edit_count, before.edited_at)
        self._assert_guarded(self._patch(thread.id, dict(self.ROUND0)))
        after = self._live(thread.id)
        self.assertEqual((after.title, after.content, after.edit_count, after.edited_at), snapshot)
        self.assertEqual(after.status, "pending")

    def test_through_round_one_only_the_reworked_round_counts(self) -> None:
        thread = self._resubmitted("u10-round1")
        self.assertEqual(self._reject_thread(thread.id, "again").status_code, 200)
        resp = self._resubmit(thread.id, self._body(content="third body"))
        self.assertEqual(resp.status_code, 200, resp.text)
        # Back to round 1's text (the one this attempt reworks) → refused.
        self._assert_guarded(self._patch(thread.id, {"content": "changed body"}))
        # Back to round 0's text → allowed (only the directly reworked round).
        ok = self._patch(thread.id, dict(self.ROUND0))
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(self._live(thread.id).content, "declined body")

    def test_only_a_full_match_is_refused(self) -> None:
        thread = self._resubmitted("u10-partial")
        # Title already equals round 0's; changing the content keeps it different.
        steps = (
            ("title only matches", {"title": "Declined title", "content": "other body"}),
            ("content only matches", {"title": "New title", "content": "declined body"}),
        )
        for label, body in steps:
            with self.subTest(label):
                self.assertEqual(self._patch(thread.id, body).status_code, 200)

    def test_a_mentions_only_difference_is_allowed(self) -> None:
        widget = self._widget("u10-mentions")
        self._mentionables(widget)
        thread = self._rejected(widget, content="hi @MentionedPerson")
        resp = self._resubmit(
            thread.id, self._body(content="hi @MentionedPerson", mentions=[{"email": MENTION_EMAIL}])
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        # Same title/content as round 0, mentions omitted → the current set,
        # which differs from round 0's (empty) → allowed.
        self.assertEqual(
            self._patch(thread.id, {"title": "Declined title", "content": "hi @MentionedPerson"}).status_code, 200
        )
        # Dropping the mention makes it a FULL match → refused.
        self._assert_guarded(
            self._patch(thread.id, {"title": "Declined title", "content": "hi @MentionedPerson", "mentions": []})
        )

    def test_an_omitted_field_compares_with_its_current_value(self) -> None:
        thread = self._resubmitted("u10-omitted")
        # Title omitted → the current "Declined title" → a full round-0 match.
        self._assert_guarded(self._patch(thread.id, {"content": "declined body"}))

    def test_a_never_rejected_pending_thread_has_no_guard(self) -> None:
        widget = self._widget("u10-never-rejected")
        thread = self._pending(widget)
        resp = self._patch(thread.id, {"content": "changed"})
        self.assertEqual(resp.status_code, 200, resp.text)
        back = self._patch(thread.id, dict(self.ROUND0))
        self.assertEqual(back.status_code, 200, back.text)


# ---------------------------------------------------------
# `_lock_thread` / `populate_existing` — the locked re-read must see what a
# concurrent transaction committed while this one waited for the lock.
# ---------------------------------------------------------


class LockedRereadRefreshes(_ResubmitFixture):
    """The services load a row unlocked (gates), then re-read it `FOR
    UPDATE`. Without `populate_existing()` the ORM returns the identity-
    mapped object with its PRE-lock values. Simulated here: load the row
    into the session, change it with raw SQL (what a concurrent commit
    looks like to this session's identity map), then decide."""

    def _bump_behind_the_orm(self, table: str, row_id: int, **values) -> None:
        sets = ", ".join(f"{k} = :{k}" for k in values)
        self.db.execute(text(f"UPDATE {table} SET {sets} WHERE id = :id"), {"id": row_id, **values})

    def _decide_thread(self, thread_id: int, **kwargs):
        return thread_service.decide_thread(
            self.db, thread_id, UserInfo(email=EDITOR, name="editor", roles=[]),
            access=self._access(EDITOR), **kwargs,
        )

    def test_thread_stale_check_sees_an_edit_committed_while_waiting(self) -> None:
        widget = self._widget("lock-refresh-u9")
        thread = self._pending(widget)
        self.db.expire_all()
        loaded = self.db.get(ThreadV2, thread.id)
        self.assertEqual(loaded.edit_count, 0)  # in the identity map, as the reviewer read it
        self._bump_behind_the_orm("threads", thread.id, edit_count=1, content="changed meanwhile")
        with self.assertRaises(HTTPException) as ctx:
            self._decide_thread(thread.id, approve=True, expected_edit_count=0)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["code"], "thread_changed")
        self.assertEqual(self._rounds(thread.id), [])

    def test_thread_decided_while_waiting_is_already_reviewed(self) -> None:
        widget = self._widget("lock-refresh-status")
        thread = self._pending(widget)
        self.db.expire_all()
        self.assertEqual(self.db.get(ThreadV2, thread.id).status, "pending")
        self._bump_behind_the_orm("threads", thread.id, status="approved")
        with self.assertRaises(HTTPException) as ctx:
            self._decide_thread(thread.id, approve=False, reason="no")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["message"], "This thread has already been reviewed")

    def test_revision_stale_check_sees_an_update_committed_while_waiting(self) -> None:
        widget = self._widget("lock-refresh-rev")
        thread = self._approved(widget)
        rev = self._stage(thread.id, {"content": "as first read"})
        self.db.expire_all()
        # Hold a strong reference: the identity map is weak, so an unreferenced
        # object would be dropped and the locked query would load it fresh.
        loaded = self.db.get(ThreadRevisionV2, rev["id"])
        self.assertEqual(loaded.edit_count, 0)
        self._bump_behind_the_orm("thread_revisions", rev["id"], edit_count=1, content="updated meanwhile")
        with self.assertRaises(HTTPException) as ctx:
            thread_service.decide_thread_revision(
                self.db, thread.id, rev["id"], UserInfo(email=EDITOR, name="editor", roles=[]),
                approve=True, expected_edit_count=0, access=self._access(EDITOR),
            )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["code"], "revision_changed")
        self.assertEqual(self._live(thread.id).content, "live body")



# ---------------------------------------------------------
# plan_my_revisions_2026-11-10.md §4 / §6 — the author's declined + pending
# items across every thread widget (`GET /threads/me/revisions` and its
# polled `/summary`).
#
# FIXTURE (reported before code): widget A on tab A (`_widget`), widget B on
# tab B (`gridstack_b`), the same five grants on each. AUTHOR's direct
# `component` grant on B is the one revoked/restored for "lost view". View As
# pairs / user go through the REAL `get_view_as_selection` with
# `view_as_service.resolve_view_as_selection` patched to return a verified
# selection; `anonymous` is unpatched.
# ---------------------------------------------------------

from app.db_v2.models.component import ComponentV2  # noqa: E402
from app.db_v2.models.resource_grant import ResourceGrantV2  # noqa: E402
from app.dependencies import CurrentHubUser, is_hub_admin  # noqa: E402
from app.services import view_as_service  # noqa: E402
from app.services.thread_service import THREAD_WIDGET_TYPE  # noqa: E402

TAB_ONLY = "tabonly@example.com"


class MyRevisions(_ResubmitFixture):
    ME = "/data/threads/me/revisions"
    SUMMARY = "/data/threads/me/revisions/summary"

    def setUp(self) -> None:
        super().setUp()
        self.a = self._widget("myrev-a")
        self.b = self._widget_on_b("myrev-b")
        self._t0 = thread_service._utc_now()

    # -- fixture ------------------------------------------------------

    def _widget_on_b(self, link: str) -> ComponentV2:
        component = ComponentV2(
            link=link, title=link, type=THREAD_WIDGET_TYPE, gridstack_id=self.gridstack_b.id
        )
        self.db.add(component)
        self.db.commit()
        self.db.refresh(component)
        for email, level in (
            (AUTHOR, "view"), (EDITOR, "edit"), (VIEWER, "view"), (EDITOR_AUTHOR, "view"), (EDITOR2, "edit"),
        ):
            self._grant(email=email, node=("component", component.id), level=level)
        return component

    def _revoke(self, email: str, component_id: int) -> None:
        user = self._seed_hub_user(email)
        self.db.query(ResourceGrantV2).filter(
            ResourceGrantV2.user_id == user.id, ResourceGrantV2.component_id == component_id
        ).delete()
        self.db.commit()

    def _stamp(self, model, row_id: int, seconds: int, column: str = "reviewed_at") -> None:
        """Pin a decision/close instant so cross-kind ordering is exact."""
        self.db.expire_all()
        row = self.db.get(model, row_id)
        setattr(row, column, self._t0 + timedelta(seconds=seconds))
        self.db.commit()

    def _declined_thread(self, widget, seconds: int, *, author: str = AUTHOR, title: str = "Declined") -> ThreadV2:
        thread = self._rejected(widget, author=author, title=title)
        self._stamp(ThreadV2, thread.id, seconds)
        return thread

    def _declined_edit_at(self, widget, seconds: int, *, title: str = "Edit") -> dict:
        thread = self._approved(widget, title=f"Live {title}")
        rev = self._declined_edit(thread, self._body(title=title, content=f"{title} body"))
        self._stamp(ThreadRevisionV2, rev["id"], seconds)
        return rev

    def _me(self, email: str = AUTHOR, params: dict | None = None) -> dict:
        resp = self.client.get(self.ME, params=params or {}, headers=self._auth(email))
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _summary(self, email: str = AUTHOR, params: dict | None = None, etag: str | None = None):
        headers = self._auth(email)
        if etag:
            headers["If-None-Match"] = etag
        return self.client.get(self.SUMMARY, params=params or {}, headers=headers)

    @staticmethod
    def _keys(items: list[dict]) -> list[tuple[str, int]]:
        return [(i["kind"], i["revision_id"] if i["kind"] == "edit" else i["thread_id"]) for i in items]

    def _is_admin_fn(self, email: str):
        current = CurrentHubUser(
            info=UserInfo(email=email, name=email, roles=[]),
            hub_user_id=self._seed_hub_user(email).id,
            email=email,
        )
        return lambda closures: is_hub_admin(self.db, current, closures=closures)

    # -- lists --------------------------------------------------------

    def test_items_from_two_widgets_both_kinds_newest_declined_first(self) -> None:
        t_a = self._declined_thread(self.a, 10, title="A thread")
        t_b = self._declined_thread(self.b, 20, title="B thread")
        e_a = self._declined_edit_at(self.a, 30, title="A edit")
        e_b = self._declined_edit_at(self.b, 40, title="B edit")
        self._declined_thread(self.a, 50, author=VIEWER, title="Someone else's")

        body = self._me()
        self.assertEqual(
            self._keys(body["items"]),
            [("edit", e_b["id"]), ("edit", e_a["id"]), ("thread", t_b.id), ("thread", t_a.id)],
        )
        self.assertFalse(body["truncated"])
        by_key = {k: i for k, i in zip(self._keys(body["items"]), body["items"])}
        a_item = by_key[("thread", t_a.id)]
        self.assertEqual(
            (a_item["component_id"], a_item["component_link"], a_item["widget_title"], a_item["location_label"]),
            (self.a.id, "myrev-a", "myrev-a", "Nav Threads › Root Threads"),
        )
        b_item = by_key[("edit", e_b["id"])]
        self.assertEqual(
            (b_item["component_id"], b_item["component_link"], b_item["location_label"]),
            (self.b.id, "myrev-b", "Nav B › Root B"),
        )
        self.assertEqual(b_item["thread_title"], "Live B edit")
        # The other author sees only their own.
        self.assertEqual([i["title"] for i in self._me(VIEWER)["items"]], ["Someone else's"])
        # A plain viewer / editor with nothing of their own: empty (non-vacuous — AUTHOR has 4).
        self.assertEqual(self._me(EDITOR)["items"], [])

    def test_open_closed_split_reworked_in_neither_superseded_in_closed(self) -> None:
        open_thread = self._declined_thread(self.a, 10, title="Open")
        closed_thread = self._declined_thread(self.b, 20, title="Closed by me")
        self.assertEqual(self._close(closed_thread.id).status_code, 204)
        reworked = self._declined_edit_at(self.a, 30, title="Reworked")
        rework = self._rework(reworked["thread_id"], reworked["id"], self._body(title="Reworked v2", content="new"))
        self.assertEqual(rework.status_code, 200, rework.text)
        superseded = self._declined_edit_at(self.b, 40, title="Superseded")
        self.db.expire_all()
        row = self.db.get(ThreadRevisionV2, superseded["id"])
        row.closed_at, row.closed_kind, row.closed_by_version = self._t0 + timedelta(seconds=50), "superseded", 9
        self.db.commit()
        self._stamp(ThreadV2, closed_thread.id, 45, column="closed_at")

        body = self._me()
        self.assertEqual(self._keys(body["items"]), [("thread", open_thread.id)])
        self.assertEqual(self._keys(body["closed"]), [("edit", superseded["id"]), ("thread", closed_thread.id)])
        self.assertEqual(body["closed"][0]["closed_kind"], "superseded")
        self.assertEqual(body["closed"][0]["closed_by_version"], 9)
        self.assertEqual(body["closed"][1]["closed_kind"], "author")
        # The reworked edit is in neither declined list; its rework is pending.
        every = self._keys(body["items"]) + self._keys(body["closed"])
        self.assertNotIn(("edit", reworked["id"]), every)
        self.assertIn(("edit", rework.json()["revision"]["id"]), self._keys(body["pending"]))

    def test_caps_and_truncated_across_widgets(self) -> None:
        for n, widget in enumerate((self.a, self.b, self.a)):
            self._declined_thread(widget, 10 + n, title=f"Open {n}")
        for n, widget in enumerate((self.a, self.b)):
            t = self._declined_thread(widget, 20 + n, title=f"Closed {n}")
            self._close(t.id)
        with mock.patch.object(thread_service, "DECLINED_ITEMS_CAP", 2), mock.patch.object(
            thread_service, "CLOSED_ITEMS_CAP", 1
        ):
            body = self._me()
        self.assertEqual([i["title"] for i in body["items"]], ["Open 2", "Open 1"])
        self.assertTrue(body["truncated"])
        self.assertEqual(len(body["closed"]), 1)
        self.assertTrue(body["closed_truncated"])

    def test_a_lost_widget_never_crowds_an_allowed_one_out_of_the_cap(self) -> None:
        """The plan's §4.2 step 5 concern (gating after a `cap + 1` fetch can
        leave fewer than `cap` items while more exist). Gating the author's
        distinct widgets BEFORE the fetch makes it exact: B's three newest
        items fill the whole window, yet A's older one is still listed."""
        older_a = self._declined_thread(self.a, 1, title="Older on A")
        for n in range(3):
            self._declined_thread(self.b, 10 + n, title=f"Newer on B {n}")
        self._revoke(AUTHOR, self.b.id)
        with mock.patch.object(thread_service, "DECLINED_ITEMS_CAP", 2):
            body = self._me()
        self.assertEqual(self._keys(body["items"]), [("thread", older_a.id)])
        self.assertFalse(body["truncated"])

    def test_lost_view_drops_the_widget_from_lists_and_counts_and_a_regrant_restores_it(self) -> None:
        t_a = self._declined_thread(self.a, 10, title="On A")
        t_b = self._declined_thread(self.b, 20, title="On B")
        closed_b = self._declined_thread(self.b, 30, title="Closed on B")
        self._close(closed_b.id)
        pending_b = self._pending(self.b, title="Pending on B")
        before = self._summary().json()
        self.assertEqual((before["open_count"], before["closed_count"], before["pending_count"]), (2, 1, 1))

        self._revoke(AUTHOR, self.b.id)
        body = self._me()
        self.assertEqual(self._keys(body["items"]), [("thread", t_a.id)])
        self.assertEqual(body["closed"], [])
        self.assertEqual(body["pending"], [])
        after = self._summary().json()
        self.assertEqual((after["open_count"], after["closed_count"], after["pending_count"]), (1, 0, 0))
        self.assertTrue(after["has_any"])

        self._grant(email=AUTHOR, node=("component", self.b.id), level="view")
        body = self._me()
        self.assertEqual(self._keys(body["items"]), [("thread", t_b.id), ("thread", t_a.id)])
        self.assertEqual(self._keys(body["pending"]), [("thread", pending_b.id)])
        self.assertEqual(self._summary().json()["open_count"], 2)

    def test_lost_view_on_every_widget_reads_as_no_items_but_has_any_false(self) -> None:
        self._declined_thread(self.b, 10)
        self._revoke(AUTHOR, self.b.id)
        self.assertEqual(self._me()["items"], [])
        s = self._summary().json()
        self.assertEqual((s["open_count"], s["has_any"]), (0, False))

    def test_gate_parity_with_check_view_access(self) -> None:
        """The `granted_single_node` shortcut must agree with
        `_check_view_access`'s `ViewerAccess.is_granted` for every persona,
        including an ancestor-only grant and a real Hub Admin."""
        self._grant(email=TAB_ONLY, node=("tab", self.root_tab_id), level="view")
        self._seed_real_admin(ADMIN)
        self._revoke(EDITOR_AUTHOR, self.b.id)
        ids = {self.a.id, self.b.id}
        for email in (AUTHOR, VIEWER, STRANGER, EDITOR_AUTHOR, TAB_ONLY, ADMIN, EDITOR):
            with self.subTest(email=email):
                user_id = self._seed_hub_user(email).id
                is_admin = email == ADMIN
                access = resolve_viewer_access(self.db, user_id, is_admin=is_admin)
                expected = {cid for cid in ids if access.is_granted(("component", cid))}
                got = thread_service._viewable_component_ids(
                    self.db, ids, hub_user_id=user_id, is_admin=self._is_admin_fn(email)
                )
                self.assertEqual(got, expected)
        # Non-vacuous: the personas really differ.
        self.assertEqual(
            thread_service._viewable_component_ids(
                self.db, ids, hub_user_id=self._seed_hub_user(TAB_ONLY).id, is_admin=self._is_admin_fn(TAB_ONLY)
            ),
            {self.a.id},
        )

    def test_hub_admin_author_skips_the_per_widget_check(self) -> None:
        self._seed_real_admin(ADMIN)
        self._declined_thread(self.a, 10, author=ADMIN)
        with mock.patch.object(thread_service, "granted_single_node") as gate:
            body = self._me(ADMIN)
        self.assertEqual(len(body["items"]), 1)
        gate.assert_not_called()

    # -- pending (M5) -------------------------------------------------

    def test_pending_lists_own_threads_and_edits_and_follows_decisions(self) -> None:
        own_thread = self._pending(self.a, title="My new thread")
        self._stamp(ThreadV2, own_thread.id, 10, column="submitted_at")
        live = self._approved(self.b, title="Live on B")
        staged = self._stage(live.id, self._body(title="Proposed", content="proposed body"))
        self._stamp(ThreadRevisionV2, staged["id"], 20, column="submitted_at")
        self._pending(self.a, author=VIEWER, title="Not mine")

        body = self._me()
        self.assertEqual(self._keys(body["pending"]), [("edit", staged["id"]), ("thread", own_thread.id)])
        edit = body["pending"][0]
        self.assertEqual((edit["version"], edit["thread_title"], edit["title"]), (2, "Live on B", "Proposed"))
        self.assertEqual((edit["component_link"], edit["edit_count"], edit["updated_at"]), ("myrev-b", 0, None))
        self.assertEqual(body["pending"][1]["component_link"], "myrev-a")
        self.assertEqual(self._summary().json()["pending_count"], 2)

        # An in-place update shows on the item (same id, edit_count 1, updated_at).
        self._stage(live.id, self._body(title="Proposed", content="proposed body, updated"))
        edit = self._me()["pending"][0]
        self.assertEqual((edit["revision_id"], edit["edit_count"]), (staged["id"], 1))
        self.assertIsNotNone(edit["updated_at"])

        # Approved → gone; a rejected edit moves to `items`.
        self.assertEqual(self._approve_thread(own_thread.id).status_code, 200)
        resp = self.client.post(
            f"/data/threads/{live.id}/revisions/{staged['id']}/reject",
            json={"reason": "no"}, headers=self._auth(EDITOR),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = self._me()
        self.assertEqual(body["pending"], [])
        self.assertEqual(self._keys(body["items"]), [("edit", staged["id"])])
        s = self._summary().json()
        self.assertEqual((s["open_count"], s["pending_count"]), (1, 0))

    def test_pending_cap(self) -> None:
        for n in range(3):
            self._pending(self.a, title=f"P{n}")
        with mock.patch.object(thread_service, "PENDING_ITEMS_CAP", 2):
            body = self._me()
        self.assertEqual(len(body["pending"]), 2)
        self.assertTrue(body["pending_truncated"])

    # -- View As (M7) -------------------------------------------------

    def test_view_as_pairs_user_and_anonymous_are_empty(self) -> None:
        self._declined_thread(self.a, 10)
        self._pending(self.a)
        self.assertEqual(len(self._me()["items"]), 1)  # non-vacuous
        self.assertTrue(self._summary().json()["has_any"])
        pairs = view_as_service.ViewAsSelection(targets=[])
        as_user = view_as_service.ViewAsSelection(
            targets=[], user=view_as_service.ViewAsUser(id=999, email=VIEWER, name="Viewer")
        )
        cases = (
            ("pairs", {"view_as": "role_x:scope_y"}, pairs),
            ("user", {"view_as_user": VIEWER}, as_user),
            ("anonymous", {"anonymous": "true"}, None),
        )
        for label, params, selection in cases:
            with self.subTest(label):
                patcher = mock.patch.object(view_as_service, "resolve_view_as_selection", return_value=selection)
                with patcher:
                    body = self._me(params=params)
                    resp = self._summary(params=params)
                self.assertEqual((body["items"], body["closed"], body["pending"]), ([], [], []))
                self.assertTrue(body["view_as"])
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(
                    resp.json(),
                    {"open_count": 0, "closed_count": 0, "pending_count": 0, "has_any": False, "view_as": True},
                )
                self.assertTrue(resp.headers["ETag"].endswith('-va"'))

    # -- summary ------------------------------------------------------

    def test_summary_counts_match_the_list_and_etag_304s_until_a_change(self) -> None:
        t1 = self._declined_thread(self.a, 10)
        self._declined_edit_at(self.b, 20)
        t3 = self._declined_thread(self.b, 30)
        self._close(t3.id)
        self._pending(self.a)
        body = self._me()
        resp = self._summary()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["Cache-Control"], "no-store")
        s = resp.json()
        self.assertEqual(
            (s["open_count"], s["closed_count"], s["pending_count"], s["has_any"], s["view_as"]),
            (len(body["items"]), len(body["closed"]), len(body["pending"]), True, False),
        )
        self.assertEqual((s["open_count"], s["closed_count"], s["pending_count"]), (2, 1, 1))
        etag = resp.headers["ETag"]
        again = self._summary(etag=etag)
        self.assertEqual(again.status_code, 304)
        self.assertEqual((again.headers["ETag"], again.headers["Cache-Control"]), (etag, "no-store"))
        self.assertEqual(self._close(t1.id).status_code, 204)
        changed = self._summary(etag=etag)
        self.assertEqual(changed.status_code, 200)
        self.assertNotEqual(changed.headers["ETag"], etag)
        self.assertEqual((changed.json()["open_count"], changed.json()["closed_count"]), (1, 2))

    def test_no_rbac_work_when_the_author_has_no_rows(self) -> None:
        with mock.patch.object(thread_service, "RbacClosures") as closures, mock.patch.object(
            thread_service, "granted_single_node"
        ) as gate:
            resp = self._summary(STRANGER)
            body = self._me(STRANGER)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["has_any"], False)
        self.assertEqual(resp.headers["ETag"], '"0-0-0-0-0"')
        self.assertEqual(body["items"], [])
        closures.assert_not_called()
        gate.assert_not_called()
        # Non-vacuous: with a row, the gate runs.
        self._declined_thread(self.a, 10)
        with mock.patch.object(thread_service, "granted_single_node", return_value=True) as gate:
            self.assertTrue(self._summary().json()["has_any"])
        gate.assert_called()

    # -- routes & parity ----------------------------------------------

    def test_me_routes_are_not_swallowed_by_the_thread_id_route(self) -> None:
        for path in (self.ME, self.SUMMARY):
            with self.subTest(path):
                resp = self.client.get(path, headers=self._auth(STRANGER))
                self.assertEqual(resp.status_code, 200, resp.text)

    def test_per_widget_inbox_carries_the_widget_fields_too(self) -> None:
        thread = self._declined_thread(self.b, 10)
        rev = self._declined_edit_at(self.b, 20)
        items = self._declined("myrev-b")["items"]
        self.assertEqual(self._keys(items), [("edit", rev["id"]), ("thread", thread.id)])
        for item in items:
            self.assertEqual(
                (item["component_id"], item["component_link"], item["widget_title"], item["location_label"]),
                (self.b.id, "myrev-b", "myrev-b", "Nav B › Root B"),
            )

    def test_thread_detail_carries_closed_at_and_kind(self) -> None:
        open_one = self._declined_thread(self.a, 10)
        closed_one = self._declined_thread(self.a, 20)
        self._close(closed_one.id)
        open_detail = self.client.get(f"/data/threads/{open_one.id}", headers=self._auth(AUTHOR)).json()
        closed_detail = self.client.get(f"/data/threads/{closed_one.id}", headers=self._auth(AUTHOR)).json()
        self.assertEqual((open_detail["closed_at"], open_detail["closed_kind"]), (None, None))
        self.assertIsNotNone(closed_detail["closed_at"])
        self.assertEqual(closed_detail["closed_kind"], "author")


if __name__ == "__main__":
    unittest.main()
