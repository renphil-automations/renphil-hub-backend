"""
One-shot migration: copy all tab / page-content data from the old schema
(app.models, DATABASE_URL) into the Phase 2 new schema (app.db_v2.models,
DATABASE_URL_V2 — see docker-compose.yml's db_v2 service).

Every old Tab becomes either:
  - a TabV2 row + a root GridstackV2 row (if it has no parent), or
  - a nested GridstackV2 row under its parent's new gridstack (if it has a
    parent via tabs_parent_lnk — this covers nav-tree children, Super
    BlockNote sub-tabs, and Super GridStack sub-tabs uniformly, since all
    three are structurally identical in the old schema).
Its PageContent.content becomes ComponentV2 row(s) under that gridstack.

Idempotency: wipe-and-reload. There's no stable key to upsert synthesized
gridstacks/components against across runs, and this is explicitly "for
testing purposes" — every run truncates the v2 tables and repopulates from
scratch, so it's always safe to re-run.

Run from the project root:

    python -m scripts.migrate_to_v2_schema
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.database import SessionLocal
from app.models.links import PageContentTabLink, TabParentLink
from app.models.page_content import PageContent
from app.models.tab import Tab
from app.services.tab_service import DEFAULT_ACCESS_CONTROL

from app.db_v2.models.component import ComponentV2
from app.db_v2.models.gridstack import GridstackV2
from app.db_v2.models.page_content import PageContentV2
from app.db_v2.models.tab import TabV2
from app.db_v2.database import SessionLocalV2

from scripts.create_v2_schema_tables import main as create_v2_schema_tables


# Sentinel widget type the backend substitutes at *read time* for widgets the
# viewer can't see (tab_service.filter_widget_content_for_user) — never real
# stored data, must never be migrated.
RESTRICTED_WIDGET_TYPE = "restricted"

# Matches gridContent.ts's makeDefaultBlockNoteCanvas layout convention for a
# single full-width BlockNote widget, reused here for legacy tab content.
LEGACY_BLOCK_NOTE_LAYOUT = {"x": 0.0, "y": 0.0, "width": 12.0, "height": 10.0}


def _reset_v2_tables(db_v2) -> None:
    print("Resetting v2 tables (TRUNCATE ... RESTART IDENTITY CASCADE)...")
    db_v2.execute(
        text("TRUNCATE TABLE components, gridstacks, tabs, page_content RESTART IDENTITY CASCADE")
    )
    db_v2.commit()


def _access_control_or_default(access_control: dict[str, Any] | None) -> dict[str, Any]:
    return access_control if access_control else DEFAULT_ACCESS_CONTROL


def _is_sgs_flagged(content: Any) -> bool:
    return isinstance(content, dict) and bool(content.get("sgs"))


def _classify_legacy_sbn_child_content(
    child_content: Any, child: Tab, warnings: list[str]
) -> list[Any]:
    """Narrowest-safe conversion of one legacy SBN sub-tab's own content into
    the Block[] a new `block_note`-typed nested component will hold (per the
    confirmed scope: a v2 SBN sub-tab is always plain-text, never its own
    rich canvas). Only two shapes convert faithfully — bare legacy Block[]/
    None, or a schemaVersion:2 canvas containing EXACTLY ONE block_note
    widget (flattened, dropping the canvas wrapper). Anything richer
    (multiple widgets, a single non-block_note widget, nested legacy
    super_block_note-in-super_block_note) is intentionally NOT reconstructed
    — returns empty content with a clear warning, rather than guessing."""
    if child_content is None:
        return []

    if isinstance(child_content, list):
        return child_content

    if isinstance(child_content, dict) and child_content.get("schemaVersion") == 2:
        widgets = child_content.get("widgets") or {}
        real_widgets = {
            wid: w
            for wid, w in widgets.items()
            if isinstance(w, dict) and w.get("type") != RESTRICTED_WIDGET_TYPE
        }
        if len(real_widgets) == 1:
            only_widget = next(iter(real_widgets.values()))
            if only_widget.get("type") == "block_note":
                data = only_widget.get("data")
                blocks = data.get("content") if isinstance(data, dict) else None
                return blocks if isinstance(blocks, list) else []

        warnings.append(
            f"old tab id={child.id} document_id={child.document_id!r} "
            f"title={child.title!r} is a Super Block Note sub-tab whose own "
            f"content is richer than plain text (a canvas with "
            f"{len(real_widgets)} widget(s), or a non-block_note single "
            f"widget) — not reconstructed in this migration pass; migrated "
            f"as an empty text page instead."
        )
        return []

    # Unrecognized shape (not list/None/dict, or dict without schemaVersion 2)
    warnings.append(
        f"old tab id={child.id} document_id={child.document_id!r} "
        f"title={child.title!r} is a Super Block Note sub-tab with an "
        f"unrecognized content shape — migrated as an empty text page."
    )
    return []


def _migrate_sbn_children(
    db_v2,
    sbn_component_id: int,
    gridstack_id: int,
    old_children: list[Tab],
    content_by_tab_id: dict[int, Any],
    children_by_parent_id: dict[int, list[Tab]],
    counters: dict[str, int],
    warnings: list[str],
    visited_tab_ids: set[int],
) -> set[int]:
    """Converts one Super Block Note widget's old-schema sub-tabs into
    nested ComponentV2 rows (super_blocknote_id = sbn_component_id, always
    type="block_note" — no UI yet to create a nested container sub-tab, so
    the migration never reconstructs legacy SBN-in-SBN nesting either).
    Returns the set of old tab ids consumed this way, so the caller can
    exclude them from the normal nested-gridstack recursion. A consumed
    child's OWN children (if any) are deliberately left out of
    `visited_tab_ids` — they're not reconstructed under the flattened
    sub-tab, but naturally resurface via main()'s existing "unreached tabs
    become extra root tabs" sweep instead of being silently dropped."""
    consumed_ids: set[int] = set()

    for index, child in enumerate(old_children):
        if child.id in visited_tab_ids:
            continue
        visited_tab_ids.add(child.id)
        consumed_ids.add(child.id)

        blocks = _classify_legacy_sbn_child_content(
            content_by_tab_id.get(child.id), child, warnings
        )

        new_page_content = PageContentV2(content=blocks)
        db_v2.add(new_page_content)
        db_v2.flush()
        counters["page_content"] += 1

        new_component = ComponentV2(
            link=str(uuid4()),
            type="block_note",
            title=child.title,
            props={
                "locked": bool(child.locked),
                "locked_by": child.locked_by or "",
                "order": index,
            },
            access_control=_access_control_or_default(child.access_control),
            x=0,
            y=0,
            width=6,
            height=6,
            gridstack_id=gridstack_id,
            super_blocknote_id=sbn_component_id,
            page_content_id=new_page_content.id,
            current_grid_id=None,
        )
        db_v2.add(new_component)
        db_v2.flush()
        counters["components"] += 1

        if children_by_parent_id.get(child.id):
            warnings.append(
                f"old tab id={child.id} document_id={child.document_id!r} "
                f"title={child.title!r} (a Super Block Note sub-tab) has its "
                f"own child tab(s) — nested legacy SBN-in-SBN is not "
                f"reconstructed in this migration pass; those descendants "
                f"are left unvisited and will surface as their own extra "
                f"root tab(s) instead of being silently dropped."
            )

    return consumed_ids


