"""
Step 4 regression test — GET /data/airtable/component/{link}/rows.

Runs the REAL endpoint through FastAPI's TestClient with three overrides so
nothing touches the production DB or the Airtable network:
  * get_db_v2        -> in-memory SQLite
  * get_current_user -> a stub identity per test
  * get_airtable_service -> a recorder that captures the arguments

Section B drops the recorder and exercises the real service with only the
HTTP layer patched, so formula construction and the fail-closed path are
covered end to end.

Run:  PYTHONPATH=. ./.venv/Scripts/python.exe <this file>
"""
from __future__ import annotations

import sys
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _jsonb_as_json(element, compiler, **kw):  # noqa: ANN001, ANN201
    return "JSON"


from app.db_v2.database import BaseV2, get_db_v2  # noqa: E402
from app.db_v2.models.tab import TabV2  # noqa: E402
from app.db_v2.models.gridstack import GridstackV2  # noqa: E402
from app.db_v2.models.component import ComponentV2  # noqa: E402
from app.db_v2.models.page_content import PageContentV2  # noqa: E402

from app.dependencies import get_airtable_service, get_current_user  # noqa: E402
from app.models.auth import UserInfo  # noqa: E402
from app.services import gridstack_service as gs  # noqa: E402
from app.main import app  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

PASS, FAIL = 0, 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}" + (f"\n          {detail}" if detail else ""))


