"""One admin edits Bot Management at a time (advisory edit lock).

Regression for QA 2026-10-25 (BM-STALE-reset): Reset carried no version check,
so it could overwrite a configuration another admin was in the middle of
changing. While an admin has unsaved changes they hold a short-lived lock;
other admins see who is editing, and their Save and Reset are refused with
423 until the editor saves, discards, or the lock expires. The lock is a
courtesy layer: the expected-version check stays the integrity guarantee, so
an unreachable lock store never blocks editing.
"""
from __future__ import annotations

import os
import time
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.models.auth import UserInfo
from app.routers import bot_management
from app.services import bot_settings_edit_lock as edit_lock
from tests.test_bot_management_mcp import ADMIN, _app_settings, _Base

OTHER = UserInfo(email="other.admin@renphil.org", name="Other Admin", roles=["Hub Admin"])
HAIKU = "anthropic_claude_haiku_45"


class FakeStore:
    """In-memory stand-in for the Upstash client (SET NX/XX EX, GET, DEL)."""

    def __init__(self) -> None:
        self.values: dict[str, tuple[str, float]] = {}

    def get(self, key):
        value = self.values.get(key)
        if value is None or value[1] < time.time():
            self.values.pop(key, None)
            return None
        return value[0]

    def set(self, key, value, *, ex=None, nx=None, xx=None):
        exists = self.get(key) is not None
        if (nx and exists) or (xx and not exists):
            return None
        self.values[key] = (value, time.time() + (ex or 3600))
        return True

    def delete(self, key):
        self.values.pop(key, None)
        return 1


class BrokenStore:
    def get(self, key):
        raise ConnectionError("store down")

    set = delete = get


class EditLockServiceTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.store = FakeStore()
        patcher = patch.object(edit_lock, "_store", return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_first_editor_gets_the_lock_and_others_see_who_holds_it(self):
        granted, holder = edit_lock.acquire(ADMIN)
        self.assertTrue(granted)
        self.assertEqual(holder.email, ADMIN.email)
        granted, holder = edit_lock.acquire(OTHER)
        self.assertFalse(granted)
        self.assertEqual((holder.email, holder.name), (ADMIN.email, "Admin"))

    def test_holder_can_refresh_and_release(self):
        edit_lock.acquire(ADMIN)
        self.assertTrue(edit_lock.acquire(ADMIN)[0])  # heartbeat refresh
        edit_lock.release(OTHER)  # only the holder can release
        self.assertIsNotNone(edit_lock.current())
        edit_lock.release(ADMIN)
        self.assertIsNone(edit_lock.current())
        self.assertTrue(edit_lock.acquire(OTHER)[0])

    def test_unreachable_store_never_blocks_editing(self):
        with patch.object(edit_lock, "_store", return_value=BrokenStore()):
            self.assertEqual(edit_lock.acquire(ADMIN), (True, None))
            self.assertIsNone(edit_lock.held_by_other(ADMIN))


class EditLockRouteTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.store = FakeStore()
        for target in (patch.object(edit_lock, "_store", return_value=self.store),
                       patch("app.config.get_settings", return_value=_app_settings()),
                       patch.dict(os.environ, {"AGENT_API_URL": "https://agent.internal.example"})):
            target.start()
            self.addCleanup(target.stop)

    def _save(self, user, **fields):
        fields.setdefault("expected_version_id", bot_management._active_version(self.db).id)
        request = bot_management.UpdateBotSettingsRequest(active_model_key=HAIKU, **fields)
        http_request = SimpleNamespace(headers={"Authorization": "Bearer admin-session"})
        return bot_management.update_settings(request, http_request, current_user=user, db=self.db)

    def test_save_by_another_admin_is_refused_while_locked(self):
        bot_management.acquire_settings_lock(current_user=ADMIN)
        with self.assertRaises(HTTPException) as caught:
            self._save(OTHER, answer_cache_enabled=False)
        self.assertEqual(caught.exception.status_code, 423)
        self.assertIn("Admin", caught.exception.detail)

    def test_reset_by_another_admin_is_refused_while_locked(self):
        bot_management.acquire_settings_lock(current_user=ADMIN)
        with self.assertRaises(HTTPException) as caught:
            bot_management.reset_settings(current_user=OTHER, db=self.db)
        self.assertEqual(caught.exception.status_code, 423)

    def test_holder_saves_and_the_lock_is_released(self):
        bot_management.acquire_settings_lock(current_user=ADMIN)
        self._save(ADMIN, answer_cache_enabled=False)
        self.assertIsNone(edit_lock.current())
        self.assertIsNone(bot_management.get_settings_lock(current_user=OTHER)["edit_lock"])

    def test_acquire_by_another_admin_returns_409_with_the_editor(self):
        bot_management.acquire_settings_lock(current_user=ADMIN)
        with self.assertRaises(HTTPException) as caught:
            bot_management.acquire_settings_lock(current_user=OTHER)
        self.assertEqual(caught.exception.status_code, 409)
        status = bot_management.get_settings_lock(current_user=OTHER)["edit_lock"]
        self.assertEqual((status["name"], status["held_by_me"]), ("Admin", False))

    def test_unlocked_saves_still_work(self):
        saved = self._save(OTHER, answer_cache_enabled=False)["active"]["settings"]
        self.assertFalse(saved["answer_cache_enabled"])
