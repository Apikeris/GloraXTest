"""Administrative projects; registered on the shared protected blueprint."""

import json
from decimal import InvalidOperation

from flask import Response, flash, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import Fact, FactRevision, Project, Source
from . import bp
from .common import audit, integer, project_list, safe_url


@bp.get("/projects")
def projects():
    from ..catalogue import available_question_counts

    values = project_list()
    # Publication already performed deep validation. Use the grouped SQL
    # counter shared with the employee catalogue instead of revalidating each
    # question serially for every project on every admin page load.
    counts = available_question_counts(values)
    return render_template("admin_projects.html", projects=values, counts=counts)


@bp.route("/projects/<project_id>", methods=["GET", "POST"])
def project_detail(project_id):
    project = db.get_or_404(Project, project_id)
    if request.method == "POST":
        try:
            limit = integer(request.form.get("question_limit"), minimum=1, maximum=20)
            distribution = json.loads(request.form.get("topic_distribution") or "{}")
            if not isinstance(distribution, dict) or any(
                not isinstance(k, str) or type(v) is not int or v < 0
                for k, v in distribution.items()
            ):
                raise ValueError("Распределение: JSON-объект «категория»: целое число вопросов ≥ 0")
            project.question_limit = limit
            project.topic_distribution = distribution
            project.enabled = request.form.get("enabled") == "on"
            audit(
                "project.settings",
                project.id,
                {
                    "enabled": project.enabled,
                    "question_limit": limit,
                    "topic_distribution": distribution,
                },
            )
            db.session.commit()
            flash("Настройки проекта сохранены", "success")
            return redirect(url_for("admin.project_detail", project_id=project.id))
        except (ValueError, json.JSONDecodeError) as exc:
            db.session.rollback()
            flash(str(exc), "error")
    facts = (
        db.session.query(Fact, FactRevision)
        .outerjoin(FactRevision, Fact.current_revision_id == FactRevision.id)
        .filter(Fact.project_id == project.id)
        .order_by(Fact.category, Fact.key)
        .all()
    )
    sources = (
        Source.query.filter_by(project_id=project.id)
        .order_by(Source.fetched_at.desc())
        .limit(40)
        .all()
    )
    return render_template(
        "admin_project_detail.html",
        project=project,
        facts=facts,
        sources=sources,
        displayed_question_limit=min(project.question_limit, 20) if project.question_limit else "",
    )


@bp.route("/projects/<project_id>/facts/new", methods=["GET", "POST"])
@bp.route("/facts/<fact_id>/edit", methods=["GET", "POST"])
def fact_edit(project_id=None, fact_id=None):
    fact = db.get_or_404(Fact, fact_id) if fact_id else None
    project = db.get_or_404(Project, fact.project_id if fact else project_id)
    revision = db.session.get(FactRevision, fact.current_revision_id) if fact else None
    errors = []
    if request.method == "POST":
        from ..facts import save_manual_fact

        try:
            value = json.loads(request.form.get("value", "null"))
            scope = json.loads(request.form.get("scope") or "{}")
            conditions = json.loads(request.form.get("conditions") or "{}")
            if not isinstance(scope, dict) or not isinstance(conditions, dict):
                raise ValueError("Область применения и условия должны быть JSON-объектами")
            source_url = request.form.get("source_url", "").strip()
            if source_url and not safe_url(source_url):
                raise ValueError("Источник должен иметь адрес http:// или https://")
            evidence = request.form.get("evidence", "").strip()
            if len(evidence) < 5:
                raise ValueError(
                    "Приведите подтверждающий фрагмент или обоснование экспертной оценки"
                )
            missing_reason = request.form.get("missing_reason", "").strip()
            if value is None and not missing_reason:
                raise ValueError("Укажите причину отсутствия значения")
            category = request.form.get("category", "").strip()
            key = request.form.get("key", "").strip()
            if not category or not key or len(key) > 150 or len(category) > 50:
                raise ValueError("Укажите категорию и ключ характеристики (не более 150 символов)")
            if fact and (key != fact.key or scope != fact.scope):
                raise ValueError(
                    "Ключ и область применения определяют идентичность факта. Для другого ключа создайте новый факт"
                )
            expert = request.form.get("expert") == "on"
            if not source_url and not expert and value is not None:
                raise ValueError("Для подтверждённого факта требуется URL источника")
            payload = dict(
                fact_id=fact.id if fact else None,
                category=category,
                key=key,
                value=value,
                scope=scope,
                unit=request.form.get("unit", "").strip() or None,
                value_type=request.form.get("value_type", "string"),
                evidence=evidence,
                source_url=source_url or (request.url if expert else None),
                method="expert_assessment" if expert else "manual",
                verification_status="verified"
                if request.form.get("verified") == "on" and value is not None
                else "needs_review",
                is_exclusive=request.form.get("is_exclusive") == "on",
                missing_reason=missing_reason or None,
                conditions=conditions,
            )
            if expert:
                payload["conditions"] = {**conditions, "expert_assessment": True}
                payload["is_exclusive"] = request.form.get("is_exclusive") == "on"
            save_manual_fact(project.id, payload, session["admin_id"])
            audit(
                "fact.create_revision",
                fact.id if fact else project.id,
                {"key": key, "expert_assessment": expert},
            )
            db.session.commit()
            flash(
                "Создана новая версия факта. Ручное значение защищено от автоматической перезаписи",
                "success",
            )
            return redirect(url_for("admin.project_detail", project_id=project.id))
        except (ValueError, InvalidOperation, IntegrityError) as exc:
            db.session.rollback()
            errors = [
                str(exc)
                if not isinstance(exc, IntegrityError)
                else "Конфликт версии: обновите страницу и повторите сохранение"
            ]
    history = (
        FactRevision.query.filter_by(fact_id=fact.id).order_by(FactRevision.created_at.desc()).all()
        if fact
        else []
    )
    return render_template(
        "admin_fact_edit.html",
        project=project,
        fact=fact,
        revision=revision,
        history=history,
        errors=errors,
    )


@bp.get("/sources/<source_id>")
def source_detail(source_id):
    source = db.get_or_404(Source, source_id)
    return render_template("admin_source.html", source=source)


@bp.get("/dataset.json")
def dataset_export():
    from ..facts import export_dataset

    payload = export_dataset()
    audit("dataset.export")
    db.session.commit()
    return Response(
        json.dumps(payload, ensure_ascii=False, default=str, indent=2),
        content_type="application/json",
        headers={"Content-Disposition": "attachment; filename=glorax-dataset.json"},
    )
