"""Inverter health cache columns

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("inverters") as batch:
        batch.add_column(sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_power_kw", sa.Float(), nullable=True))
        batch.add_column(sa.Column("last_temp_c", sa.Float(), nullable=True))
        batch.add_column(sa.Column("last_error_code", sa.String(length=30), nullable=True))
        batch.add_column(sa.Column("last_status", sa.String(length=30), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("inverters") as batch:
        batch.drop_column("last_status")
        batch.drop_column("last_error_code")
        batch.drop_column("last_temp_c")
        batch.drop_column("last_power_kw")
        batch.drop_column("last_seen_at")
