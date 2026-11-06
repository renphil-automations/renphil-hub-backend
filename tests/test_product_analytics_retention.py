"""Retention for Hub product-usage events: configuration, cutoff, batching,
and the purge command (counts only, never row contents)."""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db_v2.models.product_analytics import HubProductEventV2, HubProductEngagementV2
from app.services import product_analytics_service as service
from scripts import purge_product_analytics_events as purge_script

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


class RetentionConfigTests(unittest.TestCase):
    def test_default_is_365_days(self) -> None:
        self.assertEqual(service.DEFAULT_RETENTION_DAYS, 365)
        for raw in (None, "", "  "):
            self.assertEqual(service.parse_retention_days(raw), 365)

    def test_explicit_disable(self) -> None:
        for raw in ("0", "off", "OFF", "disabled", "none"):
            self.assertIsNone(service.parse_retention_days(raw))

    def test_valid_values(self) -> None:
        self.assertEqual(service.parse_retention_days("30"), 30)
        self.assertEqual(service.parse_retention_days(" 400 "), 400)
        self.assertEqual(service.parse_retention_days("3650"), 3650)

    def test_invalid_values_are_refused(self) -> None:
        for raw in ("-5", "29", "3651", "12.5", "1e3", "abc", "396 days", "99999999"):
            with self.assertRaises(service.RetentionConfigError, msg=raw):
                service.parse_retention_days(raw)

    def test_settings_default(self) -> None:
        from app.config import Settings

        self.assertEqual(Settings.model_fields["PRODUCT_ANALYTICS_RETENTION_DAYS"].default, "365")


class PurgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        HubProductEventV2.__table__.create(bind=self.engine)
        HubProductEngagementV2.__table__.create(bind=self.engine)
        with self.engine.begin() as connection:
            for table in ("nav_tabs", "tabs", "gridstacks"):
                connection.execute(text(f"CREATE TABLE {table} (document_id VARCHAR(255))"))
            connection.execute(text("CREATE TABLE components (link VARCHAR(255))"))
            # A real Hub always has rows in all four tables; the cleanup
            # refuses to run against a database that does not (see
            # test_cleanup_refuses_when_hub_tables_are_empty).
            connection.execute(text("INSERT INTO nav_tabs (document_id) VALUES ('nav-live')"))
            connection.execute(text("INSERT INTO tabs (document_id) VALUES ('tab-live')"))
            connection.execute(text("INSERT INTO gridstacks (document_id) VALUES ('sub-live')"))
            connection.execute(text("INSERT INTO components (link) VALUES ('component-live')"))
        self.Session = sessionmaker(bind=self.engine)

    def _empty_hub_tables(self, *tables: str) -> None:
        with self.engine.begin() as connection:
            for table in tables or service.HUB_RESOURCE_TABLES:
                connection.execute(text(f"DELETE FROM {table}"))

    def _add(self, *ages: timedelta) -> None:
        with self.Session() as db:
            for age in ages:
                at = NOW - age
                db.add(HubProductEventV2(
                    event_id=str(uuid.uuid4()), event_name="root_tab.selected", component_type="root_tab",
                    action="select", trigger="click", hub_user_id=1, actor_email="a@renphil.org",
                    environment="sandbox", page_route="/:nav_tab", properties={}, occurred_at=at, received_at=at,
                ))
            db.commit()

    def _count(self) -> int:
        with self.Session() as db:
            return int(db.execute(select(func.count()).select_from(HubProductEventV2)).scalar_one())

    def test_cutoff_deletes_only_older_rows(self) -> None:
        self._add(timedelta(days=500), timedelta(days=397), timedelta(days=396, seconds=1),
                  timedelta(days=395), timedelta(days=1), timedelta(0))
        cutoff = service.retention_cutoff(396, NOW)
        with self.Session() as db:
            self.assertEqual(service.count_expired_events(db, cutoff=cutoff), 3)
            self.assertEqual(service.purge_expired_events(db, cutoff=cutoff), 3)
        self.assertEqual(self._count(), 3)
        with self.Session() as db:
            remaining = db.execute(select(HubProductEventV2.occurred_at)).scalars().all()
        self.assertTrue(all(
            (at if at.tzinfo else at.replace(tzinfo=timezone.utc)) >= cutoff for at in remaining
        ))

    def test_exact_cutoff_row_is_kept(self) -> None:
        self._add(timedelta(days=396))
        with self.Session() as db:
            self.assertEqual(service.purge_expired_events(db, cutoff=service.retention_cutoff(396, NOW)), 0)
        self.assertEqual(self._count(), 1)

    def test_batches_are_bounded(self) -> None:
        self._add(*[timedelta(days=400 + i) for i in range(25)], timedelta(days=10))
        cutoff = service.retention_cutoff(396, NOW)
        with self.Session() as db:
            self.assertEqual(service.purge_expired_events(db, cutoff=cutoff, batch_size=10, max_batches=2), 20)
        self.assertEqual(self._count(), 6)
        with self.Session() as db:
            self.assertEqual(service.purge_expired_events(db, cutoff=cutoff, batch_size=10), 5)
        self.assertEqual(self._count(), 1)
        with self.assertRaises(ValueError), self.Session() as db:
            service.purge_expired_events(db, cutoff=cutoff, batch_size=0)

    def _run(self, raw: str | None, *argv: str, environment: str = "sandbox") -> tuple[int, list[str]]:
        lines: list[str] = []
        code = purge_script.run(list(argv), session_factory=self.Session, raw_retention=raw, environment=environment, now=NOW, out=lines.append)
        return code, lines

    def test_script_retains_expired_records_without_verified_archive(self) -> None:
        self._add(timedelta(days=500), timedelta(days=400), timedelta(days=5))
        code, lines = self._run("396", "--batch-size", "100")
        self.assertEqual(code, 0)
        self.assertIn("verified analytics archiving", lines[1])
        self.assertEqual(self._count(), 3)
        self.assertNotIn("a@renphil.org", " ".join(lines))

    def test_script_dry_run_deletes_nothing(self) -> None:
        self._add(timedelta(days=500), timedelta(days=5))
        code, lines = self._run(None, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("dry run: 1 event(s)", lines[0])
        self.assertIn("remain until archiving is available", lines[0])
        self.assertNotIn("would be deleted", lines[0])
        self.assertEqual(self._count(), 2)

    def test_script_retains_recent_intervals_and_deletes_expired(self) -> None:
        with self.Session() as db:
            for age in (500, 5):
                at = NOW - timedelta(days=age)
                db.add(HubProductEngagementV2(engagement_id=str(uuid.uuid4()), hub_user_id=1,
                    actor_email="a@renphil.org", environment="sandbox", page_route="/:nav_tab",
                    active_seconds=5, started_at=at, ended_at=at, received_at=at))
            db.commit()
        code, lines = self._run("396", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("1 age-expired active-time interval(s)", lines[1])
        code, lines = self._run("396")
        self.assertEqual(code, 0)
        self.assertIn("verified analytics archiving", lines[1])
        with self.Session() as db:
            self.assertEqual(db.query(HubProductEngagementV2).count(), 2)

    def test_deleted_pages_are_purged_but_live_and_unbound_history_stays(self) -> None:
        with self.Session() as db:
            for kind, target in [("root_tab.selected", "tab-live"), ("root_tab.selected", "tab-deleted"),
                                 ("sub_tab.selected", "sub-live"), ("navigation_tab.selected", "nav-deleted"),
                                 ("root_tab.selected", None), ("link.clicked", "tab-deleted")]:
                db.add(HubProductEventV2(event_id=str(uuid.uuid4()), event_name=kind, component_type="test",
                    action="select", trigger="click", hub_user_id=1, actor_email="a@renphil.org", environment="local",
                    page_route="/:nav_tab", target_id=target, properties={}, occurred_at=NOW, received_at=NOW))
            for nav, tab in [("nav-live", "tab-live"), ("nav-live", "sub-live"), ("nav-live", "tab-deleted"),
                             ("nav-deleted", None), (None, None)]:
                db.add(HubProductEngagementV2(engagement_id=str(uuid.uuid4()), hub_user_id=1,
                    actor_email="a@renphil.org", environment="local", page_route="/:nav_tab", nav_tab_id=nav,
                    tab_id=tab, active_seconds=5, started_at=NOW, ended_at=NOW, received_at=NOW))
            db.commit()
        code, lines = self._run("365", "--dry-run", environment="local")
        self.assertEqual(code, 0)
        self.assertIn("2 events row(s)", lines[2])
        self.assertIn("2 engagements row(s)", lines[3])
        self.assertEqual(self._count(), 6)
        with self.Session() as db:
            self.assertEqual(service.purge_deleted_page_records(db, table="events", environment="local"), 2)
            self.assertEqual(service.purge_deleted_page_records(db, table="engagements", environment="local"), 2)
        self.assertEqual(self._count(), 4)
        with self.Session() as db:
            self.assertEqual(db.query(HubProductEngagementV2).count(), 3)

    def test_deleted_component_events_are_removed_but_existing_and_unbound_events_are_kept(self) -> None:
        with self.Session() as db:
            for link in ("component-live", "component-deleted", None):
                db.add(HubProductEventV2(event_id=str(uuid.uuid4()), event_name="link.clicked", component_type="links",
                    action="click", trigger="click", hub_user_id=1, actor_email="a@renphil.org", environment="local",
                    page_route="/:nav_tab", component_id=link, properties={}, occurred_at=NOW, received_at=NOW))
            db.commit()
        with self.Session() as db:
            self.assertEqual(service.count_deleted_page_records(db, table="events"), 1)
            self.assertEqual(service.purge_deleted_page_records(db, table="events", environment="local"), 1)
        self.assertEqual(self._count(), 2)

    def test_moved_component_history_survives_deletion_of_its_original_page(self) -> None:
        """A component interaction belongs to its component. A widget moved
        away from a page before that page was deleted still exists, so its
        history (which Page Insights show under its current placement) is
        kept. A selection of the deleted page itself is removed."""
        with self.Session() as db:
            for page_id in ("tab-live", "tab-deleted", None):
                db.add(HubProductEventV2(event_id=str(uuid.uuid4()), event_name="link.clicked", component_type="links",
                    action="click", trigger="click", hub_user_id=1, actor_email="a@renphil.org", environment="local",
                    page_route="/:nav_tab", component_id="component-live", page_id=page_id, properties={}, occurred_at=NOW, received_at=NOW))
            # The deleted page's own selection event: page_id = target_id.
            db.add(HubProductEventV2(event_id=str(uuid.uuid4()), event_name="root_tab.selected", component_type="root_tab",
                action="select", trigger="click", hub_user_id=1, actor_email="a@renphil.org", environment="local",
                page_route="/:nav_tab", target_id="tab-deleted", page_id="tab-deleted", properties={}, occurred_at=NOW, received_at=NOW))
            # A component click whose component was deleted WITH its page.
            db.add(HubProductEventV2(event_id=str(uuid.uuid4()), event_name="link.clicked", component_type="links",
                action="click", trigger="click", hub_user_id=1, actor_email="a@renphil.org", environment="local",
                page_route="/:nav_tab", component_id="component-deleted", page_id="tab-deleted", properties={}, occurred_at=NOW, received_at=NOW))
            db.commit()
        with self.Session() as db:
            self.assertEqual(service.count_deleted_page_records(db, table="events", environment="local"), 2)
            self.assertEqual(service.purge_deleted_page_records(db, table="events", environment="local"), 2)
        self.assertEqual(self._count(), 3)
        with self.Session() as db:
            kept = {(r.component_id, r.page_id) for r in db.query(HubProductEventV2)}
        self.assertEqual(kept, {("component-live", "tab-live"), ("component-live", "tab-deleted"), ("component-live", None)})

    def test_deleted_blocknote_page_selections_are_removed(self) -> None:
        with self.Session() as db:
            db.execute(text("INSERT INTO components (link) VALUES ('sbn-root'), ('sbn-page-live')"))
            for target in ("sbn-page-live", "sbn-page-deleted", "sbn-root", None):
                db.add(HubProductEventV2(event_id=str(uuid.uuid4()), event_name="blocknote_tab.selected", component_type="blocknote_tab",
                    action="select", trigger="click", hub_user_id=1, actor_email="a@renphil.org", environment="local",
                    page_route="/:nav_tab", component_id="sbn-root", target_id=target, properties={}, occurred_at=NOW, received_at=NOW))
            db.commit()
        with self.Session() as db:
            self.assertEqual(service.purge_deleted_page_records(db, table="events", environment="local"), 1)
            self.assertEqual({r.target_id for r in db.query(HubProductEventV2)}, {"sbn-page-live", "sbn-root", None})

    def test_cleanup_refuses_when_hub_tables_are_empty(self) -> None:
        """An analytics table in a database without Hub rows (wrong
        DATABASE_URL_V2, partial restore) would make every bound row look
        deleted. The job refuses, in dry-run and for real, and deletes nothing."""
        with self.Session() as db:
            db.add(HubProductEventV2(event_id=str(uuid.uuid4()), event_name="root_tab.selected", component_type="root_tab",
                action="select", trigger="click", hub_user_id=1, actor_email="a@renphil.org", environment="sandbox",
                page_route="/:nav_tab", target_id="tab-live", page_id="tab-live", properties={}, occurred_at=NOW, received_at=NOW))
            db.commit()
        for empty in (("components",), ("nav_tabs", "tabs", "gridstacks", "components")):
            with self.subTest(empty=empty):
                self._empty_hub_tables(*empty)
                for argv in (["--dry-run"], []):
                    code, lines = self._run("365", *argv)
                    self.assertEqual(code, 2)
                    self.assertIn("refused", lines[0])
                    self.assertIn(empty[0], lines[0])
                    self.assertNotIn("would be deleted", " ".join(lines))
                with self.Session() as db, self.assertRaises(service.CleanupPreconditionError):
                    service.purge_deleted_page_records(db, table="events", environment="sandbox")
                self.assertEqual(self._count(), 1)
                with self.engine.begin() as connection:
                    for table, value in zip(service.HUB_RESOURCE_TABLES, ("nav-live", "tab-live", "sub-live", "component-live")):
                        if table in empty:
                            column = "link" if table == "components" else "document_id"
                            connection.execute(text(f"INSERT INTO {table} ({column}) VALUES ('{value}')"))
        code, lines = self._run("365")
        self.assertEqual((code, self._count()), (0, 1))

    def test_an_environment_without_analytics_rows_passes_the_empty_hub_guard(self) -> None:
        """A fresh deployment or a reset sandbox has empty Hub tables and no
        analytics; there is nothing the cleanup could delete, so it is not
        treated as a wrong database. The guard is per environment and comes
        back with the first analytics row of that environment."""
        self._empty_hub_tables()
        with self.Session() as db:
            db.add(HubProductEventV2(event_id=str(uuid.uuid4()), event_name="root_tab.selected", component_type="root_tab",
                action="select", trigger="click", hub_user_id=1, actor_email="a@renphil.org", environment="production",
                page_route="/:nav_tab", target_id="tab-live", page_id="tab-live", properties={}, occurred_at=NOW, received_at=NOW))
            db.commit()
        for argv in (["--dry-run"], []):
            code, lines = self._run("365", *argv, environment="sandbox")
            self.assertEqual(code, 0, lines)
        with self.Session() as db:
            self.assertEqual(service.purge_deleted_page_records(db, table="events", environment="sandbox"), 0)
            with self.assertRaises(service.CleanupPreconditionError):
                service.purge_deleted_page_records(db, table="events", environment="production")
            with self.assertRaises(service.CleanupPreconditionError):
                service.require_hub_resources(db)
        self.assertEqual(self._count(), 1)
        code, lines = self._run("365", environment="production")
        self.assertEqual(code, 2, lines)
        self.assertIn("refused", lines[0])

    def test_script_disabled_deletes_nothing(self) -> None:
        self._add(timedelta(days=5000))
        code, lines = self._run("off")
        self.assertEqual(code, 0)
        self.assertIn("disabled", lines[0])
        self.assertEqual(self._count(), 1)

    def test_script_invalid_config_deletes_nothing(self) -> None:
        self._add(timedelta(days=5000))
        for raw in ("7", "-1", "abc"):
            code, lines = self._run(raw)
            self.assertEqual(code, 2, raw)
            self.assertIn("refused", lines[0])
        self.assertEqual(self._count(), 1)

    def test_cleanup_only_deletes_its_configured_environment(self) -> None:
        with self.Session() as db:
            for environment in ("sandbox", "production"):
                for fields in ({"page_id": "deleted-page"}, {"component_id": "deleted-widget"}, {"target_id": "deleted-tab"}):
                    db.add(HubProductEventV2(event_id=str(uuid.uuid4()), event_name="root_tab.selected", component_type="root_tab",
                        action="select", trigger="click", hub_user_id=1, actor_email="a@renphil.org", environment=environment,
                        page_route="/:nav_tab", properties={}, occurred_at=NOW, received_at=NOW, **fields))
                db.add(HubProductEngagementV2(engagement_id=str(uuid.uuid4()), hub_user_id=1,
                    actor_email="a@renphil.org", environment=environment, page_route="/:nav_tab", tab_id="deleted-tab",
                    active_seconds=5, started_at=NOW, ended_at=NOW, received_at=NOW))
            db.commit()
        code, lines = self._run("365", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("3 events row(s)", lines[2])
        self.assertIn("1 engagements row(s)", lines[3])
        code, lines = self._run("365")
        self.assertEqual(code, 0)
        self.assertIn("deleted 3 event(s) and 1 active-time", lines[0])
        with self.Session() as db:
            self.assertEqual({r.environment for r in db.query(HubProductEventV2)}, {"production"})
            self.assertEqual({r.environment for r in db.query(HubProductEngagementV2)}, {"production"})

    def test_cleanup_rejects_unconfigured_environment_without_deletion(self) -> None:
        self._add(timedelta(days=5000))
        code, lines = self._run("365", environment="unconfigured")
        self.assertEqual(code, 2)
        self.assertIn("HUB_ENVIRONMENT", lines[0])
        self.assertEqual(self._count(), 1)
        with self.Session() as db, self.assertRaises(ValueError):
            service.purge_deleted_page_records(db, table="events", environment="unconfigured")

    def test_cleanup_rejects_nonpositive_max_batches(self) -> None:
        for value in ("0", "-1"):
            code, lines = self._run("365", "--max-batches", value)
            self.assertEqual(code, 2)
            self.assertIn("--max-batches", lines[0])

    def test_a_resource_that_exists_again_at_delete_time_keeps_its_rows(self) -> None:
        """The DELETE re-applies the predicate on the selected ids, so a row
        selected as stale is kept if its resource exists when the DELETE
        runs (the PostgreSQL suite exercises this with a real concurrent
        transaction)."""
        with self.Session() as db:
            db.add(HubProductEventV2(event_id=str(uuid.uuid4()), event_name="root_tab.selected", component_type="root_tab",
                action="select", trigger="click", hub_user_id=1, actor_email="a@renphil.org", environment="local",
                page_route="/:nav_tab", target_id="tab-gone", page_id="tab-gone", properties={}, occurred_at=NOW, received_at=NOW))
            db.commit()
        with self.Session() as db:
            def recreate(ids: list[int]) -> None:
                self.assertEqual(len(ids), 1)
                db.execute(text("INSERT INTO tabs (document_id) VALUES ('tab-gone')"))

            self.assertEqual(service.purge_deleted_page_records(db, table="events", environment="local", _after_select=recreate), 0)
        self.assertEqual(self._count(), 1)
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM tabs WHERE document_id = 'tab-gone'"))
        with self.Session() as db:
            self.assertEqual(service.purge_deleted_page_records(db, table="events", environment="local"), 1)

    def test_script_rejects_unbounded_batch_sizes(self) -> None:
        self._add(timedelta(days=5000))
        for size in ("0", "10", "1000000"):
            code, _ = self._run("396", "--batch-size", size)
            self.assertEqual(code, 2)
        self.assertEqual(self._count(), 1)


if __name__ == "__main__":
    unittest.main()
