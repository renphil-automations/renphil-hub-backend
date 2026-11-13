from __future__ import annotations

import logging
import os
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/validation")
os.environ.setdefault("DATABASE_URL_V2", "postgresql://localhost/validation_v2")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2, get_db_v2  # noqa: E402
from app.db_v2.models.bookmark import BookmarkV2  # noqa: E402
from app.db_v2.models.component import ComponentV2  # noqa: E402
from app.db_v2.models.gridstack import GridstackV2  # noqa: E402
from app.db_v2.models.hub import HubV2  # noqa: E402
from app.db_v2.models.nav_tab import NavTabV2  # noqa: E402
from app.db_v2.models.notification import NotificationV2  # noqa: E402
from app.db_v2.models.resource_grant import ResourceGrantV2  # noqa: E402
from app.db_v2.models.role import RoleEdgeV2, RoleV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.db_v2.models.scope import ScopeEdgeV2, ScopeV2  # noqa: E402
from app.db_v2.models.tab import TabV2  # noqa: E402
from app.db_v2.models.thread import ThreadCommentV2, ThreadRevisionV2, ThreadV2, ThreadVoteV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.dependencies import get_current_user  # noqa: E402
from app.main import app  # noqa: E402
from app.models.auth import UserInfo  # noqa: E402
from app.services import view_as_service  # noqa: E402
from app.services.view_as_service import ViewAsError  # noqa: E402

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _pair_codes(response) -> list[str]:
    """Per-pair error codes of a refused View As request, in request order."""
    return [pair["code"] for pair in response.json()["detail"]["pairs"]]


class _ViewAsDb(unittest.TestCase):
    """Role DAG:  hub_admin -> lead -> member;  "User" is public; "outsider" unrelated.
    Scope DAG:   org -> progA -> progA1;  progB unrelated;  "All Scopes" universal;
                 "Hub" public."""

    def setUp(self) -> None:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(engine, "connect", lambda conn, _rec: conn.execute("PRAGMA foreign_keys=ON"))
        BaseV2.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()

        self.role = {}
        for key, name, public in [
            ("hub_admin", "Hub Admin", False),
            ("lead", "Program Lead", False),
            ("member", "Program Member", False),
            ("outsider", "Outsider", False),
            ("public", "User", True),
        ]:
            row = RoleV2(key=key, name=name, is_system=False, is_public=public, created_at=NOW)
            self.db.add(row)
            self.role[key] = row
        self.scope = {}
        for key, name, universal, public in [
            ("all", "All Scopes", True, False),
            ("org", "Org", False, False),
            ("progA", "Program A", False, False),
            ("progA1", "Program A1", False, False),
            ("progB", "Program B", False, False),
            ("hub", "Hub", False, True),
        ]:
            row = ScopeV2(key=key, name=name, is_universal=universal, is_public=public, is_system=False, created_at=NOW)
            self.db.add(row)
            self.scope[key] = row
        self.db.flush()

        for parent, child in [("hub_admin", "lead"), ("lead", "member")]:
            self.db.add(RoleEdgeV2(parent_role_id=self.role[parent].id, child_role_id=self.role[child].id, created_at=NOW))
        for parent, child in [("org", "progA"), ("progA", "progA1")]:
            self.db.add(
                ScopeEdgeV2(parent_scope_id=self.scope[parent].id, child_scope_id=self.scope[child].id, created_at=NOW)
            )

        self.alice = self._user("Alice", work="alice@renphil.org", holds=[("lead", "org")])
        self.bob = self._user("Bob", work="bob@renphil.org", alias="bob.alias@renphil.org", holds=[("member", "progB")])
        self.carol = self._user("Carol", work="carol@renphil.org", holds=[("lead", "progA"), ("member", "progB")])
        self.admin = self._user("Admin", work="admin@renphil.org", holds=[("hub_admin", "all")])
        self.nobody = self._user("Nobody", work="nobody@renphil.org", holds=[])
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def _user(self, name, *, work=None, alias=None, holds=()):
        user = UserV2(name=name, work_email=work, alias_email=alias, created_at=NOW, updated_at=NOW)
        self.db.add(user)
        self.db.flush()
        for role_key, scope_key in holds:
            self.db.add(
                RoleAssignmentV2(
                    user_id=user.id,
                    role_id=self.role[role_key].id,
                    scope_id=self.scope[scope_key].id,
                    created_at=NOW,
                )
            )
        return user

    def _names(self, rows):
        return {row["name"] for row in rows}


class ResolveUserIdTests(_ViewAsDb):
    def test_work_email_case_insensitive(self) -> None:
        self.assertEqual(view_as_service.resolve_user_id(self.db, "  ALICE@renphil.org "), self.alice.id)

    def test_alias_email_fallback(self) -> None:
        self.assertEqual(view_as_service.resolve_user_id(self.db, "bob.alias@renphil.org"), self.bob.id)

    def test_work_email_wins_over_another_rows_alias(self) -> None:
        other = self._user("Other", work="other@renphil.org", alias="alice@renphil.org")
        self.db.commit()
        self.assertNotEqual(other.id, self.alice.id)
        self.assertEqual(view_as_service.resolve_user_id(self.db, "alice@renphil.org"), self.alice.id)

    def test_deleted_and_unknown(self) -> None:
        self.alice.is_deleted = True
        self.db.commit()
        self.assertIsNone(view_as_service.resolve_user_id(self.db, "alice@renphil.org"))
        self.assertIsNone(view_as_service.resolve_user_id(self.db, "ghost@renphil.org"))
        self.assertIsNone(view_as_service.resolve_user_id(self.db, ""))


class ViewableScopesTests(_ViewAsDb):
    def test_held_scope_plus_direct_and_indirect_children_plus_public(self) -> None:
        rows = view_as_service.list_viewable_scopes(self.db, self.alice.id)
        self.assertEqual(self._names(rows), {"Org", "Program A", "Program A1", "Hub"})
        self.assertEqual(len(rows), len({r["id"] for r in rows}))

    def test_universal_scope_covers_every_scope(self) -> None:
        rows = view_as_service.list_viewable_scopes(self.db, self.admin.id)
        self.assertEqual(self._names(rows), {s.name for s in self.scope.values()})
        self.assertEqual({r["key"] for r in rows if r["is_universal"]}, {"all"})

    def test_no_assignments_still_gets_public_scope(self) -> None:
        self.assertEqual(self._names(view_as_service.list_viewable_scopes(self.db, self.nobody.id)), {"Hub"})


class ViewableRolesTests(_ViewAsDb):
    def test_held_role_plus_child_roles_plus_public_on_held_scope(self) -> None:
        rows = view_as_service.list_viewable_roles(self.db, self.alice.id, self.scope["org"].id)
        self.assertEqual(self._names(rows), {"Program Lead", "Program Member", "User"})

    def test_role_held_on_ancestor_scope_applies_to_grandchild(self) -> None:
        rows = view_as_service.list_viewable_roles(self.db, self.alice.id, self.scope["progA1"].id)
        self.assertEqual(self._names(rows), {"Program Lead", "Program Member", "User"})

    def test_pairing_is_kept_per_assignment_row(self) -> None:
        rows = view_as_service.list_viewable_roles(self.db, self.carol.id, self.scope["progB"].id)
        self.assertEqual(self._names(rows), {"Program Member", "User"})

    def test_public_scope_only_user_gets_public_role(self) -> None:
        rows = view_as_service.list_viewable_roles(self.db, self.nobody.id, self.scope["hub"].id)
        self.assertEqual(self._names(rows), {"User"})

    def test_scope_not_allowed_and_unknown(self) -> None:
        with self.assertRaises(ViewAsError) as ctx:
            view_as_service.list_viewable_roles(self.db, self.alice.id, self.scope["progB"].id)
        self.assertEqual((ctx.exception.status_code, ctx.exception.code), (403, "view_as_scope_not_allowed"))
        with self.assertRaises(ViewAsError) as ctx:
            view_as_service.list_viewable_roles(self.db, self.alice.id, 99999)
        self.assertEqual(ctx.exception.status_code, 404)


class VerifyViewAsTests(_ViewAsDb):
    def _verify(self, user, role, scope):
        return view_as_service.verify_view_as(self.db, user.id, role_key=role, scope_key=scope)

    def test_allowed(self) -> None:
        target = self._verify(self.alice, "member", "progA1")
        self.assertEqual((target.role_id, target.scope_id), (self.role["member"].id, self.scope["progA1"].id))
        self._verify(self.alice, "lead", "org")
        self._verify(self.alice, "public", "hub")
        self._verify(self.nobody, "public", "hub")

    def test_refusals(self) -> None:
        cases = [
            (self.alice, "lead", "Nope", 400, "view_as_scope_not_found"),
            (self.alice, "lead", "progB", 403, "view_as_scope_not_allowed"),
            (self.alice, "Nope", "org", 400, "view_as_role_not_found"),
            (self.alice, "hub_admin", "org", 403, "view_as_role_not_allowed"),
            (self.alice, "outsider", "progA", 403, "view_as_role_not_allowed"),
            (self.carol, "lead", "progB", 403, "view_as_role_not_allowed"),
            # Names are no longer accepted — only keys.
            (self.alice, "lead", "Program A", 400, "view_as_scope_not_found"),
            (self.alice, "Program Member", "progA", 400, "view_as_role_not_found"),
        ]
        for user, role, scope, status, code in cases:
            with self.subTest(role=role, scope=scope):
                with self.assertRaises(ViewAsError) as ctx:
                    self._verify(user, role, scope)
                self.assertEqual((ctx.exception.status_code, ctx.exception.code), (status, code))


