import sqlite3
import subprocess
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_migration_upgrade_and_downgrade(tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_engine(url)
    assert {
        "products",
        "listings",
        "price_history",
        "alerts",
        "discovery_drafts",
        "retailer_states",
        "settings",
    } <= set(inspect(engine).get_table_names())
    assert "store_rules" in inspect(engine).get_table_names()
    columns = {c["name"] for c in inspect(engine).get_columns("listings")}
    assert {"check_interval_minutes", "extraction_method"} <= columns
    command.check(config)
    command.downgrade(config, "base")
    assert inspect(engine).get_table_names() == ["alembic_version"]
    engine.dispose()


def test_online_backup_includes_wal_and_refuses_overwrite(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    script = Path(__file__).resolve().parents[1] / "scripts" / "backup.py"
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE observations (price INTEGER)")
        db.execute("INSERT INTO observations VALUES (789)")
        db.commit()
        result = subprocess.run(
            [sys.executable, str(script), str(source), str(destination)],
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr.decode()
        with sqlite3.connect(destination) as restored:
            assert restored.execute("SELECT price FROM observations").fetchone() == (789,)
        assert destination.stat().st_mode & 0o777 == 0o600
        retry = subprocess.run(
            [sys.executable, str(script), str(source), str(destination)],
            capture_output=True,
            timeout=10,
        )
        assert retry.returncode != 0
