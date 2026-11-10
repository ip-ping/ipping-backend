"""measurements: which node resolved the name

A measurement can ask one node to resolve the name for every node (`dns_mode` fixed with `resolve_node_id`). The node
is kept, as it was when the measurement began, so a result can say who the address came from. Existing measurements
have none: the backend or each node resolved the name.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-10 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("measurements", schema=None) as batch_op:
        batch_op.add_column(sa.Column("resolved_by", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("measurements", schema=None) as batch_op:
        batch_op.drop_column("resolved_by")
