"""
Tests for Thread Review's backend (plan_thread_review_merge_2026-11-11.md
§5, session S1): B1 the pending `order` param, B2 the unified activity log
`GET /threads/moderation/activity` (+ facets), B3 the drawer's lifecycle
`GET /threads/{id}/timeline`.

Built on `_ResubmitFixture` (tests.test_thread_resubmission): every widget
gets AUTHOR (view — stages, lands pending), EDITOR and EDITOR2 (edit —
decide), VIEWER and EDITOR_AUTHOR (view). Widget B sits on tab B
(`gridstack_b`), with the same grants plus EDITOR3 (edit on B only).

THE RICH FIXTURE (`_rich`), all through the real routes:
  widget A
    T1  AUTHOR: declined ("first") → resubmitted → declined ("second") →
        resubmitted → approved by EDITOR2 (3 rounds; its v1 `original` is
        the SAME event as round 2 and must be dropped). Then edit v2
        declined ("zebra edit reason"), its rework v3 (Revision 1 of v2)
        approved, legacy pending v4 + v5 with v5 overwrite-approved (v4
        `overwritten`, same instant as v5).
    T2  EDITOR's own post (v1 kept: Published directly) + an editor edit v2.
    T3  G3: EDITOR_AUTHOR declined, then given edit on root tab A,
        resubmits → self-published (1 declined round + v1 kept).
    T4  AUTHOR: declined → resubmitted → declined → CLOSED by the author
        (the closed chip only on round 1).
  widget B
    T5  AUTHOR: declined ("b reason").
  → 14 activity rows.

`ActivityParity` is the load-bearing class: under every filter the old
History endpoints share with the union, the union's submission rows equal
`/threads/moderation/history`'s and its revision rows equal
`/threads/revision-moderation/history`'s minus exactly the deduplicated
originals.
"""

from __future__ import annotations

import base64
import json
import unittest
from datetime import timedelta
from unittest import mock

from fastapi import HTTPException

from app.db_v2.models.component import ComponentV2
from app.db_v2.models.thread import THREAD_WIDGET_TYPE, ThreadRevisionV2, ThreadSubmissionV2
from app.services import thread_service
from tests.test_thread_resubmission import (
    ADMIN,
    EDITOR2,
    EDITOR_AUTHOR,
    STRANGER,
    _ResubmitFixture,
)
from tests.test_thread_revisions import AUTHOR, BASE_TIME, EDITOR, VIEWER

EDITOR3 = "editor3@example.com"
GHOST = "ghost@example.com"
ACTIVITY = "/data/threads/moderation/activity"


