"""Archive age-expired Hub product analytics to R2, then delete them.

The scheduled retention job (runbook: PRODUCT_ANALYTICS_ARCHIVE.md):

    python -m scripts.archive_product_analytics --dry-run        # counts only: no storage call, no write
    python -m scripts.archive_product_analytics                  # cleanup + archive-before-delete
    python -m scripts.archive_product_analytics --probe-storage  # read-only HEAD on the bucket, then exit

Order of work in a live run, all for the configured `HUB_ENVIRONMENT` only:

1. Refuse unless the Hub tables have rows (a wrong database looks like
   "everything was deleted"); an environment that has no analytics rows at
   all is not an error, there is nothing it could delete.
2. Delete events and active-time intervals reliably tied to deleted Hub
   resources (deleted, not archived; `--skip-deleted-cleanup` leaves them in
   place, but they are still excluded from new archives).
3. For each table, archive-before-delete rows older than the retention cutoff
   in bounded batches: lock the batch and the Hub rows it depends on, upload
   (or recognise an identical earlier upload), read back and verify, then
   delete exactly those ids (see `product_analytics_archive.archive_expired`).

An object becomes an archive only when the batch's transaction registers it
in `hub_product_analytics_archives` (created by
`scripts/create_product_analytics_archive_tables.py`) and deletes the rows;
`--check-keys FILE` reports which keys of an operator's bucket listing are not
registered (abandoned uploads to reconcile).

Exit codes: 0 done (also "disabled" and "nothing to do"); 1 a batch failed
(that batch and everything after it were neither registered nor deleted);
2 configuration refused (nothing touched); 3 another run holds the batch
lock (this run stops; earlier batches of this run completed normally).
Prints counts, the cutoff date and object keys only, never row contents or
credentials.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.services import product_analytics_archive as archive
from app.services import product_analytics_service as service

logger = logging.getLogger("scripts.archive_product_analytics")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2
EXIT_LOCKED = 3
TABLES = ("events", "engagements")


def _safe_message(exc: BaseException) -> str:
    """The job's own messages and S3 client errors are safe to show; anything
    else is reported by type only so no unexpected payload reaches a log."""
    if isinstance(exc, archive.ArchiveError) or type(exc).__module__.startswith("botocore"):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


def run(
    argv: list[str] | None = None,
    *,
    engine: Any | None = None,
    store: Any | None = None,
    settings: Any | None = None,
    raw_retention: str | None = None,
    environment: str | None = None,
    now: datetime | None = None,
    out: Callable[[str], None] = print,
    storage: tuple[str, str] | None = None,
) -> int:
    """`storage` is only for tests that inject a `store`: the (bucket, prefix)
    the run claims to be configured for, used for the identity binding check."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="count what would be archived and deleted; touch nothing")
    parser.add_argument("--batch-size", type=int, default=5_000, help="rows per archive object and delete transaction (100-50000)")
    parser.add_argument("--max-batches", type=int, default=None, help="stop after this many archive batches per table")
    parser.add_argument("--skip-deleted-cleanup", action="store_true", help="do not delete deleted-resource rows first")
    parser.add_argument("--probe-storage", action="store_true", help="only check that the bucket is reachable (read-only), then exit")
    parser.add_argument("--check-keys", metavar="FILE", default=None,
                        help="read object keys (one per line, from an operator's bucket listing) and report which are not registered archives; no storage call, no write")
    args = parser.parse_args(argv)
    if not 100 <= args.batch_size <= 50_000:
        out("archive refused: --batch-size must be between 100 and 50000. Nothing was deleted or uploaded.")
        return EXIT_CONFIG
    if args.max_batches is not None and args.max_batches < 1:
        out("archive refused: --max-batches must be positive. Nothing was deleted or uploaded.")
        return EXIT_CONFIG

    if settings is None and (raw_retention is None or environment is None or (store is None and not args.dry_run)):
        from app.config import get_settings

        settings = get_settings()
    if raw_retention is None:
        raw_retention = settings.PRODUCT_ANALYTICS_RETENTION_DAYS
    try:
        days = service.parse_retention_days(raw_retention)
    except service.RetentionConfigError as exc:
        out(f"archive refused: {exc} Nothing was deleted or uploaded.")
        return EXIT_CONFIG
    if days is None:
        out("archive disabled: PRODUCT_ANALYTICS_RETENTION_DAYS is off. Nothing was deleted or uploaded.")
        return EXIT_OK
    if environment is None:
        environment = settings.hub_environment
    if environment not in archive.ENVIRONMENTS:
        out("archive refused: HUB_ENVIRONMENT must be local, sandbox, or production. Nothing was deleted or uploaded.")
        return EXIT_CONFIG
    try:
        storage_timeout = archive.parse_storage_timeout(getattr(settings, "PRODUCT_ANALYTICS_ARCHIVE_STORAGE_TIMEOUT_SECONDS", "240")) if settings is not None else None
    except archive.ArchiveConfigError as exc:
        out(f"archive refused: {exc}. Nothing was deleted or uploaded.")
        return EXIT_CONFIG
    # `omit` unless the deployment says `include` explicitly (decided 2026-11-03).
    view_as_policy = str(getattr(settings, "PRODUCT_ANALYTICS_ARCHIVE_VIEW_AS", "omit") or "omit").strip().lower() if settings is not None else "omit"
    if view_as_policy not in archive.VIEW_AS_POLICIES:
        out("archive refused: PRODUCT_ANALYTICS_ARCHIVE_VIEW_AS must be include or omit. Nothing was deleted or uploaded.")
        return EXIT_CONFIG
    exclude_columns = archive.VIEW_AS_COLUMNS if view_as_policy == "omit" else frozenset()

    prefix = "hub-product-analytics"
    if args.check_keys is not None:
        if engine is None:
            from app.db_v2.database import engine_v2

            engine = engine_v2
        with open(args.check_keys, encoding="utf-8") as handle:
            keys = [line.strip() for line in handle if line.strip()]
        with Session(bind=engine) as db:
            try:
                archive.require_archive_registry(db)
            except archive.ArchiveConfigError as exc:
                out(f"archive refused: {exc}. Nothing was deleted or uploaded.")
                return EXIT_CONFIG
            leftovers = archive.unregistered_keys(db, keys)
            for row in archive.registry_summary(db):
                out(f"registry: {row['environment']} {row['table']}: {row['archives']} archive(s), {row['records']} record(s), latest {row['latest']}.")
        out(f"checked {len(keys)} key(s): {len(keys) - len(leftovers)} registered archive(s), {len(leftovers)} unregistered object(s) (not archives; reconcile them).")
        for key in leftovers:
            out(f"unregistered: {key}")
        out("no storage call was made and nothing was deleted.")
        return EXIT_OK
    if store is None:
        try:
            config = archive.R2Config.from_settings(settings)
        except archive.ArchiveConfigError as exc:
            if args.dry_run and not args.probe_storage:
                out(f"dry run: storage configuration is not usable ({exc}); the live job would refuse to run.")
                config = None
            else:
                out(f"archive refused: {exc}. Nothing was deleted or uploaded.")
                return EXIT_CONFIG
        if config is not None:
            prefix = config.prefix
            storage = (config.bucket, config.prefix)
            if args.dry_run and not args.probe_storage:
                out(f"dry run: storage configuration is valid ({config.describe()}); no storage call is made in a dry run.")
            else:
                store = archive.R2ArchiveStore(config)
    if args.probe_storage:
        try:
            store.probe()
        except Exception as exc:  # noqa: BLE001 - reported by type, never raised to the scheduler as a traceback
            out(f"storage probe failed: {_safe_message(exc)}")
            return EXIT_FAILED
        out("storage probe: bucket reachable with the configured credentials. Nothing was deleted or uploaded.")
        return EXIT_OK

    if engine is None:
        from app.db_v2.database import engine_v2

        engine = engine_v2
    cutoff = service.retention_cutoff(days, now)
    with engine.connect() as connection:
        db = Session(bind=connection)
        try:
            # ── Preconditions: everything that can refuse the run happens here,
            # before the first DELETE. A dry run performs only these reads.
            try:
                archive.require_archive_registry(db)
                service.require_hub_resources(db, environment=environment)
                identity, bound = archive.lookup_registry_identity(db)
                db.rollback()
                archive.check_registry_storage_binding(bound, storage)
            except archive.ArchiveConfigError as exc:
                out(f"archive refused: {exc}. Nothing was deleted or uploaded.")
                return EXIT_CONFIG
            except service.CleanupPreconditionError as exc:
                out(f"archive refused: {exc} Nothing was deleted or uploaded.")
                return EXIT_CONFIG
            except Exception as exc:  # noqa: BLE001 - a database that cannot be read is not one to delete from
                db.rollback()
                out(f"archive failed before any deletion: {_safe_message(exc)}. Nothing was deleted or uploaded.")
                return EXIT_FAILED
            if args.dry_run:
                if identity is None:
                    out("dry run: registry identity not initialised; the live job (or python -m scripts.create_product_analytics_archive_tables) initialises it. Nothing was written.")
                else:
                    out(f"dry run: registry identity {identity}" + (f", bound to bucket {bound[0]} prefix {bound[1]}." if bound else " (not yet bound to storage; the live job binds it)."))
                if connection.dialect.name == "postgresql":
                    out("dry run: " + archive.describe_lock_modes(archive.resource_lock_modes(db)))
                    db.rollback()
                for table in TABLES:
                    stale = service.count_deleted_page_records(db, table=table, environment=environment)
                    out(f"dry run: {stale} {table} row(s) reference deleted resources and would be deleted without archiving.")
                    plan = archive.plan_archive(db, table=table, environment=environment, cutoff=cutoff, batch_size=args.batch_size)
                    out(
                        f"dry run: {plan['rows']} {table} row(s) older than {cutoff.date().isoformat()} (retention {days} days) "
                        f"would be archived in {plan['batches']} batch(es) of up to {args.batch_size}, then deleted."
                    )
                out(f"dry run: archives would {'omit' if exclude_columns else 'include'} the view_as_* columns (PRODUCT_ANALYTICS_ARCHIVE_VIEW_AS={view_as_policy}).")
                out("dry run: no archive was uploaded and nothing was deleted.")
                return EXIT_OK
            # The live run's one non-destructive write before any delete: the
            # identity is initialised (first run ever) and bound to this storage.
            try:
                archive.registry_identity(db, storage=storage)
            except archive.ArchiveConfigError as exc:
                db.rollback()
                out(f"archive refused: {exc}. Nothing was deleted or uploaded.")
                return EXIT_CONFIG
            except Exception as exc:  # noqa: BLE001 - e.g. a read-only connection: fail closed before any delete
                db.rollback()
                out(f"archive failed before any deletion: {_safe_message(exc)}. Nothing was deleted or uploaded.")
                return EXIT_FAILED

            # ── Destructive phase. `progress` is what has been committed so
            # far, so any later failure is reported accurately.
            progress: dict[str, Any] = {"cleanup": {}, "archived": {}}

            def committed_so_far() -> str:
                cleanup = ", ".join(f"{progress['cleanup'].get(t, 0)} {t}" for t in TABLES)
                archived = ", ".join(f"{o.deleted} {t} in {o.batches} committed batch(es)" for t, o in progress["archived"].items() if o.batches) or "none"
                return (f"Before this, the cleanup had deleted {cleanup} row(s) referencing deleted resources (not archived); "
                        f"archived and deleted in committed batches: {archived}.")

            if not args.skip_deleted_cleanup:
                # One committed batch per call, so `progress` is exact at the
                # moment any later batch fails: the service commits each batch
                # itself, and a failure inside it would otherwise escape with the
                # earlier commits unaccounted for.
                for table in TABLES:
                    progress["cleanup"][table] = 0
                    batches = 0
                    try:
                        while args.max_batches is None or batches < args.max_batches:
                            removed = service.purge_deleted_page_records(
                                db, table=table, environment=environment, batch_size=args.batch_size, max_batches=1,
                            )
                            if removed == 0:
                                break
                            progress["cleanup"][table] += removed
                            batches += 1
                            if removed < args.batch_size:
                                break
                    except Exception as exc:  # noqa: BLE001 - the failing batch is rolled back; earlier commits stand
                        db.rollback()
                        logger.error("Product analytics cleanup failed for %s: %s", table, _safe_message(exc))
                        out(f"cleanup failed for {table}: {_safe_message(exc)}. The failing batch was rolled back; "
                            f"no archive batch was started. {committed_so_far()}")
                        return EXIT_FAILED
                    out(f"deleted {progress['cleanup'][table]} {table} row(s) referencing deleted resources (not archived).")
            for table in TABLES:
                outcome = archive.ArchiveOutcome(table)
                progress["archived"][table] = outcome
                try:
                    archive.archive_expired(
                        db, store, table=table, environment=environment, cutoff=cutoff, prefix=prefix,
                        batch_size=args.batch_size, max_batches=args.max_batches, exclude_columns=exclude_columns, out=out,
                        storage=storage, outcome=outcome, storage_timeout_seconds=storage_timeout,
                    )
                except archive.ArchiveLockedError:
                    db.rollback()
                    out(f"archive skipped: another archive run holds the batch lock; this run stops. {committed_so_far()}")
                    return EXIT_LOCKED
                except Exception as exc:  # noqa: BLE001 - any failure must stop the job before the next delete
                    db.rollback()
                    logger.error("Product analytics archive failed for %s: %s", table, _safe_message(exc))
                    out(f"archive failed for {table}: {_safe_message(exc)}. The failed batch and everything after it were not registered or deleted. {committed_so_far()}")
                    return EXIT_FAILED
                out(
                    f"archived {outcome.archived} {table} row(s) older than {cutoff.date().isoformat()} in {outcome.batches} "
                    f"batch(es) ({outcome.registered} registered, {outcome.reused} already present); deleted {outcome.deleted}; "
                    f"{outcome.reselected} batch(es) re-selected because a resource was deleted before the batch was locked."
                )
            return EXIT_OK
        finally:
            db.close()
            connection.rollback()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sys.exit(run())
