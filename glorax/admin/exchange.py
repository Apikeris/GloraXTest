"""Administrative exchange; registered on the shared protected blueprint."""

import json

from flask import flash, render_template, request, session
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import Project
from . import bp
from .common import audit, integer, project_list


@bp.route("/ai-prompt", methods=["GET", "POST"])
def prompt_export():
    from ..importing import export_prompt

    prompt, errors = None, []
    if request.method == "POST":
        try:
            project_id = request.form.get("project_id")
            db.get_or_404(Project, project_id)
            count = integer(request.form.get("count"), default=10, minimum=1, maximum=100)
            themes = [v.strip() for v in request.form.get("themes", "").split(",") if v.strip()]
            difficulty = request.form.get("difficulty", "basic")
            if difficulty not in ("basic", "intermediate", "advanced"):
                raise ValueError("Недопустимая сложность")
            prompt = export_prompt(project_id, count, themes, difficulty)
            if not isinstance(prompt, str):
                prompt = json.dumps(prompt, ensure_ascii=False, indent=2, default=str)
            audit(
                "ai.export_prompt",
                project_id,
                {"count": count, "themes": themes, "difficulty": difficulty},
            )
            db.session.commit()
        except ValueError as exc:
            errors = [str(exc)]
    return render_template(
        "admin_prompt.html", projects=project_list(), prompt=prompt, errors=errors
    )


def read_import_payload():
    uploaded = request.files.get("file")
    if uploaded and uploaded.filename:
        if not uploaded.filename.lower().endswith(".json"):
            raise ValueError("Разрешён файл .json")
        raw = uploaded.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Размер JSON не должен превышать 1 МиБ")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("Файл должен быть в кодировке UTF-8")
    else:
        text = request.form.get("payload", "")
        if len(text.encode()) > 1024 * 1024:
            raise ValueError("Размер JSON не должен превышать 1 МиБ")
    try:
        from ..importing import parse_payload

        return parse_payload(text)
    except (ValueError, TypeError):
        raise ValueError("Некорректный JSON: проверьте кавычки, скобки и запятые")


@bp.route("/import", methods=["GET", "POST"])
def import_questions():
    from ..importing import commit_import, preview_import

    payload, preview, errors, report = None, None, [], None
    if request.method == "POST":
        try:
            payload = read_import_payload()
            if request.form.get("action") == "commit":
                selected = request.form.getlist("selected")
                if not selected:
                    raise ValueError("Выберите хотя бы один валидный вопрос")
                report = commit_import(
                    payload,
                    selected,
                    allow_updates=request.form.get("allow_updates") == "on",
                    author_id=session["admin_id"],
                )
                audit(
                    "questions.import",
                    detail={
                        "selected_external_ids": selected,
                        "allow_updates": request.form.get("allow_updates") == "on",
                    },
                )
                db.session.commit()
                flash(
                    "Импорт завершён. Свободные формулировки сохранены черновиками для смысловой проверки",
                    "success",
                )
            else:
                preview = preview_import(payload)
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            errors = [
                str(exc)
                if not isinstance(exc, IntegrityError)
                else "Конфликт импорта: данные были изменены. Повторите предпросмотр"
            ]
    return render_template(
        "admin_import.html", payload=payload, preview=preview, errors=errors, report=report
    )