class _ActivityFixture(_ResubmitFixture):
    # -- widgets --------------------------------------------------------

    def _widget_b(self, link: str) -> ComponentV2:
        widget = ComponentV2(link=link, title=link, type=THREAD_WIDGET_TYPE, gridstack_id=self.gridstack_b.id)
        self.db.add(widget)
        self.db.commit()
        self.db.refresh(widget)
        for email, level in (
            (AUTHOR, "view"), (EDITOR, "edit"), (VIEWER, "view"), (EDITOR_AUTHOR, "view"),
            (EDITOR2, "edit"), (EDITOR3, "edit"),
        ):
            self._grant(email=email, node=("component", widget.id), level=level)
        return widget

    # -- reads ------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None, *, email: str = EDITOR, headers=None):
        return self.client.get(path, params=params or {}, headers=headers or self._auth(email))

    def _activity(self, params: dict | None = None, *, email: str = EDITOR, headers=None) -> dict:
        resp = self._get(ACTIVITY, params, email=email, headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _walk(self, path: str, params: dict | None = None, *, email: str = EDITOR, headers=None) -> list[dict]:
        items: list[dict] = []
        cursor = None
        for _ in range(500):
            query = dict(params or {})
            if cursor:
                query["cursor"] = cursor
            resp = self._get(path, query, email=email, headers=headers)
            self.assertEqual(resp.status_code, 200, resp.text)
            body = resp.json()
            items.extend(body["items"])
            cursor = body["next_cursor"]
            if not cursor:
                return items
        self.fail("cursor never terminated")

    def _keys(self, params: dict | None = None, *, email: str = EDITOR, headers=None) -> list[str]:
        return [i["key"] for i in self._walk(ACTIVITY, params, email=email, headers=headers)]

    # -- the rich fixture ----------------------------------------------

    def _rich(self) -> dict:
        wa = self._widget("ra-a")
        wb = self._widget_b("ra-b")
        ids: dict = {"wa": wa, "wb": wb}

        t1 = self._pending(wa, title="T1 r0")
        self.assertEqual(self._reject_thread(t1.id, "first").status_code, 200)
        self.assertEqual(self._resubmit(t1.id, self._body(title="T1 r1")).status_code, 200)
        self.assertEqual(self._reject_thread(t1.id, "second").status_code, 200)
        self.assertEqual(self._resubmit(t1.id, self._body(title="T1 r2")).status_code, 200)
        self.assertEqual(self._approve_thread(t1.id, email=EDITOR2).status_code, 200)
        v2 = self._declined_edit(self._live(t1.id), {"title": "T1 v2", "content": "v2 body"},
                                 reason="zebra edit reason")
        resp = self._rework(t1.id, v2["id"], {"title": "T1 v3", "content": "v3 body", "mentions": []})
        self.assertEqual(resp.status_code, 200, resp.text)
        v3_id = [r.id for r in self._revisions(t1.id) if r.version == 3][0]
        self.assertEqual(self._approve(t1.id, v3_id).status_code, 200)
        legacy = self._legacy_pending(self._live(t1.id), ["v4 body", "v5 body"])
        self.assertEqual(self._approve(t1.id, legacy[1]["id"], overwrite=True).status_code, 200)
        ids["t1"] = t1.id
        ids["t1_rounds"] = [r.id for r in self._rounds(t1.id)]
        ids["t1_revs"] = {r.version: r.id for r in self._all_revisions(t1.id)}

        resp = self.client.post("/data/threads/component/ra-a", json=self._body(title="T2 own"),
                                headers=self._auth(EDITOR))
        self.assertEqual(resp.status_code, 201, resp.text)
        t2 = resp.json()["id"]
        resp = self._patch(t2, {"title": "T2 edited", "content": "editor edit"}, email=EDITOR)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["applied"])
        ids["t2"] = t2
        ids["t2_revs"] = {r.version: r.id for r in self._all_revisions(t2)}

        t3 = self._rejected(wa, author=EDITOR_AUTHOR, title="T3 g3", reason="t3 reason")
        self._grant(email=EDITOR_AUTHOR, node=("tab", self.root_tab_id), level="edit")
        resp = self._resubmit(t3.id, self._body(title="T3 published"), email=EDITOR_AUTHOR)
        self.assertEqual(resp.json()["status"], "approved", resp.text)
        ids["t3"] = t3.id
        ids["t3_rounds"] = [r.id for r in self._rounds(t3.id)]
        ids["t3_revs"] = {r.version: r.id for r in self._all_revisions(t3.id)}

        t4 = self._rejected(wa, title="T4 r0", reason="t4 first")
        self.assertEqual(self._resubmit(t4.id, self._body(title="T4 r1")).status_code, 200)
        self.assertEqual(self._reject_thread(t4.id, "t4 second").status_code, 200)
        self.assertEqual(self._close(t4.id).status_code, 204)
        ids["t4"] = t4.id
        ids["t4_rounds"] = [r.id for r in self._rounds(t4.id)]

        t5 = self._rejected(wb, title="T5 on b", reason="b reason")
        ids["t5"] = t5.id
        ids["t5_rounds"] = [r.id for r in self._rounds(t5.id)]
        return ids

    @staticmethod
    def _expected_keys(ids: dict) -> set[str]:
        revs = ids["t1_revs"]
        return {
            *(f"s{i}" for i in ids["t1_rounds"]),
            *(f"r{revs[v]}" for v in (2, 3, 4, 5)),
            *(f"r{i}" for i in ids["t2_revs"].values()),
            *(f"s{i}" for i in ids["t3_rounds"]),
            f"r{ids['t3_revs'][1]}",
            *(f"s{i}" for i in ids["t4_rounds"]),
            *(f"s{i}" for i in ids["t5_rounds"]),
        }


# ---------------------------------------------------------
# B2 — the rows themselves.
# ---------------------------------------------------------


