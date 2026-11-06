"""task attempts

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-06 11:20:15.402118
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("measurement_tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("attempts", sa.Integer(), server_default="1", nullable=False))
        batch_op.add_column(sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("measurement_tasks", schema=None) as batch_op:
        batch_op.drop_column("queued_at")
        batch_op.drop_column("attempts")
