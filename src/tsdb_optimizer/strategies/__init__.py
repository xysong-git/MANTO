"""Phase-4 strategy registry, validation and Native baseline."""

from .catalog import (
    CATALOG_SCHEMA_VERSION,
    DEFAULT_CATALOG_PATH,
    StrategyCatalog,
    StrategyCertification,
    load_strategy_catalog,
)
from .models import (
    ApplicabilityResult,
    CandidateContext,
    CandidatePlan,
    SelectionPolicy,
    StrategySpec,
    StrategyStatus,
    ValidationCheck,
    ValidationCheckStatus,
    ValidationContext,
    ValidationResult,
    ValidationStatus,
)
from .native import NativeStrategy, native_strategy_spec
from .protocol import OptimizationStrategy
from .repeat_cache import (
    CACHE_EPOCH_OPTION,
    CACHE_STRATEGY_ID,
    RepeatCacheStrategy,
    check_cacheable_sql,
    repeat_cache_strategy_spec,
)
from .registry import (
    BASELINE_STRATEGY_ID,
    RegisteredStrategy,
    StrategyRegistry,
    build_default_registry,
)
from .validator import StrategyValidator

__all__ = [
    "ApplicabilityResult",
    "BASELINE_STRATEGY_ID",
    "CACHE_EPOCH_OPTION",
    "CACHE_STRATEGY_ID",
    "CATALOG_SCHEMA_VERSION",
    "CandidateContext",
    "CandidatePlan",
    "DEFAULT_CATALOG_PATH",
    "NativeStrategy",
    "OptimizationStrategy",
    "RegisteredStrategy",
    "RepeatCacheStrategy",
    "SelectionPolicy",
    "StrategyCatalog",
    "StrategyCertification",
    "StrategyRegistry",
    "StrategySpec",
    "StrategyStatus",
    "StrategyValidator",
    "ValidationCheck",
    "ValidationCheckStatus",
    "ValidationContext",
    "ValidationResult",
    "ValidationStatus",
    "build_default_registry",
    "check_cacheable_sql",
    "load_strategy_catalog",
    "native_strategy_spec",
    "repeat_cache_strategy_spec",
]