class ActivityRows(_ActivityFixture):
    def test_rich_fixture_rows_kinds_outcomes_and_fields(self) -> None:
        ids = self._rich()
        items = self._walk(ACTIVITY)
        by_key = {i["key"]: i for i in items}
        self.assertEqual(len(items), len(by_key))
        self.assertEqual(set(by_key), self._expected_keys(ids))
        self.assertEqual(len(items), 14)

        r0, r1, r2 = (by_key[f"s{i}"] for i in ids["t1_rounds"])
        self.assertEqual(
            [(r["kind"], r["outcome"], r["round"], r["reason"], r["published_as_v1"]) for r in (r0, r1, r2)],
            [("new", "rejected", 0, "first", False), ("resubmission", "rejected", 1, "second", False),
             ("resubmission", "approved", 2, None, True)],
        )
        self.assertEqual((r2["title"], r2["decided_by_email"], r2["source"], r2["source_id"]),
                         ("T1 r2", EDITOR2, "submission", ids["t1_rounds"][2]))
        self.assertIsNone(r2["version"])

        revs = ids["t1_revs"]
        v2, v3, v4, v5 = (by_key[f"r{revs[v]}"] for v in (2, 3, 4, 5))
        self.assertEqual(
            [(r["kind"], r["outcome"], r["version"]) for r in (v2, v3, v4, v5)],
            [("edit", "rejected", 2), ("edit", "approved", 3), ("edit", "overwritten", 4), ("edit", "approved", 5)],
        )
        self.assertEqual(v2["reason"], "zebra edit reason")
        self.assertEqual(
            (v3["resubmission_number"], v3["resubmission_of_version"], v3["resubmission_root_version"]), (1, 2, 2)
        )
        self.assertEqual(v4["decided_at"], v5["decided_at"])  # overwritten shares the approval's instant
        self.assertIsNone(v2["round"])
        for row in (r0, v2, v5):
            self.assertEqual(
                (row["thread_id"], row["thread_status"], row["thread_version"], row["component_link"],
                 row["widget_title"]),
                (ids["t1"], "approved", 5, "ra-a", "ra-a"),
            )
        self.assertEqual(r0["thread_title"], v5["title"])  # the thread NOW

        t2v1, t2v2 = (by_key[f"r{ids['t2_revs'][v]}"] for v in (1, 2))
        self.assertEqual((t2v1["kind"], t2v1["outcome"], t2v1["decided_by_email"]),
                         ("published_directly", "published_directly", EDITOR))
        self.assertEqual((t2v2["kind"], t2v2["outcome"]), ("editor_edit", "published_directly"))

        g3_round = by_key[f"s{ids['t3_rounds'][0]}"]
        g3_v1 = by_key[f"r{ids['t3_revs'][1]}"]
        self.assertEqual((g3_round["kind"], g3_round["outcome"], g3_round["reason"]), ("new", "rejected", "t3 reason"))
        self.assertEqual((g3_v1["kind"], g3_v1["outcome"], g3_v1["title"], g3_v1["decided_by_email"]),
                         ("published_directly", "published_directly", "T3 published", EDITOR_AUTHOR))

        t5 = by_key[f"s{ids['t5_rounds'][0]}"]
        self.assertEqual((t5["component_link"], t5["widget_title"]), ("ra-b", "ra-b"))
        self.assertTrue(t5["location_label"])
        self.assertNotEqual(t5["location_label"], r0["location_label"])

    def test_dedupe_drops_only_the_original_of_a_thread_with_an_approved_round(self) -> None:
        ids = self._rich()
        keys = set(self._keys())
        self.assertNotIn(f"r{ids['t1_revs'][1]}", keys)  # same event as round 2
        self.assertIn(f"r{ids['t2_revs'][1]}", keys)     # editor's own post: no round at all
        self.assertIn(f"r{ids['t3_revs'][1]}", keys)     # G3: rounds, but none approved
        # Non-vacuous: the dropped v1 IS in the old revision History.
        old = [r["id"] for r in self._walk("/data/threads/revision-moderation/history")]
        self.assertIn(ids["t1_revs"][1], old)

    def test_can_open_in_hub(self) -> None:
        ids = self._rich()
        by_key = {i["key"]: i for i in self._walk(ACTIVITY)}
        revs = ids["t1_revs"]
        expected_true = {
            f"s{ids['t1_rounds'][2]}", f"r{revs[3]}", f"r{revs[5]}",
            *(f"r{i}" for i in ids["t2_revs"].values()), f"r{ids['t3_revs'][1]}",
        }
        self.assertEqual({k for k, v in by_key.items() if v["can_open_in_hub"]}, expected_true)

    def test_closed_chip_only_on_the_last_declined_round(self) -> None:
        ids = self._rich()
        by_key = {i["key"]: i for i in self._walk(ACTIVITY)}
        first, last = (by_key[f"s{i}"] for i in ids["t4_rounds"])
        self.assertEqual((last["closed_kind"], last["closed_at"] is not None), ("author", True))
        self.assertEqual((first["closed_kind"], first["closed_at"]), (None, None))
        self.assertEqual(
            {k for k, v in by_key.items() if v["closed_kind"]}, {f"s{ids['t4_rounds'][1]}"}
        )

    def test_superseded_edit_carries_its_own_close(self) -> None:
        widget = self._widget("ra-super")
        thread = self._approved(widget)
        declined = self._declined_edit(thread, {"title": "v2", "content": "v2 body"})
        staged = self._legacy_pending(self._live(thread.id), ["v3 body"])
        resp = self._approve(thread.id, staged[0]["id"], overwrite=True)
        self.assertEqual(resp.status_code, 200, resp.text)
        (row,) = [i for i in self._walk(ACTIVITY) if i["key"] == f"r{declined['id']}"]
        self.assertEqual((row["outcome"], row["closed_kind"], row["closed_by_version"]),
                         ("rejected", "superseded", 3))


# ---------------------------------------------------------
# B2 — parity with the two old History endpoints.
# ---------------------------------------------------------


