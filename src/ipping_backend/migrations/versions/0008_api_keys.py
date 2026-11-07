"""api keys

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-07 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("per_minute", sa.Integer(), nullable=False),
        sa.Column("per_day", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    with op.batch_alter_table("measurements", schema=None) as batch_op:
        batch_op.add_column(sa.Column("owner_api_key_id", sa.String(32), nullable=True))
        batch_op.create_index("ix_measurements_owner_api_key_id", ["owner_api_key_id"])


def downgrade() -> None:
    with op.batch_alter_table("measurements", schema=None) as batch_op:
        batch_op.drop_index("ix_measurements_owner_api_key_id")
        batch_op.drop_column("owner_api_key_id")
    op.drop_table("api_keys")
