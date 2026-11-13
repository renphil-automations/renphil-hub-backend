"""
Parity tests for `airtable_personalize.personalize_match` — the Python port
of `airtable_formulas.personalize_clause` used to filter cached Airtable
widget rows at serve time (plan_airtable_widget_caching_2026-08-06.md §6.4).

This is the single most important test in the whole caching feature per the
plan's own §9: the predicate decides who sees which rows of data described
as financial and medical. Every case here mirrors a case called out in the
plan or in `airtable_formulas.personalize_clause`'s own docstring.

── plan_ac_enforcement_closeout_2026-09-09.md §5/§9 additions ──────────────

`AirtableComponentAccessControlTests` below, added by that plan's step 3,
covers the read-time visibility fold now gating all six of
`app/routers/airtable.py`'s component-data routes: an ungranted caller 404s
(never 403 — §5.4, a deep link to a hidden node must not confirm the node
exists) on every one of the six; a granted caller succeeds; the body-`pat`
branch of the two `elif link:` sites (`/component/chart/preview`,
`/component/preview`) is unaffected, since it never resolves a component;
and a warm cache entry does not bypass the gate (the §5.3 ordering
constraint — the check must stay strictly above `@airtable_cache`/the row
cache, so a cache HIT is gated exactly like a miss). Same in-memory-SQLite
router-fixture pattern as `tests/test_airtable_widget_cache.py`'s
`_AirtableCacheFixture` (a real FastAPI TestClient over a real SQLite v2
schema, JWT minted locally) — kept self-contained here per this codebase's
convention (`test_threads.py`, `test_tab_locking.py`, `test_airtable_widget_
cache.py` each rebuild their own fixture rather than importing one
another's).
"""

from __future__ import annotations

import os
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest import mock

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.helpers.airtable_personalize import (
    personalize_match,
    resolve_personalize_gate,
)

