from alembic import op
import sqlalchemy as sa

revision = "b2719f77c531"
down_revision = "fa1b3a3ec8a4"
branch_labels = None
depends_on = None


def upgrade():
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.create_foreign_key(
            "fk_fact_current_revision", "fact", "fact_revision", ["current_revision_id"], ["id"]
        )
        op.create_foreign_key(
            "fk_question_current_revision",
            "question",
            "question_revision",
            ["current_revision_id"],
            ["id"],
        )
        op.execute("""CREATE FUNCTION protect_revision_history() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'Evidence and question revisions are immutable'; END;
        $$ LANGUAGE plpgsql""")
        for table in ("fact_revision", "question_revision", "source"):
            op.execute(
                f"CREATE TRIGGER immutable_{table} BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION protect_revision_history()"
            )
        op.execute("""CREATE FUNCTION protect_attempt_history() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Attempt history cannot be deleted'; END IF;
          IF NEW.snapshot::jsonb IS DISTINCT FROM OLD.snapshot::jsonb OR
             NEW.attempt_id IS DISTINCT FROM OLD.attempt_id OR NEW.position IS DISTINCT FROM OLD.position OR
             NEW.question_revision_id IS DISTINCT FROM OLD.question_revision_id THEN
             RAISE EXCEPTION 'Assigned question snapshot is immutable';
          END IF;
          IF OLD.opened_at IS NOT NULL AND (NEW.opened_at IS DISTINCT FROM OLD.opened_at OR NEW.deadline IS DISTINCT FROM OLD.deadline) THEN
             RAISE EXCEPTION 'Question deadline cannot be extended';
          END IF;
          IF OLD.outcome IS NOT NULL AND (NEW.outcome IS DISTINCT FROM OLD.outcome OR NEW.selected_option_id IS DISTINCT FROM OLD.selected_option_id OR
             NEW.accepted_at IS DISTINCT FROM OLD.accepted_at OR NEW.response_ms IS DISTINCT FROM OLD.response_ms) THEN
             RAISE EXCEPTION 'First accepted answer is immutable';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql""")
        op.execute(
            "CREATE TRIGGER immutable_attempt_item BEFORE UPDATE OR DELETE ON attempt_item FOR EACH ROW EXECUTE FUNCTION protect_attempt_history()"
        )


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER immutable_attempt_item ON attempt_item")
        op.execute("DROP FUNCTION protect_attempt_history()")
        for table in ("fact_revision", "question_revision", "source"):
            op.execute(f"DROP TRIGGER immutable_{table} ON {table}")
        op.execute("DROP FUNCTION protect_revision_history()")
        op.drop_constraint("fk_question_current_revision", "question", type_="foreignkey")
        op.drop_constraint("fk_fact_current_revision", "fact", type_="foreignkey")
