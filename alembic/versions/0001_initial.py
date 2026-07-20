"""Initial schema: users, plants, devices, credentials, batteries, anomalies, arbitrage

Revision ID: 0001
Revises:
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("hashed_password", sa.String(length=512), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    op.create_table(
        "plants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("vendor", sa.String(length=20), nullable=False),
        sa.Column("vendor_plant_id", sa.String(length=100), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("dc_capacity_kwp", sa.Float(), nullable=True),
        sa.Column("ac_capacity_kw", sa.Float(), nullable=True),
        sa.Column("timezone", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], name=op.f("fk_plants_owner_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plants")),
        sa.UniqueConstraint("vendor", "vendor_plant_id", name=op.f("uq_plants_vendor")),
    )
    op.create_index(op.f("ix_plants_owner_id"), "plants", ["owner_id"], unique=False)

    op.create_table(
        "inverters",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plant_id", sa.Uuid(), nullable=False),
        sa.Column("vendor_device_id", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("ac_capacity_kw", sa.Float(), nullable=True),
        sa.Column("efficiency_curve", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(
            ["plant_id"], ["plants.id"], name=op.f("fk_inverters_plant_id_plants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inverters")),
        sa.UniqueConstraint("plant_id", "vendor_device_id", name=op.f("uq_inverters_plant_id")),
    )
    op.create_index(op.f("ix_inverters_plant_id"), "inverters", ["plant_id"], unique=False)

    op.create_table(
        "pv_arrays",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plant_id", sa.Uuid(), nullable=False),
        sa.Column("inverter_id", sa.Uuid(), nullable=True),
        sa.Column("modules_per_string", sa.Integer(), nullable=False),
        sa.Column("strings", sa.Integer(), nullable=False),
        sa.Column("tilt_deg", sa.Float(), nullable=False),
        sa.Column("azimuth_deg", sa.Float(), nullable=False),
        sa.Column("module_params", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["inverter_id"], ["inverters.id"], name=op.f("fk_pv_arrays_inverter_id_inverters")
        ),
        sa.ForeignKeyConstraint(
            ["plant_id"], ["plants.id"], name=op.f("fk_pv_arrays_plant_id_plants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pv_arrays")),
    )
    op.create_index(op.f("ix_pv_arrays_plant_id"), "pv_arrays", ["plant_id"], unique=False)

    op.create_table(
        "vendor_credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plant_id", sa.Uuid(), nullable=False),
        sa.Column("vendor", sa.String(length=20), nullable=False),
        sa.Column("auth_type", sa.String(length=20), nullable=False),
        sa.Column("encrypted_payload", sa.LargeBinary(), nullable=False),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["plant_id"], ["plants.id"], name=op.f("fk_vendor_credentials_plant_id_plants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendor_credentials")),
        sa.UniqueConstraint("plant_id", name=op.f("uq_vendor_credentials_plant_id")),
    )

    op.create_table(
        "battery_systems",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plant_id", sa.Uuid(), nullable=False),
        sa.Column("chemistry", sa.String(length=50), nullable=False),
        sa.Column("cells_series", sa.Integer(), nullable=False),
        sa.Column("cells_parallel", sa.Integer(), nullable=False),
        sa.Column("pack_count", sa.Integer(), nullable=False),
        sa.Column("rated_energy_kwh", sa.Float(), nullable=False),
        sa.Column("rated_power_kw", sa.Float(), nullable=False),
        sa.Column("model_params", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["plant_id"], ["plants.id"], name=op.f("fk_battery_systems_plant_id_plants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_battery_systems")),
    )
    op.create_index(
        op.f("ix_battery_systems_plant_id"), "battery_systems", ["plant_id"], unique=False
    )

    op.create_table(
        "anomaly_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plant_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("deviation_pct", sa.Float(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["plant_id"], ["plants.id"], name=op.f("fk_anomaly_events_plant_id_plants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_anomaly_events")),
    )
    op.create_index(
        "ix_anomaly_events_plant_started", "anomaly_events", ["plant_id", "started_at"]
    )

    op.create_table(
        "arbitrage_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("battery_id", sa.Uuid(), nullable=False),
        sa.Column("plan_date", sa.Date(), nullable=False),
        sa.Column("market", sa.String(length=10), nullable=False),
        sa.Column("expected_revenue_try", sa.Float(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["battery_id"],
            ["battery_systems.id"],
            name=op.f("fk_arbitrage_plans_battery_id_battery_systems"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_arbitrage_plans")),
        sa.UniqueConstraint(
            "battery_id", "plan_date", "market", name=op.f("uq_arbitrage_plans_battery_id")
        ),
    )

    op.create_table(
        "arbitrage_slots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("slot_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action", sa.String(length=10), nullable=False),
        sa.Column("power_kw", sa.Float(), nullable=False),
        sa.Column("price_try_mwh", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["arbitrage_plans.id"],
            name=op.f("fk_arbitrage_slots_plan_id_arbitrage_plans"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_arbitrage_slots")),
    )
    op.create_index(op.f("ix_arbitrage_slots_plan_id"), "arbitrage_slots", ["plan_id"])


def downgrade() -> None:
    op.drop_table("arbitrage_slots")
    op.drop_table("arbitrage_plans")
    op.drop_table("anomaly_events")
    op.drop_table("battery_systems")
    op.drop_table("vendor_credentials")
    op.drop_table("pv_arrays")
    op.drop_table("inverters")
    op.drop_table("plants")
    op.drop_table("users")
