from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

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
from app.db_v2.models.tab import TabV2  # noqa: E402
from app.db_v2.models.thread import ThreadV2  # noqa: E402
from app.dependencies import ViewAsAccess, get_current_user, get_view_as_access  # noqa: E402
from app.main import app  # noqa: E402
from app.models.auth import UserInfo  # noqa: E402

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
ME = "me@renphil.org"
OTHER = "other@renphil.org"


class ThreadListAnonymousTests(unittest.TestCase):
    """One thread widget holding, per author, one thread of each status."""

    def setUp(self) -> None:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(engine, "connect", lambda conn, _rec: conn.execute("PRAGMA foreign_keys=ON"))
        BaseV2.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()

        tab = TabV2(document_id="tab", title="Tab", order=0, access_control={}, locked=False, locked_by="")
        self.db.add(tab)
        self.db.flush()
        grid = GridstackV2(document_id="tab", name="Tab", settings={}, position=0, parent_id=None, parent_tab_id=tab.id)
        self.db.add(grid)
        self.db.flush()
        component = ComponentV2(link="widget", title="Widget", type="thread", gridstack_id=grid.id)
        self.db.add(component)
        self.db.flush()

        minute = 0
        for author in (ME, OTHER):
            for status in ("approved", "pending", "rejected"):
                minute += 1
                self.db.add(
                    ThreadV2(
                        component_id=component.id, title=f"{author.split('@')[0]}-{status}", content="x",
                        author_email=author, mentions=[], status=status,
                        created_at=T0 + timedelta(minutes=minute),
                    )
                )
        self.db.commit()

        app.dependency_overrides[get_current_user] = lambda: UserInfo(email=ME, name="Me")
        app.dependency_overrides[get_db_v2] = lambda: self.db
        app.dependency_overrides[get_view_as_access] = lambda: ViewAsAccess(access=None, view_as=False)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.db.close()

    def _list(self, params=None) -> dict:
        response = self.client.get("/data/threads/component/widget", params=params or {})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _titles(self, params=None) -> set[str]:
        return {item["title"] for item in self._list(params)["items"]}

    @staticmethod
    def _own_counts(body: dict) -> tuple[int, int]:
        return body["pending_count"], body["declined_count"]

    def test_default_lists_approved_only_and_counts_own_pending_and_rejected(self) -> None:
        # 2026-11-13: the caller's own pending/rejected threads are no longer
        # listed; the widget shows them as banner counts instead.
        for params in (None, {"anonymous": "false"}):
            body = self._list(params)
            self.assertEqual({item["title"] for item in body["items"]}, {"me-approved", "other-approved"})
            self.assertEqual(self._own_counts(body), (1, 1))

    def test_anonymous_returns_approved_only(self) -> None:
        body = self._list({"anonymous": "true"})
        self.assertEqual({item["title"] for item in body["items"]}, {"me-approved", "other-approved"})
        self.assertEqual(self._own_counts(body), (0, 0))

    def test_anonymous_must_be_a_boolean(self) -> None:
        response = self.client.get("/data/threads/component/widget", params={"anonymous": "maybe"})
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