class ActivityParity(_ActivityFixture):
    TM = "/data/threads/moderation/history"
    RM = "/data/threads/revision-moderation/history"

    def _sets(self, activity_params: dict, tm_params: dict | None, rm_params: dict | None):
        with mock.patch.object(thread_service, "THREADS_PAGE_SIZE", 3):
            rows = self._walk(ACTIVITY, activity_params)
            tm = self._walk(self.TM, tm_params) if tm_params is not None else []
            rm = self._walk(self.RM, rm_params) if rm_params is not None else []
        keys = [r["key"] for r in rows]
        self.assertEqual(len(keys), len(set(keys)), "duplicate across pages")
        s = {r["source_id"] for r in rows if r["source"] == "submission"}
        r = {r["source_id"] for r in rows if r["source"] == "revision"}
        return s, r, {i["decision"]["submission_id"] for i in tm}, {i["id"] for i in rm}

    def _deduped(self, ids: dict) -> set[int]:
        return {ids["t1_revs"][1]}

    def test_unfiltered_every_decision_has_a_row(self) -> None:
        ids = self._rich()
        s, r, tm, rm = self._sets({}, {}, {})
        self.assertEqual(s, tm)
        self.assertEqual(r, rm - self._deduped(ids))
        self.assertEqual(rm - r, self._deduped(ids))  # exactly the deduplicated originals
        self.assertEqual(len(tm) + len(rm) - 1, 14)

    def test_parity_under_every_shared_filter(self) -> None:
        ids = self._rich()
        dropped = self._deduped(ids)
        # A midpoint instant: after T1's rounds, before its edits.
        t1_r2 = self._rounds(ids["t1"])[2]
        mid = (thread_service._as_utc(t1_r2.reviewed_at) + timedelta(microseconds=1)).isoformat()
        # (activity params, Threads Management params, Revision Management params);
        # None = that source contributes nothing under the filter.
        cases = [
            ({"outcome": "approved"}, {"status": "approved"}, {"status": "approved", "origin": "author"}),
            ({"outcome": "rejected"}, {"status": "rejected"}, {"status": "rejected"}),
            ({"outcome": "overwritten"}, None, {"status": "overwritten"}),
            ({"outcome": ["approved", "published_directly"]}, {"status": "approved"}, {"status": "approved"}),
            ({"date_from": mid}, {"date_from": mid}, {"date_from": mid}),
            ({"date_to": mid}, {"date_to": mid}, {"date_to": mid}),
            ({"author": AUTHOR}, {"author": AUTHOR}, {"author": AUTHOR}),
            ({"author": EDITOR_AUTHOR.upper()}, {"author": EDITOR_AUTHOR}, {"author": EDITOR_AUTHOR}),
            ({"reviewer": EDITOR}, {"reviewer": EDITOR}, {"reviewer": EDITOR}),
            ({"reviewer": EDITOR2}, {"reviewer": EDITOR2}, {"reviewer": EDITOR2}),
            ({"q": "T1"}, {"q": "T1"}, {"q": "T1"}),
            ({"q": "ra-b"}, {"q": "ra-b"}, {"q": "ra-b"}),
            ({"q": "published"}, {"q": "published"}, {"q": "published"}),
            ({"kind": "new"}, {"type": "new"}, None),
            ({"kind": "resubmission"}, {"type": "resubmission"}, None),
            ({"kind": "edit"}, None, {"origin": "author"}),
            ({"kind": "editor_edit"}, None, {"origin": "editor"}),
            ({"kind": "published_directly"}, None, {"origin": "original"}),
        ]
        for activity_params, tm_params, rm_params in cases:
            with self.subTest(activity=activity_params):
                s, r, tm, rm = self._sets(activity_params, tm_params, rm_params)
                self.assertEqual(s, tm)
                self.assertEqual(r, rm - dropped)
                self.assertTrue(s or r, "vacuous case")

    def test_approved_outcome_is_not_published_directly(self) -> None:
        """`outcome=approved` on revisions is author edits only: the old
        `status=approved` also lists originals and editor edits, which the
        union reads as Published directly."""
        ids = self._rich()
        s, r, _, rm = self._sets({"outcome": "approved"}, None, {"status": "approved", "origin": "author"})
        self.assertEqual(r, rm)
        self.assertEqual(s, {ids["t1_rounds"][2]})


# ---------------------------------------------------------
# B2 — filters.
# ---------------------------------------------------------


