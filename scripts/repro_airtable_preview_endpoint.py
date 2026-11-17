"""
Step 5 regression test — POST /data/airtable/component/preview.

The security question this endpoint has to answer correctly:
**can a caller use a token they cannot read, against a table of their
choosing?** The answer must be no. The stored token is usable only with the
stored source URL; anything else requires the caller to supply their own.

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
STORED_URL = "https://airtable.com/appSTORED000000000/tblSTORED000000000"
EVIL_URL = "https://airtable.com/appEVIL0000000000/tblEVIL0000000000"


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}" + (f"\n          {detail}" if detail else ""))


engine = create_engine("sqlite://", future=True,
                       connect_args={"check_same_thread": False}, poolclass=StaticPool)
BaseV2.metadata.create_all(engine)
db = sessionmaker(bind=engine, future=True)()

tab = TabV2(title="T", document_id="doc-1", order=0)
db.add(tab)
db.flush()
db.add(GridstackV2(document_id="doc-1", name="T", parent_id=None, parent_tab_id=tab.id))
db.commit()
gs.update_tab_content_v2(db, "doc-1", {
    "schemaVersion": 2,
    "layout": [{"id": "w1", "x": 0, "y": 0, "w": 6, "h": 6, "type": "airtable"}],
    "widgets": {"w1": {"type": "airtable", "data": {}}},
})
LINK = db.query(ComponentV2).filter(ComponentV2.type == "airtable").first().link
gs.update_airtable_component_config(
    db, LINK, source_url=STORED_URL, pat="patSTORED.secret")


class Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def preview_widget_config(self, **kwargs):
        from app.models.airtable import AirtableEditorPreviewResponse
        self.calls.append(kwargs)
        return AirtableEditorPreviewResponse(
            base_id="app", table_id="tbl", fields=["A", "B"], rows=[],
            unpersonalized_row_count=7,
        )


recorder = Recorder()
CURRENT: dict[str, Any] = {}
app.dependency_overrides[get_db_v2] = lambda: db
app.dependency_overrides[get_airtable_service] = lambda: recorder
app.dependency_overrides[get_current_user] = lambda: UserInfo(
    email=CURRENT.get("email", "admin@renphil.org"), name="T",
    roles=CURRENT.get("roles", []))

client = TestClient(app)
URL = "/data/airtable/component/preview"


def as_user(email: str, roles: list[str] | None = None) -> None:
    CURRENT["email"] = email
    CURRENT["roles"] = roles or []


def main() -> int:
    as_user("admin@renphil.org")

    print("\n1. THE BORROWING RULE — stored token, attacker-chosen table")
    recorder.calls.clear()
    r = client.post(URL, json={"link": LINK, "sourceUrl": EVIL_URL})
    check("400 — cannot aim the stored token elsewhere", r.status_code == 400, r.text[:200])
    check("no Airtable call was made", not recorder.calls)
    check("error explains what to do", "token for the new source URL" in r.text, r.text[:200])

    print("\n2. Stored token with the stored URL is fine")
    recorder.calls.clear()
    r = client.post(URL, json={"link": LINK, "sourceUrl": STORED_URL})
    check("200", r.status_code == 200, r.text[:200])
    check("used the stored URL", recorder.calls[0]["url"] == STORED_URL)
    check("used the stored token", recorder.calls[0]["api_key"] == "patSTORED.secret")

    print("\n3. Link alone (no URL in the body)")
    recorder.calls.clear()
    r = client.post(URL, json={"link": LINK})
    check("200", r.status_code == 200)
    check("resolves URL from storage", recorder.calls[0]["url"] == STORED_URL)

    print("\n4. Own-token shape borrows nothing")
    recorder.calls.clear()
    r = client.post(URL, json={"sourceUrl": EVIL_URL, "pat": "patMINE.own"})
    check("200 — caller already holds this token", r.status_code == 200)
    check("uses the caller's token", recorder.calls[0]["api_key"] == "patMINE.own")
    check("uses the caller's URL", recorder.calls[0]["url"] == EVIL_URL)

    recorder.calls.clear()
    r = client.post(URL, json={"link": LINK, "sourceUrl": EVIL_URL, "pat": "patMINE.own"})
    check("own token wins over the stored one",
          r.status_code == 200 and recorder.calls[0]["api_key"] == "patMINE.own")

    print("\n5. Malformed requests")
    check("400 with neither link nor pat",
          client.post(URL, json={}).status_code == 400)
    check("400 with a pat but no sourceUrl",
          client.post(URL, json={"pat": "patX.y"}).status_code == 400)
    check("404 for an unknown link",
          client.post(URL, json={"link": "nope"}).status_code == 404)
    check("422 for an unknown field (extra=forbid)",
          client.post(URL, json={"link": LINK, "bogus": 1}).status_code == 422)

    print("\n6. Access control on the stored-token shape")
    gs.update_airtable_component_config(db, LINK, access_control={
        "viewers": {"users": [{"email": "allowed@renphil.org"}], "roles": []}})

    as_user("nobody@renphil.org")
    check("403 for an unlisted caller",
          client.post(URL, json={"link": LINK}).status_code == 403)

    recorder.calls.clear()
    r = client.post(URL, json={"sourceUrl": EVIL_URL, "pat": "patMINE.own"})
    check("own-token shape is NOT gated by that widget's AC", r.status_code == 200)

    as_user("allowed@renphil.org")
    check("200 for a listed caller", client.post(URL, json={"link": LINK}).status_code == 200)
    as_user("nobody@renphil.org", ["Hub Admin"])
    check("200 for Hub Admin", client.post(URL, json={"link": LINK}).status_code == 200)

    print("\n7. Editor overrides reach the service")
    as_user("admin@renphil.org", ["Hub Admin"])
    recorder.calls.clear()
    client.post(URL, json={
        "link": LINK,
        "selectedColumns": ["A"],
        "filters": [{"field": "S", "operator": "eq", "value": "X"}],
        "personalizeEnabled": True,
        "personalizeColumn": "Owner",
    })
    call = recorder.calls[0]
    check("selectedColumns forwarded", call["selected_columns"] == ["A"])
    check("filters forwarded", call["filters"] == [{"field": "S", "operator": "eq", "value": "X"}])
    check("personalize forwarded",
          call["personalize_enabled"] is True and call["personalize_column"] == "Owner")
    check("caller's own email used, not a body value",
          call["caller_email"] == "admin@renphil.org")

    print("\n8. Response never carries a token")
    r = client.post(URL, json={"link": LINK})
    check("no stored pat in the body", "patSTORED" not in r.text)
    check("unpersonalized_row_count surfaced", r.json()["unpersonalized_row_count"] == 7)

    # ── B. Real service, HTTP faked ────────────────────────────────────
    print("\nB. Real service: fields without personalize, rows with")
    import asyncio
    from app.config import get_settings
    from app.services.airtable_service import AirtableService
    from pyairtable import Api

    seen: list[dict[str, Any]] = []

    def fake_request(self, method, url, **kwargs):
        opts = kwargs.get("options") or {}
        seen.append(opts)
        # Simulate: 3 rows total, only one belonging to amy@.
        everyone = [
            {"id": "r1", "fields": {"Name": "a", "Owner": "amy@renphil.org"}},
            {"id": "r2", "fields": {"Name": "b", "Owner": "bob@renphil.org"}},
            {"id": "r3", "fields": {"Name": "c", "Owner": "cat@renphil.org"}},
        ]
        if "formula" in opts and "FIND(" in opts["formula"]:
            return {"records": [everyone[0]]}
        return {"records": everyone}

    Api.request = fake_request  # type: ignore[method-assign]
    svc = AirtableService(get_settings())

    res = asyncio.run(svc.preview_widget_config(
        url=STORED_URL, api_key="patX.y", caller_email="amy@renphil.org",
        personalize_enabled=True, personalize_column="Owner"))
    check("two Airtable calls when personalizing", len(seen) == 2, str(len(seen)))
    check("first call has NO personalize formula",
          "formula" not in seen[0] or "FIND(" not in seen[0].get("formula", ""))
    check("fields discovered from the unpersonalized call",
          res.fields == ["Name", "Owner"], f"{res.fields}")
    check("rows are the admin's own only", [r["id"] for r in res.rows] == ["r1"])
    check("unpersonalized_row_count reports the full set",
          res.unpersonalized_row_count == 3)

    print("\nB2. Dropdowns populate even when the admin has NO rows")
    seen.clear()

    def none_for_me(self, method, url, **kwargs):
        opts = kwargs.get("options") or {}
        seen.append(opts)
        if "formula" in opts and "FIND(" in opts["formula"]:
            return {"records": []}
        return {"records": [{"id": "r1", "fields": {"Name": "a", "Owner": "zoe@x.org"}}]}

    Api.request = none_for_me  # type: ignore[method-assign]
    res = asyncio.run(svc.preview_widget_config(
        url=STORED_URL, api_key="patX.y", caller_email="amy@renphil.org",
        personalize_enabled=True, personalize_column="Owner"))
    check("fields still populated", res.fields == ["Name", "Owner"], f"{res.fields}")
    check("rows correctly empty", res.rows == [])
    check("count shows the table is NOT empty (so the panel can warn)",
          res.unpersonalized_row_count == 1)

    print("\nB3. Personalize off -> one call, no formula")
    seen.clear()
    res = asyncio.run(svc.preview_widget_config(
        url=STORED_URL, api_key="patX.y", caller_email="amy@renphil.org"))
    check("single Airtable call", len(seen) == 1, str(len(seen)))
    check("rows returned unfiltered", len(res.rows) == 1)

    print("\nB4. Blocked personalize makes no second call")
    seen.clear()
    res = asyncio.run(svc.preview_widget_config(
        url=STORED_URL, api_key="patX.y", caller_email="",
        personalize_enabled=True, personalize_column="Owner"))
    check("personalize_blocked", res.personalize_blocked is True)
    check("no rows", res.rows == [])
    check("fields still discovered", res.fields == ["Name", "Owner"])
    check("only the discovery call happened", len(seen) == 1, str(len(seen)))

    print(f"\n{'=' * 56}\n{PASS} passed, {FAIL} failed\n{'=' * 56}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
