"""Persistent reminders for unfinished tour requests."""
from alembic import op
import sqlalchemy as sa

revision = "20260929_03"
down_revision = "20260921_02"
branch_labels = None
depends_on = None


def _cols(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _indexes(bind, table: str) -> set[str]:
    return {i["name"] for i in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _cols(bind, "conversations")
    if "reminder_stage" not in cols:
        op.add_column("conversations", sa.Column("reminder_stage", sa.Integer(), nullable=False, server_default=sa.text("0")))
    if "reminder_next_at" not in cols:
        op.add_column("conversations", sa.Column("reminder_next_at", sa.DateTime(timezone=True), nullable=True))
    if "reminder_last_sent_at" not in cols:
        op.add_column("conversations", sa.Column("reminder_last_sent_at", sa.DateTime(timezone=True), nullable=True))

    indexes = _indexes(bind, "conversations")
    if "ix_conversations_reminder_next_at" not in indexes:
        op.create_index("ix_conversations_reminder_next_at", "conversations", ["reminder_next_at"], unique=False)


def downgrade() -> None:
    # Production-safe project policy: no destructive automatic downgrade.
    pass