class ActivityFilters(_ActivityFixture):
    def test_each_filter_alone_and_combined(self) -> None:
        ids = self._rich()
        t1r, t3r, t4r, t5r = ids["t1_rounds"], ids["t3_rounds"], ids["t4_rounds"], ids["t5_rounds"]
        revs, t2 = ids["t1_revs"], ids["t2_revs"]
        cases = (
            ({"kind": "new"}, {f"s{t1r[0]}", f"s{t3r[0]}", f"s{t4r[0]}", f"s{t5r[0]}"}),
            ({"kind": "resubmission"}, {f"s{t1r[1]}", f"s{t1r[2]}", f"s{t4r[1]}"}),
            ({"kind": "edit"}, {f"r{revs[v]}" for v in (2, 3, 4, 5)}),
            ({"kind": "editor_edit"}, {f"r{t2[2]}"}),
            ({"kind": "published_directly"}, {f"r{t2[1]}", f"r{ids['t3_revs'][1]}"}),
            ({"kind": ["editor_edit", "published_directly"]}, {f"r{t2[1]}", f"r{t2[2]}", f"r{ids['t3_revs'][1]}"}),
            ({"outcome": "overwritten"}, {f"r{revs[4]}"}),
            ({"outcome": "published_directly"}, {f"r{t2[1]}", f"r{t2[2]}", f"r{ids['t3_revs'][1]}"}),
            ({"outcome": "approved"}, {f"s{t1r[2]}", f"r{revs[3]}", f"r{revs[5]}"}),
            ({"widget": "ra-b"}, {f"s{t5r[0]}"}),
            ({"widget": ["ra-b", "ra-a"]}, self._expected_keys(ids)),
            ({"widget": "no-such-link"}, set()),
            ({"q": "zebra"}, {f"r{revs[2]}"}),                    # the reason only
            ({"q": "ZEBRA EDIT"}, {f"r{revs[2]}"}),
            ({"q": "t4 second"}, {f"s{t4r[1]}"}),
            ({"q": "T4 r0"}, {f"s{t4r[0]}"}),                     # decided title, not the current one
            ({"author": EDITOR}, {f"r{t2[1]}", f"r{t2[2]}"}),
            ({"reviewer": EDITOR_AUTHOR}, {f"r{ids['t3_revs'][1]}"}),
            ({"kind": "new", "outcome": "rejected", "widget": "ra-a"}, {f"s{t1r[0]}", f"s{t3r[0]}", f"s{t4r[0]}"}),
            ({"kind": "edit", "reviewer": EDITOR, "q": "zebra"}, {f"r{revs[2]}"}),
            ({"kind": "edit", "outcome": "published_directly"}, set()),
        )
        for params, expected in cases:
            with self.subTest(params=params):
                self.assertEqual(set(self._keys(params)), expected)

    def test_q_escapes_like_wildcards(self) -> None:
        widget = self._widget("ra-like")
        self._rejected(widget, title="plain", reason="cost 100% off")
        self.assertEqual(len(self._keys({"q": "100%"})), 1)
        self.assertEqual(self._keys({"q": "1_0"}), [])
        self.assertEqual(self._keys({"q": "%"}), self._keys({"q": "100%"}))

    def test_date_bounds(self) -> None:
        widget = self._widget("ra-dates")
        a = self._rejected(widget, title="a")
        (round_a,) = self._rounds(a.id)
        at = thread_service._as_utc(round_a.reviewed_at)
        self.assertEqual(self._keys({"date_from": at.isoformat()}), [f"s{round_a.id}"])        # inclusive
        self.assertEqual(self._keys({"date_to": at.isoformat()}), [])                           # exclusive
        self.assertEqual(self._keys({"date_to": (at + timedelta(seconds=1)).isoformat()}), [f"s{round_a.id}"])

    def test_unknown_values(self) -> None:
        for params in ({"kind": "bogus"}, {"outcome": "pending"}, {"group": "author"}, {"order": "sideways"},
                       {"q": "x" * 201}):
            with self.subTest(params=params):
                self.assertEqual(self._get(ACTIVITY, params).status_code, 422)
        access = self._access(EDITOR)
        for kwargs in ({"kinds": ["bogus"]}, {"outcomes": ["pending"]}, {"group": "author"}, {"order": "up"}):
            with self.subTest(service=kwargs), self.assertRaises(HTTPException) as ctx:
                thread_service.list_thread_activity(self.db, None, access=access, **kwargs)
            self.assertEqual(ctx.exception.status_code, 400)


# ---------------------------------------------------------
# B2 — paging and grouping.
# ---------------------------------------------------------


