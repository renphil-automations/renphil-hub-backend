from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

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
from app.db_v2.models.component import ComponentV2  # noqa: E402
from app.db_v2.models.gridstack import GridstackV2  # noqa: E402
from app.db_v2.models.hub import HubV2  # noqa: E402
from app.db_v2.models.nav_tab import NavTabV2  # noqa: E402
from app.db_v2.models.notification import NotificationV2  # noqa: E402
from app.db_v2.models.resource_grant import LEVEL_EDIT, LEVEL_VIEW  # noqa: E402
from app.db_v2.models.tab import TabV2  # noqa: E402
from app.db_v2.models.task import TaskAssigneeV2, TaskV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.dependencies import (  # noqa: E402
    CurrentHubUser,
    ViewAsAccess,
    get_current_hub_user,
    get_view_as_access,
    get_view_as_selection,
)
from app.main import app  # noqa: E402
from app.models.auth import UserInfo  # noqa: E402
from app.routers import task_manager as task_manager_router  # noqa: E402
from app.services import gridstack_service, task_manager_service as svc, view_as_service  # noqa: E402
from app.services import access_visibility_service  # noqa: E402
from app.services.access_visibility_service import (  # noqa: E402
    INVISIBLE,
    NodeVerdict,
    granted_single_node,
)
from app.services.resource_grant_service import GrantMatch  # noqa: E402

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _Access:
    """A stand-in `ViewerAccess`: `view` / `edit` / `granted` for every node."""

    full_access = False

    def __init__(self, *, view=True, edit=False, granted=None):
        self.view, self.edit = view, edit
        self.granted = view if granted is None else granted

    def verdict(self, node):
        if not self.view:
            return INVISIBLE
        return NodeVerdict(view=True, edit=self.edit, revealed=not self.granted)

    def is_granted(self, node):
        return self.granted


VIEWER = _Access(view=True, edit=False)
EDITOR = _Access(view=True, edit=True)


class _DbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(engine, "connect", lambda conn, _rec: conn.execute("PRAGMA foreign_keys=ON"))
        BaseV2.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        # A rooted chain (hub → nav tab → tab → grid → components), so the
        # single-node walk reaches the hub.
        self.db.add(HubV2(document_id="hub"))
        nav_tab = NavTabV2(slug="nav", title="Nav", order=0, protected=False)
        self.db.add(nav_tab)
        self.db.flush()
        tab = TabV2(
            document_id="tab", title="Tab", order=0, access_control={}, locked=False, locked_by="", nav_tab_id=nav_tab.id
        )
        self.db.add(tab)
        self.db.flush()
        self.tab = tab
        self.grid = GridstackV2(document_id="tab", name="Tab", settings={}, position=0, parent_id=None, parent_tab_id=tab.id)
        self.db.add(self.grid)
        self.db.flush()
        self.tm = ComponentV2(link="tm", type="task_manager", props={}, gridstack_id=self.grid.id)
        self.note = ComponentV2(link="note", type="block_note", props={}, gridstack_id=self.grid.id)
        self.db.add_all([self.tm, self.note])
        self.owner = self._user("owner@renphil.org")
        self.assignee = self._user("assignee@renphil.org")
        self.other = self._user("other@renphil.org")
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def _user(self, email: str) -> UserV2:
        user = UserV2(name=email.split("@")[0].title(), work_email=email, created_at=NOW, updated_at=NOW)
        self.db.add(user)
        self.db.flush()
        return user

    def _create(self, *, caller=None, access=EDITOR, **overrides):
        kwargs = dict(component_id=self.tm.id, title="Do it", assignee_emails=["assignee@renphil.org"])
        kwargs.update(overrides)
        return svc.create_task(self.db, caller_id=(caller or self.owner).id, access=access, **kwargs)

    def _update(self, task_id, caller, changes, is_admin=False, editor_of=()):
        return svc.update_task(
            self.db, task_id, caller_id=caller.id, is_admin=is_admin,
            can_edit_component=lambda cid: cid in editor_of, changes=changes,
        )

    def _delete(self, ids, caller, is_admin=False, editor_of=()):
        return svc.delete_tasks(
            self.db, ids, caller_id=caller.id, is_admin=is_admin, can_edit_component=lambda cid: cid in editor_of
        )

    @staticmethod
    def _assignee_ids(task):
        return [a["id"] for a in task["assignees"]]


