from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig

from app.config import Config


def upgrade():
    config = Config()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    alembic = AlembicConfig(str(root / "alembic.ini"))
    alembic.set_main_option("script_location", str(root / "migrations"))
    alembic.set_main_option("sqlalchemy.url", config.db_url.replace("%", "%%"))
    command.upgrade(alembic, "head")


if __name__ == "__main__":
    upgrade()
