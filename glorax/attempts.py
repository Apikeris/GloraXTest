import hashlib
import hmac
import random
import secrets
import time
from datetime import timedelta

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from .catalogue import available_question_counts as available_question_counts
from .extensions import db
from .facts import get_setting, latest_dataset
from .models import (
    Attempt,
    AttemptItem,
    Fact,
    FactRevision,
    Participant,
    Project,
    QuestionRevision,
    aware,
    uid,
    utcnow,
)
from .selection import balanced_sample, test_question_limit

bp = Blueprint("public", __name__)
rng = random.SystemRandom()


def server_now():
    if db.engine.dialect.name == "postgresql":
        return db.session.execute(db.select(db.func.clock_timestamp())).scalar_one()
    return utcnow()


def session_hash():
    if "participant_session" not in session:
        session["participant_session"] = secrets.token_urlsafe(32)
    return hashlib.sha256(session["participant_session"].encode()).hexdigest()


def _apply_question_settings(project, questions, shuffle=True):
    questions = list({question.id: question for question in questions}.values())
    if not questions:
        return []
    revision_ids = [question.current_revision_id for question in questions]
    revisions = {
        revision.id: revision
        for revision in db.session.scalars(
            db.select(QuestionRevision).where(QuestionRevision.id.in_(revision_ids))
        )
    }
    fact_revision_ids = {
        revision.target_fact_revision_id
        for revision in revisions.values()
        if revision.target_fact_revision_id
    }
    fact_revisions = (
        {
            revision.id: revision
            for revision in db.session.scalars(
                db.select(FactRevision).where(FactRevision.id.in_(fact_revision_ids))
            )
        }
        if fact_revision_ids
        else {}
    )
    fact_ids = {revision.fact_id for revision in fact_revisions.values()}
    facts = (
        {fact.id: fact for fact in db.session.scalars(db.select(Fact).where(Fact.id.in_(fact_ids)))}
        if fact_ids
        else {}
    )
    buckets = {}
    for question in questions:
        revision = revisions.get(question.current_revision_id)
        if revision:
            fact_revision = fact_revisions.get(revision.target_fact_revision_id)
            fact = facts.get(fact_revision.fact_id) if fact_revision else None
            scope = (fact.scope or {}) if fact else {}
            conditions = fact_revision.conditions or {} if fact_revision else {}
            family = [fact.key if fact else revision.category]
            if revision.category == "transport":
                family.extend(
                    (
                        conditions.get("map_category_type"),
                        conditions.get("mode") or conditions.get("transport_mode"),
                    )
                )
            elif revision.category in {"prices", "layouts"}:
                family.extend((scope.get("property_type"), scope.get("rooms")))
            elif revision.category == "infrastructure":
                family.append(conditions.get("category_type"))
            buckets.setdefault((revision.category, tuple(family)), []).append(question)
    limit = test_question_limit(project.question_limit, get_setting("question_limit", None))
    return balanced_sample(buckets, limit, project.topic_distribution, rng=rng, shuffle=shuffle)


def select_questions(project, shuffle=True):
    from .questions import eligible_questions

    questions = eligible_questions(project) if project.enabled else []
    return _apply_question_settings(project, questions, shuffle)


def owned_attempt(attempt_id):
    attempt = db.session.execute(
        db.select(Attempt).where(Attempt.id == attempt_id).with_for_update()
    ).scalar_one_or_none()
    if not attempt or not hmac.compare_digest(attempt.session_hash, session_hash()):
        abort(404)
    return attempt


def items_for(attempt):
    return (
        db.session.execute(
            db.select(AttemptItem)
            .where(AttemptItem.attempt_id == attempt.id)
            .order_by(AttemptItem.position)
        )
        .scalars()
        .all()
    )


def expire_attempt(attempt, now=None, items=None):
    now = now or server_now()
    if attempt.status != "in_progress":
        return
    items = items_for(attempt) if items is None else items
    for item in items:
        if item.outcome is None and item.deadline and aware(item.deadline) <= now:
            item.outcome = "timeout"
            item.accepted_at = aware(item.deadline)
            item.response_ms = 20000
    if all(i.outcome is not None for i in items):
        attempt.status = "completed"
        attempt.finished_at = now
    elif (
        aware(attempt.last_activity_at)
        + timedelta(minutes=int(get_setting("inactivity_minutes", 60)))
        <= now
    ):
        for item in items:
            if item.outcome is None:
                item.outcome = "not_reached"
                item.accepted_at = now
        attempt.status = "interrupted"
        attempt.finished_at = now