class CreateTaskTests(_DbTestCase):
    def test_create_sets_owner_assignees_defaults_and_audit_fields(self) -> None:
        task = self._create(description="desc", editable_via_webhook=True)
        self.assertEqual(task["status"], "Pending")
        self.assertEqual(task["owner_id"], self.owner.id)
        self.assertEqual(task["owner_email"], "owner@renphil.org")
        self.assertEqual(task["assignees"], [{"id": self.assignee.id, "email": "assignee@renphil.org"}])
        self.assertEqual(task["component_id"], self.tm.id)
        self.assertEqual(task["last_updated_by"], self.owner.id)
        self.assertEqual(task["status_last_updated_by"], self.owner.id)
        self.assertIsNotNone(task["created_at"])
        self.assertTrue(task["editable_by_assignee"])
        self.assertTrue(task["editable_via_webhook"])
        self.assertEqual((task["is_owner"], task["is_assignee"], task["can_manage"]), (True, False, True))

    def test_multiple_assignees_deduplicated_case_insensitively(self) -> None:
        task = self._create(assignee_emails=["assignee@renphil.org", " Other@RenPhil.org ", "ASSIGNEE@renphil.org", ""])
        self.assertEqual(self._assignee_ids(task), [self.assignee.id, self.other.id])
        self.assertEqual(self.db.query(TaskAssigneeV2).count(), 2)
        self.assertEqual(self._create(assignee_emails=[])["assignees"], [])

    def test_view_on_component_is_enough_to_create(self) -> None:
        task = self._create(caller=self.other, access=VIEWER)
        self.assertEqual((task["owner_id"], task["can_manage"]), (self.other.id, True))
        for access, status in ((_Access(view=False), 404), (_Access(view=True, granted=False), 403)):
            with self.subTest(status=status):
                with self.assertRaises(svc.TaskManagerError) as ctx:
                    self._create(access=access)
                self.assertEqual(ctx.exception.status_code, status)
        self.assertEqual(self.db.query(TaskV2).count(), 1)

    def test_component_must_exist_and_be_a_task_manager(self) -> None:
        with self.assertRaises(svc.TaskManagerError) as ctx:
            self._create(component_id=9999)
        self.assertEqual(ctx.exception.status_code, 404)
        with self.assertRaises(svc.TaskManagerError) as ctx:
            self._create(component_id=self.note.id)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_unknown_assignees_and_bad_status_are_rejected(self) -> None:
        with self.assertRaises(svc.TaskManagerError) as ctx:
            self._create(assignee_emails=["assignee@renphil.org", "nobody@renphil.org", "ghost@renphil.org"])
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("nobody@renphil.org, ghost@renphil.org", ctx.exception.message)
        with self.assertRaises(svc.TaskManagerError) as ctx:
            self._create(status="Done")
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(self.db.query(TaskV2).count(), 0)
        self.assertEqual(self.db.query(TaskAssigneeV2).count(), 0)