# Same fallback-only env defaults as tests/test_airtable_widget_cache.py, so
# this file is independently runnable regardless of import order.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://validation:validation@localhost/validation",
)
os.environ.setdefault(
    "DATABASE_URL_V2",
    "postgresql+psycopg2://validation:validation@localhost/validation_v2",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-airtable-personalize-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2, get_db_v2  # noqa: E402
from app.db_v2.models.component import ComponentV2  # noqa: E402
from app.db_v2.models.gridstack import GridstackV2  # noqa: E402
from app.db_v2.models.hub import HubV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.db_v2.models.resource_grant import NODE_COLUMNS, ResourceGrantV2  # noqa: E402
from app.db_v2.models.role import RoleV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.db_v2.models.scope import ScopeV2  # noqa: E402
from app.db_v2.models.tab import TabV2  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.gridstack_service import (  # noqa: E402
    AIRTABLE_CHART_WIDGET_TYPE,
    AIRTABLE_METRIC_WIDGET_TYPE,
    AIRTABLE_WIDGET_TYPE,
    _write_component_data,
    create_tab_v2,
)
from app.services.nav_tab_service import create_nav_tab_v2, get_nav_tab_by_document_id  # noqa: E402

FAKE_URL = "https://airtable.com/appFAKE00000000AA/tblFAKE00000000BB/viwFAKE00000000CC"


def _make_records(n: int) -> list[dict]:
    return [{"id": f"rec{i:04d}", "fields": {"Name": f"Row {i}"}} for i in range(n)]

def _treat_example_com_as_org_domain(test: unittest.TestCase) -> None:
    """This module's @example.com personas stand in for signed-in staff.
    Since the delete-user re-creation fix (plan_admin_delete_user_2026-10-18.md
    §4) the lazy sign-in path only provisions ORG-DOMAIN emails, so
    example.com is made one here. Provisioning itself is tested in
    test_admin_delete_user.py."""
    # `get_settings()` builds a fresh Settings each call, so the env var is what to patch.
    patcher = mock.patch.dict(os.environ, {"ALLOWED_EMAIL_DOMAIN": "renphil.org,example.com"})
    patcher.start()
    test.addCleanup(patcher.stop)


class _FakeAirtableTableHandle:
    class _Urls:
        records = "fake://records"
        records_post = "fake://records/listRecords"

    urls = _Urls()


def _make_fake_api_class(records: list[dict]):
    """A trimmed drop-in for `pyairtable.Api`, covering only what these
    access-control tests need: a paginated record walk, plus a `.base()`
    that raises `RequestException` (an unscoped PAT) rather than being
    absent — `fetch_table_field_hints` catches THAT specific exception and
    degrades to "no type hints"; an absent `.base` instead raises a bare
    `AttributeError`, which is not what that degrade path expects and
    surfaces as a 500 unrelated to anything these tests are actually about.
    Same shape as `test_airtable_widget_cache.py`'s `make_fake_api_class`
    (its own default, `schema_fields=None`), trimmed to this file's needs.
    """

    class _FakeApi:
        def __init__(self, api_key: str, *, retry_strategy: object = None) -> None:
            self.api_key = api_key

        def table(self, base_id: str, table_id: str) -> _FakeAirtableTableHandle:
            return _FakeAirtableTableHandle()

        def base(self, base_id: str):
            from requests.exceptions import RequestException

            raise RequestException("PAT lacks schema.bases:read scope")

        def request(
            self, method: str, url: str, fallback: tuple | None = None, options: dict | None = None
        ) -> dict:
            options = options or {}
            page_size = options.get("page_size", 100)
            offset = options.get("offset")
            start = int(offset) if offset else 0
            page = records[start : start + page_size]
            out = [{"id": r["id"], "fields": dict(r.get("fields", {}))} for r in page]
            next_offset = str(start + page_size) if start + page_size < len(records) else None
            return {"records": out, "offset": next_offset}

    return _FakeApi


class _FakeCacheService:
    """In-memory stand-in for `CacheService` — only the surface
    `AirtableService` actually calls, same shape as `test_airtable_widget_
    cache.py`'s `FakeCacheService`, trimmed to what these tests need."""

    def __init__(self) -> None:
        self.store: dict[str, object] = {}
        self.enabled = True
        # Real `CacheService.version` — the base-lock key `AirtableService`
        # builds reads this attribute directly (not just via `build_key`).
        self.version = "v1"

    def build_key(self, endpoint_name: str, params: dict) -> str:
        from app.services.cache_service import build_key

        return build_key(endpoint_name, params, version="v1")

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value, *, ttl_seconds=None, compress: bool = False) -> None:
        self.store[key] = value

    async def acquire_lock(self, key: str, *, ttl_seconds: int = 120) -> str | None:
        return f"token-{key}"

    async def release_lock(self, key: str, token: str) -> None:
        return None


