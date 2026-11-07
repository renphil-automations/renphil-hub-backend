"""Reconcile leftover objects in the product-analytics archive bucket.

    python -m scripts.reconcile_product_analytics_archives            # REPORT ONLY: list leftovers, delete nothing
    python -m scripts.reconcile_product_analytics_archives --delete   # delete STAGING leftovers older than the grace period;
                                                                      # classify final-key leftovers, delete none of them

A leftover is an object under `<prefix>/<HUB_ENVIRONMENT>/` whose key is not
in the archive registry (`hub_product_analytics_archives`). The archive job
uploads every batch to a fresh random key under `<prefix>/<env>/_staging/`
and promotes it to its content-addressed final key by a server-side copy; a
staging key is never registered and nothing reads one, so a staging leftover
(an upload the job abandoned — deadline, lost connection, crash — that still
landed) is deleted here after the grace period: R2's DeleteObject is
unconditional (verified 2026-11-04), and for a staging object that does not
matter — whatever is at the key is not an archive. An unregistered object
under a FINAL key (a promotion that landed after the job gave it up, or an
object another writer put there) is reported and classified (redundant, not
redundant, invalid, foreign) and never deleted by this script; the runbook
("Leftover objects") says what an operator may do with it.

Uses the reconciliation identity (`ANALYTICS_ARCHIVE_R2_RECONCILE_*`; list, read, delete —
never put or copy). The token cannot be scoped to the staging segment, so the
staging-only rule is enforced by this code, not by R2.

Exit codes: 0 done; 1 a deletion failed, a registered archive is missing its
object, or a registered key lies under the staging segment (investigate); 2
configuration refused (nothing touched); 3 an archive run held the job lock,
deletion stopped (re-run later; report still printed). Prints keys and counts
only.
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
                        help="delete staging leftovers older than the grace period and classify final-key leftovers "
                             "(objects under final keys are never deleted)")
    parser.add_argument("--grace-hours", type=float, default=archive.RECONCILE_GRACE_HOURS,
                        help=f"never touch an object younger than this (default {archive.RECONCILE_GRACE_HOURS:g})")
    args = parser.parse_args(argv)
    if args.grace_hours < 1 and args.delete:
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
    try:
        storage_timeout = archive.parse_storage_timeout(getattr(settings, "PRODUCT_ANALYTICS_ARCHIVE_STORAGE_TIMEOUT_SECONDS", "240")) if settings is not None else None
    except archive.ArchiveConfigError as exc:
        out(f"reconcile refused: {exc}. Nothing was touched.")
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
                out=out, storage=storage, storage_timeout_seconds=storage_timeout,
            )
        except archive.ArchiveConfigError as exc:
            out(f"reconcile refused: {exc}. Nothing was touched.")
            return EXIT_CONFIG
        except ValueError as exc:
            out(f"reconcile refused: {exc}. Nothing was touched.")
            return EXIT_CONFIG
    mode = "(delete staging leftovers)" if args.delete else "(report only)"
    out(
        f"reconcile {mode} for {environment} under {prefix}/{environment}/: "
        f"{outcome.listed} object(s) listed, {outcome.registered} registered archive(s); "
        f"staging: {outcome.staging_listed} unregistered, {outcome.too_recent} within the grace period, {outcome.candidates} leftover(s), "
        f"deleted {outcome.deleted}; final keys: {len(outcome.leftovers)} unregistered leftover(s) past the grace period (never deleted), "
        f"{outcome.final_too_recent} within the grace period, {outcome.redundant} redundant, {outcome.retained_not_redundant} not redundant, "
        f"{outcome.retained_invalid} invalid; {outcome.retained_foreign} foreign; skipped {outcome.skipped_registered_late} registered late, "
        f"{outcome.skipped_absent} already gone; {outcome.failed} failed; "
        f"{len(outcome.registered_missing)} registered archive(s) missing their object; {len(outcome.registered_staging)} registered under staging."
    )
    if not args.delete:
        out("report only: nothing was deleted.")
    if outcome.failed or outcome.registered_missing or outcome.registered_staging:
        return EXIT_FAILED
    if outcome.locked:
        return EXIT_LOCKED
    return EXIT_OK


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sys.exit(run())
