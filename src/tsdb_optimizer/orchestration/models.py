"""Phase-6 lifecycle and redacted, ID-linked trace contracts."""
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, model_serializer

from ..execution.models import OptimizationRun
from ..schemas.common import ensure_utc, utc_now
from ..schemas.learning import LearningFeedback
from ..schemas.monitoring import ExecutionRecord


class State(StrEnum):
    RECEIVED = 'RECEIVED'
    PARSED = 'PARSED'
    SNAPSHOT = 'SNAPSHOT'
    RETRIEVED = 'RETRIEVED'
    DECIDED = 'DECIDED'
    VALIDATED = 'VALIDATED'
    EXECUTED = 'EXECUTED'
    MONITORED = 'MONITORED'
    LEARNED = 'LEARNED'
    SUCCEEDED = 'SUCCEEDED'
    REJECTED = 'REJECTED'
    FAILED = 'FAILED'
    TIMED_OUT = 'TIMED_OUT'


STAGES = list(State)[:9]
TERMINAL = {State.SUCCEEDED, State.REJECTED, State.FAILED, State.TIMED_OUT}


class TraceEvent(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    index: int = Field(ge=0)
    state: State
    at: datetime = Field(default_factory=utc_now)
    elapsed_ms: float = Field(ge=0)
    query_id: str
    decision_id: str | None = None
    execution_ids: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator('at')
    @classmethod
    def utc(cls, value):
        return ensure_utc(value)


class WorkflowTrace(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal[1] = 1
    query_id: str
    sql_sha256: str
    requested_mode: str
    effective_mode: str
    events: list[TraceEvent] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # None when an executor exception leaves dispatch/completion unconfirmed.
    database_dispatches: int | None = Field(default=0, ge=0)

    @model_validator(mode='after')
    def ordered(self):
        previous = None
        for i, event in enumerate(self.events):
            if event.index != i or event.query_id != self.query_id:
                raise ValueError('trace event identity/order mismatch')
            if previous is None:
                if event.state != State.RECEIVED:
                    raise ValueError('trace must start at RECEIVED')
            else:
                if previous.state in TERMINAL:
                    raise ValueError('terminal state cannot transition')
                expected = STAGES[STAGES.index(previous.state)+1] if previous.state != State.LEARNED else None
                if event.state not in TERMINAL and event.state != expected:
                    raise ValueError('invalid lifecycle transition')
                if event.state in {State.SUCCEEDED, State.TIMED_OUT} and previous.state != State.LEARNED:
                    raise ValueError('completed execution requires monitoring/learning stages')
                if event.elapsed_ms < previous.elapsed_ms:
                    raise ValueError('trace clock moved backwards')
            previous = event
        return self


class RequestTiming(BaseModel):
    """Client monotonic timings; never interpreted as DB execution metrics."""
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)
    store_open_ms: float = Field(ge=0)
    body_ms: float = Field(ge=0)
    store_close_ms: float = Field(ge=0)
    total_ms: float = Field(ge=0)

    @model_validator(mode='after')
    def additive(self):
        if abs(self.total_ms - self.store_open_ms - self.body_ms - self.store_close_ms) > .000001:
            raise ValueError('request timings must add to total')
        return self


class WorkflowResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    trace: WorkflowTrace
    run: OptimizationRun | None = None
    records: list[ExecutionRecord] = Field(default_factory=list)
    feedback: list[LearningFeedback] = Field(default_factory=list)
    # Added after the store session has closed. Historical logs remain readable.
    request_timing: RequestTiming | None = None

    @model_serializer(mode='wrap')
    def preserve_historical_receipts(self, handler):
        data = handler(self)
        if self.request_timing is None:
            data.pop('request_timing', None)
        return data

    @model_validator(mode='after')
    def linked(self):
        if not self.trace.events or self.trace.events[-1].state not in TERMINAL:
            raise ValueError('workflow result must be terminal')
        if self.run is not None:
            decision = self.run.decision
            ids = {a.execution.execution_id for a in self.run.execution.attempts}
            if decision.query_id != self.trace.query_id:
                raise ValueError('workflow query identity mismatch')
            terminal = self.trace.events[-1].state
            final = self.run.execution.attempts[-1].execution
            expected = State.SUCCEEDED if final.status.value == 'SUCCESS' else (
                State.TIMED_OUT if final.timed_out else State.FAILED)
            if terminal != expected:
                raise ValueError('terminal state must preserve actual execution outcome')
            if self.trace.database_dispatches != sum(a.execution.request_dispatched for a in self.run.execution.attempts):
                raise ValueError('trace dispatch count differs from execution evidence')
            for record in self.records:
                if (record.query_id != decision.query_id or record.decision_id != decision.decision_id
                        or record.execution_id not in ids):
                    raise ValueError('monitor record identity mismatch')
            for feedback in self.feedback:
                if feedback.query_id != decision.query_id or feedback.execution_id not in ids:
                    raise ValueError('feedback identity mismatch')
            for event in self.trace.events:
                if event.decision_id not in (None, decision.decision_id) or not set(event.execution_ids) <= ids:
                    raise ValueError('trace correlation mismatch')
        elif self.trace.events[-1].state in {State.SUCCEEDED, State.TIMED_OUT}:
            raise ValueError('successful/timed-out workflow requires actual execution evidence')
        return self