class ActivityPaging(_ActivityFixture):
    def _seed_many(self) -> list[tuple]:
        """24 declined rounds + 24 decided revisions (with 6 kept v1s),
        across two widgets, every decision on one of FOUR instants keyed on
        the row id — so a round and a revision with the same id ALSO share
        an instant (only `source_rank` separates them). Returns the expected
        newest-first order as `(decided_at, rank, id, key, thread, widget)`."""
        wa = self._widget("rp-a")
        wb = self._widget_b("rp-b")
        for i in range(12):
            widget = wa if i % 2 else wb
            thread = self._insert_thread(widget.id, status="rejected", author_email=AUTHOR, title=f"rej {i}")
            for rnd in range(2):
                self.db.add(ThreadSubmissionV2(
                    thread_id=thread.id, round=rnd, title=f"rej {i}.{rnd}", content="c", mentions=[],
                    status="rejected", reason=f"r{i}.{rnd}", submitted_by_email=AUTHOR, submitted_at=BASE_TIME,
                    reviewed_by_email=EDITOR, reviewed_at=BASE_TIME,
                ))
        for i in range(6):
            thread = self._approved(wa if i % 2 else wb, title=f"app {i}")
            for version in (2, 3, 4):
                self._insert_revision(thread, version=version, status="rejected", reviewed_at=BASE_TIME,
                                      reviewed_by_email=EDITOR)
        self.db.commit()
        for model in (ThreadSubmissionV2, ThreadRevisionV2):
            for row in self.db.query(model):
                if row.reviewed_at is not None:
                    row.reviewed_at = BASE_TIME + timedelta(minutes=row.id % 4)
        self.db.commit()
        self.db.expire_all()

        threads = {t.id: t.component_id for t in self.db.query(thread_service.ThreadV2)}
        rows = [
            (thread_service._as_utc(s.reviewed_at), 0, s.id, f"s{s.id}", s.thread_id, threads[s.thread_id])
            for s in self.db.query(ThreadSubmissionV2)
        ] + [
            (thread_service._as_utc(r.reviewed_at), 1, r.id, f"r{r.id}", r.thread_id, threads[r.thread_id])
            for r in self.db.query(ThreadRevisionV2)
        ]
        rows.sort(key=lambda r: r[:3], reverse=True)
        s_ids = {r[2] for r in rows if r[1] == 0}
        r_ids = {r[2] for r in rows if r[1] == 1}
        self.assertTrue(s_ids & r_ids, "no id collision — the tie case would be vacuous")
        self.assertGreaterEqual(len(rows), 45)
        return rows

    def test_flat_walks_have_no_duplicates_or_gaps(self) -> None:
        expected = self._seed_many()
        newest = [r[3] for r in expected]
        for page_size in (20, 7, 1):
            with self.subTest(page_size=page_size), mock.patch.object(thread_service, "THREADS_PAGE_SIZE", page_size):
                self.assertEqual(self._keys(), newest)
                self.assertEqual(self._keys({"order": "oldest"}), list(reversed(newest)))
        # The default page size gives 3+ pages.
        first = self._activity()
        self.assertEqual(len(first["items"]), 20)
        self.assertIsNotNone(first["next_cursor"])

    def test_grouped_walks_keep_groups_contiguous_with_group_size(self) -> None:
        expected = self._seed_many()
        for group, column in (("thread", 4), ("widget", 5)):
            for order in ("newest", "oldest"):
                params = {"group": group, "order": order}
                with self.subTest(params=params):
                    with mock.patch.object(thread_service, "THREADS_PAGE_SIZE", 1000):
                        whole = self._walk(ACTIVITY, params)
                    with mock.patch.object(thread_service, "THREADS_PAGE_SIZE", 7):
                        walked = self._walk(ACTIVITY, params)
                    self.assertEqual([i["key"] for i in walked], [i["key"] for i in whole])
                    self.assertEqual(len(walked), len(expected))
                    gid = "thread_id" if group == "thread" else "component_id"
                    runs = [walked[0][gid]]
                    for item in walked[1:]:
                        if item[gid] != runs[-1]:
                            runs.append(item[gid])
                    self.assertEqual(len(runs), len(set(runs)), "a group interleaved")
                    sizes = {}
                    for r in expected:
                        sizes[r[column]] = sizes.get(r[column], 0) + 1
                    for item in walked:
                        self.assertEqual(item["group_size"], sizes[item[gid]])

    def test_other_mode_or_order_cursor_is_400(self) -> None:
        self._seed_many()
        with mock.patch.object(thread_service, "THREADS_PAGE_SIZE", 2):
            flat = self._activity()["next_cursor"]
            thread = self._activity({"group": "thread"})["next_cursor"]
            widget = self._activity({"group": "widget"})["next_cursor"]
            oldest = self._activity({"order": "oldest"})["next_cursor"]
        bad = (
            {"group": "thread", "cursor": flat},
            {"cursor": thread},
            {"group": "widget", "cursor": thread},
            {"group": "thread", "cursor": widget},
            {"cursor": oldest},
            {"order": "oldest", "cursor": flat},
            {"cursor": "junk"},
            {"cursor": base64.urlsafe_b64encode(json.dumps(["flat", "newest", 1]).encode()).decode()},
        )
        for params in bad:
            with self.subTest(params=params):
                self.assertEqual(self._get(ACTIVITY, params).status_code, 400)
        self.assertEqual(self._get(ACTIVITY, {"cursor": flat}).status_code, 200)


# ---------------------------------------------------------
# B2 — who sees what, and the route order.
# ---------------------------------------------------------


class ActivityScope(_ActivityFixture):
    def test_non_moderator_sees_nothing(self) -> None:
        ids = self._rich()
        self.assertTrue(self._keys())  # non-vacuous: EDITOR sees rows
        for email in (VIEWER, STRANGER, AUTHOR):
            with self.subTest(email=email):
                body = self._activity(email=email)
                self.assertEqual((body["items"], body["next_cursor"]), ([], None))
                facets = self._get(f"{ACTIVITY}/facets", email=email).json()
                self.assertEqual(facets, {"authors": [], "reviewers": [], "widgets": []})
        del ids

    def test_another_widget_is_absent_and_hub_admin_sees_all(self) -> None:
        ids = self._rich()
        only_b = set(self._keys(email=EDITOR3))
        self.assertEqual(only_b, {f"s{ids['t5_rounds'][0]}"})
        self.assertEqual(set(self._keys(headers=self._member_auth(ADMIN))), self._expected_keys(ids))
        # A widget filter never widens the set.
        self.assertEqual(set(self._keys({"widget": "ra-a"}, email=EDITOR3)), set())

    def test_routes_are_not_swallowed_by_the_thread_id_route(self) -> None:
        self._rich()
        for path in (ACTIVITY, f"{ACTIVITY}/facets"):
            with self.subTest(path=path):
                resp = self._get(path)
                self.assertEqual(resp.status_code, 200, resp.text)
        from app.routers.threads import router

        paths = [getattr(r, "path", "") for r in router.routes]
        catch_all = paths.index("/data/threads/{thread_id}")
        for path in ("/data/threads/moderation/activity", "/data/threads/moderation/activity/facets",
                     "/data/threads/{thread_id}/timeline"):
            with self.subTest(order=path):
                self.assertLess(paths.index(path), catch_all)