# ── fixtures ───────────────────────────────────────────────────────────
# StaticPool + check_same_thread=False: TestClient dispatches the request on
# a worker thread, and a default in-memory SQLite connection is bound to the
# thread that created it (and would be a DIFFERENT empty database anyway).
engine = create_engine(
    "sqlite://",
    future=True,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
BaseV2.metadata.create_all(engine)
Session = sessionmaker(bind=engine, future=True)
db = Session()

tab = TabV2(title="T", document_id="doc-1", order=0)
db.add(tab)
db.flush()
db.add(GridstackV2(document_id="doc-1", name="T", parent_id=None, parent_tab_id=tab.id))
db.commit()

gs.update_tab_content_v2(db, "doc-1", {
    "schemaVersion": 2,
    "layout": [{"id": "w1", "x": 0, "y": 0, "w": 6, "h": 6, "type": "airtable"}],
    "widgets": {"w1": {"type": "airtable", "data": {
        "selectedColumns": ["Name", "Amount"],
        "filters": [{"field": "Status", "operator": "eq", "value": "Active"}],
    }}},
})
comp = (
    db.query(ComponentV2)
    .filter(ComponentV2.type == "airtable")
    .first()
)
LINK = comp.link
gs.update_airtable_component_config(
    db, LINK,
    source_url="https://airtable.com/appAAAAAAAAAAAAAA/tblBBBBBBBBBBBBBB",
    pat="patSECRET.token",
    personalize_enabled=True,
    personalize_column="Owner",
)


class Recorder:
    """Stands in for AirtableService; captures what the endpoint passed."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def fetch_widget_rows(self, **kwargs):
        from app.models.airtable import AirtableWidgetRowsResponse
        self.calls.append(kwargs)
        return AirtableWidgetRowsResponse(
            base_id="appAAAAAAAAAAAAAA", table_id="tblBBBBBBBBBBBBBB",
            fields=["Name"], rows=[{"id": "rec1", "Name": "x"}],
            next_cursor="CURSOR2",
        )


recorder = Recorder()
CURRENT: dict[str, Any] = {}

app.dependency_overrides[get_db_v2] = lambda: db
app.dependency_overrides[get_airtable_service] = lambda: recorder
app.dependency_overrides[get_current_user] = lambda: UserInfo(
    email=CURRENT.get("email", "viewer@renphil.org"),
    name="Test", roles=CURRENT.get("roles", []),
)

client = TestClient(app)
URL = f"/data/airtable/component/{LINK}/rows"


def as_user(email: str, roles: list[str] | None = None) -> None:
    CURRENT["email"] = email
    CURRENT["roles"] = roles or []


def main() -> int:
    # ── A. Endpoint behaviour ──────────────────────────────────────────
    print("\nA1. Authentication")
    app.dependency_overrides.pop(get_current_user)
    check("401 without a token", client.get(URL).status_code == 401)
    app.dependency_overrides[get_current_user] = lambda: UserInfo(
        email=CURRENT.get("email", "viewer@renphil.org"),
        name="Test", roles=CURRENT.get("roles", []),
    )

    print("\nA2. Happy path, no widget-level AC")
    as_user("viewer@renphil.org")
    recorder.calls.clear()
    r = client.get(URL)
    check("200", r.status_code == 200, r.text[:200])
    body = r.json()
    check("returns rows", body["rows"] == [{"id": "rec1", "Name": "x"}])
    check("returns next_cursor", body["next_cursor"] == "CURSOR2")
    call = recorder.calls[0]
    check("service got the CALLER's email", call["caller_email"] == "viewer@renphil.org")
    check("service got the stored URL",
          call["url"] == "https://airtable.com/appAAAAAAAAAAAAAA/tblBBBBBBBBBBBBBB")
    check("service got the stored PAT", call["api_key"] == "patSECRET.token")
    check("service got stored selectedColumns", call["selected_columns"] == ["Name", "Amount"])
    check("service got stored filters",
          call["filters"] == [{"field": "Status", "operator": "eq", "value": "Active"}])
    check("service got personalize settings",
          call["personalize_enabled"] is True and call["personalize_column"] == "Owner")

    print("\nA3. Response never carries the token")
    check("no pat in the payload", "patSECRET" not in r.text, r.text[:200])

    print("\nA4. Client cannot inject query parameters")
    recorder.calls.clear()
    r = client.get(URL, params={
        "formula": "TRUE()",
        "fields": "Salary",
        "url": "https://airtable.com/appEVIL0000000000/tblEVIL0000000000",
        "personalize_enabled": "false",
        "caller_email": "someone.else@renphil.org",
    })
    check("200 (extra params ignored)", r.status_code == 200)
    call = recorder.calls[0]
    check("injected url ignored",
          call["url"] == "https://airtable.com/appAAAAAAAAAAAAAA/tblBBBBBBBBBBBBBB")
    check("injected fields ignored", call["selected_columns"] == ["Name", "Amount"])
    check("injected email ignored", call["caller_email"] == "viewer@renphil.org")
    check("personalize cannot be turned off", call["personalize_enabled"] is True)
    check("no formula reaches the service", "formula" not in call)

    print("\nA5. Cursor is passed through")
    recorder.calls.clear()
    client.get(URL, params={"cursor": "PAGE2"})
    check("cursor forwarded", recorder.calls[0]["cursor"] == "PAGE2")

    print("\nA6. Access control")
    gs.update_airtable_component_config(db, LINK, access_control={
        "viewers": {"users": [{"email": "allowed@renphil.org"}],
                    "roles": [{"name": "Finance", "scope": "Hub"}]}})

    as_user("nobody@renphil.org")
    check("403 for an unlisted user", client.get(URL).status_code == 403)

    as_user("allowed@renphil.org")
    check("200 for a listed user", client.get(URL).status_code == 200)

    as_user("nobody@renphil.org", ["Finance"])
    check("200 for a listed role", client.get(URL).status_code == 200)

    as_user("nobody@renphil.org", ["Hub Admin"])
    check("200 for Hub Admin (bypass)", client.get(URL).status_code == 200)

    as_user("ALLOWED@RenPhil.org")
    check("email match is case-insensitive", client.get(URL).status_code == 200)

    print("\nA7. Misconfiguration")
    as_user("allowed@renphil.org", ["Hub Admin"])
    check("404 for an unknown link",
          client.get("/data/airtable/component/nosuchlink/rows").status_code == 404)

    gs.update_airtable_component_config(db, LINK, pat=None)
    check("400 when no PAT is configured", client.get(URL).status_code == 400)
    gs.update_airtable_component_config(db, LINK, pat="patSECRET.token")

    gs.update_airtable_component_config(db, LINK, source_url="")
    check("400 when no source URL is configured", client.get(URL).status_code == 400)
    gs.update_airtable_component_config(
        db, LINK, source_url="https://airtable.com/appAAAAAAAAAAAAAA/tblBBBBBBBBBBBBBB")

    # ── B. Real service, HTTP layer patched ────────────────────────────
    print("\nB. Real service (only the HTTP call is faked)")
    import asyncio
    from app.config import get_settings
    from app.services.airtable_service import AirtableService
    from pyairtable import Api

    sent: dict[str, Any] = {}

    def fake_request(self, method, url, **kwargs):
        sent["method"] = method
        sent["url"] = url
        sent["options"] = kwargs.get("options")
        return {"records": [{"id": "rec1", "fields": {"Name": "n"}}], "offset": "NEXT"}

    Api.request = fake_request  # type: ignore[method-assign]
    svc = AirtableService(get_settings())

    res = asyncio.run(svc.fetch_widget_rows(
        url="https://airtable.com/appAAAAAAAAAAAAAA/tblBBBBBBBBBBBBBB",
        api_key="patSECRET.token", caller_email="amy@renphil.org",
        selected_columns=["Name"], filters=[{"field": "S", "operator": "eq", "value": "A"}],
        personalize_enabled=True, personalize_column="Owner", cursor="C1",
    ))
    opts = sent["options"]
    check("page_size is 100", opts["page_size"] == 100)
    check("cursor forwarded as offset", opts["offset"] == "C1")
    check("stored columns forwarded", opts["fields"] == ["Name"])
    check("formula ANDs filter and personalize",
          opts["formula"].startswith("AND({S} = 'A', FIND(',amy@renphil.org,'"),
          opts["formula"][:120])
    check("next_cursor surfaced", res.next_cursor == "NEXT")
    check("fields from stored selection, not the page", res.fields == ["Name"])
    check("not blocked", res.personalize_blocked is False)

    print("\nB2. FAIL CLOSED — personalize on, no email")
    sent.clear()
    res = asyncio.run(svc.fetch_widget_rows(
        url="https://airtable.com/appAAAAAAAAAAAAAA/tblBBBBBBBBBBBBBB",
        api_key="patSECRET.token", caller_email="",
        personalize_enabled=True, personalize_column="Owner",
    ))
    check("zero rows", res.rows == [])
    check("flagged personalize_blocked", res.personalize_blocked is True)
    check("NO Airtable request was made at all", "options" not in sent,
          f"sent={sent}")

    print("\nB3. FAIL CLOSED — personalize on, unusable column")
    sent.clear()
    res = asyncio.run(svc.fetch_widget_rows(
        url="https://airtable.com/appAAAAAAAAAAAAAA/tblBBBBBBBBBBBBBB",
        api_key="patSECRET.token", caller_email="amy@renphil.org",
        personalize_enabled=True, personalize_column="bad}col",
    ))
    check("zero rows", res.rows == [] and res.personalize_blocked is True)
    check("no request made", "options" not in sent)

    print("\nB4. Personalize OFF still works normally")
    sent.clear()
    res = asyncio.run(svc.fetch_widget_rows(
        url="https://airtable.com/appAAAAAAAAAAAAAA/tblBBBBBBBBBBBBBB",
        api_key="patSECRET.token", caller_email="", personalize_enabled=False,
    ))
    check("rows returned", len(res.rows) == 1 and res.personalize_blocked is False)
    check("no formula when there are no filters", "formula" not in sent["options"])

    print(f"\n{'=' * 56}\n{PASS} passed, {FAIL} failed\n{'=' * 56}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
