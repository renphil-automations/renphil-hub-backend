"""Create the Stage 1 external-link registry without processing any links."""

from __future__ import annotations

import sys
from pathlib import Path

# Allow the documented direct invocation:
# ``python scripts/create_content_link_tables.py``.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.db_v2.database import BaseV2, engine_v2
from sqlalchemy import text
from app.db_v2.external_worker_schema import CONTENT_LINK_SCHEMA_STATEMENTS
from app.db_v2.models.component import ComponentV2
from app.db_v2.models.content_link import (
    ComponentLinkMapV2,
    ContentLinkEventV2,
    ContentLinkV2,
    ExternalWorkerRunV2,
)


TARGET_TABLES = [
    ContentLinkV2.__table__,
    ComponentLinkMapV2.__table__,
    ContentLinkEventV2.__table__,
    ExternalWorkerRunV2.__table__,
]


def main() -> None:
    BaseV2.metadata.create_all(
        bind=engine_v2,
        tables=TARGET_TABLES,
        checkfirst=True,
    )
    # `create_all` does not alter existing tables. Every statement is
    # idempotent and shared with the Alembic revision 0002_external_worker.
    # Apply before deploying a worker version that reads these columns.
    with engine_v2.begin() as connection:
        for statement in CONTENT_LINK_SCHEMA_STATEMENTS:
            connection.execute(text(statement))
    print("Content link registry tables are ready.")


if __name__ == "__main__":
    main()
