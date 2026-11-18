"""POST /v2/locks/state — the lock-badge poll (lock_state_service.py).

The frontend fetched lock state once per list load and never again, so
badges showed stale holders (or no badge at all) until a hard refresh. This
endpoint returns only the lock block for the ids the client is showing; the
client polls it every 15 s and patches those fields in place.

What this file pins:
- every lock-node kind the frontend badges answers correctly (nav tab, root
  tab, sub-grid, SBN node), for both the holder ("self") and an onlooker;
- the answer is FRESH per request — a release, takeover or TTL expiry shows
  up on the very next poll (the whole point);
- parity with the list endpoints' own lock fields;
- the chrome gate: an id the caller cannot view is omitted, never reported.

Fixture: `test_lock_release_on_revoke.LockReleaseFixture` (hub -> nav 1 ->
root 1 / root 2), plus a sub-grid on root 1 and an SBN tree on root 1's
canvas. Locks are taken service-level (`acquire`), matching
test_admin_lock_takeover.py.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.db_v2.models.component import ComponentV2
from app.db_v2.models.gridstack import GridstackV2
from app.db_v2.models.tab import TabV2
from app.dependencies import HUB_ADMIN_ROLE_KEY
from app.schemas.tab import MAX_LOCK_STATE_IDS_PER_KIND
from app.services.access_visibility_service import GRIDSTACK_WIDGET_TYPE
from app.services.edit_lock_service import acquire, release
from app.services.super_blocknote_service import SBN_ROOT_TYPE
from tests.test_lock_release_on_revoke import LockReleaseFixture

ADMIN_EMAIL = "admin@renphil.org"


class LockStatePollFixture(LockReleaseFixture):
    def setUp(self) -> None:
        super().setUp()
        self.sub_grid = GridstackV2(
            document_id="sub-1", parent_id=self.root_gridstack.id, parent_tab_id=self.root_tab_id
        )
        self.db.add(self.sub_grid)
        self.db.flush()
        self.db.add(
            ComponentV2(
                link="rep-sub-1",
                type=GRIDSTACK_WIDGET_TYPE,
                gridstack_id=self.root_gridstack.id,
                current_grid_id=self.sub_grid.id,
            )
        )
        self.sbn_root = ComponentV2(
            link="sbn-root", type=SBN_ROOT_TYPE, title="SBN", gridstack_id=self.root_gridstack.id
        )
        self.db.add(self.sbn_root)
        self.db.flush()
        self.sbn_leaf = ComponentV2(
            link="sbn-leaf",
            type="block_note",
            title="Leaf",
            gridstack_id=self.root_gridstack.id,
            super_blocknote_id=self.sbn_root.id,
        )
        self.plain_widget = ComponentV2(
            link="plain-widget", type="text", title="Plain", gridstack_id=self.root_gridstack.id
        )
        self.db.add_all([self.sbn_leaf, self.plain_widget])
        self.db.commit()

        self.NAV = ("nav_tab", self.nav1.id)
        self.ROOT = ("tab", self.root_tab_id)
        self.SUB = ("gridstack", self.sub_grid.id)
        self.SBN_LEAF = ("component", self.sbn_leaf.id)

    def _poll(self, headers: dict, **body) -> dict:
        body = {
            "nav_tabs": body.get("nav_tabs", [self.nav1.document_id]),
            "tabs": body.get("tabs", [self.root_document_id, self.other_root_document_id, "sub-1"]),
            "sbn": body.get("sbn", ["sbn-root", "sbn-leaf"]),
        }
        resp = self.client.post("/v2/locks/state", json=body, headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["data"]

    def _admin_poll(self, **body) -> dict:
        return self._poll(self._admin_auth(), **body)


class TestEveryKindReportsItsState(LockStatePollFixture):
    def test_everything_free(self) -> None:
        data = self._admin_poll()
        self.assertEqual(data["nav_tabs"][self.nav1.document_id]["lock_state"], "free")
        for doc in (self.root_document_id, self.other_root_document_id, "sub-1"):
            self.assertEqual(data["tabs"][doc]["lock_state"], "free", doc)
            self.assertFalse(data["tabs"][doc]["locked"], doc)
        self.assertEqual(data["sbn"]["sbn-root"]["lock_state"], "free")
        self.assertEqual(data["sbn"]["sbn-leaf"]["lock_state"], "free")

    def test_a_root_lock_as_seen_by_an_onlooker(self) -> None:
        acquire(self.db, self.ROOT, self.editor.email)
        data = self._admin_poll()

        root = data["tabs"][self.root_document_id]
        self.assertEqual(root["lock_state"], "locked_here")
        self.assertEqual(root["lock_holder"], self.editor.email)
        self.assertTrue(root["locked"])
        self.assertEqual(root["locked_by"], self.editor.email)
        self.assertFalse(root["lock_is_stale"])
        self.assertIsNotNone(root["lock_expires_at"])

        # 2026-09-30: was "blocked_by_descendant" — a nav tab never reports
        # it now (structure-only nav-tab locks, plan_node_session_gaps §4).
        self.assertEqual(data["nav_tabs"][self.nav1.document_id]["lock_state"], "free")
        self.assertEqual(data["tabs"]["sub-1"]["lock_state"], "locked_by_ancestor")
        self.assertFalse(data["tabs"]["sub-1"]["locked"])  # own row untouched
        self.assertEqual(data["sbn"]["sbn-leaf"]["lock_state"], "locked_by_ancestor")
        self.assertEqual(data["tabs"][self.other_root_document_id]["lock_state"], "free")

    def test_the_holder_sees_self_everywhere_in_the_chain_up_to_the_root(self) -> None:
        """Renamed 2026-09-30 (was `…everywhere_in_the_chain`): the chain
        stops at the root, so the nav tab reads free, not self."""
        acquire(self.db, self.SUB, ADMIN_EMAIL)
        data = self._admin_poll()
        self.assertEqual(data["tabs"]["sub-1"]["lock_state"], "self")
        self.assertEqual(data["tabs"][self.root_document_id]["lock_state"], "self")
        self.assertEqual(data["nav_tabs"][self.nav1.document_id]["lock_state"], "free")
        self.assertEqual(data["tabs"][self.other_root_document_id]["lock_state"], "free")

    def test_an_sbn_leaf_lock(self) -> None:
        acquire(self.db, self.SBN_LEAF, self.editor.email)
        data = self._admin_poll()
        self.assertEqual(data["sbn"]["sbn-leaf"]["lock_state"], "locked_here")
        self.assertEqual(data["sbn"]["sbn-root"]["lock_state"], "blocked_by_descendant")
        self.assertEqual(data["sbn"]["sbn-root"]["lock_holder_node_label"], "Leaf")
        self.assertEqual(data["tabs"][self.root_document_id]["lock_state"], "blocked_by_descendant")

    def test_takeover_blocked_flag_is_carried(self) -> None:
        role, scope = self._role_and_scope(HUB_ADMIN_ROLE_KEY)
        self._assign(self.editor, role, scope)
        acquire(self.db, self.ROOT, self.editor.email)
        data = self._admin_poll()
        self.assertTrue(data["tabs"][self.root_document_id]["lock_takeover_blocked"])
        # 2026-09-30: was True — a nav tab offers no takeover of sessions
        # inside it any more (decision 9), so it never carries the flag.
        self.assertFalse(data["nav_tabs"][self.nav1.document_id]["lock_takeover_blocked"])
        self.assertFalse(data["tabs"][self.other_root_document_id]["lock_takeover_blocked"])


class TestFreshness(LockStatePollFixture):
    """The bug this endpoint exists for: state changes after the client's
    list load must show on the NEXT poll, without reloading anything."""

    def test_a_release_shows_on_the_next_poll(self) -> None:
        acquire(self.db, self.ROOT, self.editor.email)
        self.assertEqual(self._admin_poll()["tabs"][self.root_document_id]["lock_state"], "locked_here")
        release(self.db, self.ROOT, self.editor.email)
        self.assertEqual(self._admin_poll()["tabs"][self.root_document_id]["lock_state"], "free")

    def test_a_lock_taken_after_load_shows_on_the_next_poll(self) -> None:
        """Rewritten 2026-09-30: the nav tab no longer reflects a root lock
        beneath it (it used to turn `blocked_by_descendant`), so the nav
        tab's own freshness is pinned by locking the nav tab itself."""
        data = self._admin_poll()
        self.assertEqual(data["nav_tabs"][self.nav1.document_id]["lock_state"], "free")
        self.assertEqual(data["tabs"][self.other_root_document_id]["lock_state"], "free")
        acquire(self.db, ("tab", self.other_root_tab_id), self.other_editor.email)
        acquire(self.db, self.NAV, self.editor.email)
        data = self._admin_poll()
        self.assertEqual(data["tabs"][self.other_root_document_id]["lock_state"], "locked_here")
        self.assertEqual(data["nav_tabs"][self.nav1.document_id]["lock_state"], "locked_here")
        self.assertEqual(data["nav_tabs"][self.nav1.document_id]["lock_holder"], self.editor.email)

    def test_a_takeover_shows_the_new_holder(self) -> None:
        acquire(self.db, self.ROOT, self.editor.email)
        acquire(self.db, self.ROOT, self.other_editor.email, force=True)
        root = self._admin_poll()["tabs"][self.root_document_id]
        self.assertEqual(root["lock_state"], "locked_here")
        self.assertEqual(root["lock_holder"], self.other_editor.email)

    def test_an_expired_lock_reads_free_but_keeps_its_raw_row(self) -> None:
        acquire(self.db, self.ROOT, self.editor.email)
        tab = self.db.query(TabV2).filter(TabV2.id == self.root_tab_id).first()
        tab.locked_at = datetime.now(timezone.utc) - timedelta(days=1)
        self.db.commit()
        root = self._admin_poll()["tabs"][self.root_document_id]
        self.assertEqual(root["lock_state"], "free")
        self.assertTrue(root["locked"])
        self.assertTrue(root["lock_is_stale"])


