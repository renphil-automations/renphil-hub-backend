"""Tab / SBN-node locking — plan_access_control_algorithm_2026-08-27.md §6.6,
its two named fixes (session_handoff_2026-09-03-locking.md):

  Fix 1: the lock holder comes from the AUTHENTICATED identity, never from a
         request body field — closing all three TabV2 doors (lock, unlock,
         the generic PUT /{document_id}) plus the omission bypass on unlock,
         and — a finding not in the original brief — the same two doors on
         the parallel SBN-node lock system (super_blocknote_v2.py), which
         reuses the exact same LockTabRequest/UnlockTabRequest schemas.
  Fix 2: locks expire. `TabV2.locked_at` (a real column) and an SBN node's
         `component.props["locked_at"]` (a JSONB key — owner decision
         2026-09-03: Fix 2 extends there too) make a lock STALE, and
         claimable by anyone without `force`, past
         Settings.TAB_LOCK_TTL_SECONDS.

Same in-memory-SQLite router fixture as tests/test_rbac_api.py: a real
FastAPI TestClient over a real (SQLite) v2 schema with JWTs minted locally,
using `test_rbac_api.py`'s `finally: db.close()` dependency override — see
its own comment for why every other fixture in this repo that skips that
line cannot catch a missing `db.rollback()`.

`is_lock_stale`'s TTL boundary is tested by writing a `locked_at` timestamp
directly through `self.db` (bypassing the API) rather than sleeping —
"time injected", not slept, per the task brief.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://validation:validation@localhost/validation",
)
os.environ.setdefault(
    "DATABASE_URL_V2",
    "postgresql+psycopg2://validation:validation@localhost/validation_v2",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-tab-locking-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.config import get_settings  # noqa: E402
from app.db_v2.database import BaseV2, get_db_v2  # noqa: E402
from app.db_v2.models.component import ComponentV2  # noqa: E402
from app.db_v2.models.gridstack import GridstackV2  # noqa: E402
from app.db_v2.models.hub import HubV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.db_v2.models.nav_tab import NavTabV2  # noqa: E402
from app.db_v2.models.resource_grant import NODE_COLUMNS, ResourceGrantV2  # noqa: E402
from app.db_v2.models.tab import TabV2  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.access_visibility_service import (  # noqa: E402
    GRIDSTACK_WIDGET_TYPE,
    resolve_gridstack_node,
)
from app.services.edit_lock_service import (  # noqa: E402
    EditSession,
    EditSessionError,
    LockConflictError,
    LockNode,
    _nav_tab_contents,
    acquire,
    ancestors,
    descendants,
    lock_node_of,
    refuse_if_any_held,
    refuse_if_subtree_held,
    release,
    renew,
    require_live_session,
    resolve_lock_node,
    resolve_lock_view,
)
from app.services.gridstack_service import (  # noqa: E402
    create_tab_v2,
    create_tab_variant_v2,
    delete_tab_subtree_by_document_id_v2,
    get_gridstack_by_document_id,
    is_lock_stale,
    lock_tab_by_document_id_v2,
    unlock_tab_by_document_id_v2,
)
from app.services.nav_tab_service import (  # noqa: E402
    create_nav_tab_v2,
    delete_nav_tab_v2,
    move_tab_to_nav_tab_v2,
    update_nav_tab_v2,
)
from app.services.super_blocknote_service import (  # noqa: E402
    SBN_ROOT_TYPE,
    create_sbn_node,
    lock_sbn_node,
    unlock_sbn_node,
)
from tests._real_admin import revoke_real_admin, seed_real_admin  # noqa: E402

TTL_SECONDS = get_settings().TAB_LOCK_TTL_SECONDS  # 4h, owner decision 2026-09-03


class TabLockingFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )

        @event.listens_for(self.engine, "connect")
        def _enable_fk(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        BaseV2.metadata.create_all(self.engine)
        self._session_factory = sessionmaker(bind=self.engine)
        self.db = self._session_factory()

        app = create_app()

        # Copied from test_rbac_api.py: INCLUDING `finally: db.close()` — see
        # its own comment for why a fixture without this cannot catch a
        # missing db.rollback() (hazard the task brief calls out by name).
        def _override_db():
            db = self._session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_v2] = _override_db
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    # -- auth -------------------------------------------------------------

    def _auth(self, email: str, roles: list[str] | None = None) -> dict:
        """Since phase 3 the JWT "Hub Admin" claim alone makes nobody an
        admin (`is_hub_admin` = bootstrap list OR a `(hub_admin, universal)`
        assignment; the bootstrap list is disabled). Every "Hub Admin" token
        in this module is the admin BYPASS used to isolate lock mechanics
        from the `edit(n)` gate (see `_editor_auth`), never a test about a
        JWT-only admin, so asking for that role here also seeds the real
        assignment row (`tests/_real_admin.py`, own session)."""
        if roles and "Hub Admin" in roles:
            seed_real_admin(self._session_factory, email)
        token = jwt.encode(
            {
                "sub": email,
                "name": email.split("@")[0],
                "roles": roles or [],
                "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
            },
            os.environ["JWT_SECRET_KEY"],
            algorithm=os.environ["JWT_ALGORITHM"],
        )
        return {"Authorization": f"Bearer {token}"}

    def _admin_auth(self) -> dict:
        """`GET .../workspace` is ALSO the content visibility endpoint
        (project_ac_enforcement_gap.md item 1) and this fixture's tabs carry
        no nav_tab_id and no resource_grants — every tab here is an orphan
        (§3.3) to an ordinary identity. Used ONLY to read back lock state
        after a lock/unlock call made under Alice's or Bob's own identity,
        never for the lock/unlock action itself, so it does not change what
        those actions test."""
        return self._auth("admin@renphil.org", roles=["Hub Admin"])

    def _editor_auth(self, email: str) -> dict:
        """plan_ac_enforcement_closeout_2026-09-09.md §3 added an `edit(n)`
        gate to the four lock/unlock doors this module exercises. This
        fixture's tabs/nav-tabs are orphans to an ordinary identity (see
        `_admin_auth` above) — there is no `resource_grants` row an
        ordinary email could hold that would reach them, since nothing here
        hangs off a real `hub`/`nav_tab` chain a ResourceGrantV2 could
        anchor to. Hub Admin isolates these LOCK-MECHANICS tests (staleness,
        holder identity, propagation shape, force) from that unrelated
        permission gate, exactly the convention
        `TestGenericUpdateDoor.test_generic_update_is_refused_without_a_live_session_and_the_lock_survives_untouched`
        and `TestSbnLocking` already established for the SAME gate on the
        generic-update door and the SBN doors respectively. The gate's
        actual AUTHORIZATION behaviour — an ordinary editor succeeding, a
        non-editor refused, and the §3.3 sub-grid trap — is covered by
        `TestLockAuthorization` below, against a real hub/nav-tab tree with
        real grants, never by admin bypass."""
        return self._auth(email, roles=["Hub Admin"])

    def _demote_holder(self, email: str) -> None:
        """Phase 3 (2026-11-07): a holder who took a lock through the admin
        bypass is now a REAL admin, and an admin's session can't be taken
        over (`NODE_LOCKED_BY_ADMIN`). Force tests whose subject is an
        ordinary holder call this after the acquire, so the holder is a
        plain holder again; the lock row is left as it was."""
        revoke_real_admin(self._session_factory, email)

    ALICE = "alice@renphil.org"
    BOB = "bob@renphil.org"

    # -- builders -----------------------------------------------------------

    def _root_tab(self, title: str = "Root") -> dict:
        return create_tab_v2(self.db, title=title)

    def _variant(self, root_document_id: str, title: str = "Variant") -> dict:
        return create_tab_variant_v2(self.db, parent_document_id=root_document_id, title=title)

    def _nested_subtab(self, root_document_id: str, title: str = "Sub") -> dict:
        """A NESTED gridstack (current_grid_id sub-tab), NOT a root and NOT a
        variant — independently lockable in its own right since the
        2026-09-07 edit-mode-gap follow-up (formerly hazard 4's target:
        locking used to be refused here). `create_tab_v2` with a parent
        creates this, not a variant."""
        return create_tab_v2(self.db, title=title, parent_document_id=root_document_id)

    def _nav_tab(self, title: str = "Nav") -> dict:
        return create_nav_tab_v2(self.db, title=title)

    def _sbn_root(self, root_document_id: str, link: str = "sbn-root-1") -> ComponentV2:
        gridstack = get_gridstack_by_document_id(self.db, root_document_id)
        component = ComponentV2(link=link, title=link, type=SBN_ROOT_TYPE, gridstack_id=gridstack.id)
        self.db.add(component)
        self.db.commit()
        return component

    def _sbn_leaf(self, parent_link: str, title: str = "Leaf") -> dict:
        return create_sbn_node(self.db, parent_link, title, content=[{"type": "paragraph"}])

    def _age_tab_lock(self, document_id: str, seconds_ago: float | None) -> None:
        """Write `locked_at` directly, bypassing the API — "time injected,
        not slept". `seconds_ago=None` writes NULL, simulating a lock taken
        before this column existed (tab 42's real shape)."""
        tab = (
            self.db.query(TabV2)
            .filter(TabV2.id == get_gridstack_by_document_id(self.db, document_id).parent_tab_id)
            .first()
        )
        tab.locked_at = (
            None if seconds_ago is None else datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        )
        self.db.commit()

    def _age_nested_gridstack_lock(self, document_id: str, seconds_ago: float | None) -> None:
        """Same as `_age_tab_lock` above, but for a NESTED gridstack's OWN
        `locked_at` column (2026-09-07 edit-mode-gap follow-up) — a nested
        gridstack has no TabV2 row of its own to look one up through."""
        gridstack = get_gridstack_by_document_id(self.db, document_id)
        gridstack.locked_at = (
            None if seconds_ago is None else datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        )
        self.db.commit()

    def _age_sbn_lock(self, component: ComponentV2, seconds_ago: float | None) -> None:
        """Same as `_age_tab_lock`, on a component's OWN `locked_at` column
        — since 2026-09-17 (plan_component_locking_and_sbn §3/§6) an SBN
        node's lock lives on the four real lock columns, not `props`."""
        self.db.refresh(component)
        component.locked_at = (
            None if seconds_ago is None else datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        )
        self.db.commit()


# ---------------------------------------------------------------------------
# is_lock_stale — the shared TTL helper, unit-level
# ---------------------------------------------------------------------------


class TestIsLockStale(unittest.TestCase):
    def test_none_is_stale(self) -> None:
        self.assertTrue(is_lock_stale(None))

    def test_well_within_ttl_is_fresh(self) -> None:
        self.assertFalse(is_lock_stale(datetime.now(timezone.utc) - timedelta(seconds=60)))

    def test_well_past_ttl_is_stale(self) -> None:
        self.assertTrue(is_lock_stale(datetime.now(timezone.utc) - timedelta(seconds=TTL_SECONDS + 3600)))

    def test_boundary_just_inside_ttl_is_fresh(self) -> None:
        self.assertFalse(is_lock_stale(datetime.now(timezone.utc) - timedelta(seconds=TTL_SECONDS - 5)))

    def test_boundary_just_past_ttl_is_stale(self) -> None:
        self.assertTrue(is_lock_stale(datetime.now(timezone.utc) - timedelta(seconds=TTL_SECONDS + 5)))

    def test_exactly_at_ttl_is_still_fresh_strictly_past_it_is_stale(self) -> None:
        """Pins the `>` (not `>=`) semantics exactly, with a fixed "now"
        (`_utc_now` patched) rather than relying on wall-clock margin — a
        wide-margin boundary test (the ones above) cannot tell `>` from
        `>=` apart, since real elapsed time between constructing `locked_at`
        and the function's own `_utc_now()` call always overshoots the
        target by at least a few microseconds. This is the test that would
        have caught the `>=` mutation those coarser ones did not."""
        fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch("app.services.gridstack_service._utc_now", return_value=fixed_now):
            exactly_at_ttl = fixed_now - timedelta(seconds=TTL_SECONDS)
            self.assertFalse(is_lock_stale(exactly_at_ttl))

            one_microsecond_past_ttl = fixed_now - timedelta(seconds=TTL_SECONDS, microseconds=1)
            self.assertTrue(is_lock_stale(one_microsecond_past_ttl))

    def test_naive_datetime_is_handled_not_raised(self) -> None:
        # hazard 7: SQLite does not round-trip tzinfo. A naive value must be
        # treated as UTC, not raise "can't subtract offset-naive and
        # offset-aware datetimes".
        naive_fresh = (datetime.now(timezone.utc) - timedelta(seconds=60)).replace(tzinfo=None)
        naive_stale = (
            datetime.now(timezone.utc) - timedelta(seconds=TTL_SECONDS + 3600)
        ).replace(tzinfo=None)
        self.assertFalse(is_lock_stale(naive_fresh))
        self.assertTrue(is_lock_stale(naive_stale))


# ---------------------------------------------------------------------------
# Door 1 — PUT /v2/tabs/{document_id}/lock
# ---------------------------------------------------------------------------


class TestLockDoor(TabLockingFixture):
    def test_locked_by_is_ignored_client_supplied_value_is_rejected(self) -> None:
        """LockTabRequest has no `locked_by` field any more — a client that
        still sends one gets a 422 (extra=forbid), the loud failure this
        door needs, not a silent ignore."""
        root = self._root_tab()
        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}/lock",
            json={"locked_by": "someone-else@evil.example.com"},
            headers=self._editor_auth(self.ALICE),
        )
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_holder_is_the_authenticated_identity(self) -> None:
        root = self._root_tab()
        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()["data"]
        self.assertEqual(body["locked_by"], self.ALICE)
        self.assertTrue(body["locked"])
        self.assertIsNotNone(body["locked_at"])
        self.assertFalse(body["lock_is_stale"])
        # findings_dev_login_live_testing_2026-09-12.md addendum:
        # `lock_expires_at` was silently `null` on every real acquire here
        # (`get_tab_workspace_v2` bare never sets it) — the frontend gates
        # `setEditSession` on `lock_token && lock_expires_at` both being
        # truthy (`acquireRootLock`, DashboardV2Page.tsx), so a missing
        # `lock_expires_at` meant no `X-Edit-Tokens` was ever sent on the
        # follow-up write and every root-tab rename/delete 409'd
        # EDIT_SESSION_MISSING. Pin both fields now that `grant.expires_at`
        # is actually assigned onto the response.
        self.assertIsInstance(body["lock_token"], str)
        self.assertTrue(body["lock_token"])
        self.assertIsNotNone(body["lock_expires_at"])

    def test_second_user_cannot_take_a_fresh_lock(self) -> None:
        root = self._root_tab()
        self.client.put(f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.BOB)
        )
        self.assertEqual(resp.status_code, 409, resp.text)

    def test_same_holder_can_re_enter(self) -> None:
        root = self._root_tab()
        self.client.put(f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["data"]["locked_by"], self.ALICE)

    def test_stale_lock_is_claimable_without_force(self) -> None:
        root = self._root_tab()
        self.client.put(f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        self._age_tab_lock(root["documentId"], seconds_ago=TTL_SECONDS + 60)
        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.BOB)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["data"]["locked_by"], self.BOB)

    def test_fresh_lock_is_not_claimable(self) -> None:
        root = self._root_tab()
        self.client.put(f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        self._age_tab_lock(root["documentId"], seconds_ago=TTL_SECONDS - 60)
        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.BOB)
        )
        self.assertEqual(resp.status_code, 409, resp.text)

    def test_null_locked_at_on_an_already_locked_row_is_stale(self) -> None:
        """Tab 42's real shape: locked=True, locked_by set, locked_at NULL
        (it predates this column). Must be claimable, not permanently
        stuck."""
        root = self._root_tab()
        self.client.put(f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        self._age_tab_lock(root["documentId"], seconds_ago=None)
        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.BOB)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["data"]["locked_by"], self.BOB)

    def test_lock_works_on_a_variant(self) -> None:
        """Hazard 5: a variant is a full TabV2 with its own root gridstack,
        so _is_root passes and it is independently lockable."""
        root = self._root_tab()
        variant = self._variant(root["documentId"])
        resp = self.client.put(
            f"/v2/tabs/{variant['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        # The root is untouched by locking the variant.
        root_resp = self.client.get(f"/v2/tabs/{root['documentId']}/workspace", headers=self._admin_auth())
        self.assertFalse(root_resp.json()["data"]["locked"])

    def test_lock_works_on_a_nested_gridstack(self) -> None:
        """Hazard 4, REVISED 2026-09-07 (edit-mode-gap follow-up, independent
        per-sub-gridstack locking): a nested (non-root, non-variant) sub-tab
        is now independently lockable on its OWN locked/locked_by/locked_at
        columns, same as a variant already was — and locking it leaves its
        owning root untouched, exactly like the variant case above."""
        root = self._root_tab()
        sub = self._nested_subtab(root["documentId"])
        resp = self.client.put(
            f"/v2/tabs/{sub['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["data"]["locked"])
        self.assertEqual(resp.json()["data"]["locked_by"], self.ALICE)
        # The root is untouched by locking the nested sub-tab.
        root_resp = self.client.get(f"/v2/tabs/{root['documentId']}/workspace", headers=self._admin_auth())
        self.assertFalse(root_resp.json()["data"]["locked"])

    def test_second_user_cannot_take_a_fresh_lock_on_a_nested_gridstack(self) -> None:
        root = self._root_tab()
        sub = self._nested_subtab(root["documentId"])
        self.client.put(f"/v2/tabs/{sub['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        resp = self.client.put(
            f"/v2/tabs/{sub['documentId']}/lock", json={}, headers=self._editor_auth(self.BOB)
        )
        self.assertEqual(resp.status_code, 409, resp.text)

    def test_stale_lock_on_a_nested_gridstack_is_claimable_without_force(self) -> None:
        root = self._root_tab()
        sub = self._nested_subtab(root["documentId"])
        self.client.put(f"/v2/tabs/{sub['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        self._age_nested_gridstack_lock(sub["documentId"], seconds_ago=TTL_SECONDS + 60)
        resp = self.client.put(
            f"/v2/tabs/{sub['documentId']}/lock", json={}, headers=self._editor_auth(self.BOB)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["data"]["locked_by"], self.BOB)

    def test_sibling_nested_gridstacks_lock_independently(self) -> None:
        """The whole point of this session's change: locking one
        sub-gridstack must not reach its sibling — no cascade, no shared
        whole-tab lock. Both siblings hang off the SAME root."""
        root = self._root_tab()
        sub_a = self._nested_subtab(root["documentId"], title="Sub A")
        sub_b = self._nested_subtab(root["documentId"], title="Sub B")
        self.client.put(f"/v2/tabs/{sub_a['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        # Bob can still lock the sibling — Alice's lock on sub_a did not
        # reach it.
        resp = self.client.put(
            f"/v2/tabs/{sub_b['documentId']}/lock", json={}, headers=self._editor_auth(self.BOB)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["data"]["locked_by"], self.BOB)
        # And sub_a is still Alice's, untouched by locking sub_b.
        sub_a_resp = self.client.get(
            f"/v2/tabs/{sub_a['documentId']}/workspace", headers=self._admin_auth()
        )
        self.assertEqual(sub_a_resp.json()["data"]["locked_by"], self.ALICE)


class TestRootLockCascadesToNestedGridstacks(TabLockingFixture):
    """RE-PINNED, plan_lock_propagation_2026-09-08.md §7 — deliberately, not
    a regression. Decision 2 deletes the cascade write this class used to
    assert on ("one lock row on the node actually being edited; every other
    node's state is DERIVED"): locking a root no longer writes
    locked=True onto its nested sub-grids' own rows at all. Every guarantee
    here survives intact, just one layer down — each assertion that used to
    read a descendant's OWN `locked`/`locked_by` fields now instead confirms
    a SECOND user's acquire attempt on that descendant is refused (or, once
    the ancestor goes stale / gets released, succeeds). Class name kept
    (still literally about a root lock's effect on nested sub-grids) even
    though nothing is literally "cascaded" onto them any more.

    The two lock flavors used to be entirely unaware of each other (found
    live, 2026-09-07): locking a root/variant never touched its nested
    sub-gridstacks' own lock columns, so a second user could independently,
    successfully lock (and write to) a sub-gridstack while the first user's
    whole-subtree lock believed it already owned it. That hole stays
    closed — see `edit_lock_service.acquire`'s ancestor check."""

    def test_locking_the_root_writes_no_row_on_the_nested_sub_grid_but_still_blocks_a_second_user(
        self,
    ) -> None:
        """Decision 2, made concrete: the sub-grid's OWN raw `locked` field
        never flips to True — coverage is derived, not written — yet a
        second user is still refused, exactly as when a literal cascade
        used to flag the row."""
        root = self._root_tab()
        sub = self._nested_subtab(root["documentId"])
        resp = self.client.put(f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        self.assertEqual(resp.status_code, 200, resp.text)

        sub_resp = self.client.get(f"/v2/tabs/{sub['documentId']}/workspace", headers=self._admin_auth())
        self.assertFalse(sub_resp.json()["data"]["locked"])
        self.assertEqual(sub_resp.json()["data"]["locked_by"], "")

        blocked = self.client.put(f"/v2/tabs/{sub['documentId']}/lock", json={}, headers=self._editor_auth(self.BOB))
        self.assertEqual(blocked.status_code, 409, blocked.text)

    def test_a_second_user_cannot_independently_lock_a_sub_grid_while_the_root_holds_it(self) -> None:
        root = self._root_tab()
        sub = self._nested_subtab(root["documentId"])
        self.client.put(f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        resp = self.client.put(f"/v2/tabs/{sub['documentId']}/lock", json={}, headers=self._editor_auth(self.BOB))
        self.assertEqual(resp.status_code, 409, resp.text)

    def test_the_same_holder_can_still_directly_lock_a_sub_grid_while_holding_the_root(self) -> None:
        """Same holder never conflicts with themself, at any level (§4.1) —
        Alice holding the root's ancestor lock never blocks Alice's own
        direct acquire on a descendant sub-grid."""
        root = self._root_tab()
        sub = self._nested_subtab(root["documentId"])
        self.client.put(f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        resp = self.client.put(f"/v2/tabs/{sub['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_locking_the_root_is_refused_if_a_nested_sub_grid_is_already_held_by_someone_else(self) -> None:
        """The reverse direction: Bob independently locks the sub-grid
        first; Alice's later attempt to lock the whole root must not
        silently steal it out from under him."""
        root = self._root_tab()
        sub = self._nested_subtab(root["documentId"])
        self.client.put(f"/v2/tabs/{sub['documentId']}/lock", json={}, headers=self._editor_auth(self.BOB))
        resp = self.client.put(f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        self.assertEqual(resp.status_code, 409, resp.text)
        # And the root itself was never locked as a side effect of the
        # refused attempt.
        root_resp = self.client.get(f"/v2/tabs/{root['documentId']}/workspace", headers=self._admin_auth())
        self.assertFalse(root_resp.json()["data"]["locked"])

    def test_a_second_user_CAN_lock_a_sub_grid_once_the_roots_lock_goes_stale(self) -> None:
        """Only the ROOT's own `locked_at` needs to age now — there is no
        second, cascaded row on the sub-grid to age in step with it any
        more (decision 2), so the ancestor check alone governs whether Bob
        may claim the sub-grid directly."""
        root = self._root_tab()
        sub = self._nested_subtab(root["documentId"])
        self.client.put(f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        self._age_tab_lock(root["documentId"], seconds_ago=TTL_SECONDS + 60)
        resp = self.client.put(f"/v2/tabs/{sub['documentId']}/lock", json={}, headers=self._editor_auth(self.BOB))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["data"]["locked_by"], self.BOB)

    def test_unlocking_the_root_lifts_the_block_on_its_nested_sub_grid(self) -> None:
        """The real, still-standing guarantee behind the old row-flag
        assertion: releasing the root's ancestor lock is what lets a second
        user acquire the sub-grid — confirmed on both sides of the release,
        not just inferred from the sub-grid's own (never-written) row."""
        root = self._root_tab()
        sub = self._nested_subtab(root["documentId"])
        self.client.put(f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))

        still_blocked = self.client.put(
            f"/v2/tabs/{sub['documentId']}/lock", json={}, headers=self._editor_auth(self.BOB)
        )
        self.assertEqual(still_blocked.status_code, 409, still_blocked.text)

        unlock_resp = self.client.put(
            f"/v2/tabs/{root['documentId']}/unlock", json={}, headers=self._editor_auth(self.ALICE)
        )
        self.assertEqual(unlock_resp.status_code, 200, unlock_resp.text)

        now_free = self.client.put(f"/v2/tabs/{sub['documentId']}/lock", json={}, headers=self._editor_auth(self.BOB))
        self.assertEqual(now_free.status_code, 200, now_free.text)
        self.assertEqual(now_free.json()["data"]["locked_by"], self.BOB)

    def test_unlocking_the_root_does_not_release_a_sub_grid_someone_else_independently_re_claimed(
        self,
    ) -> None:
        """A defensive edge case, simpler under the new model than the old
        one: since there is no cascaded row to accidentally clear (decision
        2), releasing the root can only ever touch the root's own row —
        Bob's completely independent, directly-held sub-grid lock is
        untouched by construction, not merely by an ownership check that
        happens to still recognize it."""
        root = self._root_tab()
        sub = self._nested_subtab(root["documentId"])
        self.client.put(f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        self._age_tab_lock(root["documentId"], seconds_ago=TTL_SECONDS + 60)
        self.client.put(f"/v2/tabs/{sub['documentId']}/lock", json={}, headers=self._editor_auth(self.BOB))
        self.client.put(
            f"/v2/tabs/{root['documentId']}/unlock",
            json={"force": True},
            headers=self._editor_auth(self.ALICE),
        )
        sub_resp = self.client.get(f"/v2/tabs/{sub['documentId']}/workspace", headers=self._admin_auth())
        self.assertTrue(sub_resp.json()["data"]["locked"])
        self.assertEqual(sub_resp.json()["data"]["locked_by"], self.BOB)


# ---------------------------------------------------------------------------
# Door 2 — PUT /v2/tabs/{document_id}/unlock, including the omission bypass
# ---------------------------------------------------------------------------


class TestUnlockDoor(TabLockingFixture):
    def test_unlocked_by_is_ignored_client_supplied_value_is_rejected(self) -> None:
        root = self._root_tab()
        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}/unlock",
            json={"unlocked_by": "someone-else@evil.example.com"},
            headers=self._editor_auth(self.ALICE),
        )
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_holder_can_unlock_their_own_lock(self) -> None:
        root = self._root_tab()
        self.client.put(f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}/unlock", json={}, headers=self._editor_auth(self.ALICE)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["data"]["locked"])
        self.assertEqual(resp.json()["data"]["locked_by"], "")
        self.assertIsNone(resp.json()["data"]["locked_at"])

    def test_omission_bypass_is_closed_a_second_user_cannot_steal_a_fresh_lock(self) -> None:
        """THE test the original bug would have failed. Before Fix 1,
        `unlocked_by` was an optional body field and omitting it (or
        sending null) short-circuited the ownership check to False —
        `force: true` was never actually required to steal a lock. There is
        no longer a field to omit: the identity always comes from the JWT,
        so Bob's own unlock call always carries Bob's own identity and can
        never accidentally (or deliberately) carry nothing."""
        root = self._root_tab()
        self.client.put(f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}/unlock", json={}, headers=self._editor_auth(self.BOB)
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        # Confirm it is REALLY still locked, by Alice, server-side.
        check = self.client.get(f"/v2/tabs/{root['documentId']}/workspace", headers=self._admin_auth())
        self.assertTrue(check.json()["data"]["locked"])
        self.assertEqual(check.json()["data"]["locked_by"], self.ALICE)

    def test_force_unlock_ends_a_fresh_lock(self) -> None:
        """Force-unlock skips ownership even against a FRESH lock. Runs as
        Hub Admin via `_editor_auth` — force is admin-only since 2026-09-25
        (non-admin refusal is covered in `TestLockAuthorization`)."""
        root = self._root_tab()
        self.client.put(f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        self._demote_holder(self.ALICE)
        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}/unlock", json={"force": True}, headers=self._editor_auth(self.BOB)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["data"]["locked"])

    def test_stale_lock_is_unlockable_by_anyone_without_force(self) -> None:
        root = self._root_tab()
        self.client.put(f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        self._age_tab_lock(root["documentId"], seconds_ago=TTL_SECONDS + 60)
        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}/unlock", json={}, headers=self._editor_auth(self.BOB)
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_holder_can_unlock_their_own_lock_on_a_nested_gridstack(self) -> None:
        root = self._root_tab()
        sub = self._nested_subtab(root["documentId"])
        self.client.put(f"/v2/tabs/{sub['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        resp = self.client.put(
            f"/v2/tabs/{sub['documentId']}/unlock", json={}, headers=self._editor_auth(self.ALICE)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["data"]["locked"])
        self.assertEqual(resp.json()["data"]["locked_by"], "")
        self.assertIsNone(resp.json()["data"]["locked_at"])

    def test_omission_bypass_is_closed_on_a_nested_gridstack_too(self) -> None:
        root = self._root_tab()
        sub = self._nested_subtab(root["documentId"])
        self.client.put(f"/v2/tabs/{sub['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        resp = self.client.put(
            f"/v2/tabs/{sub['documentId']}/unlock", json={}, headers=self._editor_auth(self.BOB)
        )
        self.assertEqual(resp.status_code, 400, resp.text)


# ---------------------------------------------------------------------------
# Door 3 — PUT /v2/tabs/{document_id} (generic update) — the one that would
# otherwise be missed
# ---------------------------------------------------------------------------


class TestGenericUpdateDoor(TabLockingFixture):
    def test_locked_field_is_rejected(self) -> None:
        root = self._root_tab()
        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}", json={"locked": True}, headers=self._editor_auth(self.ALICE)
        )
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_locked_by_field_is_rejected(self) -> None:
        root = self._root_tab()
        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}",
            json={"locked_by": "anyone@evil.example.com"},
            headers=self._editor_auth(self.ALICE),
        )
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_generic_update_is_refused_without_a_live_session_and_the_lock_survives_untouched(
        self,
    ) -> None:
        """UPDATED for plan_lock_propagation_2026-09-08.md decision 4:
        "every mutating v2 write must carry a live edit-session token", with
        no Hub Admin exception written anywhere — locking is concurrent-edit
        SAFETY, a different axis from PERMISSION, and Hub Admin's bypass
        (ViewerAccess.verdict) has never covered anything but the latter.
        Before this plan, Bob's admin rename here succeeded outright and
        this test verified that success didn't clear Alice's lock as a side
        effect (the historical "third door" bug, plan §6.6 Fix 1). Now the
        write itself is refused — Bob holds no session on root, and
        Alice's fresh lock refuses him one — so this test's surviving
        assertion is that the REFUSAL leaves Alice's lock exactly as it
        was, not cleared as a side effect of the attempt.

        Bob still authenticates as Hub Admin, to isolate this from the
        unrelated `edit(n)` gate (project_ac_enforcement_gap.md item 2) —
        this fixture's tabs carry no nav_tab_id/resource_grants for an
        ordinary identity to hold (§3.3: orphaned, invisible to anyone but
        an admin)."""
        root = self._root_tab()
        self.client.put(f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}",
            json={"title": "Renamed"},
            headers=self._auth(self.BOB, roles=["Hub Admin"]),
        )
        self.assertEqual(resp.status_code, 409, resp.text)

        workspace = self.client.get(f"/v2/tabs/{root['documentId']}/workspace", headers=self._admin_auth())
        self.assertTrue(workspace.json()["data"]["locked"])
        self.assertEqual(workspace.json()["data"]["locked_by"], self.ALICE)

    def test_the_real_lock_doors_own_token_authorizes_the_rename(self) -> None:
        """findings_dev_login_live_testing_2026-09-12.md addendum: the one
        path that actually exercises `lock_tab_by_document_id_v2`'s
        response shape rather than `edit_lock_service.acquire()` directly —
        every other token in this module (`TestEditSessionTokens` above,
        the sibling nav-tab test) is built that second way, which always
        carries `expires_at` correctly since `LockGrant` was never the bug.
        `get_tab_workspace_v2(db, document_id)` bare never sets
        `lock_expires_at` (additive, only when a caller passes `lock_view`)
        — so the REAL `/lock` HTTP response's `lock_expires_at` came back
        `null` regardless of a real, successful acquire, `acquireRootLock`'s
        `if (locked.lock_token && locked.lock_expires_at)` guard
        (DashboardV2Page.tsx) was always false, `setEditSession` never ran,
        and no real browser session ever sent `X-Edit-Tokens` — every
        root-tab rename 409'd EDIT_SESSION_MISSING, lock conflict or not."""
        root = self._root_tab()
        lock_resp = self.client.put(
            f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE)
        )
        self.assertEqual(lock_resp.status_code, 200, lock_resp.text)
        token = lock_resp.json()["data"]["lock_token"]

        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}",
            json={"title": "Renamed"},
            headers={**self._editor_auth(self.ALICE), "X-Edit-Tokens": token},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["data"]["title"], "Renamed")


