from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from flask import current_app, jsonify, redirect, request, session, url_for

from ..auth import audit as audit
from ..extensions import db
from ..models import Admin, Project, aware
from ..questions import CATEGORIES
from . import bp

STATUSES = {"in_progress": "Идёт", "completed": "Завершена", "interrupted": "Прервана"}


OUTCOMES = {
    "correct": "Верно",
    "wrong": "Ошибка",
    "timeout": "Время истекло",
    "not_reached": "Не дошёл до вопроса",
    "legacy_unknown": "Исторические данные неполны",
    None: "Ожидает ответа",
}


QUESTION_STATUSES = {
    "draft": "Черновик",
    "published": "Опубликован",
    "archived": "Архив",
    "needs_review": "Нужна проверка",
}


@bp.before_request
def require_admin():
    if not session.get("admin_id") or not db.session.get(Admin, session["admin_id"]):
        if request.path.endswith(".json") or request.is_json:
            return jsonify(error="Необходим вход администратора"), 401
        return redirect(url_for("auth.login", next=request.full_path))


@bp.app_template_filter("admin_date")
def admin_date(value):
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    zone = ZoneInfo(current_app.config.get("DISPLAY_TIMEZONE", "Europe/Moscow"))
    return aware(value).astimezone(zone).strftime("%d.%m.%Y %H:%M:%S")


def safe_url(value):
    try:
        return value if value and urlsplit(value).scheme in ("http", "https") else None
    except ValueError:
        return None


@bp.context_processor
def admin_context():
    def page_url(page):
        args = request.args.to_dict()
        args["page"] = page
        return url_for(request.endpoint, **(request.view_args or {}), **args)

    return dict(
        attempt_statuses=STATUSES,
        outcomes=OUTCOMES,
        question_statuses=QUESTION_STATUSES,
        categories=CATEGORIES,
        page_url=page_url,
        safe_source_url=safe_url,
    )


def integer(value, default=None, minimum=None, maximum=None):
    if value in (None, ""):
        return default
    try:
        result = int(value)
    except (ValueError, TypeError):
        raise ValueError("Введите целое число")
    if minimum is not None and result < minimum or maximum is not None and result > maximum:
        raise ValueError(f"Число должно быть в диапазоне {minimum}–{maximum}")
    return result


def project_list():
    return Project.query.order_by(Project.name).all()