class TestParityWithListEndpoints(LockStatePollFixture):
    FIELDS = (
        "locked",
        "locked_by",
        "locked_at",
        "lock_is_stale",
        "lock_state",
        "lock_holder",
        "lock_holder_node_label",
        "lock_expires_at",
        "lock_takeover_blocked",
    )

    def test_root_and_nav_tab_rows_match_the_lists(self) -> None:
        acquire(self.db, self.ROOT, self.editor.email)
        data = self._admin_poll()

        roots = self.client.get(f"/v2/nav-tabs/{self.nav1.document_id}/tabs", headers=self._admin_auth())
        self.assertEqual(roots.status_code, 200, roots.text)
        for row in roots.json()["data"]:
            polled = data["tabs"][row["documentId"]]
            self.assertEqual({k: row[k] for k in self.FIELDS}, polled, row["documentId"])

        navs = self.client.get("/v2/nav-tabs", headers=self._admin_auth())
        nav_row = next(n for n in navs.json()["data"] if n["documentId"] == self.nav1.document_id)
        self.assertEqual({k: nav_row[k] for k in self.FIELDS}, data["nav_tabs"][self.nav1.document_id])

    def test_sub_grid_and_sbn_rows_match_the_lists(self) -> None:
        acquire(self.db, self.SBN_LEAF, self.editor.email)
        data = self._admin_poll()

        children = self.client.get(f"/v2/tabs/{self.root_document_id}/children", headers=self._admin_auth())
        self.assertEqual(children.status_code, 200, children.text)
        sub_row = next(r for r in children.json()["data"] if r["documentId"] == "sub-1")
        self.assertEqual({k: sub_row[k] for k in self.FIELDS}, data["tabs"]["sub-1"])

        sbn_children = self.client.get("/v2/sbn/sbn-root/children", headers=self._admin_auth())
        self.assertEqual(sbn_children.status_code, 200, sbn_children.text)
        leaf_row = sbn_children.json()["data"][0]
        self.assertEqual({k: leaf_row[k] for k in self.FIELDS}, data["sbn"]["sbn-leaf"])


