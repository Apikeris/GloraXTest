from alembic import op
import sqlalchemy as sa

revision = "c6327ea1b108"
down_revision = "b2719f77c531"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "fact", sa.Column("review_pending", sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade():
    op.drop_column("fact", "review_pending")
