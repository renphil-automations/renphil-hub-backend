"""Create the Hub product-analytics archive registry table safely (idempotent).

`hub_product_analytics_archives` records every archive object the retention
job (`scripts/archive_product_analytics.py`) has committed; an object in the
bucket is an archive only if its key is registered here. Run once per
environment before scheduling the job; the job refuses to run without it.

The identity row (`hub_product_analytics_archive_identity`) is created once,
atomically, and never rotates on its own. `--rebind-storage` is the only
operator action here: it binds the identity to the bucket/prefix in the
current settings, for a database that was cloned or restored and then
deliberately pointed at other storage (runbook, "Registry identity").
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy.orm import Session

from app.db_v2.database import BaseV2, engine_v2
from app.db_v2.models.product_analytics import HubProductAnalyticsArchiveIdentityV2, HubProductAnalyticsArchiveV2
from app.services.product_analytics_archive import R2Config, rebind_registry_identity_storage, registry_identity

TARGET_TABLES = [HubProductAnalyticsArchiveV2.__table__, HubProductAnalyticsArchiveIdentityV2.__table__]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rebind-storage", action="store_true",
                        help="bind the registry identity to the bucket/prefix in the current settings (operator action; the id is kept)")
    args = parser.parse_args(argv)
    print(f"Connecting to: {engine_v2.url.render_as_string(hide_password=True)}")
    BaseV2.metadata.create_all(bind=engine_v2, tables=TARGET_TABLES, checkfirst=True)
    for table in TARGET_TABLES:
        for index in table.indexes:
            index.create(bind=engine_v2, checkfirst=True)
    with Session(bind=engine_v2) as db:
        if args.rebind_storage:
            from app.config import get_settings

            config = R2Config.from_settings(get_settings())
            identity = rebind_registry_identity_storage(db, bucket=config.bucket, prefix=config.prefix)
            print(f"registry identity {identity} is now bound to bucket {config.bucket} prefix {config.prefix}.")
        else:
            identity = registry_identity(db)
    print(f"Hub product analytics archive registry tables are ready; registry identity {identity}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
