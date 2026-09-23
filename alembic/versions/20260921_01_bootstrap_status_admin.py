"""Bootstrap Alembic and add lead/admin workflow tables.

Safe for both a fresh database and the existing pre-Alembic Suntour database.
"""
from alembic import op
import sqlalchemy as sa

from app.models import Base

revision = "20260921_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # Fresh installs: create the whole current schema. Existing installs: only
    # missing tables (lead_messages, broadcasts, etc.) are created.
    Base.metadata.create_all(bind=bind)

    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("leads")}

    if "status" not in columns:
        op.add_column("leads", sa.Column("status", sa.String(length=32), server_default="new", nullable=False))
    if "processed_at" not in columns:
        op.add_column("leads", sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True))
    if "processed_by_platform" not in columns:
        op.add_column("leads", sa.Column("processed_by_platform", sa.String(length=16), nullable=True))
    if "processed_by_user_id" not in columns:
        op.add_column("leads", sa.Column("processed_by_user_id", sa.BigInteger(), nullable=True))
    if "processed_by_name" not in columns:
        op.add_column("leads", sa.Column("processed_by_name", sa.String(length=255), nullable=True))
    if "updated_at" not in columns:
        op.add_column(
            "leads",
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        )

    inspector = sa.inspect(bind)
    index_names = {idx["name"] for idx in inspector.get_indexes("leads")}
    if "ix_leads_status" not in index_names:
        op.create_index("ix_leads_status", "leads", ["status"], unique=False)


def downgrade() -> None:
    # Intentionally conservative. This is a bootstrap migration for an existing
    # production-shaped database; destructive downgrade is not automated.
    pass
