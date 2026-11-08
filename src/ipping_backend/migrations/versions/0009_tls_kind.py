"""tls (certificate) checks

Adds `tls` to the kinds a deployment that never restricted them has enabled. A deployment whose
administrator turned any of the five older kinds off chose a narrower service and keeps it.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-08 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KINDS = {"ping", "tcp", "http", "traceroute", "dns"}

app_settings = sa.table("app_settings", sa.column("key", sa.String), sa.column("value", sa.JSON))


def upgrade() -> None:
    bind = op.get_bind()
    row = bind.execute(
        sa.select(app_settings.c.value).where(app_settings.c.key == "enabled_kinds")
    ).first()
    if row is None:
        return  # never saved: the default already includes tls
    kinds = row[0]
    if isinstance(kinds, list) and OLD_KINDS <= set(kinds) and "tls" not in kinds:
        bind.execute(
            app_settings.update()
            .where(app_settings.c.key == "enabled_kinds")
            .values(value=[*kinds, "tls"])
        )


def downgrade() -> None:
    bind = op.get_bind()
    row = bind.execute(
        sa.select(app_settings.c.value).where(app_settings.c.key == "enabled_kinds")
    ).first()
    if row is not None and isinstance(row[0], list) and "tls" in row[0]:
        bind.execute(
            app_settings.update()
            .where(app_settings.c.key == "enabled_kinds")
            .values(value=[k for k in row[0] if k != "tls"])
        )
