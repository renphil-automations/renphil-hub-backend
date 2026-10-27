"""Retention for Hub product-usage events: configuration, cutoff, batching,
and the purge command (counts only, never row contents)."""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db_v2.models.product_analytics import HubProductEventV2
from app.services import product_analytics_service as service
from scripts import purge_product_analytics_events as purge_script

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


class RetentionConfigTests(unittest.TestCase):
    def test_default_is_396_days(self) -> None:
        self.assertEqual(service.DEFAULT_RETENTION_DAYS, 396)
        for raw in (None, "", "  "):
            self.assertEqual(service.parse_retention_days(raw), 396)

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

        self.assertEqual(Settings.model_fields["PRODUCT_ANALYTICS_RETENTION_DAYS"].default, "396")


class PurgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        HubProductEventV2.__table__.create(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)

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

    def _run(self, raw: str | None, *argv: str) -> tuple[int, list[str]]:
        lines: list[str] = []
        code = purge_script.run(list(argv), session_factory=self.Session, raw_retention=raw, now=NOW, out=lines.append)
        return code, lines

    def test_script_deletes_and_reports_only_counts(self) -> None:
        self._add(timedelta(days=500), timedelta(days=400), timedelta(days=5))
        code, lines = self._run("396", "--batch-size", "100")
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["deleted 2 event(s) older than 2025-09-25 (retention 396 days)."])
        self.assertEqual(self._count(), 1)
        self.assertNotIn("a@renphil.org", " ".join(lines))

    def test_script_dry_run_deletes_nothing(self) -> None:
        self._add(timedelta(days=500), timedelta(days=5))
        code, lines = self._run(None, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("dry run: 1 event(s)", lines[0])
        self.assertEqual(self._count(), 2)

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

    def test_script_rejects_unbounded_batch_sizes(self) -> None:
        self._add(timedelta(days=5000))
        for size in ("0", "10", "1000000"):
            code, _ = self._run("396", "--batch-size", size)
            self.assertEqual(code, 2)
        self.assertEqual(self._count(), 1)


if __name__ == "__main__":
    unittest.main()
