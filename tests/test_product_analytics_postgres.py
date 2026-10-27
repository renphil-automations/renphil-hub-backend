"""Product-usage analytics against a real, disposable PostgreSQL.

Opt-in: set PRODUCT_ANALYTICS_TEST_DATABASE_URL to an EMPTY throwaway database
(for example ``postgresql+psycopg2://qa@127.0.0.1:55432/hubqa``). The test
drops and recreates only ``hub_product_events``. Never point it at a shared,
Sandbox or Production database.

Covers what SQLite cannot: the ``ON CONFLICT DO NOTHING ... RETURNING``
idempotency path, concurrent retries of one batch, JSONB-compatible storage,
``date()`` grouping on timestamptz, and the idempotent create script.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.db_v2.database import get_db_v2
from app.db_v2.models.product_analytics import HubProductEventV2
from app.dependencies import CurrentHubUser, get_current_hub_user
from app.routers import bot_management, product_analytics
from app.services.access_visibility_service import ViewerAccess

DATABASE_URL = os.environ.get("PRODUCT_ANALYTICS_TEST_DATABASE_URL", "").strip()
BACKEND_ROOT = Path(__file__).resolve().parents[1]

MEMBER = CurrentHubUser(info=SimpleNamespace(email="member@renphil.org", roles=[]), hub_user_id=2, email="member@renphil.org")
ADMIN = CurrentHubUser(info=SimpleNamespace(email="admin@renphil.org", roles=[]), hub_user_id=1, email="admin@renphil.org")


class _Limiter:
    async def check(self, action: str, email: str) -> int | None:
        return None

    async def consume(self, action: str, email: str) -> None:
        return None


def _event(name: str = "root_tab.selected", **overrides) -> dict:
    event = {
        "event_id": str(uuid.uuid4()),
        "event_name": name,
        "trigger": "click",
        "page_route": "/finance/jane-doe-review",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "properties": {"tab_kind": "root"},
    }
    event.update(overrides)
    return event


@unittest.skipUnless(DATABASE_URL, "PRODUCT_ANALYTICS_TEST_DATABASE_URL is not set")
class ProductAnalyticsPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if any(word in DATABASE_URL.lower() for word in ("prod", "neon.tech", "sandbox")):
            raise unittest.SkipTest("refusing a database URL that looks shared")
        cls.engine = create_engine(DATABASE_URL, pool_size=8)
        HubProductEventV2.__table__.drop(bind=cls.engine, checkfirst=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        # The deployment script, run twice: it must be idempotent.
        env = {**os.environ, "DATABASE_URL_V2": DATABASE_URL}
        for _ in range(2):
            subprocess.run(
                [sys.executable, "-m", "scripts.create_product_analytics_tables"],
                cwd=BACKEND_ROOT, env=env, check=True, capture_output=True, text=True,
            )
        with self.engine.begin() as conn:
            conn.execute(text("TRUNCATE hub_product_events"))
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        self.current = MEMBER
        # Live Hub access is covered against the real fold in the SQLite
        # suite; here it is either everything or nothing.
        self.access = ViewerAccess(visibility=None, full_access=True)
        # Ingest opens its own session per worker thread; track each one.
        self.worker_sessions: list[dict] = []
        self._lock = threading.Lock()
        app = FastAPI()
        app.include_router(product_analytics.router)

        def db_override():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_v2] = db_override
        app.dependency_overrides[get_current_hub_user] = lambda: self.current
        self.client = TestClient(app)
        self._patches = [
            patch.object(bot_management, "hub_admin_target_ids", lambda _db: (10, [20])),
            patch.object(bot_management, "effective_pairs", lambda _db, uid: {(10, 20)} if uid == 1 else set()),
            patch.object(product_analytics, "get_settings", lambda: SimpleNamespace(hub_environment="sandbox")),
            patch.object(product_analytics, "get_rate_limit_service", lambda: _Limiter()),
            patch.object(product_analytics, "_viewer_access", self._fake_access),
            patch.object(product_analytics, "SessionLocalV2", self._worker_session),
        ]
        for p in self._patches:
            p.start()

    def _worker_session(self):
        db = self.Session()
        record = {"session": db, "thread": threading.get_ident(), "closed_in": None, "access_db": None}
        close = db.close

        def tracked_close():
            record["closed_in"] = threading.get_ident()
            close()

        db.close = tracked_close
        with self._lock:
            self.worker_sessions.append(record)
        return db

    def _fake_access(self, db, _current):
        with self._lock:
            for record in self.worker_sessions:
                if record["session"] is db:
                    record["access_db"] = (threading.get_ident(), db)
        return self.access

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()

    def _count(self) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(text("SELECT COUNT(*) FROM hub_product_events")).scalar_one())

    def test_schema_created_by_the_idempotent_script(self) -> None:
        inspector = inspect(self.engine)
        columns = {column["name"] for column in inspector.get_columns("hub_product_events")}
        self.assertIn("page_route", columns)
        self.assertNotIn("page_path", columns)
        indexes = {index["name"] for index in inspector.get_indexes("hub_product_events")}
        self.assertTrue({
            "ix_hub_product_events_occurred_at", "ix_hub_product_events_name_occurred",
            "ix_hub_product_events_actor_occurred", "ix_hub_product_events_component_occurred",
        } <= indexes)
        uniques = inspector.get_unique_constraints("hub_product_events")
        self.assertTrue(any(u["column_names"] == ["event_id"] for u in uniques))

    def test_retry_is_ingested_once_and_route_titles_are_not_stored(self) -> None:
        batch = [_event(), _event("button.clicked", properties={"destination_kind": "external"})]
        first = self.client.post("/v2/product-analytics/events", json={"events": batch}).json()
        retry = self.client.post("/v2/product-analytics/events", json={"events": batch}).json()
        self.assertEqual((first["accepted"], first["duplicates"]), (2, 0))
        self.assertEqual((retry["accepted"], retry["duplicates"]), (0, 2))
        self.assertEqual(self._count(), 2)
        with self.engine.connect() as conn:
            rows = conn.execute(text("SELECT page_route, environment, actor_email, properties::text FROM hub_product_events")).all()
        self.assertEqual({r[0] for r in rows}, {"/:nav_tab/:tab"})
        self.assertEqual({(r[1], r[2]) for r in rows}, {("sandbox", "member@renphil.org")})
        self.assertNotIn("jane", repr(rows).lower())

    def test_concurrent_retries_of_one_batch_store_each_event_once(self) -> None:
        batch = [_event() for _ in range(20)]
        results: list[dict] = []

        def send() -> None:
            results.append(self.client.post("/v2/product-analytics/events", json={"events": batch}).json())

        threads = [threading.Thread(target=send) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self._count(), 20)
        self.assertEqual(sum(r["accepted"] for r in results), 20)
        self.assertEqual(sum(r["duplicates"] for r in results), 100)

    def test_concurrent_distinct_batches_each_use_their_own_worker_session(self) -> None:
        from app.db_v2.database import BaseV2
        from app.db_v2.models.nav_tab import NavTabV2

        BaseV2.metadata.create_all(bind=self.engine, tables=[NavTabV2.__table__], checkfirst=True)
        doc = f"qa-nav-{uuid.uuid4()}"
        with self.engine.begin() as conn:
            conn.execute(NavTabV2.__table__.insert().values(document_id=doc, slug=doc, title="QA concurrent nav"))
        try:
            batches = [
                [_event("navigation_tab.selected", target_id=doc, properties={"tab_kind": "nav_tab"})]
                + [_event() for _ in range(9)]
                for _ in range(8)
            ]
            batches.append(batches[0])  # one retry racing its original
            results: list[tuple[int, dict]] = []
            barrier = threading.Barrier(len(batches))

            def send(batch) -> None:
                barrier.wait()
                response = self.client.post("/v2/product-analytics/events", json={"events": batch})
                with self._lock:
                    results.append((response.status_code, response.json()))

            threads = [threading.Thread(target=send, args=(b,)) for b in batches]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual({code for code, _ in results}, {200})
            self.assertEqual(sum(body["accepted"] for _, body in results), 80)
            self.assertEqual(sum(body["duplicates"] for _, body in results), 10)
            self.assertEqual(self._count(), 80)
            with self.engine.connect() as conn:
                kept = conn.execute(text("SELECT COUNT(*) FROM hub_product_events WHERE target_id = :d"), {"d": doc}).scalar_one()
            self.assertEqual(kept, 8)
            # One session per batch, never shared, each closed in the thread
            # that created it and used for that batch's access resolution.
            self.assertEqual(len(self.worker_sessions), len(batches))
            self.assertEqual(len({id(r["session"]) for r in self.worker_sessions}), len(batches))
            for record in self.worker_sessions:
                self.assertEqual(record["closed_in"], record["thread"])
                self.assertEqual(record["access_db"], (record["thread"], record["session"]))
            # Nothing left holding a transaction open on the server.
            with self.engine.connect() as conn:
                idle_in_tx = conn.execute(text(
                    "SELECT COUNT(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND state LIKE 'idle in transaction%' AND pid <> pg_backend_pid()"
                )).scalar_one()
            self.assertEqual(idle_in_tx, 0)
        finally:
            with self.engine.begin() as conn:
                conn.execute(NavTabV2.__table__.delete().where(NavTabV2.document_id == doc))

    def test_admin_reads_on_postgres(self) -> None:
        self.client.post("/v2/product-analytics/events", json={"events": [
            _event(), _event("view_tab.selected", trigger="default", cascade_id=str(uuid.uuid4())),
            _event("link.clicked", page_route="/admin/bot-management", properties={}),
        ]})
        self.current = ADMIN
        summary = self.client.get("/v2/product-analytics/summary").json()
        self.assertEqual(summary["totals"], {"events": 3, "users": 1})
        self.assertEqual(sum(day["count"] for day in summary["by_day"]), 3)
        self.assertEqual(
            {row["key"]: row["count"] for row in summary["by_page"]},
            {"/:nav_tab/:tab": 2, "/admin/bot-management": 1},
        )
        events = self.client.get("/v2/product-analytics/events", params={"page_route": "/:nav_tab"}).json()
        self.assertEqual(events["total"], 2)
        self.assertEqual(
            self.client.get("/v2/product-analytics/events", params={"page_route": "/:nav%"}).json()["total"], 0,
        )
        export = self.client.get("/v2/product-analytics/export", params={"trigger": "default"})
        self.assertEqual(export.status_code, 200)
        self.assertEqual(len(export.text.strip().splitlines()), 2)
        self.current = MEMBER
        self.assertEqual(self.client.get("/v2/product-analytics/summary").status_code, 403)


    def test_ids_are_verified_against_postgres_records(self) -> None:
        from app.db_v2.database import BaseV2
        from app.db_v2.models.nav_tab import NavTabV2

        BaseV2.metadata.create_all(bind=self.engine, tables=[NavTabV2.__table__], checkfirst=True)
        doc = f"qa-nav-{uuid.uuid4()}"
        with self.engine.begin() as conn:
            conn.execute(NavTabV2.__table__.insert().values(document_id=doc, slug=doc, title="Jane Doe QA nav"))
        try:
            body = self.client.post("/v2/product-analytics/events", json={"events": [
                _event("navigation_tab.selected", target_id=doc, properties={"tab_kind": "nav_tab"}),
                _event("navigation_tab.selected", target_id="jane-doe-salary", properties={"tab_kind": "nav_tab"}),
            ]}).json()
            self.assertEqual(body, {"accepted": 2, "duplicates": 0, "rejected": []})
            with self.engine.connect() as conn:
                targets = sorted((r[0] or "") for r in conn.execute(text("SELECT target_id FROM hub_product_events")))
            self.assertEqual(targets, ["", doc])
            self.current = ADMIN
            names = {e["target_name"] for e in self.client.get("/v2/product-analytics/events").json()["data"]}
            self.assertEqual(names, {"Jane Doe QA nav", None})
            with self.engine.connect() as conn:
                dump = repr(conn.execute(text("SELECT * FROM hub_product_events")).all())
            self.assertNotIn("Jane", dump)
            # The same real id, reported by someone who cannot view it, is
            # dropped and answered identically.
            self.current = MEMBER
            self.access = ViewerAccess(visibility=None, full_access=False)
            hidden = _event("navigation_tab.selected", target_id=doc, properties={"tab_kind": "nav_tab"})
            fake = _event("navigation_tab.selected", target_id=str(uuid.uuid4()), properties={"tab_kind": "nav_tab"})
            self.assertEqual(
                self.client.post("/v2/product-analytics/events", json={"events": [hidden]}).json(),
                self.client.post("/v2/product-analytics/events", json={"events": [fake]}).json(),
            )
            with self.engine.connect() as conn:
                stored = dict(conn.execute(text("SELECT event_id, target_id FROM hub_product_events")).all())
            self.assertIsNone(stored[hidden["event_id"]])
            self.assertIsNone(stored[fake["event_id"]])
        finally:
            with self.engine.begin() as conn:
                conn.execute(NavTabV2.__table__.delete().where(NavTabV2.document_id == doc))

    def test_purge_in_batches_on_postgres(self) -> None:
        from datetime import timedelta

        from app.services import product_analytics_service as service

        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            for days in [*range(400, 412), 5]:
                at = now - timedelta(days=days)
                conn.execute(HubProductEventV2.__table__.insert().values(
                    event_id=str(uuid.uuid4()), event_name="root_tab.selected", component_type="root_tab",
                    action="select", trigger="click", hub_user_id=1, actor_email="a@renphil.org",
                    environment="sandbox", page_route="/:nav_tab", properties={}, occurred_at=at, received_at=at,
                ))
        cutoff = service.retention_cutoff(396, now)
        with self.Session() as db:
            self.assertEqual(service.count_expired_events(db, cutoff=cutoff), 12)
            self.assertEqual(service.purge_expired_events(db, cutoff=cutoff, batch_size=5), 12)
        self.assertEqual(self._count(), 1)


if __name__ == "__main__":
    unittest.main()
