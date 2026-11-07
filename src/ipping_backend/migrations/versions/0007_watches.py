"""scheduled checks (watches) and their runs

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-07 17:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "watches",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("target", sa.String(2048), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("node_ids", sa.JSON(), nullable=True),
        sa.Column("interval_s", sa.Integer(), nullable=False),
        sa.Column("min_ok_pct", sa.Integer(), nullable=False),
        sa.Column("alert_after", sa.Integer(), nullable=False),
        sa.Column("alert_url", sa.String(2048), nullable=True),
        sa.Column("alert_format", sa.String(8), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(10), nullable=True),
        sa.Column("bad_streak", sa.Integer(), nullable=False),
        sa.Column("alerting", sa.Boolean(), nullable=False),
        sa.Column("last_alert_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_alert_error", sa.String(300), nullable=True),
    )
    op.create_index("ix_watches_next_run_at", "watches", ["next_run_at"])
    op.create_table(
        "watch_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "watch_id",
            sa.String(32),
            sa.ForeignKey("watches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("measurement_id", sa.String(32), nullable=True),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("nodes_total", sa.Integer(), nullable=False),
        sa.Column("nodes_ok", sa.Integer(), nullable=False),
        sa.Column("median_ms", sa.Float(), nullable=True),
        sa.Column("min_ms", sa.Float(), nullable=True),
        sa.Column("max_ms", sa.Float(), nullable=True),
        sa.Column("note", sa.String(200), nullable=True),
    )
    op.create_index("ix_watch_runs_watch_id", "watch_runs", ["watch_id"])
    op.create_index("ix_watch_runs_at", "watch_runs", ["at"])
    with op.batch_alter_table("measurements", schema=None) as batch_op:
        batch_op.add_column(sa.Column("watch_id", sa.String(32), nullable=True))
        batch_op.create_index("ix_measurements_watch_id", ["watch_id"])


def downgrade() -> None:
    with op.batch_alter_table("measurements", schema=None) as batch_op:
        batch_op.drop_index("ix_measurements_watch_id")
        batch_op.drop_column("watch_id")
    op.drop_index("ix_watch_runs_at", table_name="watch_runs")
    op.drop_index("ix_watch_runs_watch_id", table_name="watch_runs")
    op.drop_table("watch_runs")
    op.drop_index("ix_watches_next_run_at", table_name="watches")
    op.drop_table("watches")
