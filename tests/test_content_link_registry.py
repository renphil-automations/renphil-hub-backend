from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://validation:validation@localhost/validation")
os.environ.setdefault("DATABASE_URL_V2", "postgresql+psycopg2://validation:validation@localhost/validation_v2")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2  # noqa: E402
from app.db_v2.models.component import ComponentV2  # noqa: E402
from app.db_v2.models.content_link import ComponentLinkMapV2, ContentLinkV2  # noqa: E402
from app.db_v2.models.gridstack import GridstackV2  # noqa: E402
from app.db_v2.models.nav_tab import NavTabV2  # noqa: E402
from app.db_v2.models.page_content import PageContentV2  # noqa: E402
from app.db_v2.models.tab import TabV2  # noqa: E402
from app.services.content_link_registry import (  # noqa: E402
    mark_component_links_deleted,
    mark_components_acl_refresh,
    sync_component_links,
)


class ContentLinkRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        BaseV2.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        tab = TabV2(document_id="tab", title="Tab", order=0, access_control={}, locked=False, locked_by="")
        self.db.add(tab); self.db.flush()
        grid = GridstackV2(document_id="tab", name="Tab", settings={}, position=0, parent_id=None, parent_tab_id=tab.id)
        self.db.add(grid); self.db.flush()
        page = PageContentV2(content={"blocks": [{"href": "https://docs.google.com/document/d/abc/edit#heading"}, {"nested": "https://example.org/guide.pdf"}]})
        self.db.add(page); self.db.flush()
        self.component = ComponentV2(link="component", type="block_note", title="Guide", description="https://example.org/guide.pdf", props={}, access_control={}, gridstack_id=grid.id, page_content_id=page.id)
        self.db.add(self.component); self.db.flush()

    def tearDown(self) -> None:
        self.db.close()

    def test_sync_deduplicates_nested_links_and_soft_deletes_removed_component(self) -> None:
        self.assertEqual(sync_component_links(self.db, self.component), 2)
        self.db.flush()
        rows = self.db.query(ContentLinkV2).order_by(ContentLinkV2.normalized_url).all()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].normalized_url, "https://docs.google.com/document/d/abc/edit")
        self.assertEqual(self.db.query(ComponentLinkMapV2).count(), 2)
        self.assertEqual(mark_component_links_deleted(self.db, self.component.id), 2)
        self.db.flush()
        self.db.expire_all()
        rows = self.db.query(ContentLinkV2).all()
        self.assertTrue(all(row.deleted for row in rows))

    def test_shared_link_is_retained_until_its_last_component_is_deleted(self) -> None:
        second_page = PageContentV2(content={"href": "https://example.org/guide.pdf#download"})
        self.db.add(second_page)
        self.db.flush()
        second_component = ComponentV2(
            link="component-two",
            type="block_note",
            title="Second guide",
            description="",
            props={},
            access_control={},
            gridstack_id=self.component.gridstack_id,
            page_content_id=second_page.id,
        )
        self.db.add(second_component)
        self.db.flush()

        self.assertEqual(sync_component_links(self.db, self.component), 2)
        self.assertEqual(sync_component_links(self.db, second_component), 1)
        self.assertEqual(self.db.query(ContentLinkV2).count(), 2)
        self.assertEqual(self.db.query(ComponentLinkMapV2).count(), 3)

        self.assertEqual(mark_component_links_deleted(self.db, self.component.id), 2)
        self.db.flush()
        self.db.expire_all()
        shared_link = self.db.query(ContentLinkV2).filter_by(normalized_url="https://example.org/guide.pdf").one()
        self.assertFalse(shared_link.deleted)
        self.assertEqual(self.db.query(ComponentLinkMapV2).count(), 1)

        self.assertEqual(mark_component_links_deleted(self.db, second_component.id), 1)
        self.db.flush()
        self.db.expire_all()
        self.assertTrue(self.db.query(ContentLinkV2).filter_by(id=shared_link.id).one().deleted)

    def test_ignores_only_configured_hub_hosts(self) -> None:
        self.component.description = (
            "https://hub.example/dashboard/finance "
            "https://external.example/reference.pdf"
        )

        with patch(
            "app.services.content_link_registry._internal_hosts",
            return_value={"hub.example"},
        ):
            self.assertEqual(sync_component_links(self.db, self.component), 3)

        urls = {
            row.normalized_url for row in self.db.query(ContentLinkV2).all()
        }
        self.assertNotIn("https://hub.example/dashboard/finance", urls)
        self.assertIn("https://external.example/reference.pdf", urls)

    def test_acl_refresh_marks_only_active_links_for_affected_components(self) -> None:
        sync_component_links(self.db, self.component)
        self.db.flush()
        links = self.db.query(ContentLinkV2).all()
        for link in links:
            link.acl_refresh_required = False
        self.db.flush()

        self.assertEqual(mark_components_acl_refresh(self.db, [self.component.id]), 2)
        self.db.flush()
        self.db.expire_all()
        self.assertTrue(all(link.acl_refresh_required for link in self.db.query(ContentLinkV2).all()))

        mark_component_links_deleted(self.db, self.component.id)
        self.db.flush()
        self.assertEqual(mark_components_acl_refresh(self.db, [self.component.id]), 0)