class TestVisibilityGate(LockStatePollFixture):
    def test_a_caller_with_no_grants_learns_nothing(self) -> None:
        acquire(self.db, self.ROOT, self.other_editor.email)
        data = self._poll(self._auth(self.editor.email))
        self.assertEqual(data, {"nav_tabs": {}, "tabs": {}, "sbn": {}})

    def test_only_viewable_nodes_are_reported(self) -> None:
        self._grant(node=self.ROOT, level="view", user=self.editor)
        acquire(self.db, ("tab", self.other_root_tab_id), self.other_editor.email)
        data = self._poll(self._auth(self.editor.email))
        self.assertIn(self.root_document_id, data["tabs"])
        # The locked sibling is not viewable — its holder must not leak.
        self.assertNotIn(self.other_root_document_id, data["tabs"])

    def test_unknown_ids_and_non_sbn_links_are_omitted(self) -> None:
        data = self._admin_poll(
            nav_tabs=["no-such-nav"],
            tabs=["no-such-tab", "sbn-root"],  # an SBN link is not a tab id
            sbn=["plain-widget", "no-such-link", "  ", self.root_document_id],
        )
        self.assertEqual(data, {"nav_tabs": {}, "tabs": {}, "sbn": {}})

    def test_duplicate_and_padded_ids_resolve_once(self) -> None:
        data = self._admin_poll(
            nav_tabs=[], tabs=[self.root_document_id, f"  {self.root_document_id} "], sbn=[]
        )
        self.assertEqual(list(data["tabs"]), [self.root_document_id])


class TestRequestValidation(LockStatePollFixture):
    def test_unauthenticated_is_refused(self) -> None:
        resp = self.client.post("/v2/locks/state", json={})
        self.assertIn(resp.status_code, (401, 403))

    def test_empty_body_is_an_empty_answer(self) -> None:
        resp = self.client.post("/v2/locks/state", json={}, headers=self._admin_auth())
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["data"], {"nav_tabs": {}, "tabs": {}, "sbn": {}})

    def test_unknown_field_is_rejected(self) -> None:
        resp = self.client.post("/v2/locks/state", json={"components": []}, headers=self._admin_auth())
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_over_the_cap_is_rejected(self) -> None:
        ids = [f"id-{i}" for i in range(MAX_LOCK_STATE_IDS_PER_KIND + 1)]
        resp = self.client.post("/v2/locks/state", json={"tabs": ids}, headers=self._admin_auth())
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_non_string_ids_are_rejected(self) -> None:
        resp = self.client.post("/v2/locks/state", json={"tabs": [1]}, headers=self._admin_auth())
        self.assertEqual(resp.status_code, 422, resp.text)


if __name__ == "__main__":
    unittest.main()