class ActivityFacets(_ActivityFixture):
    def test_people_and_widgets_over_the_deduplicated_set(self) -> None:
        self._rich()
        # A legacy v1 whose publisher never decided anything else: deduped
        # away (the thread has an approved round), so GHOST is no reviewer.
        widget = self._widget("ra-ghost")
        thread = self._insert_thread(widget.id, status="approved", author_email=AUTHOR, title="ghost",
                                     reviewed_by_email=EDITOR, reviewed_at=BASE_TIME)
        self._insert_revision(thread, version=1, status="approved", origin="original",
                              reviewed_at=BASE_TIME, reviewed_by_email=GHOST)
        old = self._get("/data/threads/revision-moderation/history/facets").json()
        self.assertIn(GHOST, {p["email"] for p in old["reviewers"]})  # non-vacuous

        body = self._get(f"{ACTIVITY}/facets").json()
        self.assertEqual({p["email"] for p in body["authors"]}, {AUTHOR, EDITOR, EDITOR_AUTHOR})
        self.assertEqual({p["email"] for p in body["reviewers"]}, {EDITOR, EDITOR2, EDITOR_AUTHOR})
        self.assertEqual([w["component_link"] for w in body["widgets"]], ["ra-a", "ra-b", "ra-ghost"])
        self.assertTrue(all(w["widget_title"] and w["location_label"] for w in body["widgets"]))
        only_b = self._get(f"{ACTIVITY}/facets", email=EDITOR3).json()
        self.assertEqual([w["component_link"] for w in only_b["widgets"]], ["ra-b"])
        self.assertEqual({p["email"] for p in only_b["authors"]}, {AUTHOR})


# ---------------------------------------------------------
# B3 — the drawer's lifecycle.
# ---------------------------------------------------------


class Timeline(_ActivityFixture):
    def _timeline(self, thread_id: int, *, email: str = EDITOR, headers=None):
        return self.client.get(f"/data/threads/{thread_id}/timeline", headers=headers or self._auth(email))

    def test_order_dedupe_and_a_pending_edit_updated_in_place(self) -> None:
        ids = self._rich()
        t1 = ids["t1"]
        first = self._stage(t1, {"title": "T1 v6", "content": "v6 body"})
        resp = self._patch(t1, {"title": "T1 v6", "content": "v6 body, again"})
        self.assertEqual(resp.status_code, 200, resp.text)
        resp = self._timeline(t1)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        revs = ids["t1_revs"]
        self.assertEqual(
            [i["key"] for i in body["items"]],
            [*(f"s{i}" for i in ids["t1_rounds"]), *(f"r{revs[v]}" for v in (2, 3, 4, 5)), f"r{first['id']}"],
        )
        self.assertFalse(body["truncated"])
        pending = body["items"][-1]
        self.assertEqual((pending["outcome"], pending["kind"], pending["version"], pending["edit_count"]),
                         ("pending", "edit", 6, 1))
        self.assertIsNotNone(pending["updated_at"])
        self.assertIsNone(pending["decided_at"])
        self.assertTrue(all(i["edit_count"] is None for i in body["items"][:-1]))

    def test_pending_round_is_synthesized(self) -> None:
        widget = self._widget("tl-round")
        thread = self._rejected(widget, reason="r0 reason")
        self.assertEqual(self._resubmit(thread.id, self._body(title="Again")).status_code, 200)
        self.assertEqual(self._patch(thread.id, {"title": "Again", "content": "edited while pending"}).status_code, 200)
        items = self._timeline(thread.id).json()["items"]
        (round0,) = self._rounds(thread.id)
        self.assertEqual([i["key"] for i in items], [f"s{round0.id}", f"sp{thread.id}"])
        live = self._live(thread.id)
        pending = items[1]
        self.assertEqual(
            (pending["kind"], pending["outcome"], pending["round"], pending["title"], pending["source_id"],
             pending["edit_count"]),
            ("resubmission", "pending", 1, "Again", None, live.edit_count),
        )
        self.assertGreaterEqual(live.edit_count, 1)
        self.assertIsNotNone(pending["updated_at"])

    def test_new_pending_thread_has_only_its_pending_round(self) -> None:
        widget = self._widget("tl-new")
        thread = self._pending(widget)
        items = self._timeline(thread.id).json()["items"]
        self.assertEqual([(i["key"], i["kind"], i["edit_count"], i["updated_at"]) for i in items],
                         [(f"sp{thread.id}", "new", 0, None)])

    def test_g3_and_editor_post_keep_their_v1(self) -> None:
        ids = self._rich()
        self.assertEqual([i["key"] for i in self._timeline(ids["t3"]).json()["items"]],
                         [f"s{ids['t3_rounds'][0]}", f"r{ids['t3_revs'][1]}"])
        self.assertEqual([i["key"] for i in self._timeline(ids["t2"]).json()["items"]],
                         [f"r{ids['t2_revs'][1]}", f"r{ids['t2_revs'][2]}"])

    def test_cap_keeps_the_newest_and_flags_truncated(self) -> None:
        ids = self._rich()
        revs = ids["t1_revs"]
        with mock.patch.object(thread_service, "THREAD_TIMELINE_CAP", 3):
            body = self._timeline(ids["t1"]).json()
        self.assertTrue(body["truncated"])
        self.assertEqual([i["key"] for i in body["items"]], [f"r{revs[v]}" for v in (3, 4, 5)])

    def test_gate(self) -> None:
        ids = self._rich()
        t1 = ids["t1"]
        self.assertEqual(self._timeline(999999).status_code, 404)
        self.assertEqual(self._timeline(t1, email=STRANGER).status_code, 403)
        for email in (VIEWER, AUTHOR):  # granted view, not an editor — the author too
            with self.subTest(email=email):
                self.assertEqual(self._timeline(t1, email=email).status_code, 404)
        self.assertEqual(self._timeline(t1, email=EDITOR3).status_code, 403)  # B only, no view on A
        self.assertEqual(self._timeline(t1, email=EDITOR2).status_code, 200)
        self.assertEqual(self._timeline(t1, headers=self._member_auth(ADMIN)).status_code, 200)