def sweep_expired():
    now = server_now()
    cutoff = now - timedelta(minutes=int(get_setting("inactivity_minutes", 60)))
    expired_item = (
        db.select(AttemptItem.id)
        .where(
            AttemptItem.attempt_id == Attempt.id,
            AttemptItem.outcome.is_(None),
            AttemptItem.deadline <= now,
        )
        .exists()
    )
    attempts = list(
        db.session.scalars(
            db.select(Attempt)
            .where(
                Attempt.status == "in_progress",
                db.or_(Attempt.last_activity_at <= cutoff, expired_item),
            )
            .order_by(Attempt.last_activity_at, Attempt.id)
            .limit(200)
            .with_for_update(skip_locked=True)
        )
    )
    if attempts:
        grouped = {attempt.id: [] for attempt in attempts}
        for item in db.session.scalars(
            db.select(AttemptItem)
            .where(AttemptItem.attempt_id.in_(grouped))
            .order_by(AttemptItem.position)
        ):
            grouped[item.attempt_id].append(item)
        for attempt in attempts:
            expire_attempt(attempt, now, grouped[attempt.id])
    db.session.commit()
    return len(attempts)


def result_data(attempt):
    items = items_for(attempt)
    return {
        "score": attempt.score,
        "total": attempt.total,
        "percent": round(attempt.score * 100 / attempt.total, 1) if attempt.total else 0,
        "errors": sum(i.outcome == "wrong" for i in items),
        "timeouts": sum(i.outcome == "timeout" for i in items),
        "not_reached": sum(i.outcome == "not_reached" for i in items),
        "status": attempt.status,
    }


@bp.get("/")
def index():

    if request.method == "HEAD":
        return Response(status=200)
    stage_started = time.monotonic()
    try:
        current_app.logger.warning("Catalogue request started")
        projects = list(db.session.execute(db.select(Project).order_by(Project.name)).scalars())
        current_app.logger.warning(
            "Catalogue stage=projects count=%d seconds=%.3f",
            len(projects),
            time.monotonic() - stage_started,
        )
        stage_started = time.monotonic()
        counts = available_question_counts(projects)
        current_app.logger.warning(
            "Catalogue stage=question_counts count=%d seconds=%.3f",
            sum(counts.values()),
            time.monotonic() - stage_started,
        )
        stage_started = time.monotonic()
        cards = []
        for project in projects:
            count = counts[project.id]
            cards.append(
                {
                    "project": project,
                    "count": count,
                    "reason": "Проект отключён администратором."
                    if not project.enabled
                    else "Нет актуальных опубликованных вопросов: данные ожидают проверки или недостаточно однозначных вариантов."
                    if not count
                    else None,
                }
            )
        dataset = latest_dataset()
        current_app.logger.warning(
            "Catalogue stage=dataset seconds=%.3f", time.monotonic() - stage_started
        )
        return render_template("index.html", cards=cards, dataset=dataset)
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error(
            "Catalogue database query failed: %s; pool=%s",
            type(exc).__name__,
            db.engine.pool.status(),
        )
        return render_template(
            "error.html",
            message="Не удалось подключиться к базе проектов. Попробуйте обновить страницу через минуту.",
        ), 503


@bp.post("/start_test")
def start_test():
    name = " ".join(request.form.get("full_name", request.form.get("username", "")).split())
    code = request.form.get("employee_code", "").strip()
    if not name or len(name) > 200 or len(code) > 100:
        abort(400)
    if db.engine.dialect.name == "postgresql":
        db.session.execute(db.text("SELECT pg_advisory_xact_lock_shared(73192041)"))
    project = db.session.get(Project, request.form.get("project_id"))
    if not project:
        abort(400)
    questions = select_questions(project)
    expected = request.form.get("expected_count")
    if expected and (not expected.isdigit() or int(expected) != len(questions)):
        flash("Число доступных вопросов изменилось.", "error")
        return redirect("/")
    if not questions:
        flash("Тест пока недоступен: нет пригодных опубликованных вопросов.", "error")
        return redirect("/")
    code_hash = (
        hmac.new(
            current_app.secret_key.encode(), ("employee:" + code).encode(), hashlib.sha256
        ).hexdigest()
        if code
        else None
    )
    participant = None
    if code_hash and db.engine.dialect.name == "postgresql":
        db.session.execute(
            insert(Participant)
            .values(id=uid(), employee_code_hash=code_hash, name=name, created_at=utcnow())
            .on_conflict_do_nothing(index_elements=["employee_code_hash"])
        )
    if code_hash:
        participant = db.session.execute(
            db.select(Participant).where(Participant.employee_code_hash == code_hash)
        ).scalar_one_or_none()
    if not participant:
        participant = Participant(name=name, employee_code_hash=code_hash)
        db.session.add(participant)
        db.session.flush()
    dataset = latest_dataset()
    attempt = Attempt(
        participant_id=participant.id,
        full_name=name,
        project_id=project.id,
        project_name=project.name,
        session_hash=session_hash(),
        dataset_id=dataset.id if dataset else None,
        total=len(questions),
        settings={
            "seconds_per_question": 20,
            "question_limit": test_question_limit(
                project.question_limit, get_setting("question_limit")
            ),
            "selection_policy": "balanced_categories_v3_prices3_studio_area",
            "topic_distribution": project.topic_distribution,
            "selected_categories": {},
            "show_review": bool(get_setting("show_review", False)),
        },
    )
    db.session.add(attempt)
    db.session.flush()
    selected_categories = {}
    revisions = {
        revision.id: revision
        for revision in db.session.scalars(
            db.select(QuestionRevision).where(
                QuestionRevision.id.in_([question.current_revision_id for question in questions])
            )
        )
    }
    for position, q in enumerate(questions, 1):
        revision = revisions[q.current_revision_id]
        selected_categories[revision.category] = selected_categories.get(revision.category, 0) + 1
        options = []
        correct = None
        for option in revision.options:
            opaque = uid()
            options.append(
                {
                    "id": opaque,
                    "text": option["text"],
                    "fact_revision_id": option["fact_revision_id"],
                }
            )
            if option["id"] == revision.correct_option_id:
                correct = opaque
        rng.shuffle(options)
        snapshot = {
            "text": revision.text,
            "category": revision.category,
            "options": options,
            "correct_option_id": correct,
            "explanation": revision.explanation,
            "dataset_id": revision.dataset_id,
            "target_fact_revision_id": revision.target_fact_revision_id,
            "question_id": q.id,
            "origin": q.origin,
        }
        db.session.add(
            AttemptItem(
                attempt_id=attempt.id,
                position=position,
                question_revision_id=revision.id,
                snapshot=snapshot,
            )
        )
    attempt.settings = {**attempt.settings, "selected_categories": selected_categories}
    db.session.commit()
    return redirect(url_for("public.test_page", attempt_id=attempt.id))