def _migrate_widgets_content(
    db_v2,
    content: Any,
    gridstack_id: int,
    old_tab: Tab,
    children_by_parent_id: dict[int, list[Tab]],
    content_by_tab_id: dict[int, Any],
    counters: dict[str, int],
    warnings: list[str],
    visited_tab_ids: set[int],
) -> set[int]:
    """Create ComponentV2 (and possibly PageContentV2) rows for one old
    tab's PageContent.content under the given new gridstack. Returns the set
    of old child tab ids consumed as a Super Block Note's own nested
    sub-tabs (empty if this tab has no such widget) — the caller must
    exclude these from its own normal nested-gridstack recursion."""

    consumed_child_ids: set[int] = set()

    if isinstance(content, dict) and content.get("schemaVersion") == 2:
        layout_by_id = {entry.get("id"): entry for entry in (content.get("layout") or [])}
        widgets = content.get("widgets") or {}

        for widget_id, widget_entry in widgets.items():
            if not isinstance(widget_entry, dict):
                continue

            widget_type = widget_entry.get("type")
            if widget_type == RESTRICTED_WIDGET_TYPE:
                continue

            layout_entry = layout_by_id.get(widget_id) or {}
            widget_data = widget_entry.get("data")
            props: dict[str, Any] = {}

            if layout_entry.get("minW") is not None:
                props["min_w"] = layout_entry["minW"]
            if layout_entry.get("minH") is not None:
                props["min_h"] = layout_entry["minH"]

            new_component = ComponentV2(
                link=str(uuid4()),
                type=widget_type,
                title=widget_entry.get("title"),
                description=widget_entry.get("description"),
                props=props,
                access_control=widget_entry.get("access_control"),
                x=layout_entry.get("x", 0),
                y=layout_entry.get("y", 0),
                width=layout_entry.get("w", 6),
                height=layout_entry.get("h", 6),
                gridstack_id=gridstack_id,
                page_content_id=None,
                current_grid_id=None,
            )
            db_v2.add(new_component)

            # Every type's `data` lives in page_content now (see
            # gridstack_service.py's ComponentV2 docstring) — `mirror`
            # persists only target_link (in props, handled above via the
            # widget's own `data.targetLink` — wait, mirror's persisted
            # value is target_link, which belongs in props, not
            # page_content; every OTHER type's data goes to page_content.
            if widget_type == "mirror":
                target_link = (widget_data or {}).get("targetLink") if isinstance(widget_data, dict) else None
                new_component.props = {**props, "target_link": target_link}
            else:
                stored = (
                    (widget_data or {}).get("content", []) if widget_type == "block_note"
                    else (widget_data or {})
                )
                new_page_content = PageContentV2(content=stored)
                db_v2.add(new_page_content)
                db_v2.flush()
                counters["page_content"] += 1
                new_component.page_content_id = new_page_content.id

            db_v2.flush()
            counters["components"] += 1

            if widget_type == "super_block_note" and isinstance(widget_data, dict):
                root_tab_id = widget_data.get("rootTabId")
                own_children = children_by_parent_id.get(old_tab.id, [])

                if root_tab_id:
                    warnings.append(
                        f"super_block_note widget {widget_id!r} on old tab "
                        f"id={old_tab.id} document_id={old_tab.document_id!r} "
                        f"title={old_tab.title!r} references rootTabId="
                        f"{root_tab_id!r} (a different tab) — cross-tab "
                        f"rootTabId is not remapped in this migration pass; "
                        f"its sub-tabs are not migrated as this widget's "
                        f"children."
                    )
                elif _is_sgs_flagged(content) and own_children:
                    # Ambiguity: this same tab is ALSO Super GridStack-
                    # flagged, so its children are already being migrated
                    # (by the normal recursion) as sibling nested gridstacks
                    # for the outer SGS tab bar. SGS's own gridstack-nesting
                    # wins — don't ALSO claim these same children as this
                    # widget's SBN components (would duplicate the data).
                    warnings.append(
                        f"old tab id={old_tab.id} document_id={old_tab.document_id!r} "
                        f"title={old_tab.title!r} is BOTH Super GridStack-flagged "
                        f"AND contains an embedded super_block_note widget "
                        f"referencing the same child tabs — this is an inherent "
                        f"ambiguity in the old schema. SGS's own gridstack-nesting "
                        f"wins; the super_block_note widget's own sub-tab tree is "
                        f"left empty rather than duplicating the data."
                    )
                elif own_children:
                    # Every nested sub-tab component shares the SAME
                    # gridstack_id as its SBN parent widget — they're not
                    # separate canvases, just hidden co-located rows.
                    consumed_child_ids |= _migrate_sbn_children(
                        db_v2,
                        new_component.id,
                        gridstack_id,
                        own_children,
                        content_by_tab_id,
                        children_by_parent_id,
                        counters,
                        warnings,
                        visited_tab_ids,
                    )
    else:
        # Legacy: bare Block[], None, or {} — one block_note component
        # backed by a PageContentV2 row.
        raw_content = content if content is not None else []

        new_page_content = PageContentV2(content=raw_content)
        db_v2.add(new_page_content)
        db_v2.flush()
        counters["page_content"] += 1

        db_v2.add(
            ComponentV2(
                link=str(uuid4()),
                type="block_note",
                props={},
                access_control=None,
                x=LEGACY_BLOCK_NOTE_LAYOUT["x"],
                y=LEGACY_BLOCK_NOTE_LAYOUT["y"],
                width=LEGACY_BLOCK_NOTE_LAYOUT["width"],
                height=LEGACY_BLOCK_NOTE_LAYOUT["height"],
                gridstack_id=gridstack_id,
                page_content_id=new_page_content.id,
                current_grid_id=None,
            )
        )
        counters["components"] += 1

    return consumed_child_ids


