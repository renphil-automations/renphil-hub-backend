"""Create the Stage 1 external-link registry without processing any links."""

from __future__ import annotations

from app.db_v2.database import BaseV2, engine_v2
from app.db_v2.models.component import ComponentV2
from app.db_v2.models.content_link import ComponentLinkMapV2, ContentLinkV2


TARGET_TABLES = [
    ContentLinkV2.__table__,
    ComponentLinkMapV2.__table__,
]


def main() -> None:
    BaseV2.metadata.create_all(
        bind=engine_v2,
        tables=TARGET_TABLES,
        checkfirst=True,
    )
    print("Content link registry tables are ready.")


if __name__ == "__main__":
    main()
