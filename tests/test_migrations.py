from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from app import db as db_module
from app import models  # noqa: F401 - register tables on Base.metadata


def _engine_for(tmp_path, monkeypatch, name: str):
    engine = create_engine(
        f"sqlite:///{(tmp_path / name).as_posix()}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module.settings, "database_url", str(engine.url))
    return engine


def _head_revision() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(db_module.BASE_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(db_module.BASE_DIR / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


def _version(engine) -> str:
    with engine.connect() as connection:
        return connection.execute(text("select version_num from alembic_version")).scalar_one()


def test_migrations_create_the_schema_on_an_empty_database(tmp_path, monkeypatch):
    engine = _engine_for(tmp_path, monkeypatch, "fresh.db")

    db_module.run_migrations()

    tables = set(inspect(engine).get_table_names())
    assert {"emails", "orders", "order_items", "products", "stock_movements"} <= tables
    assert _version(engine) == _head_revision()


def test_a_preexisting_database_is_stamped_instead_of_rebuilt(tmp_path, monkeypatch):
    engine = _engine_for(tmp_path, monkeypatch, "legacy.db")
    # Simulates an install from before migrations existed: schema, no version row.
    db_module.Base.metadata.create_all(engine)

    db_module.run_migrations()

    assert _version(engine) == _head_revision()
    # Stamping must not drop or recreate anything.
    assert {"orders", "products"} <= set(inspect(engine).get_table_names())


def test_run_migrations_is_idempotent(tmp_path, monkeypatch):
    engine = _engine_for(tmp_path, monkeypatch, "twice.db")

    db_module.run_migrations()
    db_module.run_migrations()

    assert _version(engine) == _head_revision()
