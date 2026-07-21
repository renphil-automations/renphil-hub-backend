from __future__ import annotations

import os
import unittest
from copy import deepcopy

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Importing the v2 models creates the application's normal Postgres engine,
# but this focused test never connects to it. These defaults only make the
# module importable when a developer runs the test without a local .env.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://validation:validation@localhost/validation",
)
os.environ.setdefault(
    "DATABASE_URL_V2",
    "postgresql+psycopg2://validation:validation@localhost/validation_v2",
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2  # noqa: E402
from app.db_v2.models.component import ComponentV2  # noqa: E402
from app.db_v2.models.gridstack import GridstackV2  # noqa: E402
from app.db_v2.models.page_content import PageContentV2  # noqa: E402
from app.db_v2.models.tab import TabV2  # noqa: E402
from app.services.gridstack_service import (  # noqa: E402
    _create_gridstack_component,
    get_tab_content_v2,
    reorder_tabs_by_document_id_v2,
    update_tab_content_v2,
)


class ExactSearchUpdateReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        BaseV2.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

        tab = TabV2(
            document_id="root-doc",
            title="Root",
            order=0,
            access_control={},
            locked=False,
            locked_by="",
        )
        self.db.add(tab)
        self.db.flush()
        gridstack = GridstackV2(
            document_id="root-doc",
            name="Root",
            settings={},
            position=0,
            parent_id=None,
            parent_tab_id=tab.id,
        )
        self.db.add(gridstack)
        self.db.flush()
        _create_gridstack_component(self.db, gridstack)

        self.components: list[ComponentV2] = []
        for index in range(4):
            page_content = PageContentV2(
                content={"markdown": f"content {index + 1}"}
            )
            self.db.add(page_content)
            self.db.flush()
            component = ComponentV2(
                link=f"component-link-{index + 1}",
                type="markdown",
                title=f"Markdown {index + 1}",
                description=None,
                x=index,
                y=0,
                width=3,
                height=3,
                props={},
                access_control={},
                current_grid_id=None,
                gridstack_id=gridstack.id,
                page_content_id=page_content.id,
                super_blocknote_id=None,
            )
            self.db.add(component)
            self.db.flush()
            self.components.append(component)
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_exact_receipts_across_content_save_cases(self) -> None:
        baseline = get_tab_content_v2(self.db, "root-doc")
        self.assertIsNotNone(baseline)

        one_changed = deepcopy(baseline["content"])
        first_id = self.components[0].id
        one_changed["widgets"][str(first_id)]["data"]["markdown"] += " test"
        first_result = update_tab_content_v2(self.db, "root-doc", one_changed)
        self.assertEqual(
            first_result["search_updates"],
            [{"component_id": first_id, "action": "upsert"}],
        )

        unchanged_result = update_tab_content_v2(
            self.db,
            "root-doc",
            deepcopy(first_result["content"]),
        )
        self.assertEqual(unchanged_result["search_updates"], [])

        two_changed = deepcopy(unchanged_result["content"])
        changed_ids = [self.components[1].id, self.components[2].id]
        for component_id in changed_ids:
            two_changed["widgets"][str(component_id)]["data"]["markdown"] += (
                " changed"
            )
        two_result = update_tab_content_v2(self.db, "root-doc", two_changed)
        self.assertEqual(
            two_result["search_updates"],
            [
                {"component_id": changed_ids[0], "action": "upsert"},
                {"component_id": changed_ids[1], "action": "upsert"},
            ],
        )

        deleted_id = self.components[3].id
        deleted = deepcopy(two_result["content"])
        deleted["widgets"].pop(str(deleted_id))
        deleted["layout"] = [
            item for item in deleted["layout"] if item["id"] != str(deleted_id)
        ]
        delete_result = update_tab_content_v2(self.db, "root-doc", deleted)
        self.assertEqual(
            delete_result["search_updates"],
            [{"component_id": deleted_id, "action": "delete"}],
        )

        reordered = reorder_tabs_by_document_id_v2(
            self.db,
            [{"documentId": "root-doc", "order": 1}],
        )
        self.assertFalse(reordered[0].get("search_updates"))


if __name__ == "__main__":
    unittest.main()
