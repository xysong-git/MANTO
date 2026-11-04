"""Bounded raw-counter evidence for benchmark admission diagnostics only."""
from collections import deque
import threading
import time

import psutil
from pydantic import BaseModel, ConfigDict

from ..schemas.common import utc_now


class ProcessReading(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)
    pid: int
    created_at_s: float | None = None
    user_s: float | None = None
    system_s: float | None = None
    monotonic_ns: int | None = None
    error: str | None = None


class CpuReading(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)
    captured_at: str
    monotonic_ns: int
    counters: dict[str, float]
    processes: dict[str, ProcessReading]


class CpuEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)
    protocol: str = 'benchmark-cpu-counter-evidence-v1'
    snapshot_id: str
    source_type: str
    cpu_percent: float | None
    previous: CpuReading | None
    current: CpuReading | None
    capture_error: str | None = None
    attribution: str = 'APPROXIMATE_PROCESS_WINDOWS; OTHER_HOST_ACTIVITY_UNKNOWN'


class CounterProbe:
    """Pass through exactly the CPU counters used by the existing sampler.

    Process counters are separate diagnostic observations, never replacements
    for host CPU. No full command lines, SQL, or credentials are collected.
    """
    def __init__(self, processes, module=psutil):
        self.module = module
        self.processes = processes
        self.previous = self.current = None
        self.error = None

    def __getattr__(self, name):
        return getattr(self.module, name)

    def cpu_times(self):
        self.previous, self.current = self.current, None
        self.error = None
        try:
            counters = self.module.cpu_times()
        except Exception as exc:
            self.error = type(exc).__name__
            raise
        at, mono = utc_now().isoformat(), time.monotonic_ns()
        processes = {}
        for label, pid in self.processes.items():
            try:
                p = self.module.Process(pid)
                created = p.create_time()
                times = p.cpu_times()
                processes[label] = {'pid': pid, 'created_at_s': created,
                    'user_s': times.user, 'system_s': times.system,
                    'monotonic_ns': time.monotonic_ns(), 'error': None}
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as exc:
                processes[label] = {'pid': pid, 'error': type(exc).__name__}
        self.current = CpuReading(captured_at=at, monotonic_ns=mono,
            counters=counters._asdict(), processes=processes)
        return counters


class EvidenceSampler:
    """Keep evidence keyed by the returned snapshot, not the latest reading."""
    def __init__(self, sampler, probe):
        self.sampler, self.probe = sampler, probe
        self._history = deque(maxlen=32)
        self._lock = threading.Lock()

    def sample(self):
        with self._lock:
            snapshot = self.sampler.sample()
            evidence = CpuEvidence(snapshot_id=snapshot.snapshot_id,
                source_type=snapshot.source_type.value, cpu_percent=snapshot.cpu_percent,
                previous=self.probe.previous, current=self.probe.current,
                capture_error=self.probe.error)
            self._history.append(evidence)
            return snapshot

    def evidence_for(self, snapshot):
        with self._lock:
            return next((e.model_dump(mode='json') for e in self._history
                         if e.snapshot_id == snapshot.snapshot_id), None)