# ---------------------------------------------------------
# B1 — one order for both pending queues.
# ---------------------------------------------------------


class PendingOrder(_ActivityFixture):
    THREADS = "/data/threads/moderation/pending"
    REVISIONS = "/data/threads/revision-moderation/pending"

    def _seed(self) -> tuple[list[int], list[int]]:
        widget = self._widget("po")
        threads = [self._pending(widget, title=f"p{i}").id for i in range(4)]
        revisions = []
        for i in range(4):
            approved = self._approved(widget, title=f"a{i}")
            revisions.append(self._stage(approved.id, {"title": f"a{i} edit", "content": "edit"})["id"])
        # Two of each share an instant: `id` breaks the tie.
        for model, row_ids in ((thread_service.ThreadV2, threads), (ThreadRevisionV2, revisions)):
            rows = {r.id: r for r in self.db.query(model).filter(model.id.in_(row_ids))}
            for offset, row_id in enumerate(row_ids):
                rows[row_id].submitted_at = BASE_TIME + timedelta(minutes=min(offset, 2))
        self.db.commit()
        return threads, revisions

    def test_both_orders_both_endpoints_no_duplicates_or_gaps(self) -> None:
        threads, revisions = self._seed()
        for path, ids in ((self.THREADS, threads), (self.REVISIONS, revisions)):
            for order, expected in (("oldest", ids), ("newest", list(reversed(ids)))):
                with self.subTest(path=path, order=order):
                    for size in (1, 3, 20):
                        with mock.patch.object(thread_service, "THREADS_PAGE_SIZE", size):
                            got = [i["id"] for i in self._walk(path, {"order": order})]
                        self.assertEqual(got, expected)

    def test_defaults_are_unchanged(self) -> None:
        threads, revisions = self._seed()
        self.assertEqual([i["id"] for i in self._walk(self.THREADS)], threads)
        self.assertEqual([i["id"] for i in self._walk(self.REVISIONS)], list(reversed(revisions)))

    def test_other_order_cursor_is_400_and_a_legacy_cursor_still_works(self) -> None:
        threads, revisions = self._seed()
        for path, default, other in ((self.THREADS, "oldest", "newest"), (self.REVISIONS, "newest", "oldest")):
            with self.subTest(path=path), mock.patch.object(thread_service, "THREADS_PAGE_SIZE", 1):
                cursor = self._get(path, {"order": other}).json()["next_cursor"]
                self.assertEqual(self._get(path, {"order": default, "cursor": cursor}).status_code, 400)
                self.assertEqual(self._get(path, {"cursor": cursor}).status_code, 400)
                self.assertEqual(self._get(path, {"order": other, "cursor": cursor}).status_code, 200)
                self.assertEqual(self._get(path, {"cursor": "junk"}).status_code, 400)
                self.assertEqual(self._get(path, {"order": "sideways"}).status_code, 422)
                # A pre-B1 `[at, id]` cursor: valid under the default only.
                first = self._get(path).json()["items"][0]
                row_model = thread_service.ThreadV2 if path == self.THREADS else ThreadRevisionV2
                self.db.expire_all()
                row = self.db.get(row_model, first["id"])
                legacy = thread_service._encode_cursor(row.submitted_at, row.id)
                resp = self._get(path, {"cursor": legacy})
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertNotEqual(resp.json()["items"][0]["id"], first["id"])
                self.assertEqual(self._get(path, {"order": other, "cursor": legacy}).status_code, 400)
        del threads, revisions


if __name__ == "__main__":
    unittest.main()
