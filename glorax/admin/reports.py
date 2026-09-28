"""Administrative reports; registered on the shared protected blueprint."""

import csv
import io
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import (
    Response,
    abort,
    current_app,
    render_template,
    request,
)
from sqlalchemy import func

from ..data_access import literal_search
from ..extensions import db
from ..models import (
    Attempt,
    AttemptItem,
    Job,
    Participant,
    Project,
    Question,
    aware,
)
from . import bp
from .common import (
    STATUSES,
    audit,
    integer,
    project_list,
)


def attempt_query():
    query = Attempt.query
    if request.args.get("name"):
        needle = literal_search(request.args["name"])
        query = query.filter(Attempt.full_name.ilike(f"%{needle}%", escape="\\"))
    if request.args.get("participant_id"):
        query = query.filter_by(participant_id=request.args["participant_id"])
    if request.args.get("project_id"):
        query = query.filter_by(project_id=request.args["project_id"])
    if request.args.get("status") in STATUSES:
        query = query.filter_by(status=request.args["status"])
    for key, end in [("date_from", False), ("date_to", True)]:
        if request.args.get(key):
            try:
                day = datetime.strptime(request.args[key], "%Y-%m-%d").replace(
                    tzinfo=ZoneInfo(current_app.config.get("DISPLAY_TIMEZONE", "Europe/Moscow"))
                )
            except ValueError:
                abort(400, description="Некорректная дата фильтра")
            query = query.filter(
                Attempt.started_at < day + timedelta(days=1) if end else Attempt.started_at >= day
            )
    percentage = Attempt.score * 100.0 / func.nullif(Attempt.total, 0)
    try:
        if request.args.get("min_result"):
            query = query.filter(Attempt.legacy_data.is_(None)).filter(
                percentage >= integer(request.args["min_result"], minimum=0, maximum=100)
            )
        if request.args.get("max_result"):
            query = query.filter(Attempt.legacy_data.is_(None)).filter(
                percentage <= integer(request.args["max_result"], minimum=0, maximum=100)
            )
    except ValueError as exc:
        abort(400, description=str(exc))
    sorts = {
        "newest": Attempt.started_at.desc(),
        "oldest": Attempt.started_at.asc(),
        "name": Attempt.full_name.asc(),
        "score_desc": percentage.desc(),
        "score_asc": percentage.asc(),
    }
    return query.order_by(sorts.get(request.args.get("sort"), sorts["newest"]), Attempt.id)


@bp.get("/")
def dashboard():
    from ..facts import latest_dataset

    return render_template(
        "admin.html",
        counts={
            "projects": Project.query.count(),
            "participants": Participant.query.count(),
            "attempts": Attempt.query.count(),
            "published": Question.query.filter_by(status="published").count(),
            "review": Question.query.filter(Question.status.in_(["draft", "needs_review"])).count(),
        },
        dataset=latest_dataset(),
        jobs=Job.query.order_by(Job.created_at.desc()).limit(5).all(),
        recent=Attempt.query.order_by(Attempt.started_at.desc()).limit(10).all(),
    )


@bp.get("/attempts")
def attempts():
    page = attempt_query().paginate(
        page=request.args.get("page", 1, type=int), per_page=30, error_out=False
    )
    return render_template("admin_attempts.html", page=page, projects=project_list())


def csv_cell(value):
    text = "" if value is None else str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else text


