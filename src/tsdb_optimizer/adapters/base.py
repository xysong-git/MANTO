"""Database adapter boundary."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping

from ..schemas import DoctorReport, ExecutionResult, QueryRequest


class TSDBAdapter(ABC):
    """Abstract database adapter.

    Implementations must consume the entire result set, close client resources,
    and return a structured error instead of silently swallowing query errors.
    """

    @abstractmethod
    def connect(self) -> None:
        """Open the database connection, or raise AdapterError."""

    @abstractmethod
    def close(self) -> None:
        """Close the connection and release resources. Must be idempotent."""

    @abstractmethod
    def doctor(self) -> DoctorReport:
        """Probe connection, version, model, and read-only capabilities."""

    @abstractmethod
    def execute_readonly(
        self,
        request: QueryRequest,
        preview_rows: int = 20,
        *,
        strategy_id: str = "S0_NATIVE",
        execution_options: Mapping[str, str | int | float | bool] | None = None,
    ) -> ExecutionResult:
        """Validate and execute one read-only SELECT, fully consuming results.

        ``strategy_id`` labels the registry-selected strategy that produced the
        SQL text.  ``execution_options`` contains only registry-validated
        options; the only implemented option is ``cache_epoch``.  The adapter
        still performs at most one server execution per call and never retries.
        """

    def __enter__(self) -> "TSDBAdapter":
        self.connect()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
