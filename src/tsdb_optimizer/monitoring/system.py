"""Client-host sampling and explicitly-unavailable DB-host monitoring.

The IoTDB deployment audited in this repository is local, but no DB-host agent
or exporter is installed by default. This module collects real ``psutil``
metrics only for ``source_type=client_host`` and returns an ``unknown`` DB-host
snapshot whose fields are all ``None`` with reasons.  Client metrics are never
relabeled as database metrics.
"""

from __future__ import annotations

import logging
import math
import sys
import socket
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

import psutil

from ..schemas.monitoring import (
    MetricUnavailableReason,
    SystemSample,
    SystemSnapshot,
    SystemSourceType,
)
from ..schemas.common import utc_now

logger = logging.getLogger(__name__)

_METRIC_FIELDS = (
    "cpu_percent",
    "memory_percent",
    "disk_usage_percent",
    "disk_read_bytes_per_sec",
    "disk_write_bytes_per_sec",
    "active_queries",
)


class SystemSampler(Protocol):
    def sample(self) -> SystemSnapshot:
        """Return one structured sample, or raise a real sampler failure."""


def _unavailable_reasons(
    reason: MetricUnavailableReason,
    *,
    active_queries_reason: MetricUnavailableReason | None = None,
) -> dict[str, MetricUnavailableReason]:
    return {
        field: (
            active_queries_reason
            if field == "active_queries" and active_queries_reason is not None
            else reason
        )
        for field in _METRIC_FIELDS
    }


def _unavailable_snapshot(
    *,
    source_host: str,
    source_type: SystemSourceType,
    reason: MetricUnavailableReason,
    stale_after_ms: int,
    warnings: Sequence[str] = (),
    error_type: str | None = None,
    error_message: str | None = None,
    active_queries_reason: MetricUnavailableReason | None = None,
) -> SystemSnapshot:
    return SystemSnapshot(
        source_host=source_host or "unknown",
        source_type=source_type,
        valid=False,
        stale_after_ms=stale_after_ms,
        unavailable_fields=_unavailable_reasons(
            reason, active_queries_reason=active_queries_reason
        ),
        warnings=list(warnings),
        error_type=error_type,
        error_message=error_message,
    )


