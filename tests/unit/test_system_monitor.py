from __future__ import annotations

import time
from types import SimpleNamespace

from tsdb_optimizer.monitoring.system import (
    PsutilSystemSampler,
    SystemMonitor,
    UnavailableDbHostSampler,
)
from tsdb_optimizer.schemas.common import utc_now
from tsdb_optimizer.schemas.monitoring import (
    MetricUnavailableReason,
    SystemSnapshot,
    SystemSourceType,
)


class FakeCounters:
    def __init__(self, read_bytes: int, write_bytes: int):
        self.read_bytes = read_bytes
        self.write_bytes = write_bytes


class FakePsutil:
    def __init__(
        self,
        *,
        counters: list[FakeCounters] | None = None,
        memory_error: bool = False,
        disk_error: bool = False,
        io_error: bool = False,
        cpu_values: list[float] | None = None,
    ):
        self.counters = list(counters or [])
        self.memory_error = memory_error
        self.disk_error = disk_error
        self.io_error = io_error
        self.cpu_values = list(cpu_values or [10.0, 20.0, 30.0])
        self.cpu_calls = 0
        self.cpu_user = self.cpu_idle = 0
        self.io_calls = 0

    def cpu_times(self):
        from collections import namedtuple
        value = self.cpu_values[min(self.cpu_calls, len(self.cpu_values) - 1)]
        self.cpu_calls += 1
        self.cpu_user += value; self.cpu_idle += 100-value
        return namedtuple('CpuTimes','user idle')(self.cpu_user,self.cpu_idle)

    def virtual_memory(self):
        if self.memory_error:
            raise RuntimeError("memory failed")
        return SimpleNamespace(percent=50.0)

    def disk_usage(self, path):
        if self.disk_error:
            raise RuntimeError("disk failed")
        return SimpleNamespace(percent=60.0)

    def disk_io_counters(self, perdisk=False):
        if self.io_error:
            raise RuntimeError("io failed")
        value = self.counters[min(self.io_calls, len(self.counters) - 1)]
        self.io_calls += 1
        return value


class Clock:
    def __init__(self, values: list[float]):
        self.values = list(values)
        self.calls = 0

    def __call__(self) -> float:
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return value


def test_first_sample_uses_explicit_baselines_then_rates() -> None:
    fake = FakePsutil(
        counters=[FakeCounters(1000, 2000), FakeCounters(3000, 4000)]
    )
    sampler = PsutilSystemSampler(
        hostname="client-host",
        psutil_module=fake,
        clock=Clock([100.0, 102.0]),
    )

    first = sampler.sample()
    assert first.source_type is SystemSourceType.CLIENT_HOST
    assert first.source_host == "client-host"
    assert first.cpu_percent is None
    assert first.disk_read_bytes_per_sec is None
    assert first.disk_write_bytes_per_sec is None
    assert first.unavailable_fields["cpu_percent"] == (
        MetricUnavailableReason.BASELINE_NOT_ESTABLISHED
    )
    assert first.unavailable_fields["disk_read_bytes_per_sec"] == (
        MetricUnavailableReason.BASELINE_NOT_ESTABLISHED
    )
    assert first.valid is False
    assert first.memory_percent == 50.0
    assert first.disk_usage_percent == 60.0

    second = sampler.sample()
    assert second.cpu_percent == 20.0
    assert second.disk_read_bytes_per_sec == 1000.0
    assert second.disk_write_bytes_per_sec == 1000.0
    assert second.disk_usage_percent == 60.0
    assert second.valid is True
    assert second.unavailable_fields == {
        "active_queries": MetricUnavailableReason.NOT_APPLICABLE_TO_CLIENT_HOST
    }


def test_metric_errors_are_isolated_and_explicit() -> None:
    fake = FakePsutil(
        counters=[FakeCounters(1000, 2000), FakeCounters(1200, 2400)],
        memory_error=True,
    )
    sampler = PsutilSystemSampler(
        hostname="client-host",
        psutil_module=fake,
        clock=Clock([1.0, 2.0]),
    )
    sampler.sample()
    sample = sampler.sample()

    assert sample.memory_percent is None
    assert sample.unavailable_fields["memory_percent"] == (
        MetricUnavailableReason.PSUTIL_ERROR
    )
    assert sample.cpu_percent == 20.0
    assert sample.disk_usage_percent == 60.0
    assert sample.valid is False
    assert any("memory_percent failed" in warning for warning in sample.warnings)


def test_disk_io_counter_reset_is_not_masked() -> None:
    fake = FakePsutil(
        counters=[
            FakeCounters(1000, 2000),
            FakeCounters(500, 1500),
            FakeCounters(700, 1700),
        ]
    )
    sampler = PsutilSystemSampler(
        hostname="client-host",
        psutil_module=fake,
        clock=Clock([1.0, 2.0, 4.0]),
    )
    sampler.sample()
    reset = sampler.sample()
    assert reset.disk_read_bytes_per_sec is None
    assert reset.disk_write_bytes_per_sec is None
    assert reset.unavailable_fields["disk_read_bytes_per_sec"] == (
        MetricUnavailableReason.COUNTER_RESET
    )
    recovered = sampler.sample()
    assert recovered.disk_read_bytes_per_sec == 100.0
    assert recovered.disk_write_bytes_per_sec == 100.0


