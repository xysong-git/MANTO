"""External, auditable strategy certification catalog.

The registry must not trust a plugin's self-declared ``VERIFIED`` status.  A
strategy is registerable only when an external catalog entry names the same
strategy version, implementation, enabled flag and compatibility tuple, and
when every enabled non-Native strategy records all five independent proofs
required by ``IMPLEMENTATION_GUIDE.md``.

This module loads and validates declarative data only.  It never imports a
strategy implementation, connects to IoTDB, or promotes a disabled candidate.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..errors import StrategyCatalogError
from .models import SelectionPolicy, StrategySpec, StrategyStatus

CATALOG_SCHEMA_VERSION = 1
DEFAULT_CATALOG_PATH = (
    Path(__file__).resolve().parents[3] / "configs" / "strategies.yaml"
)


class StrategyCertification(BaseModel):
    """One independently audited strategy declaration.

    ``version`` and ``implementation_class`` are optional only for disabled
    catalog entries that have no implementation.  Any entry that is actually
    registered must match both fields exactly.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Z][A-Z0-9_]*$",
    )
    version: str | None = Field(default=None, min_length=1, max_length=100)
    database_model: str = Field(pattern=r"^(tree|table|unknown)$")
    compatible_server_versions: tuple[str, ...] = Field(min_length=1)
    enabled: bool
    verification_status: StrategyStatus
    is_baseline: bool = False
    selection_policy: SelectionPolicy = SelectionPolicy.LOWEST_COST
    implementation_class: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
    )
    allowed_execution_options: tuple[str, ...] = ()
    proof_entry: str | None = Field(default=None, max_length=2000)
    proof_correctness: str | None = Field(default=None, max_length=2000)
    proof_effect: str | None = Field(default=None, max_length=2000)
    proof_benefit: str | None = Field(default=None, max_length=2000)
    proof_fallback: str | None = Field(default=None, max_length=2000)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def enabled_entries_are_complete(self) -> "StrategyCertification":
        if self.enabled:
            if self.verification_status is not StrategyStatus.VERIFIED:
                raise ValueError("an enabled catalog entry must be VERIFIED")
            if self.version is None:
                raise ValueError("an enabled catalog entry needs a version")
            if self.implementation_class is None:
                raise ValueError(
                    "an enabled catalog entry needs an implementation_class"
                )
        if self.is_baseline and not self.enabled:
            raise ValueError("a baseline entry must be enabled")
        if self.enabled and not self.is_baseline:
            proofs = {
                "proof_entry": self.proof_entry,
                "proof_correctness": self.proof_correctness,
                "proof_effect": self.proof_effect,
                "proof_benefit": self.proof_benefit,
                "proof_fallback": self.proof_fallback,
            }
            missing = [
                name for name, value in proofs.items() if not (value or "").strip()
            ]
            if missing:
                raise ValueError(
                    "an enabled non-Native strategy must record all five "
                    f"proofs; missing: {missing}"
                )
        return self

    def mismatch_reasons(self, spec: StrategySpec) -> list[str]:
        """Return every field where the catalog and plugin spec disagree."""

        mismatches: list[str] = []
        if self.strategy_id != spec.strategy_id:
            mismatches.append("strategy_id")
        if self.version != spec.version:
            mismatches.append("version")
        if self.database_model != spec.database_model:
            mismatches.append("database_model")
        if self.compatible_server_versions != spec.compatible_server_versions:
            mismatches.append("compatible_server_versions")
        if self.enabled != spec.enabled:
            mismatches.append("enabled")
        if self.verification_status is not spec.verification_status:
            mismatches.append("verification_status")
        if self.is_baseline != spec.is_baseline:
            mismatches.append("is_baseline")
        if self.selection_policy is not spec.selection_policy:
            mismatches.append("selection_policy")
        if self.implementation_class != spec.implementation_class:
            mismatches.append("implementation_class")
        if self.allowed_execution_options != spec.allowed_execution_options:
            mismatches.append("allowed_execution_options")
        return mismatches


class StrategyCatalog(BaseModel):
    """Versioned certification catalog with exactly one enabled baseline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(ge=1)
    strategies: tuple[StrategyCertification, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def catalog_is_consistent(self) -> "StrategyCatalog":
        if self.schema_version != CATALOG_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported catalog schema_version {self.schema_version}; "
                f"expected {CATALOG_SCHEMA_VERSION}"
            )
        ids = [entry.strategy_id for entry in self.strategies]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"duplicate catalog strategy ids: {duplicates}")
        baselines = [
            entry.strategy_id
            for entry in self.strategies
            if entry.enabled and entry.is_baseline
        ]
        if len(baselines) != 1:
            raise ValueError(
                "catalog must contain exactly one enabled baseline; "
                f"found {baselines}"
            )
        return self

    @property
    def by_id(self) -> dict[str, StrategyCertification]:
        return {entry.strategy_id: entry for entry in self.strategies}

    @property
    def enabled_non_baseline(self) -> tuple[StrategyCertification, ...]:
        return tuple(
            entry
            for entry in self.strategies
            if entry.enabled and not entry.is_baseline
        )

    def certification_for(self, strategy_id: str) -> StrategyCertification | None:
        return self.by_id.get(strategy_id)


def load_strategy_catalog(path: Path | str | None = None) -> StrategyCatalog:
    """Load and strictly validate the external catalog.

    Missing files, unreadable files, malformed YAML and schema violations are
    all converted to :class:`StrategyCatalogError`.  The caller must fail
    closed; this function never returns a partial catalog.
    """

    catalog_path = Path(path) if path is not None else DEFAULT_CATALOG_PATH
    try:
        raw_text = catalog_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StrategyCatalogError(
            f"could not read strategy catalog {catalog_path}: {exc}"
        ) from exc
    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise StrategyCatalogError(
            f"strategy catalog is not valid YAML: {catalog_path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise StrategyCatalogError(
            f"strategy catalog root must be a mapping: {catalog_path}"
        )
    try:
        return StrategyCatalog.model_validate(raw)
    except ValidationError as exc:
        raise StrategyCatalogError(
            f"strategy catalog failed validation: {catalog_path}: {exc}"
        ) from exc


__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "DEFAULT_CATALOG_PATH",
    "StrategyCatalog",
    "StrategyCertification",
    "load_strategy_catalog",
]
