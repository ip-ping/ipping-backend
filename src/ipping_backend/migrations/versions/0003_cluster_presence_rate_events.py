"""cluster presence rate events

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-05 02:41:06.667456
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backend_instances",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "node_connections",
        sa.Column("node_id", sa.String(length=32), nullable=False),
        sa.Column("instance_id", sa.String(length=32), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("protocol_version", sa.Integer(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("node_id"),
    )
    with op.batch_alter_table("node_connections", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_node_connections_instance_id"), ["instance_id"], unique=False
        )

    op.create_table(
        "rate_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("rate_events", schema=None) as batch_op:
        batch_op.create_index("ix_rate_events_key_at", ["key", "at"], unique=False)

    with op.batch_alter_table("measurement_tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("owner_instance", sa.String(length=32), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_measurement_tasks_owner_instance"), ["owner_instance"], unique=False
        )

    with op.batch_alter_table("measurements", schema=None) as batch_op:
        batch_op.create_index(
            "ix_measurements_status_deadline", ["status", "deadline_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("measurements", schema=None) as batch_op:
        batch_op.drop_index("ix_measurements_status_deadline")

    with op.batch_alter_table("measurement_tasks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_measurement_tasks_owner_instance"))
        batch_op.drop_column("owner_instance")

    with op.batch_alter_table("rate_events", schema=None) as batch_op:
        batch_op.drop_index("ix_rate_events_key_at")

    op.drop_table("rate_events")
    with op.batch_alter_table("node_connections", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_node_connections_instance_id"))

    op.drop_table("node_connections")
    op.drop_table("backend_instances")
