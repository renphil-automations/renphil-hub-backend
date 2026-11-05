"""Reconcile leftover objects in the product-analytics archive bucket.

    python -m scripts.reconcile_product_analytics_archives            # REPORT ONLY: list leftovers, delete nothing
    # R2 deletion is disabled; reconciliation is report-only.

A leftover is an object under `<prefix>/<HUB_ENVIRONMENT>/` whose key is not
in the archive registry (`hub_product_analytics_archives`): an upload the
archive job abandoned (deadline, lost connection, crash) that still landed.
It is not an archive and nothing reads it, but its bytes stay in the bucket
until a separate safe cleanup process removes them (runbook: PRODUCT_ANALYTICS_ARCHIVE.md,
"Leftover objects").

Uses the reconciliation identity for read-only inspection. R2 object deletion
is disabled: the service does not enforce conditional DeleteObject, and a
prior HEAD cannot make deletion atomic. Leftover bytes remain in the bucket
until a separate reviewed process is designed and carried out.

Exit codes: 0 done; 1 a deletion failed or a registered archive is missing
its object (investigate); 2 configuration refused (nothing touched); 3 an
archive run held the job lock, deletion stopped (re-run later; report still
printed). Prints keys and counts only.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.services import product_analytics_archive as archive

logger = logging.getLogger("scripts.reconcile_product_analytics_archives")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2
EXIT_LOCKED = 3


def run(
    argv: list[str] | None = None,
    *,
    engine: Any | None = None,
    store: Any | None = None,
    settings: Any | None = None,
    environment: str | None = None,
    now: datetime | None = None,
    out: Callable[[str], None] = print,
    storage: tuple[str, str] | None = None,
) -> int:
    """`storage` is only for tests that inject a `store`: the (bucket, prefix)
    the run claims to be configured for."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--delete", action="store_true",
                        help="unavailable for R2: conditional object deletion is not enforced")
    parser.add_argument("--delete-reviewed", metavar="KEY", nargs="+", default=None,
                        help="unavailable for R2: conditional object deletion is not enforced")
    parser.add_argument("--grace-hours", type=float, default=archive.RECONCILE_GRACE_HOURS,
                        help=f"never touch an object younger than this (default {archive.RECONCILE_GRACE_HOURS:g})")
    args = parser.parse_args(argv)
    if store is None and (args.delete or args.delete_reviewed):
        out("reconcile refused: R2 leftover deletion is disabled because conditional deletion is not enforced. Nothing was touched.")
        return EXIT_CONFIG
    if args.grace_hours < 1 and (args.delete or args.delete_reviewed):
        out("reconcile refused: --grace-hours must be at least 1 when deleting. Nothing was touched.")
        return EXIT_CONFIG

    if settings is None and (environment is None or store is None):
        from app.config import get_settings

        settings = get_settings()
    if environment is None:
        environment = settings.hub_environment
    if environment not in archive.ENVIRONMENTS:
        out("reconcile refused: HUB_ENVIRONMENT must be local, sandbox, or production. Nothing was touched.")
        return EXIT_CONFIG
    prefix = "hub-product-analytics"
    if store is None:
        try:
            config = archive.R2Config.from_settings(settings, credentials=archive.RECONCILE_CREDENTIALS)
        except archive.ArchiveConfigError as exc:
            out(f"reconcile refused: {exc}. Nothing was touched.")
            return EXIT_CONFIG
        prefix = config.prefix
        storage = (config.bucket, config.prefix)
        store = archive.R2ReconcileStore(config)
    elif settings is not None:
        prefix = str(getattr(settings, "PRODUCT_ANALYTICS_ARCHIVE_PREFIX", prefix) or prefix).strip().strip("/")

    if engine is None:
        from app.db_v2.database import engine_v2

        engine = engine_v2
    with Session(bind=engine) as db:
        try:
            outcome = archive.reconcile_leftovers(
                db, store, environment=environment, prefix=prefix, now=now, grace_hours=args.grace_hours, delete=args.delete,
                reviewed_keys=args.delete_reviewed, out=out, storage=storage,
            )
        except archive.ArchiveConfigError as exc:
            out(f"reconcile refused: {exc}. Nothing was touched.")
            return EXIT_CONFIG
        except ValueError as exc:
            out(f"reconcile refused: {exc}. Nothing was touched.")
            return EXIT_CONFIG
    mode = "(delete reviewed keys)" if args.delete_reviewed else "(delete redundant)" if args.delete else "(report only)"
    out(
        f"reconcile {mode} for {environment} under {prefix}/{environment}/: "
        f"{outcome.listed} object(s) listed, {outcome.registered} registered archive(s), {outcome.too_recent} within the grace period, "
        f"{outcome.candidates} leftover(s); deleted {outcome.deleted}; retained {outcome.retained_not_redundant} not redundant, "
        f"{outcome.retained_foreign} foreign, {outcome.retained_invalid} invalid; skipped {outcome.skipped_registered_late} registered late, "
        f"{outcome.skipped_changed} changed, {outcome.skipped_absent} already gone; {outcome.failed} failed; "
        f"{len(outcome.registered_missing)} registered archive(s) missing their object."
    )
    if not args.delete and not args.delete_reviewed:
        out("report only: nothing was deleted. R2 leftover deletion is disabled.")
    if outcome.failed or outcome.registered_missing:
        return EXIT_FAILED
    if outcome.locked:
        return EXIT_LOCKED
    return EXIT_OK


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sys.exit(run())