@bp.get("/test/<attempt_id>")
def test_page(attempt_id):
    attempt = owned_attempt(attempt_id)
    expire_attempt(attempt)
    db.session.commit()
    if attempt.status != "in_progress":
        return redirect(url_for("public.result", attempt_id=attempt.id))
    return render_template("test.html", attempt=attempt)


@bp.get("/api/attempts/<attempt_id>/current")
def current_question(attempt_id):
    attempt = owned_attempt(attempt_id)
    now = server_now()
    items = items_for(attempt)
    expire_attempt(attempt, now, items)
    if attempt.status != "in_progress":
        db.session.commit()
        return jsonify(
            status=attempt.status, result_url=url_for("public.result", attempt_id=attempt.id)
        )
    item = next(i for i in items if i.outcome is None)
    if item.opened_at is None:
        item.opened_at = now
        item.deadline = now + timedelta(seconds=20)
    attempt.last_activity_at = now
    data = {
        "status": "in_progress",
        "id": item.id,
        "text": item.snapshot["text"],
        "category": item.snapshot["category"],
        "options": [{"id": o["id"], "text": o["text"]} for o in item.snapshot["options"]],
        "number": item.position,
        "total": attempt.total,
        "deadline": aware(item.deadline).isoformat(),
        "server_now": now.isoformat(),
    }
    db.session.commit()
    return jsonify(data)


@bp.post("/api/attempts/<attempt_id>/answer")
def answer(attempt_id):
    data = request.get_json(silent=True)
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("question_id"), str)
        or not isinstance(data.get("option_id"), str)
    ):
        abort(400)
    attempt = owned_attempt(attempt_id)
    now = server_now()
    items = items_for(attempt)
    expire_attempt(attempt, now, items)
    item = next((item for item in items if item.id == data["question_id"]), None)
    if not item or item.attempt_id != attempt.id:
        db.session.commit()
        abort(400)
    if item.outcome is not None:
        db.session.commit()
        if item.outcome in ("correct", "wrong"):
            return jsonify(accepted=True, duplicate=True)
        return jsonify(accepted=False, error="Время ответа истекло.", expired=True), 409
    if attempt.status != "in_progress" or not item.opened_at:
        db.session.commit()
        abort(409)
    if any(i.position < item.position and i.outcome is None for i in items):
        db.session.commit()
        abort(409)
    option_id = data.get("option_id")
    if option_id not in {o["id"] for o in item.snapshot["options"]}:
        db.session.commit()
        abort(400)
    item.selected_option_id = option_id
    item.accepted_at = now
    item.response_ms = max(0, int((now - aware(item.opened_at)).total_seconds() * 1000))
    item.outcome = "correct" if option_id == item.snapshot["correct_option_id"] else "wrong"
    if item.outcome == "correct":
        attempt.score += 1
    attempt.last_activity_at = now
    if all(i.outcome is not None for i in items):
        attempt.status = "completed"
        attempt.finished_at = now
    db.session.commit()
    return jsonify(accepted=True)


@bp.get("/result/<attempt_id>")
def result(attempt_id):
    attempt = owned_attempt(attempt_id)
    expire_attempt(attempt)
    db.session.commit()
    if attempt.status == "in_progress":
        return redirect(url_for("public.test_page", attempt_id=attempt.id))
    return render_template(
        "result.html",
        attempt=attempt,
        result=result_data(attempt),
        items=items_for(attempt) if attempt.settings.get("show_review") else None,
    )
