"""node auto-fill: which of a node's fields are filled in automatically from its address

A node's country, region, city, position, provider and AS number can be filled from the IP data (MaxMind) for the address it
connects from. `auto_fields` lists the ones that are: those are refreshed when the data changes, and a field an
administrator types (or one the node reported when it enrolled) is not in it, so it is never overwritten. Existing nodes have
none: nothing they hold was filled automatically.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-10 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("nodes", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("auto_fields", sa.JSON(), nullable=False, server_default="[]")
        )


def downgrade() -> None:
    with op.batch_alter_table("nodes", schema=None) as batch_op:
        batch_op.drop_column("auto_fields")
