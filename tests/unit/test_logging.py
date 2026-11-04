"""Structured JSON logging contract."""

from __future__ import annotations

import io
import json
import logging
import sys

from tsdb_optimizer.logging import JsonFormatter, configure_logging


def _record(message: str = "hello", **extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="tsdb_optimizer.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_formatter_keeps_decision_and_execution_fields() -> None:
    payload = json.loads(
        JsonFormatter().format(
            _record(
                event="decision_execution",
                query_id="q-1",
                decision_id="d-1",
                execution_id="e-1",
                strategy_id="S0_NATIVE",
                strategy_version="1.0.0",
                decision_mode="rule_based",
                expected_latency_ms=1.5,
                decision_overhead_ms=0.2,
                considered_count=1,
                cost_source="historical_statistic",
                sample_count=5,
                attempt_count=2,
                fallback_used=False,
                final_status="SUCCESS",
                client_wall_ms=3.0,
                total_wall_ms=4.0,
                source_type="client_host",
                row_count=10,
                plan_status="AVAILABLE",
                snapshot_count=2,
                elapsed_ms=2.5,
                timed_out=False,
                timeout_ms=100,
                status="SUCCESS",
                adapter="iotdb",
                error_type=None,
            )
        )
    )

    assert payload["event"] == "decision_execution"
    assert payload["decision_mode"] == "rule_based"
    assert payload["expected_latency_ms"] == 1.5
    assert payload["decision_overhead_ms"] == 0.2
    assert payload["considered_count"] == 1
    assert payload["cost_source"] == "historical_statistic"
    assert payload["sample_count"] == 5
    assert payload["attempt_count"] == 2
    assert payload["fallback_used"] is False
    assert payload["final_status"] == "SUCCESS"
    assert payload["client_wall_ms"] == 3.0
    assert payload["total_wall_ms"] == 4.0
    assert payload["source_type"] == "client_host"
    assert payload["strategy_version"] == "1.0.0"
    # A missing value is omitted rather than serialized as null.
    assert "error_type" not in payload


def test_formatter_ignores_unknown_extra_fields() -> None:
    payload = json.loads(
        JsonFormatter().format(
            _record(query_id="q-1", password="must-not-be-logged")
        )
    )
    assert payload["query_id"] == "q-1"
    assert "password" not in payload


def test_formatter_serializes_exceptions() -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = _record(event="failure")
        record.exc_info = sys.exc_info()

    payload = json.loads(JsonFormatter().format(record))
    assert "RuntimeError: boom" in payload["exc_info"]


def test_configure_logging_is_idempotent_and_emits_json() -> None:
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        root.handlers.clear()
        stream = io.StringIO()
        configure_logging("INFO", stream=stream)
        configure_logging("INFO", stream=stream)

        handlers = [
            handler
            for handler in root.handlers
            if getattr(handler, "_tsdb_optimizer_json", False)
        ]
        assert len(handlers) == 1

        logging.getLogger("tsdb_optimizer.test").info(
            "configured",
            extra={"event": "test_event", "query_id": "q-1"},
        )
        payload = json.loads(stream.getvalue())
        assert payload["event"] == "test_event"
        assert payload["query_id"] == "q-1"
        assert payload["message"] == "configured"
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