class ListTasksTests(_DbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.mine = self._create(title="owned by owner")["id"]
        self.shared = self._create(title="for both", assignee_emails=["assignee@renphil.org", "other@renphil.org"])["id"]
        self.by_other = self._create(caller=self.other, title="other's", assignee_emails=[])["id"]

    def _ids(self, caller, access):
        result = svc.list_tasks(self.db, self.tm.id, caller_id=caller.id, access=access)
        return {t["id"] for t in result["tasks"]}, result

    def test_editor_sees_and_manages_all_tasks(self) -> None:
        ids, result = self._ids(self.assignee, EDITOR)
        self.assertEqual(ids, {self.mine, self.shared, self.by_other})
        self.assertTrue(result["can_view_all_tasks"])
        self.assertTrue(result["can_create_tasks"])
        flags = {t["id"]: (t["is_owner"], t["is_assignee"], t["can_manage"]) for t in result["tasks"]}
        self.assertEqual(flags[self.mine], (False, True, True))
        self.assertEqual(flags[self.by_other], (False, False, True))

    def test_viewer_sees_only_owned_or_assigned(self) -> None:
        ids, result = self._ids(self.other, VIEWER)
        self.assertEqual(ids, {self.shared, self.by_other})
        self.assertFalse(result["can_view_all_tasks"])
        self.assertTrue(result["can_create_tasks"])
        flags = {t["id"]: (t["can_manage"], t["can_edit_status"]) for t in result["tasks"]}
        self.assertEqual(flags, {self.shared: (False, True), self.by_other: (True, True)})
        ids, _ = self._ids(self.assignee, VIEWER)
        self.assertEqual(ids, {self.mine, self.shared})
        shared = next(t for t in svc.list_tasks(self.db, self.tm.id, caller_id=self.owner.id, access=VIEWER)["tasks"] if t["id"] == self.shared)
        self.assertEqual(self._assignee_ids(shared), [self.assignee.id, self.other.id])

    def test_access_and_type_errors(self) -> None:
        for access, component_id, status in (
            (_Access(view=False), self.tm.id, 404),
            (_Access(view=True, granted=False), self.tm.id, 403),
            (EDITOR, 9999, 404),
            (EDITOR, self.note.id, 400),
        ):
            with self.subTest(status=status, component_id=component_id):
                with self.assertRaises(svc.TaskManagerError) as ctx:
                    svc.list_tasks(self.db, component_id, caller_id=self.owner.id, access=access)
                self.assertEqual(ctx.exception.status_code, status)


class UpdateTaskTests(_DbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.task = self._create()
        self.task_id = self.task["id"]

    def test_owner_updates_content_and_audit_fields(self) -> None:
        updated = self._update(
            self.task_id,
            self.owner,
            {"title": "New", "description": None, "assignees": ["other@renphil.org", "assignee@renphil.org"],
             "editable_via_webhook": True},
        )
        self.assertEqual(updated["title"], "New")
        self.assertIsNone(updated["description"])
        self.assertEqual(set(self._assignee_ids(updated)), {self.other.id, self.assignee.id})
        self.assertTrue(updated["editable_via_webhook"])
        self.assertEqual(updated["last_updated_by"], self.owner.id)
        self.assertEqual(updated["status_last_updated_at"], self.task["status_last_updated_at"])
        self.assertEqual(self.db.query(TaskAssigneeV2).count(), 2)

        replaced = self._update(self.task_id, self.owner, {"assignees": ["other@renphil.org"]})
        self.assertEqual(self._assignee_ids(replaced), [self.other.id])
        for clear in ([], None):
            self.assertEqual(self._update(self.task_id, self.owner, {"assignees": clear})["assignees"], [])
        self.assertEqual(self.db.query(TaskAssigneeV2).count(), 0)

    def test_unchanged_assignees_do_not_bump_last_updated(self) -> None:
        self._update(self.task_id, self.owner, {"assignees": ["assignee@renphil.org"]})
        self.db.expire_all()
        self.assertEqual(self.db.get(TaskV2, self.task_id).last_updated_by, self.owner.id)
        updated = self._update(self.task_id, self.other, {"assignees": ["ASSIGNEE@renphil.org"]}, is_admin=True)
        self.assertEqual(updated["last_updated_by"], self.owner.id)

    def test_admin_and_component_editor_can_update_anything(self) -> None:
        updated = self._update(self.task_id, self.other, {"title": "Admin", "status": "Closed"}, is_admin=True)
        self.assertEqual((updated["title"], updated["status"]), ("Admin", "Closed"))
        self.assertEqual(updated["last_updated_by"], self.other.id)
        self.assertEqual(updated["status_last_updated_by"], self.other.id)

        updated = self._update(
            self.task_id, self.other, {"title": "Editor", "status": "Blocked"}, editor_of={self.tm.id}
        )
        self.assertEqual((updated["title"], updated["status"], updated["can_manage"]), ("Editor", "Blocked", True))
        with self.assertRaises(svc.TaskManagerError):
            self._update(self.task_id, self.other, {"title": "x"}, editor_of={self.note.id})

    def test_status_change_sets_status_audit_fields_only(self) -> None:
        updated = self._update(self.task_id, self.owner, {"status": "In Progress"})
        self.assertEqual(updated["status"], "In Progress")
        self.assertEqual(updated["status_last_updated_by"], self.owner.id)
        self.assertEqual(updated["last_updated_at"], self.task["last_updated_at"])

    def test_any_assignee_status_depends_on_editable_by_assignee(self) -> None:
        self._update(self.task_id, self.owner, {"assignees": ["assignee@renphil.org", "other@renphil.org"]})
        updated = self._update(self.task_id, self.other, {"status": "Blocked"})
        self.assertEqual(updated["status"], "Blocked")
        self.assertEqual(updated["status_last_updated_by"], self.other.id)
        self.assertEqual((updated["can_manage"], updated["can_edit_status"]), (False, True))

        self._update(self.task_id, self.owner, {"editable_by_assignee": False})
        with self.assertRaises(svc.TaskManagerError) as ctx:
            self._update(self.task_id, self.assignee, {"status": "Closed"})
        self.assertEqual((ctx.exception.status_code, ctx.exception.code), (403, "ASSIGNEE_EDIT_DISABLED"))

    def test_forbidden_and_invalid_updates(self) -> None:
        cases = [
            (self.assignee, {"title": "nope"}, 403),
            (self.assignee, {"assignees": []}, 403),
            (self.other, {"status": "Closed"}, 403),
            (self.owner, {"status": "Done"}, 422),
            (self.owner, {"title": "  "}, 422),
            (self.owner, {"assignees": ["other@renphil.org", "nobody@renphil.org"]}, 404),
            (self.owner, {}, 400),
        ]
        for caller, changes, status in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(svc.TaskManagerError) as ctx:
                    self._update(self.task_id, caller, changes)
                self.assertEqual(ctx.exception.status_code, status)
        with self.assertRaises(svc.TaskManagerError) as ctx:
            self._update(9999, self.owner, {"status": "Closed"})
        self.assertEqual(ctx.exception.status_code, 404)
        self.db.expire_all()
        row = self.db.get(TaskV2, self.task_id)
        self.assertEqual((row.title, row.status), ("Do it", "Pending"))
        self.assertEqual(
            [r.assignee_id for r in self.db.query(TaskAssigneeV2).all()], [self.assignee.id]
        )


class DeleteTasksTests(_DbTestCase):
    def test_partial_delete_reports_failures(self) -> None:
        mine = self._create()["id"]
        theirs = self._create(caller=self.other)["id"]
        result = self._delete([mine, theirs, 9999, mine], self.owner)
        self.assertEqual(result["deleted"], [mine])
        self.assertEqual(
            [(f["task_id"], f["status_code"]) for f in result["failed"]], [(theirs, 403), (9999, 404)]
        )
        self.assertEqual({t.id for t in self.db.query(TaskV2).all()}, {theirs})
        self.assertEqual({r.task_id for r in self.db.query(TaskAssigneeV2).all()}, {theirs})

    def test_assignee_cannot_delete(self) -> None:
        task = self._create()["id"]
        self.assertEqual(self._delete([task], self.assignee)["failed"][0]["status_code"], 403)

    def test_admin_and_component_editor_delete_any_task(self) -> None:
        a = self._create(caller=self.other)["id"]
        b = self._create(caller=self.other)["id"]
        self.assertEqual(self._delete([a], self.owner, is_admin=True)["deleted"], [a])
        self.assertEqual(self._delete([b], self.assignee, editor_of={self.tm.id})["deleted"], [b])

    def test_component_edit_check_runs_once_per_component(self) -> None:
        ids = [self._create(caller=self.other)["id"] for _ in range(3)]
        calls = []
        result = svc.delete_tasks(
            self.db, ids, caller_id=self.owner.id, is_admin=False,
            can_edit_component=lambda cid: calls.append(cid) or True,
        )
        self.assertEqual(result["deleted"], ids)
        self.assertEqual(calls, [self.tm.id])


class ComponentDeletionGuardTests(_DbTestCase):
    def _canvas_without(self, component: ComponentV2):
        content = gridstack_service.get_tab_content_v2(self.db, "tab")["content"]
        content["widgets"].pop(str(component.id))
        content["layout"] = [e for e in content.get("layout") or [] if str(e.get("id")) != str(component.id)]
        return content

    def test_canvas_save_refuses_to_delete_task_manager_with_tasks(self) -> None:
        self._create()
        with self.assertRaises(svc.TaskManagerHasTasksError) as ctx:
            gridstack_service.update_tab_content_v2(self.db, "tab", self._canvas_without(self.tm))
        self.assertEqual(ctx.exception.component_ids, [self.tm.id])
        self.db.expire_all()
        self.assertIsNotNone(self.db.get(ComponentV2, self.tm.id))

    def test_canvas_save_deletes_empty_task_manager(self) -> None:
        gridstack_service.update_tab_content_v2(self.db, "tab", self._canvas_without(self.tm))
        self.db.expire_all()
        self.assertIsNone(self.db.get(ComponentV2, self.tm.id))

    def test_tab_delete_refuses_when_a_task_manager_has_tasks(self) -> None:
        self._create()
        with self.assertRaises(svc.TaskManagerHasTasksError):
            gridstack_service.delete_tab_subtree_by_document_id_v2(self.db, "tab")
        self.db.expire_all()
        self.assertIsNotNone(self.db.get(ComponentV2, self.tm.id))

        self._delete([t.id for t in self.db.query(TaskV2).all()], self.owner)
        self.assertIsNotNone(gridstack_service.delete_tab_subtree_by_document_id_v2(self.db, "tab"))
        self.assertIsNone(self.db.get(ComponentV2, self.tm.id))

    def test_guard_maps_to_409(self) -> None:
        from app.routers.tabs import value_error_to_http_exception

        exc = value_error_to_http_exception(svc.TaskManagerHasTasksError([self.tm.id]))
        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.detail["code"], "TASK_MANAGER_HAS_TASKS")
        self.assertEqual(exc.detail["component_ids"], [self.tm.id])


class TaskNotificationTests(_DbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.third = self._user("third@renphil.org")
        self.db.commit()

    def _notes(self, clear=True):
        rows = self.db.query(NotificationV2).order_by(NotificationV2.id).all()
        result = [(n.recipient_email.split("@")[0], n.type, n.excerpt) for n in rows]
        for n in rows:
            assert n.component_id == self.tm.id and n.thread_id is None
        if clear:
            self.db.query(NotificationV2).delete()
            self.db.commit()
        return result

    def _task(self, **overrides):
        task = self._create(assignee_emails=["assignee@renphil.org", "other@renphil.org"], **overrides)
        self._notes()
        return task["id"]

    def test_create_notifies_each_assignee_but_not_the_owner(self) -> None:
        self._create(title="Ship", assignee_emails=["assignee@renphil.org", "owner@renphil.org"])
        self.assertEqual(
            self._notes(), [("assignee", "task_assigned", "You've been assigned the task Ship by Owner")]
        )
        self._create(notify_on_create_and_delete=False)
        self.assertEqual(self._notes(), [])

    def test_detail_change_follows_cadence_always_only(self) -> None:
        task_id = self._task()
        self._update(task_id, self.assignee, {"status": "Blocked"})  # just to prove actor exclusion below
        self._notes()
        self._update(task_id, self.other, {"title": "Renamed", "description": "d"}, editor_of={self.tm.id})
        self.assertEqual(
            self._notes(),
            [
                ("owner", "task_updated", "Other updated the task Renamed"),
                ("assignee", "task_updated", "Other updated the task Renamed"),
            ],
        )
        for cadence in ("never", "on_status_change"):
            self._update(task_id, self.owner, {"notification_cadence": cadence})
            self._update(task_id, self.owner, {"editable_via_webhook": cadence == "never"})
            with self.subTest(cadence=cadence):
                self.assertEqual(self._notes(), [])

    def test_status_change_follows_cadence(self) -> None:
        task_id = self._task()
        self._update(task_id, self.assignee, {"status": "In Progress"})
        self.assertEqual(
            self._notes(),
            [
                ("owner", "task_status_changed", "Assignee updated the status of the task Do it to In Progress"),
                ("other", "task_status_changed", "Assignee updated the status of the task Do it to In Progress"),
            ],
        )
        self._update(task_id, self.owner, {"notification_cadence": "on_status_change"})
        self._update(task_id, self.owner, {"status": "Blocked"})
        self.assertEqual({r for r, _, _ in self._notes()}, {"assignee", "other"})
        self._update(task_id, self.owner, {"notification_cadence": "never"})
        self._update(task_id, self.owner, {"status": "Closed"})
        self.assertEqual(self._notes(), [])

    def test_details_and_status_in_one_request_send_both(self) -> None:
        task_id = self._task()
        self._update(task_id, self.owner, {"title": "T2", "status": "Blocked"})
        self.assertEqual(
            [(r, t) for r, t, _ in self._notes()],
            [
                ("assignee", "task_updated"), ("other", "task_updated"),
                ("assignee", "task_status_changed"), ("other", "task_status_changed"),
            ],
        )

    def test_notification_settings_changes_never_notify(self) -> None:
        task_id = self._task()
        self._update(task_id, self.owner, {"notification_cadence": "on_status_change"})
        self._update(task_id, self.owner, {"notify_on_create_and_delete": False})
        self.assertEqual(self._notes(), [])
        # The settings after the update apply: `never` set with a title change sends nothing.
        self._update(task_id, self.owner, {"notification_cadence": "always"})
        self._update(task_id, self.owner, {"title": "x", "notification_cadence": "never"})
        self.assertEqual(self._notes(), [])

    def test_assignee_changes_notify_added_removed_and_owner(self) -> None:
        task_id = self._task()
        self._update(
            task_id, self.third, {"assignees": ["assignee@renphil.org", "third@renphil.org"]}, editor_of={self.tm.id}
        )
        # `third` is the actor and is newly added: not notified. `other` was removed.
        self.assertEqual(
            self._notes(),
            [
                ("other", "task_unassigned", "You've been unassigned from the task Do it by Third"),
                ("owner", "task_assignees_changed", "Third assigned Third to the task Do it and unassigned Other from it"),
            ],
        )
        self._update(task_id, self.owner, {"assignees": ["other@renphil.org"]})
        self.assertEqual(
            self._notes(),
            [
                ("other", "task_assigned", "You've been assigned the task Do it by Owner"),
                ("assignee", "task_unassigned", "You've been unassigned from the task Do it by Owner"),
                ("third", "task_unassigned", "You've been unassigned from the task Do it by Owner"),
            ],
        )
        # Not a detail change: cadence 'always' alone sends no "updated the task".
        self._update(task_id, self.owner, {"notify_on_create_and_delete": False})
        self._update(task_id, self.owner, {"assignees": []})
        self.assertEqual(self._notes(), [])

    def test_delete_notifies_unless_closed_or_disabled(self) -> None:
        open_task = self._task()
        closed = self._task(status="Closed")
        quiet = self._task(notify_on_create_and_delete=False)
        result = self._delete([open_task, closed, quiet], self.assignee, editor_of={self.tm.id})
        self.assertEqual(len(result["deleted"]), 3)
        self.assertEqual(
            self._notes(),
            [
                ("owner", "task_deleted", "The task Do it was deleted by Assignee"),
                ("other", "task_deleted", "The task Do it was deleted by Assignee"),
            ],
        )

    def test_failed_update_writes_no_notification(self) -> None:
        task_id = self._task()
        with self.assertRaises(svc.TaskManagerError):
            self._update(task_id, self.owner, {"title": "ok", "notification_cadence": "sometimes"})
        self.assertEqual(self._notes(), [])

    def test_long_titles_are_clipped_to_the_excerpt_column(self) -> None:
        self._create(title="x" * 300)
        (_, _, excerpt), = self._notes()
        self.assertEqual(len(excerpt), 200)
        self.assertTrue(excerpt.endswith("…"))

    def test_task_notifications_show_in_the_bell(self) -> None:
        from app.services import thread_service

        self._create(title="Bell")
        viewer = UserInfo(email="assignee@renphil.org", name="Assignee")
        items = thread_service.list_notifications_for_user(self.db, viewer, None, False, access=EDITOR).items
        self.assertEqual([(i.type, i.component_link, i.thread_id) for i in items], [("task_assigned", "tm", None)])
        count, _ = thread_service.get_unread_notification_count(self.db, viewer, access=EDITOR)
        self.assertEqual(count, 1)
        hidden = thread_service.list_notifications_for_user(self.db, viewer, None, False, access=_Access(view=False))
        self.assertEqual(hidden.items, [])


class TaskManagerRouterTests(_DbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.caller = self.owner
        self.access = EDITOR
        self.admin = False
        self.editor_of: set[int] = set()
        app.dependency_overrides[get_db_v2] = lambda: self.db
        app.dependency_overrides[get_current_hub_user] = lambda: CurrentHubUser(
            info=UserInfo(email=self.caller.work_email, name="x"), hub_user_id=self.caller.id, email=self.caller.work_email
        )
        app.dependency_overrides[get_view_as_access] = lambda: ViewAsAccess(access=self.access, view_as=False)
        self._perm_patch = patch.object(
            task_manager_router,
            "_task_permissions",
            lambda db, current, selection=None: (self.admin, lambda cid: cid in self.editor_of),
        )
        self._perm_patch.start()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self._perm_patch.stop()
        app.dependency_overrides.clear()
        super().tearDown()

    def test_round_trip(self) -> None:
        r = self.client.post(
            "/v2/task-manager/tasks",
            json={"component_id": self.tm.id, "title": "T", "assignees": ["assignee@renphil.org", "other@renphil.org"],
                  "status": "Blocked"},
        )
        self.assertEqual(r.status_code, 201, r.text)
        task = r.json()["task"]
        self.assertEqual((task["status"], task["is_owner"], task["is_assignee"]), ("Blocked", True, False))
        self.assertEqual([a["email"] for a in task["assignees"]], ["assignee@renphil.org", "other@renphil.org"])

        r = self.client.get(f"/v2/task-manager/components/{self.tm.id}/tasks")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([t["id"] for t in r.json()["tasks"]], [task["id"]])

        self.caller = self.assignee
        r = self.client.patch(f"/v2/task-manager/tasks/{task['id']}", json={"status": "Closed"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["task"]["status_last_updated_by"], self.assignee.id)
        r = self.client.patch(f"/v2/task-manager/tasks/{task['id']}", json={"title": "x"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"]["code"], "FORBIDDEN")

        r = self.client.request("DELETE", "/v2/task-manager/tasks", json={"task_ids": [task["id"]]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["success"], r.json()["failed"][0]["status_code"]), (False, 403))

        # A component editor manages a task they neither own nor are assigned to.
        self.editor_of = {self.tm.id}
        r = self.client.patch(f"/v2/task-manager/tasks/{task['id']}", json={"assignees": []})
        self.assertEqual((r.status_code, r.json()["task"]["assignees"]), (200, []))
        r = self.client.request("DELETE", "/v2/task-manager/tasks", json={"task_ids": [task["id"]]})
        self.assertEqual((r.json()["success"], r.json()["deleted"]), (True, [task["id"]]))

    def test_error_statuses(self) -> None:
        self.access = VIEWER
        r = self.client.post("/v2/task-manager/tasks", json={"component_id": self.tm.id, "title": "T"})
        self.assertEqual(r.status_code, 201, r.text)
        self.access = _Access(view=True, granted=False)
        r = self.client.post("/v2/task-manager/tasks", json={"component_id": self.tm.id, "title": "T"})
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (403, "FORBIDDEN"))
        self.access = _Access(view=False)
        self.assertEqual(self.client.get(f"/v2/task-manager/components/{self.tm.id}/tasks").status_code, 404)
        r = self.client.post("/v2/task-manager/tasks", json={"component_id": self.tm.id, "title": "T"})
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (404, "COMPONENT_NOT_FOUND"))
        self.access = EDITOR
        r = self.client.post("/v2/task-manager/tasks", json={"component_id": self.tm.id, "title": "T", "status": "Done"})
        self.assertEqual(r.status_code, 422)
        r = self.client.post("/v2/task-manager/tasks", json={"component_id": self.note.id, "title": "T"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"]["code"], "NOT_A_TASK_MANAGER")
        r = self.client.post(
            "/v2/task-manager/tasks", json={"component_id": self.tm.id, "title": "T", "assignees": ["ghost@renphil.org"]}
        )
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (404, "ASSIGNEE_NOT_FOUND"))


class TaskManagerViewAsTests(_DbTestCase):
    """View As on the four endpoints. `get_view_as_access` / the selection /
    the adopted pairs' grants are stubbed; `_task_permissions`, the
    single-node walk and the anonymous refusal run for real."""

    def setUp(self) -> None:
        super().setUp()
        self.mine = self._create(title="owned by owner", assignee_emails=[])["id"]
        self.assigned = self._create(caller=self.other, title="for owner", assignee_emails=["owner@renphil.org"])["id"]
        self.foreign = self._create(caller=self.other, title="other's", assignee_emails=[])["id"]
        self.access = VIEWER
        self.pairs_grants: list[GrantMatch] = []
        self.selection = view_as_service.ViewAsSelection(targets=[], user=None)
        self.resolved_for: list[int | None] = []
        self.fold_calls = 0

        def view_as_access():
            self.fold_calls += 1
            return ViewAsAccess(access=self.access, view_as=True, view_as_user="sel@renphil.org")

        def grants(db, targets, *, user_id, closures):
            self.resolved_for.append(user_id)
            return self.pairs_grants

        app.dependency_overrides[get_db_v2] = lambda: self.db
        app.dependency_overrides[get_current_hub_user] = lambda: CurrentHubUser(
            info=UserInfo(email=self.owner.work_email, name="x"), hub_user_id=self.owner.id, email=self.owner.work_email
        )
        app.dependency_overrides[get_view_as_access] = view_as_access
        app.dependency_overrides[get_view_as_selection] = lambda: self.selection
        self._grants_patch = patch.object(view_as_service, "view_as_grants", grants)
        self._grants_patch.start()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self._grants_patch.stop()
        app.dependency_overrides.clear()
        super().tearDown()

    def _list(self, query=""):
        r = self.client.get(f"/v2/task-manager/components/{self.tm.id}/tasks{query}")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_list_keeps_own_and_assigned_tasks_without_anonymous(self) -> None:
        body = self._list("?view_as=r:s")
        self.assertEqual({t["id"] for t in body["tasks"]}, {self.mine, self.assigned})
        self.assertEqual((body["view_as"], body["view_as_user"], body["can_create_tasks"]), (True, "sel@renphil.org", True))
        flags = {t["id"]: (t["is_owner"], t["is_assignee"], t["can_manage"]) for t in body["tasks"]}
        self.assertEqual(flags, {self.mine: (True, False, True), self.assigned: (False, True, False)})

    def test_anonymous_list_shows_only_what_the_roles_allow(self) -> None:
        body = self._list("?view_as=r:s&anonymous=true")
        self.assertEqual((body["tasks"], body["can_view_all_tasks"], body["can_create_tasks"]), ([], False, True))

        self.access = EDITOR
        body = self._list("?view_as=r:s&anonymous=true")
        self.assertEqual({t["id"] for t in body["tasks"]}, {self.mine, self.assigned, self.foreign})
        self.assertTrue(body["can_view_all_tasks"])
        for task in body["tasks"]:
            self.assertEqual(
                (task["is_owner"], task["is_assignee"], task["can_manage"], task["can_edit_status"]),
                (False, False, True, True),
            )

    def test_writes_are_refused_under_anonymous_before_any_access_check(self) -> None:
        r = self.client.post(
            "/v2/task-manager/tasks?anonymous=true", json={"component_id": self.tm.id, "title": "T"}
        )
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (403, "VIEW_AS_ANONYMOUS_READ_ONLY"))
        r = self.client.patch(f"/v2/task-manager/tasks/{self.mine}?anonymous=true&view_as=r:s", json={"title": "x"})
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (403, "VIEW_AS_ANONYMOUS_READ_ONLY"))
        r = self.client.request(
            "DELETE", "/v2/task-manager/tasks?anonymous=true", json={"task_ids": [self.mine]}
        )
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (403, "VIEW_AS_ANONYMOUS_READ_ONLY"))
        self.assertEqual((self.fold_calls, self.resolved_for), (0, []))
        self.assertEqual(self.db.query(TaskV2).count(), 3)
        self.assertEqual(self.db.get(TaskV2, self.mine).title, "owned by owner")

    def test_create_follows_the_view_as_access_and_is_owned_by_the_caller(self) -> None:
        r = self.client.post("/v2/task-manager/tasks", json={"component_id": self.tm.id, "title": "T"})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual((r.json()["task"]["owner_id"], r.json()["view_as"]), (self.owner.id, True))
        self.access = _Access(view=False)
        r = self.client.post("/v2/task-manager/tasks", json={"component_id": self.tm.id, "title": "T"})
        self.assertEqual(r.status_code, 404)

    def test_update_and_delete_follow_the_selected_pairs_plus_own_tasks(self) -> None:
        # The pairs give view only: the caller's own task, or status as an assignee.
        self.pairs_grants = [GrantMatch(1, "tab", self.tab.id, LEVEL_VIEW)]
        r = self.client.patch(f"/v2/task-manager/tasks/{self.foreign}", json={"title": "x"})
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (403, "FORBIDDEN"))
        r = self.client.patch(f"/v2/task-manager/tasks/{self.mine}", json={"title": "mine"})
        self.assertEqual((r.status_code, r.json()["view_as"]), (200, True))
        r = self.client.patch(f"/v2/task-manager/tasks/{self.assigned}", json={"status": "Closed"})
        self.assertEqual(r.status_code, 200, r.text)
        # Looked up only when owner/admin didn't decide, with the caller's direct shares.
        self.assertEqual(self.resolved_for, [self.owner.id] * 2)

        # The pairs give edit on an ancestor of the Task Manager: any task.
        self.pairs_grants = [GrantMatch(1, "tab", self.tab.id, LEVEL_EDIT)]
        r = self.client.patch(f"/v2/task-manager/tasks/{self.foreign}", json={"title": "x"})
        self.assertEqual((r.status_code, r.json()["task"]["can_manage"]), (200, True))
        r = self.client.request("DELETE", "/v2/task-manager/tasks", json={"task_ids": [self.foreign]})
        self.assertEqual((r.json()["deleted"], r.json()["view_as"]), ([self.foreign], True))
        # Writes never build the whole-tree View As access.
        self.assertEqual(self.fold_calls, 0)

    def test_without_view_as_the_callers_own_access_is_used(self) -> None:
        self.selection = None
        with patch.object(task_manager_router, "granted_single_node", return_value=False) as single:
            r = self.client.patch(f"/v2/task-manager/tasks/{self.foreign}", json={"title": "x"})
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(single.call_args.kwargs["grants"])
        self.assertEqual(self.resolved_for, [])


class GrantedSingleNodeGrantsTests(_DbTestCase):
    """`granted_single_node(grants=...)` walks the given grants instead of the
    user's own."""

    def _granted(self, grants, *, edit_only=True):
        return granted_single_node(
            self.db, 0, ("component", self.tm.id), is_admin=False, edit_only=edit_only, grants=grants
        )

    def test_given_grants_replace_the_users_own(self) -> None:
        with patch.object(access_visibility_service, "matching_grants", side_effect=AssertionError("not called")):
            self.assertTrue(self._granted([GrantMatch(1, "tab", self.tab.id, LEVEL_EDIT)]))
            self.assertTrue(self._granted([GrantMatch(1, "component", self.tm.id, LEVEL_EDIT)]))
            self.assertFalse(self._granted([GrantMatch(1, "tab", self.tab.id, LEVEL_VIEW)]))
            self.assertTrue(self._granted([GrantMatch(1, "tab", self.tab.id, LEVEL_VIEW)], edit_only=False))
            self.assertFalse(self._granted([GrantMatch(1, "component", self.note.id, LEVEL_EDIT)]))
            self.assertFalse(self._granted([]))


if __name__ == "__main__":
    unittest.main()