class _ViewAsHub(_ViewAsDb):
    """N1 granted to (Program Member, Program A); N2 to (Program Lead, Org);
    N3 directly to Alice. Tab T1 sits under N1."""

    def setUp(self) -> None:
        super().setUp()
        hub = HubV2(document_id="hub")
        self.db.add(hub)
        self.navs = {}
        for i, slug in enumerate(["n1", "n2", "n3"]):
            nav = NavTabV2(document_id=f"nav-{slug}", slug=slug, title=slug.upper(), order=i, protected=False)
            self.db.add(nav)
            self.navs[slug] = nav
        self.db.flush()
        self.tab = TabV2(
            document_id="tab-1", title="T1", order=0, access_control={}, locked=False, locked_by="",
            nav_tab_id=self.navs["n1"].id,
        )
        self.db.add(self.tab)
        self.db.flush()
        self.grid = GridstackV2(
            document_id="tab-1", name="T1", settings={}, position=0, parent_id=None, parent_tab_id=self.tab.id
        )
        self.db.add(self.grid)
        grants = [
            dict(nav_tab_id=self.navs["n1"].id, role_id=self.role["member"].id, scope_id=self.scope["progA"].id),
            dict(nav_tab_id=self.navs["n2"].id, role_id=self.role["lead"].id, scope_id=self.scope["org"].id),
            dict(nav_tab_id=self.navs["n3"].id, user_id=self.alice.id),
        ]
        for grant in grants:
            self.db.add(ResourceGrantV2(level="view", created_at=NOW, **grant))
        self.db.commit()

        self.email = "alice@renphil.org"
        app.dependency_overrides[get_current_user] = lambda: UserInfo(email=self.email, name="Test")
        app.dependency_overrides[get_db_v2] = lambda: self.db
        self.client = TestClient(app)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        super().tearDown()

    def _nav_titles(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return {row["title"] for row in response.json()["data"]}


class ViewAsEndpointTests(_ViewAsHub):
    def test_scope_and_role_picker_endpoints(self) -> None:
        response = self.client.get("/v2/view-as/scopes")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["data"],
            [
                {"id": self.scope["hub"].id, "key": "hub", "name": "Hub", "is_universal": False},
                {"id": self.scope["org"].id, "key": "org", "name": "Org", "is_universal": False},
                {"id": self.scope["progA"].id, "key": "progA", "name": "Program A", "is_universal": False},
                {"id": self.scope["progA1"].id, "key": "progA1", "name": "Program A1", "is_universal": False},
            ],
        )

        response = self.client.get(f"/v2/view-as/scopes/{self.scope['progA'].id}/roles")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["scope_id"], self.scope["progA"].id)
        self.assertEqual(
            body["data"],
            [
                {"id": self.role["lead"].id, "key": "lead", "name": "Program Lead"},
                {"id": self.role["member"].id, "key": "member", "name": "Program Member"},
                {"id": self.role["public"].id, "key": "public", "name": "User"},
            ],
        )

        # The returned keys are exactly what the View As parameters accept.
        # (N3 is shared directly with Alice, so pairs-only View As keeps it.)
        response = self.client.get(
            "/v2/nav-tabs", params={"view_as": f"{body['data'][1]['key']}:progA"}
        )
        self.assertEqual(self._nav_titles(response), {"N1", "N3"})

        response = self.client.get(f"/v2/view-as/scopes/{self.scope['progB'].id}/roles")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "view_as_scope_not_allowed")

    def test_picker_resolves_alias_email(self) -> None:
        self.email = "bob.alias@renphil.org"
        response = self.client.get("/v2/view-as/scopes")
        self.assertEqual(self._names(response.json()["data"]), {"Program B", "Hub"})

    def test_no_params_is_regular_flow(self) -> None:
        for params in ({}, {"view_as": ["", "  "]}):
            with self.subTest(params=params):
                response = self.client.get("/v2/nav-tabs", params=params)
                self.assertEqual(self._nav_titles(response), {"N1", "N2", "N3"})
                self.assertIs(response.json()["view_as"], False)

    def test_view_as_narrows_to_the_adopted_pair_plus_own_direct_shares(self) -> None:
        # N3 is shared directly with Alice: kept with pairs only.
        with self.assertLogs("app.dependencies", level=logging.INFO) as logs:
            response = self.client.get(
                "/v2/nav-tabs", params={"view_as": "member:progA"}
            )
        self.assertEqual(self._nav_titles(response), {"N1", "N3"})
        self.assertIs(response.json()["view_as"], True)
        self.assertIs(response.json()["hub_edit"], False)
        self.assertTrue(any("View As verified" in line for line in logs.output))

        response = self.client.get(
            "/v2/nav-tabs", params={"view_as": "lead:org"}
        )
        self.assertEqual(self._nav_titles(response), {"N1", "N2", "N3"})

        response = self.client.get("/v2/nav-tabs", params={"view_as": "public:hub"})
        self.assertEqual(self._nav_titles(response), {"N3"})

    def test_only_one_param_is_400(self) -> None:
        for params in ({"view_as": "member"}, {"view_as": " :progA"}):
            with self.subTest(params=params):
                with self.assertLogs("app.dependencies", level=logging.WARNING):
                    response = self.client.get("/v2/nav-tabs", params=params)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(_pair_codes(response), ["view_as_malformed_pair"])

    def test_verification_failures(self) -> None:
        cases = [
            ({"view_as": "lead:progB"}, 403, "view_as_scope_not_allowed"),
            ({"view_as": "hub_admin:org"}, 403, "view_as_role_not_allowed"),
            ({"view_as": "lead:Nope"}, 400, "view_as_scope_not_found"),
            ({"view_as": "Nope:org"}, 400, "view_as_role_not_found"),
        ]
        for path in (
            "/v2/nav-tabs",
            "/v2/nav-tabs/nav-n1/tabs",
            "/v2/tabs/tab-1/workspace",
            "/v2/tabs/tab-1/children",
            "/v2/tabs/tab-1/variants",
        ):
            for params, status, code in cases:
                with self.subTest(path=path, code=code):
                    with self.assertLogs("app.dependencies", level=logging.WARNING) as logs:
                        response = self.client.get(path, params=params)
                    self.assertEqual(response.status_code, status, response.text)
                    self.assertEqual(_pair_codes(response), [code])
                    self.assertTrue(any("verification failed" in line for line in logs.output))

    def test_auth_runs_before_view_as_checks(self) -> None:
        app.dependency_overrides.pop(get_current_user)
        response = self.client.get("/v2/nav-tabs", params={"view_as": "member"})
        self.assertIn(response.status_code, (401, 403))
        self.assertNotIn("view_as", response.text)

    def test_removed_user_is_refused_before_view_as(self) -> None:
        self.alice.is_deleted = True
        self.db.commit()
        response = self.client.get(
            "/v2/nav-tabs", params={"view_as": "member:progA"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"]["code"], "user_removed")

    def test_new_user_is_provisioned_before_view_as(self) -> None:
        self.email = "newcomer@renphil.org"
        response = self.client.get("/v2/nav-tabs", params={"view_as": "public:hub"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["view_as"], True)
        self.assertIsNotNone(view_as_service.resolve_user_id(self.db, "newcomer@renphil.org"))

    def test_nav_tab_tabs_workspace_and_children(self) -> None:
        member_a = {"view_as": "member:progA"}
        member_a1 = {"view_as": "member:progA1"}
        public = {"view_as": "public:hub"}

        response = self.client.get("/v2/nav-tabs/nav-n1/tabs")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([r["documentId"] for r in response.json()["data"]], ["tab-1"])
        self.assertIs(response.json()["view_as"], False)

        # A grant on (Program Member, Program A) is not reached from the narrower Program A1.
        self.assertEqual(self.client.get("/v2/nav-tabs/nav-n1/tabs", params=member_a1).status_code, 404)
        self.assertEqual(self.client.get("/v2/nav-tabs/nav-n1/tabs", params=public).status_code, 404)
        self.assertEqual(self.client.get("/v2/tabs/tab-1/workspace", params=public).status_code, 404)

        response = self.client.get("/v2/nav-tabs/nav-n1/tabs", params=member_a)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([r["documentId"] for r in response.json()["data"]], ["tab-1"])
        self.assertIs(response.json()["view_as"], True)

        response = self.client.get("/v2/tabs/tab-1/workspace", params=member_a)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["documentId"], "tab-1")
        self.assertIs(response.json()["view_as"], True)

        response = self.client.get("/v2/tabs/tab-1/workspace")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["view_as"], False)

        response = self.client.get("/v2/tabs/tab-1/children", params=member_a)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"data": [], "view_as": True, "view_as_user": None})

    def test_variants(self) -> None:
        variant = TabV2(
            document_id="tab-1-v", title="T1 variant", order=0, access_control={}, locked=False, locked_by="",
            nav_tab_id=self.navs["n1"].id, parent_tab_id=self.tab.id,
        )
        self.db.add(variant)
        self.db.flush()
        self.db.add(
            GridstackV2(document_id="tab-1-v", name="T1 variant", settings={}, position=0, parent_id=None, parent_tab_id=variant.id)
        )
        self.db.commit()

        response = self.client.get("/v2/tabs/tab-1/variants")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([r["documentId"] for r in response.json()["data"]], ["tab-1-v"])
        self.assertIs(response.json()["view_as"], False)

        response = self.client.get(
            "/v2/tabs/tab-1/variants", params={"view_as": "member:progA"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([r["documentId"] for r in response.json()["data"]], ["tab-1-v"])
        self.assertIs(response.json()["view_as"], True)

        response = self.client.get("/v2/tabs/tab-1/variants", params={"view_as": "public:hub"})
        self.assertEqual(response.status_code, 404, response.text)

        response = self.client.get("/v2/tabs/tab-1/variants", params={"view_as": ":hub"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(_pair_codes(response), ["view_as_malformed_pair"])

    def test_hub_admin_viewing_as_hub_admin_keeps_full_access(self) -> None:
        self.email = "admin@renphil.org"
        response = self.client.get("/v2/nav-tabs", params={"view_as": "hub_admin:all"})
        self.assertEqual(self._nav_titles(response), {"N1", "N2", "N3"})
        self.assertIs(response.json()["hub_edit"], True)
        self.assertIs(response.json()["view_as"], True)

        response = self.client.get("/v2/nav-tabs", params={"view_as": "member:progA"})
        self.assertEqual(self._nav_titles(response), {"N1"})
        self.assertIs(response.json()["hub_edit"], False)


class ThreadModerationSummaryViewAsTests(_ViewAsHub):
    """Five thread widgets under T1, each with one pending thread and one
    pending revision, edit-granted to:
      c-member-a  (Program Member, Program A)
      c-member-a1 (Program Member, Program A1)   — child scope
      c-lead-org  (Program Lead, Org)            — parent role, parent scope
      c-public    (User, Hub)                    — public role, public scope
      c-direct    Alice directly                 — user grant
    """

    PATHS = ("/data/threads/moderation/summary", "/data/threads/revision-moderation/summary")

    def setUp(self) -> None:
        super().setUp()
        principals = {
            "c-member-a": dict(role_id=self.role["member"].id, scope_id=self.scope["progA"].id),
            "c-member-a1": dict(role_id=self.role["member"].id, scope_id=self.scope["progA1"].id),
            "c-lead-org": dict(role_id=self.role["lead"].id, scope_id=self.scope["org"].id),
            "c-public": dict(role_id=self.role["public"].id, scope_id=self.scope["hub"].id),
            "c-direct": dict(user_id=self.alice.id),
        }
        for link, principal in principals.items():
            component = ComponentV2(link=link, title=link, type="thread", gridstack_id=self.grid.id)
            self.db.add(component)
            self.db.flush()
            self.db.add(ResourceGrantV2(component_id=component.id, level="edit", created_at=NOW, **principal))
            thread = ThreadV2(
                component_id=component.id, title=link, content="x", author_email="someone@renphil.org",
                mentions=[], status="pending", created_at=NOW,
            )
            self.db.add(thread)
            self.db.flush()
            self.db.add(
                ThreadRevisionV2(
                    thread_id=thread.id, version=2, title=link, content="y", mentions=[], status="pending",
                    origin="author", submitted_by_email="someone@renphil.org", submitted_at=NOW,
                )
            )
        self.db.commit()

    def _summary(self, path, params=None):
        response = self.client.get(path, params=params or {})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        return body, response.headers["ETag"]

    def test_regular_flow_counts_role_and_direct_grants(self) -> None:
        for path in self.PATHS:
            with self.subTest(path=path):
                body, etag = self._summary(path)
                self.assertEqual(
                    body,
                    {"can_moderate": True, "moderated_component_count": 5, "pending_count": 5, "view_as": False, "view_as_user": None},
                )
                self.assertNotIn("-va", etag)

    def test_view_as_pairs_count_their_grants_plus_own_direct_ones(self) -> None:
        # Each count includes c-direct, which is shared directly with Alice.
        cases = [
            # {member, User} x {Program A, Program A1, Hub}
            ({"view_as": "member:progA"}, 4),
            # {member, User} x {Program A1, Hub}
            ({"view_as": "member:progA1"}, 3),
            # {lead, member, User} x {Org, Program A, Program A1, Hub}
            ({"view_as": "lead:org"}, 5),
            # {User} x {Hub}
            ({"view_as": "public:hub"}, 2),
        ]
        for path in self.PATHS:
            for params, expected in cases:
                with self.subTest(path=path, params=params):
                    body, etag = self._summary(path, params)
                    self.assertEqual(
                        body,
                        {
                            "can_moderate": True,
                            "moderated_component_count": expected,
                            "pending_count": expected,
                            "view_as": True,
                            "view_as_user": None,
                        },
                    )
                    self.assertTrue(etag.endswith('-va"'), etag)

    def test_anonymous_drops_own_direct_grants(self) -> None:
        cases = [
            ({"anonymous": "true"}, 4, False),  # (lead, org) reaches four; c-direct dropped
            ({"anonymous": "true", "view_as": "member:progA"}, 3, True),
            ({"anonymous": "true", "view_as": "public:hub"}, 1, True),
        ]
        for path in self.PATHS:
            for params, expected, view_as in cases:
                with self.subTest(path=path, params=params):
                    body, _ = self._summary(path, params)
                    self.assertEqual(
                        body,
                        {"can_moderate": True, "moderated_component_count": expected, "pending_count": expected,
                         "view_as": view_as, "view_as_user": None},
                    )

    def test_view_as_with_no_editable_widget(self) -> None:
        self.db.query(ResourceGrantV2).filter(
            (ResourceGrantV2.role_id == self.role["public"].id) | (ResourceGrantV2.user_id == self.alice.id)
        ).delete()
        self.db.commit()
        for path in self.PATHS:
            with self.subTest(path=path):
                body, etag = self._summary(path, {"view_as": "public:hub"})
                self.assertEqual(
                    body, {"can_moderate": False, "moderated_component_count": 0, "pending_count": 0, "view_as": True, "view_as_user": None}
                )
                self.assertEqual(etag, '"0-0-0-0-va"')

    def test_etag_of_regular_response_does_not_304_a_view_as_request(self) -> None:
        params = {"view_as": "lead:org"}
        for path in self.PATHS:
            with self.subTest(path=path):
                _body, regular_etag = self._summary(path)
                response = self.client.get(path, params=params, headers={"If-None-Match": regular_etag})
                self.assertEqual(response.status_code, 200)
                view_as_etag = response.headers["ETag"]
                response = self.client.get(path, params=params, headers={"If-None-Match": view_as_etag})
                self.assertEqual(response.status_code, 304)

    def test_view_as_user_ignores_their_direct_grants_and_has_its_own_etag(self) -> None:
        self._user("Dave", work="dave@renphil.org", holds=[("member", "progA")])
        self.db.commit()
        for path in self.PATHS:
            with self.subTest(path=path):
                # Alice: (lead, org) reaches four widgets; c-direct, shared with her directly, is left out.
                self.email = "admin@renphil.org"
                body, _ = self._summary(path, {"view_as_user": "alice@renphil.org"})
                self.assertEqual(
                    body,
                    {"can_moderate": True, "moderated_component_count": 4, "pending_count": 4,
                     "view_as": True, "view_as_user": "alice@renphil.org"},
                )

                # Dave's body equals the (member, progA) body except for
                # view_as_user. Asked by the admin, who has no direct grants.
                pair_body, pair_etag = self._summary(path, {"view_as": "member:progA"})
                user_body, user_etag = self._summary(path, {"view_as_user": "dave@renphil.org"})
                self.assertEqual({**pair_body, "view_as_user": "dave@renphil.org"}, user_body)
                self.assertNotEqual(pair_etag, user_etag)
                self.assertNotIn("dave", user_etag)
                response = self.client.get(
                    path, params={"view_as_user": "dave@renphil.org"}, headers={"If-None-Match": pair_etag}
                )
                self.assertEqual(response.status_code, 200)

    def test_view_as_errors(self) -> None:
        cases = [
            ({"view_as": "member"}, 400, "view_as_malformed_pair"),
            ({"view_as": ":progA"}, 400, "view_as_malformed_pair"),
            ({"view_as": "lead:progB"}, 403, "view_as_scope_not_allowed"),
            ({"view_as": "hub_admin:org"}, 403, "view_as_role_not_allowed"),
            ({"view_as": "Nope:org"}, 400, "view_as_role_not_found"),
        ]
        for path in self.PATHS:
            for params, status, code in cases:
                with self.subTest(path=path, code=code, params=params):
                    response = self.client.get(path, params=params)
                    self.assertEqual(response.status_code, status, response.text)
                    self.assertEqual(_pair_codes(response), [code])


class _ViewAsAssignments(_ViewAsHub):
    """Besides the base fixture's assignments: dave (member, progA),
    erin (member, progA1), frank (public, hub)."""

    def setUp(self) -> None:
        super().setUp()
        self.dave = self._user("Dave", work="dave@renphil.org", holds=[("member", "progA")])
        self.erin = self._user("Erin", work="erin@renphil.org", holds=[("member", "progA1")])
        self._user("Frank", work="frank@renphil.org", holds=[("public", "hub")])
        self.db.commit()

    def _row(self, email, role, scope):
        return (email, self.role[role].id, self.scope[scope].id)


class RevocableViewAsTests(_ViewAsAssignments):
    PATH = "/v2/rbac/me/revocable"

    def _revocable(self, params=None):
        response = self.client.get(self.PATH, params=params or {})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        pairs = {(row["user_email"], row["role_id"], row["scope_id"]) for row in body["data"]}
        return pairs, body["view_as"]

    def test_regular_flow_unchanged(self) -> None:
        pairs, view_as = self._revocable()
        self.assertIs(view_as, False)
        self.assertEqual(
            pairs,
            {
                self._row("dave@renphil.org", "member", "progA"),
                self._row("erin@renphil.org", "member", "progA1"),
                self._row("frank@renphil.org", "public", "hub"),
            },
        )

    def test_view_as_uses_only_the_adopted_pair(self) -> None:
        cases = [
            ({"view_as": "lead:progA1"}, {
                self._row("erin@renphil.org", "member", "progA1"),
                self._row("frank@renphil.org", "public", "hub"),
            }),
            ({"view_as": "member:progA1"}, {
                self._row("frank@renphil.org", "public", "hub"),
            }),
            ({"view_as": "public:hub"}, set()),
        ]
        for params, expected in cases:
            with self.subTest(params=params):
                with self.assertLogs("app.dependencies", level=logging.INFO) as logs:
                    pairs, view_as = self._revocable(params)
                self.assertIs(view_as, True)
                self.assertEqual(pairs, expected)
                self.assertTrue(any("View As verified" in line for line in logs.output))

    def test_hub_admin_loses_bypass_when_viewing_as_a_weaker_pair(self) -> None:
        self.email = "admin@renphil.org"
        everything, _ = self._revocable()
        self.assertEqual(len(everything), self.db.query(RoleAssignmentV2).count())

        pairs, _ = self._revocable({"view_as": "hub_admin:all"})
        self.assertEqual(pairs, everything)

        pairs, _ = self._revocable({"view_as": "member:progA"})
        self.assertEqual(pairs, {self._row("frank@renphil.org", "public", "hub")})

    def test_view_as_errors(self) -> None:
        cases = [
            ({"view_as": "member"}, 400, "view_as_malformed_pair"),
            ({"view_as": "lead:progB"}, 403, "view_as_scope_not_allowed"),
            ({"view_as": "hub_admin:org"}, 403, "view_as_role_not_allowed"),
            ({"view_as": "nope:org"}, 400, "view_as_role_not_found"),
        ]
        for params, status, code in cases:
            with self.subTest(code=code):
                with self.assertLogs("app.dependencies", level=logging.WARNING):
                    response = self.client.get(self.PATH, params=params)
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(_pair_codes(response), [code])


class AssignmentWriteViewAsTests(_ViewAsAssignments):
    """Alice really holds (lead, org): she may grant/revoke member on Org,
    Program A or Program A1. Viewing as (lead, progA1) narrows that to
    Program A1 only."""

    NARROW = {"view_as": "lead:progA1"}

    def _grant(self, role, scope, params=None, email="bob@renphil.org"):
        return self.client.post(
            "/v2/rbac/assignments",
            params=params or {},
            json={"user_email": email, "role_id": self.role[role].id, "scope_id": self.scope[scope].id},
        )

    def _assignment_id(self, user, role, scope):
        return (
            self.db.query(RoleAssignmentV2.id)
            .filter_by(user_id=user.id, role_id=self.role[role].id, scope_id=self.scope[scope].id)
            .scalar()
        )

    def _revoke(self, assignment_id, params=None):
        return self.client.delete(f"/v2/rbac/assignments/{assignment_id}", params=params or {})

    def test_grant_regular_flow_unchanged(self) -> None:
        response = self._grant("member", "progA")
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIs(response.json()["view_as"], False)

    def test_grant_checked_against_adopted_pair(self) -> None:
        response = self._grant("member", "progA", self.NARROW)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "not_delegable")
        self.assertIsNone(self._assignment_id(self.bob, "member", "progA"))

        response = self._grant("member", "progA1", self.NARROW)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertIs(body["view_as"], True)
        self.assertEqual(body["data"]["granted_by_email"], "alice@renphil.org")

    def test_grant_adopted_role_itself_is_not_delegable(self) -> None:
        response = self._grant("lead", "progA1", self.NARROW)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "not_delegable")

    def test_hub_admin_loses_bypass_when_viewing_as_a_weaker_pair(self) -> None:
        self.email = "admin@renphil.org"
        response = self._grant("outsider", "progB", {"view_as": "member:progA"})
        self.assertEqual(response.status_code, 409, response.text)
        response = self._grant("outsider", "progB", {"view_as": "hub_admin:all"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIs(response.json()["view_as"], True)

    def test_revoke_regular_flow_unchanged(self) -> None:
        response = self._revoke(self._assignment_id(self.dave, "member", "progA"))
        self.assertEqual(response.status_code, 204, response.text)
        self.assertIsNone(self._assignment_id(self.dave, "member", "progA"))

    def test_revoke_checked_against_adopted_pair(self) -> None:
        dave_row = self._assignment_id(self.dave, "member", "progA")
        response = self._revoke(dave_row, self.NARROW)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "not_delegable")
        self.assertIsNotNone(self._assignment_id(self.dave, "member", "progA"))

        response = self._revoke(self._assignment_id(self.erin, "member", "progA1"), self.NARROW)
        self.assertEqual(response.status_code, 204, response.text)
        self.assertIsNone(self._assignment_id(self.erin, "member", "progA1"))

    def test_view_as_errors_block_the_write(self) -> None:
        dave_row = self._assignment_id(self.dave, "member", "progA")
        cases = [
            ({"view_as": "lead"}, 400, "view_as_malformed_pair"),
            ({"view_as": "lead:progB"}, 403, "view_as_scope_not_allowed"),
            ({"view_as": "hub_admin:org"}, 403, "view_as_role_not_allowed"),
            ({"view_as": "lead:nope"}, 400, "view_as_scope_not_found"),
        ]
        for params, status, code in cases:
            with self.subTest(code=code):
                with self.assertLogs("app.dependencies", level=logging.WARNING):
                    response = self._grant("member", "progA1", params)
                self.assertEqual((response.status_code, _pair_codes(response)), (status, [code]))
                with self.assertLogs("app.dependencies", level=logging.WARNING):
                    response = self._revoke(dave_row, params)
                self.assertEqual((response.status_code, _pair_codes(response)), (status, [code]))
        self.assertIsNone(self._assignment_id(self.bob, "member", "progA1"))
        self.assertIsNotNone(self._assignment_id(self.dave, "member", "progA"))


class VerifyViewAsPairsTests(_ViewAsDb):
    def _verify(self, user, values):
        return view_as_service.verify_view_as_pairs(self.db, user.id, view_as_service.normalize_pair_values(values))

    def test_several_valid_pairs(self) -> None:
        targets = self._verify(self.carol, ["lead:progA", "member:progB"])
        self.assertEqual(
            [(t.role_key, t.scope_key) for t in targets], [("lead", "progA"), ("member", "progB")]
        )

    def test_duplicates_collapse(self) -> None:
        targets = self._verify(self.alice, ["member:progA", " member : progA ", "member:progA", "  "])
        self.assertEqual([(t.role_key, t.scope_key) for t in targets], [("member", "progA")])

    def test_blank_values_mean_not_requested(self) -> None:
        self.assertEqual(view_as_service.normalize_pair_values(["", "  "]), [])
        self.assertEqual(view_as_service.normalize_pair_values(None), [])

    def test_malformed_values(self) -> None:
        for value in ("lead", "lead:", ":org", "lead:org:x", " : "):
            with self.subTest(value=value):
                with self.assertRaises(view_as_service.ViewAsPairsError) as ctx:
                    self._verify(self.alice, [value])
                self.assertEqual(ctx.exception.status_code, 400)
                self.assertEqual([f["code"] for f in ctx.exception.failures], ["view_as_malformed_pair"])

    def test_every_failing_pair_is_reported(self) -> None:
        with self.assertRaises(view_as_service.ViewAsPairsError) as ctx:
            self._verify(self.alice, ["lead:progB", "member:progA", "hub_admin:org"])
        error = ctx.exception
        self.assertEqual((error.status_code, error.code), (403, "view_as_pairs_not_allowed"))
        self.assertEqual(
            [(f["pair"], f["role_key"], f["scope_key"], f["code"]) for f in error.failures],
            [
                ("lead:progB", "lead", "progB", "view_as_scope_not_allowed"),
                ("hub_admin:org", "hub_admin", "org", "view_as_role_not_allowed"),
            ],
        )
        self.assertIn("'lead:progB'", error.message)
        self.assertIn("'hub_admin:org'", error.message)

    def test_any_400_failure_makes_the_whole_request_400(self) -> None:
        with self.assertRaises(view_as_service.ViewAsPairsError) as ctx:
            self._verify(self.alice, ["lead:progB", "bogus"])
        self.assertEqual((ctx.exception.status_code, ctx.exception.code), (400, "view_as_invalid_pairs"))
        self.assertEqual(
            [f["code"] for f in ctx.exception.failures], ["view_as_scope_not_allowed", "view_as_malformed_pair"]
        )

    def test_too_many_pairs(self) -> None:
        with self.assertRaises(view_as_service.ViewAsPairsError) as ctx:
            view_as_service.verify_view_as_pairs(self.db, self.alice.id, [f"x{i}:y" for i in range(21)])
        self.assertEqual((ctx.exception.status_code, ctx.exception.code), (400, "view_as_too_many_pairs"))


class MultiPairViewAsEndpointTests(_ViewAsAssignments):
    """Carol really holds (lead, progA) and (member, progB). N4 is granted to
    (lead, progB) — reachable only by mixing her two pairs — and N5 to
    (member, progB)."""

    CAROL_BOTH = {"view_as": ["lead:progA", "member:progB"]}

    def setUp(self) -> None:
        super().setUp()
        for i, (slug, role, scope) in enumerate([("n4", "lead", "progB"), ("n5", "member", "progB")], start=3):
            nav = NavTabV2(document_id=f"nav-{slug}", slug=slug, title=slug.upper(), order=i, protected=False)
            self.db.add(nav)
            self.db.flush()
            self.db.add(
                ResourceGrantV2(
                    nav_tab_id=nav.id, role_id=self.role[role].id, scope_id=self.scope[scope].id,
                    level="view", created_at=NOW,
                )
            )
        self.db.commit()
        self.email = "carol@renphil.org"

    def test_content_is_the_union_of_each_pair_never_a_cross(self) -> None:
        cases = [
            ({"view_as": "lead:progA"}, {"N1"}),
            ({"view_as": "member:progB"}, {"N5"}),
            (self.CAROL_BOTH, {"N1", "N5"}),
        ]
        for params, expected in cases:
            with self.subTest(params=params):
                with self.assertLogs("app.dependencies", level=logging.INFO) as logs:
                    response = self.client.get("/v2/nav-tabs", params=params)
                self.assertEqual(self._nav_titles(response), expected)
                self.assertIs(response.json()["view_as"], True)
                verified = [line for line in logs.output if "View As verified" in line]
                self.assertEqual(len(verified), 1)
                for value in [params["view_as"]] if isinstance(params["view_as"], str) else params["view_as"]:
                    role_key, scope_key = value.split(":")
                    self.assertIn(f"role '{role_key}'", verified[0])
                    self.assertIn(f"scope '{scope_key}'", verified[0])

    def test_revocable_uses_each_pair_on_its_own(self) -> None:
        response = self.client.get("/v2/rbac/me/revocable", params=self.CAROL_BOTH)
        self.assertEqual(response.status_code, 200, response.text)
        pairs = {(r["user_email"], r["role_id"], r["scope_id"]) for r in response.json()["data"]}
        self.assertEqual(
            pairs,
            {
                self._row("dave@renphil.org", "member", "progA"),
                self._row("erin@renphil.org", "member", "progA1"),
                self._row("frank@renphil.org", "public", "hub"),
            },
        )
        # Bob's (member, progB) would need lead from one pair and progB from the other.
        self.assertNotIn(self._row("bob@renphil.org", "member", "progB"), pairs)

    def test_grant_needs_one_pair_to_cover_it(self) -> None:
        def grant(role, scope, params):
            return self.client.post(
                "/v2/rbac/assignments",
                params=params,
                json={"user_email": "nobody@renphil.org", "role_id": self.role[role].id, "scope_id": self.scope[scope].id},
            )

        response = grant("member", "progB", self.CAROL_BOTH)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "not_delegable")

        response = grant("member", "progA1", self.CAROL_BOTH)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIs(response.json()["view_as"], True)

    def test_any_hub_admin_pair_gives_full_access(self) -> None:
        self.email = "admin@renphil.org"
        response = self.client.get("/v2/nav-tabs", params={"view_as": ["member:progA", "hub_admin:all"]})
        self.assertEqual(self._nav_titles(response), {"N1", "N2", "N3", "N4", "N5"})
        self.assertIs(response.json()["hub_edit"], True)

    def test_error_lists_every_blocking_pair(self) -> None:
        self.email = "alice@renphil.org"
        params = {"view_as": ["lead:progB", "member:progA", "hub_admin:org", "bogus"]}
        for method, path in (
            ("get", "/v2/nav-tabs"),
            ("get", "/data/threads/moderation/summary"),
            ("get", "/v2/rbac/me/revocable"),
            ("delete", f"/v2/rbac/assignments/{self.db.query(RoleAssignmentV2.id).first()[0]}"),
        ):
            with self.subTest(path=path):
                with self.assertLogs("app.dependencies", level=logging.WARNING) as logs:
                    response = getattr(self.client, method)(path, params=params)
                self.assertEqual(response.status_code, 400, response.text)
                detail = response.json()["detail"]
                self.assertEqual(detail["code"], "view_as_invalid_pairs")
                self.assertEqual(
                    [(p["pair"], p["code"]) for p in detail["pairs"]],
                    [
                        ("lead:progB", "view_as_scope_not_allowed"),
                        ("hub_admin:org", "view_as_role_not_allowed"),
                        ("bogus", "view_as_malformed_pair"),
                    ],
                )
                self.assertTrue(any("'lead:progB'" in line and "'bogus'" in line for line in logs.output))

        response = self.client.get("/v2/nav-tabs", params={"view_as": ["lead:progB", "hub_admin:org"]})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "view_as_pairs_not_allowed")


class ViewAsUserTests(_ViewAsAssignments):
    """Alice really holds (lead, org). Dave holds (member, progA) and N6 is
    shared directly with him; carol's (member, progB) row is beyond Alice's
    reach, so Alice can never select Carol."""

    def setUp(self) -> None:
        super().setUp()
        nav = NavTabV2(document_id="nav-n6", slug="n6", title="N6", order=6, protected=False)
        self.db.add(nav)
        self.db.flush()
        self.db.add(ResourceGrantV2(nav_tab_id=nav.id, user_id=self.dave.id, level="view", created_at=NOW))
        self.db.commit()

    def _users(self):
        response = self.client.get("/v2/view-as/users")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    # -- the picker ----------------------------------------------------

    def test_picker_only_offers_users_entirely_within_reach(self) -> None:
        data = self._users()
        self.assertEqual(
            [u["email"] for u in data],
            ["dave@renphil.org", "erin@renphil.org", "frank@renphil.org", "nobody@renphil.org"],
        )
        self.assertEqual(
            data[0],
            {
                "id": self.dave.id,
                "name": "Dave",
                "email": "dave@renphil.org",
                "assignments": [{"role_key": "member", "scope_key": "progA"}],
            },
        )
        self.assertEqual(data[3]["assignments"], [])

    def test_picker_for_a_caller_with_two_rows(self) -> None:
        self.email = "carol@renphil.org"
        self.assertEqual(
            {u["email"] for u in self._users()},
            {"bob@renphil.org", "dave@renphil.org", "erin@renphil.org", "frank@renphil.org", "nobody@renphil.org"},
        )

    def test_picker_for_hub_admin_lists_everyone_else(self) -> None:
        self.email = "admin@renphil.org"
        self.assertEqual(
            {u["email"] for u in self._users()},
            {f"{n}@renphil.org" for n in ("alice", "bob", "carol", "dave", "erin", "frank", "nobody")},
        )

    def test_picker_skips_deleted_and_emailless_users(self) -> None:
        self._user("No Email", holds=[])
        self.dave.is_deleted = True
        self.db.commit()
        self.assertEqual(
            [u["email"] for u in self._users()], ["erin@renphil.org", "frank@renphil.org", "nobody@renphil.org"]
        )

    def test_every_picked_user_is_accepted(self) -> None:
        for user in self._users():
            with self.subTest(email=user["email"]):
                response = self.client.get("/v2/nav-tabs", params={"view_as_user": user["email"]})
                self.assertEqual(response.status_code, 200, response.text)

    # -- content -------------------------------------------------------

    def test_sees_the_selected_users_assignments_without_their_direct_grants(self) -> None:
        # N6 is shared directly with Dave: left out.
        with self.assertLogs("app.dependencies", level=logging.INFO) as logs:
            response = self.client.get("/v2/nav-tabs", params={"view_as_user": "  DAVE@renphil.org "})
        self.assertEqual(self._nav_titles(response), {"N1"})
        body = response.json()
        self.assertIs(body["view_as"], True)
        self.assertEqual(body["view_as_user"], "dave@renphil.org")
        self.assertTrue(any("View As verified" in line and "'dave@renphil.org'" in line for line in logs.output))

    def test_user_combined_with_pairs_is_the_union(self) -> None:
        response = self.client.get("/v2/nav-tabs", params={"view_as_user": "dave@renphil.org", "view_as": "lead:org"})
        self.assertEqual(self._nav_titles(response), {"N1", "N2"})

        response = self.client.get("/v2/nav-tabs", params={"view_as_user": "nobody@renphil.org"})
        self.assertEqual(self._nav_titles(response), set())
        response = self.client.get(
            "/v2/nav-tabs", params={"view_as_user": "nobody@renphil.org", "view_as": "member:progA"}
        )
        self.assertEqual(self._nav_titles(response), {"N1"})

    def test_duplicate_pairs_collapse(self) -> None:
        selection = view_as_service.resolve_view_as_selection(
            self.db, self.alice.id, ["member:progA"], "dave@renphil.org"
        )
        self.assertEqual([(t.role_key, t.scope_key) for t in selection.targets], [("member", "progA")])
        self.assertEqual(selection.user_email, "dave@renphil.org")

    def test_alias_email_selects_the_user(self) -> None:
        self.email = "carol@renphil.org"
        response = self.client.get("/v2/nav-tabs", params={"view_as_user": "bob.alias@renphil.org"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["view_as_user"], "bob.alias@renphil.org")

    def test_every_view_as_endpoint_echoes_the_user(self) -> None:
        params = {"view_as_user": "dave@renphil.org"}
        for path in (
            "/v2/nav-tabs",
            "/v2/nav-tabs/nav-n1/tabs",
            "/v2/tabs/tab-1/workspace",
            "/v2/tabs/tab-1/children",
            "/v2/tabs/tab-1/variants",
            "/data/threads/moderation/summary",
            "/data/threads/revision-moderation/summary",
            "/v2/rbac/me/revocable",
        ):
            with self.subTest(path=path):
                response = self.client.get(path, params=params)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual((response.json()["view_as"], response.json()["view_as_user"]), (True, "dave@renphil.org"))

    # -- errors --------------------------------------------------------

    def test_more_powerful_user_is_refused_and_named(self) -> None:
        with self.assertLogs("app.dependencies", level=logging.WARNING) as logs:
            response = self.client.get("/v2/nav-tabs", params={"view_as_user": "carol@renphil.org"})
        self.assertEqual(response.status_code, 403, response.text)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "view_as_user_not_allowed")
        self.assertEqual(detail["view_as_user"], "carol@renphil.org")
        self.assertEqual(
            [(p["source"], p["pair"], p["code"]) for p in detail["pairs"]],
            [("view_as_user", "member:progB", "view_as_scope_not_allowed")],
        )
        self.assertIn("selected user 'carol@renphil.org'", detail["message"])
        self.assertIn("selected user 'carol@renphil.org'", detail["pairs"][0]["message"])
        self.assertTrue(any("view_as_user 'member:progB'" in line for line in logs.output))

    def test_unknown_or_deleted_user(self) -> None:
        self.dave.is_deleted = True
        self.db.commit()
        for email in ("ghost@renphil.org", "dave@renphil.org"):
            with self.subTest(email=email):
                response = self.client.get("/v2/nav-tabs", params={"view_as_user": email})
                self.assertEqual(response.status_code, 400, response.text)
                detail = response.json()["detail"]
                self.assertEqual(detail["code"], "view_as_user_not_found")
                self.assertEqual([(p["source"], p["code"]) for p in detail["pairs"]], [("view_as_user", "view_as_user_not_found")])

    def test_user_and_pair_failures_are_reported_together(self) -> None:
        response = self.client.get(
            "/v2/nav-tabs", params={"view_as_user": "carol@renphil.org", "view_as": ["lead:progB", "member:progA"]}
        )
        self.assertEqual(response.status_code, 403, response.text)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "view_as_not_allowed")
        self.assertEqual(
            [(p["source"], p["pair"]) for p in detail["pairs"]],
            [("view_as_user", "member:progB"), ("view_as", "lead:progB")],
        )

        response = self.client.get("/v2/nav-tabs", params={"view_as_user": "ghost@renphil.org", "view_as": "bogus"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "view_as_invalid")

    def test_blank_user_means_not_requested(self) -> None:
        response = self.client.get("/v2/nav-tabs", params={"view_as_user": "   "})
        self.assertEqual(self._nav_titles(response), {"N1", "N2", "N3"})
        self.assertEqual((response.json()["view_as"], response.json()["view_as_user"]), (False, None))

    # -- assignments ---------------------------------------------------

    def test_delegation_uses_the_selected_users_rows(self) -> None:
        response = self.client.get("/v2/rbac/me/revocable", params={"view_as_user": "dave@renphil.org"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            {(r["user_email"], r["role_id"], r["scope_id"]) for r in response.json()["data"]},
            {self._row("frank@renphil.org", "public", "hub")},
        )

        def grant(params):
            return self.client.post(
                "/v2/rbac/assignments",
                params=params,
                json={"user_email": "bob@renphil.org", "role_id": self.role["member"].id, "scope_id": self.scope["progA1"].id},
            )

        response = grant({"view_as_user": "dave@renphil.org"})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "not_delegable")

        response = grant({"view_as_user": "dave@renphil.org", "view_as": "lead:progA"})
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual((body["view_as"], body["view_as_user"]), (True, "dave@renphil.org"))
        self.assertEqual(body["data"]["granted_by_email"], "alice@renphil.org")

    def test_revoke_with_selected_user(self) -> None:
        frank_row = self.db.query(RoleAssignmentV2.id).filter_by(role_id=self.role["public"].id).scalar()
        erin_row = self.db.query(RoleAssignmentV2.id).filter_by(user_id=self.erin.id).scalar()
        response = self.client.delete(f"/v2/rbac/assignments/{erin_row}", params={"view_as_user": "dave@renphil.org"})
        self.assertEqual(response.status_code, 409, response.text)
        response = self.client.delete(f"/v2/rbac/assignments/{frank_row}", params={"view_as_user": "dave@renphil.org"})
        self.assertEqual(response.status_code, 204, response.text)


class _ThreadsFixture(_ViewAsAssignments):
    """Widget W is view-granted to (member, progA). Threads: one approved
    (by someone else), Dave's pending and rejected ones, Alice's pending
    one. Dave voted +1 and Alice -1 on the approved thread."""

    def setUp(self) -> None:
        super().setUp()
        component = ComponentV2(link="w", title="W", type="thread", gridstack_id=self.grid.id)
        self.db.add(component)
        self.db.flush()
        self.db.add(
            ResourceGrantV2(
                component_id=component.id, role_id=self.role["member"].id, scope_id=self.scope["progA"].id,
                level="view", created_at=NOW,
            )
        )
        approved = None
        self.threads: dict[str, ThreadV2] = {}
        for minute, (title, author, status) in enumerate(
            [
                ("approved", "someone@renphil.org", "approved"),
                ("dave-pending", "dave@renphil.org", "pending"),
                ("dave-rejected", "dave@renphil.org", "rejected"),
                ("alice-pending", "alice@renphil.org", "pending"),
            ]
        ):
            thread = ThreadV2(
                component_id=component.id, title=title, content="x", author_email=author, mentions=[],
                status=status, created_at=NOW + timedelta(minutes=minute),
            )
            self.db.add(thread)
            self.db.flush()
            approved = approved or thread
            self.threads[title] = thread
        approved.up_count, approved.down_count = 1, 1
        for voter, value in (("dave@renphil.org", 1), ("alice@renphil.org", -1)):
            self.db.add(ThreadVoteV2(thread_id=approved.id, voter_email=voter, value=value, created_at=NOW, updated_at=NOW))
        self.db.commit()
        self.component = component


class ThreadListViewAsTests(_ThreadsFixture):
    PATH = "/data/threads/component/w"

    def _list(self, params=None):
        response = self.client.get(self.PATH, params=params or {})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        return {item["title"]: item["my_vote"] for item in body["items"]}, body

    def test_regular_flow_is_the_callers(self) -> None:
        # 2026-11-13: approved threads only; Alice's own pending thread is
        # counted (`pending_count`) instead of listed.
        items, body = self._list()
        self.assertEqual(items, {"approved": -1})
        self.assertEqual(body["pending_count"], 1)
        self.assertEqual((body["view_as"], body["view_as_user"]), (False, None))

    def test_view_as_user_marks_nothing_as_theirs(self) -> None:
        items, body = self._list({"view_as_user": "dave@renphil.org"})
        self.assertEqual(items, {"approved": 0})
        self.assertEqual((body["items"][0]["up_count"], body["items"][0]["down_count"]), (1, 1))
        self.assertEqual((body["view_as"], body["view_as_user"]), (True, "dave@renphil.org"))

    def test_anonymous_marks_no_vote_but_keeps_counts(self) -> None:
        items, body = self._list({"anonymous": "true"})
        self.assertEqual(items, {"approved": 0})
        self.assertEqual((body["items"][0]["up_count"], body["items"][0]["down_count"]), (1, 1))
        self.assertEqual((body["view_as"], body["view_as_user"]), (False, None))

    def test_pairs_only_view_keeps_the_callers_own_threads_and_votes(self) -> None:
        items, body = self._list({"view_as": "member:progA"})
        self.assertEqual(items, {"approved": -1})
        self.assertEqual(body["pending_count"], 1)
        self.assertEqual((body["view_as"], body["view_as_user"]), (True, None))

        items, body = self._list({"view_as": "member:progA", "anonymous": "true"})
        self.assertEqual(items, {"approved": 0})
        self.assertEqual(body["pending_count"], 0)
        self.assertEqual((body["view_as"], body["view_as_user"]), (True, None))

    def test_anonymous_with_view_as_user_is_refused(self) -> None:
        with self.assertLogs("app.dependencies", level=logging.WARNING):
            response = self.client.get(self.PATH, params={"view_as_user": "dave@renphil.org", "anonymous": "true"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"]["code"], "view_as_user_with_anonymous")

    def test_access_follows_the_selected_user(self) -> None:
        response = self.client.get(self.PATH, params={"view_as_user": "frank@renphil.org"})
        self.assertEqual(response.status_code, 403, response.text)


class ThreadReadViewAsTests(_ThreadsFixture):
    """On W, besides the base threads: Dave's approved thread "dave-approved"
    (up 1, down 1: Dave +1, Alice -1) with one pending edit by Dave, and one
    comment on it (up 1, down 1: Dave -1, Alice +1). Widget P sits under its
    own ungranted nav tab N7 and is shared directly with Dave only; it holds
    his approved thread "private"."""

    def setUp(self) -> None:
        super().setUp()
        thread = ThreadV2(
            component_id=self.component.id, title="dave-approved", content="x", author_email="dave@renphil.org",
            mentions=[], status="approved", up_count=1, down_count=1, created_at=NOW + timedelta(minutes=10),
        )
        self.db.add(thread)
        self.db.flush()
        self.db.add(
            ThreadRevisionV2(
                thread_id=thread.id, version=2, title="edit", content="y", mentions=[], status="pending",
                origin="author", submitted_by_email="dave@renphil.org", submitted_at=NOW,
            )
        )
        comment = ThreadCommentV2(
            thread_id=thread.id, content="c", author_email="someone@renphil.org", mentions=[],
            up_count=1, down_count=1, created_at=NOW,
        )
        self.db.add(comment)
        self.db.flush()
        for voter, thread_vote, comment_vote in (("dave@renphil.org", 1, -1), ("alice@renphil.org", -1, 1)):
            self.db.add(ThreadVoteV2(thread_id=thread.id, voter_email=voter, value=thread_vote, created_at=NOW, updated_at=NOW))
            self.db.add(ThreadVoteV2(comment_id=comment.id, voter_email=voter, value=comment_vote, created_at=NOW, updated_at=NOW))

        nav = NavTabV2(document_id="nav-n7", slug="n7", title="N7", order=7, protected=False)
        self.db.add(nav)
        self.db.flush()
        tab = TabV2(
            document_id="tab-7", title="T7", order=0, access_control={}, locked=False, locked_by="", nav_tab_id=nav.id
        )
        self.db.add(tab)
        self.db.flush()
        grid = GridstackV2(document_id="tab-7", name="T7", settings={}, position=0, parent_id=None, parent_tab_id=tab.id)
        self.db.add(grid)
        self.db.flush()
        private = ComponentV2(link="p", title="P", type="thread", gridstack_id=grid.id)
        self.db.add(private)
        self.db.flush()
        self.db.add(ResourceGrantV2(component_id=private.id, user_id=self.dave.id, level="view", created_at=NOW))
        private_thread = ThreadV2(
            component_id=private.id, title="private", content="x", author_email="dave@renphil.org",
            mentions=[], status="approved", created_at=NOW,
        )
        self.db.add(private_thread)
        self.db.commit()
        self.thread, self.private_thread = thread, private_thread

    def _detail(self, thread_id, params=None, status=200):
        response = self.client.get(f"/data/threads/{thread_id}", params=params or {})
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def _comments(self, thread_id, params=None, status=200):
        response = self.client.get(f"/data/threads/{thread_id}/comments", params=params or {})
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def test_regular_flow_is_the_callers(self) -> None:
        detail = self._detail(self.thread.id)
        self.assertEqual((detail["my_vote"], detail["up_count"], detail["down_count"]), (-1, 1, 1))
        self.assertEqual((detail["pending_revision_count"], detail["pending_revisions"]), (0, []))
        self.assertEqual((detail["view_as"], detail["view_as_user"]), (False, None))
        comments = self._comments(self.thread.id)
        self.assertEqual([c["my_vote"] for c in comments["items"]], [1])
        self.assertEqual((comments["view_as"], comments["view_as_user"]), (False, None))

    def test_view_as_user_marks_no_vote_and_no_pending_edits(self) -> None:
        params = {"view_as_user": "dave@renphil.org"}
        detail = self._detail(self.thread.id, params)
        self.assertEqual((detail["my_vote"], detail["up_count"], detail["down_count"]), (0, 1, 1))
        self.assertEqual((detail["pending_revision_count"], detail["pending_revisions"]), (0, []))
        self.assertIs(detail["viewer_is_editor"], False)
        self.assertEqual((detail["view_as"], detail["view_as_user"]), (True, "dave@renphil.org"))
        comments = self._comments(self.thread.id, params)
        self.assertEqual(
            [(c["my_vote"], c["up_count"], c["down_count"]) for c in comments["items"]], [(0, 1, 1)]
        )
        self.assertEqual((comments["view_as"], comments["view_as_user"]), (True, "dave@renphil.org"))

    def test_view_as_user_does_not_open_their_own_pending_thread(self) -> None:
        for title in ("dave-pending", "alice-pending"):
            with self.subTest(title=title):
                self._detail(self.threads[title].id, {"view_as_user": "dave@renphil.org"}, status=404)

    def test_editor_status_comes_from_roles_only(self) -> None:
        self.db.add(ResourceGrantV2(component_id=self.component.id, user_id=self.dave.id, level="edit", created_at=NOW))
        self.db.commit()
        params = {"view_as_user": "dave@renphil.org"}
        detail = self._detail(self.thread.id, params)
        self.assertEqual((detail["viewer_is_editor"], detail["pending_revisions"]), (False, []))

        self.db.add(
            ResourceGrantV2(
                component_id=self.component.id, role_id=self.role["member"].id, scope_id=self.scope["progA"].id,
                level="edit", created_at=NOW,
            )
        )
        self.db.commit()
        detail = self._detail(self.thread.id, params)
        self.assertIs(detail["viewer_is_editor"], True)
        # An editor sees the widget's pending edits and pending threads, as under anonymous.
        self.assertEqual(detail["pending_revision_count"], 1)
        self.assertEqual(detail["my_vote"], 0)
        self.assertEqual(self._detail(self.threads["dave-pending"].id, params)["title"], "dave-pending")

    def test_access_follows_the_selected_users_roles_only(self) -> None:
        # P is shared directly with Dave and reached by no role: closed to everyone here.
        params = {"view_as_user": "dave@renphil.org"}
        for get in (self._detail, self._comments):
            with self.subTest(get=get.__name__):
                get(self.private_thread.id, status=403)
                get(self.private_thread.id, params, status=403)
                get(self.thread.id, {"view_as_user": "frank@renphil.org"}, status=403)

    def test_anonymous_marks_nothing_as_the_callers(self) -> None:
        params = {"anonymous": "true"}
        detail = self._detail(self.thread.id, params)
        self.assertEqual((detail["my_vote"], detail["up_count"], detail["down_count"]), (0, 1, 1))
        self.assertEqual((detail["view_as"], detail["view_as_user"]), (False, None))
        comments = self._comments(self.thread.id, params)
        self.assertEqual([(c["my_vote"], c["up_count"], c["down_count"]) for c in comments["items"]], [(0, 1, 1)])
        self._detail(self.threads["alice-pending"].id, status=200)
        self._detail(self.threads["alice-pending"].id, params, status=404)

    def test_pairs_only_view_keeps_the_callers_votes_and_own_threads(self) -> None:
        params = {"view_as": "member:progA"}
        detail = self._detail(self.thread.id, params)
        self.assertEqual((detail["my_vote"], detail["pending_revision_count"]), (-1, 0))
        self.assertEqual((detail["view_as"], detail["view_as_user"]), (True, None))
        comments = self._comments(self.thread.id, params)
        self.assertEqual([c["my_vote"] for c in comments["items"]], [1])
        self.assertEqual(self._detail(self.threads["alice-pending"].id, params)["title"], "alice-pending")

        own = ThreadV2(
            component_id=self.component.id, title="alice-approved", content="x", author_email="alice@renphil.org",
            mentions=[], status="approved", created_at=NOW,
        )
        self.db.add(own)
        self.db.flush()
        self.db.add(
            ThreadRevisionV2(
                thread_id=own.id, version=2, title="edit", content="y", mentions=[], status="pending",
                origin="author", submitted_by_email="alice@renphil.org", submitted_at=NOW,
            )
        )
        self.db.commit()
        self.assertEqual(self._detail(own.id, params)["pending_revision_count"], 1)
        self.assertEqual(self._detail(own.id, {**params, "anonymous": "true"})["pending_revision_count"], 0)

    def test_anonymous_with_view_as_user_is_refused(self) -> None:
        params = {"view_as_user": "dave@renphil.org", "anonymous": "true"}
        for path in (f"/data/threads/{self.thread.id}", f"/data/threads/{self.thread.id}/comments"):
            with self.subTest(path=path):
                with self.assertLogs("app.dependencies", level=logging.WARNING):
                    response = self.client.get(path, params=params)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["detail"]["code"], "view_as_user_with_anonymous")

    def test_view_as_errors(self) -> None:
        response = self.client.get(f"/data/threads/{self.thread.id}", params={"view_as_user": "carol@renphil.org"})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["detail"]["code"], "view_as_user_not_allowed")
        response = self.client.get(f"/data/threads/{self.thread.id}/comments", params={"view_as": "bogus"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"]["code"], "view_as_invalid_pairs")


class AnonymousViewAsTests(_ViewAsAssignments):
    """N3 and widget-free nav tab N8 are shared directly with Alice; N1 and
    N2 reach her through (lead, org)."""

    def setUp(self) -> None:
        super().setUp()
        nav = NavTabV2(document_id="nav-n8", slug="n8", title="N8", order=8, protected=False)
        self.db.add(nav)
        self.db.flush()
        self.db.add(ResourceGrantV2(nav_tab_id=nav.id, user_id=self.alice.id, level="edit", created_at=NOW))
        self.db.commit()

    def test_anonymous_alone_uses_own_assignments_without_direct_shares(self) -> None:
        self.assertEqual(self._nav_titles(self.client.get("/v2/nav-tabs")), {"N1", "N2", "N3", "N8"})
        with self.assertLogs("app.dependencies", level=logging.INFO) as logs:
            response = self.client.get("/v2/nav-tabs", params={"anonymous": "true"})
        self.assertEqual(self._nav_titles(response), {"N1", "N2"})
        self.assertEqual((response.json()["view_as"], response.json()["view_as_user"]), (False, None))
        self.assertTrue(any("Anonymous view requested" in line for line in logs.output))

    def test_anonymous_with_pairs_drops_own_direct_shares(self) -> None:
        response = self.client.get("/v2/nav-tabs", params={"view_as": "member:progA"})
        self.assertEqual(self._nav_titles(response), {"N1", "N3", "N8"})
        response = self.client.get("/v2/nav-tabs", params={"view_as": "member:progA", "anonymous": "true"})
        self.assertEqual(self._nav_titles(response), {"N1"})
        self.assertIs(response.json()["view_as"], True)

    def test_anonymous_drops_edit_from_direct_shares(self) -> None:
        response = self.client.get("/v2/nav-tabs/nav-n8/tabs")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/v2/nav-tabs/nav-n8/tabs", params={"anonymous": "true"}).status_code, 404)

    def test_hub_admin_keeps_full_access(self) -> None:
        self.email = "admin@renphil.org"
        response = self.client.get("/v2/nav-tabs", params={"anonymous": "true"})
        self.assertEqual(self._nav_titles(response), {"N1", "N2", "N3", "N8"})
        self.assertIs(response.json()["hub_edit"], True)

    def test_anonymous_with_view_as_user_is_refused_everywhere(self) -> None:
        params = {"anonymous": "true", "view_as_user": "dave@renphil.org"}
        dave_row = self.db.query(RoleAssignmentV2.id).filter_by(user_id=self.dave.id).scalar()
        requests = [
            ("get", "/v2/nav-tabs", None),
            ("get", "/v2/tabs/tab-1/workspace", None),
            ("get", "/data/threads/moderation/summary", None),
            ("get", "/v2/rbac/me/revocable", None),
            ("post", "/v2/rbac/assignments",
             {"user_email": "bob@renphil.org", "role_id": self.role["member"].id, "scope_id": self.scope["progA1"].id}),
            ("delete", f"/v2/rbac/assignments/{dave_row}", None),
        ]
        for method, path, body in requests:
            with self.subTest(method=method, path=path):
                kwargs = {"params": params, **({"json": body} if body else {})}
                with self.assertLogs("app.dependencies", level=logging.WARNING):
                    response = getattr(self.client, method)(path, **kwargs)
                self.assertEqual(response.status_code, 400, response.text)
                detail = response.json()["detail"]
                self.assertEqual((detail["code"], detail["view_as_user"]), ("view_as_user_with_anonymous", "dave@renphil.org"))
        self.assertIsNotNone(self.db.query(RoleAssignmentV2.id).filter_by(id=dave_row).scalar())
        self.assertIsNone(self.db.query(RoleAssignmentV2.id).filter_by(user_id=self.bob.id, scope_id=self.scope["progA1"].id).scalar())

    def test_assignment_endpoints_are_unaffected(self) -> None:
        for params in ({}, {"view_as": "lead:progA1"}):
            with self.subTest(params=params):
                plain = self.client.get("/v2/rbac/me/revocable", params=params).json()
                anon = self.client.get("/v2/rbac/me/revocable", params={**params, "anonymous": "true"}).json()
                self.assertEqual(plain, anon)

    def test_thread_widget_shared_directly_is_hidden(self) -> None:
        nav = self.db.query(NavTabV2).filter_by(slug="n8").one()
        tab = TabV2(document_id="tab-8", title="T8", order=0, access_control={}, locked=False, locked_by="", nav_tab_id=nav.id)
        self.db.add(tab)
        self.db.flush()
        grid = GridstackV2(document_id="tab-8", name="T8", settings={}, position=0, parent_id=None, parent_tab_id=tab.id)
        self.db.add(grid)
        self.db.flush()
        self.db.add(ComponentV2(link="w8", title="W8", type="thread", gridstack_id=grid.id))
        self.db.commit()
        self.assertEqual(self.client.get("/data/threads/component/w8").status_code, 200)
        self.assertEqual(self.client.get("/data/threads/component/w8", params={"anonymous": "true"}).status_code, 403)


class PersonalEndpointsIgnoreViewAsTests(_ThreadsFixture):
    """`/v2/rbac/me/granted`, the two notification endpoints and
    `/bookmarks/get_bookmarks` always act as the CALLER: they accept no View
    As parameter, so a sent one is ignored (never a refusal, never another
    person's data)."""

    VIEW_AS_PARAMS = (
        {"view_as_user": "dave@renphil.org"},
        {"view_as_user": "carol@renphil.org"},
        {"view_as_user": "ghost@renphil.org"},
        {"view_as": "member:progA"},
        {"anonymous": "true"},
    )

    def setUp(self) -> None:
        super().setUp()
        erin_row = self.db.query(RoleAssignmentV2).filter_by(user_id=self.erin.id).one()
        erin_row.granted_by_user_id, erin_row.granted_by_email = self.alice.id, "alice@renphil.org"
        dave_row = self.db.query(RoleAssignmentV2).filter_by(user_id=self.dave.id).one()
        dave_row.granted_by_user_id, dave_row.granted_by_email = self.carol.id, "carol@renphil.org"
        thread = self.threads["approved"]
        for recipient, excerpt in (("alice@renphil.org", "for-alice"), ("dave@renphil.org", "for-dave")):
            self.db.add(
                NotificationV2(
                    recipient_email=recipient, type="mention", component_id=thread.component_id,
                    thread_id=thread.id, excerpt=excerpt, created_at=NOW,
                )
            )
        for user, link in ((self.alice, "alice-1"), (self.dave, "dave-1")):
            self.db.add(BookmarkV2(user_id=user.id, bookmark_link=link, type="tab", saved_at=NOW))
        self.db.commit()

    def _callers_own(self, params):
        granted = self.client.get("/v2/rbac/me/granted", params=params)
        notifications = self.client.get("/data/notifications", params=params)
        count = self.client.get("/data/notifications/unread-count", params=params)
        bookmarks = self.client.get("/bookmarks/get_bookmarks", params=params)
        for response in (granted, notifications, count, bookmarks):
            self.assertEqual(response.status_code, 200, response.text)
        return {
            "granted": [r["user_email"] for r in granted.json()["data"]],
            "granted_keys": sorted(granted.json()),
            "notifications": [n["excerpt"] for n in notifications.json()["items"]],
            "notifications_keys": sorted(notifications.json()),
            "count": count.json(),
            "bookmarks": [b["bookmark_link"] for b in bookmarks.json()["bookmarks"]],
            "bookmarks_keys": sorted(bookmarks.json()),
            "etag": count.headers["ETag"],
        }

    def test_always_the_callers_own_data(self) -> None:
        expected = {
            "granted": ["erin@renphil.org"],
            "granted_keys": ["data"],
            "notifications": ["for-alice"],
            "notifications_keys": ["items", "next_cursor"],
            "count": {"count": 1},
            "bookmarks": ["alice-1"],
            "bookmarks_keys": ["bookmarks", "user_id"],
        }
        own = self._callers_own({})
        self.assertEqual({k: v for k, v in own.items() if k != "etag"}, expected)
        self.assertNotIn("-va", own["etag"])
        for params in self.VIEW_AS_PARAMS:
            with self.subTest(params=params):
                self.assertEqual(self._callers_own(params), own)

    def test_view_as_params_are_not_declared(self) -> None:
        paths = app.openapi()["paths"]
        for path in ("/v2/rbac/me/granted", "/data/notifications", "/data/notifications/unread-count",
                     "/bookmarks/get_bookmarks"):
            with self.subTest(path=path):
                names = {p["name"] for p in paths[path]["get"].get("parameters", [])}
                self.assertFalse(names & {"view_as", "view_as_user", "anonymous"}, names)


if __name__ == "__main__":
    unittest.main()
