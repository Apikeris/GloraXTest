"""Administrative operations; registered on the shared protected blueprint."""

from flask import abort, flash, jsonify, redirect, render_template, request, url_for

from ..extensions import db
from ..models import Admin, AuditLog, Job
from . import bp
from .common import audit, integer


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    from ..facts import get_setting, set_setting

    defaults = {
        "show_review": False,
        "price_valid_days": 7,
        "fact_valid_days": 180,
        "inactivity_minutes": 60,
        "question_limit": 20,
    }
    if request.method == "POST":
        try:
            values = {"show_review": request.form.get("show_review") == "on"}
            for key in ("price_valid_days", "fact_valid_days", "inactivity_minutes"):
                values[key] = integer(request.form.get(key), minimum=1, maximum=3650)
                if values[key] is None:
                    raise ValueError("Заполните все сроки актуальности и бездействия")
            values["question_limit"] = integer(
                request.form.get("question_limit"), minimum=1, maximum=20
            )
            for key, value in values.items():
                set_setting(key, value)
            audit("settings.update", detail=values)
            db.session.commit()
            flash("Настройки сохранены", "success")
            return redirect(url_for("admin.settings"))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
    from ..selection import test_question_limit

    values = {k: get_setting(k, v) for k, v in defaults.items()}
    values["question_limit"] = test_question_limit(default_limit=values["question_limit"])
    return render_template("admin_settings.html", values=values)


@bp.get("/jobs")
def jobs():
    from ..jobs import worker_diagnostics

    page = Job.query.order_by(Job.created_at.desc()).paginate(
        page=request.args.get("page", 1, type=int), per_page=20, error_out=False
    )
    return render_template("admin_jobs.html", page=page, worker=worker_diagnostics())


@bp.post("/jobs/refresh")
def refresh():
    from ..jobs import enqueue_refresh

    job = enqueue_refresh()
    audit("job.enqueue", job.id)
    db.session.commit()
    flash(
        "Задание добавлено в очередь. При повторном нажатии используется уже активное задание",
        "success",
    )
    return redirect(url_for("admin.job_detail", job_id=job.id))


@bp.post("/jobs/<job_id>/retry")
def retry_job(job_id):
    job = db.get_or_404(Job, job_id)
    if job.state not in ("failed", "cancelled"):
        abort(400, description="Повтор доступен только для неудачного задания")
    from ..jobs import enqueue_refresh

    new_job = enqueue_refresh()
    audit("job.retry", new_job.id, {"previous_job_id": job.id})
    db.session.commit()
    return redirect(url_for("admin.job_detail", job_id=new_job.id))


@bp.get("/jobs/<job_id>")
def job_detail(job_id):
    from ..jobs import worker_diagnostics

    job = db.get_or_404(Job, job_id)
    return render_template("admin_job_detail.html", job=job, worker=worker_diagnostics(job))


@bp.get("/jobs/<job_id>.json")
def job_status(job_id):
    from ..jobs import worker_diagnostics

    job = db.get_or_404(Job, job_id)
    return jsonify(
        id=job.id,
        state=job.state,
        stage=job.stage,
        progress=job.progress,
        total=job.total,
        detail=job.detail,
        error=job.error,
        report=job.report,
        worker=worker_diagnostics(job),
        heartbeat_at=job.heartbeat_at.isoformat() if job.heartbeat_at else None,
        started_at=job.started_at.isoformat() if job.started_at else None,
        finished_at=job.finished_at.isoformat() if job.finished_at else None,
    )


@bp.get("/audit")
def audit_log():
    page = AuditLog.query.order_by(AuditLog.created_at.desc()).paginate(
        page=request.args.get("page", 1, type=int), per_page=50, error_out=False
    )
    admins = {a.id: a.username for a in Admin.query.all()}
    return render_template("admin_audit.html", page=page, admins=admins)