@bp.get("/attempts.csv")
def attempts_csv():
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(
        [
            "ID попытки",
            "ID участника",
            "ФИО при старте",
            "Проект",
            "Начало (UTC)",
            "Окончание (UTC)",
            "Статус",
            "Баллы",
            "Назначено",
            "Процент",
            "Ошибки",
            "Таймауты",
            "Не дошёл",
            "ID датасета",
        ]
    )
    selected_ids = attempt_query().with_entities(Attempt.id).order_by(None).subquery()
    totals = (
        db.select(
            AttemptItem.attempt_id,
            func.count().filter(AttemptItem.outcome == "wrong").label("wrong"),
            func.count().filter(AttemptItem.outcome == "timeout").label("timeouts"),
            func.count().filter(AttemptItem.outcome == "not_reached").label("not_reached"),
        )
        .join(selected_ids, selected_ids.c.id == AttemptItem.attempt_id)
        .group_by(AttemptItem.attempt_id)
        .subquery()
    )
    rows = (
        attempt_query()
        .outerjoin(totals, totals.c.attempt_id == Attempt.id)
        .add_columns(totals.c.wrong, totals.c.timeouts, totals.c.not_reached)
    )
    for attempt, wrong, timeouts, not_reached in rows.yield_per(100):
        counts = {"wrong": wrong or 0, "timeout": timeouts or 0, "not_reached": not_reached or 0}
        writer.writerow(
            [
                csv_cell(x)
                for x in [
                    attempt.id,
                    attempt.participant_id,
                    attempt.full_name,
                    attempt.project_name,
                    aware(attempt.started_at).isoformat(),
                    aware(attempt.finished_at).isoformat() if attempt.finished_at else "",
                    STATUSES[attempt.status],
                    attempt.score,
                    "" if attempt.legacy_data else attempt.total,
                    ""
                    if attempt.legacy_data
                    else round(attempt.score * 100 / attempt.total, 2)
                    if attempt.total
                    else 0,
                    counts.get("wrong", 0),
                    counts.get("timeout", 0),
                    counts.get("not_reached", 0),
                    attempt.dataset_id,
                ]
            ]
        )
    audit("attempts.export_csv", detail={"filters": request.args.to_dict()})
    db.session.commit()
    return Response(
        "\ufeff" + out.getvalue(),
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=glorax-results.csv"},
    )


@bp.get("/participants")
def participants():
    query = Participant.query
    if request.args.get("name"):
        query = query.filter(
            Participant.name.ilike("%" + literal_search(request.args["name"]) + "%", escape="\\")
        )
    if request.args.get("participant_id"):
        query = query.filter_by(id=request.args["participant_id"])
    page = query.order_by(Participant.created_at.desc()).paginate(
        page=request.args.get("page", 1, type=int), per_page=30, error_out=False
    )
    counts = dict(
        db.session.query(Attempt.participant_id, func.count())
        .filter(Attempt.participant_id.in_([x.id for x in page.items]))
        .group_by(Attempt.participant_id)
        .all()
    )
    return render_template("admin_participants.html", page=page, counts=counts)


@bp.get("/attempts/<attempt_id>")
def attempt_detail(attempt_id):
    attempt = db.get_or_404(Attempt, attempt_id)
    items = AttemptItem.query.filter_by(attempt_id=attempt.id).order_by(AttemptItem.position).all()
    return render_template("admin_attempt_detail.html", attempt=attempt, items=items)


@bp.get("/analytics")
def analytics():
    # Snapshot categories are read from immutable attempt items, not the current question bank.
    project_rows = (
        db.session.query(
            Attempt.project_name,
            func.count(Attempt.id),
            func.sum(Attempt.score),
            func.sum(Attempt.total),
        )
        .filter(Attempt.legacy_data.is_(None))
        .group_by(Attempt.project_name)
        .order_by(Attempt.project_name)
        .all()
    )
    category = func.coalesce(AttemptItem.snapshot["category"].as_string(), "unknown")
    totals = db.session.execute(
        db.select(
            category,
            func.count(),
            *[
                func.count().filter(AttemptItem.outcome == outcome)
                for outcome in ("correct", "wrong", "timeout", "not_reached")
            ],
        )
        .where(db.or_(AttemptItem.outcome.is_(None), AttemptItem.outcome != "legacy_unknown"))
        .group_by(category)
    )
    topics = {
        row[0]: dict(zip(("total", "correct", "wrong", "timeout", "not_reached"), row[1:]))
        for row in totals
    }
    question_text = func.coalesce(
        AttemptItem.snapshot["text"].as_string(), "Формулировка не сохранена"
    )
    errors = db.session.execute(
        db.select(question_text, func.count())
        .where(AttemptItem.outcome == "wrong")
        .group_by(question_text)
        .order_by(func.count().desc(), question_text)
        .limit(30)
    ).all()
    return render_template(
        "admin_analytics.html", project_rows=project_rows, topics=topics, errors=errors
    )
