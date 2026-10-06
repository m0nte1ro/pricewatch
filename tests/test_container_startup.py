import os
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[1] / "deploy" / "docker-entrypoint.sh"


@pytest.fixture
def startup(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Exercise migration/server sequencing without changing host ownership or running a server.
    for name, script in {
        "id": '#!/bin/sh\nprintf "10001\\n"\n',
        "python": '#!/bin/sh\nprintf "migrate:%s\\n" "$*" >> "$PRICEWATCH_TEST_LOG"\nexit "$PRICEWATCH_TEST_MIGRATION_STATUS"\n',
        "server": '#!/bin/sh\nprintf "server:%s\\n" "$*" >> "$PRICEWATCH_TEST_LOG"\n',
    }.items():
        path = bin_dir / name
        path.write_text(script)
        path.chmod(0o755)
    log = tmp_path / "events"
    data_dir = tmp_path / "new" / "data"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "PRICEWATCH_DATA_DIR": str(data_dir),
        "PRICEWATCH_TEST_LOG": str(log),
        "PRICEWATCH_TEST_MIGRATION_STATUS": "0",
    }
    return env, log, data_dir


def test_entrypoint_creates_data_and_migrates_before_exec(startup):
    env, log, data_dir = startup
    result = subprocess.run(
        ["sh", str(ENTRYPOINT), "server", "--one", "value with spaces"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert data_dir.is_dir()
    assert log.read_text().splitlines() == [
        "migrate:-m app.migrate",
        "server:--one value with spaces",
    ]
    assert "Starting Pricewatch" in result.stdout


def test_entrypoint_migration_failure_prevents_server(startup):
    env, log, _ = startup
    env["PRICEWATCH_TEST_MIGRATION_STATUS"] = "23"
    result = subprocess.run(
        ["sh", str(ENTRYPOINT), "server"], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 23
    assert log.read_text().splitlines() == ["migrate:-m app.migrate"]
    assert "Starting Pricewatch" not in result.stdout
