"""PV-coupled arbitrage: revenue split and per-slot PV routing

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PLAN_COLUMNS = ("battery_revenue_try", "pv_revenue_try", "curtailed_kwh", "recovered_kwh")
_SLOT_COLUMNS = ("pv_to_battery_kw", "pv_export_kw", "grid_charge_kw", "curtailed_kw")


def upgrade() -> None:
    with op.batch_alter_table("arbitrage_plans") as batch:
        for name in _PLAN_COLUMNS:
            batch.add_column(
                sa.Column(name, sa.Float(), nullable=False, server_default="0.0")
            )
    with op.batch_alter_table("arbitrage_slots") as batch:
        for name in _SLOT_COLUMNS:
            batch.add_column(
                sa.Column(name, sa.Float(), nullable=False, server_default="0.0")
            )


def downgrade() -> None:
    with op.batch_alter_table("arbitrage_slots") as batch:
        for name in reversed(_SLOT_COLUMNS):
            batch.drop_column(name)
    with op.batch_alter_table("arbitrage_plans") as batch:
        for name in reversed(_PLAN_COLUMNS):
            batch.drop_column(name)
