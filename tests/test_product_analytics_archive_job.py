"""The archive-before-delete job end to end on SQLite with an in-memory store:
boundaries, batching, dry run, deleted-resource exclusion, environment
isolation, failure modes, crash recovery and configuration refusals."""

from __future__ import annotations

import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db_v2.models.product_analytics import (
    HubProductAnalyticsArchiveIdentityV2, HubProductAnalyticsArchiveV2, HubProductEngagementV2, HubProductEventV2,
)
from app.services import product_analytics_archive as archive
from app.services import product_analytics_service as service
from scripts import archive_product_analytics as job

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
GOOD_SETTINGS = dict(
    R2_ACCOUNT_ID="0123456789abcdef0123456789abcdef", R2_ACCESS_KEY_ID="AKIAEXAMPLEKEYID",
    R2_SECRET_ACCESS_KEY="s3cr3t-value-that-is-long-enough", R2_BUCKET_NAME="renphil-hub-analytics-archive-bucket",
    PRODUCT_ANALYTICS_ARCHIVE_PREFIX="hub-product-analytics", PRODUCT_ANALYTICS_RETENTION_DAYS="365", hub_environment="sandbox",
)
# The View As diagnostics are omitted unless a deployment says `include` explicitly.
INCLUDE_SETTINGS = SimpleNamespace(**{**GOOD_SETTINGS, "PRODUCT_ANALYTICS_ARCHIVE_VIEW_AS": "include"})


class ArchiveJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        HubProductEventV2.__table__.create(bind=self.engine)
        HubProductEngagementV2.__table__.create(bind=self.engine)
        HubProductAnalyticsArchiveV2.__table__.create(bind=self.engine)
        HubProductAnalyticsArchiveIdentityV2.__table__.create(bind=self.engine)
        with self.engine.begin() as connection:
            for table in ("nav_tabs", "tabs", "gridstacks"):
                connection.execute(text(f"CREATE TABLE {table} (document_id VARCHAR(255))"))
            connection.execute(text("CREATE TABLE components (link VARCHAR(255))"))
            connection.execute(text("INSERT INTO nav_tabs (document_id) VALUES ('nav-live')"))
            connection.execute(text("INSERT INTO tabs (document_id) VALUES ('tab-live')"))
            connection.execute(text("INSERT INTO gridstacks (document_id) VALUES ('sub-live')"))
            connection.execute(text("INSERT INTO components (link) VALUES ('component-live')"))
        # Test rows are inspected after their session closed.
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.store = archive.MemoryArchiveStore()

    # ── helpers ──────────────────────────────────────────────────────────

    def _event(self, age: timedelta, **fields) -> HubProductEventV2:
        at = NOW - age
        values = dict(event_id=str(uuid.uuid4()), event_name="root_tab.selected", component_type="root_tab", action="select",
                      trigger="click", hub_user_id=1, actor_email="a@renphil.org", environment="sandbox", page_route="/:nav_tab",
                      target_id="tab-live", page_id="tab-live", properties={"tab_kind": "root"}, occurred_at=at, received_at=at)
        values.update(fields)
        return HubProductEventV2(**values)

    def _engagement(self, age: timedelta, **fields) -> HubProductEngagementV2:
        at = NOW - age
        values = dict(engagement_id=str(uuid.uuid4()), hub_user_id=1, actor_email="a@renphil.org", environment="sandbox",
                      page_route="/:nav_tab", nav_tab_id="nav-live", tab_id="tab-live", active_seconds=5,
                      started_at=at - timedelta(seconds=5), ended_at=at, received_at=at)
        values.update(fields)
        return HubProductEngagementV2(**values)

    def _add(self, *rows) -> None:
        with self.Session() as db:
            db.add_all(rows)
            db.commit()

    def _counts(self) -> tuple[int, int]:
        with self.Session() as db:
            return (int(db.execute(select(func.count()).select_from(HubProductEventV2)).scalar_one()),
                    int(db.execute(select(func.count()).select_from(HubProductEngagementV2)).scalar_one()))

    def _run(self, *argv: str, store=..., environment: str = "sandbox", retention: str | None = "365", settings=None,
             storage: tuple[str, str] | None = None) -> tuple[int, list[str]]:
        lines: list[str] = []
        code = job.run(list(argv), engine=self.engine, store=self.store if store is ... else store, settings=settings,
                       raw_retention=retention, environment=environment, now=NOW, out=lines.append, storage=storage)
        return code, lines

    def _archived_ids(self) -> dict[str, set[str]]:
        found = {"events": set(), "engagements": set()}
        for key, payload in self.store.objects.items():
            manifest = archive.validate_archive(payload)
            self.assertEqual(manifest["environment"], "sandbox")
            from io import BytesIO
            from zipfile import ZipFile
            import json
            with ZipFile(BytesIO(payload)) as zipped:
                for table, member in archive.MEMBER_NAMES.items():
                    for line in zipped.read(member).decode("utf-8").splitlines():
                        found[table].add(json.loads(line)[archive.RECORD_ID_COLUMN[table]])
        return found

    # ── behaviour ────────────────────────────────────────────────────────

    def test_expired_rows_are_archived_then_deleted_at_the_365_day_boundary(self) -> None:
        old_event = self._event(timedelta(days=366))
        boundary_event = self._event(timedelta(days=365, seconds=1))
        kept_event = self._event(timedelta(days=365))          # exactly the cutoff: kept (strictly older only)
        recent_event = self._event(timedelta(days=1))
        old_engagement = self._engagement(timedelta(days=400))
        kept_engagement = self._engagement(timedelta(days=364, hours=23))
        self._add(old_event, boundary_event, kept_event, recent_event, old_engagement, kept_engagement)
        code, lines = self._run()
        self.assertEqual(code, 0, lines)
        self.assertEqual(self._counts(), (2, 1))
        archived = self._archived_ids()
        self.assertEqual(archived["events"], {old_event.event_id, boundary_event.event_id})
        self.assertEqual(archived["engagements"], {old_engagement.engagement_id})
        with self.Session() as db:
            self.assertEqual({r.event_id for r in db.query(HubProductEventV2)}, {kept_event.event_id, recent_event.event_id})
        keys = sorted(self.store.objects)
        self.assertEqual(len(keys), 2)
        self.assertTrue(keys[0].startswith("hub-product-analytics/sandbox/engagements/v3/2025/"))
        self.assertTrue(keys[1].startswith("hub-product-analytics/sandbox/events/v3/2025/"))
        self.assertTrue(any("archived 2 events row(s) older than 2025-11-03" in line for line in lines), lines)
        self.assertNotIn("a@renphil.org", "\n".join(lines))
        # Running again finds nothing to do and uploads nothing new.
        before = dict(self.store.objects)
        code, lines = self._run()
        self.assertEqual((code, self.store.objects), (0, before))
        self.assertEqual(self._counts(), (2, 1))

    def test_archived_rows_round_trip_every_column(self) -> None:
        row = self._event(timedelta(days=500), component_id="component-live", page_id="tab-live", target_id=None,
                          event_name="link.clicked", component_type="link", action="open_link",
                          properties={"destination_kind": "external", "item_index": 3}, cascade_id=str(uuid.uuid4()),
                          view_as_role_key="role", view_as_scope_key="scope", view_as_context={"pairs": [], "user_id": 4})
        self._add(row)
        self.assertEqual(self._run(settings=INCLUDE_SETTINGS)[0], 0)  # every column only with the explicit `include` policy
        import json
        from io import BytesIO
        from zipfile import ZipFile
        (payload,) = self.store.objects.values()
        with ZipFile(BytesIO(payload)) as zipped:
            record = json.loads(zipped.read("events.jsonl"))
        self.assertEqual(record["event_id"], row.event_id)
        self.assertEqual(record["properties"], {"destination_kind": "external", "item_index": 3})
        self.assertEqual((record["component_id"], record["page_id"], record["view_as_context"]), ("component-live", "tab-live", {"pairs": [], "user_id": 4}))
        self.assertEqual(record["occurred_at"], (NOW - timedelta(days=500)).isoformat())
        self.assertEqual(set(record), {c.name for c in HubProductEventV2.__table__.columns})

    def test_view_as_policy_omit_is_the_default_and_strips_the_diagnostics(self) -> None:
        row = self._event(timedelta(days=400), view_as_role_key="role", view_as_scope_key="scope", view_as_context={"pairs": [], "user_id": 4})
        self._add(row)
        # No setting at all → omit; an explicit `omit` → omit; only an explicit `include` keeps the columns.
        for settings in (None, SimpleNamespace(**GOOD_SETTINGS), SimpleNamespace(**{**GOOD_SETTINGS, "PRODUCT_ANALYTICS_ARCHIVE_VIEW_AS": "omit"})):
            code, lines = self._run("--dry-run", settings=settings)
            self.assertEqual(code, 0, lines)
            self.assertIn("dry run: archives would omit the view_as_* columns (PRODUCT_ANALYTICS_ARCHIVE_VIEW_AS=omit).", lines)
        code, lines = self._run("--dry-run", settings=INCLUDE_SETTINGS)
        self.assertIn("dry run: archives would include the view_as_* columns (PRODUCT_ANALYTICS_ARCHIVE_VIEW_AS=include).", lines)
        code, lines = self._run()
        self.assertEqual(code, 0, lines)
        import json
        from io import BytesIO
        from zipfile import ZipFile
        (payload,) = self.store.objects.values()
        manifest = archive.validate_archive(payload)
        self.assertEqual(manifest["omitted_columns"], ["view_as_context", "view_as_role_key", "view_as_scope_key"])
        with ZipFile(BytesIO(payload)) as zipped:
            record = json.loads(zipped.read("events.jsonl"))
        self.assertEqual(set(record), {c.name for c in HubProductEventV2.__table__.columns} - archive.VIEW_AS_COLUMNS)
        self.assertEqual(record["event_id"], row.event_id)
        self.assertEqual(self._counts(), (0, 0))
        # An unknown policy value refuses before anything is touched.
        self._add(self._event(timedelta(days=400)))
        code, lines = self._run(settings=SimpleNamespace(**{**GOOD_SETTINGS, "PRODUCT_ANALYTICS_ARCHIVE_VIEW_AS": "strip"}))
        self.assertEqual(code, 2)
        self.assertIn("PRODUCT_ANALYTICS_ARCHIVE_VIEW_AS", lines[0])
        self.assertEqual(self._counts(), (1, 0))

    def test_dry_run_touches_neither_storage_nor_database(self) -> None:
        self._add(self._event(timedelta(days=400)), self._event(timedelta(days=400), target_id="tab-deleted", page_id="tab-deleted"),
                  self._event(timedelta(days=5)), self._engagement(timedelta(days=400)))
        code, lines = self._run("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self._counts(), (3, 1))
        self.assertIn("dry run: 1 events row(s) reference deleted resources and would be deleted without archiving.", lines)
        self.assertTrue(any(line.startswith("dry run: 1 events row(s) older than 2025-11-03 (retention 365 days) would be archived in 1 batch(es)") for line in lines), lines)
        self.assertTrue(any(line.startswith("dry run: 1 engagements row(s) older than") for line in lines), lines)
        self.assertEqual(lines[-1], "dry run: no archive was uploaded and nothing was deleted.")
        # A dry run without storage configuration still reports, and says the live job would refuse.
        code, lines = self._run("--dry-run", store=None, settings=SimpleNamespace(**{**GOOD_SETTINGS, "R2_BUCKET_NAME": ""}))
        self.assertEqual(code, 0)
        self.assertTrue(any("storage configuration is not usable (missing archive storage configuration: R2_BUCKET_NAME)" in line for line in lines), lines)
        self.assertEqual(self._counts(), (3, 1))

    def test_deleted_resource_rows_are_deleted_not_archived(self) -> None:
        stale_old = self._event(timedelta(days=400), target_id="tab-deleted", page_id="tab-deleted")
        stale_recent = self._event(timedelta(days=5), component_id="component-deleted", event_name="link.clicked", target_id=None, page_id=None)
        live_old = self._event(timedelta(days=400))
        stale_interval = self._engagement(timedelta(days=400), tab_id="tab-deleted")
        live_interval = self._engagement(timedelta(days=400))
        self._add(stale_old, stale_recent, live_old, stale_interval, live_interval)
        code, lines = self._run()
        self.assertEqual(code, 0)
        self.assertIn("deleted 2 events row(s) referencing deleted resources (not archived).", lines)
        self.assertIn("deleted 1 engagements row(s) referencing deleted resources (not archived).", lines)
        archived = self._archived_ids()
        self.assertEqual(archived, {"events": {live_old.event_id}, "engagements": {live_interval.engagement_id}})
        self.assertEqual(self._counts(), (0, 0))

    def test_skipping_deleted_cleanup_still_keeps_those_rows_out_of_archives(self) -> None:
        stale_old = self._event(timedelta(days=400), target_id="tab-deleted", page_id="tab-deleted")
        live_old = self._event(timedelta(days=400))
        self._add(stale_old, live_old)
        code, _ = self._run("--skip-deleted-cleanup")
        self.assertEqual(code, 0)
        self.assertEqual(self._archived_ids()["events"], {live_old.event_id})
        with self.Session() as db:
            self.assertEqual([r.event_id for r in db.query(HubProductEventV2)], [stale_old.event_id])

    def test_only_the_configured_environment_is_archived_or_deleted(self) -> None:
        sandbox = self._event(timedelta(days=400))
        production = self._event(timedelta(days=400), environment="production")
        production_stale = self._event(timedelta(days=5), environment="production", target_id="tab-deleted", page_id="tab-deleted")
        self._add(sandbox, production, production_stale, self._engagement(timedelta(days=400), environment="production"))
        code, _ = self._run(environment="sandbox")
        self.assertEqual(code, 0)
        self.assertEqual(self._archived_ids(), {"events": {sandbox.event_id}, "engagements": set()})
        self.assertEqual(self._counts(), (2, 1))
        with self.Session() as db:
            self.assertEqual({r.environment for r in db.query(HubProductEventV2)}, {"production"})
        code, lines = self._run(environment="unconfigured")
        self.assertEqual(code, 2)
        self.assertIn("HUB_ENVIRONMENT", lines[0])

    def test_batches_are_bounded_and_a_later_run_resumes(self) -> None:
        rows = [self._event(timedelta(days=400 + i)) for i in range(25)]
        self._add(*rows, self._event(timedelta(days=1)))
        code, lines = self._run("--batch-size", "100", "--max-batches", "2", store=self.store)
        self.assertEqual(code, 0)
        self.assertEqual(self._counts()[0], 1)  # a batch of 100 takes all 25 in one object
        self.assertEqual(len(self.store.objects), 1)
        # Smaller batches through the service directly: bounded and resumable.
        self.setUp()
        rows = [self._event(timedelta(days=400 + i)) for i in range(25)]
        self._add(*rows, self._event(timedelta(days=1)))
        with self.Session() as db:
            outcome = archive.archive_expired(db, self.store, table="events", environment="sandbox", cutoff=service.retention_cutoff(365, NOW),
                                              prefix="p", batch_size=10, max_batches=2)
        self.assertEqual((outcome.archived, outcome.deleted, outcome.batches, len(outcome.keys)), (20, 20, 2, 2))
        self.assertEqual(self._counts()[0], 6)
        with self.Session() as db:
            outcome = archive.archive_expired(db, self.store, table="events", environment="sandbox", cutoff=service.retention_cutoff(365, NOW),
                                              prefix="p", batch_size=10)
        self.assertEqual((outcome.archived, outcome.batches), (5, 1))
        self.assertEqual(self._counts()[0], 1)
        self.assertEqual(self._archived_ids()["events"], {r.event_id for r in rows})

    def test_upload_failure_deletes_nothing_and_the_next_run_recovers(self) -> None:
        class FailingStore(archive.MemoryArchiveStore):
            def put(self, key, data, metadata=None, *, if_none_match=False):
                self.calls.append(("put", key))
                raise OSError("network down")

        self._add(self._event(timedelta(days=400)), self._engagement(timedelta(days=400)))
        failing = FailingStore()
        code, lines = self._run(store=failing)
        self.assertEqual(code, 1)
        self.assertTrue(any(line.startswith("archive failed for events: OSError") for line in lines), lines)
        self.assertNotIn("network down", "\n".join(lines))  # foreign exception text is not echoed
        self.assertEqual(self._counts(), (1, 1))
        self.assertEqual(failing.objects, {})
        code, _ = self._run()
        self.assertEqual((code, self._counts()), (0, (0, 0)))

    def test_readback_verification_failure_deletes_nothing(self) -> None:
        class CorruptStore(archive.MemoryArchiveStore):
            def get(self, key):
                self.calls.append(("get", key))
                return b"corrupt"

        self._add(self._event(timedelta(days=400)))
        code, lines = self._run(store=CorruptStore())
        self.assertEqual(code, 1)
        self.assertTrue(any("ArchiveIntegrityError: archive readback verification failed" in line for line in lines), lines)
        self.assertEqual(self._counts(), (1, 0))

    def test_crash_after_upload_is_recovered_without_a_second_upload(self) -> None:
        class CrashAfterUpload(archive.MemoryArchiveStore):
            crashed = False

            def get(self, key):
                if not self.crashed:
                    self.crashed = True
                    raise RuntimeError("process killed")
                return super().get(key)

        rows = [self._event(timedelta(days=400 + i)) for i in range(3)]
        self._add(*rows)
        store = CrashAfterUpload()
        code, lines = self._run(store=store)
        self.assertEqual(code, 1)
        self.assertEqual(self._counts(), (3, 0))
        self.assertEqual(len(store.objects), 1)
        store.calls.clear()
        self.store = store
        code, lines = self._run(store=store)
        self.assertEqual(code, 0)
        self.assertEqual(self._counts(), (0, 0))
        self.assertEqual([call for call, _ in store.calls], ["head", "get"])  # recognised, verified, never re-uploaded
        self.assertTrue(any("[already present, verified sha256" in line for line in lines), lines)
        self.assertEqual(self._archived_ids()["events"], {r.event_id for r in rows})

    def test_an_existing_object_with_different_bytes_stops_the_job(self) -> None:
        rows = [self._event(timedelta(days=400))]
        self._add(*rows)
        with self.Session() as db:
            cutoff = service.retention_cutoff(365, NOW)
            records = [archive.archive_row(r, "events") for r in db.query(HubProductEventV2)]
        payload = archive.make_archive(environment="sandbox", events=records, metadata={"table": "events", "selection": "age_expired_not_deleted_resource"})
        import hashlib
        key = archive.archive_key(prefix="hub-product-analytics", environment="sandbox", table="events", first_day=(NOW - timedelta(days=400)).date(),
                                  first_id=records[0]["id"], last_id=records[0]["id"], digest=hashlib.sha256(payload).hexdigest())
        self.store.put(key, b"someone else's bytes")
        code, lines = self._run(settings=INCLUDE_SETTINGS)  # the key above was computed from every column
        self.assertEqual(code, 1)
        self.assertEqual(self._counts(), (1, 0))
        self.assertEqual(self.store.objects[key], b"someone else's bytes")

    def test_refuses_without_hub_resources_before_any_storage_call(self) -> None:
        self._add(self._event(timedelta(days=400)))
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM tabs"))
        for argv in ([], ["--dry-run"]):
            code, lines = self._run(*argv)
            self.assertEqual(code, 2)
            self.assertIn("no rows in tabs", lines[0])
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self._counts(), (1, 0))

    def test_an_environment_without_analytics_rows_is_not_mistaken_for_a_wrong_database(self) -> None:
        """A fresh or reset environment has empty Hub tables and nothing to
        delete; refusing it would page an operator for nothing. One analytics
        row in that environment brings the guard back."""
        with self.engine.begin() as connection:
            for table in service.HUB_RESOURCE_TABLES:
                connection.execute(text(f"DELETE FROM {table}"))
        self._add(self._event(timedelta(days=400), environment="production"))  # another environment's rows do not count
        for argv in ([], ["--dry-run"]):
            code, lines = self._run(*argv)
            self.assertEqual(code, 0, lines)
        self.assertEqual((self.store.calls, self._counts()), ([], (1, 0)))
        self._add(self._event(timedelta(days=400)))
        code, lines = self._run()
        self.assertEqual(code, 2, lines)
        self.assertIn("no rows in nav_tabs", lines[0])
        self.assertEqual((self.store.calls, self._counts()), ([], (2, 0)))

    def test_referenced_resources_follow_the_deleted_resource_rule_exactly(self) -> None:
        events = [
            dict(event_name="navigation_tab.selected", target_id="nav-1", page_id=None, component_id=None),
            dict(event_name="root_tab.selected", target_id="tab-1", page_id="tab-1", component_id=None),
            dict(event_name="sub_tab.selected", target_id="sub-1", page_id="sub-1", component_id=None),
            dict(event_name="blocknote_tab.selected", target_id="page-1", page_id="tab-2", component_id="sbn-1"),
            dict(event_name="link.clicked", target_id=None, page_id="tab-3", component_id="mirror-1"),  # the mirror, never its target
            dict(event_name="profile_menu.go_to_profile", target_id=None, page_id=None, component_id=None),
            dict(event_name="threads_management.tab_selected", target_id="pending", page_id="tab-4", component_id=None),
        ]
        self.assertEqual(
            archive.referenced_resources("events", events),
            {"nav_tabs": {"nav-1"}, "pages": {"tab-1", "sub-1", "tab-4"}, "components": {"sbn-1", "page-1", "mirror-1"}},
        )
        intervals = [dict(nav_tab_id="nav-1", tab_id="tab-1"), dict(nav_tab_id=None, tab_id="sub-1"), dict(nav_tab_id=None, tab_id=None)]
        self.assertEqual(archive.referenced_resources("engagements", intervals), {"nav_tabs": {"nav-1"}, "pages": {"tab-1", "sub-1"}, "components": set()})
        with self.Session() as db:
            self.assertFalse(archive.lock_referenced_resources(db, "events", events))  # SQLite: nothing to lock

    def test_without_row_locks_a_resource_deleted_during_the_upload_stops_the_batch_before_its_delete(self) -> None:
        """Failure injection for the post-upload check that PostgreSQL's row
        locks make unreachable: if rows of the batch match the deleted-
        resource rule after the upload, the batch is NOT deleted, the run
        stops and the object is named for review."""
        rows = [self._event(timedelta(days=400)), self._event(timedelta(days=401))]
        self._add(*rows)
        counts = iter([0, 2])
        with patch.object(archive, "_deleted_resource_count", lambda db, *, table, ids: next(counts)):
            code, lines = self._run()
        self.assertEqual(code, 1, lines)
        self.assertTrue(any("ArchiveIntegrityError: 2 events row(s) of the batch came to reference a deleted resource while" in line for line in lines), lines)
        self.assertEqual(self._counts(), (2, 0))
        self.assertEqual(len(self.store.objects), 1)

    def test_lock_held_by_another_run_exits_without_touching_anything(self) -> None:
        self._add(self._event(timedelta(days=400)))
        with patch.object(archive, "try_acquire_batch_lock", lambda db: False):
            code, lines = self._run()
        self.assertEqual(code, 3)
        self.assertTrue(any("another archive run holds the batch lock" in line for line in lines), lines)
        self.assertEqual((self.store.calls, self._counts()), ([], (1, 0)))

    def test_refuses_without_the_registry_table_before_any_storage_call(self) -> None:
        self._add(self._event(timedelta(days=400)))
        with self.engine.begin() as connection:
            connection.execute(text("DROP TABLE hub_product_analytics_archives"))
        for argv in ([], ["--dry-run"]):
            code, lines = self._run(*argv)
            self.assertEqual(code, 2, lines)
            self.assertIn("registry table hub_product_analytics_archives does not exist", lines[0])
        self.assertEqual((self.store.calls, self._counts()), ([], (1, 0)))

    def test_each_archive_is_registered_in_the_transaction_that_deletes_its_rows(self) -> None:
        rows = [self._event(timedelta(days=400 + i)) for i in range(3)]
        self._add(*rows, self._engagement(timedelta(days=400)))
        code, lines = self._run()
        self.assertEqual(code, 0, lines)
        with self.Session() as db:
            registry = {row.key: row for row in db.query(HubProductAnalyticsArchiveV2)}
            self.assertEqual(set(registry), set(self.store.objects))
            events_row = next(row for row in registry.values() if row.table_name == "events")
            self.assertEqual((events_row.records, events_row.environment, events_row.format_version, events_row.omitted_columns),
                             (3, "sandbox", 3, ["view_as_context", "view_as_role_key", "view_as_scope_key"]))
            self.assertEqual((events_row.first_id, events_row.last_id), (min(r.id for r in rows), max(r.id for r in rows)))
            self.assertEqual(archive.unregistered_keys(db, [*self.store.objects, "p/sandbox/events/v3/2025/leftover.zip"]), ["p/sandbox/events/v3/2025/leftover.zip"])
            summary = {(row["environment"], row["table"]): (row["archives"], row["records"]) for row in archive.registry_summary(db)}
            self.assertEqual(summary, {("sandbox", "events"): (1, 3), ("sandbox", "engagements"): (1, 1)})
        self.assertTrue(any(", registered]; deleted 3." in line for line in lines), lines)
        self.assertTrue(any("(1 registered, 0 already present)" in line for line in lines), lines)

    def test_check_keys_reports_unregistered_objects_without_touching_storage(self) -> None:
        import tempfile
        from pathlib import Path

        self._add(self._event(timedelta(days=400)))
        self.assertEqual(self._run()[0], 0)
        (registered,) = list(self.store.objects)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        listing = Path(directory.name) / "keys.txt"
        listing.write_text(f"{registered}\n\np/sandbox/events/v3/2025/abandoned.zip\n", encoding="utf-8")
        self.store.calls.clear()
        code, lines = self._run("--check-keys", str(listing))
        self.assertEqual(code, 0, lines)
        self.assertIn("checked 2 key(s): 1 registered archive(s), 1 unregistered object(s) (not archives; reconcile them).", lines)
        self.assertIn("unregistered: p/sandbox/events/v3/2025/abandoned.zip", lines)
        self.assertTrue(any(line.startswith("registry: sandbox events: 1 archive(s), 1 record(s)") for line in lines), lines)
        self.assertEqual(self.store.calls, [])

    def test_configuration_refusals_touch_nothing(self) -> None:
        self._add(self._event(timedelta(days=5000)))
        cases = {
            "retention off": dict(retention="off", expected=(0, "disabled")),
            "retention invalid": dict(retention="7", expected=(2, "refused")),
            "no storage": dict(store=None, settings=SimpleNamespace(**{**GOOD_SETTINGS, "R2_SECRET_ACCESS_KEY": ""}), expected=(2, "R2_SECRET_ACCESS_KEY")),
            "bad batch": dict(argv=("--batch-size", "10"), expected=(2, "--batch-size")),
            "bad max": dict(argv=("--max-batches", "0"), expected=(2, "--max-batches")),
        }
        for name, case in cases.items():
            with self.subTest(name):
                code, lines = self._run(*case.get("argv", ()), store=case.get("store", ...), retention=case.get("retention", "365"),
                                        settings=case.get("settings"))
                self.assertEqual(code, case["expected"][0], lines)
                self.assertIn(case["expected"][1], lines[0])
                self.assertNotIn(GOOD_SETTINGS["R2_ACCESS_KEY_ID"], "\n".join(lines))
        self.assertEqual((self.store.calls, self._counts()), ([], (1, 0)))

    def test_concurrent_runs_without_a_database_lock_still_delete_each_row_once(self) -> None:
        """SQLite has no advisory lock, so two runs can interleave; the delete
        is restricted to exact ids and reports what it removed, so no row is
        ever counted twice and every row is in a verified archive."""
        # A file-backed database so each thread gets its own connection; the
        # in-memory StaticPool engine would share one SQLite connection.
        import tempfile
        from pathlib import Path
        from sqlalchemy.pool import NullPool

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.engine = create_engine(f"sqlite:///{Path(directory.name) / 'archive.db'}", poolclass=NullPool,
                                    connect_args={"timeout": 30, "check_same_thread": False})
        HubProductEventV2.__table__.create(bind=self.engine)
        HubProductEngagementV2.__table__.create(bind=self.engine)
        HubProductAnalyticsArchiveV2.__table__.create(bind=self.engine)
        HubProductAnalyticsArchiveIdentityV2.__table__.create(bind=self.engine)
        with self.engine.begin() as connection:
            for table in ("nav_tabs", "tabs", "gridstacks"):
                connection.execute(text(f"CREATE TABLE {table} (document_id VARCHAR(255))"))
            connection.execute(text("CREATE TABLE components (link VARCHAR(255))"))
            connection.execute(text("INSERT INTO tabs (document_id) VALUES ('tab-live')"))
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        rows = [self._event(timedelta(days=400 + i)) for i in range(20)]
        self._add(*rows)
        lock = threading.Lock()
        results: list[archive.ArchiveOutcome] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                with self.Session() as db:
                    outcome = archive.archive_expired(db, self.store, table="events", environment="sandbox",
                                                      cutoff=service.retention_cutoff(365, NOW), prefix="p", batch_size=5)
            except BaseException as exc:  # noqa: BLE001 - a worker failure is the test's failure, not a warning
                with lock:
                    errors.append(exc)
                return
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(outcome.deleted for outcome in results), 20)
        # Two runs can build the same batch and therefore the same key; the
        # registry insert is conflict-safe, so exactly one registration per key.
        self.assertEqual(sum(outcome.registered for outcome in results), len(self.store.objects))
        self.assertEqual(self._counts()[0], 0)
        self.assertEqual(self._archived_ids()["events"], {r.event_id for r in rows})
        with self.Session() as db:
            self.assertEqual({row.key for row in db.query(HubProductAnalyticsArchiveV2)}, set(self.store.objects))

    def _restore(self, key: str, **kwargs) -> dict:
        with self.Session() as db:
            result = archive.restore_registered_archive(db, self.store, key=key, environment="sandbox", **kwargs)
            db.commit()
        return result

    def test_restore_from_a_registered_archive_round_trips_every_column_and_is_idempotent(self) -> None:
        event = self._event(timedelta(days=400), properties={"tab_kind": "root", "nested": {"a": [1, "x", None]}},
                            view_as_role_key="role", view_as_scope_key="scope", view_as_context={"user_id": 4})
        interval = self._engagement(timedelta(days=400))
        self._add(event, interval)
        with self.Session() as db:
            originals = {
                "events": {c.name: getattr(db.get(HubProductEventV2, event.id), c.name) for c in HubProductEventV2.__table__.columns},
                "engagements": {c.name: getattr(db.get(HubProductEngagementV2, interval.id), c.name) for c in HubProductEngagementV2.__table__.columns},
            }
        self.assertEqual(self._run(settings=INCLUDE_SETTINGS)[0], 0)
        self.assertEqual(self._counts(), (0, 0))
        results = {key: self._restore(key) for key in self.store.objects}
        self.assertEqual({r["table"]: r["inserted"][r["table"]] for r in results.values()}, {"events": 1, "engagements": 1})
        with self.Session() as db:
            restored_event = db.query(HubProductEventV2).one()
            restored_interval = db.query(HubProductEngagementV2).one()
            for name, value in originals["events"].items():
                if name != "id":
                    self.assertEqual(getattr(restored_event, name), value, name)
            for name, value in originals["engagements"].items():
                if name != "id":
                    self.assertEqual(getattr(restored_interval, name), value, name)
        # Restoring again inserts nothing (same event_id / engagement_id).
        for key in self.store.objects:
            again = self._restore(key)
            self.assertEqual((sum(again["inserted"].values()), again["already_present"][again["table"]]), (0, 1))
        self.assertEqual(self._counts(), (1, 1))

    def test_restore_of_an_archive_written_under_the_default_policy_leaves_the_view_as_columns_null(self) -> None:
        self._add(self._event(timedelta(days=400), view_as_role_key="role", view_as_scope_key="scope", view_as_context={"user_id": 4}))
        self.assertEqual(self._run()[0], 0)  # default policy: omit
        (key,) = list(self.store.objects)
        self.assertEqual(archive.validate_archive(self.store.objects[key])["omitted_columns"], ["view_as_context", "view_as_role_key", "view_as_scope_key"])
        self.assertEqual(self._restore(key)["inserted"]["events"], 1)
        with self.Session() as db:
            row = db.query(HubProductEventV2).one()
            self.assertEqual((row.view_as_role_key, row.view_as_scope_key, row.view_as_context), (None, None, None))
            self.assertEqual(row.actor_email, "a@renphil.org")  # the real actor identity is kept

    def test_restore_starts_from_the_registry_and_refuses_everything_else_without_writing(self) -> None:
        self._add(self._event(timedelta(days=400)))
        self.assertEqual(self._run()[0], 0)
        (key,) = list(self.store.objects)
        good = self.store.objects[key]
        other = archive.make_archive(environment="sandbox", events=[{"id": 9, "event_id": "other", "environment": "sandbox",
                                                                      "occurred_at": NOW, "received_at": NOW}])
        self.store.objects["hub-product-analytics/sandbox/events/v3/2025/leftover.zip"] = other
        cases = {
            "unregistered leftover": dict(key="hub-product-analytics/sandbox/events/v3/2025/leftover.zip", environment="sandbox"),
            "unknown key": dict(key="hub-product-analytics/sandbox/events/v3/2025/nope.zip", environment="sandbox"),
            "other environment": dict(key=key, environment="production"),
        }
        for name, kwargs in cases.items():
            with self.subTest(name), self.Session() as db:
                with self.assertRaises(archive.ArchiveRestoreError):
                    archive.restore_registered_archive(db, self.store, **kwargs)
                db.rollback()
        # Missing object, replaced object (digest mismatch) and tampered bytes: refused before any write.
        tampered = good[:100] + bytes([good[100] ^ 0xFF]) + good[101:]  # one byte flipped inside a member
        self.assertNotEqual(tampered, good)
        for name, replacement in {"missing": None, "replaced": other, "tampered": tampered}.items():
            with self.subTest(name), self.Session() as db:
                if replacement is None:
                    del self.store.objects[key]
                else:
                    self.store.objects[key] = replacement
                try:
                    with self.assertRaises(archive.ArchiveRestoreError) as caught:
                        archive.restore_registered_archive(db, self.store, key=key, environment="sandbox")
                    self.assertIn("nothing was written", str(caught.exception).lower())
                finally:
                    self.store.objects[key] = good
                db.rollback()
        self.assertEqual(self._counts(), (0, 0))
        with self.Session() as db:
            dry = archive.restore_registered_archive(db, self.store, key=key, environment="sandbox", dry_run=True)
            db.rollback()
        self.assertEqual((dry["dry_run"], dry["records"], self._counts()), (True, 1, (0, 0)))
        self.assertEqual(self._restore(key)["inserted"]["events"], 1)
        self.assertEqual(self._counts(), (1, 0))

    def test_restore_skips_records_of_deleted_resources_unless_recovery_mode_is_explicit(self) -> None:
        self._add(self._event(timedelta(days=400)), self._event(timedelta(days=401), event_name="link.clicked", component_type="link",
                                                             action="open_link", component_id="component-live", target_id=None, page_id="tab-live"))
        self.assertEqual(self._run()[0], 0)
        (key,) = list(self.store.objects)
        with self.engine.begin() as connection:  # the page and its component are deleted after the archive was written
            connection.execute(text("DELETE FROM components WHERE link = 'component-live'"))
            connection.execute(text("INSERT INTO components (link) VALUES ('component-other')"))
        result = self._restore(key)
        self.assertEqual((result["inserted"]["events"], result["deleted_resource_skipped"]["events"], result["already_present"]["events"]), (1, 1, 0))
        self.assertEqual(self._counts(), (1, 0))
        recovered = self._restore(key, include_deleted_resources=True)
        self.assertEqual((recovered["inserted"]["events"], recovered["deleted_resource_skipped"]["events"], recovered["already_present"]["events"]), (1, 0, 1))
        self.assertEqual(self._counts(), (2, 0))

    def test_restore_script_reports_and_refuses_like_the_entry_point(self) -> None:
        from scripts import restore_product_analytics_archive as restore_job

        self._add(self._event(timedelta(days=400)))
        self.assertEqual(self._run()[0], 0)
        (key,) = list(self.store.objects)
        lines: list[str] = []
        code = restore_job.run(["--key", key, "--dry-run"], engine=self.engine, store=self.store, environment="sandbox", out=lines.append)
        self.assertEqual((code, self._counts()), (0, (0, 0)))
        self.assertTrue(lines[0].startswith("dry run: ") and "Nothing was written" in lines[0], lines)
        lines.clear()
        code = restore_job.run(["--key", "p/unknown.zip"], engine=self.engine, store=self.store, environment="sandbox", out=lines.append)
        self.assertEqual((code, self._counts()), (1, (0, 0)))
        self.assertIn("not a registered archive", lines[0])
        lines.clear()
        code = restore_job.run(["--key", key], engine=self.engine, store=self.store, environment="sandbox", out=lines.append)
        self.assertEqual((code, self._counts()), (0, (1, 0)))
        self.assertIn("restored 1 events record(s)", lines[0])

    # ── reconciliation of leftovers (memory store; locks are exercised on PostgreSQL) ──

    def _clocked_store(self, start: datetime) -> tuple[archive.MemoryArchiveStore, list[datetime]]:
        clock = [start]
        return archive.MemoryArchiveStore(clock=lambda: clock[0]), clock

    def _identity(self) -> str:
        with self.Session() as db:
            return archive.registry_identity(db)

    def _leftover(self, store, rows, *, suffix: str, marker: str | None = ...) -> str:
        """An object exactly like one the job writes (format, key, registry-id
        marker) for `rows`, which stay in the live table: a leftover whose
        batch never committed."""
        records = [archive.archive_row(row, "events", exclude=archive.VIEW_AS_COLUMNS) for row in rows]
        payload = archive.make_archive(environment="sandbox", events=records, exclude_columns=archive.VIEW_AS_COLUMNS,
                                       metadata={"table": "events", "selection": "age_expired_not_deleted_resource"})
        key = f"hub-product-analytics/sandbox/events/v3/2025/leftover-{suffix}.zip"
        metadata = {"sha256": "x", "format-version": "3"}
        if marker is ...:
            metadata[archive.REGISTRY_ID_METADATA] = self._identity()
        elif marker is not None:
            metadata[archive.REGISTRY_ID_METADATA] = marker
        store.put(key, payload, metadata)
        return key

    def _identity_rows(self) -> int:
        with self.engine.connect() as connection:
            return int(connection.execute(text("SELECT count(*) FROM hub_product_analytics_archive_identity")).scalar_one())

    def test_registry_identity_is_created_once_and_stable(self) -> None:
        first = self._identity()
        self.assertEqual(len(first), 36)
        self.assertEqual(self._identity(), first)
        self.assertEqual(self._run(store=archive.MemoryArchiveStore())[0], 0)
        self.assertEqual(self._identity(), first)

    def test_dry_run_check_keys_and_report_only_reconciliation_never_initialise_the_identity(self) -> None:
        """Read-only commands write nothing: with an empty identity table they
        say so and leave it empty; only the live job (or the create script)
        initialises it."""
        from scripts import reconcile_product_analytics_archives as reconcile_job

        self._add(self._event(timedelta(days=400)), self._event(timedelta(days=5), target_id="tab-deleted", page_id="tab-deleted"))
        self.assertEqual(self._identity_rows(), 0)
        with self.Session() as db:
            self.assertEqual(archive.lookup_registry_identity(db), (None, None))
        code, lines = self._run("--dry-run")
        self.assertEqual(code, 0, lines)
        self.assertIn("dry run: registry identity not initialised; the live job (or python -m scripts.create_product_analytics_archive_tables) initialises it. Nothing was written.", lines)
        self.assertEqual((self._identity_rows(), self._counts(), self.store.calls), (0, (2, 0), []))
        lines = []
        code = reconcile_job.run([], engine=self.engine, store=self.store, environment="sandbox", now=NOW, out=lines.append)
        self.assertEqual(code, 0, lines)
        self.assertIn("registry identity not initialised: ownership of objects cannot be established; a deleting run would refuse.", lines)
        self.assertEqual(self._identity_rows(), 0)
        # A deleting reconciliation refuses rather than initialising.
        lines = []
        code = reconcile_job.run(["--delete"], engine=self.engine, store=self.store, environment="sandbox", now=NOW, out=lines.append)
        self.assertEqual(code, 2, lines)
        self.assertIn("not initialised", lines[0])
        self.assertEqual(self._identity_rows(), 0)
        # The live job initialises it, before its cleanup, and then works.
        code, lines = self._run()
        self.assertEqual((code, self._identity_rows(), self._counts()), (0, 1, (0, 0)))

    def test_preconditions_are_checked_before_any_cleanup_delete(self) -> None:
        """Stale (deleted-resource) rows are present, and a precondition fails:
        the run must refuse with every row intact — the cleanup must not have
        run first."""
        stale = [self._event(timedelta(days=5 + i), target_id="tab-deleted", page_id="tab-deleted") for i in range(3)]
        self._add(*stale, self._event(timedelta(days=400)))
        with self.Session() as db:  # the identity is bound to other storage
            archive.registry_identity(db, storage=("bucket-a", "hub-product-analytics"))
        code, lines = self._run(storage=("bucket-b", "hub-product-analytics"))
        self.assertEqual(code, 2, lines)
        self.assertIn("bound to a different bucket/prefix", lines[0])
        self.assertTrue(lines[0].endswith("Nothing was deleted or uploaded."), lines)
        self.assertEqual((self._counts(), self.store.calls), ((4, 0), []))
        code, lines = self._run("--dry-run", storage=("bucket-b", "hub-product-analytics"))
        self.assertEqual((code, self._counts()), (2, (4, 0)))
        # A missing identity table also refuses before the cleanup.
        with self.engine.begin() as connection:
            connection.execute(text("DROP TABLE hub_product_analytics_archive_identity"))
        code, lines = self._run()
        self.assertEqual(code, 2, lines)
        self.assertIn("registry identity table", lines[0])
        self.assertEqual((self._counts(), self.store.calls), ((4, 0), []))

    def test_failure_messages_name_what_the_cleanup_and_committed_batches_already_deleted(self) -> None:
        stale = [self._event(timedelta(days=5 + i), target_id="tab-deleted", page_id="tab-deleted") for i in range(2)]
        expired = [self._event(timedelta(days=400 + i)) for i in range(150)]
        self._add(*stale, *expired, self._engagement(timedelta(days=5), tab_id="tab-deleted"))

        class FailsOnSecondPut(archive.MemoryArchiveStore):
            puts = 0

            def put(self, key, data, metadata=None, *, if_none_match=False):
                self.puts += 1
                if self.puts == 2:
                    raise OSError("storage failed")
                super().put(key, data, metadata, if_none_match=if_none_match)

        code, lines = self._run("--batch-size", "100", store=FailsOnSecondPut())
        self.assertEqual(code, 1, lines)
        failure = next(line for line in lines if line.startswith("archive failed for events: OSError"))
        self.assertIn("The failed batch and everything after it were not registered or deleted.", failure)
        self.assertIn("Before this, the cleanup had deleted 2 events, 1 engagements row(s) referencing deleted resources (not archived); archived and deleted in committed batches: 100 events in 1 committed batch(es).", failure)
        self.assertNotIn("Nothing was deleted", failure)
        self.assertEqual(self._counts(), (50, 0))
        # A storage failure on the very first batch, after the cleanup ran, says the cleanup's part.
        self.setUp()
        self._add(self._event(timedelta(days=5), target_id="tab-deleted", page_id="tab-deleted"), self._event(timedelta(days=400)))

        class FailingStore(archive.MemoryArchiveStore):
            def put(self, key, data, metadata=None, *, if_none_match=False):
                raise OSError("storage failed")

        code, lines = self._run(store=FailingStore())
        self.assertEqual(code, 1, lines)
        failure = next(line for line in lines if line.startswith("archive failed for events: OSError"))
        self.assertIn("the cleanup had deleted 1 events, 0 engagements row(s) referencing deleted resources (not archived); archived and deleted in committed batches: none.", failure)
        self.assertEqual(self._counts(), (1, 0))

    def test_reconcile_reports_by_default_and_deletes_only_redundant_leftovers_in_its_own_scope(self) -> None:
        store, clock = self._clocked_store(NOW - timedelta(hours=48))
        self._add(self._event(timedelta(days=400)), self._engagement(timedelta(days=400)))
        self.assertEqual(self._run(store=store)[0], 0)
        registered = set(store.objects)
        live_rows = [self._event(timedelta(days=410 + i)) for i in range(3)]
        self._add(*live_rows)
        redundant = self._leftover(store, live_rows, suffix="redundant")
        foreign = self._leftover(store, live_rows, suffix="foreign", marker="00000000-0000-0000-0000-000000000000")
        unmarked = self._leftover(store, live_rows, suffix="unmarked", marker=None)
        store.put("hub-product-analytics/sandbox/events/v3/2025/not-an-archive.zip", b"junk", {archive.REGISTRY_ID_METADATA: self._identity()})
        clock[0] = NOW - timedelta(minutes=5)
        recent = self._leftover(store, live_rows, suffix="recent")
        store.put("hub-product-analytics/production/events/v3/2025/other-env.zip", b"env")
        store.put("other-prefix/sandbox/events/v3/2025/other-prefix.zip", b"prefix")
        before = dict(store.objects)
        lines: list[str] = []
        with self.Session() as db:
            report = archive.reconcile_leftovers(db, store, environment="sandbox", prefix="hub-product-analytics", now=NOW, out=lines.append)
        self.assertEqual((report.listed, report.registered, report.too_recent, report.candidates, report.deleted), (7, 2, 1, 4, 0))
        self.assertEqual(store.objects, before)
        self.assertFalse(any(call == "delete" for call, _ in store.calls))
        with self.Session() as db:
            outcome = archive.reconcile_leftovers(db, store, environment="sandbox", prefix="hub-product-analytics", now=NOW, delete=True, out=lines.append)
        self.assertEqual(outcome.deleted_keys, [redundant])
        self.assertEqual((outcome.retained_foreign, outcome.retained_invalid, outcome.retained_not_redundant), (2, 1, 0))
        self.assertEqual({key for key, _ in outcome.retained}, {foreign, unmarked, "hub-product-analytics/sandbox/events/v3/2025/not-an-archive.zip"})
        self.assertEqual(set(store.objects), (set(before) - {redundant}))
        self.assertTrue(registered <= set(store.objects))
        self.assertEqual(self._counts(), (3, 0))  # the leftover's rows are still live: nothing was lost
        with self.Session() as db:
            again = archive.reconcile_leftovers(db, store, environment="sandbox", prefix="hub-product-analytics", now=NOW, delete=True)
            self.assertEqual((again.deleted, again.too_recent, again.retained_foreign, again.retained_invalid), (0, 1, 2, 1))
            later = archive.reconcile_leftovers(db, store, environment="sandbox", prefix="hub-product-analytics", now=NOW + timedelta(hours=25), delete=True)
        self.assertEqual((later.deleted_keys, later.too_recent), ([recent], 0))

    def test_reconcile_retains_leftovers_whose_records_are_no_longer_live_until_an_operator_reviews_them(self) -> None:
        store, _ = self._clocked_store(NOW - timedelta(hours=48))
        self._add(self._event(timedelta(days=400)))
        self.assertEqual(self._run(store=store)[0], 0)
        (registered,) = list(store.objects)
        rows = [self._event(timedelta(days=410)), self._event(timedelta(days=411))]
        self._add(*rows)
        leftover = self._leftover(store, rows, suffix="cleanup-deleted")
        with self.engine.begin() as connection:  # the cleanup later removed those rows (deleted resource)
            connection.execute(text("DELETE FROM hub_product_events WHERE event_id IN (:a, :b)"), {"a": rows[0].event_id, "b": rows[1].event_id})
        lines: list[str] = []
        with self.Session() as db:
            outcome = archive.reconcile_leftovers(db, store, environment="sandbox", prefix="hub-product-analytics", now=NOW, delete=True, out=lines.append)
        self.assertEqual((outcome.deleted, outcome.retained_not_redundant), (0, 1))
        self.assertEqual(outcome.retained[0][0], leftover)
        self.assertIn("2 of 2 record(s) have no identical live row", outcome.retained[0][1])
        self.assertIn(leftover, store.objects)
        with self.Session() as db:
            with self.assertRaises(ValueError):
                archive.reconcile_leftovers(db, store, environment="sandbox", prefix="hub-product-analytics", now=NOW, reviewed_keys=["other-prefix/x.zip"])
            refused = archive.reconcile_leftovers(db, store, environment="sandbox", prefix="hub-product-analytics", now=NOW, reviewed_keys=[registered], out=lines.append)
        self.assertEqual((refused.deleted, registered in store.objects), (0, True))
        self.assertTrue(any(line.startswith("reviewed key not deleted: not a leftover candidate") for line in lines), lines)
        with self.Session() as db:
            reviewed = archive.reconcile_leftovers(db, store, environment="sandbox", prefix="hub-product-analytics", now=NOW, reviewed_keys=[leftover], out=lines.append)
        self.assertEqual((reviewed.deleted_keys, leftover in store.objects, registered in store.objects), ([leftover], False, True))
        self.assertTrue(any(line == f"deleted reviewed leftover: {leftover}" for line in lines), lines)

    def test_reconcile_never_deletes_registered_objects_and_continues_after_a_failure(self) -> None:
        store, _ = self._clocked_store(NOW - timedelta(hours=48))
        self._add(self._event(timedelta(days=400)))
        self.assertEqual(self._run(store=store)[0], 0)
        (registered,) = list(store.objects)
        rows_a, rows_b = [self._event(timedelta(days=410))], [self._event(timedelta(days=411))]
        self._add(*rows_a, *rows_b)
        a = self._leftover(store, rows_a, suffix="a")
        b = self._leftover(store, rows_b, suffix="b")

        class FailsOnB(archive.MemoryArchiveStore):
            def delete(self, key):
                if key == b:
                    raise OSError("storage unreachable")
                super().delete(key)

        flaky = FailsOnB(clock=store.clock)
        flaky.objects, flaky.metadata, flaky.modified = store.objects, store.metadata, store.modified
        lines: list[str] = []
        with self.Session() as db:
            outcome = archive.reconcile_leftovers(db, flaky, environment="sandbox", prefix="hub-product-analytics", now=NOW, delete=True, out=lines.append)
        self.assertEqual((outcome.candidates, outcome.deleted_keys, outcome.failed), (2, [a], 1))
        self.assertTrue(any(line.startswith(f"failed on {b}: OSError") for line in lines), lines)
        self.assertIn(registered, store.objects)
        with self.Session() as db:
            again = archive.reconcile_leftovers(db, store, environment="sandbox", prefix="hub-product-analytics", now=NOW, delete=True)
        self.assertEqual((again.deleted_keys, set(store.objects)), ([b], {registered}))
        # A registered archive whose object is gone is an alert, never an action.
        del store.objects[registered]
        with self.Session() as db:
            alert = archive.reconcile_leftovers(db, store, environment="sandbox", prefix="hub-product-analytics", now=NOW, delete=True, out=lines.append)
        self.assertEqual(alert.registered_missing, [registered])
        self.assertTrue(any(line.startswith(f"ALERT: registered archive {registered} was not in the listing") for line in lines), lines)

    def test_reconcile_script_is_report_only_by_default_and_uses_exit_codes(self) -> None:
        from scripts import reconcile_product_analytics_archives as reconcile_job

        store, _ = self._clocked_store(NOW - timedelta(hours=48))
        self._add(self._event(timedelta(days=400)))
        self.assertEqual(self._run(store=store)[0], 0)
        rows = [self._event(timedelta(days=410))]
        self._add(*rows)
        leftover = self._leftover(store, rows, suffix="script")
        lines: list[str] = []
        code = reconcile_job.run([], engine=self.engine, store=store, environment="sandbox", now=NOW, out=lines.append)
        self.assertEqual((code, len(store.objects)), (0, 2))
        self.assertTrue(any(line.startswith("report only: nothing was deleted.") for line in lines), lines)
        self.assertTrue(any(line.startswith(f"leftover (unregistered, age 48.0 h): {leftover}") for line in lines), lines)
        lines.clear()
        self.assertEqual(reconcile_job.run(["--delete", "--grace-hours", "0.5"], engine=self.engine, store=store, environment="sandbox", now=NOW, out=lines.append), 2)
        self.assertEqual(reconcile_job.run(["--delete-reviewed", "other-prefix/x.zip"], engine=self.engine, store=store, environment="sandbox", now=NOW, out=lines.append), 2)
        self.assertEqual(len(store.objects), 2)
        lines.clear()
        code = reconcile_job.run(["--delete"], engine=self.engine, store=store, environment="sandbox", now=NOW, out=lines.append)
        self.assertEqual((code, len(store.objects)), (0, 1))
        self.assertTrue(any("deleted 1; retained 0 not redundant, 0 foreign, 0 invalid" in line for line in lines), lines)
        self.assertFalse(any("a@renphil.org" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
