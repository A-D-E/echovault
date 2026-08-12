import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from memory.markdown import parse_session_file


REPOSITORY_ROOT = Path(__file__).parents[1]


def test_rust_dashboard_client_writes_one_canonical_python_record(
    tmp_path: Path,
) -> None:
    executable = shutil.which("memory")
    if executable is None:
        pytest.fail("memory console script is required for the dashboard bridge E2E")
    memory_home = tmp_path / ".memory"
    environment = {
        **os.environ,
        "ECHOVAULT_TEST_MEMORY_EXECUTABLE": executable,
        "MEMORY_HOME": str(memory_home),
    }

    subprocess.run(
        [
            "cargo",
            "test",
            "--manifest-path",
            "dashboard/Cargo.toml",
            "--test",
            "python_bridge",
            "cli_mutation_client_writes_canonical_python_storage",
            "--",
            "--ignored",
            "--exact",
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
        text=True,
        capture_output=True,
    )

    database_path = memory_home / "index.db"
    connection = sqlite3.connect(
        f"file:{database_path}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id, what, file_path, last_updated_by, operation_history, "
            "content_fingerprint FROM memories"
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == 1
    row = rows[0]

    session_files = sorted((memory_home / "vault").glob("*/*-session.md"))
    assert session_files == [Path(row["file_path"])]
    document = parse_session_file(session_files[0])
    assert document.schema_version == 2
    assert len(document.entries) == 1
    entry = document.entries[0]
    memory = entry.to_memory(str(session_files[0]))

    assert memory.id == row["id"]
    assert memory.what == row["what"] == "updated by rust"
    assert memory.last_updated_by == row["last_updated_by"] == "dashboard"
    assert memory.content_fingerprint == row["content_fingerprint"]
    assert entry.metadata["content_fingerprint"] == row["content_fingerprint"]
    database_operations = json.loads(row["operation_history"])
    assert [item["operation_id"] for item in database_operations] == [
        operation.operation_id for operation in memory.operations
    ]
    assert len(database_operations) == 2
