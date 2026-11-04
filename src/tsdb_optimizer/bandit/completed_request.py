"""Finalized client request timing for offline E2E learning, never DB latency.

Recorded only AFTER Orchestrator.run returns (including its store cleanup).
The offline fitting/audit cost is separate; this is not an online callback.
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..orchestration.models import WorkflowResult
from ..schemas.common import ensure_utc
from ..schemas.learning import Experience, digest

E2E_REWARD_VERSION = 'negative-e2e-seconds-v1'


class CompletedRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)
    schema_version: Literal[1] = 1
    reward_version: Literal['negative-e2e-seconds-v1'] = E2E_REWARD_VERSION
    source: Literal['client_orchestrator_perf_counter_ns'] = 'client_orchestrator_perf_counter_ns'
    query_id: str
    decision_id: str
    execution_id: str
    started_at: datetime
    finished_at: datetime
    elapsed_ns: int = Field(gt=0, strict=True)
    workflow_sha256: str
    experience_sha256: str

    @field_validator('started_at', 'finished_at')
    @classmethod
    def utc(cls, value):
        return ensure_utc(value)

    @property
    def end_to_end_ms(self):
        return self.elapsed_ns / 1_000_000

    @property
    def reward(self):
        # Linear scaling preserves the arithmetic-mean E2E objective.
        return -self.elapsed_ns / 1_000_000_000

    def validate_evidence(self, experience, workflow):
        e = Experience.model_validate(experience.model_dump(round_trip=True))
        w = WorkflowResult.model_validate(workflow.model_dump(round_trip=True))
        if (self.workflow_sha256 != digest(w.model_dump(mode='json', round_trip=True))
                or self.experience_sha256 != digest(e.model_dump(mode='json', round_trip=True))):
            raise ValueError('completed request evidence digest mismatch')
        if (self.query_id, self.decision_id, self.execution_id) != (e.query_id, e.decision_id, e.execution_id):
            raise ValueError('completed request identity mismatch')
        if (e.sample_status.value != 'VALID' or e.quality_reasons or w.run is None
                or w.trace.events[-1].state.value != 'SUCCEEDED' or w.trace.warnings
                or w.run.execution.warnings or w.run.execution.fallback_used
                or len(w.run.execution.attempts) != 1 or any(f.warnings for f in w.feedback)):
            raise ValueError('unqualified completed request')
        r = w.run.execution.attempts[0].execution
        if (r.execution_id != e.execution_id or r.query_id != e.query_id
                or w.run.decision.decision_id != e.decision_id or r.strategy_id != e.strategy_id
                or w.trace.sql_sha256 != e.query_hash
                or not any(record == e.record for record in w.records)):
            raise ValueError('completed workflow and experience differ')
        if (self.started_at > w.trace.events[0].at
                or self.started_at > e.record.started_at
                or self.finished_at < e.record.finished_at
                or self.finished_at < w.trace.events[-1].at):
            raise ValueError('request UTC interval does not enclose workflow')
        wall_ms = (self.finished_at-self.started_at).total_seconds()*1000
        # Clock jumps invalidate feedback rather than relabeling monotonic timing.
        if wall_ms <= 0 or abs(wall_ms-self.end_to_end_ms) > 5:
            raise ValueError('UTC/monotonic request clock disagreement')
        if self.end_to_end_ms < max(e.record.client_wall_ms, w.trace.events[-1].elapsed_ms):
            raise ValueError('request timer excludes workflow stages')
        return e

    @classmethod
    def capture(cls, experience, workflow, *, started_at, finished_at, elapsed_ns):
        result = cls(query_id=experience.query_id, decision_id=experience.decision_id,
            execution_id=experience.execution_id, started_at=started_at, finished_at=finished_at,
            elapsed_ns=elapsed_ns,
            workflow_sha256=digest(workflow.model_dump(mode='json', round_trip=True)),
            experience_sha256=digest(experience.model_dump(mode='json', round_trip=True)))
        result.validate_evidence(experience, workflow)
        return result
