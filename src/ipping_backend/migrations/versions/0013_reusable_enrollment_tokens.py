"""enrollment tokens that can be used again

A reusable token enrolls any number of nodes until it expires or is revoked (the new nodes still wait for approval); `uses`
counts them. Existing tokens are single-use, as they were.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-11 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("enrollment_tokens", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("reusable", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("uses", sa.Integer(), nullable=False, server_default="0"))
    # a token that was already used was used once
    op.execute("UPDATE enrollment_tokens SET uses = 1 WHERE used_at IS NOT NULL")


def downgrade() -> None:
    with op.batch_alter_table("enrollment_tokens", schema=None) as batch_op:
        batch_op.drop_column("uses")
        batch_op.drop_column("reusable")
