"""sponsored nodes: a flag on nodes and on enrollment tokens

A sponsored node's provider is named where the node is shown: the interface makes the provider's name stand out. The flag
of a node is the flag of the token it enrolled with; an administrator can change it. Existing nodes and tokens are not
sponsored.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-12 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("nodes", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("sponsored", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    with op.batch_alter_table("enrollment_tokens", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("sponsored", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("enrollment_tokens", schema=None) as batch_op:
        batch_op.drop_column("sponsored")
    with op.batch_alter_table("nodes", schema=None) as batch_op:
        batch_op.drop_column("sponsored")
