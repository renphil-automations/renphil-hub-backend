"""The archive job against a real, disposable PostgreSQL.

Opt-in: set PRODUCT_ANALYTICS_TEST_DATABASE_URL to an EMPTY throwaway database.
Covers what SQLite cannot: the deleted-resource predicate against the real
Hub tables, the session-level advisory lock that serialises concurrent runs,
and batch deletes with `rowcount` on PostgreSQL. Storage is in memory; no
real bucket is ever contacted.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

import app.db_v2.models as _models_package
from app.db_v2.database import BaseV2
from app.db_v2.models.component import ComponentV2
from app.db_v2.models.gridstack import GridstackV2
from app.db_v2.models.nav_tab import NavTabV2
from app.db_v2.models.product_analytics import HubProductAnalyticsArchiveV2, HubProductEngagementV2, HubProductEventV2
from app.db_v2.models.tab import TabV2
from app.services import gridstack_service
from app.services import product_analytics_archive as archive
from app.services import product_analytics_service as service
from scripts import archive_product_analytics as job

DATABASE_URL = os.environ.get("PRODUCT_ANALYTICS_TEST_DATABASE_URL", "").strip()
NOW = datetime.now(timezone.utc).replace(microsecond=0)


@unittest.skipUnless(DATABASE_URL, "PRODUCT_ANALYTICS_TEST_DATABASE_URL is not set")
class ArchiveJobPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if any(word in DATABASE_URL.lower() for word in ("prod", "neon.tech", "sandbox")):
            raise unittest.SkipTest("refusing a database URL that looks shared")
        # The whole V2 schema, so the Hub's own deletion and update services
        # can run against the locks (they touch content links, locks, …).
        for module in pkgutil.iter_modules(_models_package.__path__):
            importlib.import_module(f"app.db_v2.models.{module.name}")
        cls.engine = create_engine(DATABASE_URL, pool_size=8, pool_pre_ping=True)
        BaseV2.metadata.create_all(bind=cls.engine, checkfirst=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(
                "TRUNCATE hub_product_events, hub_product_engagements, hub_product_analytics_archives, components, gridstacks, tabs, nav_tabs CASCADE"
            ))
        self.Session = sessionmaker(bind=self.engine)
        with self.Session() as db:
            nav = NavTabV2(document_id="nav-live", slug=f"qa-{uuid.uuid4().hex[:8]}", title="QA nav")
            db.add(nav)
            db.flush()
            tab = TabV2(document_id="tab-live", title="QA tab", nav_tab_id=nav.id)
            db.add(tab)
            db.flush()
            grid = GridstackV2(document_id="tab-live", parent_tab_id=tab.id)
            db.add(grid)
            db.flush()
            db.add(ComponentV2(link="component-live", type="links", gridstack_id=grid.id))
            db.commit()
        self.store = archive.MemoryArchiveStore()

    def _event(self, age: timedelta, **fields) -> dict:
        at = NOW - age
        # Every key is present in every row: a multi-row insert takes its
        # column list from the first dict, so a key only some rows carry
        # would be silently dropped.
        values = dict(event_id=str(uuid.uuid4()), event_name="root_tab.selected", component_type="root_tab", action="select",
                      trigger="click", cascade_id=None, hub_user_id=1, actor_email="a@renphil.org", environment="sandbox",
                      page_route="/:nav_tab", component_id=None, target_id="tab-live", page_id="tab-live",
                      properties={"tab_kind": "root"}, occurred_at=at, received_at=at)
        values.update(fields)
        return values

    def _insert_events(self, *rows: dict) -> None:
        with self.engine.begin() as connection:
            connection.execute(HubProductEventV2.__table__.insert(), list(rows))

    def _count(self) -> int:
        with self.engine.connect() as connection:
            return int(connection.execute(select(func.count()).select_from(HubProductEventV2)).scalar_one())

    def _run(self, *argv: str, store=None) -> tuple[int, list[str]]:
        lines: list[str] = []
        code = job.run(list(argv), engine=self.engine, store=store or self.store, raw_retention="365", environment="sandbox", now=NOW, out=lines.append)
        return code, lines

    def test_end_to_end_archive_before_delete_on_postgres(self) -> None:
        live_old = self._event(timedelta(days=400))
        stale_old = self._event(timedelta(days=400), target_id="tab-deleted", page_id="tab-deleted")
        moved_component = self._event(timedelta(days=400), event_name="link.clicked", component_type="link", action="open_link",
                                      component_id="component-live", page_id="tab-deleted", target_id=None)
        recent = self._event(timedelta(days=3))
        other_env = self._event(timedelta(days=400), environment="production")
        self._insert_events(live_old, stale_old, moved_component, recent, other_env)
        code, lines = self._run("--dry-run")
        self.assertEqual(code, 0, lines)
        self.assertEqual((self.store.calls, self._count()), ([], 5))
        code, lines = self._run("--batch-size", "100")
        self.assertEqual(code, 0, lines)
        self.assertIn("deleted 1 events row(s) referencing deleted resources (not archived).", lines)
        self.assertEqual(self._count(), 2)
        with self.engine.connect() as connection:
            remaining = {row[0] for row in connection.execute(select(HubProductEventV2.event_id))}
        self.assertEqual(remaining, {recent["event_id"], other_env["event_id"]})
        (key,) = [k for k in self.store.objects if "/events/" in k]
        manifest = archive.validate_archive(self.store.objects[key])
        self.assertEqual((manifest["events"], manifest["environment"]), (2, "sandbox"))
        import json
        from io import BytesIO
        from zipfile import ZipFile
        with ZipFile(BytesIO(self.store.objects[key])) as zipped:
            archived = {json.loads(line)["event_id"] for line in zipped.read("events.jsonl").decode().splitlines()}
        self.assertEqual(archived, {live_old["event_id"], moved_component["event_id"]})
        with self.Session() as db:
            self.assertEqual(self._registry(db), {key: 2})
            self.assertEqual(archive.unregistered_keys(db, [key, "p/other.zip"]), ["p/other.zip"])
        # Idempotent: nothing left to do, nothing uploaded again.
        before = dict(self.store.objects)
        self.assertEqual(self._run()[0], 0)
        self.assertEqual(self.store.objects, before)

    def test_second_concurrent_run_is_refused_by_the_advisory_lock(self) -> None:
        self._insert_events(*[self._event(timedelta(days=400 + i)) for i in range(5)])
        blocking, inside_put, release = self._blocking_store()
        first: list[tuple[int, list[str]]] = []
        thread = threading.Thread(target=lambda: first.append(self._run(store=blocking)))
        thread.start()
        self.assertTrue(inside_put.wait(timeout=30))
        try:
            code, lines = self._run()
            self.assertEqual(code, job.EXIT_LOCKED, lines)
            self.assertEqual(self.store.calls, [])
            self.assertEqual(self._count(), 5)
        finally:
            release.set()
            thread.join(timeout=60)
        self.assertEqual(first[0][0], 0, first[0][1])
        self.assertEqual(self._count(), 0)
        self.assertEqual(len(blocking.objects), 1)
        # The lock was released: a later run proceeds normally.
        self.assertEqual(self._run()[0], 0)

    def test_crash_after_upload_recovers_on_postgres_without_a_second_upload(self) -> None:
        rows = [self._event(timedelta(days=400 + i)) for i in range(3)]
        self._insert_events(*rows)

        class CrashAfterUpload(archive.MemoryArchiveStore):
            crashed = False

            def get(self, key):
                if not self.crashed:
                    self.crashed = True
                    raise RuntimeError("process killed")
                return super().get(key)

        store = CrashAfterUpload()
        code, _ = self._run(store=store)
        self.assertEqual((code, self._count(), len(store.objects)), (1, 3, 1))
        store.calls.clear()
        code, lines = self._run(store=store)
        self.assertEqual((code, self._count()), (0, 0))
        self.assertEqual([call for call, _ in store.calls], ["head", "get"])

    def _blocking_store(self):
        inside_put, release = threading.Event(), threading.Event()

        class BlockingStore(archive.MemoryArchiveStore):
            def put(self, key, data, metadata=None, *, if_none_match=False):
                inside_put.set()
                release.wait(timeout=30)
                super().put(key, data, metadata, if_none_match=if_none_match)

        return BlockingStore(), inside_put, release

    def _archived_records(self, store) -> list[dict]:
        import json
        from io import BytesIO
        from zipfile import ZipFile
        records: list[dict] = []
        for payload in store.objects.values():
            with ZipFile(BytesIO(payload)) as zipped:
                records += [json.loads(line) for line in zipped.read("events.jsonl").decode().splitlines()]
        return records

    def test_rows_are_locked_while_their_archive_uploads_so_a_concurrent_update_cannot_diverge(self) -> None:
        """`SELECT … FOR UPDATE` holds the batch's rows for the whole upload:
        a concurrent UPDATE waits (here: hits `lock_timeout`), and once the
        job commits it finds the rows gone (0 rows). The archived record is
        therefore the exact version that was deleted."""
        from sqlalchemy.exc import OperationalError

        rows = [self._event(timedelta(days=400 + i)) for i in range(3)]
        self._insert_events(*rows)
        store, inside_put, release = self._blocking_store()
        first: list[tuple[int, list[str]]] = []
        thread = threading.Thread(target=lambda: first.append(self._run(store=store)))
        thread.start()
        self.assertTrue(inside_put.wait(timeout=30))
        update = text("UPDATE hub_product_events SET properties = '{\"tab_kind\": \"changed\"}'::json WHERE event_id = :e")
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SET LOCAL lock_timeout ='1500ms'"))
                with self.assertRaises(OperationalError) as caught:
                    connection.execute(update, {"e": rows[0]["event_id"]})
                connection.rollback()
            self.assertIn("lock", str(caught.exception).lower())
            # A writer that waits without a timeout is simply blocked.
            late: list[int] = []

            def waiting_writer() -> None:
                with self.engine.begin() as connection:
                    late.append(connection.execute(update, {"e": rows[1]["event_id"]}).rowcount)

            waiter = threading.Thread(target=waiting_writer)
            waiter.start()
            waiter.join(timeout=1.5)
            self.assertTrue(waiter.is_alive(), "the update must block while the rows are locked")
        finally:
            release.set()
            thread.join(timeout=60)
        waiter.join(timeout=30)
        self.assertEqual(first[0][0], 0, first[0][1])
        self.assertEqual(late, [0])
        self.assertEqual(self._count(), 0)
        archived = {record["event_id"]: record["properties"] for record in self._archived_records(store)}
        self.assertEqual(archived, {row["event_id"]: {"tab_kind": "root"} for row in rows})

    # ── resource deletion vs. archiving (real concurrent transactions) ───

    CUTOFF = NOW - timedelta(days=365)

    def _add_page(self, document_id: str) -> None:
        with self.engine.begin() as connection:
            tab_id = connection.execute(
                text("INSERT INTO tabs (document_id, title) VALUES (:d, 'page') RETURNING id"), {"d": document_id}
            ).scalar_one()
            connection.execute(text("INSERT INTO gridstacks (document_id, parent_tab_id) VALUES (:d, :t)"), {"d": document_id, "t": tab_id})

    def _add_component(self, link: str, *, page: str = "tab-live") -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("INSERT INTO components (link, type, gridstack_id) SELECT :l, 'links', id FROM gridstacks WHERE document_id = :d"),
                {"l": link, "d": page},
            )

    def _component_rows(self, *links: str, age: int = 400) -> list[dict]:
        return [self._event(timedelta(days=age + i), event_name="link.clicked", component_type="link", action="open_link",
                            component_id=link, target_id=None, page_id="tab-live") for i, link in enumerate(links)]

    def _hub_delete_page(self, connection, document_id: str, *component_links: str) -> None:
        """Delete a page the way `gridstack_service` does: its components,
        its canvases, then the tab row."""
        for link in component_links:
            connection.execute(text("DELETE FROM components WHERE link = :l"), {"l": link})
        connection.execute(text("DELETE FROM gridstacks WHERE document_id = :d"), {"d": document_id})
        connection.execute(text("DELETE FROM tabs WHERE document_id = :d"), {"d": document_id})

    def _archive(self, store, *, lines=None, **kwargs) -> archive.ArchiveOutcome:
        with self.Session() as db:
            return archive.archive_expired(db, store, table="events", environment="sandbox", cutoff=self.CUTOFF, prefix="p",
                                           out=(lines.append if lines is not None else None), **kwargs)

    def _ddl(self, statement: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(statement))

    def _registry(self, db) -> dict[str, int]:
        """Registered keys → record counts."""
        return {row.key: row.records for row in db.query(HubProductAnalyticsArchiveV2)}

    def _wait_for_objects(self, store, count: int, *, seconds: float = 10) -> None:
        for _ in range(int(seconds * 10)):
            if len(store.objects) >= count:
                return
            threading.Event().wait(0.1)

    def _blocked(self, statement: str, *, timeout_ms: int = 1200) -> bool:
        """Does `statement` wait on a lock right now? (own connection, `lock_timeout`)"""
        with self.engine.connect() as connection:
            connection.execute(text(f"SET LOCAL lock_timeout ='{timeout_ms}ms'"))
            try:
                connection.execute(text(statement))
            except OperationalError as exc:
                connection.rollback()
                if "lock" not in str(exc).lower():
                    raise
                return True
            connection.rollback()
            return False

    def _start_blocked_job(self, *rows: dict):
        """Insert `rows`, start the job in a thread, wait until it is inside the upload."""
        self._insert_events(*rows)
        store, inside_put, release = self._blocking_store()
        result: list[tuple[int, list[str]]] = []
        thread = threading.Thread(target=lambda: result.append(self._run(store=store)))
        thread.start()
        self.assertTrue(inside_put.wait(timeout=30))
        return store, release, thread, result

    # ── lock strength follows the live catalog ───────────────────────────

    def test_lock_strength_follows_the_live_catalog_not_the_models(self) -> None:
        """`FOR KEY SHARE` blocks an UPDATE of the join column only where a
        unique index covers it; `document_id` never has one (the models
        declare plain indexes) and the dev database has none on
        `components.link` either, although the model declares `unique=True`.
        The job reads `pg_index` and uses `FOR SHARE` wherever the column is
        not unique, so the join column cannot change under an upload."""
        with self.Session() as db:
            modes = archive.resource_lock_modes(db)
        # The schema created from the models: only components.link is unique.
        self.assertEqual(modes, {"nav_tabs": "share", "tabs": "share", "gridstacks": "share", "components": "key_share"})
        rows = [self._event(timedelta(days=400)),
                self._event(timedelta(days=401), event_name="navigation_tab.selected", component_type="navigation_tab",
                            target_id="nav-live", page_id=None, properties={}),
                *self._component_rows("component-live")]
        store, release, thread, result = self._start_blocked_job(*rows)
        try:
            for statement in ("UPDATE tabs SET document_id = 'tab-renamed' WHERE document_id = 'tab-live'",
                              "UPDATE gridstacks SET document_id = 'tab-renamed' WHERE document_id = 'tab-live'",
                              "UPDATE nav_tabs SET document_id = 'nav-renamed' WHERE document_id = 'nav-live'",
                              "UPDATE gridstacks SET settings = '{}'::json WHERE document_id = 'tab-live'",
                              "UPDATE components SET link = 'component-renamed' WHERE link = 'component-live'"):
                with self.subTest(statement):
                    self.assertTrue(self._blocked(statement), statement)
            self.assertFalse(self._blocked("UPDATE components SET title = 'edited' WHERE link = 'component-live'"))
        finally:
            release.set()
            thread.join(timeout=60)
        self.assertEqual(result[0][0], 0, result[0][1])
        self.assertTrue(any(line.startswith("resource locks while a batch uploads: nav_tabs FOR SHARE") and "components FOR KEY SHARE" in line
                            for line in result[0][1]), result[0][1])
        self.assertEqual(self._count(), 0)

        # The dev database's shape: no unique index on components.link at all.
        self._ddl("DROP INDEX ix_components_link")
        self.addCleanup(self._ddl, "CREATE UNIQUE INDEX IF NOT EXISTS ix_components_link ON components (link)")
        with self.Session() as db:
            self.assertEqual(archive.resource_lock_modes(db)["components"], "share")
        store, release, thread, result = self._start_blocked_job(*self._component_rows("component-live", age=500))
        try:
            self.assertTrue(self._blocked("UPDATE components SET link = 'component-renamed' WHERE link = 'component-live'"))
            self.assertTrue(self._blocked("UPDATE components SET title = 'edited' WHERE link = 'component-live'"))
        finally:
            release.set()
            thread.join(timeout=60)
        self.assertEqual(result[0][0], 0, result[0][1])

        # With a unique index on tabs.document_id the weaker lock is enough again.
        self._ddl("CREATE UNIQUE INDEX qa_tabs_document_id_uq ON tabs (document_id)")
        self.addCleanup(self._ddl, "DROP INDEX IF EXISTS qa_tabs_document_id_uq")
        with self.Session() as db:
            self.assertEqual(archive.resource_lock_modes(db)["tabs"], "key_share")
        store, release, thread, result = self._start_blocked_job(self._event(timedelta(days=600)))
        try:
            self.assertTrue(self._blocked("UPDATE tabs SET document_id = 'tab-renamed' WHERE document_id = 'tab-live'"))
            self.assertFalse(self._blocked("UPDATE tabs SET title = 'renamed' WHERE document_id = 'tab-live'"))
            self.assertTrue(self._blocked("DELETE FROM tabs WHERE document_id = 'tab-live'"))
        finally:
            release.set()
            thread.join(timeout=60)
        self.assertEqual(result[0][0], 0, result[0][1])

    def test_the_hub_services_own_deletion_and_update_paths_wait_for_the_batch(self) -> None:
        """The real service functions, not hand-written SQL: the page
        deletion (`delete_tab_subtree_by_document_id_v2`) and the tab rename
        (`update_tab_by_document_id_v2`) wait for the batch; a component
        content edit (`update_component_content`) proceeds under the
        key-share lock of this schema."""
        rows = [self._event(timedelta(days=400)), *self._component_rows("component-live")]
        store, release, thread, result = self._start_blocked_job(*rows)
        deleted: list[dict] = []
        errors: list[BaseException] = []

        def hub_delete() -> None:
            try:
                with self.Session() as db:
                    deleted.append(gridstack_service.delete_tab_subtree_by_document_id_v2(db, "tab-live"))
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assertions below
                errors.append(exc)

        deleter = threading.Thread(target=hub_delete)
        try:
            with self.Session() as db:
                db.execute(text("SET LOCAL lock_timeout ='1200ms'"))
                with self.assertRaises(OperationalError):
                    gridstack_service.update_tab_by_document_id_v2(db, "tab-live", title="Renamed while archiving")
                db.rollback()
            with self.Session() as db:
                db.execute(text("SET LOCAL lock_timeout ='1200ms'"))
                self.assertIsNotNone(gridstack_service.update_component_content(db, "component-live", title="Edited while archiving"))
            deleter.start()
            deleter.join(timeout=1.5)
            self.assertEqual(errors, [])
            self.assertTrue(deleter.is_alive(), "the Hub's own page deletion must wait for the batch")
        finally:
            release.set()
            thread.join(timeout=60)
        deleter.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(result[0][0], 0, result[0][1])
        self.assertEqual(deleted[0]["deleted_count"], 1)
        self.assertEqual(self._count(), 0)
        self.assertEqual({r["event_id"] for r in self._archived_records(store)}, {r["event_id"] for r in rows})
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT count(*) FROM tabs")).scalar_one(), 0)

    # ── enforced bounds on the lock hold ─────────────────────────────────

    def _stalled_store(self, stall: str):
        inside, release = threading.Event(), threading.Event()

        class Stalled(archive.MemoryArchiveStore):
            def put(self, key, data, metadata=None, *, if_none_match=False):
                if stall == "put":
                    inside.set()
                    release.wait(timeout=120)
                super().put(key, data, metadata, if_none_match=if_none_match)

            def get(self, key):
                if stall == "get":
                    inside.set()
                    release.wait(timeout=120)
                return super().get(key)

        return Stalled(), inside, release

    def _assert_stall_is_abandoned(self, stall: str) -> None:
        rows = self._component_rows("component-live", "component-live")
        self._insert_events(*rows)
        store, inside, release = self._stalled_store(stall)
        result: list[tuple[int, list[str]]] = []
        started = datetime.now(timezone.utc)
        with patch.object(archive, "STORAGE_CALL_DEADLINE_S", 1.0):
            thread = threading.Thread(target=lambda: result.append(self._run(store=store)))
            thread.start()
            self.assertTrue(inside.wait(timeout=30))
            done: list[int] = []

            def deleter() -> None:
                with self.engine.begin() as connection:
                    done.append(connection.execute(text("DELETE FROM components WHERE link = 'component-live'")).rowcount)

            waiter = threading.Thread(target=deleter)
            waiter.start()
            thread.join(timeout=30)
        self.assertFalse(thread.is_alive(), "the job must give the batch up at the deadline")
        waiter.join(timeout=30)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        self.assertLess(elapsed, 15, elapsed)
        code, lines = result[0]
        self.assertEqual(code, 1, lines)
        self.assertTrue(any("ArchiveTimeoutError: storage" in line and "did not complete within 1 s" in line for line in lines), lines)
        self.assertEqual((done, self._count()), ([1], 2))
        # The abandoned call completes later: the object lands, but nothing
        # registered it, so it is not an archive.
        release.set()
        self._wait_for_objects(store, 1)
        self.assertEqual(len(store.objects), 1)
        self.assertEqual({r["event_id"] for r in self._archived_records(store)}, {r["event_id"] for r in rows})
        with self.Session() as db:
            self.assertEqual(self._registry(db), {})
            self.assertEqual(archive.unregistered_keys(db, store.objects), list(store.objects))

    def test_a_stalled_upload_is_abandoned_at_the_deadline_and_releases_the_locks(self) -> None:
        self._assert_stall_is_abandoned("put")

    def test_a_stalled_readback_is_abandoned_at_the_deadline_and_releases_the_locks(self) -> None:
        self._assert_stall_is_abandoned("get")

    def test_a_late_upload_after_the_deadline_can_land_but_is_never_an_archive(self) -> None:
        """Codex's ordering, reproduced exactly: the batch's PUT stalls past
        the deadline; the job rolls back and its locks are gone; the
        referenced page is deleted and committed; the PUT is released and
        the object appears — containing that page's records. The job cannot
        cancel the request, so this object CAN exist. What makes the
        guarantee hold is that it is not registered: it is not an archive,
        the next run never reuses it, and `--check-keys` names it for
        reconciliation. The leftover-object policy is Chris's decision."""
        page_rows = [self._event(timedelta(days=400)), self._event(timedelta(days=401))]
        rows = [*page_rows, *self._component_rows("component-live")]
        self._insert_events(*rows)
        store, inside, release = self._stalled_store("put")
        result: list[tuple[int, list[str]]] = []
        with patch.object(archive, "STORAGE_CALL_DEADLINE_S", 1.0):
            thread = threading.Thread(target=lambda: result.append(self._run(store=store)))
            thread.start()
            self.assertTrue(inside.wait(timeout=30))
            thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0][0], 1, result[0][1])
        self.assertEqual((self._count(), len(store.objects)), (3, 0))
        # Locks released: the referenced page and component are deleted and committed.
        self.assertFalse(self._blocked("DELETE FROM components WHERE link = 'component-live'"))
        with self.engine.begin() as connection:
            self._hub_delete_page(connection, "tab-live", "component-live")
        # The abandoned PUT completes afterwards: the object exists and holds the deleted page's records.
        release.set()
        self._wait_for_objects(store, 1)
        self.assertEqual(len(store.objects), 1)
        self.assertEqual({r["event_id"] for r in self._archived_records(store)}, {r["event_id"] for r in rows})
        (leftover,) = list(store.objects)
        with self.Session() as db:
            self.assertEqual(self._registry(db), {})
            self.assertEqual(archive.unregistered_keys(db, [leftover]), [leftover])
        # The next run deletes those rows as deleted-resource rows, archives
        # nothing of them, and never registers or reuses the leftover.
        self._add_page("tab-after")
        self._add_component("component-after", page="tab-after")
        self._insert_events(self._event(timedelta(days=1), target_id="tab-after", page_id="tab-after"))
        code, lines = self._run(store=store)
        self.assertEqual(code, 0, lines)
        self.assertIn("deleted 3 events row(s) referencing deleted resources (not archived).", lines)
        self.assertEqual((len(store.objects), self._count()), (1, 1))
        with self.Session() as db:
            self.assertEqual(self._registry(db), {})
            self.assertEqual(archive.unregistered_keys(db, [leftover]), [leftover])

    def test_a_session_lost_after_registration_before_the_commit_registers_and_deletes_nothing(self) -> None:
        rows = self._component_rows("component-live", "component-live")
        self._insert_events(*rows)
        real = archive.register_archive

        def register_then_lose_the_session(db, **kwargs):
            registered = real(db, **kwargs)
            db.execute(text("SELECT pg_terminate_backend(pg_backend_pid())"))
            return registered

        with patch.object(archive, "register_archive", register_then_lose_the_session):
            code, lines = self._run()
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith("archive failed for events: OperationalError") for line in lines), lines)
        self.assertEqual((self._count(), len(self.store.objects)), (2, 1))
        with self.Session() as db:
            self.assertEqual(self._registry(db), {})
            self.assertEqual(archive.unregistered_keys(db, self.store.objects), list(self.store.objects))
        # A later run finds the identical object, verifies it and registers it with the delete.
        code, lines = self._run()
        self.assertEqual(code, 0, lines)
        self.assertTrue(any("[already present, verified sha256" in line and ", registered]" in line for line in lines), lines)
        with self.Session() as db:
            self.assertEqual(self._registry(db), {next(iter(self.store.objects)): 2})
        self.assertEqual(self._count(), 0)

    def test_catalog_lock_selection_ignores_partial_expression_multicolumn_and_invalid_unique_indexes(self) -> None:
        def tabs_mode() -> str:
            with self.Session() as db:
                return archive.resource_lock_modes(db)["tabs"]

        self.assertEqual(tabs_mode(), "share")
        for name, ddl in (
            ("qa_tabs_partial", "CREATE UNIQUE INDEX qa_tabs_partial ON tabs (document_id) WHERE title IS NOT NULL"),
            ("qa_tabs_expr", "CREATE UNIQUE INDEX qa_tabs_expr ON tabs (lower(document_id))"),
            ("qa_tabs_multi", "CREATE UNIQUE INDEX qa_tabs_multi ON tabs (document_id, title)"),
        ):
            with self.subTest(name):
                self._ddl(ddl)
                self.addCleanup(self._ddl, f"DROP INDEX IF EXISTS {name}")
                self.assertEqual(tabs_mode(), "share", name)
                self._ddl(f"DROP INDEX {name}")
        self._ddl("CREATE UNIQUE INDEX qa_tabs_uq ON tabs (document_id)")
        self.addCleanup(self._ddl, "DROP INDEX IF EXISTS qa_tabs_uq")
        self.assertEqual(tabs_mode(), "key_share")
        # An index PostgreSQL considers invalid (as after a failed CONCURRENTLY build) does not count.
        self._ddl("UPDATE pg_index SET indisvalid = false WHERE indexrelid = 'qa_tabs_uq'::regclass")
        self.assertEqual(tabs_mode(), "share")
        self._ddl("DROP INDEX qa_tabs_uq")
        self.assertEqual(tabs_mode(), "share")

    # ── reconciliation of leftovers under real concurrency ───────────────
    # The store fake behaves like the real R2 (verified 2026-11-04): DELETE is
    # unconditional, so a HEAD followed by a DELETE is never a compare-and-delete.

    PREFIX = "hub-product-analytics"

    def _reconcile(self, store, *, delete=True, grace_hours=0.0, now=None, lock_timeout_ms=800, lines=None, reviewed_keys=None) -> archive.ReconcileOutcome:
        with self.Session() as db:
            return archive.reconcile_leftovers(db, store, environment="sandbox", prefix=self.PREFIX, now=now, grace_hours=grace_hours,
                                               delete=delete, reviewed_keys=reviewed_keys, lock_timeout_ms=lock_timeout_ms,
                                               out=(lines.append if lines is not None else None))

    def _identity(self) -> str:
        with self.Session() as db:
            return archive.registry_identity(db)

    def _old_store(self) -> archive.MemoryArchiveStore:
        return archive.MemoryArchiveStore(clock=lambda: datetime.now(timezone.utc) - timedelta(hours=48))

    def _make_leftover(self, store, rows: list[dict], *, suffix: str, marker: str | None = ...) -> str:
        """Insert `rows` (they stay live) and put the object the job would have
        written for them, with the registry-id marker: a batch that uploaded
        but never committed."""
        self._insert_events(*rows)
        with self.Session() as db:
            orm = db.query(HubProductEventV2).filter(HubProductEventV2.event_id.in_([r["event_id"] for r in rows])).order_by(HubProductEventV2.id).all()
            records = [archive.archive_row(r, "events", exclude=archive.VIEW_AS_COLUMNS) for r in orm]
        payload = archive.make_archive(environment="sandbox", events=records, exclude_columns=archive.VIEW_AS_COLUMNS,
                                       metadata={"table": "events", "selection": "age_expired_not_deleted_resource"})
        key = f"{self.PREFIX}/sandbox/events/v3/2025/leftover-{suffix}.zip"
        metadata = {"sha256": "x", "format-version": "3"}
        if marker is ...:
            metadata[archive.REGISTRY_ID_METADATA] = self._identity()
        elif marker is not None:
            metadata[archive.REGISTRY_ID_METADATA] = marker
        store.put(key, payload, metadata)
        return key

    def test_reconciliation_waits_for_a_batch_that_uploaded_but_has_not_committed_its_registry_row(self) -> None:
        """The object exists, the registry row does not yet: the batch holds the
        job lock between its upload and its commit. The reconciler cannot take
        the lock, deletes nothing, and once the batch commits the object is a
        registered archive it will never touch."""
        rows = [self._event(timedelta(days=400)), self._event(timedelta(days=401))]
        self._insert_events(*rows)
        store, inside_get, release = self._stalled_store("get")  # blocked in the readback: uploaded, not committed
        result: list[tuple[int, list[str]]] = []
        thread = threading.Thread(target=lambda: result.append(self._run(store=store)))
        thread.start()
        self.assertTrue(inside_get.wait(timeout=30))
        try:
            self.assertEqual(len(store.objects), 1)
            with self.Session() as db:
                self.assertEqual(archive.unregistered_keys(db, store.objects), list(store.objects))
            lines: list[str] = []
            outcome = self._reconcile(store, lines=lines)
            self.assertEqual((outcome.candidates, outcome.locked, outcome.deleted), (1, True, 0))
            self.assertTrue(any("an archive run holds the job lock" in line for line in lines), lines)
            self.assertEqual(len(store.objects), 1)
        finally:
            release.set()
            thread.join(timeout=60)
        self.assertEqual(result[0][0], 0, result[0][1])
        outcome = self._reconcile(store)
        self.assertEqual((outcome.registered, outcome.candidates, outcome.deleted, len(store.objects)), (1, 0, 0, 1))

    def test_reconciliation_rechecks_the_registry_under_the_lock_before_deleting(self) -> None:
        """Between the listing and the deletion another run registers the
        object (a retry that recognised an earlier upload). The re-check
        under the job lock sees the registry row and the delete is skipped."""
        rows = [self._event(timedelta(days=400)), self._event(timedelta(days=401))]
        self._insert_events(*rows)

        class CorruptOnce(archive.MemoryArchiveStore):
            corrupt = True

            def get(self, key):
                if self.corrupt:
                    self.corrupt = False
                    return b"corrupt"
                return super().get(key)

        store = CorruptOnce(clock=lambda: datetime.now(timezone.utc) - timedelta(hours=48))
        self.assertEqual(self._run(store=store)[0], 1)  # uploaded, readback failed: an unregistered object
        (key,) = list(store.objects)
        real_registered_keys = archive.registered_keys
        calls: list[int] = []

        def registers_after_the_first_check(db, keys):
            known = real_registered_keys(db, keys)
            calls.append(len(known))
            if len(calls) == 1:
                # Between the reconciler's pre-lock check and its lock, a retry
                # of the job recognises the object and registers it.
                self.assertEqual(self._run(store=store)[0], 0)
            return known

        lines: list[str] = []
        with patch.object(archive, "registered_keys", registers_after_the_first_check):
            outcome = self._reconcile(store, lines=lines)
        self.assertEqual((outcome.candidates, outcome.skipped_registered_late, outcome.deleted), (1, 1, 0))
        self.assertIn(key, store.objects)
        with self.Session() as db:
            self.assertEqual(self._registry(db), {key: 2})
        self.assertEqual(self._count(), 0)

    def test_a_late_upload_is_reconciled_only_after_the_grace_period_and_never_registered(self) -> None:
        """A PUT that lands after the job timed out and released its locks: its
        rows are still live (the batch never committed), so it is a redundant
        leftover — never registered, deleted only after the grace period."""
        rows = self._component_rows("component-live", "component-live")
        self._insert_events(*rows)
        store, inside, release = self._stalled_store("put")
        with patch.object(archive, "STORAGE_CALL_DEADLINE_S", 1.0):
            thread = threading.Thread(target=lambda: self._run(store=store))
            thread.start()
            self.assertTrue(inside.wait(timeout=30))
            thread.join(timeout=30)
        release.set()
        self._wait_for_objects(store, 1)
        (leftover,) = list(store.objects)
        self.assertEqual(store.metadata[leftover][archive.REGISTRY_ID_METADATA], self._identity())
        recent = self._reconcile(store, grace_hours=archive.RECONCILE_GRACE_HOURS)
        self.assertEqual((recent.too_recent, recent.candidates, recent.deleted), (1, 0, 0))
        report = self._reconcile(store, delete=False)
        self.assertEqual((report.leftovers, report.deleted, len(store.objects)), ([leftover], 0, 1))
        old = self._reconcile(store, now=datetime.now(timezone.utc) + timedelta(hours=25), grace_hours=archive.RECONCILE_GRACE_HOURS)
        self.assertEqual((old.deleted_keys, store.objects), ([leftover], {}))
        with self.Session() as db:
            self.assertEqual(self._registry(db), {})
        self.assertEqual(self._count(), 2)  # the rows were never deleted; the next run archives them afresh
        self.assertEqual(self._run(store=store)[0], 0)
        self.assertEqual((self._count(), len(store.objects)), (0, 1))
        with self.Session() as db:
            self.assertEqual(self._registry(db), {next(iter(store.objects)): 2})

    def test_an_object_replaced_between_listing_and_head_is_skipped_but_after_head_it_is_deleted(self) -> None:
        """What the unconditional delete means, shown rather than claimed: a
        replacement before the HEAD is caught by the identity check; a
        replacement after the HEAD is not — the object at the key is deleted.
        Only a writer that bypasses the job's conditional PUT and the job lock
        (a third-party tool with the bucket token) can produce that case."""
        store = self._old_store()
        key = self._make_leftover(store, [self._event(timedelta(days=400))], suffix="replaced")

        class ReplacedBeforeHead(archive.MemoryArchiveStore):
            def stat(self, key):
                archive.MemoryArchiveStore.put(self, key, b"replaced before head")
                return super().stat(key)

        before = ReplacedBeforeHead(clock=store.clock)
        before.objects, before.metadata, before.modified = store.objects, store.metadata, store.modified
        outcome = self._reconcile(before)
        self.assertEqual((outcome.skipped_changed, outcome.deleted), (1, 0))
        self.assertEqual(store.objects[key], b"replaced before head")
        # Restore a genuine leftover, then replace it after the HEAD (inside the delete).
        store.objects.pop(key)
        key = self._make_leftover(store, [self._event(timedelta(days=401))], suffix="replaced")

        class ReplacedAfterHead(archive.MemoryArchiveStore):
            def delete(self, key):
                archive.MemoryArchiveStore.put(self, key, b"replaced after head")
                super().delete(key)  # unconditional, like R2: the replacement is what gets deleted

        after = ReplacedAfterHead(clock=store.clock)
        after.objects, after.metadata, after.modified = store.objects, store.metadata, store.modified
        outcome = self._reconcile(after)
        self.assertEqual((outcome.deleted_keys, key in store.objects), ([key], False))

    def test_concurrent_reconcilers_serialise_and_an_interrupted_run_is_completed_by_the_next(self) -> None:
        store = self._old_store()
        keys = [self._make_leftover(store, [self._event(timedelta(days=400 + i))], suffix=f"c{i}") for i in range(4)]
        outcomes: list[archive.ReconcileOutcome] = []
        errors: list[BaseException] = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker() -> None:
            barrier.wait()
            try:
                outcome = self._reconcile(store, lock_timeout_ms=5000)
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion
                with lock:
                    errors.append(exc)
                return
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual((sum(o.deleted for o in outcomes), sum(o.failed for o in outcomes), store.objects), (4, 0, {}))
        # Interruption: a failure on one key does not stop the others; the next run finishes the job.
        keys = [self._make_leftover(store, [self._event(timedelta(days=500 + i))], suffix=f"i{i}") for i in range(3)]

        class FailsOnce(archive.MemoryArchiveStore):
            failed = False

            def delete(self, key):
                if key == keys[1] and not self.failed:
                    self.failed = True
                    raise ConnectionResetError("interrupted")
                super().delete(key)

        flaky = FailsOnce(clock=store.clock)
        flaky.objects, flaky.metadata, flaky.modified = store.objects, store.metadata, store.modified
        first = self._reconcile(flaky)
        self.assertEqual((first.deleted, first.failed, sorted(store.objects)), (2, 1, [keys[1]]))
        second = self._reconcile(flaky)
        self.assertEqual((second.deleted, second.failed, store.objects), (1, 0, {}))

    def test_reconciliation_is_isolated_per_environment_and_prefix_and_idempotent(self) -> None:
        store = self._old_store()
        self._insert_events(self._event(timedelta(days=400)))
        self.assertEqual(self._run(store=store)[0], 0)
        registered = set(store.objects)
        foreign = {f"{self.PREFIX}/production/events/v3/2025/x.zip", f"{self.PREFIX}/local/events/v3/2025/x.zip",
                   "another-prefix/sandbox/events/v3/2025/x.zip", f"{self.PREFIX}/sandbox-other/events/v3/2025/x.zip"}
        for key in foreign:
            store.put(key, b"foreign", {archive.REGISTRY_ID_METADATA: self._identity()})
        ours = self._make_leftover(store, [self._event(timedelta(days=410))], suffix="ours")
        outcome = self._reconcile(store)
        self.assertEqual((outcome.listed, outcome.registered, outcome.deleted_keys), (len(registered) + 1, len(registered), [ours]))
        self.assertEqual(set(store.objects), registered | foreign)
        for _ in range(2):
            again = self._reconcile(store)
            self.assertEqual((again.candidates, again.deleted, again.failed), (0, 0, 0))
        self.assertEqual(set(store.objects), registered | foreign)

    def test_reconciliation_retains_foreign_and_not_redundant_objects_and_deletes_reviewed_keys_only(self) -> None:
        store = self._old_store()
        other = self._make_leftover(store, [self._event(timedelta(days=400))], suffix="other-registry", marker="11111111-1111-1111-1111-111111111111")
        unmarked = self._make_leftover(store, [self._event(timedelta(days=401))], suffix="unmarked", marker=None)
        gone = self._make_leftover(store, [self._event(timedelta(days=402), target_id="tab-doomed", page_id="tab-doomed")], suffix="cleanup-deleted")
        with self.Session() as db:  # the cleanup removed the doomed row (its page no longer exists)
            self.assertEqual(service.purge_deleted_page_records(db, table="events", environment="sandbox"), 1)
        lines: list[str] = []
        outcome = self._reconcile(store, lines=lines)
        self.assertEqual((outcome.deleted, outcome.retained_foreign, outcome.retained_not_redundant), (0, 2, 1))
        self.assertEqual({key for key, _ in outcome.retained}, {other, unmarked, gone})
        self.assertEqual(len(store.objects), 3)
        reviewed = self._reconcile(store, reviewed_keys=[gone, other])
        self.assertEqual((reviewed.deleted_keys, reviewed.retained_foreign), ([gone], 1))
        self.assertEqual(set(store.objects), {other, unmarked})

    def test_a_restored_database_makes_registered_objects_redundant_not_lost(self) -> None:
        """After a database restore the registry rows and the live rows come
        back together: an object archived after the backup point has no
        registry row but its records are live again, so it is redundant and
        may be deleted (the next run re-archives). A registry row removed
        while the rows stay absent is the dangerous half, and it is retained."""
        store = self._old_store()
        rows = [self._event(timedelta(days=400)), self._event(timedelta(days=401))]
        self._insert_events(*rows)
        self.assertEqual(self._run(store=store)[0], 0)
        (key,) = list(store.objects)
        with self.engine.begin() as connection:  # registry row lost, rows NOT back: retained
            connection.execute(text("DELETE FROM hub_product_analytics_archives"))
        outcome = self._reconcile(store)
        self.assertEqual((outcome.deleted, outcome.retained_not_redundant), (0, 1))
        self.assertIn("2 of 2 record(s) have no identical live row", outcome.retained[0][1])
        # Rows re-inserted with NEW ids (as a script restore would do) are not identical: retained.
        self._insert_events(*rows)
        outcome = self._reconcile(store)
        self.assertEqual((outcome.deleted, outcome.retained_not_redundant), (0, 1))
        # A database restore brings the rows back with their ORIGINAL ids and contents: redundant.
        _, archived = archive.archive_records(store.objects[key])
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM hub_product_events WHERE event_id IN :ids").bindparams(ids=tuple(r["event_id"] for r in rows)))
        self._insert_events(*[{**row, "id": next(a["id"] for a in archived["events"] if a["event_id"] == row["event_id"])} for row in rows])
        outcome = self._reconcile(store)
        self.assertEqual((outcome.deleted_keys, store.objects), ([key], {}))
        self.assertEqual(self._run(store=store)[0], 0)
        self.assertEqual((self._count(), len(store.objects)), (0, 1))

    # ── redundancy is proven by content, under row locks ─────────────────

    def _row(self, event_id: str):
        with self.Session() as db:
            return db.query(HubProductEventV2).filter(HubProductEventV2.event_id == event_id).one()

    def test_a_live_row_with_the_same_id_but_different_contents_is_not_redundant(self) -> None:
        store = self._old_store()
        row = self._event(timedelta(days=400))
        key = self._make_leftover(store, [row], suffix="changed")
        cases = {
            "properties changed": "UPDATE hub_product_events SET properties = '{\"tab_kind\": \"changed\"}'::json WHERE event_id = :e",
            "target changed": "UPDATE hub_product_events SET target_id = 'tab-other' WHERE event_id = :e",
            "timestamp changed": "UPDATE hub_product_events SET occurred_at = occurred_at + interval '1 second' WHERE event_id = :e",
            "environment changed": "UPDATE hub_product_events SET environment = 'production' WHERE event_id = :e",
        }
        original = {c.name: getattr(self._row(row["event_id"]), c.name) for c in HubProductEventV2.__table__.columns}
        for name, statement in cases.items():
            with self.subTest(name):
                with self.engine.begin() as connection:
                    connection.execute(text(statement), {"e": row["event_id"]})
                outcome = self._reconcile(store)
                self.assertEqual((outcome.deleted, outcome.retained_not_redundant), (0, 1), name)
                self.assertIn("1 of 1 record(s) have no identical live row", outcome.retained[0][1])
                self.assertIn(key, store.objects)
                with self.engine.begin() as connection:  # put the row back exactly
                    connection.execute(text("DELETE FROM hub_product_events WHERE event_id = :e"), {"e": row["event_id"]})
                    connection.execute(HubProductEventV2.__table__.insert(), original)
        # Identical again (same id, same content): redundant.
        outcome = self._reconcile(store)
        self.assertEqual((outcome.deleted_keys, store.objects), ([key], {}))

    def test_a_live_row_updated_or_deleted_while_the_reconciler_decides_waits_for_the_decision(self) -> None:
        """The live rows are locked FOR SHARE from the comparison until the
        object is deleted: a concurrent UPDATE or DELETE of them waits (here:
        hits a lock timeout) instead of changing the data under the decision.
        A change that lands BEFORE the comparison makes the object non-redundant."""
        store = self._old_store()
        row = self._event(timedelta(days=400))
        key = self._make_leftover(store, [row], suffix="racing")
        inside, release = threading.Event(), threading.Event()

        class PausesBeforeDelete(archive.MemoryArchiveStore):
            def delete(self, key):
                inside.set()
                release.wait(timeout=30)
                super().delete(key)

        pausing = PausesBeforeDelete(clock=store.clock)
        pausing.objects, pausing.metadata, pausing.modified = store.objects, store.metadata, store.modified
        outcomes: list[archive.ReconcileOutcome] = []
        thread = threading.Thread(target=lambda: outcomes.append(self._reconcile(pausing)))
        thread.start()
        self.assertTrue(inside.wait(timeout=30))
        try:
            self.assertTrue(self._blocked("UPDATE hub_product_events SET properties = '{}'::json WHERE event_id = '%s'" % row["event_id"]))
            self.assertTrue(self._blocked("DELETE FROM hub_product_events WHERE event_id = '%s'" % row["event_id"]))
        finally:
            release.set()
            thread.join(timeout=60)
        self.assertEqual((outcomes[0].deleted_keys, store.objects), ([key], {}))
        self.assertEqual(self._count(), 1)  # the data is still live and unchanged
        # A change that commits before the comparison: retained.
        key = self._make_leftover(store, [self._event(timedelta(days=401))], suffix="changed-first")
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE hub_product_events SET properties = '{}'::json WHERE id = (SELECT max(id) FROM hub_product_events)"))
        outcome = self._reconcile(store)
        self.assertEqual((outcome.deleted, outcome.retained_not_redundant, key in store.objects), (0, 1, True))

    def test_redundancy_ignores_omitted_view_as_columns_and_handles_duplicates_and_engagements(self) -> None:
        store = self._old_store()
        row = self._event(timedelta(days=400), view_as_role_key="role", view_as_scope_key="scope", view_as_context={"user_id": 4})
        key = self._make_leftover(store, [row], suffix="viewas")  # archived with the View As columns omitted
        self.assertEqual(archive.validate_archive(store.objects[key])["omitted_columns"], sorted(archive.VIEW_AS_COLUMNS))
        duplicate = f"{self.PREFIX}/sandbox/events/v3/2025/leftover-duplicate.zip"
        store.put(duplicate, store.objects[key], store.metadata[key])  # a second leftover with the same records
        outcome = self._reconcile(store)
        self.assertEqual((sorted(outcome.deleted_keys), store.objects), (sorted([key, duplicate]), {}))
        self.assertEqual(self._count(), 1)
        # An archive carrying the View As columns (explicit include) must match them too.
        with self.Session() as db:
            orm = db.query(HubProductEventV2).one()
            full = archive.make_archive(environment="sandbox", events=[archive.archive_row(orm, "events")],
                                        metadata={"table": "events", "selection": "age_expired_not_deleted_resource"})
        store.put(f"{self.PREFIX}/sandbox/events/v3/2025/leftover-full.zip", full, {archive.REGISTRY_ID_METADATA: self._identity()})
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE hub_product_events SET view_as_role_key = 'other'"))
        outcome = self._reconcile(store)
        self.assertEqual((outcome.deleted, outcome.retained_not_redundant), (0, 1))
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE hub_product_events SET view_as_role_key = 'role'"))
        self.assertEqual(self._reconcile(store).deleted, 1)

    def test_registry_identity_is_a_database_singleton_initialised_atomically(self) -> None:
        from sqlalchemy.exc import IntegrityError

        with self.engine.begin() as connection:
            connection.execute(text("TRUNCATE hub_product_analytics_archive_identity"))
        results: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def initialiser() -> None:
            barrier.wait()
            try:
                with self.Session() as db:
                    identity = archive.registry_identity(db)
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                with lock:
                    errors.append(exc)
                return
            with lock:
                results.append(identity)

        threads = [threading.Thread(target=initialiser) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(len(set(results)), 1, results)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT count(*) FROM hub_product_analytics_archive_identity")).scalar_one(), 1)
            with self.assertRaises(IntegrityError):  # the database itself refuses a second row
                connection.execute(text("INSERT INTO hub_product_analytics_archive_identity (singleton, id, created_at) VALUES (2, 'x', now())"))
            connection.rollback()
        self.assertEqual(self._identity(), results[0])

    def test_registry_identity_binds_to_storage_on_first_use_and_fails_closed_elsewhere(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(text("TRUNCATE hub_product_analytics_archive_identity"))
        with self.Session() as db:
            identity = archive.registry_identity(db, storage=("bucket-a", "hub-product-analytics"), bind_storage=False)  # a report: no binding
            self.assertEqual(db.execute(text("SELECT bucket FROM hub_product_analytics_archive_identity")).scalar_one(), None)
            self.assertEqual(archive.registry_identity(db, storage=("bucket-a", "hub-product-analytics")), identity)  # binds
            self.assertEqual(db.execute(text("SELECT bucket, prefix FROM hub_product_analytics_archive_identity")).one(), ("bucket-a", "hub-product-analytics"))
            self.assertEqual(archive.registry_identity(db, storage=("bucket-a", "hub-product-analytics")), identity)
            self.assertEqual(archive.registry_identity(db), identity)  # no storage known (tests, local): no check
            for other in (("bucket-b", "hub-product-analytics"), ("bucket-a", "other-prefix")):
                with self.subTest(other), self.assertRaises(archive.ArchiveConfigError) as caught:
                    archive.registry_identity(db, storage=other)
                self.assertIn("bound to a different bucket/prefix", str(caught.exception))
                self.assertNotIn("bucket-b", str(caught.exception))
                self.assertEqual(archive.registry_identity(db, storage=other, bind_storage=False), identity)  # a dry run neither binds nor refuses
            self.assertEqual(archive.registry_storage_binding(db), ("bucket-a", "hub-product-analytics"))
        # The job and the reconciler refuse too; nothing is uploaded or deleted.
        self._insert_events(self._event(timedelta(days=400)))
        store = self._old_store()
        with self.Session() as db, self.assertRaises(archive.ArchiveConfigError):
            archive.archive_expired(db, store, table="events", environment="sandbox", cutoff=self.CUTOFF, prefix="p", storage=("bucket-b", "p"))
        self.assertEqual((store.calls, self._count()), ([], 1))
        leftover = self._make_leftover(store, [self._event(timedelta(days=401))], suffix="bound")
        with self.Session() as db, self.assertRaises(archive.ArchiveConfigError):
            archive.reconcile_leftovers(db, store, environment="sandbox", prefix=self.PREFIX, delete=True, grace_hours=0, storage=("bucket-b", self.PREFIX))
        self.assertIn(leftover, store.objects)
        lines: list[str] = []
        with self.Session() as db:  # a report-only run with other storage still reports (it binds nothing) and warns
            report = archive.reconcile_leftovers(db, store, environment="sandbox", prefix=self.PREFIX, grace_hours=0,
                                                 storage=("bucket-b", self.PREFIX), out=lines.append)
            self.assertEqual(report.candidates, 1)
        self.assertTrue(any(line.startswith("WARNING: the registry identity is bound to a different bucket/prefix") for line in lines), lines)
        with self.Session() as db:  # the deliberate operator re-bind keeps the id
            self.assertEqual(archive.rebind_registry_identity_storage(db, bucket="bucket-b", prefix=self.PREFIX), identity)
            self.assertEqual(archive.registry_identity(db, storage=("bucket-b", self.PREFIX)), identity)

    def test_dry_run_and_report_only_reconciliation_run_inside_read_only_transactions(self) -> None:
        """Proven by the database, not by inspection: an engine whose every
        transaction is READ ONLY runs the dry run, `--check-keys` and the
        report-only reconciliation to completion, and an empty identity table
        stays empty. A deleting reconciliation and the live job refuse or
        fail on such a connection; nothing is written."""
        from scripts import reconcile_product_analytics_archives as reconcile_job

        with self.engine.begin() as connection:
            connection.execute(text("TRUNCATE hub_product_analytics_archive_identity"))
        self._insert_events(self._event(timedelta(days=400)), self._event(timedelta(days=5), target_id="tab-deleted", page_id="tab-deleted"))
        store = self._old_store()
        store.put(f"{self.PREFIX}/sandbox/events/v3/2025/leftover.zip", b"x", {archive.REGISTRY_ID_METADATA: "anything"})
        read_only = create_engine(DATABASE_URL, connect_args={"options": "-c default_transaction_read_only=on"}, pool_pre_ping=True)
        self.addCleanup(read_only.dispose)
        import tempfile
        from pathlib import Path

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        listing = Path(directory.name) / "keys.txt"
        listing.write_text(f"{self.PREFIX}/sandbox/events/v3/2025/leftover.zip\n", encoding="utf-8")
        lines: list[str] = []
        code = job.run(["--dry-run"], engine=read_only, store=store, raw_retention="365", environment="sandbox", now=NOW, out=lines.append,
                       storage=("bucket-a", self.PREFIX))
        self.assertEqual(code, 0, lines)
        self.assertIn("dry run: registry identity not initialised; the live job (or python -m scripts.create_product_analytics_archive_tables) initialises it. Nothing was written.", lines)
        self.assertTrue(any(line.startswith("dry run: 1 events row(s) reference deleted resources") for line in lines), lines)
        lines.clear()
        code = job.run(["--check-keys", str(listing)], engine=read_only, store=store, raw_retention="365", environment="sandbox", now=NOW, out=lines.append)
        self.assertEqual(code, 0, lines)
        self.assertIn("checked 1 key(s): 0 registered archive(s), 1 unregistered object(s) (not archives; reconcile them).", lines)
        lines.clear()
        code = reconcile_job.run([], engine=read_only, store=store, environment="sandbox", now=None, out=lines.append, storage=("bucket-a", self.PREFIX))
        self.assertEqual(code, 0, lines)
        self.assertIn("registry identity not initialised: ownership of objects cannot be established; a deleting run would refuse.", lines)
        self.assertTrue(any(line.startswith("leftover (unregistered") for line in lines), lines)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT count(*) FROM hub_product_analytics_archive_identity")).scalar_one(), 0)
        self.assertEqual(self._count(), 2)
        self.assertEqual(store.calls.count(("delete", f"{self.PREFIX}/sandbox/events/v3/2025/leftover.zip")), 0)
        # Deleting runs on the same read-only connection: refused (identity absent) or failed, never a write.
        lines.clear()
        self.assertEqual(reconcile_job.run(["--delete"], engine=read_only, store=store, environment="sandbox", out=lines.append, storage=("bucket-a", self.PREFIX)), 2)
        self.assertIn("not initialised", lines[0])
        lines.clear()
        code = job.run([], engine=read_only, store=store, raw_retention="365", environment="sandbox", now=NOW, out=lines.append, storage=("bucket-a", self.PREFIX))
        self.assertEqual(code, 1, lines)
        self.assertTrue(lines[-1].startswith("archive failed before any deletion: InternalError") and lines[-1].endswith("Nothing was deleted or uploaded."), lines)
        self.assertEqual(self._count(), 2)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT count(*) FROM hub_product_analytics_archive_identity")).scalar_one(), 0)
        # The live job on a normal connection initialises and binds the identity before its cleanup.
        code, lines = self._run(store=store)
        self.assertEqual(code, 0, lines)
        with self.Session() as db:
            identity, bound = archive.lookup_registry_identity(db)
        self.assertIsNotNone(identity)
        self.assertIsNone(bound)  # no storage was given to this test run; the real script binds bucket/prefix

    def test_a_mismatched_storage_binding_refuses_the_job_before_its_cleanup_with_stale_rows_intact(self) -> None:
        stale = [self._event(timedelta(days=5 + i), target_id="tab-deleted", page_id="tab-deleted") for i in range(3)]
        self._insert_events(*stale, self._event(timedelta(days=400)))
        with self.engine.begin() as connection:
            connection.execute(text("TRUNCATE hub_product_analytics_archive_identity"))
        with self.Session() as db:
            archive.registry_identity(db, storage=("bucket-a", self.PREFIX))
        lines: list[str] = []
        code = job.run([], engine=self.engine, store=self.store, raw_retention="365", environment="sandbox", now=NOW, out=lines.append,
                       storage=("bucket-b", self.PREFIX))
        self.assertEqual(code, 2, lines)
        self.assertIn("bound to a different bucket/prefix", lines[0])
        self.assertTrue(lines[0].endswith("Nothing was deleted or uploaded."), lines)
        self.assertEqual((self._count(), self.store.calls), (4, []))
        with self.Session() as db:
            self.assertEqual(service.count_deleted_page_records(db, table="events", environment="sandbox"), 3)
        # Same storage: the job runs, cleanup first, then the archive.
        code, lines = job.run([], engine=self.engine, store=self.store, raw_retention="365", environment="sandbox", now=NOW, out=lines.append,
                              storage=("bucket-a", self.PREFIX)), lines
        self.assertEqual(code, 0, lines)
        self.assertEqual(self._count(), 0)

    def test_a_cleanup_batch_failing_after_earlier_batches_committed_is_reported_exactly(self) -> None:
        """A real database failure in the cleanup: another transaction holds a
        stale row FOR UPDATE and the job's connection has a lock timeout. The
        batches committed before the failing one stand, the failing one is
        rolled back, the report names the committed counts per table, the
        exit is 1, and no archive batch starts."""
        stale_events = [self._event(timedelta(days=5 + i), target_id="tab-deleted", page_id="tab-deleted") for i in range(150)]
        self._insert_events(*stale_events, self._event(timedelta(days=400)), self._event(timedelta(days=401)))
        with self.engine.begin() as connection:
            connection.execute(HubProductEngagementV2.__table__.insert(), [
                dict(engagement_id=str(uuid.uuid4()), hub_user_id=1, actor_email="a@renphil.org", environment="sandbox", page_route="/:nav_tab",
                     nav_tab_id=None, tab_id="tab-deleted", active_seconds=5, started_at=NOW - timedelta(days=5), ended_at=NOW - timedelta(days=5),
                     received_at=NOW - timedelta(days=5)) for _ in range(3)
            ])
        timed_out = create_engine(DATABASE_URL, connect_args={"options": "-c lock_timeout=800ms"}, pool_pre_ping=True)
        self.addCleanup(timed_out.dispose)
        with self.engine.connect() as connection:
            lowest, highest = connection.execute(text(
                "SELECT min(id), max(id) FROM hub_product_events WHERE target_id = 'tab-deleted'"
            )).one()
        # Failure in the SECOND cleanup batch (batches of 100, oldest ids first): the first batch has committed.
        holder = self.engine.connect()
        holder.begin()
        holder.execute(text("SELECT id FROM hub_product_events WHERE id = :i FOR UPDATE"), {"i": highest})
        try:
            lines: list[str] = []
            code = job.run(["--batch-size", "100"], engine=timed_out, store=self.store, raw_retention="365", environment="sandbox", now=NOW, out=lines.append)
        finally:
            holder.rollback()
            holder.close()
        self.assertEqual(code, 1, lines)
        failure = lines[-1]
        self.assertTrue(failure.startswith("cleanup failed for events: OperationalError"), failure)
        self.assertIn("The failing batch was rolled back; no archive batch was started.", failure)
        self.assertIn("the cleanup had deleted 100 events, 0 engagements row(s) referencing deleted resources (not archived); archived and deleted in committed batches: none.", failure)
        self.assertNotIn("Nothing was deleted", failure)
        self.assertNotIn("lock timeout", failure.lower())  # the driver's message is not echoed, only the type
        self.assertEqual(self.store.calls, [])
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT count(*) FROM hub_product_events WHERE target_id = 'tab-deleted'")).scalar_one(), 50)
            self.assertEqual(connection.execute(text("SELECT count(*) FROM hub_product_events WHERE target_id = 'tab-live'")).scalar_one(), 2)
            self.assertEqual(connection.execute(text("SELECT count(*) FROM hub_product_engagements")).scalar_one(), 3)
        # Failure BEFORE the first commit: a row of the first batch is held.
        holder = self.engine.connect()
        holder.begin()
        holder.execute(text("SELECT id FROM hub_product_events WHERE id = (SELECT min(id) FROM hub_product_events WHERE target_id = 'tab-deleted') FOR UPDATE"))
        try:
            lines = []
            code = job.run(["--batch-size", "100"], engine=timed_out, store=self.store, raw_retention="365", environment="sandbox", now=NOW, out=lines.append)
        finally:
            holder.rollback()
            holder.close()
        self.assertEqual(code, 1, lines)
        self.assertTrue(lines[-1].startswith("cleanup failed for events: OperationalError"), lines)
        self.assertIn("the cleanup had deleted 0 events, 0 engagements row(s)", lines[-1])
        self.assertEqual(self.store.calls, [])
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT count(*) FROM hub_product_events WHERE target_id = 'tab-deleted'")).scalar_one(), 50)
        # Without the conflict the job finishes: the remaining 50 stale rows and 3 intervals go, then the archive runs.
        code, lines = self._run("--batch-size", "100")
        self.assertEqual(code, 0, lines)
        self.assertIn("deleted 50 events row(s) referencing deleted resources (not archived).", lines)
        self.assertIn("deleted 3 engagements row(s) referencing deleted resources (not archived).", lines)
        self.assertEqual(self._count(), 0)

    def test_restore_skips_deleted_resource_records_using_the_real_predicate(self) -> None:
        rows = [self._event(timedelta(days=400)), *self._component_rows("component-live")]
        self._insert_events(*rows)
        self.assertEqual(self._run()[0], 0)
        (key,) = list(self.store.objects)
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM components WHERE link = 'component-live'"))
        self._add_component("component-after")
        with self.Session() as db:
            result = archive.restore_registered_archive(db, self.store, key=key, environment="sandbox")
            db.commit()
        self.assertEqual((result["inserted"]["events"], result["deleted_resource_skipped"]["events"]), (1, 1))
        self.assertEqual(self._count(), 1)
        with self.Session() as db:
            recovered = archive.restore_registered_archive(db, self.store, key=key, environment="sandbox", include_deleted_resources=True)
            db.commit()
        self.assertEqual((recovered["inserted"]["events"], recovered["already_present"]["events"]), (1, 1))
        self.assertEqual(self._count(), 2)

    def test_a_closed_http_connection_fails_the_batch_closed(self) -> None:
        class Reset(archive.MemoryArchiveStore):
            def put(self, key, data, metadata=None, *, if_none_match=False):
                raise ConnectionResetError("connection closed by peer")

        self._insert_events(self._event(timedelta(days=400)))
        code, lines = self._run(store=Reset())
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith("archive failed for events: ConnectionResetError") for line in lines), lines)
        self.assertEqual(self._count(), 1)

    def test_a_database_session_closed_during_the_upload_deletes_nothing_and_frees_the_locks(self) -> None:
        rows = self._component_rows("component-live", "component-live")
        store, release, thread, result = self._start_blocked_job(*rows)
        try:
            with self.engine.begin() as connection:
                terminated = connection.execute(text(
                    "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid() AND state = 'idle in transaction'"
                )).scalar_one()
            self.assertEqual(terminated, 1)
            self.assertFalse(self._blocked("UPDATE components SET title = 'free' WHERE link = 'component-live'"))
            self.assertFalse(self._blocked("DELETE FROM components WHERE link = 'component-live'"))
        finally:
            release.set()
            thread.join(timeout=60)
        code, lines = result[0]
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith("archive failed for events: OperationalError") for line in lines), lines)
        self.assertEqual(self._count(), 2)
        self.assertEqual(len(store.objects), 1)  # uploaded before the session died: an orphan the next run recognises

    def test_statements_inside_the_batch_run_under_a_statement_timeout(self) -> None:
        self._insert_events(self._event(timedelta(days=400)))

        def slow_count(db, *, table, ids):
            db.execute(text("SELECT pg_sleep(1)"))
            return 0

        with patch.object(archive, "STATEMENT_TIMEOUT_MS", 200), patch.object(archive, "_deleted_resource_count", slow_count):
            code, lines = self._run()
        self.assertEqual(code, 1, lines)
        self.assertTrue(any(line.startswith("archive failed for events: OperationalError") for line in lines), lines)
        self.assertEqual((self._count(), self.store.calls), (1, []))

    def test_deleting_a_resource_the_batch_depends_on_waits_for_the_batch(self) -> None:
        """While a batch uploads, its nav tabs/tabs/sub-gridstacks/components
        are key-share locked: a `DELETE` of one of them waits (here: hits a
        `lock_timeout`, or blocks until the batch commits), while edits,
        moves and deletions of other resources proceed. The archive is
        written while every resource it references exists; the deletion
        completes afterwards and the next cleanup sees nothing to do."""
        from sqlalchemy.exc import OperationalError

        rows = [self._event(timedelta(days=400)), self._event(timedelta(days=401)), *self._component_rows("component-live")]
        self._insert_events(*rows)
        store, inside_put, release = self._blocking_store()
        first: list[tuple[int, list[str]]] = []
        thread = threading.Thread(target=lambda: first.append(self._run(store=store)))
        thread.start()
        self.assertTrue(inside_put.wait(timeout=30))
        finished_deletion: list[float] = []
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SET LOCAL lock_timeout ='1500ms'"))
                for statement in ("DELETE FROM components WHERE link = 'component-live'",
                                  "DELETE FROM tabs WHERE document_id = 'tab-live'",
                                  "DELETE FROM gridstacks WHERE document_id = 'tab-live'"):
                    with self.subTest(statement), self.assertRaises(OperationalError) as caught:
                        connection.execute(text(statement))
                    self.assertIn("lock", str(caught.exception).lower())
                    connection.rollback()
                    connection.execute(text("SET LOCAL lock_timeout ='1500ms'"))
            # `tabs.document_id` has no unique index, so the tab row is held
            # FOR SHARE: its rename waits too (the cost). `components.link`
            # is unique in this schema, so the component is held FOR KEY
            # SHARE: editing it proceeds. Unreferenced resources are free.
            self.assertTrue(self._blocked("UPDATE tabs SET title = 'renamed' WHERE document_id = 'tab-live'"))
            with self.engine.begin() as connection:
                connection.execute(text("SET LOCAL lock_timeout = '1500ms'"))
                self.assertEqual(connection.execute(text("UPDATE components SET x = 5 WHERE link = 'component-live'")).rowcount, 1)
                connection.execute(text("INSERT INTO tabs (document_id, title) VALUES ('tab-unrelated', 'other')"))
                self.assertEqual(connection.execute(text("DELETE FROM tabs WHERE document_id = 'tab-unrelated'")).rowcount, 1)

            def waiting_deleter() -> None:
                with self.engine.begin() as connection:
                    self._hub_delete_page(connection, "tab-live", "component-live")
                finished_deletion.append(1)

            waiter = threading.Thread(target=waiting_deleter)
            waiter.start()
            waiter.join(timeout=1.5)
            self.assertTrue(waiter.is_alive(), "a deletion of a referenced page must wait for the batch")
            self.assertEqual(finished_deletion, [])
        finally:
            release.set()
            thread.join(timeout=60)
        waiter.join(timeout=30)
        code, lines = first[0]
        self.assertEqual(code, 0, lines)
        self.assertFalse(any(line.startswith("note:") for line in lines), lines)
        self.assertTrue(any("0 batch(es) re-selected" in line for line in lines), lines)
        self.assertEqual(finished_deletion, [1])
        self.assertEqual(self._count(), 0)
        self.assertEqual({r["event_id"] for r in self._archived_records(store)}, {r["event_id"] for r in rows})
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT count(*) FROM tabs WHERE document_id = 'tab-live'")).scalar_one(), 0)
        # The resource was deleted after its rows were archived: the archive
        # is not reopened, and nothing is left for the cleanup.
        self._add_page("tab-after")
        self._add_component("component-after", page="tab-after")
        self._insert_events(self._event(timedelta(days=1), target_id="tab-after", page_id="tab-after"))  # the environment stays non-empty
        code, lines = self._run()
        self.assertEqual(code, 0, lines)
        self.assertIn("deleted 0 events row(s) referencing deleted resources (not archived).", lines)
        self.assertEqual(len(store.objects), 1)

    def test_a_resource_deleted_between_selection_and_lock_abandons_the_batch_before_any_upload(self) -> None:
        self._add_page("tab-doomed")
        doomed = [self._event(timedelta(days=400 + i), target_id="tab-doomed", page_id="tab-doomed") for i in range(3)]
        live = [self._event(timedelta(days=410 + i)) for i in range(2)]
        self._insert_events(*doomed, *live)
        acted: list[list[int]] = []

        def delete_page_after_selection(ids: list[int]) -> None:
            acted.append(ids)
            if len(acted) > 1:
                return
            with self.engine.begin() as connection:
                self._hub_delete_page(connection, "tab-doomed")

        lines: list[str] = []
        outcome = self._archive(self.store, lines=lines, _after_select=delete_page_after_selection)
        self.assertEqual([len(ids) for ids in acted], [5, 2])
        self.assertEqual((outcome.reselected, outcome.archived, outcome.deleted, outcome.batches), (1, 2, 2, 1))
        self.assertEqual([call for call, _ in self.store.calls].count("put"), 1)
        self.assertTrue(any(line.startswith("note: 3 selected events row(s) came to reference a deleted resource before their batch was locked") for line in lines), lines)
        self.assertEqual({r["event_id"] for r in self._archived_records(self.store)}, {r["event_id"] for r in live})
        self.assertEqual(self._count(), 3)  # the doomed rows are deleted-resource rows now: the cleanup's job, never an archive's
        with self.Session() as db:
            self.assertEqual(service.purge_deleted_page_records(db, table="events", environment="sandbox"), 3)

    def test_a_deletion_in_flight_when_the_batch_locks_is_waited_for_then_the_batch_is_reselected(self) -> None:
        from unittest.mock import patch

        self._add_page("tab-doomed")
        doomed = [self._event(timedelta(days=400 + i), target_id="tab-doomed", page_id="tab-doomed") for i in range(3)]
        live = [self._event(timedelta(days=410 + i)) for i in range(2)]
        self._insert_events(*doomed, *live)
        holder = self.engine.connect()
        holder.begin()
        self._hub_delete_page(holder, "tab-doomed")  # uncommitted: the Hub request is still running
        outcomes: list[archive.ArchiveOutcome] = []
        lines: list[str] = []
        with patch.object(archive, "LOCK_TIMEOUT_MS", 4_000):
            thread = threading.Thread(target=lambda: outcomes.append(self._archive(self.store, lines=lines)))
            thread.start()
            thread.join(timeout=1.0)
            self.assertTrue(thread.is_alive(), "the batch must wait for the in-flight deletion")
            self.assertEqual(self.store.calls, [])
            holder.commit()
            thread.join(timeout=30)
        holder.close()
        self.assertEqual((outcomes[0].reselected, outcomes[0].archived), (1, 2))
        self.assertEqual({r["event_id"] for r in self._archived_records(self.store)}, {r["event_id"] for r in live})
        self.assertEqual(self._count(), 3)
        # A holder that outlives the lock timeout costs the batch, never the holder: nothing uploaded or deleted.
        self._insert_events(*[self._event(timedelta(days=420 + i), target_id="tab-live", page_id="tab-live") for i in range(2)])
        holder = self.engine.connect()
        holder.begin()
        self._hub_delete_page(holder, "tab-live", "component-live")  # uncommitted, outlives the job's lock timeout
        try:
            store = archive.MemoryArchiveStore()
            lines = []
            with patch.object(archive, "LOCK_TIMEOUT_MS", 300), patch.object(archive, "LOCK_RETRIES", 2), self.assertRaises(archive.ArchiveError) as caught:
                self._archive(store, lines=lines)
            self.assertIn("could not lock the Hub rows", str(caught.exception))
            self.assertEqual([line for line in lines if "retrying (1/2)" in line or "retrying (2/2)" in line].__len__(), 2)
            self.assertEqual(store.calls, [])
            self.assertEqual(self._count(), 5)
        finally:
            holder.rollback()
            holder.close()

    def test_an_upload_failure_releases_the_locks_and_the_pending_deletion_proceeds(self) -> None:
        rows = [*self._component_rows("component-live", "component-live"), self._event(timedelta(days=400))]
        self._insert_events(*rows)
        inside_put, release = threading.Event(), threading.Event()

        class FailAfterRelease(archive.MemoryArchiveStore):
            def put(self, key, data, metadata=None, *, if_none_match=False):
                self.calls.append(("put", key))
                inside_put.set()
                release.wait(timeout=30)
                raise OSError("storage unreachable")

        failing = FailAfterRelease()
        first: list[tuple[int, list[str]]] = []
        thread = threading.Thread(target=lambda: first.append(self._run(store=failing)))
        thread.start()
        self.assertTrue(inside_put.wait(timeout=30))
        done: list[int] = []

        def deleter() -> None:
            with self.engine.begin() as connection:
                done.append(connection.execute(text("DELETE FROM components WHERE link = 'component-live'")).rowcount)

        waiter = threading.Thread(target=deleter)
        try:
            waiter.start()
            waiter.join(timeout=1.5)
            self.assertTrue(waiter.is_alive())
        finally:
            release.set()
            thread.join(timeout=60)
        waiter.join(timeout=30)
        self.assertEqual(first[0][0], 1, first[0][1])
        self.assertEqual((done, failing.objects, self._count()), ([1], {}, 3))
        # Next run: the component's rows are deleted-resource rows and are
        # deleted without archiving; only the page row is archived.
        self._add_component("component-other")  # the Hub still has components
        code, lines = self._run()
        self.assertEqual(code, 0, lines)
        self.assertIn("deleted 2 events row(s) referencing deleted resources (not archived).", lines)
        self.assertEqual([r["event_id"] for r in self._archived_records(self.store)], [rows[2]["event_id"]])
        self.assertEqual(self._count(), 0)

    def test_a_rollback_after_the_upload_leaves_an_orphan_captured_while_its_resources_existed(self) -> None:
        """Documented limit: a batch that uploaded but failed before its
        delete (here: readback failure) leaves an object. Its rows were
        captured while their resources existed and could not be deleted;
        the resource may be deleted afterwards, and the next run then
        deletes those rows without archiving. The orphan stays and is never
        reopened; readers de-duplicate and apply the deleted-resource rule."""
        rows = self._component_rows("component-live", "component-live")
        self._insert_events(*rows)
        inside_put, release = threading.Event(), threading.Event()

        class CorruptReadback(archive.MemoryArchiveStore):
            corrupt = True

            def put(self, key, data, metadata=None, *, if_none_match=False):
                inside_put.set()
                release.wait(timeout=30)
                super().put(key, data, metadata, if_none_match=if_none_match)

            def get(self, key):
                return b"corrupt" if self.corrupt else super().get(key)

        store = CorruptReadback()
        first: list[tuple[int, list[str]]] = []
        thread = threading.Thread(target=lambda: first.append(self._run(store=store)))
        thread.start()
        self.assertTrue(inside_put.wait(timeout=30))
        done: list[int] = []

        def deleter() -> None:
            with self.engine.begin() as connection:
                done.append(connection.execute(text("DELETE FROM components WHERE link = 'component-live'")).rowcount)

        waiter = threading.Thread(target=deleter)
        try:
            waiter.start()
            waiter.join(timeout=1.0)
            self.assertTrue(waiter.is_alive())
        finally:
            release.set()
            thread.join(timeout=60)
        waiter.join(timeout=30)
        self.assertEqual(first[0][0], 1, first[0][1])
        self.assertEqual((done, len(store.objects), self._count()), ([1], 1, 2))
        store.corrupt = False
        self.assertEqual({r["event_id"] for r in self._archived_records(store)}, {r["event_id"] for r in rows})
        self._insert_events(self._event(timedelta(days=1)))  # the environment stays non-empty
        self._add_component("component-other")
        code, lines = self._run(store=store)
        self.assertEqual(code, 0, lines)
        self.assertIn("deleted 2 events row(s) referencing deleted resources (not archived).", lines)
        self.assertEqual(len(store.objects), 1)  # the orphan is neither reused nor rewritten
        self.assertEqual(self._count(), 1)

    def test_the_deleted_resource_cleanup_runs_alongside_an_uploading_batch_without_blocking(self) -> None:
        self._insert_events(self._event(timedelta(days=400)), self._event(timedelta(days=401)),
                            self._event(timedelta(days=5), target_id="tab-deleted", page_id="tab-deleted"))
        store, inside_put, release = self._blocking_store()
        outcomes: list[archive.ArchiveOutcome] = []
        thread = threading.Thread(target=lambda: outcomes.append(self._archive(store)))
        thread.start()
        self.assertTrue(inside_put.wait(timeout=30))
        try:
            with self.Session() as db:
                db.execute(text("SET LOCAL lock_timeout ='1500ms'"))
                self.assertEqual(service.purge_deleted_page_records(db, table="events", environment="sandbox"), 1)
        finally:
            release.set()
            thread.join(timeout=60)
        self.assertEqual((outcomes[0].archived, outcomes[0].deleted, self._count()), (2, 2, 0))

    def test_moving_a_component_and_deleting_a_mirror_target_during_the_upload_are_not_blocked(self) -> None:
        """History follows the component: a move during the upload is an
        ordinary update (not blocked) and the rows are archived under the
        component. A mirror's rows depend on the mirror row only; deleting
        what it shows is not blocked either."""
        with self.Session() as db:
            grid = db.execute(select(GridstackV2.id).where(GridstackV2.document_id == "tab-live")).scalar_one()
            other = TabV2(document_id="tab-other", title="other")
            db.add(other)
            db.flush()
            db.add(GridstackV2(document_id="tab-other", parent_tab_id=other.id))
            db.add(ComponentV2(link="target-live", type="links", gridstack_id=grid))
            db.add(ComponentV2(link="mirror-live", type="mirror", gridstack_id=grid, props={"target_link": "target-live"}))
            db.commit()
        rows = self._component_rows("mirror-live", "component-live")
        self._insert_events(*rows)
        store, inside_put, release = self._blocking_store()
        first: list[tuple[int, list[str]]] = []
        thread = threading.Thread(target=lambda: first.append(self._run(store=store)))
        thread.start()
        self.assertTrue(inside_put.wait(timeout=30))
        try:
            with self.engine.begin() as connection:
                connection.execute(text("SET LOCAL lock_timeout = '1500ms'"))
                moved = connection.execute(text(
                    "UPDATE components SET gridstack_id = (SELECT id FROM gridstacks WHERE document_id = 'tab-other') WHERE link = 'component-live'"
                )).rowcount
                removed = connection.execute(text("DELETE FROM components WHERE link = 'target-live'")).rowcount
            self.assertEqual((moved, removed), (1, 1))
        finally:
            release.set()
            thread.join(timeout=60)
        self.assertEqual(first[0][0], 0, first[0][1])
        self.assertEqual(self._count(), 0)
        self.assertEqual({r["event_id"] for r in self._archived_records(store)}, {r["event_id"] for r in rows})
        with self.engine.connect() as connection:
            self.assertEqual(
                set(connection.execute(text("SELECT link FROM components")).scalars()), {"component-live", "mirror-live"}
            )

    def test_concurrent_cleanup_runs_delete_each_stale_row_once_and_keep_live_rows(self) -> None:
        stale = [self._event(timedelta(days=i), target_id="tab-deleted", page_id="tab-deleted") for i in range(300)]
        live = [self._event(timedelta(days=i)) for i in range(100)]
        self._insert_events(*stale, *live)
        results: list[int] = []
        errors: list[BaseException] = []
        lock = threading.Lock()
        barrier = threading.Barrier(3)

        def worker() -> None:
            barrier.wait()
            try:
                with self.Session() as db:
                    removed = service.purge_deleted_page_records(db, table="events", environment="sandbox", batch_size=50)
            except BaseException as exc:  # noqa: BLE001 - collected for the assertion
                with lock:
                    errors.append(exc)
                return
            with lock:
                results.append(removed)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertEqual(errors, [])
        self.assertEqual(sum(results), 300)
        self.assertEqual(self._count(), 100)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT count(*) FROM hub_product_events WHERE target_id = 'tab-live'")).scalar_one(), 100)

    def test_cleanup_keeps_rows_whose_resource_exists_again_when_the_delete_runs(self) -> None:
        """Real concurrent transaction: between the cleanup's selection and
        its DELETE, another session re-creates the resource (or moves the
        component back). The DELETE re-applies the predicate and removes
        nothing; the rows survive."""
        self._insert_events(
            self._event(timedelta(days=5), target_id="tab-gone", page_id="tab-gone"),
            self._event(timedelta(days=5), event_name="link.clicked", component_type="link", action="open_link",
                        component_id="component-gone", target_id=None, page_id=None),
        )

        def resources_reappear(ids: list[int]) -> None:
            self.assertEqual(len(ids), 2)
            with self.engine.begin() as connection:
                connection.execute(text("INSERT INTO tabs (document_id, title) VALUES ('tab-gone', 'back')"))
                grid = connection.execute(text("SELECT id FROM gridstacks WHERE document_id = 'tab-live'")).scalar_one()
                connection.execute(text("INSERT INTO components (link, type, gridstack_id) VALUES ('component-gone', 'links', :g)"), {"g": grid})

        with self.Session() as db:
            self.assertEqual(service.purge_deleted_page_records(db, table="events", environment="sandbox", _after_select=resources_reappear), 0)
        self.assertEqual(self._count(), 2)
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM components WHERE link = 'component-gone'"))
            connection.execute(text("DELETE FROM tabs WHERE document_id = 'tab-gone'"))
        with self.Session() as db:
            self.assertEqual(service.purge_deleted_page_records(db, table="events", environment="sandbox"), 2)

    def test_purge_loop_terminates_when_a_row_is_removed_between_select_and_delete(self) -> None:
        self._insert_events(self._event(timedelta(days=5), target_id="tab-deleted", page_id="tab-deleted"))
        with self.Session() as db:
            self.assertEqual(service.purge_deleted_page_records(db, table="events", environment="sandbox"), 1)
            self.assertEqual(service.purge_deleted_page_records(db, table="events", environment="sandbox"), 0)


if __name__ == "__main__":
    unittest.main()
