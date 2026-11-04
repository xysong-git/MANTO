"""Phase-2 (Agent 1) parser and query-feature contracts.

The parser behind these models is deliberately conservative: it only claims
the SQL subset exercised by the Phase-0 fixtures.  Every extracted feature
carries its provenance and, when absent, an explicit missing reason.  Unknown
values are represented by ``None``; this module never estimates row counts.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

T = TypeVar("T")


class ParseStatus(str, Enum):
    """Outcome of parsing one SQL statement."""

    SUCCESS = "SUCCESS"
    UNSUPPORTED = "UNSUPPORTED"
    ERROR = "ERROR"


class ParseFailureReason(str, Enum):
    """Why a parse did not produce a usable AST."""

    EMPTY_INPUT = "EMPTY_INPUT"
    MULTIPLE_STATEMENTS = "MULTIPLE_STATEMENTS"
    NOT_SELECT = "NOT_SELECT"
    SET_OPERATION = "SET_OPERATION"
    PARSER_CANNOT_PARSE = "PARSER_CANNOT_PARSE"
    UNSUPPORTED_SYNTAX = "UNSUPPORTED_SYNTAX"
    UNKNOWN_AST_NODE = "UNKNOWN_AST_NODE"
    INTERNAL_PARSER_ERROR = "INTERNAL_PARSER_ERROR"


class UnsupportedConstruct(str, Enum):
    """Constructs the Phase-2 parser refuses to interpret."""

    EMPTY_INPUT = "EMPTY_INPUT"
    MULTIPLE_STATEMENTS = "MULTIPLE_STATEMENTS"
    NOT_A_SELECT = "NOT_A_SELECT"
    SET_OPERATION = "SET_OPERATION"
    MISSING_FROM = "MISSING_FROM"
    SUBQUERY = "SUBQUERY"
    EXPLICIT_JOIN = "EXPLICIT_JOIN"
    TABLE_ALIAS = "TABLE_ALIAS"
    SELECT_ALIAS = "SELECT_ALIAS"
    LAST_SYNTAX = "LAST_SYNTAX"
    DISTINCT = "DISTINCT"
    GROUP_BY = "GROUP_BY"
    HAVING = "HAVING"
    ORDER_BY = "ORDER_BY"
    LIMIT = "LIMIT"
    OFFSET = "OFFSET"
    UNKNOWN_SELECT_ITEM = "UNKNOWN_SELECT_ITEM"
    UNKNOWN_FUNCTION = "UNKNOWN_FUNCTION"
    UNKNOWN_WHERE_EXPRESSION = "UNKNOWN_WHERE_EXPRESSION"
    UNKNOWN_AST_NODE = "UNKNOWN_AST_NODE"
    INVALID_PATH = "INVALID_PATH"
    WILDCARD_PATH = "WILDCARD_PATH"
    QUOTED_IDENTIFIER = "QUOTED_IDENTIFIER"
    UNSUPPORTED_LITERAL = "UNSUPPORTED_LITERAL"
    UNSUPPORTED_CLAUSE = "UNSUPPORTED_CLAUSE"
    ALIGN_BY_DEVICE = "ALIGN_BY_DEVICE"
    SQLGLOT_PARSE_ERROR = "SQLGLOT_PARSE_ERROR"
    PARSER_INTERNAL_ERROR = "PARSER_INTERNAL_ERROR"


class FeatureSource(str, Enum):
    """Where a feature value came from."""

    SQL_AST = "SQL_AST"
    SERVER_METADATA = "SERVER_METADATA"
    CATALOG = "CATALOG"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class MissingReason(str, Enum):
    """Why a feature value is ``None``."""

    NOT_PRESENT_IN_SQL = "NOT_PRESENT_IN_SQL"
    INCOMPLETE_TIME_RANGE = "INCOMPLETE_TIME_RANGE"
    INVALID_TIME_RANGE = "INVALID_TIME_RANGE"
    AMBIGUOUS_TIME_RANGE = "AMBIGUOUS_TIME_RANGE"
    UNSUPPORTED_SYNTAX = "UNSUPPORTED_SYNTAX"
    NEEDS_METADATA = "NEEDS_METADATA"
    NEEDS_STATISTICS = "NEEDS_STATISTICS"
    PARSER_LIMITATION = "PARSER_LIMITATION"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class QueryType(str, Enum):
    """Multi-label structural classification of a supported query."""

    PROJECTION = "PROJECTION"
    AGGREGATION = "AGGREGATION"
    TIME_FILTER = "TIME_FILTER"
    TIME_POINT = "TIME_POINT"
    TIME_RANGE = "TIME_RANGE"
    VALUE_FILTER = "VALUE_FILTER"
    MULTI_DEVICE = "MULTI_DEVICE"
    MULTI_SERIES = "MULTI_SERIES"
    GROUP_BY = "GROUP_BY"
    ORDER_BY = "ORDER_BY"
    DESCENDING_ORDER = "DESCENDING_ORDER"
    LIMIT = "LIMIT"
    COUNT_ALL = "COUNT_ALL"
    WILDCARD_PROJECTION = "WILDCARD_PROJECTION"


class ComparisonOperator(str, Enum):
    EQ = "="
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="


class Feature(BaseModel, Generic[T]):
    """A feature value with explicit provenance and missing reason.

    Invariants:

    - a present value has a real source and no missing reason;
    - a missing value always has a missing reason;
    - ``NOT_AVAILABLE`` is only used when no value exists.
    """

    model_config = ConfigDict(extra="forbid")

    value: T | None = None
    source: FeatureSource = FeatureSource.NOT_AVAILABLE
    missing_reason: MissingReason | None = None

    @model_validator(mode="after")
    def value_and_provenance_must_agree(self) -> "Feature[T]":
        if self.value is None:
            if self.missing_reason is None:
                raise ValueError("missing_reason is required when value is None")
        else:
            if self.missing_reason is not None:
                raise ValueError("missing_reason must be None when value is present")
            if self.source is FeatureSource.NOT_AVAILABLE:
                raise ValueError("source must be identified when value is present")
        return self


class TimePredicate(BaseModel):
    """A comparison against the IoTDB ``time`` column with an integer value."""

    model_config = ConfigDict(extra="forbid")

    operator: ComparisonOperator
    value_ms: int


class TimeRange(BaseModel):
    """Static bounds derived only from time predicates (never from statistics)."""

    model_config = ConfigDict(extra="forbid")

    start_ms: int | None = None
    end_ms: int | None = None
    start_inclusive: bool | None = None
    end_inclusive: bool | None = None

    @model_validator(mode="after")
    def at_least_one_bound(self) -> "TimeRange":
        if self.start_ms is None and self.end_ms is None:
            raise ValueError("a time range needs at least one bound")
        return self


class DeviceRef(BaseModel):
    """A tree-model device path written in ``FROM``.

    Its existence is not verified by the parser; see
    ``QueryFeatures.device_paths_verified``.
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    parts: list[str] = Field(min_length=3, max_length=4)


