from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_TABLES = {
    "users",
    "plants",
    "inverters",
    "pv_arrays",
    "vendor_credentials",
    "battery_systems",
    "anomaly_events",
    "arbitrage_plans",
    "arbitrage_slots",
}


@pytest.fixture
def alembic_config(tmp_path):
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path}/migration.db")
    return config


def test_upgrade_head_creates_all_tables(alembic_config, tmp_path):
    command.upgrade(alembic_config, "head")

    engine = create_engine(f"sqlite:///{tmp_path}/migration.db")
    tables = set(inspect(engine).get_table_names())
    assert EXPECTED_TABLES <= tables


def test_downgrade_base_drops_all_tables(alembic_config, tmp_path):
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    engine = create_engine(f"sqlite:///{tmp_path}/migration.db")
    tables = set(inspect(engine).get_table_names())
    assert EXPECTED_TABLES.isdisjoint(tables)


def test_migration_matches_orm_metadata(alembic_config, tmp_path):
    """Migration'ın oluşturduğu şema ORM metadata'sındaki tablolarla aynı olmalı."""
    from luminmind.core.models import Base

    command.upgrade(alembic_config, "head")
    engine = create_engine(f"sqlite:///{tmp_path}/migration.db")
    db_tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    orm_tables = set(Base.metadata.tables)
    assert db_tables == orm_tables

    inspector = inspect(engine)
    for table_name in sorted(orm_tables):
        db_columns = {c["name"] for c in inspector.get_columns(table_name)}
        orm_columns = {c.name for c in Base.metadata.tables[table_name].columns}
        assert db_columns == orm_columns, f"column mismatch in {table_name}"
