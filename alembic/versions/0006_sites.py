"""Site (saha) hierarchy: one plant, many factory sites

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from luminmind.core.models.base import NAMING_CONVENTION

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SQLite kısıt düşürmeyi ancak tabloyu yeniden kurarak yapabilir ve bunun için
# kısıtın adını bilmesi gerekir; ad da isimlendirme sözleşmesinden gelir.
# Sözleşme geçirilmezse `drop_constraint` SQLite'ta sessizce çuvallar.
_BATCH = {"naming_convention": NAMING_CONVENTION}


def upgrade() -> None:
    op.create_table(
        "sites",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("code", sa.String(length=60), nullable=False),
        sa.Column("series_key", sa.String(length=120), nullable=False),
        sa.Column("dc_capacity_kwp", sa.Float(), nullable=True),
        sa.Column("ac_capacity_kw", sa.Float(), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("altitude_m", sa.Float(), nullable=True),
        sa.Column("commissioned_on", sa.Date(), nullable=True),
        sa.Column("feed_in_tariff_try_kwh", sa.Float(), nullable=True),
        sa.Column("grid_export_limit_kw", sa.Float(), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["plant_id"], ["plants.id"], name="fk_sites_plant_id_plants"),
        sa.PrimaryKeyConstraint("id", name="pk_sites"),
        sa.UniqueConstraint("series_key", name="uq_sites_series_key"),
        sa.UniqueConstraint("plant_id", "code", name="uq_sites_plant_id_code"),
    )
    op.create_index("ix_sites_plant_id", "sites", ["plant_id"], unique=False)

    with op.batch_alter_table("inverters", **_BATCH) as batch:
        batch.add_column(sa.Column("site_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key("fk_inverters_site_id_sites", "sites", ["site_id"], ["id"])
        batch.create_index("ix_inverters_site_id", ["site_id"], unique=False)
        # Cihaz numarası tesiste değil sahada tekil (iki fabrikada da 1 var)
        batch.drop_constraint("uq_inverters_plant_id", type_="unique")
        batch.create_unique_constraint(
            "uq_inverters_site_id", ["site_id", "vendor_device_id"]
        )

    with op.batch_alter_table("pv_arrays", **_BATCH) as batch:
        batch.add_column(sa.Column("site_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key("fk_pv_arrays_site_id_sites", "sites", ["site_id"], ["id"])
        batch.create_index("ix_pv_arrays_site_id", ["site_id"], unique=False)

    with op.batch_alter_table("anomaly_events", **_BATCH) as batch:
        batch.add_column(sa.Column("site_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key("fk_anomaly_events_site_id_sites", "sites", ["site_id"], ["id"])
        batch.create_index("ix_anomaly_events_site_id", ["site_id"], unique=False)

    with op.batch_alter_table("twin_calibrations", **_BATCH) as batch:
        batch.add_column(sa.Column("site_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_twin_calibrations_site_id_sites", "sites", ["site_id"], ["id"]
        )
        batch.create_index("ix_twin_calibrations_site_id", ["site_id"], unique=False)
        # Kalibrasyon saha bazlı öğrenilir; tekillik de sahaya bağlanır
        batch.drop_constraint("uq_twin_calibrations_plant_id", type_="unique")
        batch.create_unique_constraint(
            "uq_twin_calibrations_site_id", ["site_id", "fitted_at"]
        )


def downgrade() -> None:
    with op.batch_alter_table("twin_calibrations", **_BATCH) as batch:
        batch.drop_constraint("uq_twin_calibrations_site_id", type_="unique")
        batch.create_unique_constraint(
            "uq_twin_calibrations_plant_id", ["plant_id", "fitted_at"]
        )
        batch.drop_index("ix_twin_calibrations_site_id")
        batch.drop_constraint("fk_twin_calibrations_site_id_sites", type_="foreignkey")
        batch.drop_column("site_id")

    with op.batch_alter_table("anomaly_events", **_BATCH) as batch:
        batch.drop_index("ix_anomaly_events_site_id")
        batch.drop_constraint("fk_anomaly_events_site_id_sites", type_="foreignkey")
        batch.drop_column("site_id")

    with op.batch_alter_table("pv_arrays", **_BATCH) as batch:
        batch.drop_index("ix_pv_arrays_site_id")
        batch.drop_constraint("fk_pv_arrays_site_id_sites", type_="foreignkey")
        batch.drop_column("site_id")

    with op.batch_alter_table("inverters", **_BATCH) as batch:
        batch.drop_constraint("uq_inverters_site_id", type_="unique")
        batch.create_unique_constraint(
            "uq_inverters_plant_id", ["plant_id", "vendor_device_id"]
        )
        batch.drop_index("ix_inverters_site_id")
        batch.drop_constraint("fk_inverters_site_id_sites", type_="foreignkey")
        batch.drop_column("site_id")

    op.drop_index("ix_sites_plant_id", table_name="sites")
    op.drop_table("sites")
