"""Focused coverage for the Agent's live Hub authorization bridge."""

from __future__ import annotations

import base64
import hashlib
import hmac
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
                _Db(SimpleNamespace(id=9, is_active=True, name="Person")),
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
                _Db(SimpleNamespace(id=1, is_active=True, name="Admin")),
            )

        self.assertTrue(response["is_admin"])
        self.assertEqual(response["granted_node_keys"], [])
