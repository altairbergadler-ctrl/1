"""provider credential vault and component health

Revision ID: 0005_provider_health_credentials
Revises: 0004_provider_attempts
Create Date: 2026-08-10
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_provider_health_credentials"
down_revision = "0004_provider_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("validated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("provider", name="uq_provider_credentials_provider"),
    )
    component = sa.Enum(
        "account", "provider_api", "sidecar", "worker",
        name="provider_health_component",
    )
    state = sa.Enum(
        "healthy", "expired", "rate_limited", "provider_down", "not_configured",
        name="provider_health_state",
    )
    op.create_table(
        "provider_health",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("component", component, nullable=False),
        sa.Column("state", state, nullable=False),
        sa.Column("detail_code", sa.String(length=64)),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("credential_version", sa.Integer()),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.Column("retry_at", sa.DateTime()),
        sa.UniqueConstraint(
            "provider", "component", name="uq_provider_health_provider_component"
        ),
    )
    op.create_index(
        "ix_provider_health_provider", "provider_health", ["provider"]
    )


def downgrade() -> None:
    op.drop_index("ix_provider_health_provider", table_name="provider_health")
    op.drop_table("provider_health")
    op.drop_table("provider_credentials")
    sa.Enum(name="provider_health_state").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="provider_health_component").drop(op.get_bind(), checkfirst=True)
