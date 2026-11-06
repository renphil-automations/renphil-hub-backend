"""Restore one registered product-analytics archive into the live tables.

    python -m scripts.restore_product_analytics_archive --key <registered key> --dry-run
    python -m scripts.restore_product_analytics_archive --key <registered key>
    python -m scripts.restore_product_analytics_archive --key <registered key> --include-deleted-resources

Starts from the registry, never from the bucket: the key must be a registered
archive of this `HUB_ENVIRONMENT`; the object is downloaded and must have the
registered SHA-256, validate as a supported archive format and carry the
registered environment and record count. Only then are its JSONL records
inserted (`product_analytics_archive.restore_registered_archive`): a record
already present is left untouched (idempotent), columns the archive omitted
(for example the View As diagnostics under the default policy) come back NULL
and cannot be recovered, and records that now match the deleted-resource rule
are removed again in the same transaction unless `--include-deleted-resources`
is given (an explicit recovery mode that puts analytics of deleted pages back
into active data until the next cleanup). A refusal writes nothing; `--dry-run`
performs every check and the download and writes nothing.

Uses the writer identity's read access (`GetObject`); needs no list or delete.
Exit codes: 0 restored (or dry run passed); 1 refused — nothing written;
2 configuration refused. Prints counts and the key only.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.services import product_analytics_archive as archive

logger = logging.getLogger("scripts.restore_product_analytics_archive")

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_CONFIG = 2


def run(
    argv: list[str] | None = None,
    *,
    engine: Any | None = None,
    store: Any | None = None,
    settings: Any | None = None,
    environment: str | None = None,
    out: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--key", required=True, help="the registered archive key to restore")
    parser.add_argument("--dry-run", action="store_true", help="verify the registry, the object and the archive; write nothing")
    parser.add_argument("--include-deleted-resources", action="store_true",
                        help="keep records whose Hub resource no longer exists (explicit recovery mode)")
    args = parser.parse_args(argv)

    if settings is None and (environment is None or store is None):
        from app.config import get_settings

        settings = get_settings()
    if environment is None:
        environment = settings.hub_environment
    if environment not in archive.ENVIRONMENTS:
        out("restore refused: HUB_ENVIRONMENT must be local, sandbox, or production. Nothing was written.")
        return EXIT_CONFIG
    if store is None:
        try:
            store = archive.R2ArchiveStore(archive.R2Config.from_settings(settings))
        except archive.ArchiveConfigError as exc:
            out(f"restore refused: {exc}. Nothing was written.")
            return EXIT_CONFIG
    if engine is None:
        from app.db_v2.database import engine_v2

        engine = engine_v2
    with Session(bind=engine) as db:
        try:
            archive.require_archive_registry(db)
            result = archive.restore_registered_archive(
                db, store, key=args.key, environment=environment,
                include_deleted_resources=args.include_deleted_resources, dry_run=args.dry_run,
            )
        except archive.ArchiveConfigError as exc:
            out(f"restore refused: {exc}. Nothing was written.")
            return EXIT_CONFIG
        except (archive.ArchiveRestoreError, archive.ArchiveIntegrityError) as exc:
            db.rollback()
            out(f"restore refused: {exc}. Nothing was written.")
            return EXIT_REFUSED
        except Exception as exc:  # noqa: BLE001 - reported by type; the transaction is rolled back
            db.rollback()
            out(f"restore failed: {type(exc).__name__}. Nothing was written.")
            return EXIT_REFUSED
        if args.dry_run:
            db.rollback()
            out(f"dry run: {result['key']} is a registered {result['table']} archive of {result['records']} record(s), "
                f"format v{result['format_version']}, sha256 {result['sha256'][:16]}; the object matches the registry and validates. Nothing was written.")
            return EXIT_OK
        db.commit()
    table = result["table"]
    out(
        f"restored {result['inserted'][table]} {table} record(s) from {result['key']}; "
        f"{result['already_present'][table]} already present; "
        f"{result['deleted_resource_skipped'][table]} skipped because their Hub resource no longer exists"
        f"{' (kept: --include-deleted-resources)' if args.include_deleted_resources else ''}."
    )
    return EXIT_OK


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sys.exit(run())
