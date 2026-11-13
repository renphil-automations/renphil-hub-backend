"""Bring ``form_details`` to the shape ``app.db_v2.models.form_details`` expects.

Idempotent: creates the table if it doesn't exist, otherwise upgrades an
existing one (adds missing columns and the ``component_id`` uniqueness, and
drops the retired webhook columns/index if they're still there). Safe to
re-run.

    python -m scripts.migrate_form_details_table
"""

from __future__ import annotations

from sqlalchemy import text

from app.db_v2.database import BaseV2, engine_v2
from app.db_v2.models.component import ComponentV2  # noqa: F401  (FK target)
from app.db_v2.models.form_details import FormDetailsV2

UPGRADE_STATEMENTS = [
    "ALTER TABLE public.form_details ADD COLUMN IF NOT EXISTS embed_url TEXT",
    "ALTER TABLE public.form_details ADD COLUMN IF NOT EXISTS pat TEXT",
    "ALTER TABLE public.form_details ADD COLUMN IF NOT EXISTS requires_authentication BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE public.form_details ADD COLUMN IF NOT EXISTS authentication_type VARCHAR",
    "ALTER TABLE public.form_details ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    "UPDATE public.form_details SET created_at = now() WHERE created_at IS NULL",
    "ALTER TABLE public.form_details ALTER COLUMN created_at SET DEFAULT now()",
    "ALTER TABLE public.form_details ALTER COLUMN created_at SET NOT NULL",
    # Fail (and roll the whole upgrade back) if a pre-existing row has no
    # embed_url / pat — fill those in first.
    "ALTER TABLE public.form_details ALTER COLUMN embed_url SET NOT NULL",
    "ALTER TABLE public.form_details ALTER COLUMN pat SET NOT NULL",
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
            WHERE i.indrelid = 'public.form_details'::regclass
              AND i.indisunique AND i.indnatts = 1 AND a.attname = 'component_id'
        ) THEN
            ALTER TABLE public.form_details
                ADD CONSTRAINT form_details_component_id_key UNIQUE (component_id);
        END IF;
    END $$;
    """,
    # Retired: the status webhook is no longer stored per form.
    "DROP INDEX IF EXISTS public.form_details_webhook_token_hash_key",
    "ALTER TABLE public.form_details DROP COLUMN IF EXISTS webhook",
    "ALTER TABLE public.form_details DROP COLUMN IF EXISTS webhook_url",
    "ALTER TABLE public.form_details DROP COLUMN IF EXISTS webhook_token_hash",
    "ALTER TABLE public.form_details DROP COLUMN IF EXISTS webhook_token_updated_at",
]


def main() -> None:
    with engine_v2.begin() as conn:
        BaseV2.metadata.create_all(bind=conn, tables=[FormDetailsV2.__table__], checkfirst=True)
        for statement in UPGRADE_STATEMENTS:
            conn.execute(text(statement))
    print("form_details table is ready.")


if __name__ == "__main__":
    main()
