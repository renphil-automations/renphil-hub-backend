"""Ancestor takeover + admin-held sessions — owner decisions 2026-09-26.

1. An admin may take over from ANY ancestor: a forced acquire on root 1
   ends userX's session on a widget under it in one step (`acquire`'s
   decision-8 subtree force). REVISED 2026-09-30
   (plan_node_session_gaps_2026-09-28.md §4, option 2): a nav tab is no
   longer anyone's lock ancestor — forcing a nav tab ends only the nav
   tab's own session, and a nav-tab lock never blocks a root beneath it.
   The tests that used nav 1 as "the ancestor" now use root 1 over a
   widget, so the admin-protection rules below are still pinned one level
   down; the nav-tab cases assert the new self-only behaviour.
2. An admin's own session is NEVER taken over — not on the same node, not
   from an ancestor, not by force-unlock. All-or-nothing: if any session in
   the forced subtree is an admin's, the whole takeover is refused
   (`NODE_LOCKED_BY_ADMIN`).
3. "Admin" for a lock HOLDER is by assignment only
   (`rbac_graph_service.hub_admin_user_ids`). A JWT-only admin's session is
   NOT protected — `TestJwtOnlyAdminGap` pins that on purpose.

Fixture: `test_lock_release_on_revoke.LockReleaseFixture` (hub -> nav 1 ->
root 1 / root 2). Holders' locks are taken service-level (`acquire`), the
takeover itself goes through the real HTTP doors as a JWT Hub Admin.
"""

from __future__ import annotations

import unittest

from app.db_v2.models.component import ComponentV2
from app.dependencies import HUB_ADMIN_ROLE_KEY
from app.services.edit_lock_service import (
    LockConflictError,
    acquire,
    release,
    resolve_lock_view,
)
from tests.test_lock_release_on_revoke import LockReleaseFixture

ADMIN_EMAIL = "admin@renphil.org"


class AdminTakeoverFixture(LockReleaseFixture):
    def setUp(self) -> None:
        super().setUp()
        self.NAV = ("nav_tab", self.nav1.id)
        self.ROOT = ("tab", self.root_tab_id)
        self.OTHER_ROOT = ("tab", self.other_root_tab_id)
        self.widget = ComponentV2(
            link="widget-1", title="Widget", type="text", gridstack_id=self.root_gridstack.id
        )
        self.db.add(self.widget)
        self.db.commit()
        self.WIDGET = ("component", self.widget.id)

    def _make_assignment_admin(self, user) -> None:
        role, scope = self._role_and_scope(HUB_ADMIN_ROLE_KEY)
        self._assign(user, role, scope)

    def _holder(self, node) -> tuple[bool, str]:
        self.db.expire_all()
        from app.services.edit_lock_service import _locked_quad, _node_row

        locked, locked_by, _at, _token = _locked_quad(_node_row(self.db, node))
        return bool(locked), locked_by or ""

    def _force_nav(self):
        return self.client.put(
            f"/v2/nav-tabs/{self.nav1.document_id}/lock", json={"force": True}, headers=self._admin_auth()
        )

    def _force_root(self):
        return self.client.put(
            f"/v2/tabs/{self.root_document_id}/lock", json={"force": True}, headers=self._admin_auth()
        )

    def _second_widget(self):
        widget2 = ComponentV2(
            link="widget-2", title="Widget 2", type="text", gridstack_id=self.root_gridstack.id
        )
        self.db.add(widget2)
        self.db.commit()
        return ("component", widget2.id)


class TestAncestorTakeover(AdminTakeoverFixture):
    def test_forcing_the_nav_tab_no_longer_ends_a_users_session_on_a_root_beneath_it(self) -> None:
        """FLIPPED 2026-09-30 (was `…ends_a_users_session_on_a_root_beneath_it`):
        a nav-tab force ends only the nav tab's own session (plan
        node_session_gaps §4, decision 9). The root session survives; the
        admin takes it over from the root's own badge instead."""
        acquire(self.db, self.ROOT, self.editor.email)
        resp = self._force_nav()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._holder(self.NAV), (True, ADMIN_EMAIL))
        self.assertEqual(self._holder(self.ROOT), (True, self.editor.email))

    def test_forcing_the_root_ends_a_users_session_on_a_widget_on_its_canvas(self) -> None:
        acquire(self.db, self.WIDGET, self.editor.email)
        resp = self._force_root()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(self._holder(self.WIDGET)[0])

    def test_a_parents_lock_still_cannot_be_broken_from_a_child(self) -> None:
        """Owner decision: ancestors only. Unchanged rule, pinned — one
        level down since 2026-09-30 (the nav tab is no longer a parent in
        the lock tree): a root held by the editor cannot be broken by
        forcing a widget on its canvas."""
        acquire(self.db, self.ROOT, self.editor.email)
        with self.assertRaises(LockConflictError):
            acquire(self.db, self.WIDGET, ADMIN_EMAIL, force=True)
        self.assertEqual(self._holder(self.ROOT), (True, self.editor.email))

    def test_a_nav_tab_lock_is_not_a_parent_forcing_a_root_under_it_leaves_it_alone(self) -> None:
        """New 2026-09-30, the old parent/child test's nav-tab half: the
        editor's nav-tab session neither blocks the root nor is ended by a
        force on it."""
        acquire(self.db, self.NAV, self.editor.email)
        resp = self._force_root()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._holder(self.ROOT), (True, ADMIN_EMAIL))
        self.assertEqual(self._holder(self.NAV), (True, self.editor.email))


