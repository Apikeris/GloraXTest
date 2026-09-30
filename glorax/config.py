import os
from datetime import timedelta
from pathlib import Path

from sqlalchemy.engine import make_url


def postgres_connection_args():
    return {
        "connect_timeout": 5,
        "keepalives": 1,
        "keepalives_idle": 20,
        "keepalives_interval": 5,
        "keepalives_count": 3,
        "tcp_user_timeout": 10000,
        "options": "-c timezone=UTC -c statement_timeout=15000 -c lock_timeout=5000 -c idle_in_transaction_session_timeout=20000",
    }


def configuration():
    production = os.getenv("APP_ENV", "development") == "production"
    secret = os.getenv("SECRET_KEY")
    if not secret or len(secret) < 32:
        raise RuntimeError("SECRET_KEY отсутствует или короче 32 символов.")
    url = os.getenv("DATABASE_URL", "sqlite:///glorax-v2.db")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    options = {"pool_pre_ping": True, "hide_parameters": True}
    if url.startswith("postgresql"):
        options.update(
            pool_size=int(os.getenv("DB_POOL_SIZE", "3")),
            max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "1")),
            pool_recycle=300,
            pool_timeout=5,
            connect_args=postgres_connection_args(),
        )
    if production:
        parsed = make_url(url)
        ca = os.getenv("PGSSLROOTCERT") or parsed.query.get("sslrootcert")
        if not url.startswith("postgresql"):
            raise RuntimeError("Production требует PostgreSQL.")
        if not ca or not Path(ca).is_file():
            raise RuntimeError("CA-файл PGSSLROOTCERT не найден.")
        if parsed.query.get("sslmode") not in (None, "verify-full"):
            raise RuntimeError("В production разрешён только sslmode=verify-full.")
        options["connect_args"].update(sslmode="verify-full", sslrootcert=str(ca))
    return dict(
        SECRET_KEY=secret,
        SQLALCHEMY_DATABASE_URI=url,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS=options,
        SESSION_COOKIE_SECURE=production,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        WTF_CSRF_TIME_LIMIT=8 * 60 * 60,
        DEBUG=False,
        PRODUCTION=production,
        DISPLAY_TIMEZONE=os.getenv("DISPLAY_TIMEZONE", "Europe/Moscow"),
        WORKER_LEASE_SECONDS=int(os.getenv("WORKER_LEASE_SECONDS", "600")),
        WORKER_MAX_ATTEMPTS=int(os.getenv("WORKER_MAX_ATTEMPTS", "3")),
        TRUST_PROXY=os.getenv("TRUST_PROXY", "0") == "1",
    )
