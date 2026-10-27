"""Delete Hub product-usage events older than PRODUCT_ANALYTICS_RETENTION_DAYS.

Run once a day in every deployment that records events (see
PRODUCT_ANALYTICS_EVENTS.md, "Retention"):

    python -m scripts.purge_product_analytics_events            # delete
    python -m scripts.purge_product_analytics_events --dry-run  # count only

* Deletes only `hub_product_events` rows whose `occurred_at` is older than the
  retention period, `--batch-size` rows per transaction (default 5,000).
* Prints counts and the cutoff date only, never row contents.
* `PRODUCT_ANALYTICS_RETENTION_DAYS`: unset = 396 days; `0`/`off` = purging
  disabled (exit 0, nothing deleted); anything else outside 30–3650 days is
  refused (exit 2, nothing deleted).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from typing import Callable

from app.services import product_analytics_service as service

EXIT_OK = 0
EXIT_CONFIG = 2


def run(
    argv: list[str] | None = None,
    *,
    session_factory: Callable | None = None,
    raw_retention: str | None = None,
    now: datetime | None = None,
    out: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="count what would be deleted; delete nothing")
    parser.add_argument("--batch-size", type=int, default=5_000, help="rows per delete transaction (100-50000)")
    parser.add_argument("--max-batches", type=int, default=None, help="stop after this many batches")
    args = parser.parse_args(argv)
    if not 100 <= args.batch_size <= 50_000:
        out("purge refused: --batch-size must be between 100 and 50000")
        return EXIT_CONFIG

    if raw_retention is None:
        from app.config import get_settings

        raw_retention = get_settings().PRODUCT_ANALYTICS_RETENTION_DAYS
    try:
        days = service.parse_retention_days(raw_retention)
    except service.RetentionConfigError as exc:
        out(f"purge refused: {exc} Nothing was deleted.")
        return EXIT_CONFIG
    if days is None:
        out("purge disabled: PRODUCT_ANALYTICS_RETENTION_DAYS is off. Nothing was deleted.")
        return EXIT_OK

    if session_factory is None:
        from app.db_v2.database import SessionLocalV2

        session_factory = SessionLocalV2
    cutoff = service.retention_cutoff(days, now)
    with session_factory() as db:
        if args.dry_run:
            count = service.count_expired_events(db, cutoff=cutoff)
            out(f"dry run: {count} event(s) older than {cutoff.date().isoformat()} (retention {days} days) would be deleted.")
            return EXIT_OK
        deleted = service.purge_expired_events(
            db, cutoff=cutoff, batch_size=args.batch_size, max_batches=args.max_batches,
        )
    out(f"deleted {deleted} event(s) older than {cutoff.date().isoformat()} (retention {days} days).")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(run())