# ---------------------------------------------------------------------------
# Service-level defense-in-depth — the falsy-`unlocked_by` guard in
# unlock_tab_by_document_id_v2's ownership check is UNREACHABLE through the
# router now (the JWT always supplies a real email), so no router-level test
# can exercise it. Called directly, bypassing the router, the way an
# internal/scripted caller could — see the function's own docstring for why
# the guard stays even though the API can no longer trigger it.
# ---------------------------------------------------------------------------


class TestUnlockedByFalsyGuardIsServiceLevelDefenseInDepth(TabLockingFixture):
    def test_empty_string_unlocked_by_does_not_match_every_lock(self) -> None:
        root = self._root_tab()
        lock_tab_by_document_id_v2(self.db, root["documentId"], locked_by=self.ALICE)
        with self.assertRaises(ValueError):
            unlock_tab_by_document_id_v2(self.db, root["documentId"], unlocked_by="")

    def test_none_unlocked_by_does_not_match_every_lock(self) -> None:
        root = self._root_tab()
        lock_tab_by_document_id_v2(self.db, root["documentId"], locked_by=self.ALICE)
        with self.assertRaises(ValueError):
            unlock_tab_by_document_id_v2(self.db, root["documentId"], unlocked_by=None)

    def test_sbn_mirrors_the_same_guard(self) -> None:
        root = self._root_tab()
        sbn_root = self._sbn_root(root["documentId"])
        lock_sbn_node(self.db, sbn_root.link, locked_by=self.ALICE)
        with self.assertRaises(ValueError):
            unlock_sbn_node(self.db, sbn_root.link, unlocked_by=None)
        with self.assertRaises(ValueError):
            unlock_sbn_node(self.db, sbn_root.link, unlocked_by="")


# ---------------------------------------------------------------------------
# Hazard 1 — locking stays advisory, writes are NOT blocked (pinned)
# ---------------------------------------------------------------------------


class TestLockingStaysAdvisory(TabLockingFixture):
    """REVERSED, DELIBERATELY, plan_lock_propagation_2026-09-08.md decision
    4 — this class used to pin plan §12's "locking is advisory, unenforced
    on writes" and named itself accordingly. Decision 4 is the explicit
    opposite: "every mutating v2 write must carry a live edit-session
    token." The class NAME is kept (grep-ability across the two plans'
    handoffs) even though its one test now pins the reversed behaviour —
    see `test_content_write_is_refused_without_a_live_session`'s own
    docstring for what replaced the old pin."""

    def test_content_write_is_refused_without_a_live_session(self) -> None:
        """THE reversal. Bob authenticates as Hub Admin — isolates "the
        write needs a session" from the UNRELATED `edit(n)` permission gate
        (project_ac_enforcement_gap.md item 2): this fixture's orphaned
        tabs (§3.3) give an ordinary identity no grant to hold, and Bob's
        admin bypass clears that gate on its own, so a 400 here can only be
        the NEW session check, never the old permission one. No Hub Admin
        exception exists for the session requirement (unlike the
        permission fold) — locking is concurrent-edit safety, a different
        axis entirely; see TestGenericUpdateDoor's own updated test for the
        matching case on the generic PUT door."""
        root = self._root_tab()
        self.client.put(f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}/content",
            json={"content": []},
            headers=self._auth(self.BOB, roles=["Hub Admin"]),
        )
        self.assertEqual(resp.status_code, 409, resp.text)

    def test_content_write_succeeds_for_the_actual_session_holder(self) -> None:
        """The other half of decision 4 — a live session, correctly
        presented, is not just a refusal generator: Alice, holding the
        lock she just acquired, can save content through the same door
        Bob was refused on. Alice authenticates as Hub Admin too, same
        isolation reason as the class's other test (§3.3's orphaned
        fixture tabs) — this test is about the SESSION check passing, not
        about the unrelated permission one."""
        root = self._root_tab()
        alice_admin = self._auth(self.ALICE, roles=["Hub Admin"])
        lock_resp = self.client.put(f"/v2/tabs/{root['documentId']}/lock", json={}, headers=alice_admin)
        token = lock_resp.json()["data"]["lock_token"]
        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}/content",
            json={"content": []},
            headers={**alice_admin, "X-Edit-Tokens": token},
        )
        self.assertEqual(resp.status_code, 200, resp.text)


# ---------------------------------------------------------------------------
# The SBN-node lock system — a finding not in the original brief. Same two
# fixes, different storage (component.props, not a column).
# ---------------------------------------------------------------------------


class TestSbnLocking(TabLockingFixture):
    """MECHANISM ONLY — the AUTHORIZATION of these same routes is
    `test_sbn_access_control.py`'s `TestLocking`, against a properly rooted
    tree with real grants.

    Every call below authenticates as a Hub Admin, for exactly the reason
    `_admin_auth` already gives for this fixture's reads: these tabs carry no
    `nav_tab_id` and no `resource_grants`, so every node here is an orphan
    (§3.3) and an ordinary identity now gets a fail-closed 404 from the
    `edit(n)` gate that §6.3 added to lock/unlock on 2026-09-09. Two DISTINCT
    admin identities are used rather than one, because what these tests
    actually assert — the holder is taken from the JWT, a fresh lock is not
    claimable by someone else, a stale one is — is about two callers being
    told apart, which the bypass does not affect.
    """

    def _lock_auth(self, email: str) -> dict:
        return self._auth(email, roles=["Hub Admin"])

    def _make_root_and_sbn(self) -> tuple[dict, ComponentV2]:
        root = self._root_tab()
        sbn_root = self._sbn_root(root["documentId"])
        return root, sbn_root

    def test_locked_by_is_rejected_on_sbn_lock_too(self) -> None:
        _, sbn_root = self._make_root_and_sbn()
        resp = self.client.put(
            f"/v2/sbn/{sbn_root.link}/lock",
            json={"locked_by": "evil@example.com"},
            headers=self._lock_auth(self.ALICE),
        )
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_holder_is_the_authenticated_identity(self) -> None:
        _, sbn_root = self._make_root_and_sbn()
        resp = self.client.put(f"/v2/sbn/{sbn_root.link}/lock", json={}, headers=self._lock_auth(self.ALICE))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["data"]["locked_by"], self.ALICE)
        self.assertIsNotNone(resp.json()["data"]["locked_at"])

    def test_second_user_cannot_take_a_fresh_lock(self) -> None:
        """409 NODE_LOCKED since 2026-09-17 (was a plain 400): the SBN lock
        is `edit_lock_service.acquire`'s `LockConflictError` now, the same
        error contract every other lock door has."""
        _, sbn_root = self._make_root_and_sbn()
        self.client.put(f"/v2/sbn/{sbn_root.link}/lock", json={}, headers=self._lock_auth(self.ALICE))
        resp = self.client.put(f"/v2/sbn/{sbn_root.link}/lock", json={}, headers=self._lock_auth(self.BOB))
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "NODE_LOCKED")
        self.assertEqual(resp.json()["detail"]["blocking"][0]["holder"], self.ALICE)

    def test_the_lock_response_carries_the_token_and_expiry_and_nothing_else_does(self) -> None:
        """plan_component_locking §6: `lock_sbn_node` is the ONLY SBN
        response that populates `lock_token` (the narrow-population rule
        `TabWorkspaceResponse.lock_token` sets)."""
        _, sbn_root = self._make_root_and_sbn()
        resp = self.client.put(f"/v2/sbn/{sbn_root.link}/lock", json={}, headers=self._lock_auth(self.ALICE))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["data"]["lock_token"])
        self.assertIsNotNone(resp.json()["data"]["lock_expires_at"])
        # The lock lives on the columns, not props.
        self.db.expire_all()
        row = self.db.query(ComponentV2).filter(ComponentV2.id == sbn_root.id).first()
        self.assertTrue(row.locked)
        self.assertEqual(row.locked_by, self.ALICE)
        self.assertEqual(row.lock_token, resp.json()["data"]["lock_token"])
        self.assertNotIn("locked", row.props or {})

        ws = self.client.get(f"/v2/sbn/{sbn_root.link}/workspace", headers=self._lock_auth(self.ALICE))
        self.assertIsNone(ws.json()["data"]["lock_token"])
        self.assertEqual(ws.json()["data"]["lock_state"], "self")

    def test_omission_bypass_closed_on_sbn_unlock(self) -> None:
        _, sbn_root = self._make_root_and_sbn()
        self.client.put(f"/v2/sbn/{sbn_root.link}/lock", json={}, headers=self._lock_auth(self.ALICE))
        resp = self.client.put(f"/v2/sbn/{sbn_root.link}/unlock", json={}, headers=self._lock_auth(self.BOB))
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_stale_sbn_lock_is_claimable(self) -> None:
        """Fix 2 extended to SBN (owner decision 2026-09-03) — locked_at
        was props['locked_at'] then; since 2026-09-17 it is the real
        `components.locked_at` column, same `is_lock_stale` rule."""
        _, sbn_root = self._make_root_and_sbn()
        self.client.put(f"/v2/sbn/{sbn_root.link}/lock", json={}, headers=self._lock_auth(self.ALICE))
        self._age_sbn_lock(sbn_root, seconds_ago=TTL_SECONDS + 60)
        resp = self.client.put(f"/v2/sbn/{sbn_root.link}/lock", json={}, headers=self._lock_auth(self.BOB))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["data"]["locked_by"], self.BOB)

    def test_fresh_sbn_lock_is_not_claimable(self) -> None:
        _, sbn_root = self._make_root_and_sbn()
        self.client.put(f"/v2/sbn/{sbn_root.link}/lock", json={}, headers=self._lock_auth(self.ALICE))
        self._age_sbn_lock(sbn_root, seconds_ago=TTL_SECONDS - 60)
        resp = self.client.put(f"/v2/sbn/{sbn_root.link}/lock", json={}, headers=self._lock_auth(self.BOB))
        self.assertEqual(resp.status_code, 409, resp.text)

    def test_null_locked_at_on_an_already_locked_sbn_node_is_stale(self) -> None:
        _, sbn_root = self._make_root_and_sbn()
        self.client.put(f"/v2/sbn/{sbn_root.link}/lock", json={}, headers=self._lock_auth(self.ALICE))
        self._age_sbn_lock(sbn_root, seconds_ago=None)
        resp = self.client.put(f"/v2/sbn/{sbn_root.link}/lock", json={}, headers=self._lock_auth(self.BOB))
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_a_root_session_covers_its_leaves_a_leaf_is_no_longer_independently_claimable(self) -> None:
        """REVERSED 2026-09-17 (plan_component_locking_and_sbn §6, finding
        4b) — this was `test_a_leaf_is_independently_lockable_not_just_the_
        root`, pinning "locking the root must NOT lock the leaf, and vice
        versa": an editor holding the root could edit sub-tab X while
        someone else held X, last writer wins. Now the SBN is in the lock
        tree: the leaf's OWN row stays unlocked (decision 2, one lock row
        per subtree) but its derived state is `locked_by_ancestor`, and
        Bob's acquire on it is refused while Alice holds the root."""
        root, sbn_root = self._make_root_and_sbn()
        leaf = self._sbn_leaf(sbn_root.link)

        self.client.put(f"/v2/sbn/{sbn_root.link}/lock", json={}, headers=self._lock_auth(self.ALICE))
        leaf_resp = self.client.get(f"/v2/sbn/{leaf['documentId']}/workspace", headers=self._lock_auth(self.BOB))
        self.assertFalse(leaf_resp.json()["data"]["locked"])  # own row untouched
        self.assertEqual(leaf_resp.json()["data"]["lock_state"], "locked_by_ancestor")
        self.assertEqual(leaf_resp.json()["data"]["lock_holder"], self.ALICE)

        resp = self.client.put(f"/v2/sbn/{leaf['documentId']}/lock", json={}, headers=self._lock_auth(self.BOB))
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["blocking"][0]["relation"], "ancestor")

    def test_a_held_leaf_refuses_the_root_and_the_canvas_above_it(self) -> None:
        """The other direction of finding 4b/4c: an admin entering canvas
        edit mode while a sub-tab is mid-edit is refused, naming it."""
        root, sbn_root = self._make_root_and_sbn()
        leaf = self._sbn_leaf(sbn_root.link)
        self.client.put(f"/v2/sbn/{leaf['documentId']}/lock", json={}, headers=self._lock_auth(self.BOB))

        resp = self.client.put(f"/v2/sbn/{sbn_root.link}/lock", json={}, headers=self._lock_auth(self.ALICE))
        self.assertEqual(resp.status_code, 409, resp.text)
        resp = self.client.put(f"/v2/tabs/{root['documentId']}/lock", json={}, headers=self._lock_auth(self.ALICE))
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["blocking"][0]["node_label"], "Leaf")
        # Force takes the whole subtree over (Bob demoted first: an admin's
        # session can't be taken over, and Bob was only an admin to lock).
        self._demote_holder(self.BOB)
        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}/lock", json={"force": True}, headers=self._lock_auth(self.ALICE)
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_sbn_generic_update_does_not_accept_lock_fields(self) -> None:
        """UpdateTabRequest has no locked/locked_by fields at all any more
        (shared schema) — update_node never forwarded them to
        update_sbn_node even before this session, so this is confirming
        the door was never open here, not just that it stays closed."""
        _, sbn_root = self._make_root_and_sbn()
        resp = self.client.put(
            f"/v2/sbn/{sbn_root.link}", json={"locked": True}, headers=self._lock_auth(self.ALICE)
        )
        self.assertEqual(resp.status_code, 422, resp.text)