def _migrate_tab_subtree(
    db_v2,
    old_tab: Tab,
    parent_gridstack_id: int | None,
    root_tab_id: int | None,
    content_by_tab_id: dict[int, Any],
    children_by_parent_id: dict[int, list[Tab]],
    counters: dict[str, int],
    warnings: list[str],
    visited_tab_ids: set[int],
) -> None:
    if old_tab.id in visited_tab_ids:
        # Guards against cycles in tabs_parent_lnk (seen in real cloned data:
        # a tab whose parent link points at itself) — without this, a cyclic
        # child would recurse forever, or (if reached twice via the
        # unreached-tab sweep below) be migrated as a duplicate.
        return
    visited_tab_ids.add(old_tab.id)

    content = content_by_tab_id.get(old_tab.id)
    is_root = parent_gridstack_id is None

    if is_root:
        new_tab = TabV2(
            document_id=old_tab.document_id,
            title=old_tab.title,
            order=old_tab.order,
            access_control=_access_control_or_default(old_tab.access_control),
            locked=old_tab.locked,
            locked_by=old_tab.locked_by,
            source_link=old_tab.source_link,
            created_at=old_tab.created_at,
            updated_at=old_tab.updated_at,
        )
        db_v2.add(new_tab)
        db_v2.flush()
        counters["tabs"] += 1

        settings: dict[str, Any] = {}
        if _is_sgs_flagged(content):
            settings["sgs"] = content["sgs"]

        new_gridstack = GridstackV2(
            document_id=new_tab.document_id,
            name=old_tab.title,
            settings=settings,
            position=old_tab.order,
            parent_id=None,
            parent_tab_id=new_tab.id,
        )
        this_root_tab_id = new_tab.id
    else:
        settings = {"access_control": _access_control_or_default(old_tab.access_control)}
        if _is_sgs_flagged(content):
            settings["sgs"] = content["sgs"]

        new_gridstack = GridstackV2(
            document_id=str(uuid4()),
            name=old_tab.title,
            settings=settings,
            position=old_tab.order,
            parent_id=parent_gridstack_id,
            parent_tab_id=root_tab_id,
        )
        this_root_tab_id = root_tab_id

    db_v2.add(new_gridstack)
    db_v2.flush()
    counters["gridstacks"] += 1

    # `consumed_child_ids`: old tab ids already migrated as a Super Block
    # Note widget's own nested sub-tab components (see
    # _migrate_sbn_children) — must be excluded from the recursion below,
    # which would otherwise ALSO migrate them as sibling nested gridstacks
    # (its normal, unconditional behavior for every other kind of child).
    consumed_child_ids = _migrate_widgets_content(
        db_v2,
        content,
        new_gridstack.id,
        old_tab,
        children_by_parent_id,
        content_by_tab_id,
        counters,
        warnings,
        visited_tab_ids,
    )

    for child in children_by_parent_id.get(old_tab.id, []):
        if child.id in consumed_child_ids:
            continue
        _migrate_tab_subtree(
            db_v2,
            child,
            new_gridstack.id,
            this_root_tab_id,
            content_by_tab_id,
            children_by_parent_id,
            counters,
            warnings,
            visited_tab_ids,
        )


