"""Analytics-ready profile, contact snapshots and rich broadcasts.

Idempotent for databases created from the v6 bootstrap migration and fresh installs.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.models import Base

revision = "20260921_02"
down_revision = "20260921_01"
branch_labels = None
depends_on = None


def _cols(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _indexes(bind, table: str) -> set[str]:
    return {i["name"] for i in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()

    # New analytics/delivery tables. checkfirst makes this safe when v1 bootstrap
    # already created them on a brand-new database from the current metadata.
    Base.metadata.tables["user_events"].create(bind=bind, checkfirst=True)
    Base.metadata.tables["broadcast_deliveries"].create(bind=bind, checkfirst=True)

    user_cols = _cols(bind, "users")
    additions = [
        ("first_name", sa.String(length=255), True, None),
        ("last_name", sa.String(length=255), True, None),
        ("language_code", sa.String(length=16), True, None),
        ("is_premium", sa.Boolean(), True, None),
        ("last_activity_at", sa.DateTime(timezone=True), True, None),
        ("last_seen_at", sa.DateTime(timezone=True), True, None),
        ("profile_meta", postgresql.JSONB(astext_type=sa.Text()), False, sa.text("'{}'::jsonb")),
        ("declared_gender", sa.String(length=32), True, None),
        ("acquisition_source", sa.String(length=255), True, None),
        ("start_param", sa.String(length=255), True, None),
    ]
    for name, typ, nullable, default in additions:
        if name not in user_cols:
            op.add_column("users", sa.Column(name, typ, nullable=nullable, server_default=default))

    lead_cols = _cols(bind, "leads")
    for name, typ in [
        ("contact_value", sa.String(length=255)),
        ("contact_url", sa.Text()),
        ("contact_platform", sa.String(length=16)),
    ]:
        if name not in lead_cols:
            op.add_column("leads", sa.Column(name, typ, nullable=True))

    bc_cols = _cols(bind, "broadcasts")
    broadcast_additions = [
        ("button_url", sa.Text(), True, None),
        ("button_text", sa.String(length=64), True, None),
        ("media_type", sa.String(length=16), True, None),
        ("media_path", sa.Text(), True, None),
        ("media_source_platform", sa.String(length=16), True, None),
        ("media_source_id", sa.Text(), True, None),
        ("total_recipient_count", sa.Integer(), False, sa.text("0")),
        ("updated_at", sa.DateTime(timezone=True), False, sa.text("now()")),
        ("started_at", sa.DateTime(timezone=True), True, None),
    ]
    for name, typ, nullable, default in broadcast_additions:
        if name not in bc_cols:
            op.add_column("broadcasts", sa.Column(name, typ, nullable=nullable, server_default=default))

    # v6 text was required; keep existing data and permit a media-only broadcast.
    op.execute("ALTER TABLE broadcasts ALTER COLUMN text SET DEFAULT ''")
    op.execute("UPDATE broadcasts SET text = '' WHERE text IS NULL")
    op.execute("ALTER TABLE broadcasts ALTER COLUMN text SET NOT NULL")

    user_indexes = _indexes(bind, "users")
    if "ix_users_last_seen_at" not in user_indexes:
        op.create_index("ix_users_last_seen_at", "users", ["last_seen_at"], unique=False)

    # Export-ready read models. Future CSV/XLSX export can simply SELECT from these views.
    op.execute("""
    CREATE OR REPLACE VIEW analytics_leads AS
    SELECT
        l.id AS lead_id, l.created_at AS lead_created_at, l.updated_at AS lead_updated_at, l.status,
        l.processed_at, l.processed_by_name,
        u.id AS user_id, u.platform, u.platform_user_id, u.username, u.display_name, u.first_name, u.last_name,
        u.phone AS profile_phone, u.language_code, u.is_premium, u.subscribed, u.subscribed_at,
        u.created_at AS user_created_at, u.last_seen_at, u.acquisition_source, u.start_param,
        l.client_name, l.contact_method, l.phone AS lead_phone, l.contact_value, l.contact_url, l.contact_platform,
        l.destination, l.travel_dates, l.nights, l.travelers,
        l.raw_payload ->> 'departure_city' AS departure_city,
        l.raw_payload ->> 'adults' AS adults,
        l.raw_payload ->> 'children_count' AS children_count,
        l.raw_payload ->> 'children_ages' AS children_ages,
        l.raw_payload ->> 'hotel_category' AS hotel_category,
        l.raw_payload ->> 'meal_plan' AS meal_plan,
        l.raw_payload AS raw_payload
    FROM leads l
    JOIN users u ON u.id = l.user_id
    """)

    op.execute("""
    CREATE OR REPLACE VIEW analytics_users AS
    SELECT
        u.id AS user_id, u.platform, u.platform_user_id, u.username, u.display_name, u.first_name, u.last_name,
        u.phone, u.language_code, u.is_premium, u.subscribed, u.subscribed_at, u.unsubscribed_at,
        u.created_at, u.updated_at, u.last_seen_at, u.last_activity_at, u.acquisition_source, u.start_param,
        u.declared_gender, u.profile_meta,
        COUNT(l.id) AS leads_count,
        MIN(l.created_at) AS first_lead_at,
        MAX(l.created_at) AS last_lead_at
    FROM users u
    LEFT JOIN leads l ON l.user_id = u.id
    GROUP BY u.id
    """)


def downgrade() -> None:
    # Conservative: production analytics data must never be dropped automatically.
    pass