# ---------------------------------------------------------------------------
# Lock/unlock AUTHORIZATION — plan_ac_enforcement_closeout_2026-09-09.md §3.
# `TestLockDoor`/`TestUnlockDoor`/`TestNavTabLockingDoors` above (and
# `TestSbnLocking`, by its own docstring's identical split) are MECHANISM
# ONLY — every editor there is a Hub Admin specifically to isolate lock
# staleness/propagation/identity from this gate. This class and the one
# below it are the counterpart: a properly rooted hub -> nav_tab -> tab ->
# gridstack tree with REAL per-user resource_grants, so an ordinary
# (non-admin) editor actually exercises `require_edit`/
# `resolve_gridstack_node` — never the Hub Admin bypass, which would prove
# nothing about §3.3's trap. Getting the resolver choice wrong fails CLOSED,
# so only a legitimate non-admin editor being refused (or correctly let
# through) can catch it — a permissive test cannot.
# ---------------------------------------------------------------------------


class TestLockAuthorization(TabLockingFixture):
    """hub -> nav1 -> root1 -> gridstack -> sub_a (nested; represented by
    its own component row, per §3.2 — gridstacks hold no grants).

      editor        : edit on root1 (the TAB)          — folds down to sub_a
      narrow_editor : edit on sub_a's OWN REPRESENTATION COMPONENT — the
                      §3.3 trap case, see its own tests below
      viewer        : VIEW-only grant on root1          — sees, cannot edit
      outsider      : no grant anywhere                 — cannot even see
    """

    def setUp(self) -> None:
        super().setUp()
        now = datetime.now(timezone.utc)
        self.hub = HubV2(document_id="hub-1")
        self.db.add(self.hub)
        self.nav1 = NavTabV2(document_id="nav-1", slug="nav-1", title="Nav 1", protected=False)
        self.db.add(self.nav1)
        self.db.flush()

        root = create_tab_v2(self.db, title="Root 1", nav_tab_id=self.nav1.id)
        self.root_document_id = root["documentId"]
        self.root_gridstack = get_gridstack_by_document_id(self.db, self.root_document_id)
        self.root_tab_id = self.root_gridstack.parent_tab_id

        sub_a = create_tab_v2(self.db, title="Sub A", parent_document_id=self.root_document_id)
        self.sub_a_document_id = sub_a["documentId"]
        self.sub_a_gridstack = get_gridstack_by_document_id(self.db, self.sub_a_document_id)
        # The sub-grid's own AC node (§3.3) — its REPRESENTATION component,
        # never the gridstack row itself. Asserted in its own test below,
        # not just assumed here.
        self.sub_a_ac_node = resolve_gridstack_node(self.db, self.sub_a_gridstack)

        self.editor = UserV2(email="editor@renphil.org", has_signed_in=True, created_at=now)
        self.narrow_editor = UserV2(email="narrow-editor@renphil.org", has_signed_in=True, created_at=now)
        self.viewer = UserV2(email="viewer@renphil.org", has_signed_in=True, created_at=now)
        self.outsider_email = "outsider@renphil.org"
        self.db.add_all([self.editor, self.narrow_editor, self.viewer])
        self.db.flush()

        self._grant(self.editor, ("tab", self.root_tab_id), "edit")
        self._grant(self.narrow_editor, self.sub_a_ac_node, "edit")
        self._grant(self.viewer, ("tab", self.root_tab_id), "view")
        self.db.commit()

    def _grant(self, user: UserV2, node: tuple[str, int], level: str) -> None:
        node_kind, node_id = node
        self.db.add(
            ResourceGrantV2(
                **{NODE_COLUMNS[node_kind]: node_id},
                user_id=user.id,
                level=level,
                created_at=datetime.now(timezone.utc),
            )
        )

    def test_sub_a_resolves_to_its_representation_component_not_the_gridstack(self) -> None:
        """The premise every other test in this class depends on — pinned
        on its own so a future change to `resolve_gridstack_node` fails
        here first, with a clear name, rather than as a mystery 403/404 in
        one of the trap tests below."""
        self.assertEqual(self.sub_a_ac_node[0], "component")

    # -- root tab: editor / viewer / outsider / admin ------------------------

    def test_an_editor_may_lock_and_unlock_the_root(self) -> None:
        resp = self.client.put(
            f"/v2/tabs/{self.root_document_id}/lock", json={}, headers=self._auth(self.editor.email)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        resp = self.client.put(
            f"/v2/tabs/{self.root_document_id}/unlock", json={}, headers=self._auth(self.editor.email)
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_a_view_only_caller_may_not_lock_the_root(self) -> None:
        """Can SEE the node (a view grant) but not EDIT it — §6.3's row maps
        this to 403: the caller's own UI already confirms the node exists,
        so a 404 here would hide nothing and only confuse."""
        resp = self.client.put(
            f"/v2/tabs/{self.root_document_id}/lock", json={}, headers=self._auth(self.viewer.email)
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_a_view_only_caller_may_not_unlock_the_root(self) -> None:
        self.client.put(f"/v2/tabs/{self.root_document_id}/lock", json={}, headers=self._auth(self.editor.email))
        self.client.put(f"/v2/tabs/{self.root_document_id}/unlock", json={}, headers=self._auth(self.editor.email))
        resp = self.client.put(
            f"/v2/tabs/{self.root_document_id}/unlock", json={}, headers=self._auth(self.viewer.email)
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_a_view_only_caller_may_not_force_unlock_the_root(self) -> None:
        """§3.2: force-unlock collapses to the SAME single `edit(n)` check
        — holding `view` alone must not let `force: true` through either,
        and the lock must survive the refused attempt untouched."""
        self.client.put(f"/v2/tabs/{self.root_document_id}/lock", json={}, headers=self._auth(self.editor.email))
        resp = self.client.put(
            f"/v2/tabs/{self.root_document_id}/unlock",
            json={"force": True},
            headers=self._auth(self.viewer.email),
        )
        self.assertEqual(resp.status_code, 403, resp.text)
        check = self.client.get(
            f"/v2/tabs/{self.root_document_id}/workspace", headers=self._auth(self.editor.email)
        )
        self.assertTrue(check.json()["data"]["locked"])

    def test_an_outsider_with_no_grant_at_all_gets_a_404_not_a_403(self) -> None:
        """Invisible, not merely uneditable (§9's fail-closed convention):
        cannot even SEE the node, so the refusal must not confirm it
        exists at all."""
        resp = self.client.put(
            f"/v2/tabs/{self.root_document_id}/lock", json={}, headers=self._auth(self.outsider_email)
        )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_hub_admin_may_lock_with_no_grant_at_all(self) -> None:
        resp = self.client.put(
            f"/v2/tabs/{self.root_document_id}/lock", json={}, headers=self._admin_auth()
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    # -- §3.3's trap, exercised for real: a sub-grid's lock/unlock gates on
    # `resolve_gridstack_node` (a component), never `resolve_lock_node` (a
    # gridstack) — see gridstack_service.lock_tab_by_document_id_v2's own
    # comment on why using the wrong one here fails CLOSED.
    # -------------------------------------------------------------------

    def test_the_broad_editor_reaches_the_sub_grid_by_the_downward_fold(self) -> None:
        """The editor's grant sits on the ROOT TAB, not the sub-grid —
        succeeds only because the sub-grid's AC node folds down from a real
        ancestor node of its own kind (component), the ordinary way."""
        resp = self.client.put(
            f"/v2/tabs/{self.sub_a_document_id}/lock", json={}, headers=self._auth(self.editor.email)
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_the_narrow_editor_holding_only_the_sub_grids_own_component_grant_may_lock_it(
        self,
    ) -> None:
        """THE §3.3 TRAP, caught. `narrow_editor` holds `edit` on
        `("component", <sub_a's representation id>)` and NOTHING on root1
        — this can only pass if the lock door's AC check calls
        `resolve_gridstack_node` (which resolves a sub-grid to that exact
        component) rather than `resolve_lock_node` (which would resolve it
        to `("gridstack", id)`, a NodeRef this grant was never written
        against, and which is not even a valid AC node kind — §3.2). Using
        the lock resolver here evaluates an unknown/invisible ref and fails
        CLOSED, so a regression here shows up as THIS test wrongly refusing
        a legitimate editor — never as a permissive test passing when it
        should not."""
        resp = self.client.put(
            f"/v2/tabs/{self.sub_a_document_id}/lock",
            json={},
            headers=self._auth(self.narrow_editor.email),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_the_narrow_editor_may_not_lock_the_root_they_hold_nothing_on(self) -> None:
        """The mirror image — narrow_editor's only grant is on the
        sub-grid, and grants never fold UP (§5.1's fold is DOWN the root
        path only), so they may not lock root1 itself."""
        resp = self.client.put(
            f"/v2/tabs/{self.root_document_id}/lock",
            json={},
            headers=self._auth(self.narrow_editor.email),
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    # -- force is Hub Admin only (owner decision 2026-09-25) ----------------
    # `edit(n)` still gates the lock action itself; `force` is an extra,
    # admin-only check on top (`require_force_allowed`).

    def test_the_narrow_editor_may_not_force_unlock_the_sub_grid_they_hold_edit_on(self) -> None:
        """Holding `edit(n)` is no longer enough for `force` — replaces the
        closeout-§3.2 test that pinned the opposite. The lock must survive
        the refused attempt untouched."""
        self.client.put(f"/v2/tabs/{self.sub_a_document_id}/lock", json={}, headers=self._admin_auth())
        resp = self.client.put(
            f"/v2/tabs/{self.sub_a_document_id}/unlock",
            json={"force": True},
            headers=self._auth(self.narrow_editor.email),
        )
        self.assertEqual(resp.status_code, 403, resp.text)
        check = self.client.get(f"/v2/tabs/{self.sub_a_document_id}/workspace", headers=self._admin_auth())
        self.assertTrue(check.json()["data"]["locked"])
        self.assertEqual(check.json()["data"]["locked_by"], "admin@renphil.org")

    def test_an_editor_may_not_force_take_over_the_root(self) -> None:
        self.client.put(f"/v2/tabs/{self.root_document_id}/lock", json={}, headers=self._admin_auth())
        resp = self.client.put(
            f"/v2/tabs/{self.root_document_id}/lock",
            json={"force": True},
            headers=self._auth(self.editor.email),
        )
        self.assertEqual(resp.status_code, 403, resp.text)
        check = self.client.get(f"/v2/tabs/{self.root_document_id}/workspace", headers=self._admin_auth())
        self.assertEqual(check.json()["data"]["locked_by"], "admin@renphil.org")

    def test_an_editors_force_is_refused_even_when_nothing_is_held(self) -> None:
        """The flag itself is refused, not just a real takeover — a
        non-admin never needs it (a stale lock is reclaimed by a plain
        acquire), so there is no case where passing it is legitimate."""
        resp = self.client.put(
            f"/v2/tabs/{self.root_document_id}/lock",
            json={"force": True},
            headers=self._auth(self.editor.email),
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_an_outsiders_force_is_still_a_404(self) -> None:
        """`require_edit` runs first; an invisible caller must still get
        the 404 (checked separately above) and learn nothing from `force`."""
        resp = self.client.put(
            f"/v2/tabs/{self.root_document_id}/lock",
            json={"force": True},
            headers=self._auth(self.outsider_email),
        )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_hub_admin_may_force_take_over_an_editors_fresh_lock(self) -> None:
        self.client.put(f"/v2/tabs/{self.root_document_id}/lock", json={}, headers=self._auth(self.editor.email))
        resp = self.client.put(
            f"/v2/tabs/{self.root_document_id}/lock", json={"force": True}, headers=self._admin_auth()
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["data"]["locked_by"], "admin@renphil.org")


class TestComponentScopedLockAuthorization(TestLockAuthorization):
    """session_handoff_2026-09-16 follow-up (the `ComponentContentEditModal`
    403 report) — `link`, the alternate `edit(component)` path
    `_require_edit_for_lock_action` adds alongside the existing
    `edit(resolve_gridstack_node(...))` check. Distinct from
    `TestLockAuthorization`'s own `narrow_editor` above: that one holds
    `edit` on a SUB-GRID's own representation component (§3.3's trap);
    `widget_editor` here holds `edit` on an ORDINARY WIDGET living ON
    root1's canvas — the actual shape of a component-only edit grant
    `ComponentContentEditModal` exists for, which `resolve_gridstack_node`
    (root1 resolves to `("tab", ...)`) never reaches by itself, since edit
    only folds DOWN the tree.

    Extends `TestLockAuthorization`'s fixture (same hub/nav/root1/sub_a
    tree, same editor/viewer/outsider) rather than duplicating it, plus one
    real widget on root1, one widget on a wholly separate second root (for
    the cross-canvas guard), and one new narrow holder."""

    def setUp(self) -> None:
        super().setUp()
        now = datetime.now(timezone.utc)

        self.widget = ComponentV2(
            link="widget-a", title="Widget A", type="markdown", gridstack_id=self.root_gridstack.id
        )
        self.db.add(self.widget)

        other_root = create_tab_v2(self.db, title="Root 2")
        other_gridstack = get_gridstack_by_document_id(self.db, other_root["documentId"])
        self.other_widget = ComponentV2(
            link="widget-b", title="Widget B", type="markdown", gridstack_id=other_gridstack.id
        )
        self.db.add(self.other_widget)
        self.db.flush()

        self.widget_editor = UserV2(email="widget-editor@renphil.org", has_signed_in=True, created_at=now)
        self.db.add(self.widget_editor)
        self.db.flush()
        self._grant(self.widget_editor, ("component", self.widget.id), "edit")
        self.db.commit()

    def test_widget_only_editor_holds_no_edit_on_the_root_itself(self) -> None:
        """Premise check, like `TestLockAuthorization`'s own sub_a-resolves
        test — `edit(component)` does not fold UP to give `edit` on root1
        (the fold is DOWN only). 403, not 404: §5.2's ancestor-reveal
        mechanism means holding a grant on a descendant DOES make root1
        `view`-able (so its title/existence can be shown in chrome/
        breadcrumbs), just not editable — the same reason
        `test_a_view_only_caller_may_not_lock_the_root` above gets 403.
        A lock attempt with no `link` therefore reaches (and fails) the
        `edit` half of the check, not the `view` half."""
        resp = self.client.put(
            f"/v2/tabs/{self.root_document_id}/lock", json={}, headers=self._auth(self.widget_editor.email)
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_widget_only_editor_may_lock_the_root_by_naming_their_widgets_link(self) -> None:
        resp = self.client.put(
            f"/v2/tabs/{self.root_document_id}/lock",
            json={"link": self.widget.link},
            headers=self._auth(self.widget_editor.email),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_widget_only_editor_may_unlock_by_naming_their_widgets_link(self) -> None:
        self.client.put(
            f"/v2/tabs/{self.root_document_id}/lock",
            json={"link": self.widget.link},
            headers=self._auth(self.widget_editor.email),
        )
        resp = self.client.put(
            f"/v2/tabs/{self.root_document_id}/unlock",
            json={"link": self.widget.link},
            headers=self._auth(self.widget_editor.email),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_widget_only_editor_cannot_unlock_by_omitting_the_link_after_locking_with_it(self) -> None:
        """The exact bug this fix closes, pinned on the RELEASE side too —
        see `unlock_tab_by_document_id_v2`'s own docstring on why `link` is
        "not optional in practice" for this caller: omitting it here hits
        the same 403 as the premise test above, so a caller who successfully
        locked would be left unable to ever release it."""
        self.client.put(
            f"/v2/tabs/{self.root_document_id}/lock",
            json={"link": self.widget.link},
            headers=self._auth(self.widget_editor.email),
        )
        resp = self.client.put(
            f"/v2/tabs/{self.root_document_id}/unlock",
            json={},
            headers=self._auth(self.widget_editor.email),
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_a_link_the_caller_holds_no_edit_on_does_not_justify_locking(self) -> None:
        """`widget.link` genuinely belongs to root1's own canvas, but naming
        a component the CALLER holds no grant on must not work either — the
        fallback still runs `require_edit` against that exact component, not
        merely "does a component by this link exist here". `viewer` holds
        VIEW (not edit) on root1, which folds down to VIEW (not edit) on
        `widget` too, so this is genuinely the edit check failing, not a
        visibility one."""
        resp = self.client.put(
            f"/v2/tabs/{self.root_document_id}/lock",
            json={"link": self.widget.link},
            headers=self._auth(self.viewer.email),
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_a_link_belonging_to_a_different_canvas_does_not_justify_locking_this_one(self) -> None:
        """The cross-canvas guard: `component.gridstack_id == gridstack.id`
        in `_require_edit_for_lock_action`. `other_editor` can genuinely
        VIEW root1 (so this isn't merely the invisible-node 404 case above)
        and genuinely holds `edit` on `other_widget` — which lives on a
        WHOLLY SEPARATE root's canvas. Naming it while locking root1 must
        still be refused: without this check, a grant on ANY component
        anywhere in the hub would justify locking ANY canvas, turning a
        one-widget grant into hub-wide lock-griefing power."""
        other_editor = UserV2(
            email="other-editor@renphil.org", has_signed_in=True, created_at=datetime.now(timezone.utc)
        )
        self.db.add(other_editor)
        self.db.flush()
        self._grant(other_editor, ("tab", self.root_tab_id), "view")
        self._grant(other_editor, ("component", self.other_widget.id), "edit")
        self.db.commit()

        resp = self.client.put(
            f"/v2/tabs/{self.root_document_id}/lock",
            json={"link": self.other_widget.link},
            headers=self._auth(other_editor.email),
        )
        self.assertEqual(resp.status_code, 403, resp.text)


class TestNavTabLockAuthorization(TabLockingFixture):
    """The same §3 gate, on `("nav_tab", id)` directly — no resolver split
    to get wrong here (§3.3's trap is a TabV2/gridstack-only concern; a nav
    tab already IS its own AC node, per `lock_nav_tab_by_document_id_v2`'s
    own comment)."""

    def setUp(self) -> None:
        super().setUp()
        now = datetime.now(timezone.utc)
        self.hub = HubV2(document_id="hub-1")
        self.db.add(self.hub)
        self.nav1 = NavTabV2(document_id="nav-1", slug="nav-1", title="Nav 1", protected=False)
        self.db.add(self.nav1)
        self.db.flush()

        self.editor = UserV2(email="nav-editor@renphil.org", has_signed_in=True, created_at=now)
        self.viewer = UserV2(email="nav-viewer@renphil.org", has_signed_in=True, created_at=now)
        self.db.add_all([self.editor, self.viewer])
        self.db.flush()

        self.db.add(
            ResourceGrantV2(
                nav_tab_id=self.nav1.id, user_id=self.editor.id, level="edit", created_at=now
            )
        )
        self.db.add(
            ResourceGrantV2(
                nav_tab_id=self.nav1.id, user_id=self.viewer.id, level="view", created_at=now
            )
        )
        self.db.commit()

    def test_an_editor_may_lock_and_unlock_the_nav_tab(self) -> None:
        resp = self.client.put(
            f"/v2/nav-tabs/{self.nav1.document_id}/lock", json={}, headers=self._auth(self.editor.email)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        resp = self.client.put(
            f"/v2/nav-tabs/{self.nav1.document_id}/unlock", json={}, headers=self._auth(self.editor.email)
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_a_view_only_caller_may_not_lock_the_nav_tab(self) -> None:
        resp = self.client.put(
            f"/v2/nav-tabs/{self.nav1.document_id}/lock", json={}, headers=self._auth(self.viewer.email)
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_an_outsider_with_no_grant_gets_a_404(self) -> None:
        resp = self.client.put(
            f"/v2/nav-tabs/{self.nav1.document_id}/lock",
            json={},
            headers=self._auth("nav-outsider@renphil.org"),
        )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_a_view_only_caller_may_not_force_unlock_the_nav_tab(self) -> None:
        self.client.put(
            f"/v2/nav-tabs/{self.nav1.document_id}/lock", json={}, headers=self._auth(self.editor.email)
        )
        resp = self.client.put(
            f"/v2/nav-tabs/{self.nav1.document_id}/unlock",
            json={"force": True},
            headers=self._auth(self.viewer.email),
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_hub_admin_may_lock_the_nav_tab_with_no_grant(self) -> None:
        resp = self.client.put(
            f"/v2/nav-tabs/{self.nav1.document_id}/lock", json={}, headers=self._admin_auth()
        )
        self.assertEqual(resp.status_code, 200, resp.text)


# ---------------------------------------------------------------------------
# edit_lock_service tree resolution — plan_lock_propagation_2026-09-08.md §2,
# §8 phase 1. No behaviour change yet: nothing here is wired into
# lock_tab_by_document_id_v2 / unlock_tab_by_document_id_v2. Built on the
# plan's own §1.1 worked example: nav1 -> root1 -> {var1, sub-A},
# var1 -> {sub-B, sub-C}.
# ---------------------------------------------------------------------------


class TestLockTreeResolution(TabLockingFixture):
    def setUp(self) -> None:
        super().setUp()
        self.nav1 = self._nav_tab("nav1")
        self.root1 = create_tab_v2(self.db, title="root1", nav_tab_id=self.nav1["id"])
        # var1 is created BEFORE root1 gets any sub-grid of its own:
        # create_tab_variant_v2's "first variant adopts the root's existing
        # canvas" migration (_migrate_root_content_to_variant) would
        # otherwise reparent a pre-existing root sub-grid onto the variant
        # out from under it — confirmed live 2026-09-08 while building this
        # fixture (sub-A moved onto var1 when created in the other order).
        # Creating var1 first, while root1's canvas is still empty, means
        # there is nothing to migrate; sub-A (added after) is unambiguously
        # root1's own.
        self.var1 = self._variant(self.root1["documentId"], title="var1")
        self.sub_a = self._nested_subtab(self.root1["documentId"], title="sub-A")
        self.sub_b = self._nested_subtab(self.var1["documentId"], title="sub-B")
        self.sub_c = self._nested_subtab(self.var1["documentId"], title="sub-C")

        self.root1_tab_id = get_gridstack_by_document_id(self.db, self.root1["documentId"]).parent_tab_id
        self.var1_tab_id = get_gridstack_by_document_id(self.db, self.var1["documentId"]).parent_tab_id
        self.sub_a_id = get_gridstack_by_document_id(self.db, self.sub_a["documentId"]).id
        self.sub_b_id = get_gridstack_by_document_id(self.db, self.sub_b["documentId"]).id
        self.sub_c_id = get_gridstack_by_document_id(self.db, self.sub_c["documentId"]).id
        self.nav1_id = self.nav1["id"]

    # -- resolve_lock_node --------------------------------------------------

    def test_resolve_lock_node_of_a_root_gridstack_is_its_tab(self) -> None:
        gridstack = get_gridstack_by_document_id(self.db, self.root1["documentId"])
        self.assertEqual(resolve_lock_node(gridstack), ("tab", self.root1_tab_id))

    def test_resolve_lock_node_of_a_variants_own_gridstack_is_the_variant_tab_not_the_root(self) -> None:
        gridstack = get_gridstack_by_document_id(self.db, self.var1["documentId"])
        self.assertEqual(resolve_lock_node(gridstack), ("tab", self.var1_tab_id))
        self.assertNotEqual(self.var1_tab_id, self.root1_tab_id)

    def test_resolve_lock_node_of_a_nested_subgrid_is_the_gridstack_itself(self) -> None:
        gridstack = get_gridstack_by_document_id(self.db, self.sub_a["documentId"])
        self.assertEqual(resolve_lock_node(gridstack), ("gridstack", self.sub_a_id))

    # -- ancestors ------------------------------------------------------------

    def test_ancestors_of_nav_tab_are_empty(self) -> None:
        self.assertEqual(ancestors(self.db, ("nav_tab", self.nav1_id)), [])

    # FLIPPED 2026-09-30 (plan_node_session_gaps §4 item 1, option 2): a
    # chain stops at the tab level — nav-tab locks are structure-only, so
    # the nav tab is never an ancestor. The four tests below used to end
    # every chain in `("nav_tab", nav1)`.

    def test_ancestors_of_a_root_tab_are_empty_its_nav_tab_is_not_a_lock_ancestor(self) -> None:
        self.assertEqual(ancestors(self.db, ("tab", self.root1_tab_id)), [])

    def test_ancestors_of_a_variant_are_just_its_root(self) -> None:
        self.assertEqual(ancestors(self.db, ("tab", self.var1_tab_id)), [("tab", self.root1_tab_id)])

    def test_ancestors_of_a_sub_grid_under_the_root_are_just_the_root(self) -> None:
        self.assertEqual(ancestors(self.db, ("gridstack", self.sub_a_id)), [("tab", self.root1_tab_id)])

    def test_ancestors_of_a_sub_grid_under_a_variant_climb_through_the_variant(self) -> None:
        """The child->parent, no-siblings case from §1's worked table: sub-B's
        ancestor chain passes through var1 (not root1 directly), and stops
        at root1."""
        self.assertEqual(
            ancestors(self.db, ("gridstack", self.sub_b_id)),
            [("tab", self.var1_tab_id), ("tab", self.root1_tab_id)],
        )

    # -- descendants ------------------------------------------------------------

    def test_descendants_of_a_sub_grid_are_its_real_components(self) -> None:
        """REVERSED 2026-09-17 (plan_component_locking_and_sbn §4, decision
        A) — this was `test_descendants_of_a_sub_grid_are_empty_a_sub_grid_
        is_always_a_leaf`, pinning "a gridstack is always a leaf". A
        gridstack is no longer a leaf: its descendants are the real
        components on it. A sub-grid with no widgets is still empty (its
        representation row is not a node); one with a widget lists it."""
        self.assertEqual(descendants(self.db, ("gridstack", self.sub_a_id)), [])
        widget = ComponentV2(link="w-on-sub-b", type="text", gridstack_id=self.sub_b_id)
        self.db.add(widget)
        self.db.commit()
        self.assertEqual(descendants(self.db, ("gridstack", self.sub_b_id)), [("component", widget.id)])
        # The gridstack's representation row (current_grid_id set) is never
        # a node — sub-A still has none even though such a row exists.
        self.assertIsNotNone(
            self.db.query(ComponentV2).filter(ComponentV2.current_grid_id == self.sub_a_id).first()
        )

    def test_descendants_of_a_variant_are_just_its_own_sub_grids(self) -> None:
        self.assertCountEqual(
            descendants(self.db, ("tab", self.var1_tab_id)),
            [("gridstack", self.sub_b_id), ("gridstack", self.sub_c_id)],
        )

    def test_descendants_of_the_root_include_the_variant_AND_the_variants_own_sub_grids(self) -> None:
        """THE §1.1 HOLE: sub-B and sub-C belong to var1, not root1, yet they
        are still root1's descendants — locking root1 must be able to see
        them. This is exactly what today's `_cascade_lock_to_nested_gridstacks`
        (parent_tab_id == tab.id only) misses."""
        self.assertCountEqual(
            descendants(self.db, ("tab", self.root1_tab_id)),
            [
                ("tab", self.var1_tab_id),
                ("gridstack", self.sub_a_id),
                ("gridstack", self.sub_b_id),
                ("gridstack", self.sub_c_id),
            ],
        )

    def test_descendants_of_the_nav_tab_are_empty_its_contents_are_not_lock_descendants(self) -> None:
        """FLIPPED 2026-09-30 (plan_node_session_gaps §4 item 2): was
        `…include_every_root_variant_and_sub_grid_beneath_it`. A nav tab's
        lock covers only itself; what sits inside it moved, unchanged, to
        `_nav_tab_contents` (pinned in the next test)."""
        self.assertEqual(descendants(self.db, ("nav_tab", self.nav1_id)), [])

    def test_nav_tab_contents_are_every_root_variant_and_sub_grid_beneath_it(self) -> None:
        """What `descendants(nav_tab)` returned before option 2 — kept
        exactly, for `refuse_if_subtree_held`'s nav-tab-delete check."""
        self.assertCountEqual(
            _nav_tab_contents(self.db, self.nav1_id),
            [
                ("tab", self.root1_tab_id),
                ("tab", self.var1_tab_id),
                ("gridstack", self.sub_a_id),
                ("gridstack", self.sub_b_id),
                ("gridstack", self.sub_c_id),
            ],
        )

    def test_a_second_independent_nav_tab_tree_does_not_leak_into_the_first(self) -> None:
        nav2 = self._nav_tab("nav2")
        root2 = create_tab_v2(self.db, title="root2", nav_tab_id=nav2["id"])
        root2_tab_id = get_gridstack_by_document_id(self.db, root2["documentId"]).parent_tab_id

        self.assertNotIn(("tab", root2_tab_id), _nav_tab_contents(self.db, self.nav1_id))
        self.assertNotIn(("tab", self.root1_tab_id), _nav_tab_contents(self.db, nav2["id"]))
        self.assertEqual(ancestors(self.db, ("tab", root2_tab_id)), [])


# ---------------------------------------------------------------------------
# Components as lock nodes — plan_component_locking_and_sbn_2026-09-17.md
# §2/§4, decision A. The fourth kind: every REAL component (current_grid_id
# IS NULL) — ordinary widget, SBN root, SBN sub-tab — is a node; a sub-grid's
# representation row is not.
#
# Fixture: nav1 -> root1 -> {var1, sub-A}; root1's own canvas holds widgets
# W1, W2 and SBN S with sub-tabs s1 -> {s1a}, s2 (plan §2.1's worked
# example); sub-A's canvas holds widget W_SUB. var1 is created BEFORE any of
# root1's widgets for the same first-variant-adopts-the-canvas reason
# TestLockTreeResolution.setUp documents.
# ---------------------------------------------------------------------------


class ComponentLockTreeFixture(TabLockingFixture):
    def setUp(self) -> None:
        super().setUp()
        self.nav1 = self._nav_tab("nav1")
        self.root1 = create_tab_v2(self.db, title="root1", nav_tab_id=self.nav1["id"])
        self.var1 = self._variant(self.root1["documentId"], title="var1")
        self.sub_a = self._nested_subtab(self.root1["documentId"], title="sub-A")

        root_gs = get_gridstack_by_document_id(self.db, self.root1["documentId"])
        sub_a_gs = get_gridstack_by_document_id(self.db, self.sub_a["documentId"])
        self.root1_tab_id = root_gs.parent_tab_id
        self.root_gs_id = root_gs.id
        self.sub_a_id = sub_a_gs.id
        self.nav1_id = self.nav1["id"]

        def widget(link: str, gridstack_id: int, *, title: str | None, sbn_parent: int | None = None,
                   ctype: str = "text") -> ComponentV2:
            row = ComponentV2(
                link=link, title=title, type=ctype, gridstack_id=gridstack_id, super_blocknote_id=sbn_parent
            )
            self.db.add(row)
            self.db.flush()
            return row

        self.w1 = widget("w1", root_gs.id, title="Quarterly KPIs")
        self.w2 = widget("w2", root_gs.id, title=None)  # untitled — _label_for's fallback case
        self.s = widget("s", root_gs.id, title="Notes", ctype=SBN_ROOT_TYPE)
        self.s1 = widget("s1", root_gs.id, title="s1", sbn_parent=self.s.id, ctype="block_note")
        self.s1a = widget("s1a", root_gs.id, title="s1a", sbn_parent=self.s1.id, ctype="block_note")
        self.s2 = widget("s2", root_gs.id, title="s2", sbn_parent=self.s.id, ctype="block_note")
        self.w_sub = widget("w-sub", sub_a_gs.id, title="On sub-A")
        self.db.commit()

        self.NAV1: LockNode = ("nav_tab", self.nav1_id)
        self.ROOT1: LockNode = ("tab", self.root1_tab_id)
        self.VAR1: LockNode = ("tab", get_gridstack_by_document_id(self.db, self.var1["documentId"]).parent_tab_id)
        self.SUB_A: LockNode = ("gridstack", self.sub_a_id)
        self.W1: LockNode = ("component", self.w1.id)
        self.W2: LockNode = ("component", self.w2.id)
        self.S: LockNode = ("component", self.s.id)
        self.S1: LockNode = ("component", self.s1.id)
        self.S1A: LockNode = ("component", self.s1a.id)
        self.S2: LockNode = ("component", self.s2.id)
        self.W_SUB: LockNode = ("component", self.w_sub.id)
        self.ALL_ROOT_CANVAS_COMPONENTS = [self.W1, self.W2, self.S, self.S1, self.S1A, self.S2]

    def _representation_rows(self) -> set[LockNode]:
        rows = self.db.query(ComponentV2.id).filter(ComponentV2.current_grid_id.isnot(None)).all()
        self.assertGreater(len(rows), 0)  # the fixture does create them
        return {("component", r.id) for r in rows}


class TestComponentLockTreeResolution(ComponentLockTreeFixture):
    # -- ancestors ------------------------------------------------------------

    # FLIPPED 2026-09-30 (plan_node_session_gaps §4 item 1): every chain
    # below used to end in NAV1; it now stops at the tab level.

    def test_ancestors_of_a_widget_on_a_root_canvas_are_just_its_tab(self) -> None:
        """A root canvas has no lock node of its own — its TabV2 row is the
        node — so the first hop up from a widget on it is straight to the
        tab, exactly as `resolve_lock_node` says; the nav tab is not a lock
        ancestor."""
        self.assertEqual(ancestors(self.db, self.W1), [self.ROOT1])

    def test_ancestors_of_a_widget_on_a_sub_grid_climb_through_the_sub_grid(self) -> None:
        self.assertEqual(ancestors(self.db, self.W_SUB), [self.SUB_A, self.ROOT1])

    def test_ancestors_of_an_sbn_sub_tab_walk_the_sbn_chain_then_the_canvas_chain(self) -> None:
        """The one unbounded part of the tree: s1a -> s1 -> S (the
        `super_blocknote_id` chain), then the canvas's own fixed chain."""
        self.assertEqual(ancestors(self.db, self.S1A), [self.S1, self.S, self.ROOT1])
        self.assertEqual(ancestors(self.db, self.S1), [self.S, self.ROOT1])
        self.assertEqual(ancestors(self.db, self.S), [self.ROOT1])

    def test_a_dangling_sbn_parent_terminates_the_chain_fail_closed(self) -> None:
        # The fixture enables FK enforcement; a dangling reference is the
        # exact corrupt shape this test needs, so switch it off for the one
        # write that creates it.
        self.db.execute(sa_text("PRAGMA foreign_keys=OFF"))
        self.db.execute(ComponentV2.__table__.update().where(ComponentV2.id == self.s1a.id).values(super_blocknote_id=999_999))
        self.db.commit()
        self.db.execute(sa_text("PRAGMA foreign_keys=ON"))
        # Nothing above the dangling hop exists to be held — the chain ends.
        self.assertEqual(ancestors(self.db, self.S1A), [("component", 999_999)])

    def test_an_sbn_cycle_terminates_rather_than_walking_forever(self) -> None:
        """Same corrupt shape `test_access_visibility.test_an_sbn_cycle_
        terminates_and_fails_closed` guards on the AC tree: s1 -> s1a -> s1."""
        self.db.execute(ComponentV2.__table__.update().where(ComponentV2.id == self.s1.id).values(super_blocknote_id=self.s1a.id))
        self.db.commit()
        chain = ancestors(self.db, self.S1A)
        self.assertEqual(chain[0], self.S1)
        self.assertNotIn(self.S1A, chain)
        subtree = descendants(self.db, self.S1)
        self.assertIn(self.S1A, subtree)
        self.assertNotIn(self.S1, subtree)

    # -- descendants ------------------------------------------------------------

    def test_descendants_of_an_ordinary_widget_are_empty(self) -> None:
        self.assertEqual(descendants(self.db, self.W1), [])

    def test_descendants_of_an_sbn_root_are_its_whole_subtree(self) -> None:
        self.assertCountEqual(descendants(self.db, self.S), [self.S1, self.S1A, self.S2])
        self.assertCountEqual(descendants(self.db, self.S1), [self.S1A])
        self.assertEqual(descendants(self.db, self.S2), [])

    def test_descendants_of_a_sub_grid_are_the_widgets_on_it(self) -> None:
        self.assertEqual(descendants(self.db, self.SUB_A), [self.W_SUB])

    def test_descendants_of_the_root_tab_include_the_widgets_on_its_own_root_canvas(self) -> None:
        """THE TRAP the plan calls out: today's sub-grid query keeps only
        `parent_id IS NOT NULL` because only sub-grids were lock nodes;
        copying that filter into the component query drops every widget on
        a root/variant canvas. root1's own canvas widgets — W1, W2, S and
        S's whole subtree — are its descendants, alongside sub-A and its
        widget and the variant."""
        self.assertCountEqual(
            descendants(self.db, self.ROOT1),
            [self.VAR1, self.SUB_A, self.W_SUB] + self.ALL_ROOT_CANVAS_COMPONENTS,
        )

    def test_the_nav_tab_has_no_descendants_but_its_contents_reach_every_component(self) -> None:
        """FLIPPED 2026-09-30 (was `test_descendants_of_the_nav_tab_reach_
        every_component_beneath_it`): `descendants(nav_tab)` is empty
        (structure-only); the old set moved, unchanged, to
        `_nav_tab_contents` — THE TRAP the kickoff names: nav-tab delete's
        check must still see every widget and SBN node."""
        self.assertEqual(descendants(self.db, self.NAV1), [])
        self.assertCountEqual(
            _nav_tab_contents(self.db, self.nav1_id),
            [self.ROOT1, self.VAR1, self.SUB_A, self.W_SUB] + self.ALL_ROOT_CANVAS_COMPONENTS,
        )

    def test_representation_rows_never_appear_in_any_descendant_set(self) -> None:
        reps = self._representation_rows()
        for node in (self.ROOT1, self.VAR1, self.SUB_A):
            self.assertFalse(reps & set(descendants(self.db, node)), f"representation row leaked under {node}")
        self.assertFalse(reps & set(_nav_tab_contents(self.db, self.nav1_id)), "representation row in nav contents")

    # -- lock_node_of ------------------------------------------------------------

    def test_lock_node_of_a_real_component_is_itself(self) -> None:
        """THE PIVOT (plan §4): every `_require_live_session(db, session,
        ("component", …))` call site — `update_component_content`,
        `update_airtable_component_config`, and phase B's SBN writes — is
        rerouted onto the component's own row by this one branch."""
        for node in self.ALL_ROOT_CANVAS_COMPONENTS + [self.W_SUB]:
            self.assertEqual(lock_node_of(self.db, node), node)

    def test_lock_node_of_a_sub_grids_representation_row_is_the_gridstack_not_the_row(self) -> None:
        rep = self.db.query(ComponentV2).filter(ComponentV2.current_grid_id == self.sub_a_id).first()
        self.assertEqual(lock_node_of(self.db, ("component", rep.id)), self.SUB_A)

    def test_lock_node_of_a_root_canvas_representation_row_is_the_tab(self) -> None:
        rep = self.db.query(ComponentV2).filter(ComponentV2.current_grid_id == self.root_gs_id).first()
        self.assertEqual(lock_node_of(self.db, ("component", rep.id)), self.ROOT1)

    def test_lock_node_of_a_dangling_component_is_none(self) -> None:
        self.assertIsNone(lock_node_of(self.db, ("component", 999_999)))


class TestComponentPropagationConflicts(ComponentLockTreeFixture):
    """plan_component_locking §2.1's worked table, cell by cell, against the
    service directly."""

    def _assert_open(self, node: LockNode, holder: str) -> None:
        acquire(self.db, node, holder)
        release(self.db, node, holder)

    def _assert_refused(self, node: LockNode, holder: str) -> LockConflictError:
        with self.assertRaises(LockConflictError) as ctx:
            acquire(self.db, node, holder)
        return ctx.exception

    # -- row 1: Alice holds W1 (the per-widget modal) --------------------------

    def test_holding_one_widget_leaves_its_siblings_and_the_sbn_open_but_blocks_its_chain(self) -> None:
        """Row 1, and the one behaviour that FLIPS: lock-propagation §4
        decision 2's mutual exclusion between two narrow editors on one
        canvas is retired — W2 is a sibling and satisfies both halves of
        §1's rule."""
        # NAV1 moved from refused to open 2026-09-30 in this test and the
        # next two: nav-tab locks are structure-only (plan_node_session_gaps
        # §4), so a chain below never blocks the nav tab.
        acquire(self.db, self.W1, self.ALICE)
        for node in (self.W2, self.S, self.S1, self.S2, self.S1A, self.SUB_A, self.W_SUB, self.NAV1):
            self._assert_open(node, self.BOB)
        for node in (self.ROOT1, self.W1):
            self._assert_refused(node, self.BOB)

    # -- row 2: Alice holds s1 ----------------------------------------------------

    def test_holding_an_sbn_sub_tab_blocks_its_own_subtree_and_chain_not_its_siblings(self) -> None:
        acquire(self.db, self.S1, self.ALICE)
        for node in (self.W1, self.W2, self.S2, self.SUB_A, self.NAV1):
            self._assert_open(node, self.BOB)
        for node in (self.S1A, self.S, self.ROOT1, self.S1):
            self._assert_refused(node, self.BOB)

    # -- row 3: Alice holds S (the SBN root) --------------------------------------

    def test_holding_the_sbn_root_blocks_every_sub_tab_and_the_chain_not_other_widgets(self) -> None:
        acquire(self.db, self.S, self.ALICE)
        for node in (self.W1, self.W2, self.SUB_A, self.NAV1):
            self._assert_open(node, self.BOB)
        for node in (self.S1, self.S2, self.S1A, self.ROOT1, self.S):
            self._assert_refused(node, self.BOB)

    # -- row 4: Alice holds root1 (canvas edit mode) ------------------------------

    def test_holding_the_canvas_blocks_every_widget_and_sbn_node_on_it(self) -> None:
        """Row 4, what changes for the admin: canvas edit mode now covers
        W1/W2/S and the whole SBN subtree, so a narrow editor can no longer
        open a widget modal underneath a live canvas session."""
        acquire(self.db, self.ROOT1, self.ALICE)
        for node in self.ALL_ROOT_CANVAS_COMPONENTS + [self.SUB_A, self.W_SUB, self.VAR1]:
            self._assert_refused(node, self.BOB)
        # 2026-09-30: the nav tab above is open (structure-only nav-tab
        # locks, plan_node_session_gaps §4) — it used to be refused here.
        self._assert_open(self.NAV1, self.BOB)

    def test_a_held_widget_refuses_the_canvas_naming_the_widget_and_force_breaks_it(self) -> None:
        """Row 4 from the other side: an admin entering canvas edit mode
        while a widget is held is refused with §6.4's own phrasing, naming
        the widget; `force` takes the subtree over and rotates the ousted
        holder's token."""
        alice = acquire(self.db, self.W1, self.ALICE)
        err = self._assert_refused(self.ROOT1, self.BOB)
        self.assertEqual(
            str(err), f'An item inside this, "Quarterly KPIs", is being edited by {self.ALICE}.'
        )
        self.assertEqual([(b.holder, b.node_label, b.relation) for b in err.blocking],
                         [(self.ALICE, "Quarterly KPIs", "descendant")])

        acquire(self.db, self.ROOT1, self.BOB, force=True)
        self.db.expire_all()
        w1 = self.db.query(ComponentV2).filter(ComponentV2.id == self.w1.id).first()
        self.assertFalse(w1.locked)
        self.assertEqual(w1.locked_by, self.ALICE)  # kept — TAKEN_OVER, not MISSING
        self.assertNotEqual(w1.lock_token, alice.token)
        self.assertIsNotNone(w1.lock_token)

    def test_a_stale_widget_hold_never_refuses_the_canvas(self) -> None:
        acquire(self.db, self.W1, self.ALICE)
        w1 = self.db.query(ComponentV2).filter(ComponentV2.id == self.w1.id).first()
        w1.locked_at = datetime.now(timezone.utc) - timedelta(seconds=TTL_SECONDS + 60)
        self.db.commit()
        acquire(self.db, self.ROOT1, self.BOB)  # must not raise
        self.db.expire_all()
        # Housekeeping (§4.1 step 4): the stale foreign row was cleared.
        w1 = self.db.query(ComponentV2).filter(ComponentV2.id == self.w1.id).first()
        self.assertFalse(w1.locked)
        self.assertEqual(w1.locked_by, "")

    def test_a_refused_acquire_of_an_untitled_widget_names_its_type_never_an_empty_string(self) -> None:
        acquire(self.db, self.W2, self.ALICE)  # W2 has no title
        err = self._assert_refused(self.ROOT1, self.BOB)
        self.assertEqual(err.blocking[0].node_label, "text")
        self.assertEqual(str(err), f'An item inside this, "text", is being edited by {self.ALICE}.')

    def test_the_same_holder_never_conflicts_with_themself_at_the_component_level(self) -> None:
        acquire(self.db, self.ROOT1, self.ALICE)
        for node in (self.W1, self.S, self.S1A):
            acquire(self.db, node, self.ALICE)  # covered by her own canvas session — never refused

    def test_two_sub_grid_widgets_and_a_root_widget_are_all_mutually_open(self) -> None:
        acquire(self.db, self.W_SUB, self.ALICE)
        self._assert_open(self.W1, self.BOB)
        self._assert_refused(self.SUB_A, self.BOB)
        self._assert_refused(self.ROOT1, self.BOB)

    def test_a_representation_row_is_never_a_lock_node_even_if_someone_writes_locked_on_it(self) -> None:
        """Belt-and-braces on `_held_rows`: a hand-edited representation row
        claiming `locked=True` must not read as a live session anywhere."""
        rep = self.db.query(ComponentV2).filter(ComponentV2.current_grid_id == self.sub_a_id).first()
        rep.locked = True
        rep.locked_by = self.ALICE
        rep.locked_at = datetime.now(timezone.utc)
        rep.lock_token = "hand-edited"
        self.db.commit()
        # The representation row's own ancestor chain DOES pass through
        # sub-A (its gridstack_id is sub-A), so if `_held_rows` let it
        # through as an active lock, sub-A would read as
        # blocked_by_descendant here. It must read as free.
        self.assertEqual(resolve_lock_view(self.db, self.BOB).state_for(self.SUB_A).state, "free")
        self.assertEqual(resolve_lock_view(self.db, self.BOB).state_for(self.ROOT1).state, "free")
        acquire(self.db, self.SUB_A, self.BOB)  # not refused either


class TestComponentLockView(ComponentLockTreeFixture):
    def test_a_held_widget_reads_as_blocked_by_descendant_on_its_tab_but_not_its_nav_tab(self) -> None:
        """FLIPPED 2026-09-30 (was `…on_its_tab_and_nav_tab`): the nav-tab
        half now reads free — a nav tab never reports
        `blocked_by_descendant` (plan_node_session_gaps §4, decision 9)."""
        acquire(self.db, self.W1, self.ALICE)
        view = resolve_lock_view(self.db, self.BOB)
        state = view.state_for(self.ROOT1)
        self.assertEqual(state.state, "blocked_by_descendant")
        self.assertEqual(state.holder, self.ALICE)
        self.assertEqual(state.node_label, "Quarterly KPIs")
        self.assertEqual(view.state_for(self.NAV1).state, "free")
        self.assertEqual(view.state_for(self.W2).state, "free")
        self.assertEqual(view.state_for(self.W1).state, "locked_here")

    def test_a_held_canvas_reads_as_locked_by_ancestor_on_every_widget_and_sbn_node(self) -> None:
        acquire(self.db, self.ROOT1, self.ALICE)
        view = resolve_lock_view(self.db, self.BOB)
        for node in self.ALL_ROOT_CANVAS_COMPONENTS + [self.W_SUB]:
            state = view.state_for(node)
            self.assertEqual(state.state, "locked_by_ancestor", node)
            self.assertEqual(state.node_label, "root1")

    def test_a_held_sbn_sub_tab_reads_as_locked_by_ancestor_below_and_blocked_above(self) -> None:
        acquire(self.db, self.S1, self.ALICE)
        view = resolve_lock_view(self.db, self.BOB)
        self.assertEqual(view.state_for(self.S1A).state, "locked_by_ancestor")
        self.assertEqual(view.state_for(self.S).state, "blocked_by_descendant")
        self.assertEqual(view.state_for(self.S2).state, "free")
        self.assertEqual(resolve_lock_view(self.db, self.ALICE).state_for(self.S).state, "self")


class TestComponentSessionsCoverWrites(ComponentLockTreeFixture):
    """`require_live_session` on a component node: its own token passes; an
    ancestor's token (the canvas, an SBN parent) passes; a sibling's does
    not, and neither does the nav tab's (structure-only since 2026-09-30,
    plan_node_session_gaps §4)."""

    def _session(self, *grants) -> EditSession:
        return EditSession(holder=self.ALICE, tokens=frozenset(g.token for g in grants))

    def test_a_widgets_own_token_covers_its_own_write(self) -> None:
        grant = acquire(self.db, self.W1, self.ALICE)
        self.assertIsNotNone(require_live_session(self._session(grant), self.W1, self.db))

    def test_a_canvas_token_covers_a_widget_write_one_hop_down(self) -> None:
        """What keeps phase A safe alone (plan §11): the modal still holds
        the canvas, and `lock_node_of` now routes the write to the widget —
        the ancestor hop is what makes that still pass."""
        grant = acquire(self.db, self.ROOT1, self.ALICE)
        self.assertIsNotNone(require_live_session(self._session(grant), self.W1, self.db))
        self.assertIsNotNone(require_live_session(self._session(grant), self.S1A, self.db))

    def test_an_sbn_root_token_covers_every_sub_tab_write(self) -> None:
        grant = acquire(self.db, self.S, self.ALICE)
        for node in (self.S1, self.S2, self.S1A):
            self.assertIsNotNone(require_live_session(self._session(grant), node, self.db))

    def test_a_nav_tab_token_does_not_cover_a_widget_write(self) -> None:
        """New 2026-09-30 (plan_node_session_gaps §4, O2): a nav-tab session
        never makes a canvas editable, at any depth."""
        grant = acquire(self.db, self.NAV1, self.ALICE)
        for node in (self.W1, self.S1A, self.W_SUB):
            with self.assertRaises(EditSessionError) as ctx:
                require_live_session(self._session(grant), node, self.db)
            self.assertEqual(ctx.exception.code, "EDIT_SESSION_MISSING", node)

    def test_a_sibling_widgets_token_does_not_cover_this_widget(self) -> None:
        grant = acquire(self.db, self.W2, self.ALICE)
        with self.assertRaises(EditSessionError) as ctx:
            require_live_session(self._session(grant), self.W1, self.db)
        self.assertEqual(ctx.exception.code, "EDIT_SESSION_MISSING")

    def test_an_expired_widget_session_is_refused_as_expired(self) -> None:
        grant = acquire(self.db, self.W1, self.ALICE)
        w1 = self.db.query(ComponentV2).filter(ComponentV2.id == self.w1.id).first()
        w1.locked_at = datetime.now(timezone.utc) - timedelta(seconds=TTL_SECONDS + 60)
        self.db.commit()
        with self.assertRaises(EditSessionError) as ctx:
            require_live_session(self._session(grant), self.W1, self.db)
        self.assertEqual(ctx.exception.code, "EDIT_SESSION_EXPIRED")

    def test_renew_validates_a_component_session_on_its_own_row_only(self) -> None:
        canvas = acquire(self.db, self.ROOT1, self.ALICE)
        renewed = renew(self.db, self._session(canvas), self.ROOT1)
        self.assertEqual(renewed.token, canvas.token)
        # A canvas session does NOT vouch for a widget session that was
        # never taken (self_only) — same rule as for sub-grids.
        with self.assertRaises(EditSessionError):
            renew(self.db, self._session(canvas), self.W1)

    def test_the_component_content_door_passes_on_the_canvas_token_via_the_ancestor_hop(self) -> None:
        """Through the real HTTP door this time, so phase A's "safe alone"
        claim is pinned end to end: a modal that still locks the canvas
        can write its widget."""
        resp = self.client.put(
            f"/v2/tabs/{self.root1['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        token = resp.json()["data"]["lock_token"]
        headers = {**self._auth(self.ALICE, roles=["Hub Admin"]), "X-Edit-Tokens": token}
        resp = self.client.put("/v2/tabs/components/by-link/w1/content", json={"title": "Renamed"}, headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)


# ---------------------------------------------------------------------------
# edit_lock_service.acquire — propagation conflicts, plan §1.1's worked
# table, cell by cell, called directly against the service (not through the
# router — nav_tab acquire has no HTTP door until §8 phase 5, and the
# service call is more direct for exhaustively checking every relation).
#
# Fixture, richer than TestLockTreeResolution's: nav1 -> {root1, root1b},
# root1 -> {var1, var1b, sub-A}, var1 -> {sub-B, sub-C}; nav2 -> root2 as a
# wholly separate tree. var1b is created AFTER var1 so it starts blank
# (create_tab_variant_v2's first-variant-adopts-the-canvas migration only
# ever fires once — see TestLockTreeResolution.setUp's own comment).
# ---------------------------------------------------------------------------


class TestPropagationConflicts(TabLockingFixture):
    def setUp(self) -> None:
        super().setUp()
        self.nav1 = self._nav_tab("nav1")
        self.root1 = create_tab_v2(self.db, title="root1", nav_tab_id=self.nav1["id"])
        self.var1 = self._variant(self.root1["documentId"], title="var1")
        self.var1b = self._variant(self.root1["documentId"], title="var1b")
        self.sub_a = self._nested_subtab(self.root1["documentId"], title="sub-A")
        self.sub_b = self._nested_subtab(self.var1["documentId"], title="sub-B")
        self.sub_c = self._nested_subtab(self.var1["documentId"], title="sub-C")
        self.root1b = create_tab_v2(self.db, title="root1b", nav_tab_id=self.nav1["id"])

        self.nav2 = self._nav_tab("nav2")
        self.root2 = create_tab_v2(self.db, title="root2", nav_tab_id=self.nav2["id"])

        def tab_id_of(resp: dict) -> int:
            return get_gridstack_by_document_id(self.db, resp["documentId"]).parent_tab_id

        def gs_id_of(resp: dict) -> int:
            return get_gridstack_by_document_id(self.db, resp["documentId"]).id

        self.NAV1: LockNode = ("nav_tab", self.nav1["id"])
        self.ROOT1: LockNode = ("tab", tab_id_of(self.root1))
        self.ROOT1B: LockNode = ("tab", tab_id_of(self.root1b))
        self.VAR1: LockNode = ("tab", tab_id_of(self.var1))
        self.VAR1B: LockNode = ("tab", tab_id_of(self.var1b))
        self.SUB_A: LockNode = ("gridstack", gs_id_of(self.sub_a))
        self.SUB_B: LockNode = ("gridstack", gs_id_of(self.sub_b))
        self.SUB_C: LockNode = ("gridstack", gs_id_of(self.sub_c))
        self.NAV2: LockNode = ("nav_tab", self.nav2["id"])
        self.ROOT2: LockNode = ("tab", tab_id_of(self.root2))

    # -- helpers --------------------------------------------------------

    def _assert_open(self, node: LockNode, holder: str) -> None:
        acquire(self.db, node, holder)  # raises on refusal — the assertion IS not raising
        release(self.db, node, holder)  # give the node back so later assertions in the
        # same test aren't polluted by this probe's own lock.

    def _assert_refused(self, node: LockNode, holder: str) -> None:
        with self.assertRaises(LockConflictError):
            acquire(self.db, node, holder)

    # -- §1.1 row 1: Alice holds sub-B -----------------------------------

    def test_holding_a_leaf_blocks_only_its_own_ancestor_chain(self) -> None:
        # NAV1 moved from refused to open 2026-09-30 in rows 1-3: nav-tab
        # locks are structure-only (plan_node_session_gaps §4), so the chain
        # a hold blocks stops at the tab level.
        acquire(self.db, self.SUB_B, self.ALICE)

        for node in (self.SUB_A, self.SUB_C, self.ROOT1B, self.ROOT2, self.NAV1):
            self._assert_open(node, self.BOB)

        for node in (self.VAR1, self.ROOT1, self.SUB_B):
            self._assert_refused(node, self.BOB)

    # -- §1.1 row 2: Alice holds var1 -------------------------------------

    def test_holding_a_variant_blocks_its_own_subtree_and_ancestors_not_siblings(self) -> None:
        acquire(self.db, self.VAR1, self.ALICE)

        for node in (self.SUB_A, self.VAR1B, self.ROOT1B, self.ROOT2, self.NAV1):
            self._assert_open(node, self.BOB)

        for node in (self.SUB_B, self.SUB_C, self.ROOT1, self.VAR1):
            self._assert_refused(node, self.BOB)

    # -- §1.1 row 3: Alice holds root1 ------------------------------------

    def test_holding_the_root_blocks_the_variant_and_its_sub_grids_the_1_1_hole(self) -> None:
        """THE §1.1 HOLE, exercised through acquire this time (not just tree
        resolution): var1/sub-B/sub-C are var1's own, not root1's, yet Bob
        is refused all three while root1 is held — today's
        `_cascade_lock_to_nested_gridstacks` (parent_tab_id == tab.id only)
        would have let every one of these through."""
        acquire(self.db, self.ROOT1, self.ALICE)

        for node in (self.ROOT1B, self.ROOT2, self.NAV1):
            self._assert_open(node, self.BOB)

        for node in (self.VAR1, self.VAR1B, self.SUB_A, self.SUB_B, self.SUB_C, self.ROOT1):
            self._assert_refused(node, self.BOB)

    # -- §1.1 row 4: Alice holds nav1 -------------------------------------

    def test_holding_the_nav_tab_blocks_only_the_nav_tab_itself(self) -> None:
        """FLIPPED 2026-09-30 (was `test_holding_the_nav_tab_blocks_its_whole_
        subtree_not_other_nav_tabs`): option 2, plan_node_session_gaps §2.3
        — "enter a root tab or widget while someone edits the nav tab:
        allowed"; "two people editing the same nav tab: refused"."""
        acquire(self.db, self.NAV1, self.ALICE)

        for node in (
            self.ROOT2,
            self.ROOT1,
            self.ROOT1B,
            self.VAR1,
            self.VAR1B,
            self.SUB_A,
            self.SUB_B,
            self.SUB_C,
        ):
            self._assert_open(node, self.BOB)

        self._assert_refused(self.NAV1, self.BOB)

    def test_a_nav_tab_acquire_succeeds_while_others_hold_a_root_a_variant_and_a_sub_grid_inside(self) -> None:
        """New 2026-09-30 (plan §4): the reverse direction — "enter a nav tab
        while someone edits inside: allowed"."""
        acquire(self.db, self.ROOT1B, self.ALICE)
        acquire(self.db, self.VAR1, "carol@renphil.org")
        acquire(self.db, self.SUB_A, "dave@renphil.org")
        acquire(self.db, self.NAV1, self.BOB)  # must not raise

    # -- cross-cutting -----------------------------------------------------

    def test_same_holder_never_conflicts_with_themself_at_any_level(self) -> None:
        """Rewritten 2026-09-30: this used to start from NAV1 ("her own
        subtree"); a nav tab has no lock subtree any more, so it starts from
        the root — the highest node that still covers anything."""
        acquire(self.db, self.ROOT1, self.ALICE)
        # Alice can still acquire ANYTHING in her own subtree directly —
        # every level, not just the one immediately below.
        for node in (self.VAR1, self.SUB_A, self.SUB_B):
            acquire(self.db, node, self.ALICE)

    def test_cousins_across_different_nav_tabs_never_conflict(self) -> None:
        acquire(self.db, self.SUB_B, self.ALICE)
        acquire(self.db, self.ROOT2, self.BOB)  # unrelated tree — must not raise

    def test_a_refused_acquire_names_the_actual_holder_and_node(self) -> None:
        acquire(self.db, self.SUB_B, self.ALICE)
        with self.assertRaises(LockConflictError) as ctx:
            acquire(self.db, self.ROOT1, self.BOB)
        self.assertEqual(ctx.exception.blocking[0].holder, self.ALICE)
        self.assertEqual(ctx.exception.blocking[0].node_label, "sub-B")
        self.assertEqual(ctx.exception.blocking[0].relation, "descendant")

    def test_a_stale_hold_anywhere_in_the_chain_never_refuses(self) -> None:
        acquire(self.db, self.VAR1, self.ALICE)
        self._age_tab_lock(self.var1["documentId"], seconds_ago=TTL_SECONDS + 60)
        # Bob may now take the ancestor (root1), the node itself (var1), and
        # a descendant (sub-B) — the stale hold vetoes nothing.
        acquire(self.db, self.SUB_B, self.BOB)
        release(self.db, self.SUB_B, self.BOB)
        acquire(self.db, self.VAR1, self.BOB)
        release(self.db, self.VAR1, self.BOB)


# ---------------------------------------------------------------------------
# force (§4.2, decision 8) — breaks the whole subtree, never an ancestor.
# ---------------------------------------------------------------------------


class TestForceBreaksSubtree(TabLockingFixture):
    def setUp(self) -> None:
        super().setUp()
        self.root = self._root_tab("root")
        self.sub = self._nested_subtab(self.root["documentId"], "sub")
        self.ROOT: LockNode = resolve_lock_node(get_gridstack_by_document_id(self.db, self.root["documentId"]))
        self.SUB: LockNode = resolve_lock_node(get_gridstack_by_document_id(self.db, self.sub["documentId"]))

    def test_a_refused_acquire_lists_every_conflicting_holder_before_anyone_forces(self) -> None:
        acquire(self.db, self.SUB, self.ALICE)

        with self.assertRaises(LockConflictError) as ctx:
            acquire(self.db, self.ROOT, self.BOB)
        holders = {b.holder for b in ctx.exception.blocking}
        self.assertEqual(holders, {self.ALICE})

    def test_force_takes_over_a_fresh_self_conflict(self) -> None:
        acquire(self.db, self.ROOT, self.ALICE)
        session = acquire(self.db, self.ROOT, self.BOB, force=True)
        self.assertEqual(session.holder, self.BOB)

    def test_force_clears_a_conflicting_descendants_lock_row_and_rotates_its_token(self) -> None:
        """UPDATED for §5's require_live_session (phase 3): `locked_by` is
        deliberately KEPT on the broken row, not blanked — see
        `_force_break_lock`'s own docstring on why that's what lets Alice's
        next write resolve to EDIT_SESSION_TAKEN_OVER rather than the less
        accurate EDIT_SESSION_MISSING. `locked` still flips False (the row
        reads as free) and the token is rotated to something Alice never
        received."""
        alice_sub_session = acquire(self.db, self.SUB, self.ALICE)
        self.assertTrue(alice_sub_session.token)

        acquire(self.db, self.ROOT, self.BOB, force=True)

        sub_row = get_gridstack_by_document_id(self.db, self.sub["documentId"])
        self.assertFalse(sub_row.locked)
        self.assertEqual(sub_row.locked_by, self.ALICE)
        # The old token no longer matches ANYTHING in the subtree — rotated
        # to a fresh, unpublished value, not cleared to a comparable None.
        self.assertNotEqual(sub_row.lock_token, alice_sub_session.token)
        self.assertIsNotNone(sub_row.lock_token)

    def test_force_does_not_touch_a_row_already_held_by_the_forcing_holder(self) -> None:
        acquire(self.db, self.SUB, self.ALICE)
        acquire(self.db, self.ROOT, self.ALICE, force=True)
        sub_row = get_gridstack_by_document_id(self.db, self.sub["documentId"])
        # Nothing to take over from yourself — the sub-grid's row is
        # untouched (still exactly as Alice's own direct acquire left it).
        self.assertTrue(sub_row.locked)
        self.assertEqual(sub_row.locked_by, self.ALICE)

    def test_force_never_overrides_an_ancestor_conflict(self) -> None:
        """Decision 8 (and acquire's own docstring): force only ever
        applies to `node`'s own subtree. Bob holding root1 makes root1 an
        ANCESTOR of sub — no amount of forcing sub can break Bob's session
        on root1, since that session lives on a DIFFERENT node than the one
        being forced."""
        acquire(self.db, self.ROOT, self.BOB)
        with self.assertRaises(LockConflictError):
            acquire(self.db, self.SUB, self.ALICE, force=True)

    def test_force_with_no_conflicts_at_all_behaves_like_an_ordinary_acquire(self) -> None:
        session = acquire(self.db, self.ROOT, self.ALICE, force=True)
        self.assertEqual(session.holder, self.ALICE)


# ---------------------------------------------------------------------------
# edit_lock_service.resolve_lock_view / LockView.state_for — §4.3's derived
# read state. Same tree as TestPropagationConflicts (nav1 -> {root1,
# root1b}, root1 -> {var1, var1b, sub-A}, var1 -> {sub-B, sub-C}).
# ---------------------------------------------------------------------------


class TestLockView(TabLockingFixture):
    def setUp(self) -> None:
        super().setUp()
        self.nav1 = self._nav_tab("nav1")
        self.root1 = create_tab_v2(self.db, title="root1", nav_tab_id=self.nav1["id"])
        self.var1 = self._variant(self.root1["documentId"], title="var1")
        self.sub_a = self._nested_subtab(self.root1["documentId"], title="sub-A")
        self.sub_b = self._nested_subtab(self.var1["documentId"], title="sub-B")

        def tab_id_of(resp: dict) -> int:
            return get_gridstack_by_document_id(self.db, resp["documentId"]).parent_tab_id

        def gs_id_of(resp: dict) -> int:
            return get_gridstack_by_document_id(self.db, resp["documentId"]).id

        self.NAV1: LockNode = ("nav_tab", self.nav1["id"])
        self.ROOT1: LockNode = ("tab", tab_id_of(self.root1))
        self.VAR1: LockNode = ("tab", tab_id_of(self.var1))
        self.SUB_A: LockNode = ("gridstack", gs_id_of(self.sub_a))
        self.SUB_B: LockNode = ("gridstack", gs_id_of(self.sub_b))

    def test_an_untouched_node_is_free(self) -> None:
        state = resolve_lock_view(self.db, self.BOB).state_for(self.SUB_A)
        self.assertEqual(state.state, "free")
        self.assertEqual(state.holder, "")
        self.assertIsNone(state.expires_at)

    def test_the_holders_own_view_of_their_own_node_is_self(self) -> None:
        acquire(self.db, self.ROOT1, self.ALICE)
        state = resolve_lock_view(self.db, self.ALICE).state_for(self.ROOT1)
        self.assertEqual(state.state, "self")
        self.assertEqual(state.holder, self.ALICE)
        self.assertEqual(state.node_label, "root1")

    def test_a_foreign_viewers_view_of_the_same_node_is_locked_here(self) -> None:
        acquire(self.db, self.ROOT1, self.ALICE)
        state = resolve_lock_view(self.db, self.BOB).state_for(self.ROOT1)
        self.assertEqual(state.state, "locked_here")
        self.assertEqual(state.holder, self.ALICE)

    def test_a_descendant_of_a_foreign_held_ancestor_is_locked_by_ancestor(self) -> None:
        acquire(self.db, self.ROOT1, self.ALICE)
        state = resolve_lock_view(self.db, self.BOB).state_for(self.SUB_A)
        self.assertEqual(state.state, "locked_by_ancestor")
        self.assertEqual(state.holder, self.ALICE)
        self.assertEqual(state.node_label, "root1")

    def test_an_ancestor_of_a_foreign_held_descendant_is_blocked_by_descendant(self) -> None:
        acquire(self.db, self.SUB_B, self.ALICE)
        state = resolve_lock_view(self.db, self.BOB).state_for(self.VAR1)
        self.assertEqual(state.state, "blocked_by_descendant")
        self.assertEqual(state.holder, self.ALICE)
        self.assertEqual(state.node_label, "sub-B")

    def test_the_holders_own_descendant_and_ancestor_coverage_both_read_as_self(self) -> None:
        """The viewer holding an ANCESTOR or a DESCENDANT of `node` (not
        `node` itself) still reads as "self", not as a foreign block — they
        are covered by their own session, not blocked by someone else's."""
        acquire(self.db, self.ROOT1, self.ALICE)
        self.assertEqual(resolve_lock_view(self.db, self.ALICE).state_for(self.SUB_A).state, "self")

        acquire(self.db, self.SUB_B, self.ALICE)
        self.assertEqual(resolve_lock_view(self.db, self.ALICE).state_for(self.VAR1).state, "self")

    def test_a_stale_lock_reads_as_free_on_the_derived_state_even_though_the_row_stays_locked(self) -> None:
        acquire(self.db, self.ROOT1, self.ALICE)
        self._age_tab_lock(self.root1["documentId"], seconds_ago=TTL_SECONDS + 60)
        state = resolve_lock_view(self.db, self.BOB).state_for(self.ROOT1)
        self.assertEqual(state.state, "free")

    def test_a_sibling_subtree_is_unaffected(self) -> None:
        acquire(self.db, self.SUB_B, self.ALICE)  # inside var1, not root1's own sub-A
        state = resolve_lock_view(self.db, self.BOB).state_for(self.SUB_A)
        self.assertEqual(state.state, "free")

    # -- structure-only nav-tab locks (plan_node_session_gaps §4, 2026-09-30) --

    def test_a_nav_tab_never_reports_blocked_by_descendant(self) -> None:
        for node in (self.ROOT1, self.VAR1, self.SUB_B):
            acquire(self.db, node, self.ALICE)
            self.assertEqual(resolve_lock_view(self.db, self.BOB).state_for(self.NAV1).state, "free", node)
            release(self.db, node, self.ALICE)

    def test_nothing_inside_reports_locked_by_ancestor_from_a_held_nav_tab(self) -> None:
        acquire(self.db, self.NAV1, self.ALICE)
        view = resolve_lock_view(self.db, self.BOB)
        self.assertEqual(view.state_for(self.NAV1).state, "locked_here")
        for node in (self.ROOT1, self.VAR1, self.SUB_A, self.SUB_B):
            self.assertEqual(view.state_for(node).state, "free", node)

    def test_holding_only_the_nav_tab_does_not_read_as_self_on_its_roots(self) -> None:
        """THE O2 CONTRACT step 2 relies on: a viewer holding only the nav
        tab sees their roots as free — not "self" — so the frontend must
        take a real root lock (P1) before any canvas becomes editable."""
        acquire(self.db, self.NAV1, self.ALICE)
        view = resolve_lock_view(self.db, self.ALICE)
        self.assertEqual(view.state_for(self.NAV1).state, "self")
        for node in (self.ROOT1, self.VAR1, self.SUB_A):
            self.assertEqual(view.state_for(node).state, "free", node)


# ---------------------------------------------------------------------------
# edit_lock_service.require_live_session / refuse_if_any_held — §5. Service-
# level (no HTTP door of its own; every mirrored call site is exercised
# through the router once wired, §8 phase 3's remaining work).
# ---------------------------------------------------------------------------


class TestEditSessionTokens(TabLockingFixture):
    def setUp(self) -> None:
        super().setUp()
        self.root = self._root_tab("root")
        self.sub = self._nested_subtab(self.root["documentId"], "sub")
        self.ROOT: LockNode = resolve_lock_node(get_gridstack_by_document_id(self.db, self.root["documentId"]))
        self.SUB: LockNode = resolve_lock_node(get_gridstack_by_document_id(self.db, self.sub["documentId"]))

    def test_missing_token_is_refused(self) -> None:
        acquire(self.db, self.ROOT, self.ALICE)
        session = EditSession(holder=self.ALICE, tokens=frozenset())
        with self.assertRaises(EditSessionError) as ctx:
            require_live_session(session, self.ROOT, self.db)
        self.assertEqual(ctx.exception.code, "EDIT_SESSION_MISSING")

    def test_a_token_belonging_to_a_different_node_entirely_is_refused_as_missing(self) -> None:
        other_root = self._root_tab("other")
        OTHER: LockNode = resolve_lock_node(get_gridstack_by_document_id(self.db, other_root["documentId"]))
        other_grant = acquire(self.db, OTHER, self.ALICE)

        session = EditSession(holder=self.ALICE, tokens=frozenset({other_grant.token}))
        with self.assertRaises(EditSessionError) as ctx:
            require_live_session(session, self.ROOT, self.db)
        self.assertEqual(ctx.exception.code, "EDIT_SESSION_MISSING")

    def test_a_stale_tokens_write_is_refused_as_expired(self) -> None:
        grant = acquire(self.db, self.ROOT, self.ALICE)
        self._age_tab_lock(self.root["documentId"], seconds_ago=TTL_SECONDS + 60)

        session = EditSession(holder=self.ALICE, tokens=frozenset({grant.token}))
        with self.assertRaises(EditSessionError) as ctx:
            require_live_session(session, self.ROOT, self.db)
        self.assertEqual(ctx.exception.code, "EDIT_SESSION_EXPIRED")

    def test_a_forced_out_descendant_holder_is_refused_as_taken_over(self) -> None:
        """`_force_break_lock` (edit_lock_service.py) deliberately keeps
        `locked_by` on the row it frees so this is detectable at all — see
        its own docstring."""
        grant = acquire(self.db, self.SUB, self.ALICE)
        acquire(self.db, self.ROOT, self.BOB, force=True)  # breaks SUB, held fresh by Alice

        session = EditSession(holder=self.ALICE, tokens=frozenset({grant.token}))
        with self.assertRaises(EditSessionError) as ctx:
            require_live_session(session, self.SUB, self.db)
        self.assertEqual(ctx.exception.code, "EDIT_SESSION_TAKEN_OVER")

    def test_a_forced_out_holder_of_the_taken_over_node_itself_reads_as_missing_not_taken_over(
        self,
    ) -> None:
        """Accepted, documented asymmetry (require_live_session's own
        docstring): unlike a DESCENDANT's row (freed, so `locked_by` can be
        left as a trail), the taken-over node's OWN row is legitimately
        overwritten with the NEW holder's identity by `acquire`'s
        `_write_lock` — decision 2 allows only one row per node, so
        Alice's old identity is genuinely gone from it, not merely hidden.
        Both codes drive the same client-side retry flow (§6.3), so this
        is cosmetic wording, not a functional gap."""
        grant = acquire(self.db, self.ROOT, self.ALICE)
        acquire(self.db, self.ROOT, self.BOB, force=True)

        session = EditSession(holder=self.ALICE, tokens=frozenset({grant.token}))
        with self.assertRaises(EditSessionError) as ctx:
            require_live_session(session, self.ROOT, self.db)
        self.assertEqual(ctx.exception.code, "EDIT_SESSION_MISSING")

    def test_a_live_matching_token_on_the_exact_node_passes(self) -> None:
        grant = acquire(self.db, self.SUB, self.ALICE)
        session = EditSession(holder=self.ALICE, tokens=frozenset({grant.token}))
        require_live_session(session, self.SUB, self.db)  # must not raise

    def test_a_token_on_an_ancestor_covers_a_descendant_write(self) -> None:
        grant = acquire(self.db, self.ROOT, self.ALICE)
        session = EditSession(holder=self.ALICE, tokens=frozenset({grant.token}))
        require_live_session(session, self.SUB, self.db)  # must not raise — root covers sub

    def test_a_token_on_a_descendant_does_NOT_cover_the_ancestor(self) -> None:
        """The direction matters: holding a narrow session never widens to
        cover something ABOVE it — only ancestors cover descendants, never
        the reverse (§1's asymmetric rule)."""
        grant = acquire(self.db, self.SUB, self.ALICE)
        session = EditSession(holder=self.ALICE, tokens=frozenset({grant.token}))
        with self.assertRaises(EditSessionError):
            require_live_session(session, self.ROOT, self.db)

    def test_a_leaked_token_is_useless_to_another_identity(self) -> None:
        grant = acquire(self.db, self.ROOT, self.ALICE)
        session = EditSession(holder=self.BOB, tokens=frozenset({grant.token}))
        with self.assertRaises(EditSessionError) as ctx:
            require_live_session(session, self.ROOT, self.db)
        self.assertEqual(ctx.exception.code, "EDIT_SESSION_MISSING")

    def test_sending_all_held_tokens_lets_one_call_cover_either_of_two_sessions(self) -> None:
        """§5.1: a caller sends EVERY token they hold; a write against
        EITHER node succeeds without the call site needing to know which
        session covers its own target."""
        other_root = self._root_tab("other-root")
        OTHER: LockNode = resolve_lock_node(get_gridstack_by_document_id(self.db, other_root["documentId"]))
        root_grant = acquire(self.db, self.ROOT, self.ALICE)
        other_grant = acquire(self.db, OTHER, self.ALICE)

        session = EditSession(holder=self.ALICE, tokens=frozenset({root_grant.token, other_grant.token}))
        require_live_session(session, self.ROOT, self.db)
        require_live_session(session, OTHER, self.db)

    def test_success_renews_locked_at_as_a_side_effect(self) -> None:
        grant = acquire(self.db, self.ROOT, self.ALICE)
        self._age_tab_lock(self.root["documentId"], seconds_ago=TTL_SECONDS - 30)  # fresh, but old-ish

        session = EditSession(holder=self.ALICE, tokens=frozenset({grant.token}))
        require_live_session(session, self.ROOT, self.db)
        self.db.commit()

        tab = self.db.query(TabV2).filter(TabV2.id == self.ROOT[1]).first()
        age = (datetime.now(timezone.utc) - tab.locked_at.replace(tzinfo=timezone.utc)).total_seconds()
        self.assertLess(age, 5)  # renewed to "now", not left at TTL_SECONDS - 30


class TestReorderIsSessionExempt(TabLockingFixture):
    def setUp(self) -> None:
        super().setUp()
        self.root = self._root_tab("root")
        self.sub_a = self._nested_subtab(self.root["documentId"], "sub-A")
        self.sub_b = self._nested_subtab(self.root["documentId"], "sub-B")
        self.SUB_A: LockNode = resolve_lock_node(get_gridstack_by_document_id(self.db, self.sub_a["documentId"]))
        self.SUB_B: LockNode = resolve_lock_node(get_gridstack_by_document_id(self.db, self.sub_b["documentId"]))

    def test_reorder_with_no_token_at_all_succeeds_when_nothing_is_held(self) -> None:
        refuse_if_any_held(self.db, [self.SUB_A, self.SUB_B], self.BOB)  # must not raise

    def test_reorder_is_refused_when_a_node_in_the_set_is_held_fresh_by_someone_else(self) -> None:
        acquire(self.db, self.SUB_A, self.ALICE)
        with self.assertRaises(LockConflictError):
            refuse_if_any_held(self.db, [self.SUB_A, self.SUB_B], self.BOB)

    def test_a_stale_hold_does_not_refuse_reorder(self) -> None:
        acquire(self.db, self.SUB_A, self.ALICE)
        self._age_nested_gridstack_lock(self.sub_a["documentId"], seconds_ago=TTL_SECONDS + 60)
        refuse_if_any_held(self.db, [self.SUB_A, self.SUB_B], self.BOB)  # must not raise

    def test_the_holder_may_reorder_their_own_held_set(self) -> None:
        acquire(self.db, self.SUB_A, self.ALICE)
        refuse_if_any_held(self.db, [self.SUB_A, self.SUB_B], self.ALICE)  # must not raise


# ---------------------------------------------------------------------------
# §5.4/§7 TestNavTabLocking — the service-level gap `TestEditSessionTokens`
# left untested: `require_live_session`'s own chain-walk with a NAV TAB in
# it, and whether the three nav-tab-mutating service functions
# (update_nav_tab_v2, delete_nav_tab_v2, move_tab_to_nav_tab_v2) actually
# enforce it when called directly — same pattern as
# `TestPropagationConflicts` calling `acquire`/`release` directly rather
# than through HTTP. `test_tab_visibility_wiring.py` already covers the
# router-level end-to-end behaviour for all three; this is the more
# surgical check the handoff named as not yet written. Nav-tab lock/unlock
# ROUTES don't exist yet (phase 5), so every session here is minted
# directly via `acquire`, same as `TestEditSessionTokens` above.
# ---------------------------------------------------------------------------


class TestNavTabLocking(TabLockingFixture):
    def setUp(self) -> None:
        super().setUp()
        self.nav = self._nav_tab("nav")
        self.other_nav = self._nav_tab("other-nav")
        self.root = create_tab_v2(self.db, title="Root", nav_tab_id=self.nav["id"])
        self.NAV: LockNode = ("nav_tab", self.nav["id"])
        self.OTHER_NAV: LockNode = ("nav_tab", self.other_nav["id"])
        self.ROOT: LockNode = resolve_lock_node(
            get_gridstack_by_document_id(self.db, self.root["documentId"])
        )

    def _session(self, holder: str, *tokens: str) -> EditSession:
        return EditSession(holder=holder, tokens=frozenset(tokens))

    # -- require_live_session's own chain-walk, with a nav_tab in it --------

    def test_a_token_on_the_nav_tab_itself_covers_a_write_to_the_nav_tab(self) -> None:
        grant = acquire(self.db, self.NAV, self.ALICE)
        require_live_session(self._session(self.ALICE, grant.token), self.NAV, self.db)  # must not raise

    def test_a_token_on_the_nav_tab_does_not_cover_a_write_to_a_root_tab_beneath_it(self) -> None:
        """FLIPPED 2026-09-30 (was `…covers_a_write_to_a_root_tab_beneath_it`):
        nav-tab locks are structure-only (plan_node_session_gaps §4, O2) —
        a nav-tab token authorizes no write inside a root tab."""
        grant = acquire(self.db, self.NAV, self.ALICE)
        with self.assertRaises(EditSessionError) as ctx:
            require_live_session(self._session(self.ALICE, grant.token), self.ROOT, self.db)
        self.assertEqual(ctx.exception.code, "EDIT_SESSION_MISSING")

    def test_no_token_at_all_is_refused_as_missing_on_the_nav_tab(self) -> None:
        with self.assertRaises(EditSessionError) as ctx:
            require_live_session(self._session(self.ALICE), self.NAV, self.db)
        self.assertEqual(ctx.exception.code, "EDIT_SESSION_MISSING")

    def test_a_token_on_a_different_nav_tab_does_not_cover_this_one(self) -> None:
        grant = acquire(self.db, self.OTHER_NAV, self.ALICE)
        with self.assertRaises(EditSessionError) as ctx:
            require_live_session(self._session(self.ALICE, grant.token), self.NAV, self.db)
        self.assertEqual(ctx.exception.code, "EDIT_SESSION_MISSING")

    def test_a_token_on_a_root_tab_does_not_cover_its_nav_tab(self) -> None:
        """The direction matters, mirrored one level up from
        `test_a_token_on_a_descendant_does_NOT_cover_the_ancestor`."""
        grant = acquire(self.db, self.ROOT, self.ALICE)
        with self.assertRaises(EditSessionError):
            require_live_session(self._session(self.ALICE, grant.token), self.NAV, self.db)

    def test_two_nav_tabs_are_fully_independent(self) -> None:
        acquire(self.db, self.NAV, self.ALICE)
        acquire(self.db, self.OTHER_NAV, self.BOB)  # must not raise

    # -- the three nav-tab-mutating service functions, called directly ------

    def test_update_nav_tab_refuses_a_rename_with_no_session(self) -> None:
        with self.assertRaises(EditSessionError) as ctx:
            update_nav_tab_v2(
                self.db, self.nav["documentId"], title="Renamed", session=self._session(self.ALICE)
            )
        self.assertEqual(ctx.exception.code, "EDIT_SESSION_MISSING")

    def test_update_nav_tab_succeeds_with_a_live_session(self) -> None:
        grant = acquire(self.db, self.NAV, self.ALICE)
        result = update_nav_tab_v2(
            self.db,
            self.nav["documentId"],
            title="Renamed",
            session=self._session(self.ALICE, grant.token),
        )
        self.assertEqual(result["title"], "Renamed")

    def test_delete_nav_tab_refuses_with_no_session(self) -> None:
        empty_nav = self._nav_tab("empty")
        with self.assertRaises(EditSessionError) as ctx:
            delete_nav_tab_v2(self.db, empty_nav["documentId"], session=self._session(self.ALICE))
        self.assertEqual(ctx.exception.code, "EDIT_SESSION_MISSING")

    def test_delete_nav_tab_succeeds_with_a_live_session_on_the_nav_tab_itself_not_the_hub(self) -> None:
        """§4.2's documented divergence: the PERMISSION gate is edit(hub),
        but the SESSION target is the nav tab being deleted — there is no
        lock node for the hub at all (§9 item 1)."""
        empty_nav = self._nav_tab("empty")
        grant = acquire(self.db, ("nav_tab", empty_nav["id"]), self.ALICE)
        result = delete_nav_tab_v2(
            self.db, empty_nav["documentId"], session=self._session(self.ALICE, grant.token)
        )
        self.assertIsNotNone(result)

    def test_move_tab_to_nav_tab_refuses_with_no_session(self) -> None:
        with self.assertRaises(EditSessionError) as ctx:
            move_tab_to_nav_tab_v2(
                self.db,
                self.root["documentId"],
                self.other_nav["documentId"],
                session=self._session(self.ALICE),
            )
        self.assertEqual(ctx.exception.code, "EDIT_SESSION_MISSING")

    def test_move_tab_to_nav_tab_succeeds_with_only_the_old_nav_tabs_session(self) -> None:
        """FLIPPED 2026-09-30 (was `…refuses_with_only_the_old_nav_tabs_
        session`): owner decision O3, plan_node_session_gaps §3 — the
        destination needs edit permission but no session."""
        grant = acquire(self.db, self.NAV, self.ALICE)
        result = move_tab_to_nav_tab_v2(
            self.db,
            self.root["documentId"],
            self.other_nav["documentId"],
            session=self._session(self.ALICE, grant.token),
        )
        self.assertIsNotNone(result)
        tab = self.db.query(TabV2).filter(TabV2.id == self.ROOT[1]).first()
        self.assertEqual(tab.nav_tab_id, self.other_nav["id"])

    def test_move_tab_to_nav_tab_refuses_with_only_the_new_nav_tabs_session(self) -> None:
        grant = acquire(self.db, self.OTHER_NAV, self.ALICE)
        with self.assertRaises(EditSessionError) as ctx:
            move_tab_to_nav_tab_v2(
                self.db,
                self.root["documentId"],
                self.other_nav["documentId"],
                session=self._session(self.ALICE, grant.token),
            )
        self.assertEqual(ctx.exception.code, "EDIT_SESSION_MISSING")

    def test_move_tab_to_nav_tab_succeeds_with_both_nav_tabs_sessions(self) -> None:
        old_grant = acquire(self.db, self.NAV, self.ALICE)
        new_grant = acquire(self.db, self.OTHER_NAV, self.ALICE)
        result = move_tab_to_nav_tab_v2(
            self.db,
            self.root["documentId"],
            self.other_nav["documentId"],
            session=self._session(self.ALICE, old_grant.token, new_grant.token),
        )
        self.assertIsNotNone(result)


# ---------------------------------------------------------------------------
# plan_node_session_gaps_2026-09-28.md §4 (option 2, 2026-09-30): nav-tab
# locks are structure-only, so a nav-tab session no longer excludes anyone
# editing inside. `refuse_if_subtree_held` takes over for the three writes
# that would destroy or relocate someone else's live work: root delete, root
# move, nav-tab delete. Fixture: nav -> root -> {var, sub, widget on root's
# canvas}; other_nav as a move destination.
# ---------------------------------------------------------------------------


class TestStructuralWritesRefuseWhileSomeoneEditsInside(TabLockingFixture):
    CAROL = "carol@renphil.org"

    def setUp(self) -> None:
        super().setUp()
        self.nav = self._nav_tab("nav")
        self.other_nav = self._nav_tab("other-nav")
        self.root = create_tab_v2(self.db, title="Root", nav_tab_id=self.nav["id"])
        self.var = self._variant(self.root["documentId"], title="Var")
        self.sub = self._nested_subtab(self.root["documentId"], title="Sub")
        root_gs = get_gridstack_by_document_id(self.db, self.root["documentId"])
        self.widget = ComponentV2(link="w-inside", title="Inside widget", type="text", gridstack_id=root_gs.id)
        self.db.add(self.widget)
        self.db.commit()

        self.NAV: LockNode = ("nav_tab", self.nav["id"])
        self.ROOT: LockNode = ("tab", root_gs.parent_tab_id)
        self.VAR: LockNode = ("tab", get_gridstack_by_document_id(self.db, self.var["documentId"]).parent_tab_id)
        self.SUB: LockNode = ("gridstack", get_gridstack_by_document_id(self.db, self.sub["documentId"]).id)
        self.WIDGET: LockNode = ("component", self.widget.id)
        self.INSIDE = {"root": self.ROOT, "variant": self.VAR, "sub-grid": self.SUB, "widget": self.WIDGET}

    def _session(self, holder: str, *grants) -> EditSession:
        return EditSession(holder=holder, tokens=frozenset(g.token for g in grants))

    def _delete_root(self, session: EditSession):
        return delete_tab_subtree_by_document_id_v2(self.db, self.root["documentId"], session=session)

    def _move_root(self, session: EditSession):
        return move_tab_to_nav_tab_v2(
            self.db, self.root["documentId"], self.other_nav["documentId"], session=session
        )

    def _root_exists(self) -> bool:
        self.db.expire_all()
        return self.db.query(TabV2).filter(TabV2.id == self.ROOT[1]).first() is not None

    # -- the nav-tab session alone -----------------------------------------------

    def test_root_create_with_only_the_nav_tab_session_succeeds(self) -> None:
        """The pen model's + New tab (plan §2.2 row 2): create_tab_v2's own
        session check on the nav tab (unchanged) is met by the nav-tab
        session, even while someone else edits another root inside."""
        acquire(self.db, self.ROOT, self.BOB)
        nav = acquire(self.db, self.NAV, self.ALICE)
        created = create_tab_v2(
            self.db, title="New", nav_tab_id=self.nav["id"], session=self._session(self.ALICE, nav)
        )
        self.assertEqual(created["title"], "New")

    def test_a_nav_tab_token_alone_gets_session_missing_on_a_root_canvas_write(self) -> None:
        nav = acquire(self.db, self.NAV, self.ALICE)
        for node in (self.ROOT, self.VAR, self.SUB, self.WIDGET):
            with self.assertRaises(EditSessionError) as ctx:
                require_live_session(self._session(self.ALICE, nav), node, self.db)
            self.assertEqual(ctx.exception.code, "EDIT_SESSION_MISSING", node)

    def test_two_users_on_the_same_nav_tab_are_still_refused(self) -> None:
        acquire(self.db, self.NAV, self.ALICE)
        with self.assertRaises(LockConflictError):
            acquire(self.db, self.NAV, self.BOB)

    # -- root delete ---------------------------------------------------------------

    def test_root_delete_is_refused_naming_the_holder_while_someone_edits_anything_inside(self) -> None:
        nav = acquire(self.db, self.NAV, self.ALICE)
        for label, node in self.INSIDE.items():
            with self.subTest(inside=label):
                acquire(self.db, node, self.BOB)
                with self.assertRaises(LockConflictError) as ctx:
                    self._delete_root(self._session(self.ALICE, nav))
                self.assertEqual(ctx.exception.code, "NODE_LOCKED")
                self.assertEqual([b.holder for b in ctx.exception.blocking], [self.BOB])
                self.assertTrue(self._root_exists())
                release(self.db, node, self.BOB)

    def test_root_delete_names_every_holder_inside(self) -> None:
        nav = acquire(self.db, self.NAV, self.ALICE)
        acquire(self.db, self.WIDGET, self.BOB)
        acquire(self.db, self.VAR, self.CAROL)
        with self.assertRaises(LockConflictError) as ctx:
            self._delete_root(self._session(self.ALICE, nav))
        self.assertCountEqual(
            [(b.holder, b.relation) for b in ctx.exception.blocking],
            [(self.BOB, "descendant"), (self.CAROL, "descendant")],
        )

    def test_root_delete_succeeds_when_only_the_caller_holds_the_root(self) -> None:
        """The pen model's delete: Alice pressed the root's pen from inside
        the nav-tab session, so she holds both — her own holds never count."""
        nav = acquire(self.db, self.NAV, self.ALICE)
        root = acquire(self.db, self.ROOT, self.ALICE)
        acquire(self.db, self.WIDGET, self.ALICE)  # her own narrower hold too
        result = self._delete_root(self._session(self.ALICE, nav, root))
        self.assertIsNotNone(result)
        self.assertFalse(self._root_exists())

    def test_root_delete_with_variants_needs_no_root_session(self) -> None:
        """Pins the recursion's `session=None` (gridstack_service): the
        pre-existing rule is "a session on the nav tab", and deleting a root
        that HAS variants must not quietly start needing a root session too
        now that the nav-tab token no longer covers the root."""
        nav = acquire(self.db, self.NAV, self.ALICE)
        result = self._delete_root(self._session(self.ALICE, nav))
        self.assertIsNotNone(result)
        self.assertFalse(self._root_exists())
        self.assertIsNone(self.db.query(TabV2).filter(TabV2.id == self.VAR[1]).first())

    def test_a_stale_hold_inside_never_refuses_root_delete(self) -> None:
        nav = acquire(self.db, self.NAV, self.ALICE)
        acquire(self.db, self.WIDGET, self.BOB)
        self.db.query(ComponentV2).filter(ComponentV2.id == self.widget.id).update(
            {"locked_at": datetime.now(timezone.utc) - timedelta(seconds=TTL_SECONDS + 60)}
        )
        self.db.commit()
        self.assertIsNotNone(self._delete_root(self._session(self.ALICE, nav)))

    def test_root_delete_is_refused_through_the_router_with_node_locked_and_blocking(self) -> None:
        """The wire contract step 2 relies on: 409, `NODE_LOCKED`, and a
        `blocking` list naming the holder."""
        acquire(self.db, self.WIDGET, self.BOB)
        resp = self.client.put(
            f"/v2/nav-tabs/{self.nav['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE)
        )
        token = resp.json()["lock_token"]
        resp = self.client.delete(
            f"/v2/tabs/{self.root['documentId']}",
            headers={**self._editor_auth(self.ALICE), "X-Edit-Tokens": token},
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        detail = resp.json()["detail"]
        self.assertEqual(detail["code"], "NODE_LOCKED")
        self.assertEqual([b["holder"] for b in detail["blocking"]], [self.BOB])
        self.assertTrue(self._root_exists())

    # -- root move -------------------------------------------------------------------

    def test_root_move_is_refused_naming_the_holder_while_someone_edits_anything_inside(self) -> None:
        nav = acquire(self.db, self.NAV, self.ALICE)
        for label, node in self.INSIDE.items():
            with self.subTest(inside=label):
                acquire(self.db, node, self.BOB)
                with self.assertRaises(LockConflictError) as ctx:
                    self._move_root(self._session(self.ALICE, nav))
                self.assertEqual(ctx.exception.code, "NODE_LOCKED")
                self.assertEqual([b.holder for b in ctx.exception.blocking], [self.BOB])
                self.db.expire_all()
                tab = self.db.query(TabV2).filter(TabV2.id == self.ROOT[1]).first()
                self.assertEqual(tab.nav_tab_id, self.nav["id"])  # not moved
                release(self.db, node, self.BOB)

    def test_root_move_succeeds_when_only_the_caller_holds_the_root(self) -> None:
        nav = acquire(self.db, self.NAV, self.ALICE)
        root = acquire(self.db, self.ROOT, self.ALICE)
        self.assertIsNotNone(self._move_root(self._session(self.ALICE, nav, root)))

    def test_someone_holding_the_destination_nav_tab_never_blocks_a_move(self) -> None:
        """O3 plus option 2: no session on the destination, and a lock on it
        covers only the nav tab itself."""
        acquire(self.db, ("nav_tab", self.other_nav["id"]), self.BOB)
        nav = acquire(self.db, self.NAV, self.ALICE)
        self.assertIsNotNone(self._move_root(self._session(self.ALICE, nav)))

    # -- nav-tab delete ----------------------------------------------------------------

    def test_nav_tab_delete_is_refused_while_anyone_edits_anything_inside(self) -> None:
        """One of each kind (root, variant, sub-grid, real component) — THE
        TRAP: `_nav_tab_contents` must still reach every one of them."""
        nav = acquire(self.db, self.NAV, self.ALICE)
        for label, node in self.INSIDE.items():
            with self.subTest(inside=label):
                acquire(self.db, node, self.BOB)
                with self.assertRaises(LockConflictError) as ctx:
                    delete_nav_tab_v2(self.db, self.nav["documentId"], session=self._session(self.ALICE, nav))
                self.assertEqual(ctx.exception.code, "NODE_LOCKED")
                self.assertEqual([b.holder for b in ctx.exception.blocking], [self.BOB])
                self.db.expire_all()
                self.assertIsNotNone(self.db.query(NavTabV2).filter(NavTabV2.id == self.nav["id"]).first())
                release(self.db, node, self.BOB)

    def test_nav_tab_delete_succeeds_when_only_the_caller_holds_things_inside(self) -> None:
        nav = acquire(self.db, self.NAV, self.ALICE)
        acquire(self.db, self.ROOT, self.ALICE)
        result = delete_nav_tab_v2(self.db, self.nav["documentId"], session=self._session(self.ALICE, nav))
        self.assertIsNotNone(result)

    # -- force on a nav tab is self-only (decision 9, deliberate) ---------------------

    def test_force_on_the_nav_tab_ends_only_its_own_session_never_one_inside(self) -> None:
        """`acquire`'s force loop runs over `descendants(nav_tab)`, now
        empty — so forcing a nav tab no longer breaks sessions inside it.
        Intended (plan §4 / kickoff decision 9): an admin takes over a
        session inside from that root's own badge."""
        bob_nav = acquire(self.db, self.NAV, self.BOB)
        bob_var = acquire(self.db, self.VAR, self.BOB)
        carol_widget = acquire(self.db, self.WIDGET, self.CAROL)  # on root's canvas, not the variant's
        acquire(self.db, self.NAV, self.ALICE, force=True)
        self.db.expire_all()
        nav_row = self.db.query(NavTabV2).filter(NavTabV2.id == self.nav["id"]).first()
        self.assertEqual(nav_row.locked_by, self.ALICE)
        self.assertNotEqual(nav_row.lock_token, bob_nav.token)
        # Bob's variant and Carol's widget sessions are untouched and live.
        require_live_session(self._session(self.BOB, bob_var), self.VAR, self.db)
        require_live_session(self._session(self.CAROL, carol_widget), self.WIDGET, self.db)

    # -- the check itself ----------------------------------------------------------------

    def test_refuse_if_subtree_held_ignores_the_callers_own_holds_at_every_level(self) -> None:
        for node in (self.ROOT, self.VAR, self.SUB, self.WIDGET):
            acquire(self.db, node, self.ALICE)
        refuse_if_subtree_held(self.db, self.ROOT, self.ALICE)  # must not raise
        refuse_if_subtree_held(self.db, self.NAV, self.ALICE)  # must not raise
        with self.assertRaises(LockConflictError):
            refuse_if_subtree_held(self.db, self.ROOT, self.BOB)

    def test_refuse_if_subtree_held_counts_the_node_itself_as_self(self) -> None:
        acquire(self.db, self.ROOT, self.BOB)
        with self.assertRaises(LockConflictError) as ctx:
            refuse_if_subtree_held(self.db, self.ROOT, self.ALICE)
        self.assertEqual([(b.holder, b.relation) for b in ctx.exception.blocking], [(self.BOB, "self")])

    def test_refuse_if_subtree_held_does_not_look_upward(self) -> None:
        """A hold ABOVE the node (the nav tab) is not "inside" it."""
        acquire(self.db, self.NAV, self.BOB)
        refuse_if_subtree_held(self.db, self.ROOT, self.ALICE)  # must not raise


# ---------------------------------------------------------------------------
# §6.7/§8 phase 5 — PUT /v2/nav-tabs/{document_id}/lock and /unlock. These
# routes did not exist before this phase — every test in `TestNavTabLocking`
# above mints its sessions by calling `edit_lock_service.acquire`/`release`
# directly, never through HTTP (confirmed fresh at this phase's own kickoff).
# This class is the router-level counterpart to `TestLockDoor`/the tab
# unlock tests above, adapted for `NavTabResponse`'s UNWRAPPED response shape
# (no "data" envelope — matches `create_nav_tab`/`update_nav_tab`'s existing
# convention, which the new lock/unlock routes mirror, not the tab workspace
# routes' wrapped one).
# ---------------------------------------------------------------------------


class TestNavTabLockingDoors(TabLockingFixture):
    def setUp(self) -> None:
        super().setUp()
        self.nav = self._nav_tab("nav")
        self.other_nav = self._nav_tab("other-nav")
        self.root = create_tab_v2(self.db, title="Root", nav_tab_id=self.nav["id"])

    def test_locked_by_is_ignored_client_supplied_value_is_rejected(self) -> None:
        """Same Fix-1 shape as the tab door (`LockTabRequest` has no
        `locked_by` field at all, reused as-is here) — a client that still
        sends one gets 422, not a silent ignore."""
        resp = self.client.put(
            f"/v2/nav-tabs/{self.nav['documentId']}/lock",
            json={"locked_by": "someone-else@evil.example.com"},
            headers=self._editor_auth(self.ALICE),
        )
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_holder_is_the_authenticated_identity_and_a_token_comes_back(self) -> None:
        resp = self.client.put(
            f"/v2/nav-tabs/{self.nav['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["locked"])
        self.assertEqual(body["locked_by"], self.ALICE)
        self.assertIsNotNone(body["locked_at"])
        self.assertFalse(body["lock_is_stale"])
        # §4.1's "return the token + expires_at" — only the acquirer's own
        # response carries it (schemas/tab.py's `lock_token` docstring).
        # findings_dev_login_live_testing_2026-09-12.md addendum: this
        # comment already promised `+ expires_at`, but only `lock_token` was
        # ever actually asserted (or assigned — `_format_nav_tab` bare never
        # sets `lock_expires_at`) — see the root-tab door test's identical
        # note for what that silently broke downstream.
        self.assertIsInstance(body["lock_token"], str)
        self.assertTrue(body["lock_token"])
        self.assertIsNotNone(body["lock_expires_at"])

    def test_lock_404s_for_an_unknown_document_id(self) -> None:
        resp = self.client.put(
            "/v2/nav-tabs/does-not-exist/lock", json={}, headers=self._editor_auth(self.ALICE)
        )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_second_user_cannot_take_a_fresh_lock(self) -> None:
        self.client.put(
            f"/v2/nav-tabs/{self.nav['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE)
        )
        resp = self.client.put(
            f"/v2/nav-tabs/{self.nav['documentId']}/lock", json={}, headers=self._editor_auth(self.BOB)
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "NODE_LOCKED", resp.text)

    def test_a_lock_on_the_nav_tab_does_not_block_a_root_tab_beneath_it_through_the_router(self) -> None:
        """FLIPPED 2026-09-30 (was `…blocks_a_root_tab_beneath_it…`, pinning
        lock-propagation decision 3's "a nav-tab lock propagates down"):
        nav-tab locks are structure-only (plan_node_session_gaps §4, option
        2). Through the REAL router on both ends, in both orders."""
        resp = self.client.put(
            f"/v2/nav-tabs/{self.nav['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        resp = self.client.put(
            f"/v2/tabs/{self.root['documentId']}/lock", json={}, headers=self._editor_auth(self.BOB)
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_a_lock_on_a_root_tab_does_not_block_its_nav_tab_through_the_router(self) -> None:
        resp = self.client.put(
            f"/v2/tabs/{self.root['documentId']}/lock", json={}, headers=self._editor_auth(self.BOB)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        resp = self.client.put(
            f"/v2/nav-tabs/{self.nav['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE)
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_same_holder_can_re_enter(self) -> None:
        self.client.put(
            f"/v2/nav-tabs/{self.nav['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE)
        )
        resp = self.client.put(
            f"/v2/nav-tabs/{self.nav['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["locked_by"], self.ALICE)

    def test_force_takes_over_and_the_ousted_holders_token_stops_covering_the_nav_tab(self) -> None:
        first = self.client.put(
            f"/v2/nav-tabs/{self.nav['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE)
        )
        alice_token = first.json()["lock_token"]
        self._demote_holder(self.ALICE)
        resp = self.client.put(
            f"/v2/nav-tabs/{self.nav['documentId']}/lock",
            json={"force": True},
            headers=self._editor_auth(self.BOB),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["locked_by"], self.BOB)
        # §4.2/decision 8: force takes the node over — Alice's old token
        # never validates again either way, but on the SELF node (as
        # opposed to a descendant `_force_break_lock` clears) the resolved
        # code is EDIT_SESSION_MISSING, not TAKEN_OVER: `acquire`'s
        # `_write_lock` legitimately overwrites `locked_by` to Bob here
        # (decision 2 — one row per node), so there is no surviving
        # `locked_by == alice` row left for `require_live_session` to
        # attribute a TAKEN_OVER to. Documented, accepted asymmetry — see
        # that function's own docstring ("only the descendant case can
        # preserve it for free").
        alice_session = EditSession(holder=self.ALICE, tokens=frozenset({alice_token}))
        with self.assertRaises(EditSessionError) as ctx:
            require_live_session(alice_session, ("nav_tab", self.nav["id"]), self.db)
        self.assertEqual(ctx.exception.code, "EDIT_SESSION_MISSING")

    def test_unlock_succeeds_and_clears_the_row(self) -> None:
        self.client.put(
            f"/v2/nav-tabs/{self.nav['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE)
        )
        resp = self.client.put(
            f"/v2/nav-tabs/{self.nav['documentId']}/unlock", json={}, headers=self._editor_auth(self.ALICE)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["locked"])
        self.assertEqual(resp.json()["locked_by"], "")
        self.assertIsNone(resp.json()["locked_at"])

    def test_omission_bypass_is_closed_a_second_user_cannot_steal_a_fresh_lock_via_unlock(self) -> None:
        """Same Fix-1 second half as the tab door: no `unlocked_by` field to
        omit, so Bob's unlock always carries Bob's OWN identity and is
        refused without `force`."""
        self.client.put(
            f"/v2/nav-tabs/{self.nav['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE)
        )
        resp = self.client.put(
            f"/v2/nav-tabs/{self.nav['documentId']}/unlock", json={}, headers=self._editor_auth(self.BOB)
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        # Confirm it is REALLY still locked, by Alice, server-side.
        check = self.client.get("/v2/nav-tabs", headers=self._admin_auth())
        nav_row = next(n for n in check.json()["data"] if n["documentId"] == self.nav["documentId"])
        self.assertTrue(nav_row["locked"])
        self.assertEqual(nav_row["locked_by"], self.ALICE)

    def test_unlock_force_ends_a_fresh_lock(self) -> None:
        """As Hub Admin via `_editor_auth` — see `test_force_unlock_ends_a_fresh_lock`."""
        self.client.put(
            f"/v2/nav-tabs/{self.nav['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE)
        )
        self._demote_holder(self.ALICE)
        resp = self.client.put(
            f"/v2/nav-tabs/{self.nav['documentId']}/unlock",
            json={"force": True},
            headers=self._editor_auth(self.BOB),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["locked"])

    def test_a_token_acquired_through_the_router_covers_a_live_session_check(self) -> None:
        """Ties the new router to the ALREADY-tested enforcement path
        (`require_live_session`, unchanged by this phase) — the token this
        door hands back is a real, usable session token, not just a
        response-shape echo."""
        resp = self.client.put(
            f"/v2/nav-tabs/{self.nav['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE)
        )
        token = resp.json()["lock_token"]
        session = EditSession(holder=self.ALICE, tokens=frozenset({token}))
        require_live_session(session, ("nav_tab", self.nav["id"]), self.db)  # must not raise


# ---------------------------------------------------------------------------
# §5.3/§7: the router-level half of reorder session-exemption.
# `TestReorderIsSessionExempt` above only calls `refuse_if_any_held`
# directly against the service; the plan's own coverage table asks for the
# SAME guarantee through the real router, for all four surfaces — nav tabs,
# root tabs, variants, and SGS sub-tabs (the last two share
# `reorder_tabs_by_document_id_v2`/`PUT /v2/tabs/reorder`, keyed by the
# reordered items' shared gridstack parent) — plus the `order`-only branch
# of the generic `PUT /v2/tabs/{id}`. Every "refused" case here asserts the
# `NODE_LOCKED` code specifically, not just a bare 409 — a mis-wired
# `require_live_session` call would ALSO 409 (as `EDIT_SESSION_MISSING`),
# and that would be the wrong kind of failure for a session-exempt door.
# ---------------------------------------------------------------------------


class TestReorderIsSessionExemptAtTheRouterLevel(TabLockingFixture):
    def setUp(self) -> None:
        super().setUp()
        self.nav = self._nav_tab("nav")
        self.root_a = create_tab_v2(self.db, title="Root A", nav_tab_id=self.nav["id"])
        self.root_b = create_tab_v2(self.db, title="Root B", nav_tab_id=self.nav["id"])
        self.sub_a = self._nested_subtab(self.root_a["documentId"], "sub-A")
        self.sub_b = self._nested_subtab(self.root_a["documentId"], "sub-B")
        self.variant_a = self._variant(self.root_a["documentId"], "Variant A")
        self.variant_b = self._variant(self.root_a["documentId"], "Variant B")

    def _lock(self, holder: str, document_id: str) -> None:
        resp = self.client.put(
            f"/v2/tabs/{document_id}/lock", json={}, headers=self._editor_auth(holder)
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def _admin(self) -> dict:
        return self._auth(self.BOB, roles=["Hub Admin"])

    # -- nav tabs -----------------------------------------------------------

    def test_nav_tab_reorder_succeeds_with_no_token_when_nothing_is_held(self) -> None:
        nav2 = self._nav_tab("nav2")
        resp = self.client.put(
            "/v2/nav-tabs/reorder",
            json={"orderedDocumentIds": [self.nav["documentId"], nav2["documentId"]]},
            headers=self._admin(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_nav_tab_reorder_is_refused_when_a_reordered_nav_tab_is_held_fresh_by_someone_else(
        self,
    ) -> None:
        nav2 = self._nav_tab("nav2")
        acquire(self.db, ("nav_tab", self.nav["id"]), self.ALICE)
        resp = self.client.put(
            "/v2/nav-tabs/reorder",
            json={"orderedDocumentIds": [self.nav["documentId"], nav2["documentId"]]},
            headers=self._admin(),
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "NODE_LOCKED", resp.text)

    # -- root tabs (PUT /v2/tabs/reorder, parent_id is None) ----------------

    def test_root_reorder_succeeds_with_no_token_when_nothing_is_held(self) -> None:
        resp = self.client.put(
            "/v2/tabs/reorder",
            json={
                "items": [
                    {"documentId": self.root_a["documentId"], "order": 1},
                    {"documentId": self.root_b["documentId"], "order": 0},
                ]
            },
            headers=self._admin(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_root_reorder_is_refused_when_a_reordered_root_is_held_fresh_by_someone_else(self) -> None:
        self._lock(self.ALICE, self.root_a["documentId"])
        resp = self.client.put(
            "/v2/tabs/reorder",
            json={
                "items": [
                    {"documentId": self.root_a["documentId"], "order": 1},
                    {"documentId": self.root_b["documentId"], "order": 0},
                ]
            },
            headers=self._admin(),
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "NODE_LOCKED", resp.text)

    # -- SGS sub-tabs (same endpoint, parent_id is a gridstack) -------------

    def test_subtab_reorder_succeeds_with_no_token_when_nothing_is_held(self) -> None:
        resp = self.client.put(
            "/v2/tabs/reorder",
            json={
                "items": [
                    {"documentId": self.sub_a["documentId"], "order": 1},
                    {"documentId": self.sub_b["documentId"], "order": 0},
                ]
            },
            headers=self._admin(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_subtab_reorder_is_refused_when_a_reordered_sub_tab_is_held_fresh_by_someone_else(
        self,
    ) -> None:
        self._lock(self.ALICE, self.sub_a["documentId"])
        resp = self.client.put(
            "/v2/tabs/reorder",
            json={
                "items": [
                    {"documentId": self.sub_a["documentId"], "order": 1},
                    {"documentId": self.sub_b["documentId"], "order": 0},
                ]
            },
            headers=self._admin(),
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "NODE_LOCKED", resp.text)

    # -- variants (PUT /v2/tabs/{id}/variants/reorder) ----------------------

    def test_variant_reorder_succeeds_with_no_token_when_nothing_is_held(self) -> None:
        resp = self.client.put(
            f"/v2/tabs/{self.root_a['documentId']}/variants/reorder",
            json={"orderedDocumentIds": [self.variant_a["documentId"], self.variant_b["documentId"]]},
            headers=self._admin(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_variant_reorder_is_refused_when_a_reordered_variant_is_held_fresh_by_someone_else(
        self,
    ) -> None:
        self._lock(self.ALICE, self.variant_a["documentId"])
        resp = self.client.put(
            f"/v2/tabs/{self.root_a['documentId']}/variants/reorder",
            json={"orderedDocumentIds": [self.variant_a["documentId"], self.variant_b["documentId"]]},
            headers=self._admin(),
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "NODE_LOCKED", resp.text)

    # -- the order-only branch of the generic PUT /v2/tabs/{id} -------------

    def test_generic_update_order_only_succeeds_with_no_token_when_free(self) -> None:
        resp = self.client.put(
            f"/v2/tabs/{self.root_a['documentId']}", json={"order": 5}, headers=self._admin()
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_generic_update_order_only_is_refused_while_the_node_itself_is_held_fresh_by_someone_else(
        self,
    ) -> None:
        """§5.4's own text: the checked node is the tab BEING reordered
        (`resolve_lock_node(gridstack)`), not its parent — unlike the
        `order is None` (rename) branch, which gates the full
        `require_live_session` on the parent instead."""
        self._lock(self.ALICE, self.root_a["documentId"])
        resp = self.client.put(
            f"/v2/tabs/{self.root_a['documentId']}", json={"order": 5}, headers=self._admin()
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "NODE_LOCKED", resp.text)

    def test_generic_update_order_only_is_unaffected_by_a_stale_hold(self) -> None:
        self._lock(self.ALICE, self.root_a["documentId"])
        self._age_tab_lock(self.root_a["documentId"], seconds_ago=TTL_SECONDS + 60)
        resp = self.client.put(
            f"/v2/tabs/{self.root_a['documentId']}", json={"order": 5}, headers=self._admin()
        )
        self.assertEqual(resp.status_code, 200, resp.text)


# ---------------------------------------------------------------------------
# PUT /v2/tabs/components/by-link/{link}/content — decision 6: no session
# token of its own, but refused while the component's owning canvas (or one
# of ITS ancestors) is held fresh by someone else.
# ---------------------------------------------------------------------------


class TestComponentWritesRequireALiveSession(TabLockingFixture):
    """REVERSED 2026-09-16 (TTL fix) — this class was
    `TestComponentWritesCheckAncestors`, pinning decision 6 ("checks
    ancestors but takes no lock": no token, refused only on a FRESH FOREIGN
    hold). That rule predates the per-widget modal acquiring a real lock
    (session_handoff_2026-09-16 §4) and, once it did, meant the modal's own
    session was never validated on write — an hour-expired lock still
    saved. An ordinary canvas widget's write now takes the SAME
    `require_live_session` gate the canvas save has; the old decision-6
    behaviour survives ONLY for SBN descendants (`TestSbnDescendantWrites
    StayTokenless` below), which by explicit owner scope-out take no lock."""

    def setUp(self) -> None:
        super().setUp()
        self.root = self._root_tab("root")
        gridstack = get_gridstack_by_document_id(self.db, self.root["documentId"])
        self.component = ComponentV2(link="widget-1", title="Widget", type="text", gridstack_id=gridstack.id)
        self.db.add(self.component)
        self.db.commit()

    def _lock(self, holder: str) -> str:
        resp = self.client.put(
            f"/v2/tabs/{self.root['documentId']}/lock", json={}, headers=self._editor_auth(holder)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["data"]["lock_token"]

    def _write(self, holder: str, token: str | None = None, link: str | None = None):
        headers = self._auth(holder, roles=["Hub Admin"])
        if token is not None:
            headers["X-Edit-Tokens"] = token
        return self.client.put(
            f"/v2/tabs/components/by-link/{link or self.component.link}/content",
            json={"title": "Renamed"},
            headers=headers,
        )

    def _reload_title(self, link: str = "widget-1") -> str | None:
        self.db.expire_all()
        row = self.db.query(ComponentV2).filter(ComponentV2.link == link).first()
        return row.title if row else None

    def test_refused_as_missing_with_no_token_even_when_the_canvas_is_free(self) -> None:
        """The reversal itself: decision 6's "no token is required at all"
        is gone for an ordinary widget."""
        resp = self._write(self.BOB)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "EDIT_SESSION_MISSING")
        self.assertEqual(self._reload_title(), "Widget")

    def test_allowed_with_the_writers_own_live_session(self) -> None:
        token = self._lock(self.ALICE)
        resp = self._write(self.ALICE, token=token)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._reload_title(), "Renamed")

    def test_refused_as_expired_once_the_writers_own_session_is_stale(self) -> None:
        """THE 2026-09-16 REPORT, at the HTTP door: the modal holder's own
        lock passed its TTL, and their save must fail — it used to succeed
        because one's own stale lock could never read as a FOREIGN fresh
        hold, which was the only thing checked."""
        token = self._lock(self.ALICE)
        self._age_tab_lock(self.root["documentId"], seconds_ago=TTL_SECONDS + 60)
        resp = self._write(self.ALICE, token=token)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "EDIT_SESSION_EXPIRED")
        self.assertEqual(self._reload_title(), "Widget")

    def test_refused_while_the_owning_canvas_is_held_fresh_by_someone_else(self) -> None:
        """A token Bob holds on some OTHER node never covers Alice's canvas
        — resolves as MISSING, the write is refused, and Alice's lock is
        untouched. (Same outcome as before the reversal, different code
        path: the live-session gate subsumes the old foreign-hold check.)"""
        self._lock(self.ALICE)
        other = self._root_tab("other")
        bob_token = self.client.put(
            f"/v2/tabs/{other['documentId']}/lock", json={}, headers=self._editor_auth(self.BOB)
        ).json()["data"]["lock_token"]
        resp = self._write(self.BOB, token=bob_token)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "EDIT_SESSION_MISSING")
        self.assertEqual(self._reload_title(), "Widget")

    def test_a_root_session_covers_a_widget_on_a_sub_grid_below_it(self) -> None:
        """Ancestor sessions still cover descendant writes, exactly as they
        do for the canvas save (`TestEditSessionTokens.test_a_token_on_an_
        ancestor_covers_a_descendant_write`) — a full-canvas editor working
        a nested sub-grid's widget through this door needs no second lock."""
        sub = self._nested_subtab(self.root["documentId"], "sub")
        sub_gridstack = get_gridstack_by_document_id(self.db, sub["documentId"])
        nested_widget = ComponentV2(
            link="widget-2", title="Nested Widget", type="text", gridstack_id=sub_gridstack.id
        )
        self.db.add(nested_widget)
        self.db.commit()

        token = self._lock(self.ALICE)
        resp = self._write(self.ALICE, token=token, link="widget-2")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._reload_title("widget-2"), "Renamed")

    def test_a_forced_out_holder_is_refused_and_told_so(self) -> None:
        token = self._lock(self.ALICE)
        self._demote_holder(self.ALICE)
        forced = self.client.put(
            f"/v2/tabs/{self.root['documentId']}/lock", json={"force": True}, headers=self._editor_auth(self.BOB)
        )
        self.assertEqual(forced.status_code, 200, forced.text)
        resp = self._write(self.ALICE, token=token)
        self.assertEqual(resp.status_code, 409, resp.text)
        # The taken-over node's OWN row is overwritten with Bob's identity
        # (documented asymmetry, require_live_session's docstring) — so
        # this resolves to MISSING, not TAKEN_OVER. Either way: refused.
        self.assertIn(resp.json()["detail"]["code"], ("EDIT_SESSION_MISSING", "EDIT_SESSION_TAKEN_OVER"))
        self.assertEqual(self._reload_title(), "Widget")


class TestSbnDescendantWritesRequireALiveSession(TabLockingFixture):
    """REVERSED 2026-09-17 (plan_component_locking_and_sbn §5.2, decision
    A) — this class was `TestSbnDescendantWritesStayTokenless`, pinning
    decision 6's last surviving case: a widget with `super_blocknote_id`
    set wrote tokenlessly, refused only on a FRESH FOREIGN hold up the
    chain, because the SBN editor took no lock (owner scope-out, 2026-09-16
    §1/§4). Every SBN member is a lock node now and the SBN editor holds a
    real session, so `update_component_content`'s SBN branch is gone and
    this door takes the SAME `require_live_session` gate for every row.
    All three of the old tests are replaced by their opposites below."""

    def setUp(self) -> None:
        super().setUp()
        self.root = self._root_tab("root")
        gridstack = get_gridstack_by_document_id(self.db, self.root["documentId"])
        self.sbn_root = ComponentV2(link="sbn-root", title="SBN", type=SBN_ROOT_TYPE, gridstack_id=gridstack.id)
        self.db.add(self.sbn_root)
        self.db.flush()
        self.leaf = ComponentV2(
            link="sbn-leaf",
            title="Leaf",
            type="text",
            gridstack_id=gridstack.id,
            super_blocknote_id=self.sbn_root.id,
        )
        self.db.add(self.leaf)
        self.db.commit()

    def _write(self, holder: str, token: str | None = None):
        headers = self._auth(holder, roles=["Hub Admin"])
        if token is not None:
            headers["X-Edit-Tokens"] = token
        return self.client.put(
            f"/v2/tabs/components/by-link/{self.leaf.link}/content",
            json={"title": "Renamed"},
            headers=headers,
        )

    def test_a_token_is_now_required_for_an_sbn_descendant(self) -> None:
        """Was `test_no_token_is_required_for_an_sbn_descendant` (200)."""
        resp = self._write(self.BOB)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "EDIT_SESSION_MISSING")

    def test_a_foreign_canvas_hold_reads_as_missing_not_node_locked(self) -> None:
        """Was `test_still_refused_while_the_owning_canvas_is_held_fresh_by_
        someone_else` (409 NODE_LOCKED via the tokenless foreign-hold check).
        Still refused — but through the live-session gate, so the code is
        MISSING: Bob presented no token that covers this node."""
        self.client.put(f"/v2/tabs/{self.root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE))
        resp = self._write(self.BOB)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "EDIT_SESSION_MISSING")

    def test_the_writers_own_stale_canvas_hold_is_refused_as_expired(self) -> None:
        """Was `test_still_allowed_when_the_canvas_is_held_by_the_writer_
        themself_even_stale` (200) — the exact decision-6 asymmetry, now
        closed for SBN rows exactly as the 2026-09-16 fix closed it for
        ordinary widgets."""
        token = self.client.put(
            f"/v2/tabs/{self.root['documentId']}/lock", json={}, headers=self._editor_auth(self.ALICE)
        ).json()["data"]["lock_token"]
        self._age_tab_lock(self.root["documentId"], seconds_ago=TTL_SECONDS + 60)
        resp = self._write(self.ALICE, token=token)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "EDIT_SESSION_EXPIRED")

    def test_an_sbn_root_session_covers_the_descendant_write(self) -> None:
        token = self.client.put(
            f"/v2/sbn/{self.sbn_root.link}/lock", json={}, headers=self._editor_auth(self.ALICE)
        ).json()["data"]["lock_token"]
        resp = self._write(self.ALICE, token=token)
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_the_leafs_own_component_session_covers_its_write(self) -> None:
        token = self.client.put(
            f"/v2/tabs/components/by-link/{self.leaf.link}/lock", json={}, headers=self._editor_auth(self.ALICE)
        ).json()["data"]["lock_token"]
        resp = self._write(self.ALICE, token=token)
        self.assertEqual(resp.status_code, 200, resp.text)


class TestRenewIsAValidateNotAnAcquire(TabLockingFixture):
    """`edit_lock_service.renew` — the save preflight's validate door
    (2026-09-16 TTL fix). The property that matters, pinned against the
    plain `acquire` it replaced in the preflight: a same-holder re-entry
    through `acquire` silently RESURRECTS a stale lock (by design, §4.1);
    `renew` must never do that."""

    def setUp(self) -> None:
        super().setUp()
        self.root = self._root_tab("root")
        self.sub = self._nested_subtab(self.root["documentId"], "sub")
        self.ROOT: LockNode = resolve_lock_node(get_gridstack_by_document_id(self.db, self.root["documentId"]))
        self.SUB: LockNode = resolve_lock_node(get_gridstack_by_document_id(self.db, self.sub["documentId"]))

    def _row_locked_at(self, node: LockNode):
        self.db.expire_all()
        model = {"tab": TabV2, "gridstack": GridstackV2, "nav_tab": NavTabV2}[node[0]]
        return self.db.query(model).filter(model.id == node[1]).first().locked_at

    def test_a_fresh_self_held_session_renews_and_keeps_its_token(self) -> None:
        grant = acquire(self.db, self.ROOT, self.ALICE)
        self._age_tab_lock(self.root["documentId"], seconds_ago=TTL_SECONDS - 30)
        session = EditSession(holder=self.ALICE, tokens=frozenset({grant.token}))

        renewed = renew(self.db, session, self.ROOT)

        self.assertEqual(renewed.token, grant.token)
        self.assertGreater(renewed.expires_at, grant.expires_at - timedelta(seconds=60))
        age = (datetime.now(timezone.utc) - self._row_locked_at(self.ROOT).replace(tzinfo=timezone.utc)).total_seconds()
        self.assertLess(age, 5)

    def test_a_stale_self_held_session_is_refused_as_expired_and_NOT_resurrected(self) -> None:
        """THE 2026-09-16 REPORT. Contrast with `acquire` two lines down:
        same row, same holder, same staleness — `acquire` happily brings it
        back to life (which is what the old preflight did, defeating the
        TTL); `renew` refuses and leaves `locked_at` exactly as old as it
        found it."""
        grant = acquire(self.db, self.ROOT, self.ALICE)
        self._age_tab_lock(self.root["documentId"], seconds_ago=TTL_SECONDS + 60)
        stale_locked_at = self._row_locked_at(self.ROOT)
        session = EditSession(holder=self.ALICE, tokens=frozenset({grant.token}))

        with self.assertRaises(EditSessionError) as ctx:
            renew(self.db, session, self.ROOT)
        self.assertEqual(ctx.exception.code, "EDIT_SESSION_EXPIRED")
        self.assertEqual(self._row_locked_at(self.ROOT), stale_locked_at)  # untouched
        self.assertTrue(is_lock_stale(self._row_locked_at(self.ROOT)))

        # …and the contrast: this is exactly what the old preflight did.
        acquire(self.db, self.ROOT, self.ALICE)
        self.assertFalse(is_lock_stale(self._row_locked_at(self.ROOT)))

    def test_no_token_is_refused_as_missing(self) -> None:
        acquire(self.db, self.ROOT, self.ALICE)
        with self.assertRaises(EditSessionError) as ctx:
            renew(self.db, EditSession(holder=self.ALICE, tokens=frozenset()), self.ROOT)
        self.assertEqual(ctx.exception.code, "EDIT_SESSION_MISSING")

    def test_a_reclaimed_by_someone_else_row_is_refused_as_missing(self) -> None:
        """The expired-then-reclaimed shape from the live repro: Alice's
        session went stale, Bob's ordinary acquire reclaimed it (new token,
        `locked_by` now Bob). Alice's renew must not find anything of hers."""
        grant = acquire(self.db, self.ROOT, self.ALICE)
        self._age_tab_lock(self.root["documentId"], seconds_ago=TTL_SECONDS + 60)
        acquire(self.db, self.ROOT, self.BOB)
        session = EditSession(holder=self.ALICE, tokens=frozenset({grant.token}))
        with self.assertRaises(EditSessionError) as ctx:
            renew(self.db, session, self.ROOT)
        self.assertEqual(ctx.exception.code, "EDIT_SESSION_MISSING")

    def test_a_forced_out_descendant_session_is_refused_as_taken_over(self) -> None:
        grant = acquire(self.db, self.SUB, self.ALICE)
        acquire(self.db, self.ROOT, self.BOB, force=True)
        session = EditSession(holder=self.ALICE, tokens=frozenset({grant.token}))
        with self.assertRaises(EditSessionError) as ctx:
            renew(self.db, session, self.SUB)
        self.assertEqual(ctx.exception.code, "EDIT_SESSION_TAKEN_OVER")

    def test_renew_is_self_only_a_fresh_root_session_does_NOT_vouch_for_a_stale_sub_grid_session(
        self,
    ) -> None:
        """`self_only=True`'s reason to exist. A WRITE on the sub-grid would
        pass on the root token (ancestor covers descendant); a RENEWAL of
        the sub-grid SESSION must not — the client would mark that session
        renewed while its own row stayed stale."""
        root_grant = acquire(self.db, self.ROOT, self.ALICE)
        sub_grant = acquire(self.db, self.SUB, self.ALICE)
        self._age_nested_gridstack_lock(self.sub["documentId"], seconds_ago=TTL_SECONDS + 60)
        session = EditSession(holder=self.ALICE, tokens=frozenset({root_grant.token, sub_grant.token}))

        require_live_session(session, self.SUB, self.db)  # a write: root covers it, passes
        self.db.rollback()  # discard that renewal so the assertion below is about `renew`
        with self.assertRaises(EditSessionError) as ctx:
            renew(self.db, session, self.SUB)
        self.assertEqual(ctx.exception.code, "EDIT_SESSION_EXPIRED")


class TestRenewDoors(TabLockingFixture):
    """`PUT /v2/tabs/{id}/lock/renew` and `PUT /v2/nav-tabs/{id}/lock/renew`
    — the HTTP shape the frontend preflight actually calls."""

    def setUp(self) -> None:
        super().setUp()
        self.root = self._root_tab("root")
        self.sub = self._nested_subtab(self.root["documentId"], "sub")
        self.nav = self._nav_tab("nav")

    def _lock(self, path: str, holder: str) -> dict:
        resp = self.client.put(path, json={}, headers=self._editor_auth(holder))
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        return body["data"] if "data" in body else body

    def _renew(self, path: str, holder: str, token: str | None, json: dict | None = None):
        headers = self._editor_auth(holder)
        if token is not None:
            headers["X-Edit-Tokens"] = token
        return self.client.put(path, json=json or {}, headers=headers)

    def test_tab_renew_succeeds_with_a_fresh_session_and_echoes_token_and_expiry(self) -> None:
        locked = self._lock(f"/v2/tabs/{self.root['documentId']}/lock", self.ALICE)
        resp = self._renew(f"/v2/tabs/{self.root['documentId']}/lock/renew", self.ALICE, locked["lock_token"])
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()["data"]
        self.assertEqual(data["lock_token"], locked["lock_token"])
        self.assertIsNotNone(data["lock_expires_at"])

    def test_tab_renew_refuses_a_stale_session_as_expired_and_leaves_the_row_stale(self) -> None:
        locked = self._lock(f"/v2/tabs/{self.root['documentId']}/lock", self.ALICE)
        self._age_tab_lock(self.root["documentId"], seconds_ago=TTL_SECONDS + 60)
        resp = self._renew(f"/v2/tabs/{self.root['documentId']}/lock/renew", self.ALICE, locked["lock_token"])
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "EDIT_SESSION_EXPIRED")
        ws = self.client.get(f"/v2/tabs/{self.root['documentId']}/workspace", headers=self._admin_auth()).json()["data"]
        self.assertTrue(ws["lock_is_stale"])  # NOT resurrected

    def test_tab_renew_with_no_token_is_missing(self) -> None:
        self._lock(f"/v2/tabs/{self.root['documentId']}/lock", self.ALICE)
        resp = self._renew(f"/v2/tabs/{self.root['documentId']}/lock/renew", self.ALICE, None)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "EDIT_SESSION_MISSING")

    def test_tab_renew_never_mints_a_lock_on_a_free_node(self) -> None:
        """A renew on a node nobody holds is MISSING, and afterwards the
        node is STILL free — the one thing that makes this usable as a
        preflight instead of `lock`."""
        resp = self._renew(f"/v2/tabs/{self.root['documentId']}/lock/renew", self.ALICE, "not-a-real-token")
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "EDIT_SESSION_MISSING")
        ws = self.client.get(f"/v2/tabs/{self.root['documentId']}/workspace", headers=self._admin_auth()).json()["data"]
        self.assertFalse(ws["locked"])

    def test_sub_grid_renew_works_against_the_gridstack_row(self) -> None:
        locked = self._lock(f"/v2/tabs/{self.sub['documentId']}/lock", self.ALICE)
        resp = self._renew(f"/v2/tabs/{self.sub['documentId']}/lock/renew", self.ALICE, locked["lock_token"])
        self.assertEqual(resp.status_code, 200, resp.text)
        self._age_nested_gridstack_lock(self.sub["documentId"], seconds_ago=TTL_SECONDS + 60)
        resp = self._renew(f"/v2/tabs/{self.sub['documentId']}/lock/renew", self.ALICE, locked["lock_token"])
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "EDIT_SESSION_EXPIRED")

    def test_nav_tab_renew_succeeds_fresh_and_refuses_stale(self) -> None:
        locked = self._lock(f"/v2/nav-tabs/{self.nav['documentId']}/lock", self.ALICE)
        resp = self._renew(f"/v2/nav-tabs/{self.nav['documentId']}/lock/renew", self.ALICE, locked["lock_token"])
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["lock_token"], locked["lock_token"])
        self.assertIsNotNone(resp.json()["lock_expires_at"])

        nav_row = self.db.query(NavTabV2).filter(NavTabV2.document_id == self.nav["documentId"]).first()
        nav_row.locked_at = datetime.now(timezone.utc) - timedelta(seconds=TTL_SECONDS + 60)
        self.db.commit()
        resp = self._renew(f"/v2/nav-tabs/{self.nav['documentId']}/lock/renew", self.ALICE, locked["lock_token"])
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "EDIT_SESSION_EXPIRED")

    def test_a_renew_on_a_tab_document_id_sent_to_the_nav_tab_route_is_404_and_vice_versa(self) -> None:
        """The frontend preflight must route by `nodeKind` — pins that the
        two doors are not interchangeable (the old preflight sent a nav-tab
        session's id to the TAB lock route and 404'd the whole save)."""
        locked = self._lock(f"/v2/nav-tabs/{self.nav['documentId']}/lock", self.ALICE)
        resp = self._renew(f"/v2/tabs/{self.nav['documentId']}/lock/renew", self.ALICE, locked["lock_token"])
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_unknown_body_fields_are_422_force_in_particular(self) -> None:
        """No `force` on a renew — there is nothing to take over."""
        locked = self._lock(f"/v2/tabs/{self.root['documentId']}/lock", self.ALICE)
        resp = self._renew(
            f"/v2/tabs/{self.root['documentId']}/lock/renew", self.ALICE, locked["lock_token"], json={"force": True}
        )
        self.assertEqual(resp.status_code, 422, resp.text)


if __name__ == "__main__":
    unittest.main()
