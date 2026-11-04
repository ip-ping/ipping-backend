"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-04 01:57:22.437762
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=64), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("source_ip", sa.String(length=45), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_audit_events_at"), ["at"], unique=False)

    op.create_table(
        "enrollment_tokens",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("node_id", sa.String(length=32), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_table(
        "measurements",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("input_kind", sa.String(length=16), nullable=False),
        sa.Column("target_host", sa.String(length=255), nullable=False),
        sa.Column("target_ip", sa.String(length=45), nullable=True),
        sa.Column("resolved_ips", sa.JSON(), nullable=False),
        sa.Column("dns_mode", sa.String(length=16), nullable=False),
        sa.Column("family", sa.String(length=8), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("owner_session_id", sa.String(length=64), nullable=True),
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
        sa.Column("source_ip", sa.String(length=45), nullable=True),
        sa.Column("shared", sa.Boolean(), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("measurements", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_measurements_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(
            "ix_measurements_owner_status", ["owner_session_id", "status"], unique=False
        )

    op.create_table(
        "nodes",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("credential_hash", sa.String(length=64), nullable=False),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column("region", sa.String(length=100), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("asn", sa.Integer(), nullable=True),
        sa.Column("location_verified", sa.Boolean(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=True),
        sa.Column("protocol_version", sa.Integer(), nullable=True),
        sa.Column("remote_addr", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("nodes", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_nodes_status"), ["status"], unique=False)

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "measurement_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("measurement_id", sa.String(length=32), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=40), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["measurement_id"], ["measurements.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("measurement_id", "seq", name="uq_events_measurement_seq"),
    )
    op.create_table(
        "measurement_tasks",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("measurement_id", sa.String(length=32), nullable=False),
        sa.Column("node_id", sa.String(length=32), nullable=False),
        sa.Column("node_info", sa.JSON(), nullable=False),
        sa.Column("attempt_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("tested_ip", sa.String(length=45), nullable=True),
        sa.Column("resolved_ips", sa.JSON(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["measurement_id"], ["measurements.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("measurement_tasks", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_measurement_tasks_measurement_id"), ["measurement_id"], unique=False
        )
        batch_op.create_index("ix_tasks_node_status", ["node_id", "status"], unique=False)

    op.create_table(
        "web_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("csrf_token", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "samples",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("rtt_us", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["measurement_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "seq", name="uq_samples_task_seq"),
    )


def downgrade() -> None:
    op.drop_table("samples")
    op.drop_table("web_sessions")
    with op.batch_alter_table("measurement_tasks", schema=None) as batch_op:
        batch_op.drop_index("ix_tasks_node_status")
        batch_op.drop_index(batch_op.f("ix_measurement_tasks_measurement_id"))

    op.drop_table("measurement_tasks")
    op.drop_table("measurement_events")
    op.drop_table("users")
    with op.batch_alter_table("nodes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_nodes_status"))

    op.drop_table("nodes")
    with op.batch_alter_table("measurements", schema=None) as batch_op:
        batch_op.drop_index("ix_measurements_owner_status")
        batch_op.drop_index(batch_op.f("ix_measurements_created_at"))

    op.drop_table("measurements")
    op.drop_table("enrollment_tokens")
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_audit_events_at"))

    op.drop_table("audit_events")
    op.drop_table("app_settings")
