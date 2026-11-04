"""The only source from which Agent 2 may select a strategy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..errors import StrategyRegistryError
from ..parsing import SQLParserAdapter
from .catalog import StrategyCatalog, load_strategy_catalog
from .models import (
    CandidatePlan,
    StrategySpec,
    StrategyStatus,
    ValidationContext,
    ValidationResult,
)
from .native import NativeStrategy
from .protocol import OptimizationStrategy
from .repeat_cache import CACHE_STRATEGY_ID, RepeatCacheStrategy
from .validator import StrategyValidator

BASELINE_STRATEGY_ID = "S0_NATIVE"


@dataclass(frozen=True)
class RegisteredStrategy:
    spec: StrategySpec
    plugin: OptimizationStrategy


class StrategyRegistry:
    """An explicit, finite registry of strategy specs and implementations.

    Disabled entries may be inspected but are never returned by
    :meth:`enabled_entries` and therefore cannot be selected by Agent 2.

    The registry owns the central :class:`StrategyValidator`.  Decision Engine
    and Executor must call :meth:`validate_candidate` in addition to any
    plugin-supplied validation; a plugin cannot approve itself.
    """

    def __init__(
        self,
        baseline_strategy_id: str = BASELINE_STRATEGY_ID,
        *,
        validator: StrategyValidator | None = None,
        catalog: StrategyCatalog | None = None,
        allow_uncertified_for_testing: bool = False,
    ):
        if catalog is None and not allow_uncertified_for_testing:
            raise StrategyRegistryError(
                "a strategy certification catalog is required; pass "
                "catalog=load_strategy_catalog() or explicitly opt out with "
                "allow_uncertified_for_testing=True in tests only"
            )
        self.baseline_strategy_id = baseline_strategy_id
        self.validator = validator or StrategyValidator()
        self.catalog = catalog
        self._entries: dict[str, RegisteredStrategy] = {}
        self._runtime_quarantine: set[tuple[str, str]] = set()
        self._quarantine_checker: Callable[[str, str], bool] | None = None

    def _assert_catalog_certification(
        self, plugin: OptimizationStrategy, spec: StrategySpec
    ) -> None:
        if self.catalog is None:
            # The only way to reach this branch is the explicit test-only
            # opt-out checked in __init__.
            return
        certification = self.catalog.certification_for(spec.strategy_id)
        if certification is None:
            raise StrategyRegistryError(
                f"strategy {spec.strategy_id} is not in the external "
                "certification catalog"
            )
        mismatches = certification.mismatch_reasons(spec)
        if mismatches:
            raise StrategyRegistryError(
                f"strategy {spec.strategy_id} does not match the external "
                f"certification catalog: {mismatches}"
            )
        # The spec's implementation_class string is supplied by the plugin, so
        # matching it against the catalog alone would still trust plugin
        # metadata.  Compare it with the actual plugin class as well; otherwise
        # any plugin could claim a certified implementation class.
        actual_class = (
            f"{type(plugin).__module__}.{type(plugin).__qualname__}"
        )
        if certification.implementation_class != actual_class:
            raise StrategyRegistryError(
                f"strategy {spec.strategy_id} implementation class "
                f"{actual_class!r} does not match the certified class "
                f"{certification.implementation_class!r}"
            )

    def register(
        self,
        plugin: OptimizationStrategy,
        *,
        spec: StrategySpec | None = None,
    ) -> RegisteredStrategy:
        effective_spec = spec or plugin.spec
        if not isinstance(effective_spec, StrategySpec):
            raise StrategyRegistryError("strategy spec must be a StrategySpec")
        if getattr(plugin, "spec", None) is None:
            raise StrategyRegistryError("strategy plugin has no spec")
        if plugin.spec.strategy_id != effective_spec.strategy_id:
            raise StrategyRegistryError(
                "plugin strategy_id does not match the supplied spec"
            )
        if effective_spec != plugin.spec:
            raise StrategyRegistryError(
                "supplied spec does not match the plugin's own spec"
            )
        self._assert_catalog_certification(plugin, effective_spec)
        if effective_spec.strategy_id in self._entries:
            raise StrategyRegistryError(
                f"strategy already registered: {effective_spec.strategy_id}"
            )
        if effective_spec.enabled:
            if effective_spec.verification_status is not StrategyStatus.VERIFIED:
                raise StrategyRegistryError(
                    f"refusing to enable unverified strategy "
                    f"{effective_spec.strategy_id} "
                    f"({effective_spec.verification_status})"
                )
            if effective_spec.is_baseline:
                if effective_spec.strategy_id != self.baseline_strategy_id:
                    raise StrategyRegistryError(
                        f"baseline must be {self.baseline_strategy_id!r}, "
                        f"got {effective_spec.strategy_id!r}"
                    )
                other_baselines = [
                    entry.spec.strategy_id
                    for entry in self._entries.values()
                    if entry.spec.enabled and entry.spec.is_baseline
                ]
                if other_baselines:
                    raise StrategyRegistryError(
                        f"more than one enabled baseline: {other_baselines}"
                    )
        entry = RegisteredStrategy(spec=effective_spec, plugin=plugin)
        self._entries[effective_spec.strategy_id] = entry
        return entry

    def get(self, strategy_id: str) -> RegisteredStrategy:
        try:
            return self._entries[strategy_id]
        except KeyError as exc:
            raise StrategyRegistryError(
                f"strategy is not registered: {strategy_id}"
            ) from exc

    def set_quarantine_checker(self, checker: Callable[[str, str], bool]) -> None:
        """Bind persisted runtime quarantine; lookup errors intentionally fail closed."""
        self._quarantine_checker = checker

    def quarantine_version(self, strategy_id: str, version: str) -> None:
        """Immediately block known bad runtime versions, even if audit writes fail."""
        self._runtime_quarantine.add((strategy_id, version))

    def _quarantined(self, entry: RegisteredStrategy) -> bool:
        return ((entry.spec.strategy_id, entry.spec.version) in self._runtime_quarantine
                or (self._quarantine_checker is not None
                    and self._quarantine_checker(entry.spec.strategy_id, entry.spec.version)))

    def get_enabled(self, strategy_id: str) -> RegisteredStrategy:
        entry = self.get(strategy_id)
        if not entry.spec.enabled:
            raise StrategyRegistryError(f"strategy is disabled: {strategy_id}")
        if self._quarantined(entry):
            raise StrategyRegistryError(f"strategy version is quarantined: {strategy_id}/{entry.spec.version}")
        return entry

    @property
    def baseline(self) -> RegisteredStrategy:
        entry = self.get_enabled(self.baseline_strategy_id)
        if not entry.spec.is_baseline:
            raise StrategyRegistryError(
                f"{self.baseline_strategy_id} is not marked as the baseline"
            )
        if entry.spec.verification_status is not StrategyStatus.VERIFIED:
            raise StrategyRegistryError(
                f"baseline {self.baseline_strategy_id} is not VERIFIED"
            )
        return entry

    def enabled_entries(self) -> tuple[RegisteredStrategy, ...]:
        entries = [entry for entry in self._entries.values()
                   if entry.spec.enabled and not self._quarantined(entry)]
        entries.sort(
            key=lambda entry: (
                0 if entry.spec.strategy_id == self.baseline_strategy_id else 1,
                entry.spec.strategy_id,
            )
        )
        return tuple(entries)

    def all_entries(self) -> tuple[RegisteredStrategy, ...]:
        return tuple(
            sorted(self._entries.values(), key=lambda entry: entry.spec.strategy_id)
        )

    def selectable_ids(self) -> tuple[str, ...]:
        return tuple(entry.spec.strategy_id for entry in self.enabled_entries())

    def assert_selectable(self, strategy_id: str) -> RegisteredStrategy:
        entry = self.get_enabled(strategy_id)
        if not entry.spec.enabled:
            raise StrategyRegistryError(f"strategy is disabled: {strategy_id}")
        if entry.spec.verification_status is not StrategyStatus.VERIFIED:
            raise StrategyRegistryError(
                f"strategy is not VERIFIED: {strategy_id}"
            )
        return entry

    def assert_catalog_coverage(self) -> None:
        """Require every enabled catalog strategy to be registered here.

        The catalog is the external authority.  If it enables a strategy that
        the running code does not register, silently continuing would make the
        deployed strategy set narrower than the certified catalog.  That is a
        configuration/integration error and must fail closed.  Disabled catalog
        entries may be absent, because they are not selectable.
        """

        if self.catalog is None:
            return
        expected = {
            entry.strategy_id
            for entry in self.catalog.strategies
            if entry.enabled
        }
        actual = set(self.selectable_ids())
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        if missing or unexpected:
            raise StrategyRegistryError(
                "registered strategies do not match the enabled catalog "
                f"entries; missing={missing}, unexpected={unexpected}"
            )

    def validate_candidate(
        self,
        entry: RegisteredStrategy,
        original_sql: str,
        candidate: CandidatePlan,
        context: ValidationContext,
    ) -> ValidationResult:
        """Run the registry-owned validation that no plugin may bypass.

        The entry must still be the exact spec/plugin pair that was registered.
        This makes central validation independent of a plugin's own
        ``validate`` implementation and of post-registration spec mutation.
        """

        if entry.spec != entry.plugin.spec:
            raise StrategyRegistryError(
                f"registered spec for {entry.spec.strategy_id} no longer matches "
                "the plugin's own spec"
            )
        return self.validator.validate(
            entry.spec,
            original_sql,
            candidate,
            context,
        )


def build_default_registry(
    parser: SQLParserAdapter | None = None,
    *,
    catalog: StrategyCatalog | None = None,
    catalog_path: Path | str | None = None,
    allow_uncertified_for_testing: bool = False,
    cache_epoch: str | None = None,
) -> StrategyRegistry:
    """Build the finite Phase-4 registry from the external catalog.

    By default the catalog is required and loaded from
    ``configs/strategies.yaml``.  A missing/invalid catalog fails closed.  The
    registry always contains ``S0_NATIVE``.  It also registers the certified
    ``S1_REPEAT_CACHE`` strategy whenever the effective catalog enables it; a
    catalog that omits or disables S1 yields a Native-only deployment, and a
    catalog that enables it without registration still fails the coverage
    check.  ``cache_epoch`` is a caller-owned dataset epoch; without it the
    repeat cache is registered but reports itself as not applicable, so no
    freshness claim is made.
    """

    if catalog is not None and catalog_path is not None:
        raise StrategyRegistryError(
            "pass either catalog or catalog_path, not both"
        )
    effective_catalog = catalog
    if effective_catalog is None and not allow_uncertified_for_testing:
        effective_catalog = load_strategy_catalog(catalog_path)
    validator = StrategyValidator(parser=parser or SQLParserAdapter())
    registry = StrategyRegistry(
        validator=validator,
        catalog=effective_catalog,
        allow_uncertified_for_testing=allow_uncertified_for_testing,
    )
    registry.register(NativeStrategy(validator=validator))
    cache_certification = (
        effective_catalog.certification_for(CACHE_STRATEGY_ID)
        if effective_catalog is not None
        else None
    )
    if effective_catalog is None or (
        cache_certification is not None and cache_certification.enabled
    ):
        registry.register(RepeatCacheStrategy(cache_epoch=cache_epoch))
    # Deliberately assert the baseline contract and full catalog coverage at
    # construction time.  An enabled catalog entry without an implementation
    # fails closed instead of being silently ignored.
    registry.baseline
    registry.assert_catalog_coverage()
    return registry


__all__ = [
    "BASELINE_STRATEGY_ID",
    "RegisteredStrategy",
    "StrategyRegistry",
    "build_default_registry",
]