class TestAdminSessionsAreNeverTakenOver(AdminTakeoverFixture):
    """Moved one level down 2026-09-30 (the root is now the highest
    ancestor a force can reach through): every "from an ancestor" case
    forces root 1 over a widget instead of nav 1 over root 1."""

    def setUp(self) -> None:
        super().setUp()
        self._make_assignment_admin(self.editor)  # `editor` is now an admin by assignment

    def test_ancestor_takeover_over_an_admin_is_refused_and_their_lock_survives(self) -> None:
        acquire(self.db, self.WIDGET, self.editor.email)
        resp = self._force_root()
        self.assertEqual(resp.status_code, 409, resp.text)
        detail = resp.json()["detail"]
        self.assertEqual(detail["code"], "NODE_LOCKED_BY_ADMIN")
        self.assertEqual(
            [(b["holder"], b["relation"], b["is_admin"]) for b in detail["blocking"]],
            [(self.editor.email, "descendant", True)],
        )
        self.assertEqual(self._holder(self.WIDGET), (True, self.editor.email))
        self.assertFalse(self._holder(self.ROOT)[0])

    def test_same_node_takeover_over_an_admin_is_refused(self) -> None:
        acquire(self.db, self.ROOT, self.editor.email)
        resp = self._force_root()
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "NODE_LOCKED_BY_ADMIN")
        self.assertEqual(self._holder(self.ROOT), (True, self.editor.email))

    def test_one_admin_below_blocks_the_whole_takeover_nobody_is_ousted(self) -> None:
        """All-or-nothing: `other_editor` (not an admin) on the widget and
        `editor` (admin) on a second widget — both on root 1's canvas."""
        widget2 = self._second_widget()
        acquire(self.db, self.WIDGET, self.other_editor.email)
        acquire(self.db, widget2, self.editor.email)
        resp = self._force_root()
        self.assertEqual(resp.status_code, 409, resp.text)
        blocking = {b["holder"]: b["is_admin"] for b in resp.json()["detail"]["blocking"]}
        self.assertEqual(blocking, {self.other_editor.email: False, self.editor.email: True})
        self.assertEqual(self._holder(self.WIDGET), (True, self.other_editor.email))
        self.assertEqual(self._holder(widget2), (True, self.editor.email))

    def test_a_plain_refused_acquire_already_flags_the_admin(self) -> None:
        """The client reads `is_admin` off the plain (non-force) 409 to skip
        the "Take over anyway?" confirm entirely."""
        acquire(self.db, self.WIDGET, self.editor.email)
        resp = self.client.put(f"/v2/tabs/{self.root_document_id}/lock", json={}, headers=self._admin_auth())
        self.assertEqual(resp.status_code, 409, resp.text)
        detail = resp.json()["detail"]
        self.assertEqual(detail["code"], "NODE_LOCKED")
        self.assertTrue(detail["blocking"][0]["is_admin"])

    def test_force_unlock_of_an_admins_fresh_lock_is_refused(self) -> None:
        acquire(self.db, self.ROOT, self.editor.email)
        resp = self.client.put(
            f"/v2/tabs/{self.root_document_id}/unlock", json={"force": True}, headers=self._admin_auth()
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "NODE_LOCKED_BY_ADMIN")
        self.assertEqual(self._holder(self.ROOT), (True, self.editor.email))

    def test_an_admin_is_never_blocked_by_their_own_session(self) -> None:
        acquire(self.db, self.WIDGET, self.editor.email)
        acquire(self.db, self.ROOT, self.editor.email, force=True)
        self.assertEqual(self._holder(self.ROOT), (True, self.editor.email))

    def test_a_stale_admin_lock_is_still_reclaimable(self) -> None:
        """A stale session is nobody's session — the admin rule protects
        LIVE work only."""
        acquire(self.db, self.ROOT, self.editor.email)
        from datetime import datetime, timedelta, timezone

        from app.db_v2.models.tab import TabV2

        tab = self.db.query(TabV2).filter(TabV2.id == self.root_tab_id).first()
        tab.locked_at = datetime.now(timezone.utc) - timedelta(days=1)
        self.db.commit()
        resp = self._force_root()
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_service_level_refusal_carries_the_code(self) -> None:
        acquire(self.db, self.WIDGET, self.editor.email)
        with self.assertRaises(LockConflictError) as ctx:
            acquire(self.db, self.ROOT, ADMIN_EMAIL, force=True)
        self.assertEqual(ctx.exception.code, "NODE_LOCKED_BY_ADMIN")
        with self.assertRaises(LockConflictError):
            release(self.db, self.WIDGET, ADMIN_EMAIL, force=True)

    def test_forcing_the_nav_tab_never_touches_an_admins_session_inside(self) -> None:
        """New 2026-09-30: with the nav tab structure-only, a nav-tab force
        does not even SEE the admin's root session — it succeeds and the
        admin's session is untouched (never taken over, never refused)."""
        acquire(self.db, self.ROOT, self.editor.email)
        resp = self._force_nav()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._holder(self.ROOT), (True, self.editor.email))


