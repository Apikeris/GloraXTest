import hashlib
import json

from flask import abort, flash, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError

from ..data_access import literal_search
from ..extensions import db
from ..models import (
    DatasetFact,
    Fact,
    FactRevision,
    Project,
    Question,
    QuestionRevision,
    uid,
)
from . import bp
from .common import QUESTION_STATUSES, audit, project_list


@bp.get("/questions")
def questions():
    query = (
        db.session.query(Question, QuestionRevision, Project)
        .join(Project, Question.project_id == Project.id)
        .outerjoin(QuestionRevision, Question.current_revision_id == QuestionRevision.id)
    )
    if request.args.get("project_id"):
        query = query.filter(Question.project_id == request.args["project_id"])
    if request.args.get("status") in QUESTION_STATUSES:
        query = query.filter(Question.status == request.args["status"])
    if request.args.get("origin") in ("generated", "manual", "ai_import"):
        query = query.filter(Question.origin == request.args["origin"])
    if request.args.get("category"):
        query = query.filter(QuestionRevision.category == request.args["category"])
    if request.args.get("q"):
        query = query.filter(
            QuestionRevision.text.ilike("%" + literal_search(request.args["q"]) + "%", escape="\\")
        )
    page = query.order_by(Project.name, QuestionRevision.created_at.desc()).paginate(
        page=request.args.get("page", 1, type=int), per_page=30, error_out=False
    )
    return render_template("admin_questions.html", page=page, projects=project_list())


def editor_facts():
    from ..facts import latest_dataset

    dataset = latest_dataset()
    rows = []
    if dataset:
        rows = (
            db.session.query(FactRevision, Fact, Project)
            .join(Fact, FactRevision.fact_id == Fact.id)
            .join(Project, Fact.project_id == Project.id)
            .join(DatasetFact, DatasetFact.revision_id == FactRevision.id)
            .filter(
                DatasetFact.dataset_id == dataset.id, FactRevision.verification_status == "verified"
            )
            .order_by(Project.name, Fact.category, Fact.key)
            .all()
        )
    return dataset, rows


@bp.route("/questions/new", methods=["GET", "POST"])
@bp.route("/questions/<question_id>", methods=["GET", "POST"])
def question_edit(question_id=None):
    from ..questions import format_value, validate_revision

    question = db.get_or_404(Question, question_id) if question_id else None
    revision = db.session.get(QuestionRevision, question.current_revision_id) if question else None
    dataset, facts = editor_facts()
    errors = []
    if request.method == "POST":
        if not dataset:
            errors = ["Нет опубликованного снимка данных"]
        else:
            try:
                project_id = question.project_id if question else request.form.get("project_id")
                if not db.session.get(Project, project_id):
                    raise ValueError("Выберите существующий проект")
                text = request.form.get("text", "").strip()
                explanation = request.form.get("explanation", "").strip()
                if not 10 <= len(text) <= 500 or len(explanation) > 2000:
                    raise ValueError("Вопрос: 10–500 символов, объяснение: не более 2000")
                if question is None:
                    question = Question(
                        id=uid(), project_id=project_id, origin="manual", status="draft"
                    )
                    db.session.add(question)
                    db.session.flush()
                options = []
                for index in range(4):
                    ref = request.form.get(f"option_{index}")
                    fact_revision = db.session.get(FactRevision, ref)
                    if not fact_revision:
                        raise ValueError(f"Вариант {index + 1}: версия факта не найдена")
                    options.append(
                        {
                            "id": str(index),
                            "fact_revision_id": ref,
                            "text": format_value(fact_revision),
                        }
                    )
                target = request.form.get("target_fact_revision_id")
                correct = request.form.get("correct_option_id", "0")
                new = QuestionRevision(
                    id=uid(),
                    question_id=question.id,
                    dataset_id=dataset.id,
                    text=text,
                    category=request.form.get("category", "").strip(),
                    options=options,
                    correct_option_id=correct,
                    target_fact_revision_id=target,
                    explanation=explanation,
                    difficulty=request.form.get("difficulty", "basic"),
                    tags=[t.strip() for t in request.form.get("tags", "").split(",") if t.strip()],
                    author_id=session["admin_id"],
                    semantic_reviewed=request.form.get("semantic_reviewed") == "on",
                    fingerprint=hashlib.sha256(
                        json.dumps(
                            {
                                "text": text,
                                "options": options,
                                "target": target,
                                "correct": correct,
                                "explanation": explanation,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ).encode()
                    ).hexdigest(),
                )
                errors = validate_revision(new, require_current=True, semantic_review=True)
                if errors:
                    db.session.rollback()
                else:
                    db.session.add(new)
                    db.session.flush()
                    if question.origin == "generated":
                        question.origin = "manual"
                    question.current_revision_id = new.id
                    question.status = "draft"
                    question.review_reason = None
                    audit("question.edit", question.id, {"revision_id": new.id})
                    db.session.commit()
                    flash(
                        "Новая версия сохранена как черновик",
                        "success",
                    )
                    return redirect(url_for("admin.question_edit", question_id=question.id))
            except (ValueError, IntegrityError) as exc:
                db.session.rollback()
                errors = [
                    str(exc) if not isinstance(exc, IntegrityError) else "Конфликт сохранения"
                ]
    return render_template(
        "admin_question_edit.html",
        question=question,
        revision=revision,
        projects=project_list(),
        dataset=dataset,
        facts=facts,
        errors=errors,
        render_fact=format_value,
    )


@bp.post("/questions/<question_id>/status")
def question_status(question_id):
    from ..questions import validate_revision

    question = db.get_or_404(Question, question_id)
    status = request.form.get("status")
    if status not in QUESTION_STATUSES:
        abort(400)
    if status == "published":
        revision = db.session.get(QuestionRevision, question.current_revision_id)
        if not revision:
            flash("У вопроса нет версии", "error")
            return redirect(url_for("admin.question_edit", question_id=question.id))
        errors = validate_revision(
            revision, require_current=True, semantic_review=revision.semantic_reviewed
        )
        if question.origin != "generated" and not revision.semantic_reviewed:
            errors.append("Подтвердите смысловую проверку в редакторе и сохраните новую версию")
        if errors:
            for error in errors:
                flash(str(error), "error")
            return redirect(url_for("admin.question_edit", question_id=question.id))
    question.status = status
    audit("question.status", question.id, {"status": status})
    db.session.commit()
    flash("Статус вопроса изменён. Снимки прошлых попыток сохранены", "success")
    return redirect(url_for("admin.question_edit", question_id=question.id))
