"""scripts/make_branch_field_conflicts.py (owner, 2026-10-28): the people it
prepares come back from the next Import as `field_conflict` items, and a
background "Accept all" over them restores Airtable's values.

SQLite in memory; phase 10's fixture (Bo has two first-link conflicts and
Dee three after the first run, Ada none). The function takes any HR text
field; the fixture's run reads `first_name`, so that is the one used here.
"""

from __future__ import annotations

import importlib.util
import os
import unittest

from tests.test_airtable_sync_phase10 import JobFixture  # noqa: E402  (sets the env first)

from app.db_v2.models.airtable_sync import AirtableSyncRecordV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402

_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts", "make_branch_field_conflicts.py")
_spec = importlib.util.spec_from_file_location("make_branch_field_conflicts", _PATH)
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

FIELD = "first_name"


class TestBranchConflicts(JobFixture):
    def setUp(self) -> None:
        super().setUp()
        # Answer the fixture's own conflicts first so only the script's remain.
        job = self.start_ok({"run_id": self.run.id, "kind": "field_conflict", "choice": "accept_airtable"})
        self.assertEqual(self.job(job["id"]).status, "succeeded")
        self.assertEqual(self.items("field_conflict"), [])

    def prepare(self, limit=None) -> dict:
        with self.engine.connect() as conn:
            with conn.begin():
                return script.make_conflicts(conn, FIELD, limit)

    def first_names(self) -> dict[int, str | None]:
        self.db.expire_all()
        return {u.id: u.first_name for u in self.db.query(UserV2).all()}

    def test_the_next_import_reports_one_conflict_per_person(self):
        before = self.first_names()
        result = self.prepare()
        self.assertGreaterEqual(result["changed"], 2)
        after = self.first_names()
        for uid in result["user_ids"]:
            self.assertEqual(after[uid], script.MARK + (before[uid] or ""))
        self.db.expire_all()
        for rec in self.db.query(AirtableSyncRecordV2).filter(AirtableSyncRecordV2.hub_id.in_(result["user_ids"])):
            self.assertNotIn(FIELD, rec.base)

        run = self.run_sync()
        conflicts = self.items("field_conflict")
        self.assertEqual(len(conflicts), result["changed"])
        self.assertTrue(all(i.field == FIELD for i in conflicts))

        job = self.start_ok({"run_id": run.id, "kind": "field_conflict", "choice": "accept_airtable"})
        self.assertEqual(self.job(job["id"]).done, result["changed"])
        self.assertEqual(self.first_names(), before)
        self.assertEqual(self.items("field_conflict"), [])

    def test_limit_and_rerun(self):
        first = self.prepare(limit=1)
        self.assertEqual(first["changed"], 1)
        # The prepared person no longer has a base value, so a second call skips them.
        second = self.prepare()
        self.assertNotIn(first["user_ids"][0], second["user_ids"])
        self.assertEqual(second["eligible"], first["eligible"] - 1)

    def test_refuses_while_a_job_is_running(self):
        self.defer = True
        self.start_ok({"run_id": self.run.id, "kind": "field_conflict", "choice": "accept_airtable"})
        before = self.first_names()
        with self.assertRaises(RuntimeError):
            self.prepare()
        self.assertEqual(self.first_names(), before)


if __name__ == "__main__":
    unittest.main()
