from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

settings = get_settings()
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = settings.attachment_dir.parent
DATA_DIR.mkdir(parents=True, exist_ok=True)

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)

if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def run_migrations() -> None:
    """Bring the database schema up to the latest Alembic revision.

    A database created before migrations existed already has every table but no
    ``alembic_version`` row, so it is stamped at head rather than rebuilt.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    config = Config(str(BASE_DIR / "alembic.ini"))
    # Absolute paths and an explicit URL: alembic may be called from any cwd.
    # ConfigParser treats % as interpolation, so literal ones must be escaped.
    config.set_main_option("script_location", str(BASE_DIR / "migrations").replace("%", "%%"))
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    # The app owns logging; alembic.ini's logging section would reset root handlers.
    config.attributes["configure_logger"] = False

    tables = set(inspect(engine).get_table_names())
    if tables - {"alembic_version"} and "alembic_version" not in tables:
        command.stamp(config, "head")
    else:
        command.upgrade(config, "head")


def init_db() -> None:
    from . import models  # noqa: F401  (registers tables)

    settings.attachment_dir.mkdir(parents=True, exist_ok=True)
    run_migrations()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
