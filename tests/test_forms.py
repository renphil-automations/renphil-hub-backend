from __future__ import annotations

import asyncio
import json
import os
import uuid
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
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
from app.db_v2.models.form_details import FormDetailsV2  # noqa: E402
from app.db_v2.models.gridstack import GridstackV2  # noqa: E402
from app.db_v2.models.page_content import PageContentV2  # noqa: E402
from app.db_v2.models.tab import TabV2  # noqa: E402
from app.dependencies import get_current_user, get_edit_session, get_viewer_access  # noqa: E402
from app.main import app  # noqa: E402
from app.models.auth import UserInfo  # noqa: E402
from app.services import form_service, gridstack_service  # noqa: E402
from app.services.gridstack_service import _UNSET  # noqa: E402
from app.services.access_visibility_service import NodeNotEditableError  # noqa: E402

_SETTINGS = SimpleNamespace(FORM_BUILDER_BASE_URL="https://forms.example.org/", FORM_BUILDER_SOURCE_UI="hub")


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(coro):
    return asyncio.run(coro)


class FormBuilderProxyTests(unittest.TestCase):
    def _patch(self, handler, settings=_SETTINGS):
        return (
            patch.object(form_service, "get_http_client", lambda: _mock_client(handler)),
            patch.object(form_service, "get_settings", lambda: settings),
        )

    def test_list_forms_sends_pat_and_source_ui(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["pat"] = request.headers.get("x-pat")
            return httpx.Response(200, json={"forms": [{"id": "f1", "title": "Intake"}, {"id": 2}, {"title": "no id"}]})

        p1, p2 = self._patch(handler)
        with p1, p2:
            forms = _run(form_service.list_forms("  pat_123 "))

        self.assertEqual(seen["url"], "https://forms.example.org/api/forms?sourceUi=hub")
        self.assertEqual(seen["pat"], "pat_123")
        self.assertEqual(forms, [{"id": "f1", "title": "Intake"}, {"id": "2", "title": ""}])

    def test_missing_pat_and_unconfigured_base_url_are_422(self) -> None:
        p1, p2 = self._patch(lambda r: httpx.Response(200, json={}))
        with p1, p2:
            with self.assertRaises(form_service.FormBuilderError) as ctx:
                _run(form_service.list_forms("  "))
        self.assertEqual(ctx.exception.status_code, 422)

        unconfigured = SimpleNamespace(FORM_BUILDER_BASE_URL=None, FORM_BUILDER_SOURCE_UI="hub")
        p1, p2 = self._patch(lambda r: httpx.Response(200, json={}), unconfigured)
        with p1, p2:
            with self.assertRaises(form_service.FormBuilderError) as ctx:
                _run(form_service.list_forms("pat"))
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("FORM_BUILDER_BASE_URL", ctx.exception.detail)

    def test_upstream_status_mapping(self) -> None:
        cases = [(401, 422), (403, 422), (404, 422), (500, 502), (302, 502)]
        for upstream, expected in cases:
            p1, p2 = self._patch(lambda r, s=upstream: httpx.Response(s, json={}))
            with self.subTest(upstream=upstream), p1, p2:
                with self.assertRaises(form_service.FormBuilderError) as ctx:
                    _run(form_service.get_form_embed_info("pat", "f1"))
                self.assertEqual(ctx.exception.status_code, expected)

    def test_network_error_and_bad_json_are_502(self) -> None:
        def boom(request):
            raise httpx.ConnectError("nope", request=request)

        for handler in (boom, lambda r: httpx.Response(200, content=b"<html>")):
            p1, p2 = self._patch(handler)
            with p1, p2:
                with self.assertRaises(form_service.FormBuilderError) as ctx:
                    _run(form_service.list_forms("pat"))
                self.assertEqual(ctx.exception.status_code, 502)

    def test_embed_info_maps_fields_and_quotes_form_id(self) -> None:
        seen = {}

        def handler(request):
            seen["path"] = request.url.raw_path.decode()
            return httpx.Response(200, json={"id": "a/b", "embedUrl": "https://forms.example.org/e/1", "requiresAuthentication": True})

        p1, p2 = self._patch(handler)
        with p1, p2:
            info = _run(form_service.get_form_embed_info("pat", "a/b"))
        self.assertEqual(seen["path"], "/api/forms/a%2Fb?sourceUi=hub")
        self.assertEqual(info, {"id": "a/b", "embed_url": "https://forms.example.org/e/1", "requires_authentication": True})

    def test_embed_info_rejects_unavailable_form(self) -> None:
        for body in ({"id": "f1", "unavailable": True, "embedUrl": "https://x"}, {"id": "f1"}):
            p1, p2 = self._patch(lambda r, b=body: httpx.Response(200, json=b))
            with p1, p2:
                with self.assertRaises(form_service.FormBuilderError) as ctx:
                    _run(form_service.get_form_embed_info("pat", "f1"))
                self.assertEqual(ctx.exception.status_code, 422)


FORM_1 = "11111111-1111-4111-8111-111111111111"
FORM_2 = "22222222-2222-4222-8222-222222222222"


class _DbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        # SQLite only honours ON DELETE CASCADE with foreign keys switched on.
        event.listen(engine, "connect", lambda conn, _rec: conn.execute("PRAGMA foreign_keys=ON"))
        BaseV2.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        tab = TabV2(document_id="tab", title="Tab", order=0, access_control={}, locked=False, locked_by="")
        self.db.add(tab)
        self.db.flush()
        self.grid = GridstackV2(document_id="tab", name="Tab", settings={}, position=0, parent_id=None, parent_tab_id=tab.id)
        self.db.add(self.grid)
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def _details(self, link: str) -> FormDetailsV2:
        component = self.db.query(ComponentV2).filter(ComponentV2.link == link).one()
        return self.db.query(FormDetailsV2).filter(FormDetailsV2.component_id == component.id).one()


class SaveFormComponentServiceTests(_DbTestCase):
    def _save(self, **overrides):
        kwargs = dict(
            link=None,
            document_id="tab",
            form_id=FORM_1,
            embed_url="https://forms.example.org/e/1",
            requires_authentication=True,
            pat="pat_original",
        )
        kwargs.update(overrides)
        return form_service.save_form_component(self.db, **kwargs)

    def test_create_then_update_by_link(self) -> None:
        created = self._save(title="Intake", layout={"x": 2, "y": 3, "w": 4, "h": None})

        self.assertTrue(created["created"])
        view = created["component"]
        self.assertEqual(view["type"], "form")
        self.assertEqual(view["document_id"], "tab")
        self.assertEqual((view["x"], view["y"], view["w"], view["h"]), (2, 3, 4, 6))
        self.assertEqual(view["form"]["form_id"], FORM_1)
        self.assertEqual(
            view["form"],
            {"form_id": FORM_1, "embed_url": "https://forms.example.org/e/1", "requires_authentication": True},
        )
        self.assertEqual(created["search_updates"], [{"component_id": view["id"], "action": "upsert"}])

        row = self.db.query(ComponentV2).filter(ComponentV2.link == view["link"]).one()
        self.assertEqual(row.props, {})
        details = self._details(view["link"])
        self.assertEqual(details.form_id, uuid.UUID(FORM_1))
        self.assertEqual(details.pat, "pat_original")
        page = self.db.query(PageContentV2).filter(PageContentV2.id == row.page_content_id).one()
        self.assertEqual(page.content, {})

        # Update: title omitted (kept), PAT omitted (kept), spec replaced.
        updated = self._save(link=view["link"], document_id=None, form_id=FORM_2, requires_authentication=False, pat=_UNSET)

        self.assertFalse(updated["created"])
        self.assertEqual(updated["component"]["id"], view["id"])
        self.assertEqual(updated["component"]["title"], "Intake")
        self.db.expire_all()
        details = self._details(view["link"])
        self.assertEqual(details.form_id, uuid.UUID(FORM_2))
        self.assertFalse(details.requires_authentication)
        self.assertEqual(details.pat, "pat_original")
        self.assertEqual(self.db.query(ComponentV2).count(), 1)
        self.assertEqual(self.db.query(FormDetailsV2).count(), 1)

    def test_form_only_update_emits_no_search_update(self) -> None:
        link = self._save()["component"]["link"]
        again = self._save(link=link, embed_url="https://forms.example.org/e/2")
        self.assertEqual(again["search_updates"], [])
        self.assertEqual(again["component"]["form"]["embed_url"], "https://forms.example.org/e/2")

    def test_update_of_form_component_without_details_row_creates_it(self) -> None:
        bare = ComponentV2(link="bare", type="form", props={}, gridstack_id=self.grid.id)
        self.db.add(bare)
        self.db.commit()
        with self.assertRaises(ValueError):
            self._save(link="bare", pat=_UNSET)
        self.assertEqual(self.db.query(FormDetailsV2).count(), 0)
        result = self._save(link="bare")
        self.assertFalse(result["created"])
        self.assertEqual(self._details("bare").embed_url, "https://forms.example.org/e/1")
        self.assertEqual(self._details("bare").pat, "pat_original")

    def test_pat_required_on_create_and_kept_unless_sent(self) -> None:
        for missing in (_UNSET, None, "  "):
            with self.assertRaises(ValueError):
                self._save(pat=missing)
        self.assertEqual(self.db.query(ComponentV2).count(), 0)

        result = self._save()
        link = result["component"]["link"]
        self.assertNotIn("pat_original", json.dumps(result, default=str))

        for keep in (_UNSET, None, ""):
            self._save(link=link, pat=keep, embed_url="https://forms.example.org/e/9")
            self.db.expire_all()
            self.assertEqual(self._details(link).pat, "pat_original")

        self._save(link=link, pat="  pat_new ")
        self.db.expire_all()
        self.assertEqual(self._details(link).pat, "pat_new")

    def test_rejects_bad_input(self) -> None:
        with self.assertRaises(ValueError):
            self._save(form_id="not-a-uuid")
        other = ComponentV2(link="note", type="block_note", props={}, gridstack_id=self.grid.id)
        self.db.add(other)
        self.db.commit()
        with self.assertRaises(ValueError):
            self._save(link="note")
        with self.assertRaises(ValueError):
            self._save(document_id=None)
        self.assertEqual(self.db.query(ComponentV2).filter(ComponentV2.type == "form").count(), 0)
        self.assertEqual(self.db.query(FormDetailsV2).count(), 0)

    def test_unknown_link_creates_and_unknown_gridstack_returns_none(self) -> None:
        result = self._save(link="does-not-exist")
        self.assertTrue(result["created"])
        self.assertNotEqual(result["component"]["link"], "does-not-exist")
        self.assertIsNone(self._save(document_id="missing"))

    def test_deleting_component_cascades_to_form_details(self) -> None:
        link = self._save()["component"]["link"]
        self.db.query(ComponentV2).filter(ComponentV2.link == link).delete()
        self.db.commit()
        self.assertEqual(self.db.query(FormDetailsV2).count(), 0)

    def test_edit_gates_use_the_right_node(self) -> None:
        seen = []

        class _Access:
            def verdict(self, node):
                seen.append(node)
                return SimpleNamespace(view=True, edit=False)

        with self.assertRaises(NodeNotEditableError):
            self._save(access=_Access())
        self.assertEqual(seen, [("tab", self.grid.parent_tab_id)])

        link = self._save()["component"]["link"]
        row = self.db.query(ComponentV2).filter(ComponentV2.link == link).one()
        seen.clear()
        with self.assertRaises(NodeNotEditableError):
            self._save(link=link, access=_Access(), form_id=FORM_2)
        self.assertEqual(seen, [("component", row.id)])
        self.db.expire_all()
        self.assertEqual(self._details(link).form_id, uuid.UUID(FORM_1))


class FormsRouterTests(_DbTestCase):
    def setUp(self) -> None:
        super().setUp()
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(email="a@renphil.org")
        app.dependency_overrides[get_db_v2] = lambda: self.db
        app.dependency_overrides[get_viewer_access] = lambda: None
        app.dependency_overrides[get_edit_session] = lambda: None
        self.client = TestClient(app)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        super().tearDown()

    def test_requires_authentication(self) -> None:
        app.dependency_overrides.pop(get_current_user)
        self.assertEqual(self.client.get("/forms", headers={"X-Form-PAT": "p"}).status_code, 401)
        self.assertEqual(self.client.get("/forms/f1", headers={"X-Form-PAT": "p"}).status_code, 401)
        self.assertEqual(self.client.put("/forms/component", json={}).status_code, 401)

    def test_list_and_embed_info(self) -> None:
        seen = []

        def handler(request):
            seen.append((request.url.path, request.headers.get("x-pat")))
            if request.url.path == "/api/forms":
                return httpx.Response(200, json={"forms": [{"id": "f1", "title": "Intake"}]})
            if request.url.path == "/api/forms/f1":
                return httpx.Response(200, json={"id": "f1", "embedUrl": "https://forms.example.org/e/1"})
            return httpx.Response(401)

        headers = {"X-Form-PAT": "pat_header_value"}
        with patch.object(form_service, "get_http_client", lambda: _mock_client(handler)), patch.object(
            form_service, "get_settings", lambda: _SETTINGS
        ):
            listed = self.client.get("/forms", headers=headers)
            info = self.client.get("/forms/f1", headers=headers)
            rejected = self.client.get("/forms/nope", headers=headers)
            missing_pat = self.client.get("/forms")

        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json(), {"success": True, "forms": [{"id": "f1", "title": "Intake"}]})
        self.assertEqual(
            info.json(),
            {"success": True, "id": "f1", "embed_url": "https://forms.example.org/e/1", "requires_authentication": False},
        )
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(rejected.json()["detail"], "The form-builder service rejected the PAT.")
        self.assertEqual(missing_pat.status_code, 422)
        self.assertEqual(missing_pat.json()["detail"], "A form-builder PAT is required.")
        for resp in (listed, info, rejected, missing_pat):
            self.assertEqual(resp.headers.get("cache-control"), "no-store")
        self.assertEqual(
            seen,
            [("/api/forms", "pat_header_value"), ("/api/forms/f1", "pat_header_value"), ("/api/forms/nope", "pat_header_value")],
        )

    def test_old_post_routes_are_gone(self) -> None:
        self.assertEqual(self.client.post("/forms/list", json={"pat": "p"}).status_code, 405)
        self.assertEqual(self.client.post("/forms/embed_info", json={"pat": "p", "form_id": "f1"}).status_code, 405)
        self.assertEqual(self.client.post("/forms/rotate_webhook_token").status_code, 405)

    def test_save_round_trip(self) -> None:
        body = {
            "document_id": "tab",
            "title": "Intake",
            "form_id": FORM_1,
            "embed_url": "https://forms.example.org/e/1",
            "requires_authentication": True,
            "pat": "pat_secret_value",
        }
        resp = self.client.put("/forms/component", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertTrue(data["created"])
        self.assertNotIn("pat_secret_value", resp.text)
        self.assertNotIn("pat", data["component"]["form"])

        update = self.client.put(
            "/forms/component",
            json={"link": data["component"]["link"], "form_id": FORM_1, "embed_url": "https://forms.example.org/e/2"},
        )
        self.assertEqual(update.status_code, 200, update.text)
        self.assertFalse(update.json()["created"])
        self.assertEqual(update.json()["component"]["form"]["embed_url"], "https://forms.example.org/e/2")
        self.db.expire_all()
        self.assertEqual(self._details(data["component"]["link"]).pat, "pat_secret_value")

    def test_save_validation_errors(self) -> None:
        base = {"form_id": FORM_1, "embed_url": "https://forms.example.org/e/1", "pat": "p"}
        self.assertEqual(self.client.put("/forms/component", json=base).status_code, 400)
        no_pat = {k: v for k, v in base.items() if k != "pat"}
        missing_pat = self.client.put("/forms/component", json={**no_pat, "document_id": "tab"})
        self.assertEqual(missing_pat.status_code, 400)
        self.assertIn("pat is required", missing_pat.json()["detail"])
        self.assertEqual(self.client.put("/forms/component", json={**base, "document_id": "missing"}).status_code, 404)
        self.assertEqual(
            self.client.put("/forms/component", json={**base, "document_id": "tab", "embed_url": "javascript:alert(1)"}).status_code,
            422,
        )
        self.assertEqual(
            self.client.put("/forms/component", json={**base, "document_id": "tab", "form_id": "f1"}).status_code,
            422,
        )
        self.assertEqual(
            self.client.put("/forms/component", json={**base, "document_id": "tab", "type": "airtable"}).status_code,
            422,
        )
        for retired in ({"webhook_url": "https://hub.example.org/wh"}, {"webhook_token": "x"}):
            self.assertEqual(
                self.client.put("/forms/component", json={**base, "document_id": "tab", **retired}).status_code,
                422,
            )


class CanvasContentTests(_DbTestCase):
    """The canvas read/save know nothing about `form_details`."""

    def setUp(self) -> None:
        super().setUp()
        self.form_link = form_service.save_form_component(
            self.db,
            link=None,
            document_id="tab",
            form_id=FORM_1,
            embed_url="https://forms.example.org/e/1",
            requires_authentication=True,
            pat="pat_secret_value",
            title="Intake",
        )["component"]["link"]
        self.form = self.db.query(ComponentV2).filter(ComponentV2.link == self.form_link).one()

    def _content(self):
        return gridstack_service.get_tab_content_v2(self.db, "tab")["content"]

    def test_canvas_read_does_not_expose_form_details(self) -> None:
        content = self._content()
        entry = content["widgets"][str(self.form.id)]
        self.assertEqual(entry["type"], "form")
        self.assertEqual(entry["link"], self.form_link)
        self.assertNotIn("form", entry)
        dumped = json.dumps(content, default=str)
        self.assertNotIn("pat_secret_value", dumped)
        self.assertNotIn("forms.example.org", dumped)

    def test_canvas_save_round_trip_keeps_form_details(self) -> None:
        content = self._content()
        content["widgets"][str(self.form.id)]["title"] = "Renamed"
        gridstack_service.update_tab_content_v2(self.db, "tab", content)

        self.db.expire_all()
        details = self._details(self.form_link)
        self.assertEqual(details.embed_url, "https://forms.example.org/e/1")
        self.assertEqual(details.pat, "pat_secret_value")
        self.assertEqual(self.db.query(ComponentV2).filter(ComponentV2.link == self.form_link).one().title, "Renamed")


class _EmbedTestCase(_DbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.calls = []
        self.auth_status = 200
        self.auth_body = {
            "token": "eyJhbGciOi.payload.sig",
            "formId": FORM_1,
            "userEmail": "viewer@renphil.org",
            "requiresAuthentication": True,
            "expiresInSeconds": 3600,
            "maxUses": 100,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            return httpx.Response(self.auth_status, json=self.auth_body)

        self._patches = [
            patch.object(form_service, "get_http_client", lambda: _mock_client(handler)),
            patch.object(form_service, "get_settings", lambda: _SETTINGS),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        super().tearDown()

    def _form(self, *, requires_authentication: bool, embed_url: str = "https://forms.example.org/e/1?theme=dark%20blue") -> str:
        return form_service.save_form_component(
            self.db,
            link=None,
            document_id="tab",
            form_id=FORM_1,
            embed_url=embed_url,
            requires_authentication=requires_authentication,
            pat="pat_secret_value",
        )["component"]["link"]


class FormEmbedServiceTests(_EmbedTestCase):
    def _resolve(self, link, access=None):
        target = form_service.get_form_embed_target(self.db, link, access=access)
        return _run(form_service.resolve_form_embed(target, "viewer@renphil.org"))

    def test_public_form_returns_stored_url_without_calling_form_builder(self) -> None:
        link = self._form(requires_authentication=False)
        result = self._resolve(link)
        self.assertEqual(
            result,
            {
                "link": link,
                "form_id": FORM_1,
                "embed_url": "https://forms.example.org/e/1?theme=dark%20blue",
                "requires_authentication": False,
                "expires_in_seconds": None,
                "max_uses": None,
            },
        )
        self.assertEqual(self.calls, [])

    def test_authenticated_form_appends_token(self) -> None:
        link = self._form(requires_authentication=True)
        result = self._resolve(link)

        self.assertEqual(result["embed_url"], "https://forms.example.org/e/1?theme=dark%20blue&token=eyJhbGciOi.payload.sig")
        self.assertEqual((result["expires_in_seconds"], result["max_uses"]), (3600, 100))
        self.assertTrue(result["requires_authentication"])

        (request,) = self.calls
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), "https://forms.example.org/api/authenticate_with_pat")
        self.assertEqual(
            json.loads(request.content),
            {"pat": "pat_secret_value", "formId": FORM_1, "userEmail": "viewer@renphil.org", "sourceUi": "hub"},
        )

    def test_with_embed_token_handles_query_shapes(self) -> None:
        cases = {
            "https://f.example.org/e/1": "https://f.example.org/e/1?token=t.k-n_",
            "https://f.example.org/e/1?a=1": "https://f.example.org/e/1?a=1&token=t.k-n_",
            "https://f.example.org/e/1?token=old&a=1#top": "https://f.example.org/e/1?a=1&token=t.k-n_#top",
        }
        for url, expected in cases.items():
            self.assertEqual(form_service.with_embed_token(url, "t.k-n_"), expected)

    def test_form_builder_errors_are_mapped(self) -> None:
        link = self._form(requires_authentication=True)
        cases = [
            (400, {"error": "userEmail is required — this form identifies each respondent."}, 422, "userEmail is required"),
            (403, {"error": "This PAT is not granted access to this form"}, 422, "no longer has access"),
            (404, {"error": "Form not found"}, 422, "no longer exists"),
            (503, {"error": "busy"}, 503, "busy"),
            (500, {"error": "boom"}, 502, "couldn't complete"),
            (200, {"formId": FORM_1}, 502, "couldn't complete"),
        ]
        for status, body, expected_status, expected_detail in cases:
            self.auth_status, self.auth_body = status, body
            with self.subTest(status=status, body=body):
                with self.assertRaises(form_service.FormBuilderError) as ctx:
                    self._resolve(link)
                self.assertEqual(ctx.exception.status_code, expected_status)
                self.assertIn(expected_detail, ctx.exception.detail)

    def test_lookup_404s(self) -> None:
        link = self._form(requires_authentication=False)
        self.db.add_all([
            ComponentV2(link="note", type="block_note", props={}, gridstack_id=self.grid.id),
            ComponentV2(link="bare", type="form", props={}, gridstack_id=self.grid.id),
        ])
        self.db.commit()
        hidden = SimpleNamespace(is_granted=lambda node: False)
        for bad_link, access, detail in [
            ("missing", None, "Form component not found"),
            ("note", None, "Form component not found"),
            (link, hidden, "Form component not found"),
            ("bare", None, "hasn't been configured"),
        ]:
            with self.subTest(link=bad_link):
                with self.assertRaises(form_service.FormBuilderError) as ctx:
                    form_service.get_form_embed_target(self.db, bad_link, access=access)
                self.assertEqual(ctx.exception.status_code, 404)
                self.assertIn(detail, ctx.exception.detail)
        self.assertEqual(self.calls, [])


class FormEmbedRouterTests(_EmbedTestCase):
    def setUp(self) -> None:
        super().setUp()
        app.dependency_overrides[get_current_user] = lambda: UserInfo(email="viewer@renphil.org", name="Viewer")
        app.dependency_overrides[get_db_v2] = lambda: self.db
        app.dependency_overrides[get_viewer_access] = lambda: SimpleNamespace(is_granted=lambda node: True)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        super().tearDown()

    def test_authenticated_embed_uses_jwt_email_and_never_returns_pat(self) -> None:
        link = self._form(requires_authentication=True, embed_url="https://forms.example.org/e/1")
        resp = self.client.get(f"/forms/component/{link}/embed")

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            resp.json(),
            {
                "success": True,
                "link": link,
                "form_id": FORM_1,
                "embed_url": "https://forms.example.org/e/1?token=eyJhbGciOi.payload.sig",
                "requires_authentication": True,
                "expires_in_seconds": 3600,
                "max_uses": 100,
            },
        )
        self.assertEqual(resp.headers.get("cache-control"), "no-store")
        self.assertNotIn("pat_secret_value", resp.text)
        self.assertEqual(json.loads(self.calls[0].content)["userEmail"], "viewer@renphil.org")

    def test_errors(self) -> None:
        link = self._form(requires_authentication=True)
        self.auth_status, self.auth_body = 403, {"error": "This PAT is not granted access to this form"}
        forbidden = self.client.get(f"/forms/component/{link}/embed")
        self.assertEqual(forbidden.status_code, 422)
        self.assertEqual(forbidden.headers.get("cache-control"), "no-store")

        missing = self.client.get("/forms/component/missing/embed")
        self.assertEqual(missing.status_code, 404)

        app.dependency_overrides[get_viewer_access] = lambda: SimpleNamespace(is_granted=lambda node: False)
        self.assertEqual(self.client.get(f"/forms/component/{link}/embed").status_code, 404)

        app.dependency_overrides.pop(get_current_user)
        self.assertEqual(self.client.get(f"/forms/component/{link}/embed").status_code, 401)