class PsutilSystemSampler:
    """Collect client-host CPU, memory, disk capacity and disk-I/O rates.

    CPU and disk-I/O rates need a previous observation.  Their first sample is
    marked unavailable rather than filled with zero.  Disk capacity usage and
    memory usage are available immediately.
    """

    _source_type = SystemSourceType.CLIENT_HOST

    def __init__(
        self,
        *,
        hostname: str | None = None,
        disk_usage_path: str | Path = "/",
        stale_after_ms: int = 5000,
        psutil_module: Any = psutil,
        clock: Callable[[], float] = time.monotonic,
        active_query_provider: Callable[[], int | None] | None = None,
    ):
        if stale_after_ms < 1:
            raise ValueError("stale_after_ms must be positive")
        self._psutil = psutil_module
        self._disk_usage_path = str(disk_usage_path)
        self._stale_after_ms = stale_after_ms
        self._clock = clock
        self._active_query_provider = active_query_provider
        self._lock = threading.Lock()
        self._cpu_times_baseline = None
        self._io_baseline: tuple[int, int, float] | None = None
        self._hostname_warning: str | None = None
        if hostname is None:
            try:
                resolved = socket.gethostname().strip()
            except Exception as exc:  # noqa: BLE001 - host name is diagnostic only
                resolved = ""
                self._hostname_warning = (
                    f"could not resolve client hostname: {type(exc).__name__}: {exc}"
                )
            hostname = resolved or "unknown"
        self._hostname = hostname

    @property
    def hostname(self) -> str:
        return self._hostname

    def sample(self) -> SystemSnapshot:
        with self._lock:
            return self._sample_locked()

    def _sample_locked(self) -> SystemSnapshot:
        values: dict[str, float | int | None] = {
            field: None for field in _METRIC_FIELDS
        }
        unavailable: dict[str, MetricUnavailableReason] = {}
        warnings: list[str] = []
        if self._hostname_warning:
            warnings.append(self._hostname_warning)

        cpu = self._sample_cpu(unavailable, warnings)
        memory = self._sample_memory(unavailable, warnings)
        disk_usage = self._sample_disk_usage(unavailable, warnings)
        read_rate, write_rate = self._sample_disk_io(unavailable, warnings)
        active_queries = self._sample_active_queries(unavailable, warnings)

        values.update(
            cpu_percent=cpu,
            memory_percent=memory,
            disk_usage_percent=disk_usage,
            disk_read_bytes_per_sec=read_rate,
            disk_write_bytes_per_sec=write_rate,
            active_queries=active_queries,
        )
        valid = all(
            values[name] is not None
            for name in ("cpu_percent", "memory_percent", "disk_usage_percent")
        )
        return SystemSnapshot(
            source_host=self._hostname,
            source_type=self._source_type,
            cpu_measurement="cpu-times-instance-v1",
            valid=valid,
            stale_after_ms=self._stale_after_ms,
            unavailable_fields=unavailable,
            warnings=warnings,
            **values,
        )

    def _metric_float(
        self,
        value: Any,
        field_name: str,
        unavailable: dict[str, MetricUnavailableReason],
        warnings: list[str],
        *,
        maximum: float = 100.0,
    ) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            unavailable[field_name] = MetricUnavailableReason.INVALID_VALUE
            warnings.append(f"{field_name} was not numeric: {type(exc).__name__}")
            return None
        if number != number or number < 0.0 or number > maximum:  # NaN or range
            unavailable[field_name] = MetricUnavailableReason.INVALID_VALUE
            warnings.append(f"{field_name} was outside [0, {maximum}]: {number!r}")
            return None
        return round(number, 3)

    def _sample_cpu(
        self,
        unavailable: dict[str, MetricUnavailableReason],
        warnings: list[str],
    ) -> float | None:
        try:
            current = self._psutil.cpu_times()._asdict()
            if 'idle' not in current or any(not math.isfinite(v) or v < 0 for v in current.values()):
                raise ValueError('invalid CPU time counters')
        except Exception as exc:
            unavailable['cpu_percent'] = MetricUnavailableReason.PSUTIL_ERROR
            warnings.append(f'cpu_times failed: {type(exc).__name__}: {exc}')
            self._cpu_times_baseline = None
            return None
        previous, self._cpu_times_baseline = self._cpu_times_baseline, current
        if previous is None:
            unavailable['cpu_percent'] = MetricUnavailableReason.BASELINE_NOT_ESTABLISHED
            return None
        if current.keys()!=previous.keys() or any(current[k]<previous[k] for k in current):
            unavailable['cpu_percent'] = MetricUnavailableReason.COUNTER_RESET
            return None
        delta={k:current[k]-previous[k] for k in current}
        total=sum(delta.values())
        if sys.platform.startswith('linux'):
            total-=delta.get('guest',0)+delta.get('guest_nice',0)
        busy=total-delta['idle']-delta.get('iowait',0)
        if total<=0:
            unavailable['cpu_percent'] = MetricUnavailableReason.BASELINE_NOT_ESTABLISHED
            return None
        return self._metric_float(100*busy/total,'cpu_percent',unavailable,warnings)

    def _sample_memory(
        self,
        unavailable: dict[str, MetricUnavailableReason],
        warnings: list[str],
    ) -> float | None:
        try:
            raw = self._psutil.virtual_memory().percent
        except Exception as exc:  # noqa: BLE001 - isolate one metric
            unavailable["memory_percent"] = MetricUnavailableReason.PSUTIL_ERROR
            warnings.append(f"memory_percent failed: {type(exc).__name__}: {exc}")
            return None
        return self._metric_float(raw, "memory_percent", unavailable, warnings)

    def _sample_disk_usage(
        self,
        unavailable: dict[str, MetricUnavailableReason],
        warnings: list[str],
    ) -> float | None:
        try:
            raw = self._psutil.disk_usage(self._disk_usage_path).percent
        except Exception as exc:  # noqa: BLE001 - isolate one metric
            unavailable["disk_usage_percent"] = MetricUnavailableReason.PSUTIL_ERROR
            warnings.append(
                f"disk_usage_percent failed: {type(exc).__name__}: {exc}"
            )
            return None
        return self._metric_float(
            raw, "disk_usage_percent", unavailable, warnings
        )

    def _sample_disk_io(
        self,
        unavailable: dict[str, MetricUnavailableReason],
        warnings: list[str],
    ) -> tuple[float | None, float | None]:
        try:
            counters = self._psutil.disk_io_counters(perdisk=False)
        except Exception as exc:  # noqa: BLE001 - isolate one metric pair
            unavailable["disk_read_bytes_per_sec"] = (
                MetricUnavailableReason.PSUTIL_ERROR
            )
            unavailable["disk_write_bytes_per_sec"] = (
                MetricUnavailableReason.PSUTIL_ERROR
            )
            warnings.append(f"disk_io_counters failed: {type(exc).__name__}: {exc}")
            return None, None
        if counters is None:
            unavailable["disk_read_bytes_per_sec"] = (
                MetricUnavailableReason.PSUTIL_ERROR
            )
            unavailable["disk_write_bytes_per_sec"] = (
                MetricUnavailableReason.PSUTIL_ERROR
            )
            warnings.append("disk_io_counters returned no aggregate counters")
            return None, None

        try:
            read_bytes = int(counters.read_bytes)
            write_bytes = int(counters.write_bytes)
        except (TypeError, ValueError, AttributeError) as exc:
            unavailable["disk_read_bytes_per_sec"] = (
                MetricUnavailableReason.INVALID_VALUE
            )
            unavailable["disk_write_bytes_per_sec"] = (
                MetricUnavailableReason.INVALID_VALUE
            )
            warnings.append(f"disk I/O counters were invalid: {type(exc).__name__}")
            return None, None

        now = self._clock()
        baseline = self._io_baseline
        self._io_baseline = (read_bytes, write_bytes, now)
        if baseline is None:
            unavailable["disk_read_bytes_per_sec"] = (
                MetricUnavailableReason.BASELINE_NOT_ESTABLISHED
            )
            unavailable["disk_write_bytes_per_sec"] = (
                MetricUnavailableReason.BASELINE_NOT_ESTABLISHED
            )
            return None, None

        previous_read, previous_write, previous_at = baseline
        elapsed = now - previous_at
        if elapsed <= 0.0:
            unavailable["disk_read_bytes_per_sec"] = (
                MetricUnavailableReason.INVALID_VALUE
            )
            unavailable["disk_write_bytes_per_sec"] = (
                MetricUnavailableReason.INVALID_VALUE
            )
            warnings.append(f"disk I/O elapsed time was not positive: {elapsed!r}")
            return None, None
        if read_bytes < previous_read or write_bytes < previous_write:
            unavailable["disk_read_bytes_per_sec"] = (
                MetricUnavailableReason.COUNTER_RESET
            )
            unavailable["disk_write_bytes_per_sec"] = (
                MetricUnavailableReason.COUNTER_RESET
            )
            warnings.append("disk I/O counters decreased; baseline was reset")
            return None, None

        read_rate = (read_bytes - previous_read) / elapsed
        write_rate = (write_bytes - previous_write) / elapsed
        return round(read_rate, 3), round(write_rate, 3)

    def _sample_active_queries(
        self,
        unavailable: dict[str, MetricUnavailableReason],
        warnings: list[str],
    ) -> int | None:
        if self._active_query_provider is None:
            unavailable["active_queries"] = (
                MetricUnavailableReason.NOT_APPLICABLE_TO_CLIENT_HOST
            )
            return None
        try:
            raw = self._active_query_provider()
        except Exception as exc:  # noqa: BLE001 - isolate optional metric
            unavailable["active_queries"] = MetricUnavailableReason.PSUTIL_ERROR
            warnings.append(f"active_queries provider failed: {type(exc).__name__}: {exc}")
            return None
        if raw is None:
            unavailable["active_queries"] = MetricUnavailableReason.PSUTIL_ERROR
            return None
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            unavailable["active_queries"] = MetricUnavailableReason.INVALID_VALUE
            warnings.append(f"active_queries provider returned invalid value {raw!r}")
            return None
        return raw


