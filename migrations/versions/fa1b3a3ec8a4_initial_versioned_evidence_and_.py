from alembic import op
import sqlalchemy as sa


revision = "fa1b3a3ec8a4"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():

    op.create_table(
        "admin",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "dataset",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("dataset", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_dataset_status"), ["status"], unique=False)

    op.create_table(
        "job",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("active_key", sa.String(length=40), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("stage", sa.String(length=80), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.String(length=36), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("active_key"),
    )
    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_job_state"), ["state"], unique=False)

    op.create_table(
        "legacy_record",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("table_name", sa.String(length=60), nullable=False),
        sa.Column("old_id", sa.Integer(), nullable=False),
        sa.Column("new_id", sa.String(length=36), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_hash", "table_name", "old_id", name="uq_legacy_record"),
    )
    op.create_table(
        "login_throttle",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("window_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "participant",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("employee_code_hash", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("employee_code_hash"),
    )
    op.create_table(
        "project",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=500), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("name", sa.String(length=250), nullable=False),
        sa.Column("city", sa.String(length=150), nullable=True),
        sa.Column("region", sa.String(length=150), nullable=True),
        sa.Column("status", sa.String(length=100), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("coverage", sa.JSON(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("question_limit", sa.Integer(), nullable=True),
        sa.Column("topic_distribution", sa.JSON(), nullable=False),
        sa.CheckConstraint("question_limit IS NULL OR question_limit > 0"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_table(
        "setting",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "attempt",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("participant_id", sa.String(length=36), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("project_name", sa.String(length=250), nullable=False),
        sa.Column("session_hash", sa.String(length=64), nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=True),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("legacy_data", sa.JSON(), nullable=True),
        sa.CheckConstraint("status IN ('in_progress','completed','interrupted')"),
        sa.CheckConstraint("score >= 0"),
        sa.CheckConstraint("total >= 0"),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["dataset.id"],
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"],
            ["participant.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("attempt", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_attempt_full_name"), ["full_name"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_attempt_last_activity_at"), ["last_activity_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_attempt_participant_id"), ["participant_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_attempt_project_id"), ["project_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_attempt_started_at"), ["started_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_attempt_status"), ["status"], unique=False)

    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("admin_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("entity_id", sa.String(length=100), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["admin_id"],
            ["admin.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_audit_log_created_at"), ["created_at"], unique=False)

    op.create_table(
        "fact",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("key", sa.String(length=150), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("scope_key", sa.String(length=64), nullable=False),
        sa.Column("current_revision_id", sa.String(length=36), nullable=True),
        sa.Column("manual_override", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["current_revision_id"],
            ["fact_revision.id"],
            name="fk_fact_current_revision",
            use_alter=True,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "key", "scope_key", name="uq_fact_identity"),
    )
    with op.batch_alter_table("fact", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_fact_project_id"), ["project_id"], unique=False)

    op.create_table(
        "question",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("external_id", sa.String(length=150), nullable=True),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("current_revision_id", sa.String(length=36), nullable=True),
        sa.Column("generation_key", sa.String(length=250), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.CheckConstraint("origin IN ('generated','manual','ai_import')"),
        sa.CheckConstraint("status IN ('draft','published','archived','needs_review')"),
        sa.ForeignKeyConstraint(
            ["current_revision_id"],
            ["question_revision.id"],
            name="fk_question_current_revision",
            use_alter=True,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("generation_key"),
        sa.UniqueConstraint("project_id", "external_id", name="uq_question_external"),
    )
    with op.batch_alter_table("question", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_question_project_id"), ["project_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_question_status"), ["status"], unique=False)

    op.create_table(
        "source",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["dataset.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("source", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_source_dataset_id"), ["dataset_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_source_project_id"), ["project_id"], unique=False)

    op.create_table(
        "fact_revision",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("fact_id", sa.String(length=36), nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("numeric_value", sa.Numeric(precision=24, scale=6), nullable=True),
        sa.Column("value_type", sa.String(length=30), nullable=False),
        sa.Column("unit", sa.String(length=50), nullable=True),
        sa.Column("source_id", sa.String(length=36), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("method", sa.String(length=60), nullable=False),
        sa.Column("verification_status", sa.String(length=30), nullable=False),
        sa.Column("is_exclusive", sa.Boolean(), nullable=False),
        sa.Column("missing_reason", sa.Text(), nullable=True),
        sa.Column("conditions", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("author_id", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["admin.id"],
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["dataset.id"],
        ),
        sa.ForeignKeyConstraint(
            ["fact_id"],
            ["fact.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["source.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("fact_revision", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_fact_revision_dataset_id"), ["dataset_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_fact_revision_fact_id"), ["fact_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_fact_revision_verification_status"),
            ["verification_status"],
            unique=False,
        )

    op.create_table(
        "dataset_fact",
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["dataset.id"],
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["fact_revision.id"],
        ),
        sa.PrimaryKeyConstraint("dataset_id", "revision_id"),
    )
    op.create_table(
        "question_revision",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("question_id", sa.String(length=36), nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("correct_option_id", sa.String(length=100), nullable=False),
        sa.Column("target_fact_revision_id", sa.String(length=36), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("difficulty", sa.String(length=20), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("template_version", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("author_id", sa.String(length=36), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("semantic_reviewed", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["admin.id"],
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["dataset.id"],
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["question.id"],
        ),
        sa.ForeignKeyConstraint(
            ["target_fact_revision_id"],
            ["fact_revision.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("question_revision", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_question_revision_question_id"), ["question_id"], unique=False
        )

    op.create_table(
        "attempt_item",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("question_revision_id", sa.String(length=36), nullable=True),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("selected_option_id", sa.String(length=36), nullable=True),
        sa.Column("outcome", sa.String(length=24), nullable=True),
        sa.Column("response_ms", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('correct','wrong','timeout','not_reached','legacy_unknown')"
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempt.id"],
        ),
        sa.ForeignKeyConstraint(
            ["question_revision_id"],
            ["question_revision.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", "position", name="uq_attempt_position"),
    )
    with op.batch_alter_table("attempt_item", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_attempt_item_attempt_id"), ["attempt_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_attempt_item_deadline"), ["deadline"], unique=False)


def downgrade():

    with op.batch_alter_table("attempt_item", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_attempt_item_deadline"))
        batch_op.drop_index(batch_op.f("ix_attempt_item_attempt_id"))

    op.drop_table("attempt_item")
    with op.batch_alter_table("question_revision", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_question_revision_question_id"))

    op.drop_table("question_revision")
    op.drop_table("dataset_fact")
    with op.batch_alter_table("fact_revision", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_fact_revision_verification_status"))
        batch_op.drop_index(batch_op.f("ix_fact_revision_fact_id"))
        batch_op.drop_index(batch_op.f("ix_fact_revision_dataset_id"))

    op.drop_table("fact_revision")
    with op.batch_alter_table("source", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_source_project_id"))
        batch_op.drop_index(batch_op.f("ix_source_dataset_id"))

    op.drop_table("source")
    with op.batch_alter_table("question", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_question_status"))
        batch_op.drop_index(batch_op.f("ix_question_project_id"))

    op.drop_table("question")
    with op.batch_alter_table("fact", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_fact_project_id"))

    op.drop_table("fact")
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_audit_log_created_at"))

    op.drop_table("audit_log")
    with op.batch_alter_table("attempt", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_attempt_status"))
        batch_op.drop_index(batch_op.f("ix_attempt_started_at"))
        batch_op.drop_index(batch_op.f("ix_attempt_project_id"))
        batch_op.drop_index(batch_op.f("ix_attempt_participant_id"))
        batch_op.drop_index(batch_op.f("ix_attempt_last_activity_at"))
        batch_op.drop_index(batch_op.f("ix_attempt_full_name"))

    op.drop_table("attempt")
    op.drop_table("setting")
    op.drop_table("project")
    op.drop_table("participant")
    op.drop_table("login_throttle")
    op.drop_table("legacy_record")
    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_job_state"))

    op.drop_table("job")
    with op.batch_alter_table("dataset", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_dataset_status"))

    op.drop_table("dataset")
    op.drop_table("admin")