class _AirtableAccessControlFixture(unittest.TestCase):
    """In-memory SQLite v2 schema, a TestClient, JWT minting, an
    Airtable-widget seeder, and the `ResourceGrantV2` grant helper — same
    shape as `test_threads.py`'s `_ThreadFixture` (step 2 of this arc) and
    `test_airtable_widget_cache.py`'s `_AirtableCacheFixture`."""

    def setUp(self) -> None:
        _treat_example_com_as_org_domain(self)
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        BaseV2.metadata.create_all(self.engine)
        self._session_factory = sessionmaker(bind=self.engine)
        self.db = self._session_factory()

        self.hub = HubV2(document_id="hub-doc")
        self.db.add(self.hub)
        self.db.commit()

        app = create_app()
        app.dependency_overrides[get_db_v2] = lambda: (yield self._session_factory())
        self.client = TestClient(app, raise_server_exceptions=False)

        nav = create_nav_tab_v2(self.db, title="Nav AC")
        nav_row = get_nav_tab_by_document_id(self.db, nav["documentId"])
        self.root = create_tab_v2(self.db, title="Root AC", nav_tab_id=nav_row.id)
        root_row = self.db.query(TabV2).filter(TabV2.document_id == self.root["documentId"]).first()
        self.root_gridstack = (
            self.db.query(GridstackV2)
            .filter(GridstackV2.parent_tab_id == root_row.id, GridstackV2.parent_id.is_(None))
            .first()
        )

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    # -- auth ---------------------------------------------------------------

    def _token(self, email: str, roles: list[str] | None = None) -> str:
        expire = datetime.now(timezone.utc) + timedelta(minutes=30)
        return jwt.encode(
            {"sub": email, "name": email.split("@")[0], "roles": roles or [], "exp": expire},
            os.environ["JWT_SECRET_KEY"],
            algorithm=os.environ["JWT_ALGORITHM"],
        )

    def _auth(self, email: str, roles: list[str] | None = None) -> dict:
        return {"Authorization": f"Bearer {self._token(email, roles)}"}

    # -- resource grants (D5 — a direct per-user grant) ----------------------

    def _seed_hub_user(self, email: str) -> UserV2:
        normalized = email.strip().lower()
        existing = self.db.query(UserV2).filter(UserV2.email == normalized).first()
        if existing is not None:
            return existing
        row = UserV2(email=normalized, has_signed_in=True, created_at=datetime.now(timezone.utc))
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def _grant(self, *, email: str, node: tuple[str, int], level: str = "view") -> None:
        user_row = self._seed_hub_user(email)
        node_kind, node_id = node
        self.db.add(
            ResourceGrantV2(
                **{NODE_COLUMNS[node_kind]: node_id},
                user_id=user_row.id,
                level=level,
                created_at=datetime.now(timezone.utc),
            )
        )
        self.db.commit()

    # -- seeder ---------------------------------------------------------------

    def _seed_widget(
        self,
        link: str,
        *,
        widget_type: str = AIRTABLE_WIDGET_TYPE,
        extra_data: dict | None = None,
    ) -> ComponentV2:
        component = ComponentV2(
            link=link, title=link, type=widget_type, gridstack_id=self.root_gridstack.id
        )
        self.db.add(component)
        self.db.commit()
        self.db.refresh(component)
        _write_component_data(
            self.db,
            component,
            {
                "sourceUrl": FAKE_URL,
                "pat": "fake-pat",
                "selectedColumns": [],
                "filters": [],
                "personalizeEnabled": False,
                "personalizeColumn": None,
                **(extra_data or {}),
            },
        )
        self.db.commit()
        self.db.refresh(component)
        return component

    @contextmanager
    def _patched(self, n: int = 3, *, cache: "_FakeCacheService | None" = None):
        """One combined context manager: a fake Airtable API with `n`
        records, and a cache (fresh unless `cache` is passed in, so a caller
        can warm one and reuse it for a second request)."""
        with mock.patch(
            "app.services.airtable_service.Api", _make_fake_api_class(_make_records(n))
        ), mock.patch(
            "app.services.airtable_service.get_cache_service",
            return_value=cache if cache is not None else _FakeCacheService(),
        ):
            yield

    # -- query counting (the closures-sharing regression) -------------------

    def _count_table_reads(self, *tables: str):
        """Install a cursor-level listener that tallies SELECTs per table
        name, matching `test_airtable_widget_cache.py`'s own
        `_count_row_reads`. Returns `(counts, detach)`; call `detach()` when
        finished."""
        from sqlalchemy import event

        counts: dict[str, int] = {t: 0 for t in tables}

        def _on_execute(_conn, _cursor, statement, _params, _ctx, _many):
            head = statement.lstrip()[:6].upper()
            if head != "SELECT":
                return
            for table in counts:
                if f" {table}" in statement or f'"{table}"' in statement:
                    counts[table] += 1

        event.listen(self.engine, "before_cursor_execute", _on_execute)
        return counts, lambda: event.remove(self.engine, "before_cursor_execute", _on_execute)


