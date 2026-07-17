import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()


def _resolve_database_url_v2() -> str | URL:
    """
    Resolve the Phase 2 new-schema database connection from environment
    variables. This is a fully separate database from the one configured in
    ``app.database`` (different schema: tabs/gridstacks/components/
    page_content) — see docker-compose.yml's ``db_v2`` service.

    Same two configuration styles as ``app.database._resolve_database_url``,
    just ``_V2``-suffixed so they never collide with the primary
    DATABASE_URL/PG_* config:

    1. A single DATABASE_URL_V2 (preferred).
       Example:
         postgresql+psycopg2://user:pass@host:5433/dbname?sslmode=require

    2. Discrete PG_V2_* parts (libpq-style). Used if DATABASE_URL_V2 is missing.
       Required: PG_V2_HOST, PG_V2_DATABASE, PG_V2_USER, PG_V2_PASSWORD
       Optional: PG_V2_PORT (default 5432), PG_V2_SSLMODE, PG_V2_CONNECT_TIMEOUT

    Raises RuntimeError with a clear message if neither style is configured.
    """
    url = os.getenv("DATABASE_URL_V2")
    if url:
        return url

    host = os.getenv("PG_V2_HOST")
    database = os.getenv("PG_V2_DATABASE")
    user = os.getenv("PG_V2_USER")
    password = os.getenv("PG_V2_PASSWORD")

    if not all([host, database, user, password]):
        raise RuntimeError(
            "The Phase 2 new-schema database is not configured. Set "
            "DATABASE_URL_V2, or provide all of PG_V2_HOST, PG_V2_DATABASE, "
            "PG_V2_USER, PG_V2_PASSWORD (with optional PG_V2_PORT, "
            "PG_V2_SSLMODE, PG_V2_CONNECT_TIMEOUT)."
        )

    port_raw = os.getenv("PG_V2_PORT", "5432")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise RuntimeError(f"PG_V2_PORT must be an integer, got {port_raw!r}") from exc

    query: dict[str, str] = {}
    sslmode = os.getenv("PG_V2_SSLMODE")
    if sslmode:
        query["sslmode"] = sslmode
    connect_timeout = os.getenv("PG_V2_CONNECT_TIMEOUT")
    if connect_timeout:
        query["connect_timeout"] = connect_timeout

    return URL.create(
        drivername="postgresql+psycopg2",
        username=user,
        password=password,
        host=host,
        port=port,
        database=database,
        query=query,
    )


DATABASE_URL_V2 = _resolve_database_url_v2()

# Neon (and most managed Postgres) close idle server-side connections after a
# few minutes. Without ``pool_pre_ping`` the pool hands out a stale socket and
# the next query fails with ``server closed the connection unexpectedly``.
# ``pool_recycle`` proactively refreshes connections older than the value.
# Mirrors the primary engine's config in ``app.database`` — this v2 engine is a
# fully separate connection pool and needs the same safeguards independently.
engine_v2 = create_engine(
    DATABASE_URL_V2,
    pool_pre_ping=True,
    pool_recycle=300,
    connect_args={"connect_timeout": 10, "keepalives": 1, "keepalives_idle": 30,
                  "keepalives_interval": 10, "keepalives_count": 3},
)

SessionLocalV2 = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine_v2,
)

# Deliberately a separate declarative base from app.database.Base: several
# new-schema table names (e.g. "tabs") match old-schema table names, and two
# classes mapped to the same table name under one shared Base/metadata would
# raise InvalidRequestError.
BaseV2 = declarative_base()


def get_db_v2():
    db = SessionLocalV2()
    try:
        yield db
    finally:
        db.close()
