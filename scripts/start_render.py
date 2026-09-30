import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    env = os.environ.copy()
    env.setdefault("DB_POOL_SIZE", "2")
    env.setdefault("DB_MAX_OVERFLOW", "0")
    env.setdefault("PYTHONUNBUFFERED", "1")
    if env.get("RENDER") == "true":
        env.setdefault("TRUST_PROXY", "1")
    port = int(env.get("PORT", "10000"))
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    web_command = [
        sys.executable,
        "-m",
        "gunicorn",
        "app:app",
        "--config",
        str(ROOT / "scripts" / "gunicorn_conf.py"),
        "--bind",
        f"0.0.0.0:{port}",
        "--worker-class",
        "gthread",
        "--workers",
        "1",
        "--threads",
        "4",
        "--timeout",
        "60",
        "--graceful-timeout",
        "10",
        "--access-logfile",
        "-",
        "--error-logfile",
        "-",
    ]

    os.execvpe(web_command[0], web_command, env)


if __name__ == "__main__":
    raise SystemExit(main())