class PersonalizeMatchParityTests(unittest.TestCase):
    # ── the case that matters most: no unanchored substring leakage ──────
    def test_amy_does_not_match_tamy(self) -> None:
        # The exact failure the delimiter-bounded formula was written to
        # fix (airtable_formulas.py's own docstring): an unanchored
        # substring match would let amy@ see tamy@'s rows.
        self.assertFalse(personalize_match("tamy@renphil.org", "amy@renphil.org"))
        self.assertTrue(personalize_match("tamy@renphil.org", "tamy@renphil.org"))
        self.assertTrue(personalize_match("amy@renphil.org", "amy@renphil.org"))

    def test_single_address(self) -> None:
        self.assertTrue(personalize_match("amy@renphil.org", "amy@renphil.org"))
        self.assertFalse(personalize_match("amy@renphil.org", "bob@renphil.org"))

    def test_semicolon_delimited_list(self) -> None:
        value = "amy@renphil.org; bob@renphil.org; cara@renphil.org"
        self.assertTrue(personalize_match(value, "amy@renphil.org"))
        self.assertTrue(personalize_match(value, "bob@renphil.org"))
        self.assertTrue(personalize_match(value, "cara@renphil.org"))
        self.assertFalse(personalize_match(value, "dan@renphil.org"))

    def test_pipe_delimited_list(self) -> None:
        value = "amy@renphil.org|bob@renphil.org"
        self.assertTrue(personalize_match(value, "amy@renphil.org"))
        self.assertTrue(personalize_match(value, "bob@renphil.org"))
        self.assertFalse(personalize_match(value, "am@renphil.org"))

    def test_mixed_delimiters(self) -> None:
        value = "amy@renphil.org; bob@renphil.org|cara@renphil.org"
        for email in ("amy@renphil.org", "bob@renphil.org", "cara@renphil.org"):
            self.assertTrue(personalize_match(value, email), email)

    def test_array_value(self) -> None:
        # Multi-select / linked-record / collaborator fields arrive as a
        # Python list, not a delimited string.
        value = ["amy@renphil.org", "bob@renphil.org"]
        self.assertTrue(personalize_match(value, "amy@renphil.org"))
        self.assertTrue(personalize_match(value, "bob@renphil.org"))
        self.assertFalse(personalize_match(value, "cara@renphil.org"))

    def test_case_insensitive(self) -> None:
        self.assertTrue(personalize_match("Amy@RenPhil.org", "amy@renphil.org"))
        self.assertTrue(personalize_match("amy@renphil.org", "AMY@RENPHIL.ORG"))

    def test_surrounding_and_internal_spaces_ignored(self) -> None:
        self.assertTrue(personalize_match("  amy@renphil.org  ", "amy@renphil.org"))
        self.assertTrue(
            personalize_match("amy@renphil.org ; bob@renphil.org", "bob@renphil.org")
        )

    def test_comma_delimited_list(self) -> None:
        value = "amy@renphil.org, bob@renphil.org"
        self.assertTrue(personalize_match(value, "amy@renphil.org"))
        self.assertTrue(personalize_match(value, "bob@renphil.org"))

    # ── landmine L4: newline separators must NOT be supported ────────────
    def test_newline_separated_list_matches_nothing(self) -> None:
        # A column holding one address per line has no comma/semicolon/pipe
        # between entries, so the whole cell normalizes to one long token
        # that cannot equal any single email — it must match nothing, not
        # "helpfully" split on newlines. Porting in newline support would
        # silently widen access (plan landmine L4).
        value = "amy@renphil.org\nbob@renphil.org\ncara@renphil.org"
        for email in ("amy@renphil.org", "bob@renphil.org", "cara@renphil.org"):
            self.assertFalse(personalize_match(value, email), email)

    # ── empty / falsy inputs ──────────────────────────────────────────────
    def test_empty_value_never_matches(self) -> None:
        self.assertFalse(personalize_match(None, "amy@renphil.org"))
        self.assertFalse(personalize_match("", "amy@renphil.org"))
        self.assertFalse(personalize_match([], "amy@renphil.org"))

    def test_empty_email_never_matches(self) -> None:
        self.assertFalse(personalize_match("amy@renphil.org", ""))
        self.assertFalse(personalize_match("amy@renphil.org", "   "))


