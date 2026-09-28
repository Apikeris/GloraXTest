"""Persistent identities and append-only evidence/history. All timestamps are UTC."""

from datetime import datetime, timezone
from uuid import uuid4

from .extensions import db


def uid():
    return str(uuid4())


def utcnow():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


class Admin(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=uid)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)


class LoginThrottle(db.Model):
    key = db.Column(db.String(64), primary_key=True)
    failures = db.Column(db.Integer, default=0, nullable=False)
    window_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)


class Dataset(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=uid)
    status = db.Column(db.String(24), nullable=False, default="staging", index=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    published_at = db.Column(db.DateTime(timezone=True))
    report = db.Column(db.JSON, default=dict, nullable=False)


class Project(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=uid)
    key = db.Column(db.String(500), nullable=False, unique=True)
    canonical_url = db.Column(db.Text)
    name = db.Column(db.String(250), nullable=False)
    city = db.Column(db.String(150))
    region = db.Column(db.String(150))
    status = db.Column(db.String(100))
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    coverage = db.Column(db.JSON, default=dict, nullable=False)
    last_seen_at = db.Column(db.DateTime(timezone=True))
    question_limit = db.Column(db.Integer)
    topic_distribution = db.Column(db.JSON, default=dict, nullable=False)
    __table_args__ = (db.CheckConstraint("question_limit IS NULL OR question_limit > 0"),)


class Source(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=uid)
    dataset_id = db.Column(db.String(36), db.ForeignKey("dataset.id"), nullable=False, index=True)
    project_id = db.Column(db.String(36), db.ForeignKey("project.id"), index=True)
    url = db.Column(db.Text, nullable=False)
    content = db.Column(db.Text, nullable=False)
    content_type = db.Column(db.String(100), nullable=False, default="text/html")
    checksum = db.Column(db.String(64), nullable=False)
    fetched_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)


class Fact(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=uid)
    project_id = db.Column(db.String(36), db.ForeignKey("project.id"), nullable=False, index=True)
    category = db.Column(db.String(50), nullable=False)
    key = db.Column(db.String(150), nullable=False)
    scope = db.Column(db.JSON, default=dict, nullable=False)
    scope_key = db.Column(db.String(64), nullable=False)
    current_revision_id = db.Column(
        db.String(36),
        db.ForeignKey("fact_revision.id", use_alter=True, name="fk_fact_current_revision"),
    )
    manual_override = db.Column(db.Boolean, default=False, nullable=False)
    review_pending = db.Column(db.Boolean, default=False, nullable=False)
    __table_args__ = (
        db.UniqueConstraint("project_id", "key", "scope_key", name="uq_fact_identity"),
    )


class FactRevision(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=uid)
    fact_id = db.Column(db.String(36), db.ForeignKey("fact.id"), nullable=False, index=True)
    dataset_id = db.Column(db.String(36), db.ForeignKey("dataset.id"), nullable=False, index=True)
    value = db.Column(db.JSON, nullable=True)
    numeric_value = db.Column(db.Numeric(24, 6))
    value_type = db.Column(db.String(30), nullable=False, default="string")
    unit = db.Column(db.String(50))
    source_id = db.Column(db.String(36), db.ForeignKey("source.id"))
    source_url = db.Column(db.Text)
    evidence = db.Column(db.Text)
    method = db.Column(db.String(60), nullable=False)
    verification_status = db.Column(db.String(30), nullable=False, index=True)
    is_exclusive = db.Column(db.Boolean, default=False, nullable=False)
    missing_reason = db.Column(db.Text)
    conditions = db.Column(db.JSON, default=dict, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    valid_until = db.Column(db.DateTime(timezone=True))
    author_id = db.Column(db.String(36), db.ForeignKey("admin.id"))


class DatasetFact(db.Model):
    dataset_id = db.Column(db.String(36), db.ForeignKey("dataset.id"), primary_key=True)
    revision_id = db.Column(db.String(36), db.ForeignKey("fact_revision.id"), primary_key=True)


class Question(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=uid)
    project_id = db.Column(db.String(36), db.ForeignKey("project.id"), nullable=False, index=True)
    external_id = db.Column(db.String(150))
    origin = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="draft", index=True)
    current_revision_id = db.Column(
        db.String(36),
        db.ForeignKey("question_revision.id", use_alter=True, name="fk_question_current_revision"),
    )
    generation_key = db.Column(db.String(250), unique=True)
    review_reason = db.Column(db.Text)
    __table_args__ = (
        db.UniqueConstraint("project_id", "external_id", name="uq_question_external"),
        db.CheckConstraint("status IN ('draft','published','archived','needs_review')"),
        db.CheckConstraint("origin IN ('generated','manual','ai_import')"),
    )


