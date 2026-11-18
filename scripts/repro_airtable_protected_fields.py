"""
Phase 0 regression test — Airtable protected fields.

Proves the security argument for the protected-fields design: the
UNAUTHENTICATED canvas save (`PUT /v2/tabs/{id}/content` ->
update_tab_content_v2) cannot change any field that decides who sees which
rows, nor the credential used to fetch them. Only the AUTHENTICATED config
endpoint can.

Same in-memory-SQLite technique as prior sessions' repro scripts
(@compiles(JSONB, "sqlite") -> JSON), exercising the real service functions
end to end rather than reimplementing them.

Run:  PYTHONPATH=. ./.venv/Scripts/python.exe <this file>
"""
from __future__ import annotations

import json
import sys

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker


@compiles(JSONB, "sqlite")
def _jsonb_as_json(element, compiler, **kw):  # noqa: ANN001, ANN201
    return "JSON"


# All four models must be imported so SQLAlchemy resolves the string FKs.
from app.db_v2.database import BaseV2  # noqa: E402
from app.db_v2.models.tab import TabV2  # noqa: E402
from app.db_v2.models.gridstack import GridstackV2  # noqa: E402
from app.db_v2.models.component import ComponentV2  # noqa: E402
from app.db_v2.models.page_content import PageContentV2  # noqa: E402

from app.services import gridstack_service as gs  # noqa: E402


PASS, FAIL = 0, 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}" + (f"\n          {detail}" if detail else ""))


def make_db():
    engine = create_engine("sqlite://", future=True)
    BaseV2.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def seed_canvas(db):
    """Minimal root tab + its gridstack, addressable by document_id."""
    tab = TabV2(title="Test tab", document_id="doc-tab-1", order=0)
    db.add(tab)
    db.flush()
    grid = GridstackV2(
        document_id="doc-tab-1", name="Test tab", parent_id=None, parent_tab_id=tab.id
    )
    db.add(grid)
    db.commit()
    return tab, grid


def airtable_widget(**data):
    return {
        "schemaVersion": 2,
        "layout": [{"id": "w1", "x": 0, "y": 0, "w": 6, "h": 6, "type": "airtable"}],
        "widgets": {
            "w1": {
                "type": "airtable",
                "title": "Payroll",
                "data": data,
                "access_control": {"viewers": {"users": [], "roles": []}},
            }
        },
    }


def widget_entry(db, grid):
    """The single real (non-representation) component on the canvas."""
    return (
        db.query(ComponentV2)
        .filter(
            ComponentV2.gridstack_id == grid.id,
            ComponentV2.type.notin_(gs.GRIDSTACK_REPRESENTATION_TYPES),
        )
        .first()
    )