class UnavailableDbHostSampler:
    """Represent a DB-host metrics source that is intentionally not configured.

    This class does not call ``psutil`` and does not use the client hostname or
    client values.  Its source is ``unknown`` and every metric is unavailable.
    """

    def __init__(
        self,
        *,
        source_host: str = "unknown",
        stale_after_ms: int = 5000,
        warning: str = (
            "no DB-host monitoring agent or exporter is configured; "
            "client-host psutil metrics were not used for the DB host"
        ),
    ):
        if stale_after_ms < 1:
            raise ValueError("stale_after_ms must be positive")
        self.source_host = source_host or "unknown"
        self.stale_after_ms = stale_after_ms
        self.warning = warning

    def sample(self) -> SystemSnapshot:
        return _unavailable_snapshot(
            source_host=self.source_host,
            source_type=SystemSourceType.UNKNOWN,
            reason=MetricUnavailableReason.NO_DB_HOST_AGENT,
            stale_after_ms=self.stale_after_ms,
            warnings=[self.warning],
            active_queries_reason=MetricUnavailableReason.NO_DB_METRICS_SOURCE,
        )


class SystemMonitor:
    """Periodic sampler for client-host and DB-host snapshots.

    A sampler exception is logged and converted to an invalid snapshot with an
    explicit error and unavailable reasons.  It is never silently replaced by a
    stale value or a zero.
    """

    def __init__(
        self,
        *,
        client_sampler: SystemSampler | None = None,
        db_host_sampler: SystemSampler | None = None,
        interval_s: float = 1.0,
        history_size: int = 120,
    ):
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        if history_size < 1:
            raise ValueError("history_size must be positive")
        self.client_sampler = client_sampler or PsutilSystemSampler()
        self.db_host_sampler = db_host_sampler or UnavailableDbHostSampler()
        self.interval_s = interval_s
        self._samples: deque[SystemSample] = deque(maxlen=history_size)
        self._latest: SystemSample | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.is_running:
                return
            self._stop_event = threading.Event()
            thread = threading.Thread(
                target=self._run,
                name="tsdb-system-monitor",
                daemon=True,
            )
            self._thread = thread
        # Capture immediately so callers do not have to wait one interval for
        # the first snapshot.  CPU/I-O values still need a second sample.
        self.sample_once()
        thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning(
                    "system monitor thread did not stop within %.3fs", timeout
                )
                return  # retain live thread identity; do not start a second sampler
        with self._lock:
            if self._thread is thread:
                self._thread = None

    def _run(self) -> None:
        deadline = time.monotonic() + self.interval_s
        while not self._stop_event.wait(max(0.0, deadline-time.monotonic())):
            try:
                self.sample_once()
            except Exception:  # noqa: BLE001 - monitor thread must stay alive
                logger.exception("periodic system sample failed unexpectedly")
            # Fixed cadence, with at most one immediate catch-up after an
            # overrun. Slow identity probes must not add a second full delay.
            deadline = max(deadline+self.interval_s, time.monotonic())

    def sample_once(self) -> SystemSample:
        # DB identity probes may be slow. Capture the cheap client snapshot
        # afterwards so DB probing does not age it before atomic publication.
        db_host = self._capture(
            self.db_host_sampler,
            source_type=SystemSourceType.UNKNOWN,
            host="unknown",
        )
        client = self._capture(
            self.client_sampler,
            source_type=SystemSourceType.CLIENT_HOST,
            host="unknown",
        )
        sample = SystemSample(client_host=client, db_host=db_host)
        with self._lock:
            self._latest = sample
            self._samples.append(sample)
        return sample

    @staticmethod
    def _capture(
        sampler: SystemSampler,
        *,
        source_type: SystemSourceType,
        host: str,
    ) -> SystemSnapshot:
        try:
            return sampler.sample()
        except Exception as exc:  # noqa: BLE001 - preserve a structured failure
            logger.exception(
                "system sampler failed",
                extra={"event": "system_sampler_error", "source_type": source_type.value},
            )
            reason = (
                MetricUnavailableReason.PSUTIL_ERROR
                if source_type is SystemSourceType.CLIENT_HOST
                else MetricUnavailableReason.NO_DB_HOST_AGENT
            )
            return _unavailable_snapshot(
                source_host=host,
                source_type=source_type,
                reason=reason,
                active_queries_reason=(
                    MetricUnavailableReason.NO_DB_METRICS_SOURCE
                    if source_type is not SystemSourceType.CLIENT_HOST
                    else None
                ),
                stale_after_ms=5000,
                warnings=[f"sampler raised {type(exc).__name__}"],
                error_type=type(exc).__name__,
                error_message=str(exc)[:4000],
            )

    def latest_sample(self) -> SystemSample | None:
        with self._lock:
            return self._latest

    def fresh_sample(
        self,
        *,
        now: Any | None = None,
        max_age_ms: int | None = None,
    ) -> SystemSample | None:
        """Return the latest sample only if its client snapshot is fresh."""

        sample = self.latest_sample()
        if sample is None:
            return None
        if not sample.client_host.is_fresh(now=now, max_age_ms=max_age_ms):
            return None
        return sample

    def history(self) -> tuple[SystemSample, ...]:
        with self._lock:
            return tuple(self._samples)


def sample_snapshot_ids(sample: SystemSample) -> list[str]:
    """Return snapshot IDs in a stable client-then-DB order."""

    ids = [sample.client_host.snapshot_id, sample.db_host.snapshot_id]
    if sample.db_metrics is not None:
        ids.append(sample.db_metrics.snapshot_id)
    return ids


def sample_snapshots(sample: SystemSample) -> list[SystemSnapshot]:
    snapshots = [sample.client_host, sample.db_host]
    if sample.db_metrics is not None:
        snapshots.append(sample.db_metrics)
    return snapshots
