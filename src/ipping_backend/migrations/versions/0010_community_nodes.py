"""community nodes: a trust tier on nodes and enrollment tokens, and the stored location audit

A node is `official` (run by the service's own administrators) or `community` (run by a volunteer). Existing nodes and
tokens are official. The tier of a node is the tier of the token it enrolled with; an administrator can change it.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-09 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("nodes", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("tier", sa.String(16), nullable=False, server_default="official")
        )
        batch_op.add_column(sa.Column("location_audit", sa.JSON(), nullable=True))
    with op.batch_alter_table("enrollment_tokens", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("tier", sa.String(16), nullable=False, server_default="official")
        )


def downgrade() -> None:
    with op.batch_alter_table("enrollment_tokens", schema=None) as batch_op:
        batch_op.drop_column("tier")
    with op.batch_alter_table("nodes", schema=None) as batch_op:
        batch_op.drop_column("location_audit")
        batch_op.drop_column("tier")
