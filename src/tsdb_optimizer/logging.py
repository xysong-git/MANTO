"""Structured JSON logging.

Every CLI operation annotates log records with ``query_id`` and/or
``execution_id`` when available.  Secrets are never passed to this module.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, TextIO

_EXTRA_FIELDS = (
    "query_id",
    "decision_id",
    "execution_id",
    "event",
    "state",
    "event_index",
    "execution_ids",
    "status",
    "adapter",
    "strategy_id",
    "row_count",
    "error_type",
    "elapsed_ms",
    "timed_out",
    "timeout_ms",
    "plan_status",
    "snapshot_count",
    "source_type",
    "strategy_version",
    "decision_mode",
    "expected_latency_ms",
    "decision_overhead_ms",
    "considered_count",
    "cost_source",
    "sample_count",
    "attempt_count",
    "fallback_used",
    "fallback_trigger",
    "final_status",
    "client_wall_ms",
    "total_wall_ms",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Configure a single JSON handler on the root logger.

    This is intentionally idempotent for the CLI process, but it does not
    forcibly remove handlers installed by a host application or test runner.
    """

    root = logging.getLogger()
    root.setLevel(level.upper())
    target = stream if stream is not None else sys.stderr
    for handler in root.handlers:
        if getattr(handler, "_tsdb_optimizer_json", False):
            handler.setLevel(level.upper())
            if hasattr(handler, "setStream"):
                try:
                    handler.setStream(target)
                except ValueError:
                    # A host/test runner may have closed the previous stream
                    # between CLI invocations in the same process.  Replace it
                    # rather than failing logging setup.
                    handler.stream = target  # type: ignore[attr-defined]
            return
    handler = logging.StreamHandler(target)
    handler.setLevel(level.upper())
    handler.setFormatter(JsonFormatter())
    handler._tsdb_optimizer_json = True  # type: ignore[attr-defined]
    root.addHandler(handler)