def main() -> None:
    print("Ensuring v2 schema tables exist...")
    create_v2_schema_tables()

    db_old = SessionLocal()
    db_v2 = SessionLocalV2()

    try:
        _reset_v2_tables(db_v2)

        print("Loading old-schema data...")
        all_tabs = db_old.query(Tab).all()
        all_links = db_old.query(TabParentLink).all()
        all_content_links = db_old.query(PageContentTabLink).all()
        all_page_contents = {pc.id: pc for pc in db_old.query(PageContent).all()}

        tabs_by_id = {tab.id: tab for tab in all_tabs}
        parent_by_child_id = {
            link.tab_id: link.inv_tab_id
            for link in all_links
            if link.inv_tab_id in tabs_by_id
        }

        children_by_parent_id: dict[int, list[Tab]] = {}
        for link in all_links:
            if link.inv_tab_id in tabs_by_id and link.tab_id in tabs_by_id:
                children_by_parent_id.setdefault(link.inv_tab_id, []).append(
                    tabs_by_id[link.tab_id]
                )
        for children in children_by_parent_id.values():
            children.sort(key=lambda t: (t.order if t.order is not None else 0, t.id))

        content_link_by_tab_id = {link.tab_id: link.page_content_id for link in all_content_links}
        content_by_tab_id = {
            tab_id: all_page_contents[pc_id].content
            for tab_id, pc_id in content_link_by_tab_id.items()
            if pc_id in all_page_contents
        }

        root_tabs = [tab for tab in all_tabs if tab.id not in parent_by_child_id]
        root_tabs.sort(key=lambda t: (t.order if t.order is not None else 0, t.id))

        counters = {"tabs": 0, "gridstacks": 0, "components": 0, "page_content": 0}
        warnings: list[str] = []
        visited_tab_ids: set[int] = set()

        print(f"Migrating {len(root_tabs)} root tab(s) ({len(all_tabs)} total old tabs)...")

        for root_tab in root_tabs:
            _migrate_tab_subtree(
                db_v2,
                root_tab,
                None,
                None,
                content_by_tab_id,
                children_by_parent_id,
                counters,
                warnings,
                visited_tab_ids,
            )

        # Sweep for tabs unreachable from any real root — e.g. a tab caught
        # in a tabs_parent_lnk cycle (seen in real cloned data: a self-
        # referencing parent link). Migrate each as its own extra root
        # rather than silently dropping it.
        unreached = [tab for tab in all_tabs if tab.id not in visited_tab_ids]
        for tab in unreached:
            warnings.append(
                f"old tab id={tab.id} document_id={tab.document_id!r} "
                f"title={tab.title!r} was unreachable from any root tab "
                f"(likely a tabs_parent_lnk cycle) — migrated as an extra "
                f"root tab instead of being dropped."
            )
            _migrate_tab_subtree(
                db_v2,
                tab,
                None,
                None,
                content_by_tab_id,
                children_by_parent_id,
                counters,
                warnings,
                visited_tab_ids,
            )

        db_v2.commit()

        print("\nMigration complete.")
        print(f"  tabs:         {counters['tabs']}")
        print(f"  gridstacks:   {counters['gridstacks']}")
        print(f"  components:   {counters['components']}")
        print(f"  page_content: {counters['page_content']}")

        if warnings:
            print(f"\n{len(warnings)} warning(s):")
            for warning in warnings:
                print(f"  - {warning}")

    except Exception:
        db_v2.rollback()
        raise
    finally:
        db_old.close()
        db_v2.close()


if __name__ == "__main__":
    main()
