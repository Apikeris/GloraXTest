from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from sqlalchemy import text
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import configuration
from .extensions import csrf, db, migrate


def create_app(test_config=None):
    load_dotenv()
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    if test_config:
        app.config.update(
            SECRET_KEY="test-key-only-never-production-0123456789",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            MAX_CONTENT_LENGTH=2 * 1024 * 1024,
            DISPLAY_TIMEZONE="Europe/Moscow",
            WORKER_LEASE_SECONDS=600,
            WORKER_MAX_ATTEMPTS=3,
        )
        app.config.update(test_config)
    else:
        app.config.update(configuration())
    if app.config.get("TRUST_PROXY"):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    db.init_app(app)
    migrate.init_app(app, db, compare_type=True)
    csrf.init_app(app)
    from . import models
    from .admin import bp as admin_bp
    from .attempts import bp as public_bp
    from .auth import bp as auth_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp)
    from .cli import register_cli

    register_cli(app)

    @app.template_filter("localtime")
    def localtime(value):
        return (
            models.aware(value)
            .astimezone(ZoneInfo(app.config["DISPLAY_TIMEZONE"]))
            .strftime("%d.%m.%Y %H:%M")
            if value
            else "Неизвестно"
        )

    @app.template_filter("ru_status")
    def ru_status(value):
        return {
            "in_progress": "Идёт",
            "completed": "Завершена",
            "interrupted": "Прервана",
            "draft": "Черновик",
            "published": "Опубликован",
            "archived": "Архив",
            "needs_review": "На проверке",
            "verified": "Подтверждён",
            "legacy_unverified": "Старые непроверенные данные",
            "correct": "Верно",
            "wrong": "Ошибка",
            "timeout": "Время истекло",
            "not_reached": "Не открыт",
            "legacy_unknown": "Недостаточно старых данных",
            "queued": "В очереди",
            "running": "Выполняется",
            "succeeded": "Завершено",
            "failed": "Ошибка",
        }.get(value, value or "Нет данных")

    @app.get("/healthz")
    def health():
        # Liveness checks must prove the HTTP worker can answer. Do not make
        # Render's router depend on Aiven: a transient database outage would
        # otherwise make an otherwise-live web process appear dead and can
        # leave users looking at a blank/502 page.
        return jsonify(status="ok")

    @app.get("/readyz")
    def readiness():
        """Dependency check for operators; unlike /healthz this probes PostgreSQL."""
        try:
            with db.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return jsonify(status="ok", database="ok")
        except Exception as exc:
            # Pool counters contain no credentials or SQL, and distinguish a
            # saturated pool from connection/TLS/database failures in Render.
            app.logger.warning(
                "Database readiness check failed: %s; pool=%s",
                type(exc).__name__,
                db.engine.pool.status(),
            )
            db.session.rollback()
            return jsonify(status="unavailable", database="unavailable"), 503

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
        )
        response.headers["Cache-Control"] = (
            "public, max-age=3600"
            if request.path.startswith(app.static_url_path + "/")
            else "no-store"
        )
        if app.config.get("PRODUCTION"):
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.errorhandler(HTTPException)
    def http_error(exc):
        message = {
            400: "Проверьте данные запроса. Обновите страницу, если истёк срок формы.",
            403: "Доступ запрещён.",
            404: "Страница не найдена.",
            409: "Данные изменились. Обновите страницу.",
            413: "Размер файла превышает 2 МБ.",
            429: "Слишком много попыток. Повторите позже.",
        }.get(exc.code, exc.description)
        if request.path.startswith("/api/") or request.is_json:
            return jsonify(error=message), exc.code
        return render_template("error.html", message=message), exc.code

    return app