def test_psutil_io_error_marks_both_rate_fields() -> None:
    fake = FakePsutil(counters=[FakeCounters(1000, 2000)], io_error=True)
    sample = PsutilSystemSampler(
        hostname="h", psutil_module=fake, clock=Clock([1.0])
    ).sample()
    assert sample.disk_read_bytes_per_sec is None
    assert sample.disk_write_bytes_per_sec is None
    assert sample.unavailable_fields["disk_read_bytes_per_sec"] == (
        MetricUnavailableReason.PSUTIL_ERROR
    )
    assert sample.unavailable_fields["disk_write_bytes_per_sec"] == (
        MetricUnavailableReason.PSUTIL_ERROR
    )


def test_unavailable_db_host_sampler_never_uses_client_metrics() -> None:
    snapshot = UnavailableDbHostSampler().sample()
    assert snapshot.source_type is SystemSourceType.UNKNOWN
    assert snapshot.source_host == "unknown"
    assert snapshot.valid is False
    for field in (
        "cpu_percent",
        "memory_percent",
        "disk_usage_percent",
        "disk_read_bytes_per_sec",
        "disk_write_bytes_per_sec",
        "active_queries",
    ):
        assert getattr(snapshot, field) is None
    assert snapshot.unavailable_fields["cpu_percent"] == (
        MetricUnavailableReason.NO_DB_HOST_AGENT
    )
    assert snapshot.unavailable_fields["active_queries"] == (
        MetricUnavailableReason.NO_DB_METRICS_SOURCE
    )
    assert any("client-host psutil" in warning for warning in snapshot.warnings)


class FixedClientSampler:
    def __init__(self, snapshot: SystemSnapshot | None = None):
        self.snapshot = snapshot or SystemSnapshot(
            source_host="client",
            source_type=SystemSourceType.CLIENT_HOST,
            cpu_percent=1.0,
            memory_percent=2.0,
            disk_usage_percent=3.0,
            disk_read_bytes_per_sec=4.0,
            disk_write_bytes_per_sec=5.0,
            active_queries=None,
            valid=True,
            unavailable_fields={
                "active_queries": MetricUnavailableReason.NOT_APPLICABLE_TO_CLIENT_HOST
            },
        )
        self.calls = 0

    def sample(self) -> SystemSnapshot:
        self.calls += 1
        return self.snapshot


class RaisingSampler:
    def sample(self) -> SystemSnapshot:
        raise RuntimeError("sampler exploded")


def test_periodic_monitor_records_history_and_is_idempotent() -> None:
    sampler = FixedClientSampler()
    monitor = SystemMonitor(
        client_sampler=sampler,
        db_host_sampler=UnavailableDbHostSampler(),
        interval_s=0.01,
        history_size=10,
    )
    monitor.start()
    monitor.start()
    deadline = time.monotonic() + 1.0
    while len(monitor.history()) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    monitor.stop()
    monitor.stop()

    assert sampler.calls >= 3
    assert len(monitor.history()) >= 3
    latest = monitor.latest_sample()
    assert latest is not None
    assert latest.db_host.source_type is SystemSourceType.UNKNOWN
    assert monitor.fresh_sample() is not None
    assert monitor.is_running is False


def test_monitor_converts_sampler_exception_to_invalid_snapshot() -> None:
    monitor = SystemMonitor(
        client_sampler=RaisingSampler(),
        db_host_sampler=UnavailableDbHostSampler(),
        interval_s=1.0,
    )
    sample = monitor.sample_once()
    assert sample.client_host.source_type is SystemSourceType.CLIENT_HOST
    assert sample.client_host.valid is False
    assert sample.client_host.error_type == "RuntimeError"
    assert sample.client_host.cpu_percent is None
    assert sample.client_host.unavailable_fields["cpu_percent"] == (
        MetricUnavailableReason.PSUTIL_ERROR
    )


def test_fresh_sample_rejects_stale_latest() -> None:
    stale = SystemSnapshot(
        source_host="client",
        source_type=SystemSourceType.CLIENT_HOST,
        captured_at=utc_now().replace(year=2020),
        cpu_percent=1.0,
        memory_percent=2.0,
        disk_usage_percent=3.0,
        disk_read_bytes_per_sec=4.0,
        disk_write_bytes_per_sec=5.0,
        valid=True,
        stale_after_ms=1000,
        unavailable_fields={
            "active_queries": MetricUnavailableReason.NOT_APPLICABLE_TO_CLIENT_HOST
        },
    )
    monitor = SystemMonitor(
        client_sampler=FixedClientSampler(stale),
        db_host_sampler=UnavailableDbHostSampler(),
        interval_s=1.0,
    )
    monitor.sample_once()
    assert monitor.fresh_sample() is None
    assert monitor.latest_sample() is not None
