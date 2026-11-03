"""Focused coverage for the Agent's live Hub authorization bridge."""

from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.routers import agent_access
from app.services import agent_identity
from app.services.agent_identity import AgentIdentity


class _Query:
    def __init__(self, result: object) -> None:
        self._result = result

    def filter(self, *_args: object) -> "_Query":
        return self

    def first(self) -> object:
        return self._result


class _Db:
    def __init__(self, hub_user: object) -> None:
        self._hub_user = hub_user

    def query(self, *_args: object) -> _Query:
        return _Query(self._hub_user)


def _signed_identity_headers(*, email: str, roles: list[str]) -> tuple[str, str]:
    payload = {
        "v": 1,
        "iat": int(time.time()),
        "email": email,
        "roles": roles,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(
        b"test-secret",
        b"renphil-airtable-personal-v1:" + encoded.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return encoded, signature


class AgentLiveAclTests(unittest.TestCase):
    def test_agent_identity_accepts_the_existing_signed_agent_context(self) -> None:
        with patch.object(
            agent_identity,
            "get_settings",
            return_value=SimpleNamespace(AGENT_SYNC_TOKEN="test-secret"),
        ):
            encoded, signature = _signed_identity_headers(
                email=" Person@Example.org ",
                roles=["Member", "Member", " Hub Admin "],
            )
            identity = agent_identity.verify_agent_identity(encoded, signature)

        self.assertEqual(
            identity,
            AgentIdentity(
                email="person@example.org",
                roles=("Member", "Hub Admin"),
            ),
        )

    def test_agent_identity_rejects_an_invalid_signature(self) -> None:
        with patch.object(
            agent_identity,
            "get_settings",
            return_value=SimpleNamespace(AGENT_SYNC_TOKEN="test-secret"),
        ):
            encoded, _signature = _signed_identity_headers(
                email="person@example.org",
                roles=[],
            )
            with self.assertRaises(HTTPException) as error:
                agent_identity.verify_agent_identity(encoded, "not-a-valid-signature")

        self.assertEqual(error.exception.status_code, 401)

    def test_live_acl_response_contains_only_current_payload_grants(self) -> None:
        with (
            patch.object(agent_access, "RbacClosures", lambda _db: object()),
            patch.object(agent_access, "is_hub_admin", lambda *_args, **_kwargs: False),
            patch.object(
                agent_access,
                "resolve_viewer_access",
                lambda *_args, **_kwargs: SimpleNamespace(visibility=object()),
            ),
            patch.object(
                agent_access,
                "list_visible_nodes",
                lambda _visibility: [
                    {"node_kind": "component", "node_id": 42, "granted": True},
                    {"node_kind": "tab", "node_id": 5, "granted": False},
                    {"node_kind": "component", "node_id": 8, "granted": True},
                ],
            ),
        ):
            response = agent_access._build_live_acl_response(
                AgentIdentity(email="person@example.org", roles=("Member",)),
                _Db(SimpleNamespace(id=9, has_signed_in=True, name="Person")),
            )

        self.assertEqual(response["email"], "person@example.org")
        self.assertFalse(response["is_admin"])
        self.assertEqual(response["granted_node_keys"], ["component:42", "component:8"])
        self.assertIsInstance(response["authorization_fingerprint"], str)

    def test_live_acl_fingerprint_changes_when_grants_change(self) -> None:
        before = agent_access._authorization_fingerprint(
            email="person@example.org",
            is_admin=False,
            granted_node_keys=["component:8"],
        )
        after = agent_access._authorization_fingerprint(
            email="person@example.org",
            is_admin=False,
            granted_node_keys=["component:42", "component:8"],
        )

        self.assertNotEqual(before, after)

    def test_live_acl_admin_bypass_does_not_emit_a_partial_node_list(self) -> None:
        with (
            patch.object(agent_access, "RbacClosures", lambda _db: object()),
            patch.object(agent_access, "is_hub_admin", lambda *_args, **_kwargs: True),
            patch.object(
                agent_access,
                "resolve_viewer_access",
                lambda *_args, **_kwargs: self.fail("admin must not compute grants"),
            ),
        ):
            response = agent_access._build_live_acl_response(
                AgentIdentity(email="admin@example.org", roles=("Hub Admin",)),
                _Db(SimpleNamespace(id=1, has_signed_in=True, name="Admin")),
            )

        self.assertTrue(response["is_admin"])
        self.assertEqual(response["granted_node_keys"], [])

    def test_verified_live_acl_uses_postgres_roles_not_airtable(self) -> None:
        """Roles come from Neon ``users`` -> ``role_assignments`` -> ``roles``.

        The signed Agent envelope's role snapshot is ignored and Airtable's
        Access Control table is never called.
        """
        hub_user = SimpleNamespace(id=9, has_signed_in=True, name="Person")
        seen: dict[str, object] = {}

        def fake_build(identity, _db, *, hub_user=None, closures=None):
            seen["identity"] = identity
            seen["hub_user"] = hub_user
            return {"email": identity.email, "is_admin": True, "granted_node_keys": [],
                    "authorization_fingerprint": "fp"}

        with (
            patch.object(agent_access, "find_user_by_email", lambda _db, _email: hub_user),
            patch.object(agent_access, "RbacClosures", lambda _db: object()),
            patch.object(agent_access, "_postgres_role_names", lambda _db, user_id, _c: ["Hub Admin"] if user_id == 9 else []),
            patch.object(agent_access, "_build_live_acl_response", fake_build),
        ):
            response = agent_access._build_verified_live_acl_response(
                AgentIdentity(email="person@example.org", roles=("Member",)),
                _Db(hub_user),
            )

        self.assertEqual(response["resolved_roles"], ["Hub Admin"])
        self.assertEqual(seen["identity"].roles, ("Hub Admin",))  # never the JWT "Member"
        self.assertIs(seen["hub_user"], hub_user)
        module_source = inspect.getsource(agent_access)
        self.assertNotIn("get_user_roles", module_source)
        self.assertNotIn("airtable_service", module_source)
        self.assertFalse(inspect.iscoroutinefunction(agent_access.get_verified_granted_nodes))

    def test_postgres_role_names_include_inherited_child_roles(self) -> None:
        class _Closures:
            def role_descendants(self, role_id: int) -> set[int]:
                return {1: {1, 2, 3}, 4: {4}}[role_id]

        class _RoleQuery:
            def __init__(self) -> None:
                self.ids: list[int] = []

            def filter(self, clause) -> "_RoleQuery":
                self.ids = list(clause.right.value)
                return self

            def all(self) -> list[tuple[str]]:
                names = {1: "Hub Admin", 2: "Program Lead", 3: "Hub Member", 4: "Hub Member"}
                return [(names[i],) for i in self.ids]

        class _RoleDb:
            def query(self, *_args: object) -> _RoleQuery:
                return _RoleQuery()

        with patch.object(agent_access, "held_assignments", lambda _db, _uid: [(1, 10), (4, 11)]):
            names = agent_access._postgres_role_names(_RoleDb(), 9, _Closures())
        self.assertEqual(names, ["Hub Admin", "Hub Member", "Program Lead"])

        with patch.object(agent_access, "held_assignments", lambda _db, _uid: []):
            self.assertEqual(agent_access._postgres_role_names(_RoleDb(), 9, _Closures()), [])

    def test_verified_live_acl_refuses_unknown_or_never_signed_in_users(self) -> None:
        for hub_user in (None, SimpleNamespace(id=9, has_signed_in=False, name="Person")):
            with patch.object(agent_access, "find_user_by_email", lambda _db, _email: hub_user):
                with self.assertRaises(HTTPException) as error:
                    agent_access._build_verified_live_acl_response(
                        AgentIdentity(email="person@example.org", roles=("Hub Admin",)),
                        _Db(hub_user),
                    )
            self.assertEqual(error.exception.status_code, 403)

    def test_live_acl_refuses_a_user_who_has_not_signed_in(self) -> None:
        for hub_user in (None, SimpleNamespace(id=9, has_signed_in=False, name="Person")):
            with self.assertRaises(HTTPException) as error:
                agent_access._build_live_acl_response(
                    AgentIdentity(email="person@example.org", roles=("Member",)),
                    _Db(hub_user),
                )
            self.assertEqual(error.exception.status_code, 403)
            self.assertEqual(error.exception.detail, "Agent identity has not signed in to the Hub yet.")
