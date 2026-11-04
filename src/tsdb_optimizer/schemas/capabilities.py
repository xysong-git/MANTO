"""Doctor and capability-report contracts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ServerInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = "unknown"
    version: str | None = None
    build_info: str | None = None
    client_version: str | None = None


class CapabilityReport(BaseModel):
    """Only capabilities actually probed by the doctor are marked true."""

    model_config = ConfigDict(extra="forbid")

    databases: list[str] = Field(default_factory=list)
    show_version: bool = False
    show_databases: bool = False
    show_timeseries: bool = False
    explain_text: bool = False
    explain_last_query: bool = False
    # EXPLAIN ANALYZE executes the query.  Doctor never runs it on a normal path.
    # The value is set only after a connected, version-certified probe; a
    # disconnected or unsupported-version report must not advertise it.
    explain_analyze: str = "not_probed"
    notes: list[str] = Field(default_factory=list)


class DoctorReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = False
    connected: bool = False
    server: ServerInfo | None = None
    capabilities: CapabilityReport = Field(default_factory=CapabilityReport)
    elapsed_ms: float = Field(default=0.0, ge=0.0)
    warnings: list[str] = Field(default_factory=list)
    error_type: str | None = None
    error_message: str | None = None
