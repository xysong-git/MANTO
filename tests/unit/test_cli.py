import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tsdb_optimizer import cli
from tsdb_optimizer.cli import main
from tsdb_optimizer.schemas import ExecutionResult
from tsdb_optimizer.schemas.execution import ExecutionStatus


def test_help_exits_zero(capsys) -> None:
    try:
        main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    output = capsys.readouterr().out
    assert "doctor" in output
    assert "query" in output
    assert "optimize" in output


def test_optimize_help_lists_modes(capsys) -> None:
    try:
        main(["optimize", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    output = capsys.readouterr().out
    assert "--mode" in output
    assert "baseline" in output
    assert "rule_based" in output
    assert "adaptive" in output


def test_unsafe_sql_is_rejected_without_database(capsys) -> None:
    code = main(["query", "--sql", "DELETE FROM root.test.g_0.d_0"])
    assert code == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error_type"] == "UnsafeSQLError"


def test_missing_password_is_config_error(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("IOTDB_PASSWORD", raising=False)
    code = main(["query", "--sql", "SELECT s_130 FROM root.test.g_0.d_0"])
    assert code == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error_type"] == "ConfigError"
    assert "IOTDB_PASSWORD" in error["error_message"]


def test_invalid_preview_rows_is_rejected_before_connecting(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IOTDB_PASSWORD", "test-password")
    code = main(
        [
            "query",
            "--sql",
            "SELECT s_130 FROM root.test.g_0.d_0",
            "--preview-rows",
            "0",
        ]
    )
    assert code == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error_type"] == "ConfigError"


def test_negative_timeout_is_rejected_before_connecting(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IOTDB_PASSWORD", "test-password")
    code = main(
        [
            "query",
            "--sql",
            "SELECT s_130 FROM root.test.g_0.d_0",
            "--timeout-ms",
            "-1",
        ]
    )
    assert code == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error_type"] == "ConfigError"
    assert "timeout" in error["error_message"].lower()


def test_missing_sql_file_is_config_error(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IOTDB_PASSWORD", "test-password")
    code = main(["query", "--file", str(tmp_path / "missing.sql")])
    assert code == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error_type"] == "ConfigError"


def test_query_reports_persistence_failure_without_hiding_success(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    started = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

    class FakeStore:
        def __init__(self, path):
            self.path = path

        def initialize(self) -> None:
            return None

        def record_query(self, request, digest) -> None:
            return None

        def record_execution(self, result) -> None:
            raise OSError("disk is full")

    class FakeAdapter:
        def __init__(self, settings):
            self.settings = settings

        def execute_readonly(self, request, preview_rows: int):
            del preview_rows
            return ExecutionResult(
                query_id=request.query_id,
                execution_id="execution-cli-persist",
                status=ExecutionStatus.SUCCESS,
                sql_sha256="a" * 64,
                started_at=started,
                finished_at=started + timedelta(milliseconds=1),
                request_dispatched=True,
                request_completed=True,
            )

        def close(self) -> None:
            return None

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IOTDB_PASSWORD", "test-password")
    monkeypatch.setattr(cli, "SQLiteStore", FakeStore)
    monkeypatch.setattr(cli, "IoTDBTreeAdapter", FakeAdapter)

    code = main(["query", "--sql", "SELECT s_130 FROM root.test.g_0.d_0"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "SUCCESS"
    assert any(
        warning.startswith("EXECUTION_PERSISTENCE_FAILED")
        and "disk is full" in warning
        for warning in payload["warnings"]
    )


def test_optimize_rejects_unsafe_sql_without_database(capsys) -> None:
    code = main(["optimize", "--sql", "DELETE FROM root.test.g_0.d_0"])
    assert code == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error_type"] == "UnsafeSQLError"


def test_optimize_rejects_invalid_preview_rows(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IOTDB_PASSWORD", "test-password")
    code = main(
        [
            "optimize",
            "--sql",
            "SELECT s_130 FROM root.test.g_0.d_0",
            "--preview-rows",
            "0",
        ]
    )
    assert code == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error_type"] == "ConfigError"


def test_optimize_rejects_negative_timeout(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IOTDB_PASSWORD", "test-password")
    code = main(
        [
            "optimize",
            "--sql",
            "SELECT s_130 FROM root.test.g_0.d_0",
            "--timeout-ms",
            "-1",
        ]
    )
    assert code == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error_type"] == "ConfigError"


def test_optimize_rejects_unknown_decision_mode() -> None:
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "optimize",
                "--sql",
                "SELECT s_130 FROM root.test.g_0.d_0",
                "--mode",
                "llm_freeform",
            ]
        )
    assert exc.value.code == 2


def test_optimize_help_lists_cache_epoch_and_server_identity(capsys) -> None:
    try:
        main(["optimize", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    output = capsys.readouterr().out
    assert "--cache-epoch" in output
    assert "--server-version" in output
    assert "--database-model" in output


def test_invalid_cache_epoch_is_rejected_before_connecting(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IOTDB_PASSWORD", "test-password")
    code = main(
        [
            "optimize",
            "--sql",
            "SELECT s_130 FROM root.test.g_0.d_0",
            "--cache-epoch",
            "   ",
        ]
    )
    assert code == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error_type"] == "ConfigError"
    assert "cache epoch" in error["error_message"]