class QuestionRevision(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=uid)
    question_id = db.Column(db.String(36), db.ForeignKey("question.id"), nullable=False, index=True)
    dataset_id = db.Column(db.String(36), db.ForeignKey("dataset.id"), nullable=False)
    text = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(50), nullable=False)
    options = db.Column(db.JSON, nullable=False)
    correct_option_id = db.Column(db.String(100), nullable=False)
    target_fact_revision_id = db.Column(db.String(36), db.ForeignKey("fact_revision.id"))
    explanation = db.Column(db.Text, nullable=False, default="")
    difficulty = db.Column(db.String(20), nullable=False, default="basic")
    tags = db.Column(db.JSON, default=list, nullable=False)
    template_version = db.Column(db.String(50))
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    author_id = db.Column(db.String(36), db.ForeignKey("admin.id"))
    fingerprint = db.Column(db.String(64), nullable=False)
    semantic_reviewed = db.Column(db.Boolean, default=False, nullable=False)


class Participant(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=uid)
    employee_code_hash = db.Column(db.String(64), unique=True)
    name = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)


class Attempt(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=uid)
    participant_id = db.Column(
        db.String(36), db.ForeignKey("participant.id"), nullable=False, index=True
    )
    full_name = db.Column(db.String(200), nullable=False, index=True)
    project_id = db.Column(db.String(36), db.ForeignKey("project.id"), index=True)
    project_name = db.Column(db.String(250), nullable=False)
    session_hash = db.Column(db.String(64), nullable=False)
    dataset_id = db.Column(db.String(36), db.ForeignKey("dataset.id"))
    settings = db.Column(db.JSON, default=dict, nullable=False)
    status = db.Column(db.String(20), default="in_progress", nullable=False, index=True)
    started_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    finished_at = db.Column(db.DateTime(timezone=True))
    last_activity_at = db.Column(
        db.DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    total = db.Column(db.Integer, nullable=False)
    score = db.Column(db.Integer, default=0, nullable=False)
    legacy_data = db.Column(db.JSON)
    __table_args__ = (
        db.CheckConstraint("status IN ('in_progress','completed','interrupted')"),
        db.CheckConstraint("score >= 0"),
        db.CheckConstraint("total >= 0"),
    )


class AttemptItem(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=uid)
    attempt_id = db.Column(db.String(36), db.ForeignKey("attempt.id"), nullable=False, index=True)
    position = db.Column(db.Integer, nullable=False)
    question_revision_id = db.Column(db.String(36), db.ForeignKey("question_revision.id"))
    snapshot = db.Column(db.JSON, nullable=False)
    opened_at = db.Column(db.DateTime(timezone=True))
    deadline = db.Column(db.DateTime(timezone=True), index=True)
    accepted_at = db.Column(db.DateTime(timezone=True))
    selected_option_id = db.Column(db.String(36))
    outcome = db.Column(db.String(24))
    response_ms = db.Column(db.Integer)
    __table_args__ = (
        db.UniqueConstraint("attempt_id", "position", name="uq_attempt_position"),
        db.CheckConstraint(
            "outcome IS NULL OR outcome IN ('correct','wrong','timeout','not_reached','legacy_unknown')"
        ),
    )


class Job(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=uid)
    kind = db.Column(db.String(40), nullable=False, default="refresh")
    active_key = db.Column(db.String(40), unique=True)
    state = db.Column(db.String(20), nullable=False, default="queued", index=True)
    stage = db.Column(db.String(80), default="Ожидание", nullable=False)
    progress = db.Column(db.Integer, default=0, nullable=False)
    total = db.Column(db.Integer, default=0, nullable=False)
    detail = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    started_at = db.Column(db.DateTime(timezone=True))
    finished_at = db.Column(db.DateTime(timezone=True))
    heartbeat_at = db.Column(db.DateTime(timezone=True))
    available_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    lease_token = db.Column(db.String(36))
    attempts = db.Column(db.Integer, default=0, nullable=False)
    report = db.Column(db.JSON, default=dict, nullable=False)
    error = db.Column(db.Text)


class Setting(db.Model):
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.JSON, nullable=False)


class AuditLog(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=uid)
    admin_id = db.Column(db.String(36), db.ForeignKey("admin.id"))
    action = db.Column(db.String(100), nullable=False)
    entity_id = db.Column(db.String(100))
    detail = db.Column(db.JSON, default=dict, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class LegacyRecord(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=uid)
    source_hash = db.Column(db.String(64), nullable=False)
    table_name = db.Column(db.String(60), nullable=False)
    old_id = db.Column(db.Integer, nullable=False)
    new_id = db.Column(db.String(36))
    data = db.Column(db.JSON, nullable=False)
    __table_args__ = (
        db.UniqueConstraint("source_hash", "table_name", "old_id", name="uq_legacy_record"),
    )