class TestLockViewTakeoverBlocked(AdminTakeoverFixture):
    """The read-side flag the badge uses to hide the takeover icon."""

    def test_a_users_session_below_leaves_takeover_available(self) -> None:
        """Rewritten 2026-09-30: the "below" case is a widget under root 1
        (was root 1 under nav 1). The nav tab reads free — it never reports
        `blocked_by_descendant` and offers no takeover (decision 9)."""
        acquire(self.db, self.WIDGET, self.editor.email)
        view = resolve_lock_view(self.db, ADMIN_EMAIL)
        root = view.state_for(self.ROOT)
        self.assertEqual(root.state, "blocked_by_descendant")
        self.assertFalse(root.takeover_blocked)
        self.assertFalse(view.state_for(self.WIDGET).takeover_blocked)
        self.assertEqual(view.state_for(self.NAV).state, "free")

    def test_an_admins_session_blocks_takeover_on_the_node_and_every_ancestor(self) -> None:
        self._make_assignment_admin(self.editor)
        acquire(self.db, self.WIDGET, self.editor.email)
        view = resolve_lock_view(self.db, ADMIN_EMAIL)
        self.assertEqual(view.state_for(self.WIDGET).state, "locked_here")
        for node in (self.WIDGET, self.ROOT):
            self.assertTrue(view.state_for(node).takeover_blocked, node)
        # A sibling branch is unaffected — and so, since 2026-09-30, is the
        # nav tab (not a lock ancestor any more).
        self.assertFalse(view.state_for(self.OTHER_ROOT).takeover_blocked)
        self.assertFalse(view.state_for(self.NAV).takeover_blocked)

    def test_the_flag_reaches_the_root_tab_list_but_never_the_nav_tab_list(self) -> None:
        """FLIPPED 2026-09-30 (was `test_the_flag_reaches_the_nav_tab_list_
        response`, which read `blocked_by_descendant` + the flag off the
        nav tab): the nav tab now reads free with no flag; the root row in
        the nav tab's root list carries it."""
        self._make_assignment_admin(self.editor)
        acquire(self.db, self.ROOT, self.editor.email)
        resp = self.client.get("/v2/nav-tabs", headers=self._admin_auth())
        self.assertEqual(resp.status_code, 200, resp.text)
        nav = next(n for n in resp.json()["data"] if n["documentId"] == self.nav1.document_id)
        self.assertEqual(nav["lock_state"], "free")
        self.assertFalse(nav["lock_takeover_blocked"])

        resp = self.client.get(f"/v2/nav-tabs/{self.nav1.document_id}/tabs", headers=self._admin_auth())
        self.assertEqual(resp.status_code, 200, resp.text)
        root = next(r for r in resp.json()["data"] if r["documentId"] == self.root_document_id)
        self.assertEqual(root["lock_state"], "locked_here")
        self.assertTrue(root["lock_takeover_blocked"])


class TestJwtOnlyAdminGap(AdminTakeoverFixture):
    """Owner decision: holder admin-ness is by ASSIGNMENT only. A JWT-only
    admin (Airtable role, no `role_assignments` row) is not recognized from
    a stored `locked_by`, so their session CAN still be taken over. Pinned
    so the day phase 3's backstop lands in `hub_admin_user_ids`, this goes
    red and someone looks."""

    def test_a_jwt_only_admins_session_is_not_protected(self) -> None:
        """Forced on the root itself since 2026-09-30 (a nav-tab force no
        longer reaches a root session at all)."""
        jwt_admin = "jwt-only-admin@renphil.org"
        acquire(self.db, self.ROOT, jwt_admin)
        resp = self._force_root()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._holder(self.ROOT), (True, ADMIN_EMAIL))


if __name__ == "__main__":
    unittest.main()
