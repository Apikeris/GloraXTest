import pytest

from glorax.config import configuration


def test_production_requires_secret_postgres_and_verified_tls(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///anything.db")
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        configuration()
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@host.example/db")
    monkeypatch.delenv("PGSSLROOTCERT", raising=False)
    with pytest.raises(RuntimeError, match="CA"):
        configuration()
    ca = tmp_path / "ca.pem"
    ca.write_text("test-placeholder-file")
    monkeypatch.setenv("PGSSLROOTCERT", str(ca))
    config = configuration()
    args = config["SQLALCHEMY_ENGINE_OPTIONS"]["connect_args"]
    assert args["sslmode"] == "verify-full" and args["sslrootcert"] == str(ca)
    assert (
        args["connect_timeout"] == 5
        and args["keepalives"] == 1
        and args["tcp_user_timeout"] == 10000
    )
    assert "statement_timeout=15000" in args["options"] and "lock_timeout=5000" in args["options"]
    assert config["SESSION_COOKIE_SECURE"] and not config["DEBUG"]
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@host.example/db?sslmode=require")
    with pytest.raises(RuntimeError, match="verify-full"):
        configuration()
