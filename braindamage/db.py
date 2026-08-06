from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

DATABASE_URL = f"sqlite:///{DATA_DIR / 'braindamage.db'}"

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def upgrade_database() -> None:
    """Bring the on-disk database to the schema expected by this checkout.

    Desktop users launch the application directly rather than running a
    deployment step, so pending Alembic revisions must be applied before any
    page constructs an ORM query.  Resolve alembic.ini relative to the package
    instead of the process working directory so launching from elsewhere is
    safe too.
    """
    project_root = Path(__file__).resolve().parent.parent
    config = Config(project_root / "alembic.ini")
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")
