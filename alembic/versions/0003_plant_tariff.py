"""Plant feed-in tariff (₺/kWh)

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("plants") as batch:
        batch.add_column(sa.Column("feed_in_tariff_try_kwh", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("plants") as batch:
        batch.drop_column("feed_in_tariff_try_kwh")
