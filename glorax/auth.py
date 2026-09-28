import hashlib
from datetime import timedelta
from functools import wraps

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy.dialects.postgresql import insert
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db
from .models import Admin, AuditLog, LoginThrottle, aware, utcnow

bp = Blueprint("auth", __name__)
DUMMY_HASH = generate_password_hash("timing-only-not-an-account-password")


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("admin_id") or not db.session.get(Admin, session["admin_id"]):
            if request.is_json:
                abort(403)
            return redirect(url_for("auth.login"))
        return fn(*args, **kwargs)

    return wrapped


def audit(action, entity_id=None, detail=None):
    db.session.add(
        AuditLog(
            admin_id=session.get("admin_id"),
            action=action,
            entity_id=entity_id,
            detail=detail or {},
        )
    )


@bp.route("/admin/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()[:100]
        key = hashlib.sha256((request.remote_addr or "unknown").encode()).hexdigest()
        now = utcnow()
        if db.engine.dialect.name == "postgresql":
            db.session.execute(
                insert(LoginThrottle)
                .values(key=key, failures=0, window_at=now)
                .on_conflict_do_nothing()
            )
        elif not db.session.get(LoginThrottle, key):
            db.session.add(LoginThrottle(key=key, failures=0, window_at=now))
            db.session.flush()
        limiter = db.session.execute(
            db.select(LoginThrottle).where(LoginThrottle.key == key).with_for_update()
        ).scalar_one()
        if aware(limiter.window_at) < now - timedelta(minutes=15):
            limiter.failures = 0
            limiter.window_at = now
        if limiter.failures >= 5:
            db.session.commit()
            abort(429)
        admin = db.session.execute(
            db.select(Admin).where(Admin.username == username)
        ).scalar_one_or_none()
        valid = check_password_hash(
            admin.password_hash if admin else DUMMY_HASH, request.form.get("password", "")
        )
        if admin and valid:
            limiter.failures = 0
            session.clear()
            session["admin_id"] = admin.id
            session.permanent = True
            audit("login", admin.id)
            db.session.commit()
            return redirect("/admin")
        limiter.failures += 1
        db.session.commit()
        flash("Неверный логин или пароль.", "error")
    return render_template("login.html")


@bp.post("/admin/logout")
@admin_required
def logout():
    audit("logout")
    db.session.commit()
    session.clear()
    return redirect("/")
