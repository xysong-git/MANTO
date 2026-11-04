"""Exception types shared by the Phase-1 foundation."""


class TSDBOptimizerError(Exception):
    """Base class for expected, user-facing errors."""


class ConfigError(TSDBOptimizerError):
    """Configuration is missing or invalid."""


class UnsafeSQLError(TSDBOptimizerError):
    """The SQL text is not a single, clearly read-only SELECT."""


class AdapterError(TSDBOptimizerError):
    """The database adapter could not complete the requested operation."""


class ParserInternalError(TSDBOptimizerError):
    """The parser reached an inconsistent state; callers must fail closed."""


class StrategyError(TSDBOptimizerError):
    """Base class for strategy registration, validation and execution errors."""


class StrategyRegistryError(StrategyError):
    """A strategy cannot be registered or selected from the registry."""


class StrategyCatalogError(StrategyError):
    """The external strategy certification catalog is missing or invalid."""


class StrategyValidationError(StrategyError):
    """A candidate strategy failed a safety or compatibility check."""


class DecisionError(TSDBOptimizerError):
    """No safe, registered strategy decision could be produced."""


class ExecutionError(TSDBOptimizerError):
    """The selected strategy could not be executed safely."""
