"""Prospect: pre-installation feasibility designs and frozen reports

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Mevcut santrallerin hepsi kurulu; sunucu varsayılanı bu yüzden
    # `operational`. Sütun eklendikten sonra varsayılan kaldırılmıyor —
    # uygulama tarafındaki `default` yeni satırları zaten dolduruyor ama
    # doğrudan SQL ile eklenen kayıtların NULL kalmaması iyi.
    op.add_column(
        "plants",
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="operational",
        ),
    )
    op.create_index("ix_plants_status", "plants", ["status"])

    op.create_table(
        "prospect_designs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("customer", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("polygon", sa.JSON(), nullable=False),
        sa.Column("obstacles", sa.JSON(), nullable=False),
        sa.Column(
            "mount_type", sa.String(length=30), nullable=False, server_default="rooftop_tilted"
        ),
        sa.Column("tilt_deg", sa.Float(), nullable=False, server_default="15.0"),
        sa.Column("azimuth_deg", sa.Float(), nullable=False, server_default="180.0"),
        sa.Column("setback_m", sa.Float(), nullable=False, server_default="0.6"),
        sa.Column("obstacle_clearance_m", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("row_pitch_m", sa.Float(), nullable=True),
        sa.Column("module_spec", sa.JSON(), nullable=False),
        sa.Column("inverter_spec", sa.JSON(), nullable=False),
        sa.Column("plant_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_prospect_designs_owner_id_users"
        ),
        # Santral silinirse tasarım düşmez, yalnızca bağı kopar: fizibilite
        # kaydı kurulumdan bağımsız bir belgedir.
        sa.ForeignKeyConstraint(
            ["plant_id"],
            ["plants.id"],
            name="fk_prospect_designs_plant_id_plants",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_prospect_designs"),
    )
    op.create_index("ix_prospect_designs_owner_id", "prospect_designs", ["owner_id"])
    op.create_index("ix_prospect_designs_status", "prospect_designs", ["status"])
    op.create_index("ix_prospect_designs_plant_id", "prospect_designs", ["plant_id"])

    op.create_table(
        "prospect_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("design_id", sa.Uuid(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "engine_version", sa.String(length=30), nullable=False, server_default="prospect-v1"
        ),
        sa.Column("data_provenance", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("module_count", sa.Integer(), nullable=False),
        sa.Column("dc_capacity_kwp", sa.Float(), nullable=False),
        sa.Column("ac_capacity_kw", sa.Float(), nullable=False),
        sa.Column("area_m2", sa.Float(), nullable=False),
        sa.Column("row_pitch_m", sa.Float(), nullable=False),
        sa.Column("gcr", sa.Float(), nullable=False),
        sa.Column("orientation", sa.String(length=20), nullable=False),
        sa.Column("modules_per_string", sa.Integer(), nullable=False),
        sa.Column("strings", sa.Integer(), nullable=False),
        sa.Column("inverter_count", sa.Integer(), nullable=False),
        sa.Column("year_one_kwh", sa.Float(), nullable=False),
        sa.Column("specific_yield_kwh_kwp", sa.Float(), nullable=False),
        sa.Column("performance_ratio", sa.Float(), nullable=False),
        sa.Column("poa_kwh_m2", sa.Float(), nullable=False),
        sa.Column("ghi_kwh_m2", sa.Float(), nullable=False),
        sa.Column("lifetime_kwh", sa.Float(), nullable=False),
        sa.Column("p90_year_one_kwh", sa.Float(), nullable=False),
        sa.Column("capex_try", sa.Float(), nullable=False),
        sa.Column("npv_try", sa.Float(), nullable=False),
        # IRR ve geri ödeme tanımsız olabilir; 0 yazmak "getiri yok" gibi
        # okunacağı için nullable bırakıldı.
        sa.Column("irr_real", sa.Float(), nullable=True),
        sa.Column("lcoe_try_kwh", sa.Float(), nullable=False),
        sa.Column("payback_years", sa.Float(), nullable=True),
        sa.Column("layout", sa.JSON(), nullable=False),
        sa.Column("monthly_kwh", sa.JSON(), nullable=False),
        sa.Column("waterfall", sa.JSON(), nullable=False),
        sa.Column("projection", sa.JSON(), nullable=False),
        sa.Column("assumptions", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["design_id"],
            ["prospect_designs.id"],
            name="fk_prospect_reports_design_id_prospect_designs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_prospect_reports"),
    )
    op.create_index("ix_prospect_reports_design_id", "prospect_reports", ["design_id"])
    op.create_index("ix_prospect_reports_computed_at", "prospect_reports", ["computed_at"])


def downgrade() -> None:
    op.drop_index("ix_prospect_reports_computed_at", table_name="prospect_reports")
    op.drop_index("ix_prospect_reports_design_id", table_name="prospect_reports")
    op.drop_table("prospect_reports")
    op.drop_index("ix_prospect_designs_plant_id", table_name="prospect_designs")
    op.drop_index("ix_prospect_designs_status", table_name="prospect_designs")
    op.drop_index("ix_prospect_designs_owner_id", table_name="prospect_designs")
    op.drop_table("prospect_designs")
    op.drop_index("ix_plants_status", table_name="plants")
    op.drop_column("plants", "status")
