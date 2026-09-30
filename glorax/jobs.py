import re
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, IntegrityError

from .extensions import db
from .models import Job, Setting, aware, uid, utcnow


def worker_heartbeat():
    value = utcnow().isoformat()
    db.session.execute(
        insert(Setting)
        .values(key="worker_queue_heartbeat", value=value)
        .on_conflict_do_update(index_elements=[Setting.key], set_={"value": value})
    )
    db.session.commit()


def worker_diagnostics(job=None):
    row = db.session.get(Setting, "worker_queue_heartbeat")
    try:
        seen = datetime.fromisoformat(row.value) if row else None
    except (ValueError, TypeError):
        seen = None
    now = utcnow()
    online = bool(seen and (now - aware(seen)).total_seconds() < 60)
    message = "Worker подключён к очереди." if online else "Нет связи с обработчиком заданий."
    if job is None:
        job = db.session.scalar(db.select(Job).where(Job.active_key == "refresh"))
    if (
        job
        and job.state == "running"
        and job.heartbeat_at
        and (now - aware(job.heartbeat_at)).total_seconds()
        < current_app.config.get("WORKER_LEASE_SECONDS", 600)
    ):
        message = "Сбор выполняется."
    if job and job.state == "queued" and aware(job.available_at) > now:
        message = "Ожидание повторной попытки."
    return {
        "online": online,
        "last_seen_at": seen.isoformat() if seen else None,
        "message": message,
    }


def safe_job_error(exc):
    from .parser import SourceError

    if isinstance(exc, DBAPIError):
        state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        state = state if isinstance(state, str) and re.fullmatch(r"[0-9A-Z]{5}", state) else None
        reason = {
            "53300": "Достигнут лимит подключений к БД.",
            "53200": "PostgreSQL сообщил о нехватке памяти.",
            "57014": "Запрос отменён или превысил серверный таймаут.",
            "25P03": "PostgreSQL закрыл простаивающую транзакцию.",
            "40P01": "Конфликт блокировок PostgreSQL; задание будет повторено.",
            "57P01": "PostgreSQL перезапускается или остановлен.",
        }.get(
            state,
            "Соединение с PostgreSQL прервано или запрос отклонён.",
        )
        return f"Ошибка БД ({type(exc).__name__}; SQLSTATE {state or 'не получен'}): {reason}"
    return (
        str(exc)[:2000]
        if isinstance(exc, (ValueError, SourceError))
        else "Сбой обработки. Тип: " + type(exc).__name__
    )


def enqueue_refresh():
    existing = db.session.execute(
        db.select(Job).where(Job.active_key == "refresh")
    ).scalar_one_or_none()
    if existing:
        return existing
    job = Job(kind="refresh", active_key="refresh", state="queued")
    db.session.add(job)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        job = db.session.execute(db.select(Job).where(Job.active_key == "refresh")).scalar_one()
    return job


def recover_jobs():
    cutoff = utcnow() - timedelta(seconds=current_app.config.get("WORKER_LEASE_SECONDS", 600))
    jobs = (
        db.session.execute(
            db.select(Job)
            .where(Job.state == "running", Job.heartbeat_at < cutoff)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    for job in jobs:
        job.lease_token = None
        if job.attempts >= current_app.config.get("WORKER_MAX_ATTEMPTS", 3):
            job.state = "failed"
            job.active_key = None
            job.finished_at = utcnow()
            job.error = "Worker потерял соединение; исчерпаны повторы."
        else:
            job.state = "queued"
            job.stage = "Восстановление после остановки"
            job.available_at = utcnow()
    db.session.commit()
    return len(jobs)


def claim_job():
    job = db.session.execute(
        db.select(Job)
        .where(Job.state == "queued", Job.available_at <= utcnow())
        .order_by(Job.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    ).scalar_one_or_none()
    if not job:
        db.session.rollback()
        return None
    job.state = "running"
    job.started_at = utcnow()
    job.heartbeat_at = utcnow()
    job.lease_token = uid()
    job.attempts += 1
    job.stage = "Обнаружение проектов"
    job.error = None
    job.report = {
        key: value
        for key, value in (job.report or {}).items()
        if key not in ("last_error", "failed_stage")
    }
    result = (job.id, job.lease_token)
    db.session.commit()
    return result


def run_job(job_id, token, collector=None):
    from .facts import publish_collection
    from .parser import scrape

    collector = collector or scrape

    def guard():

        job = db.session.execute(
            db.select(Job)
            .where(Job.id == job_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).scalar_one()
        if job.state != "running" or job.lease_token != token:
            raise RuntimeError("Задание уже передано другому worker.")
        return job

    def progress(stage, done=0, total=0, detail=""):
        job = guard()
        job.stage = {
            "discovery": "Обнаружение проектов",
            "collection": "Сбор",
            "normalization": "Нормализация",
        }.get(str(stage), str(stage))[:80]
        job.progress = int(done)
        job.total = int(total)
        job.detail = str(detail)[:2000]
        job.heartbeat_at = utcnow()
        db.session.commit()

    try:
        collection = collector(progress=progress)
        if not collection.get("complete"):
            job = guard()
            job.report = {
                "coverage": collection.get("coverage", {}),
                "errors": collection.get("errors", []),
            }
            db.session.commit()
        progress("Проверка и публикация", 0, 1)
        job = guard()
        snapshot = publish_collection(collection, job_guard=guard)
        job = guard()
        job.state = "succeeded"
        job.active_key = None
        job.finished_at = utcnow()
        job.stage = "Готово"
        job.progress = job.total = 1
        job.report = {"dataset_id": snapshot.id, **snapshot.report}
        job.error = None
        db.session.commit()
        return True
    except Exception as exc:
        db.session.rollback()
        job = db.session.execute(
            db.select(Job).where(Job.id == job_id).with_for_update()
        ).scalar_one_or_none()
        if job and job.lease_token == token and job.state == "running":
            safe = safe_job_error(exc)
            current_app.logger.error("Refresh failed: job=%s stage=%s %s", job_id, job.stage, safe)
            job.error = safe
            job.report = {**job.report, "last_error": safe, "failed_stage": job.stage}
            if job.attempts < current_app.config.get("WORKER_MAX_ATTEMPTS", 3):
                job.state = "queued"
                job.stage = "Ожидание повторной попытки"
                job.available_at = utcnow() + timedelta(seconds=30 * job.attempts)
                job.lease_token = None
            else:
                job.state = "failed"
                job.active_key = None
                job.finished_at = utcnow()
                job.stage = "Ошибка"
            db.session.commit()
        else:
            db.session.rollback()
        return False
