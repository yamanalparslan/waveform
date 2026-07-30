"""Twin calibration state, plant siting metadata and array geometry

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("plants") as batch:
        batch.add_column(sa.Column("altitude_m", sa.Float(), nullable=True))
        batch.add_column(sa.Column("commissioned_on", sa.Date(), nullable=True))
        batch.add_column(sa.Column("grid_export_limit_kw", sa.Float(), nullable=True))

    with op.batch_alter_table("pv_arrays") as batch:
        batch.add_column(
            sa.Column(
                "mount_type",
                sa.String(length=30),
                nullable=False,
                server_default="fixed_ground",
            )
        )
        batch.add_column(sa.Column("gcr", sa.Float(), nullable=True))
        batch.add_column(sa.Column("albedo", sa.Float(), nullable=True))
        batch.add_column(sa.Column("bifaciality", sa.Float(), nullable=True))
        batch.add_column(sa.Column("module_type", sa.String(length=20), nullable=True))

    op.create_table(
        "twin_calibrations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plant_id", sa.Uuid(), nullable=False),
        sa.Column("fitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scale", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("soiling_base_ratio", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("hour_bias", sa.JSON(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quality", sa.JSON(), nullable=False),
        sa.Column("version", sa.String(length=30), nullable=False, server_default="calib-v1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["plant_id"], ["plants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plant_id", "fitted_at"),
    )
    op.create_index(
        "ix_twin_calibrations_plant_id", "twin_calibrations", ["plant_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_twin_calibrations_plant_id", table_name="twin_calibrations")
    op.drop_table("twin_calibrations")

    with op.batch_alter_table("pv_arrays") as batch:
        batch.drop_column("module_type")
        batch.drop_column("bifaciality")
        batch.drop_column("albedo")
        batch.drop_column("gcr")
        batch.drop_column("mount_type")

    with op.batch_alter_table("plants") as batch:
        batch.drop_column("grid_export_limit_kw")
        batch.drop_column("commissioned_on")
        batch.drop_column("altitude_m")
