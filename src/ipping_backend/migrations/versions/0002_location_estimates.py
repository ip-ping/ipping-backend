"""location estimates

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-04 16:59:30.373431
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "location_estimates",
        sa.Column("measurement_id", sa.String(length=32), nullable=False),
        sa.Column("model_version", sa.String(length=40), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["measurement_id"], ["measurements.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("measurement_id"),
    )


def downgrade() -> None:
    op.drop_table("location_estimates")