class SeriesSelector(BaseModel):
    """A series named in ``SELECT``.

    ``device``/``qualified_path`` are present only when the SQL used a
    five-part path.  A bare series name is evaluated against every device in
    ``FROM`` by IoTDB; it is not expanded by this parser.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    qualified_path: str | None = None
    device: str | None = None


class SeriesFilter(BaseModel):
    """A value filter against a fully qualified series path."""

    model_config = ConfigDict(extra="forbid")

    path: str
    device: str
    series: str
    operator: ComparisonOperator
    value: int


class AggregationSpec(BaseModel):
    """One supported aggregate projection."""

    model_config = ConfigDict(extra="forbid")

    function: str
    series: SeriesSelector | None = None
    is_star: bool = False

    @model_validator(mode="after")
    def star_only_for_count(self) -> "AggregationSpec":
        if self.is_star:
            if self.function != "COUNT":
                raise ValueError("only COUNT(*) may use a star argument")
            if self.series is not None:
                raise ValueError("COUNT(*) must not carry a series")
        elif self.series is None:
            raise ValueError("a non-star aggregation needs a series")
        return self


class GroupBySpec(BaseModel):
    """Reserved shape for a future GROUP BY parser.

    Phase 2 returns no supported GROUP BY query: every GROUP BY form in the
    Phase-0 workload is rejected by SQLGlot (interval form) or is not part of
    the verified subset.  The model exists so unsupported/missing features can
    be represented without inventing interval semantics.
    """

    model_config = ConfigDict(extra="forbid")

    form: str
    expressions: list[str] = Field(default_factory=list)
    interval_ms: int | None = None
    range_start_ms: int | None = None
    range_end_ms: int | None = None


class OrderBySpec(BaseModel):
    """The only supported ORDER BY shape: a single ``time`` column."""

    model_config = ConfigDict(extra="forbid")

    column: str = "time"
    direction: str = Field(pattern=r"^(ASC|DESC)$")


class ParseResult(BaseModel):
    """Raw parser output for exactly one statement.

    ``ast`` is parser-private, excluded from JSON serialization, and is only
    used by :meth:`SQLParserAdapter.extract_features`.  It is never persisted.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    query_id: str
    sql: str
    parse_status: ParseStatus
    query_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    template_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    normalized_template: str | None = None
    parser_name: str
    parser_version: str
    parser_dialect: str
    statement_count: int = Field(ge=0)
    failure_reason: ParseFailureReason | None = None
    unsupported_constructs: list[UnsupportedConstruct] = Field(default_factory=list)
    failure_detail: str | None = None
    warnings: list[str] = Field(default_factory=list)
    ast: Any | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def status_and_template_must_agree(self) -> "ParseResult":
        if self.parse_status is ParseStatus.SUCCESS:
            if self.failure_reason is not None:
                raise ValueError("a successful parse cannot have a failure reason")
            if not self.normalized_template:
                raise ValueError("a successful parse needs a normalized template")
            if self.template_hash is None:
                raise ValueError("a successful parse needs a template hash")
            if self.statement_count != 1:
                raise ValueError("a successful parse must contain one statement")
        else:
            if self.normalized_template is not None:
                raise ValueError("a failed parse must not expose a template")
            if self.template_hash is not None:
                raise ValueError("a failed parse must not expose a template hash")
            if self.failure_reason is None:
                raise ValueError("a failed parse needs a failure reason")
        return self


