"""Preview a reviewed Airtable snapshot; --apply explicitly writes Neon.

Example: python -m scripts.import_feedback_snapshot snapshot.json --environment sandbox
No live Airtable call or deletion is performed. Existing source ids are skipped.
"""
import argparse
import json
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from app.services.feedback_import_service import FeedbackImportError, prepare_snapshot, import_prepared


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--environment", required=True, choices=["local", "sandbox", "production", "unknown"])
    parser.add_argument("--missing-source", choices=["hub", "bot"], help="Only after explicit review of blank-source records")
    parser.add_argument("--skip-ambiguous", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Write to the configured V2 database; default is dry-run")
    parser.add_argument("--expected-host", help="Required with --apply; exact reviewed database endpoint")
    parser.add_argument("--expected-database", help="Required with --apply; exact reviewed database name")
    args = parser.parse_args(argv)
    try:
        if args.snapshot.stat().st_size > 20 * 1024 * 1024:
            raise FeedbackImportError("Snapshot exceeds 20 MB.")
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8-sig"))
        records = snapshot.get("records") if isinstance(snapshot, dict) else snapshot
        from app.config import get_settings
        settings = get_settings()
        fields = {key: getattr(settings, attr) for key, attr in {
            "source": "AT_F_FEEDBACK_SOURCE", "message": "AT_F_FEEDBACK_MESSAGE", "email": "AT_F_FEEDBACK_FROM",
            "impression": "AT_F_FEEDBACK_IMPRESSION", "message_id": "AT_F_FEEDBACK_MESSAGE_ID",
            "query": "AT_F_FEEDBACK_QUERY", "response": "AT_F_FEEDBACK_RESPONSE"}.items()}
        rows, ambiguous = prepare_snapshot(records, environment=args.environment, missing_source=args.missing_source,
                                           skip_ambiguous=args.skip_ambiguous, fields=fields)
        print(f"Validated snapshot: hub={sum(kind == 'hub' for kind, _ in rows)}, bot={sum(kind == 'bot' for kind, _ in rows)}, skipped_ambiguous={ambiguous}")
        if not args.apply:
            print("Dry run: no database changes. Existing-source reconciliation occurs only with --apply.")
            return 0
        # Prevent a typo from importing sandbox rows into a production runtime.
        if settings.hub_environment not in {"local", args.environment}:
            raise FeedbackImportError("Snapshot environment differs from configured Hub environment.")
        from app.db_v2.database import SessionLocalV2, engine_v2
        if not args.expected_host or not args.expected_database:
            raise FeedbackImportError("Applying requires --expected-host and --expected-database after reviewing the target.")
        if engine_v2.url.host != args.expected_host or engine_v2.url.database != args.expected_database:
            raise FeedbackImportError("Configured database does not match the reviewed target; no rows imported.")
        with SessionLocalV2() as db:
            counts = import_prepared(db, rows)
        print("Imported: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
        return 0
    except FeedbackImportError as exc:
        print(str(exc))
        return 2
    except (OSError, ValueError, SQLAlchemyError) as exc:
        print(f"Import failed safely: {type(exc).__name__}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
