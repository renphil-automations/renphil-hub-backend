import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()


def _resolve_database_url() -> str | URL:
    """
    Resolve the database connection from environment variables.

    Two configuration styles are supported:

    1. A single DATABASE_URL (preferred — standard SQLAlchemy / Vercel / Neon).
       Example:
         postgresql+psycopg2://user:pass@host:5432/dbname?sslmode=require

    2. Discrete PG_* parts (libpq-style). Used if DATABASE_URL is missing.
       Required: PG_HOST, PG_DATABASE, PG_USER, PG_PASSWORD
       Optional: PG_PORT (default 5432), PG_SSLMODE, PG_CONNECT_TIMEOUT

    Raises RuntimeError with a clear message if neither style is configured.
    """
    url = os.getenv("DATABASE_URL")
    if url:
        return url

    host = os.getenv("PG_HOST")
    database = os.getenv("PG_DATABASE")
    user = os.getenv("PG_USER")
    password = os.getenv("PG_PASSWORD")

    if not all([host, database, user, password]):
        raise RuntimeError(
            "Database is not configured. Set DATABASE_URL, or provide all of "
            "PG_HOST, PG_DATABASE, PG_USER, PG_PASSWORD (with optional PG_PORT, "
            "PG_SSLMODE, PG_CONNECT_TIMEOUT). On Vercel, configure these under "
            "Settings → Environment Variables and redeploy."
        )

    port_raw = os.getenv("PG_PORT", "5432")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise RuntimeError(f"PG_PORT must be an integer, got {port_raw!r}") from exc

    query: dict[str, str] = {}
    sslmode = os.getenv("PG_SSLMODE")
    if sslmode:
        query["sslmode"] = sslmode
    connect_timeout = os.getenv("PG_CONNECT_TIMEOUT")
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


DATABASE_URL = _resolve_database_url()

# Neon (and most managed Postgres) close idle server-side connections after a
# few minutes. Without ``pool_pre_ping`` the pool hands out a stale socket and
# the next query fails with ``SSL connection has been closed unexpectedly``.
# ``pool_recycle`` proactively refreshes connections older than the value.
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    connect_args={"connect_timeout": 10, "keepalives": 1, "keepalives_idle": 30,
                  "keepalives_interval": 10, "keepalives_count": 3},
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()