def main() -> int:
    db = make_db()
    _tab, grid = seed_canvas(db)

    # ── 1. Create the widget through the normal canvas save ────────────
    print("\n1. Canvas save cannot set protected fields on CREATE")
    gs.update_tab_content_v2(
        db,
        "doc-tab-1",
        airtable_widget(
            sourceUrl="https://airtable.com/appREAL0000000000/tblREAL0000000000",
            pat="patATTACKER0001.secret",
            personalizeEnabled=True,
            personalizeColumn="Owner Email",
            filters=[],
        ),
    )
    comp = widget_entry(db, grid)
    check("component created", comp is not None)
    data = gs._resolve_component_data(db, comp)
    check("pat NOT persisted from content save", "pat" not in data, f"got {data!r}")
    check("sourceUrl NOT persisted", "sourceUrl" not in data, f"got {data!r}")
    check("personalizeEnabled NOT persisted", "personalizeEnabled" not in data)
    check("personalizeColumn NOT persisted", "personalizeColumn" not in data)
    check("access_control NOT persisted", comp.access_control is None,
          f"got {comp.access_control!r}")
    check("unprotected field survives", data.get("filters") == [])

    link = comp.link

    # ── 2. The authenticated config endpoint CAN set them ──────────────
    print("\n2. Config service sets protected fields")
    real_ac = {"viewers": {"users": [], "roles": [{"name": "Hub Admin", "scope": "Hub"}]}}
    cfg = gs.update_airtable_component_config(
        db,
        link,
        source_url="https://airtable.com/appREAL0000000000/tblREAL0000000000",
        pat="patREALTOKEN01.supersecret",
        personalize_enabled=True,
        personalize_column="Owner Email",
        access_control=real_ac,
    )
    check("config returns hasPat", cfg["hasPat"] is True)
    check("config never returns the pat", "pat" not in cfg, f"got keys {sorted(cfg)}")
    check("patHint is the token id prefix", cfg["patHint"] == "patREALTOKEN01",
          f"got {cfg['patHint']!r}")
    check("patUpdatedAt stamped", bool(cfg["patUpdatedAt"]))
    check("personalizeEnabled stored", cfg["personalizeEnabled"] is True)
    check("access_control stored", cfg["access_control"] == real_ac)

    # ── 3. THE ONE THAT MATTERS: hostile unauthenticated canvas save ───
    print("\n3. Hostile canvas save cannot weaken the boundary")
    gs.update_tab_content_v2(
        db,
        "doc-tab-1",
        {
            "schemaVersion": 2,
            "layout": [{"id": str(comp.id), "x": 0, "y": 0, "w": 6, "h": 6,
                        "type": "airtable"}],
            "widgets": {
                str(comp.id): {
                    "type": "airtable",
                    "link": link,
                    "title": "Payroll",
                    "data": {
                        # every protected field, attacked
                        "sourceUrl": "https://airtable.com/appOTHER000000000/tblOTHER000000000",
                        "pat": "patATTACKER0002.secret",
                        "personalizeEnabled": False,
                        "personalizeColumn": "",
                        "patUpdatedAt": "1999-01-01T00:00:00+00:00",
                        # unprotected, should go through
                        "filters": [{"field": "Status", "operator": "eq",
                                     "value": "Active"}],
                        "selectedColumns": ["Amount"],
                    },
                    # AC cleared -> would defeat the check on /rows
                    "access_control": None,
                }
            },
        },
    )
    db.expire_all()
    comp = widget_entry(db, grid)
    data = gs._resolve_component_data(db, comp)

    # Read through the internal accessor: `_resolve_component_data` strips
    # `pat` for clients now (step 2), so the sanitised view can no longer
    # answer "did the token change".
    check("pat UNCHANGED",
          gs.get_airtable_pat_for_component(db, link) == "patREALTOKEN01.supersecret",
          f"got {gs.get_airtable_pat_for_component(db, link)!r}")
    check("hostile save did not leak pat back to the client", "pat" not in data)
    check("sourceUrl UNCHANGED",
          data.get("sourceUrl") == "https://airtable.com/appREAL0000000000/tblREAL0000000000",
          f"got {data.get('sourceUrl')!r}")
    check("personalizeEnabled UNCHANGED (still True)",
          data.get("personalizeEnabled") is True, f"got {data.get('personalizeEnabled')!r}")
    check("personalizeColumn UNCHANGED",
          data.get("personalizeColumn") == "Owner Email",
          f"got {data.get('personalizeColumn')!r}")
    check("patUpdatedAt UNCHANGED", not data.get("patUpdatedAt", "").startswith("1999"),
          f"got {data.get('patUpdatedAt')!r}")
    check("access_control UNCHANGED (not cleared)", comp.access_control == real_ac,
          f"got {comp.access_control!r}")
    check("unprotected filters DID change",
          data.get("filters") == [{"field": "Status", "operator": "eq", "value": "Active"}])
    check("unprotected selectedColumns DID change",
          data.get("selectedColumns") == ["Amount"])

    # ── 4. PAT three-way semantics ─────────────────────────────────────
    print("\n4. PAT preserve / replace / clear")
    cfg = gs.update_airtable_component_config(db, link, personalize_column="Owner Email")
    check("omitted pat preserves", cfg["patHint"] == "patREALTOKEN01")

    cfg = gs.update_airtable_component_config(db, link, pat="   ")
    check("blank pat preserves", cfg["patHint"] == "patREALTOKEN01")

    stamp_before = cfg["patUpdatedAt"]
    cfg = gs.update_airtable_component_config(db, link, pat="patREALTOKEN01.supersecret")
    check("resending the SAME pat does not bump patUpdatedAt",
          cfg["patUpdatedAt"] == stamp_before,
          f"{stamp_before!r} -> {cfg['patUpdatedAt']!r}")

    cfg = gs.update_airtable_component_config(db, link, pat="patROTATED02.newsecret")
    check("non-empty pat replaces", cfg["patHint"] == "patROTATED02")
    check("a real rotation DOES bump patUpdatedAt", cfg["patUpdatedAt"] != stamp_before)

    cfg = gs.update_airtable_component_config(db, link, pat=None)
    check("explicit null clears", cfg["hasPat"] is False and cfg["patHint"] is None,
          f"got {cfg!r}")
    check("patUpdatedAt cleared with the token", cfg["patUpdatedAt"] is None)

    # ── 5. patHint never echoes an unrecognised secret ─────────────────
    print("\n5. patHint safety")
    check("unrecognised shape is digested",
          gs._pat_hint("totally-not-an-airtable-token").startswith("sha256:"))
    check("no PAT -> no hint", gs._pat_hint("") is None and gs._pat_hint(None) is None)

    # ── 6. Non-airtable widgets are untouched by any of this ───────────
    print("\n6. Other widget types unaffected")
    gs.update_tab_content_v2(
        db,
        "doc-tab-1",
        {
            "schemaVersion": 2,
            "layout": [{"id": "n1", "x": 0, "y": 0, "w": 4, "h": 4, "type": "links"}],
            "widgets": {
                "n1": {
                    "type": "links",
                    "title": "Links",
                    "data": {"sourceUrl": "anything", "pat": "not-protected-here"},
                    "access_control": {"viewers": {"users": [], "roles": []}},
                }
            },
        },
    )
    links = (
        db.query(ComponentV2)
        .filter(ComponentV2.gridstack_id == grid.id, ComponentV2.type == "links")
        .first()
    )
    links_data = gs._resolve_component_data(db, links)
    check("links widget keeps its own data verbatim",
          links_data.get("pat") == "not-protected-here" and
          links_data.get("sourceUrl") == "anything", f"got {links_data!r}")
    check("links widget access_control still written",
          links.access_control == {"viewers": {"users": [], "roles": []}},
          f"got {links.access_control!r}")

    # ── 7. Unknown / wrong-type links ──────────────────────────────────
    print("\n7. Link resolution")
    check("unknown link -> None", gs.get_airtable_component_config(db, "nope") is None)
    check("non-airtable link -> None",
          gs.get_airtable_component_config(db, links.link) is None)
    check("update on unknown link -> None",
          gs.update_airtable_component_config(db, "nope", pat="x") is None)

    # ── 8. PAT strip on every client-facing path ───────────────────────
    print("\n8. PAT never reaches a client")
    db2 = make_db()
    _t2, grid2 = seed_canvas(db2)
    gs.update_tab_content_v2(db2, "doc-tab-1", airtable_widget(filters=[]))
    comp2 = widget_entry(db2, grid2)
    link2 = comp2.link
    gs.update_airtable_component_config(
        db2, link2, pat="patSECRET99.hidden", source_url="https://airtable.com/appA/tblB"
    )

    # (a) the serializer behind GET /v2/tabs/{id}/content — unauthenticated
    serialized = gs._serialize_gridstack_content(db2, grid2)
    blob = json.dumps(serialized)
    check("pat absent from serialized tab content", "patSECRET99" not in blob, blob[:200])
    entry = serialized["widgets"][str(comp2.id)]
    check("hasPat surfaced instead", entry["data"].get("hasPat") is True,
          f"got {entry['data']!r}")
    check("pat key gone from widget data", "pat" not in entry["data"])
    check("sourceUrl still visible", entry["data"].get("sourceUrl") == "https://airtable.com/appA/tblB")

    # (b) the by-link lookup
    by_link = gs.get_component_by_link_v2(db2, link2)
    check("pat absent from by-link lookup", "patSECRET99" not in json.dumps(by_link))

    # (c) the internal accessor still sees it
    check("internal accessor returns the real pat",
          gs.get_airtable_pat_for_component(db2, link2) == "patSECRET99.hidden")
    check("accessor -> None for unknown link",
          gs.get_airtable_pat_for_component(db2, "nope") is None)

    # ── 9. The strip must not DELETE the token on the next write ───────
    print("\n9. Strip does not cause data loss on write-back")
    # A canvas save round-trips the sanitised blob (no `pat`) straight back.
    gs.update_tab_content_v2(
        db2,
        "doc-tab-1",
        {
            "schemaVersion": 2,
            "layout": [{"id": str(comp2.id), "x": 0, "y": 0, "w": 6, "h": 6,
                        "type": "airtable"}],
            "widgets": {str(comp2.id): {"type": "airtable", "link": link2,
                                        "data": dict(entry["data"])}},
        },
    )
    db2.expire_all()
    check("pat survives a canvas save that echoed hasPat back",
          gs.get_airtable_pat_for_component(db2, link2) == "patSECRET99.hidden",
          f"got {gs.get_airtable_pat_for_component(db2, link2)!r}")

    # Updating an unrelated field through the config endpoint must not wipe it.
    gs.update_airtable_component_config(db2, link2, personalize_column="Owner")
    check("pat survives an unrelated config update",
          gs.get_airtable_pat_for_component(db2, link2) == "patSECRET99.hidden")

    # ── 10. hasPat is never persisted ──────────────────────────────────
    print("\n10. hasPat is computed, never stored")
    raw = gs._raw_component_data(db2, gs._airtable_component_by_link(db2, link2))
    check("hasPat absent from stored blob", "hasPat" not in raw, f"got {sorted(raw)}")

    # A client asserting hasPat on a widget with NO token must not stick.
    db3 = make_db()
    _t3, grid3 = seed_canvas(db3)
    gs.update_tab_content_v2(db3, "doc-tab-1", airtable_widget(hasPat=True))
    comp3 = widget_entry(db3, grid3)
    raw3 = gs._raw_component_data(db3, comp3)
    check("client-asserted hasPat not stored", "hasPat" not in raw3, f"got {sorted(raw3)}")
    check("computed hasPat is False with no token",
          gs._resolve_component_data(db3, comp3).get("hasPat") is False)

    print(f"\n{'=' * 52}\n{PASS} passed, {FAIL} failed\n{'=' * 52}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