class ResolvePersonalizeGateTests(unittest.TestCase):
    def test_disabled_never_blocks(self) -> None:
        self.assertFalse(
            resolve_personalize_gate(
                personalize_enabled=False, personalize_column=None, email=None
            )
        )

    def test_enabled_with_column_and_email_is_not_blocked(self) -> None:
        self.assertFalse(
            resolve_personalize_gate(
                personalize_enabled=True,
                personalize_column="Owner Email",
                email="amy@renphil.org",
            )
        )

    def test_enabled_without_column_is_blocked(self) -> None:
        self.assertTrue(
            resolve_personalize_gate(
                personalize_enabled=True, personalize_column=None, email="amy@renphil.org"
            )
        )
        self.assertTrue(
            resolve_personalize_gate(
                personalize_enabled=True, personalize_column="  ", email="amy@renphil.org"
            )
        )

    def test_enabled_without_email_is_blocked(self) -> None:
        self.assertTrue(
            resolve_personalize_gate(
                personalize_enabled=True, personalize_column="Owner Email", email=None
            )
        )
        self.assertTrue(
            resolve_personalize_gate(
                personalize_enabled=True, personalize_column="Owner Email", email="   "
            )
        )


# =====================================================================
# plan_ac_enforcement_closeout_2026-09-09.md §5/§9 — access control on the
# six component-data routes in app/routers/airtable.py
# =====================================================================