class QueryFeatures(BaseModel):
    """Agent 1 feature output for one SQL statement.

    Feature fields use :class:`Feature` so every value has a source and every
    missing value has a reason.  ``estimated_rows`` is always ``None`` in
    Phase 2 because no statistics are consulted.
    """

    model_config = ConfigDict(extra="forbid")

    query_id: str
    query_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    template_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    normalized_template: str | None = None
    query_types: list[QueryType] = Field(default_factory=list)

    time_predicates: Feature[list[TimePredicate]]
    time_range: Feature[TimeRange]
    time_range_ms: Feature[int]
    value_filters: Feature[list[SeriesFilter]]
    filter_count: Feature[int]
    devices: Feature[list[DeviceRef]]
    device_count: Feature[int]
    device_paths_verified: Feature[bool]
    selected_series: Feature[list[SeriesSelector]]
    series_count: Feature[int]
    aggregations: Feature[list[AggregationSpec]]
    aggregation_types: Feature[list[str]]
    group_by: Feature[GroupBySpec]
    has_group_by: Feature[bool]
    order_by: Feature[list[OrderBySpec]]
    has_order_by: Feature[bool]
    limit: Feature[int]
    has_limit: Feature[bool]
    estimated_rows: Feature[float]
    warnings: list[str] = Field(default_factory=list)

    @staticmethod
    def _feature_items(model: "QueryFeatures") -> list[tuple[str, Feature[Any]]]:
        items: list[tuple[str, Feature[Any]]] = []
        for name in type(model).model_fields:
            value = getattr(model, name)
            if isinstance(value, Feature):
                items.append((name, value))
        return items

    @computed_field  # type: ignore[prop-decorator]
    @property
    def feature_sources(self) -> dict[str, str]:
        return {
            name: feature.source.value
            for name, feature in self._feature_items(self)
        }

    @computed_field  # type: ignore[prop-decorator]
    @property
    def missing_features(self) -> list[str]:
        return [
            name
            for name, feature in self._feature_items(self)
            if feature.value is None
        ]


class QueryAnalysisResult(BaseModel):
    """Agent 1 output: parser status plus structured query features."""

    model_config = ConfigDict(extra="forbid")

    query_id: str
    parse: ParseResult
    features: QueryFeatures

    @model_validator(mode="after")
    def parse_and_features_must_describe_the_same_query(self) -> "QueryAnalysisResult":
        if self.query_id != self.parse.query_id:
            raise ValueError("query_id does not match the parse result")
        if self.query_id != self.features.query_id:
            raise ValueError("query_id does not match the features")
        if self.parse.query_hash != self.features.query_hash:
            raise ValueError("query_hash does not match the features")
        if self.parse.parse_status is ParseStatus.SUCCESS:
            if self.parse.template_hash != self.features.template_hash:
                raise ValueError("template_hash does not match the features")
            if self.parse.normalized_template != self.features.normalized_template:
                raise ValueError("normalized_template does not match the features")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def parse_status(self) -> ParseStatus:
        return self.parse.parse_status

    @computed_field  # type: ignore[prop-decorator]
    @property
    def query_hash(self) -> str:
        return self.parse.query_hash

    @computed_field  # type: ignore[prop-decorator]
    @property
    def normalized_template(self) -> str | None:
        return self.parse.normalized_template

    @computed_field  # type: ignore[prop-decorator]
    @property
    def query_types(self) -> list[QueryType]:
        return self.features.query_types

    @computed_field  # type: ignore[prop-decorator]
    @property
    def features_missing(self) -> list[str]:
        return self.features.missing_features

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unsupported_constructs(self) -> list[UnsupportedConstruct]:
        return self.parse.unsupported_constructs
