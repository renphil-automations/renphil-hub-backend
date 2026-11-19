"""Add the Resolved / Unresolved status to Hub and Bot feedback. Additive and safe to run more than once.

Run it against the intended database BEFORE deploying the backend that lets people change a status:
    python -m scripts.add_feedback_status_columns --expected-host <host> --expected-database <name>
Existing feedback becomes Unresolved. Nothing is deleted or rewritten. The two --expected flags are required so the
script cannot be pointed at the wrong database by accident.
"""
import argparse

from sqlalchemy import text

from app.db_v2.database import engine_v2

TABLES = ("hub_feedback", "bot_feedback")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--expected-host", required=True)
    parser.add_argument("--expected-database", required=True)
    args = parser.parse_args()
    url = engine_v2.url
    if url.host != args.expected_host or url.database != args.expected_database:
        raise SystemExit(f"Refusing to run: connected to {url.host}/{url.database}, expected {args.expected_host}/{args.expected_database}.")
    with engine_v2.begin() as connection:
        for table in TABLES:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'unresolved'"))
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS status_updated_at TIMESTAMPTZ"))
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS status_updated_by_user_id INTEGER"))
            connection.execute(text(f"""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_{table}_status') THEN
                        ALTER TABLE {table} ADD CONSTRAINT ck_{table}_status CHECK (status IN ('unresolved', 'resolved'));
                    END IF;
                END $$;"""))
    print("Feedback status columns are ready on", f"{url.host}/{url.database}")


if __name__ == "__main__":
    main()