class AirtableComponentAccessControlTests(_AirtableAccessControlFixture):
    """All six sites 404 for an ungranted caller (never 403 — §5.4); a
    granted caller succeeds; the body-`pat` branch of the two `elif link:`
    sites is unaffected; a cache hit is still gated (§5.3's ordering
    constraint, which is the regression most likely to be introduced by
    getting the dependency placement wrong)."""

    UNGRANTED = "outsider@example.com"
    GRANTED = "allowed@example.com"

    # -- ungranted caller: 404, never 403, on every one of the six ---------

    def test_rows_404s_for_an_ungranted_caller(self) -> None:
        self._seed_widget("ac-rows-widget")
        with self._patched():
            resp = self.client.get(
                "/data/airtable/component/ac-rows-widget/rows",
                headers=self._auth(self.UNGRANTED),
            )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_metric_404s_for_an_ungranted_caller(self) -> None:
        self._seed_widget(
            "ac-metric-widget", widget_type=AIRTABLE_METRIC_WIDGET_TYPE,
            extra_data={"aggregation": "count"},
        )
        with self._patched():
            resp = self.client.get(
                "/data/airtable/component/ac-metric-widget/metric",
                headers=self._auth(self.UNGRANTED),
            )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_chart_404s_for_an_ungranted_caller(self) -> None:
        self._seed_widget(
            "ac-chart-widget",
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            extra_data={"groupField": "Name", "aggregation": "count"},
        )
        with self._patched():
            resp = self.client.get(
                "/data/airtable/component/ac-chart-widget/chart",
                headers=self._auth(self.UNGRANTED),
            )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_rows_full_404s_for_an_ungranted_caller(self) -> None:
        self._seed_widget("ac-rows-full-widget", extra_data={"viewerControlsEnabled": True})
        with self._patched():
            resp = self.client.get(
                "/data/airtable/component/ac-rows-full-widget/rows/full",
                headers=self._auth(self.UNGRANTED),
            )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_chart_preview_link_branch_404s_for_an_ungranted_caller(self) -> None:
        self._seed_widget(
            "ac-chart-preview-widget",
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            extra_data={"groupField": "Name", "aggregation": "count"},
        )
        with self._patched():
            resp = self.client.post(
                "/data/airtable/component/chart/preview",
                json={"link": "ac-chart-preview-widget", "groupField": "Name",
                      "aggregation": "count"},
                headers=self._auth(self.UNGRANTED),
            )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_preview_link_branch_404s_for_an_ungranted_caller(self) -> None:
        self._seed_widget("ac-preview-widget")
        with self._patched():
            resp = self.client.post(
                "/data/airtable/component/preview",
                json={"link": "ac-preview-widget"},
                headers=self._auth(self.UNGRANTED),
            )
        self.assertEqual(resp.status_code, 404, resp.text)

    # -- granted caller succeeds on every one of the six --------------------

    def test_granted_caller_succeeds_on_all_six_sites(self) -> None:
        rows_widget = self._seed_widget("ac-rows-widget-2")
        metric_widget = self._seed_widget(
            "ac-metric-widget-2", widget_type=AIRTABLE_METRIC_WIDGET_TYPE,
            extra_data={"aggregation": "count"},
        )
        chart_widget = self._seed_widget(
            "ac-chart-widget-2",
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            extra_data={"groupField": "Name", "aggregation": "count"},
        )
        full_rows_widget = self._seed_widget(
            "ac-rows-full-widget-2", extra_data={"viewerControlsEnabled": True}
        )
        for widget in (rows_widget, metric_widget, chart_widget, full_rows_widget):
            self._grant(email=self.GRANTED, node=("component", widget.id))

        with self._patched():
            self.assertEqual(
                self.client.get(
                    "/data/airtable/component/ac-rows-widget-2/rows",
                    headers=self._auth(self.GRANTED),
                ).status_code,
                200,
            )
        with self._patched():
            self.assertEqual(
                self.client.get(
                    "/data/airtable/component/ac-metric-widget-2/metric",
                    headers=self._auth(self.GRANTED),
                ).status_code,
                200,
            )
        with self._patched():
            self.assertEqual(
                self.client.get(
                    "/data/airtable/component/ac-chart-widget-2/chart",
                    headers=self._auth(self.GRANTED),
                ).status_code,
                200,
            )
        with self._patched():
            self.assertEqual(
                self.client.get(
                    "/data/airtable/component/ac-rows-full-widget-2/rows/full",
                    headers=self._auth(self.GRANTED),
                ).status_code,
                200,
            )
        with self._patched():
            self.assertEqual(
                self.client.post(
                    "/data/airtable/component/chart/preview",
                    json={"link": "ac-chart-widget-2", "groupField": "Name",
                          "aggregation": "count"},
                    headers=self._auth(self.GRANTED),
                ).status_code,
                200,
            )
        with self._patched():
            self.assertEqual(
                self.client.post(
                    "/data/airtable/component/preview",
                    json={"link": "ac-rows-widget-2"},
                    headers=self._auth(self.GRANTED),
                ).status_code,
                200,
            )

    # -- the body-pat branch of the two `elif link:` sites is unaffected ----

    def test_chart_preview_body_pat_branch_is_unaffected_by_the_gate(self) -> None:
        """No `link` in the body — the caller supplied their own credential,
        so no component is resolved and the §5 gate never runs."""
        with self._patched():
            resp = self.client.post(
                "/data/airtable/component/chart/preview",
                json={
                    "sourceUrl": FAKE_URL,
                    "pat": "own-pat",
                    "groupField": "Name",
                    "aggregation": "count",
                },
                headers=self._auth(self.UNGRANTED),
            )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_preview_body_pat_branch_is_unaffected_by_the_gate(self) -> None:
        with self._patched():
            resp = self.client.post(
                "/data/airtable/component/preview",
                json={"sourceUrl": FAKE_URL, "pat": "own-pat"},
                headers=self._auth(self.UNGRANTED),
            )
        self.assertEqual(resp.status_code, 200, resp.text)

    # -- a cache hit is still gated (§5.3's ordering constraint) ------------

    def test_a_warm_cache_hit_is_still_gated(self) -> None:
        """The regression this step is most likely to introduce if the
        access check is wired below `@airtable_cache`/the row cache instead
        of above it (§5.3's own explicit warning) — also covered, for the
        original pre-existing cache-bypass regression test this property
        extends, in `tests/test_airtable_widget_cache.py`'s
        `test_authorization_is_not_bypassed_by_a_warm_cache`."""
        widget = self._seed_widget("ac-warm-widget")
        self._grant(email=self.GRANTED, node=("component", widget.id))
        shared_cache = _FakeCacheService()

        with self._patched(cache=shared_cache):
            first = self.client.get(
                "/data/airtable/component/ac-warm-widget/rows",
                headers=self._auth(self.GRANTED),
            )
            self.assertEqual(first.status_code, 200, first.text)
            self.assertTrue(shared_cache.store, "first request should have warmed the cache")

            second = self.client.get(
                "/data/airtable/component/ac-warm-widget/rows",
                headers=self._auth(self.UNGRANTED),
            )
            self.assertEqual(
                second.status_code,
                404,
                "an ungranted caller must be refused even though the cache is warm",
            )

    def test_closures_are_built_at_most_once_for_a_granted_non_admin_caller(self) -> None:
        """`_resolve_airtable_bundle_and_access` shares ONE `RbacClosures`
        snapshot between `is_hub_admin`'s branch-2 check and
        `granted_single_node`'s own walk, via `_LazyRbacClosures` — the
        second-session-follow-up fix for the double-build the first
        implementation had (bare `closures=None` passed to each
        independently).

        A D5 direct per-user grant (`_grant`'s default, used by every other
        test in this class) does NOT exercise this at all: `held_closures`
        never calls `.role_descendants(...)`/`.scope_descendants(...)` for
        a caller with zero `role_assignments` rows, so the underlying
        `RbacClosures` is never built either way. This test therefore uses
        a REAL role/scope assignment — the only shape that actually reaches
        the shared code path.

        It ALSO needs a `hub_admin`-keyed role and a universal scope to
        exist at all: `is_hub_admin`'s branch 2 (`_hub_admin_target_ids`)
        short-circuits to `False` with ZERO further queries — never
        reaching `effective_pairs`/`RbacClosures` at all — the moment
        EITHER is missing, which a first draft of this test missed
        entirely: it passed for the wrong reason (nothing built `RbacClosures`
        twice because nothing built it even once via that branch), caught
        only by deliberately reverting the fix and confirming the test
        still passed when it should have failed."""
        hub_admin_role = RoleV2(
            key="hub_admin", name="Hub Admin", created_at=datetime.now(timezone.utc)
        )
        role = RoleV2(key="ac-test-role", name="AC Test Role", created_at=datetime.now(timezone.utc))
        # Doubles as the universal scope `_hub_admin_target_ids` needs (at
        # most one row may ever be universal) AND the scope this caller's
        # own assignment/grant use — `ac-test-role` has no DAG edge to
        # `hub_admin`, so holding it does not make the caller admin.
        scope = ScopeV2(
            key="ac-test-scope", name="AC Test Scope", is_universal=True,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add_all([hub_admin_role, role, scope])
        self.db.commit()

        widget = self._seed_widget("ac-closures-widget")
        widget_kind, widget_id = "component", widget.id
        self.db.add(
            ResourceGrantV2(
                **{NODE_COLUMNS[widget_kind]: widget_id},
                role_id=role.id,
                scope_id=scope.id,
                level="view",
                created_at=datetime.now(timezone.utc),
            )
        )
        self.db.commit()

        caller = "assigned@example.com"
        hub_user = self._seed_hub_user(caller)
        self.db.add(
            RoleAssignmentV2(
                user_id=hub_user.id, role_id=role.id, scope_id=scope.id,
                created_at=datetime.now(timezone.utc),
            )
        )
        self.db.commit()

        # `parent_child_roles`/`parent_child_scopes` are the precise signal:
        # `_role_edge_pairs`/`_scope_edge_pairs` are private helpers called
        # ONLY from `RbacClosures.__init__` (confirmed by grep), nowhere
        # else — unlike `roles`/`scopes` themselves, which `is_hub_admin`'s
        # OWN `_hub_admin_target_ids` lookup also legitimately touches once,
        # independent of closures entirely, so counting those would conflate
        # two different queries.
        counts, detach = self._count_table_reads("parent_child_roles", "parent_child_scopes")
        try:
            with self._patched():
                resp = self.client.get(
                    "/data/airtable/component/ac-closures-widget/rows",
                    headers=self._auth(caller),  # non-admin JWT: no roles claim
                )
        finally:
            detach()

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            counts,
            {"parent_child_roles": 1, "parent_child_scopes": 1},
            f"RbacClosures should be built exactly once for this request, got {counts}",
        )


if __name__ == "__main__":
    unittest.main()
