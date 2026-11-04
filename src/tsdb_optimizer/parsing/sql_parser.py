"""Fixture-backed SQL parser adapter for the Phase-2 supported subset.

SQLGlot 30.18.0 has **no IoTDB dialect**.  This adapter therefore uses SQLGlot
only as a general SQL tokenizer/parser and applies a strict AST whitelist on top
of it.  A successful SQLGlot parse is not sufficient: the statement is
supported only if every node, clause, path and literal matches the subset
exercised by the Phase-0 fixtures.  Everything else is returned as
``UNSUPPORTED`` and is never rewritten.

Notable IoTDB constructs that SQLGlot cannot represent safely:

- ``SELECT LAST s FROM ...`` (note: no parentheses) is silently parsed as an
  alias of a column named ``last``; this adapter detects and rejects it.
- ``GROUP BY ([start, end), interval)`` raises ``ParseError``.
- Wildcard paths such as ``root.db.g_0.*`` raise ``ParseError``.

No regular expression is used to decide safety or semantics.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import uuid4

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.tokens import Token, Tokenizer, TokenType

from ..errors import ParserInternalError
from ..schemas.query_features import (
    AggregationSpec,
    ComparisonOperator,
    DeviceRef,
    Feature,
    FeatureSource,
    GroupBySpec,
    MissingReason,
    OrderBySpec,
    ParseFailureReason,
    ParseResult,
    ParseStatus,
    QueryFeatures,
    QueryType,
    SeriesFilter,
    SeriesSelector,
    TimePredicate,
    TimeRange,
    UnsupportedConstruct,
)

_OPERATORS: dict[type[exp.Expression], ComparisonOperator] = {
    exp.EQ: ComparisonOperator.EQ,
    exp.GT: ComparisonOperator.GT,
    exp.GTE: ComparisonOperator.GTE,
    exp.LT: ComparisonOperator.LT,
    exp.LTE: ComparisonOperator.LTE,
}

_ALLOWED_NODE_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.From,
    exp.Table,
    exp.Join,
    exp.Where,
    exp.And,
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.Column,
    exp.Dot,
    exp.Identifier,
    exp.Literal,
    exp.Neg,
    exp.Order,
    exp.Ordered,
    exp.Limit,
    exp.Count,
    exp.LastValue,
    exp.Anonymous,
    exp.Star,
)

_ALLOWED_SELECT_ARGS = {
    "kind",
    "hint",
    "distinct",
    "expressions",
    "limit",
    "exclude",
    "operation_modifiers",
    "from_",
    "joins",
    "where",
    "group",
    "having",
    "qualify",
    "order",
    "offset",
    "with_",
    "sample",
    "windows",
}

_ALLOWED_SELECT_ITEM_FUNCTIONS = {"COUNT", "LAST_VALUE", "MAX_TIME"}

# Extra argument keys accepted on each node type.  Any other non-empty argument
# is outside the verified AST shape and makes the statement UNSUPPORTED.
_NODE_ALLOWED_ARGS: dict[type[exp.Expression], set[str]] = {
    exp.Select: _ALLOWED_SELECT_ARGS,
    exp.From: {"this", "expressions"},
    exp.Table: {"this", "catalog", "db", "alias"},
    exp.Where: {"this"},
    exp.And: {"this", "expression"},
    exp.EQ: {"this", "expression"},
    exp.NEQ: {"this", "expression"},
    exp.GT: {"this", "expression"},
    exp.GTE: {"this", "expression"},
    exp.LT: {"this", "expression"},
    exp.LTE: {"this", "expression"},
    exp.Column: {"this", "table", "db", "catalog"},
    exp.Dot: {"this", "expression"},
    exp.Identifier: {"this", "quoted"},
    exp.Literal: {"this", "is_string"},
    exp.Neg: {"this"},
    exp.Order: {"expressions"},
    exp.Ordered: {"this", "desc", "nulls_first", "with_fill"},
    exp.Limit: {"expression"},
    exp.Count: {"this", "big_int", "distinct"},
    exp.LastValue: {"this"},
    exp.Anonymous: {"this", "expressions"},
    exp.Star: set(),
}


class _UnsupportedQuery(Exception):
    def __init__(
        self,
        reason: ParseFailureReason,
        constructs: list[UnsupportedConstruct],
        detail: str,
    ):
        super().__init__(detail)
        self.reason = reason
        self.constructs = constructs
        self.detail = detail


@dataclass(frozen=True)
class _Analysis:
    devices: list[DeviceRef]
    selected_series: list[SeriesSelector] | None
    select_templates: list[str]
    aggregations: list[AggregationSpec]
    time_predicates: list[TimePredicate]
    predicate_templates: list[str]
    time_range: TimeRange | None
    time_range_missing: MissingReason | None
    time_range_ms: int | None
    time_range_ms_missing: MissingReason | None
    value_filters: list[SeriesFilter]
    filter_count: int
    has_group_by: bool
    order_by: list[OrderBySpec]
    limit: int | None
    query_types: list[QueryType]
    warnings: list[str]
    normalized_template: str


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unsupported(
    construct: UnsupportedConstruct,
    detail: str,
    *,
    reason: ParseFailureReason = ParseFailureReason.UNSUPPORTED_SYNTAX,
) -> None:
    raise _UnsupportedQuery(reason, [construct], detail)


def _construct_hints(tokens: list[Token]) -> list[UnsupportedConstruct]:
    """Diagnostic hints for a statement SQLGlot could not parse.

    These hints never turn a failed parse into a supported one.  They only make
    the failure report more useful (for example, the eight IoTDB interval
    GROUP BY workload cases).
    """

    hints: list[UnsupportedConstruct] = []
    if any(token.token_type is TokenType.GROUP_BY for token in tokens):
        hints.append(UnsupportedConstruct.GROUP_BY)
    for index in range(len(tokens) - 1):
        if (
            tokens[index].token_type is TokenType.DOT
            and tokens[index + 1].token_type is TokenType.STAR
        ):
            hints.append(UnsupportedConstruct.WILDCARD_PATH)
            break
    for index in range(len(tokens) - 2):
        words = [tokens[index + offset].text.upper() for offset in range(3)]
        if words == ["ALIGN", "BY", "DEVICE"]:
            hints.append(UnsupportedConstruct.ALIGN_BY_DEVICE)
            break
    return hints


def _has_nulls_ordering(tokens: list[Token]) -> bool:
    in_order_by = False
    for index, token in enumerate(tokens):
        if token.token_type is TokenType.ORDER_BY:
            in_order_by = True
            continue
        if (
            in_order_by
            and token.text.upper() == "NULLS"
            and index + 1 < len(tokens)
            and tokens[index + 1].text.upper() in {"FIRST", "LAST"}
        ):
            return True
    return False


def _identifier(node: exp.Expression) -> str:
    if not isinstance(node, exp.Identifier):
        _unsupported(
            UnsupportedConstruct.INVALID_PATH,
            f"expected an identifier, got {type(node).__name__}",
        )
    if node.args.get("quoted"):
        _unsupported(
            UnsupportedConstruct.QUOTED_IDENTIFIER,
            f"quoted identifier is outside the verified subset: {node.this!r}",
        )
    name = node.this
    if not isinstance(name, str) or not name:
        _unsupported(UnsupportedConstruct.INVALID_PATH, "empty identifier")
    if "\x00" in name:
        _unsupported(UnsupportedConstruct.INVALID_PATH, "NUL in identifier")
    return name


def _column_parts(node: exp.Expression) -> list[str]:
    if isinstance(node, exp.Identifier):
        return [_identifier(node)]
    if isinstance(node, exp.Column):
        parts: list[str] = []
        for key in ("catalog", "db", "table"):
            value = node.args.get(key)
            if value is not None:
                parts.append(_identifier(value))
        parts.append(_identifier(node.this))
        return parts
    if isinstance(node, exp.Dot):
        return _column_parts(node.this) + [_identifier(node.expression)]
    _unsupported(
        UnsupportedConstruct.INVALID_PATH,
        f"expected a column or dotted path, got {type(node).__name__}",
    )


def _table_parts(table: exp.Table) -> list[str]:
    parts: list[str] = []
    catalog = table.args.get("catalog")
    database = table.args.get("db")
    if catalog is not None:
        parts.append(_identifier(catalog))
    if database is not None:
        parts.append(_identifier(database))

    base = table.this
    if isinstance(base, exp.Identifier):
        parts.append(_identifier(base))
    elif isinstance(base, exp.Dot):
        parts.extend(_column_parts(base))
    else:
        _unsupported(
            UnsupportedConstruct.INVALID_PATH,
            f"unsupported FROM path node: {type(base).__name__}",
        )
    return parts


def _int_literal(node: exp.Expression, *, allow_negative: bool = True) -> int:
    sign = 1
    if isinstance(node, exp.Neg):
        if not allow_negative:
            _unsupported(
                UnsupportedConstruct.UNSUPPORTED_LITERAL,
                "negative value is not accepted here",
            )
        sign = -1
        node = node.this

    if not isinstance(node, exp.Literal):
        _unsupported(
            UnsupportedConstruct.UNSUPPORTED_LITERAL,
            f"expected a numeric literal, got {type(node).__name__}",
        )
    if node.args.get("is_string"):
        _unsupported(
            UnsupportedConstruct.UNSUPPORTED_LITERAL,
            "string literal is not accepted in this position",
        )
    text = node.this
    if not isinstance(text, str) or not text or not all(char.isdigit() for char in text):
        _unsupported(
            UnsupportedConstruct.UNSUPPORTED_LITERAL,
            f"only plain integer literals are supported: {text!r}",
        )
    return sign * int(text)


def _is_plain_time_column(node: exp.Expression) -> bool:
    if not isinstance(node, exp.Column):
        return False
    if any(node.args.get(key) is not None for key in ("catalog", "db", "table")):
        return False
    return _identifier(node.this).lower() == "time"


def _series_selector(node: exp.Expression) -> SeriesSelector:
    parts = _column_parts(node)
    if len(parts) == 1:
        name = parts[0]
        if name.lower() == "time":
            _unsupported(
                UnsupportedConstruct.UNKNOWN_SELECT_ITEM,
                "selecting the time column as a series is not in the verified subset",
            )
        return SeriesSelector(name=name)
    if len(parts) == 5 and parts[0] == "root":
        return SeriesSelector(
            name=parts[4],
            qualified_path=".".join(parts),
            device=".".join(parts[:4]),
        )
    _unsupported(
        UnsupportedConstruct.INVALID_PATH,
        "qualified series must be a five-part root.<db>.<group>.<device>.<series> path",
    )


def _series_filter(
    node: exp.Expression,
    operator: ComparisonOperator,
    value: int,
) -> SeriesFilter:
    parts = _column_parts(node)
    if len(parts) == 1:
        _unsupported(
            UnsupportedConstruct.UNKNOWN_WHERE_EXPRESSION,
            "value filters must use a fully qualified series path",
        )
    if len(parts) != 5 or parts[0] != "root":
        _unsupported(
            UnsupportedConstruct.INVALID_PATH,
            "value filter path must have five parts and start with root",
        )
    return SeriesFilter(
        path=".".join(parts),
        device=".".join(parts[:4]),
        series=parts[4],
        operator=operator,
        value=value,
    )


def _aggregation(
    node: exp.Expression,
    *,
    function: str,
    argument: exp.Expression,
    is_star: bool,
) -> tuple[AggregationSpec, str, str | None]:
    if is_star:
        return (
            AggregationSpec(function=function, is_star=True),
            f"{function}(*)",
            f"{function}(*) expands per series in IoTDB; it is not a single-row count."
            if function == "COUNT"
            else None,
        )

    if not isinstance(argument, (exp.Column, exp.Dot)):
        _unsupported(
            UnsupportedConstruct.UNKNOWN_FUNCTION,
            f"{function} argument must be a series column",
        )
    selector = _series_selector(argument)
    template_argument = selector.qualified_path or selector.name
    return (
        AggregationSpec(function=function, series=selector),
        f"{function}({template_argument})",
        None,
    )


def _check_select_args(select: exp.Select) -> None:
    for key, value in select.args.items():
        if key in _ALLOWED_SELECT_ARGS:
            continue
        if value not in (None, [], {}):
            _unsupported(
                UnsupportedConstruct.UNSUPPORTED_CLAUSE,
                f"unexpected SELECT clause: {key}",
            )

    if select.args.get("group") is not None:
        _unsupported(
            UnsupportedConstruct.GROUP_BY,
            "GROUP BY is not part of the verified Phase-2 subset",
        )
    if select.args.get("having") is not None:
        _unsupported(UnsupportedConstruct.HAVING, "HAVING is not supported")
    if select.args.get("offset") is not None:
        _unsupported(UnsupportedConstruct.OFFSET, "OFFSET is not supported")
    if select.args.get("distinct") is not None:
        _unsupported(UnsupportedConstruct.DISTINCT, "SELECT DISTINCT is not supported")
    if select.args.get("hint") is not None:
        _unsupported(UnsupportedConstruct.UNSUPPORTED_CLAUSE, "optimizer hints are not supported")
    if select.args.get("exclude") is not None:
        _unsupported(UnsupportedConstruct.UNSUPPORTED_CLAUSE, "EXCLUDE is not supported")
    if select.args.get("operation_modifiers"):
        _unsupported(
            UnsupportedConstruct.UNSUPPORTED_CLAUSE,
            "SELECT operation modifiers are not supported",
        )
    if select.args.get("with_") is not None:
        _unsupported(
            UnsupportedConstruct.UNSUPPORTED_CLAUSE,
            "WITH / CTE is not supported",
        )
    if select.args.get("qualify") is not None:
        _unsupported(UnsupportedConstruct.UNSUPPORTED_CLAUSE, "QUALIFY is not supported")
    if select.args.get("sample") is not None:
        _unsupported(UnsupportedConstruct.UNSUPPORTED_CLAUSE, "TABLESAMPLE is not supported")
    if select.args.get("windows") not in (None, []):
        _unsupported(
            UnsupportedConstruct.UNSUPPORTED_CLAUSE,
            "named WINDOW definitions are not supported",
        )
    if select.args.get("kind") is not None:
        _unsupported(UnsupportedConstruct.UNSUPPORTED_CLAUSE, "SELECT kind is not supported")


def _check_node_args(node: exp.Expression) -> None:
    allowed = _NODE_ALLOWED_ARGS.get(type(node))
    if allowed is None:
        return
    for key, value in node.args.items():
        if key in allowed:
            continue
        if value not in (None, [], {}):
            _unsupported(
                UnsupportedConstruct.UNSUPPORTED_CLAUSE,
                f"unexpected {type(node).__name__} argument: {key}",
            )


def _walk_known_nodes(statement: exp.Select) -> None:
    for node in statement.walk():
        if node is statement:
            continue

        _check_node_args(node)

        if isinstance(node, exp.Alias):
            inner = node.this
            if (
                isinstance(inner, exp.Column)
                and _identifier(inner.this).lower() == "last"
            ):
                _unsupported(
                    UnsupportedConstruct.LAST_SYNTAX,
                    "IoTDB LAST syntax is silently parsed as an alias by SQLGlot",
                )
            _unsupported(
                UnsupportedConstruct.SELECT_ALIAS,
                "SELECT aliases are outside the verified subset",
            )
        if isinstance(node, exp.TableAlias):
            _unsupported(
                UnsupportedConstruct.TABLE_ALIAS,
                "table aliases are outside the verified subset",
            )
        if isinstance(node, exp.Subquery):
            _unsupported(
                UnsupportedConstruct.SUBQUERY,
                "subqueries are outside the verified subset",
            )
        if isinstance(node, (exp.Or, exp.Not, exp.Between, exp.In, exp.Like, exp.Is)):
            _unsupported(
                UnsupportedConstruct.UNKNOWN_WHERE_EXPRESSION,
                f"unsupported WHERE node: {type(node).__name__}",
            )
        if isinstance(node, exp.Func) and not isinstance(node, exp.And):
            if not isinstance(node, (exp.Count, exp.LastValue, exp.Anonymous)):
                _unsupported(
                    UnsupportedConstruct.UNKNOWN_FUNCTION,
                    f"function is outside the verified subset: {type(node).__name__}",
                )
            if isinstance(node, exp.Anonymous):
                function = str(node.this).upper()
                if function not in _ALLOWED_SELECT_ITEM_FUNCTIONS:
                    _unsupported(
                        UnsupportedConstruct.UNKNOWN_FUNCTION,
                        f"function is outside the verified subset: {function}",
                    )
        if not isinstance(node, _ALLOWED_NODE_TYPES):
            _unsupported(
                UnsupportedConstruct.UNKNOWN_AST_NODE,
                f"unexpected AST node: {type(node).__name__}",
                reason=ParseFailureReason.UNKNOWN_AST_NODE,
            )


def _parse_from(select: exp.Select) -> tuple[list[DeviceRef], list[str]]:
    from_node = select.args.get("from_")
    if from_node is None:
        _unsupported(
            UnsupportedConstruct.MISSING_FROM,
            "tree-model SELECT requires a FROM device path",
        )
    if not isinstance(from_node, exp.From):
        _unsupported(
            UnsupportedConstruct.UNKNOWN_AST_NODE,
            f"unsupported FROM node: {type(from_node).__name__}",
        )
    if from_node.args.get("expressions") not in (None, []):
        _unsupported(UnsupportedConstruct.UNSUPPORTED_CLAUSE, "multiple FROM expressions")

    tables: list[exp.Table] = []
    first = from_node.this
    if isinstance(first, exp.Subquery):
        _unsupported(UnsupportedConstruct.SUBQUERY, "subquery in FROM")
    if not isinstance(first, exp.Table):
        _unsupported(
            UnsupportedConstruct.UNKNOWN_AST_NODE,
            f"unsupported FROM source: {type(first).__name__}",
        )
    tables.append(first)

    for join in select.args.get("joins") or []:
        if not isinstance(join, exp.Join):
            _unsupported(
                UnsupportedConstruct.UNKNOWN_AST_NODE,
                f"unsupported join node: {type(join).__name__}",
            )
        extra = [
            key
            for key, value in join.args.items()
            if key != "this" and value not in (None, [], {})
        ]
        if extra:
            _unsupported(
                UnsupportedConstruct.EXPLICIT_JOIN,
                f"explicit JOIN clauses are not supported: {sorted(extra)}",
            )
        if not isinstance(join.this, exp.Table):
            _unsupported(
                UnsupportedConstruct.UNKNOWN_AST_NODE,
                f"unsupported JOIN source: {type(join.this).__name__}",
            )
        tables.append(join.this)

    devices: list[DeviceRef] = []
    warnings: list[str] = []
    for table in tables:
        for key in ("alias", "pivots", "hints", "format", "version"):
            if table.args.get(key) not in (None, [], {}):
                if key == "alias":
                    _unsupported(
                        UnsupportedConstruct.TABLE_ALIAS,
                        "table aliases are outside the verified subset",
                    )
                _unsupported(
                    UnsupportedConstruct.UNSUPPORTED_CLAUSE,
                    f"unsupported table clause: {key}",
                )
        parts = _table_parts(table)
        if parts[0] != "root":
            _unsupported(
                UnsupportedConstruct.INVALID_PATH,
                "tree paths must start with root",
            )
        if len(parts) not in (3, 4):
            _unsupported(
                UnsupportedConstruct.INVALID_PATH,
                "FROM must contain device paths with three or four parts",
            )
        devices.append(DeviceRef(path=".".join(parts), parts=parts))

    warnings.append(
        "Device paths are parsed from SQL text only; existence is not verified "
        "against catalog metadata."
    )
    return devices, warnings


def _parse_select_items(
    select: exp.Select,
) -> tuple[
    list[SeriesSelector] | None,
    bool,
    list[str],
    list[AggregationSpec],
    list[str],
]:
    items = select.expressions
    if not items:
        _unsupported(UnsupportedConstruct.UNKNOWN_SELECT_ITEM, "empty SELECT list")

    if any(isinstance(item, exp.Star) for item in items):
        if len(items) != 1:
            _unsupported(
                UnsupportedConstruct.UNKNOWN_SELECT_ITEM,
                "SELECT * cannot be mixed with other projections",
            )
        return None, True, ["*"], [], [
            "SELECT * expands to one column per series; result width may be large."
        ]

    selected_series: list[SeriesSelector] = []
    select_templates: list[str] = []
    aggregations: list[AggregationSpec] = []
    warnings: list[str] = []

    for item in items:
        if isinstance(item, exp.Alias):
            inner = item.this
            if (
                isinstance(inner, exp.Column)
                and _identifier(inner.this).lower() == "last"
            ):
                _unsupported(
                    UnsupportedConstruct.LAST_SYNTAX,
                    "IoTDB LAST syntax is silently parsed as an alias by SQLGlot",
                )
            _unsupported(
                UnsupportedConstruct.SELECT_ALIAS,
                "SELECT aliases are outside the verified subset",
            )

        if isinstance(item, (exp.Column, exp.Dot)):
            selector = _series_selector(item)
            selected_series.append(selector)
            select_templates.append(selector.qualified_path or selector.name)
            continue

        if isinstance(item, exp.Count):
            if item.args.get("distinct") is not None:
                _unsupported(
                    UnsupportedConstruct.UNKNOWN_FUNCTION,
                    "COUNT(DISTINCT ...) is outside the verified subset",
                )
            argument = item.this
            if isinstance(argument, exp.Star):
                spec, template, warning = _aggregation(
                    item, function="COUNT", argument=argument, is_star=True
                )
            else:
                spec, template, warning = _aggregation(
                    item, function="COUNT", argument=argument, is_star=False
                )
                selected_series.append(spec.series)
            aggregations.append(spec)
            select_templates.append(template)
            if warning:
                warnings.append(warning)
            continue

        if isinstance(item, exp.LastValue):
            spec, template, warning = _aggregation(
                item, function="LAST_VALUE", argument=item.this, is_star=False
            )
            selected_series.append(spec.series)
            aggregations.append(spec)
            select_templates.append(template)
            if warning:
                warnings.append(warning)
            continue

        if isinstance(item, exp.Anonymous):
            function = str(item.this).upper()
            if function != "MAX_TIME":
                _unsupported(
                    UnsupportedConstruct.UNKNOWN_FUNCTION,
                    f"function is outside the verified subset: {function}",
                )
            arguments = item.expressions
            if len(arguments) != 1:
                _unsupported(
                    UnsupportedConstruct.UNKNOWN_FUNCTION,
                    "MAX_TIME requires exactly one series argument",
                )
            spec, template, warning = _aggregation(
                item, function=function, argument=arguments[0], is_star=False
            )
            selected_series.append(spec.series)
            aggregations.append(spec)
            select_templates.append(template)
            if warning:
                warnings.append(warning)
            continue

        if isinstance(item, exp.Func):
            _unsupported(
                UnsupportedConstruct.UNKNOWN_FUNCTION,
                f"function is outside the verified subset: {type(item).__name__}",
            )
        _unsupported(
            UnsupportedConstruct.UNKNOWN_SELECT_ITEM,
            f"unsupported SELECT expression: {type(item).__name__}",
        )

    return selected_series or None, False, select_templates, aggregations, warnings


def _parse_predicate(
    node: exp.Expression,
) -> tuple[str, TimePredicate | SeriesFilter, str]:
    operator = _OPERATORS.get(type(node))
    if operator is None:
        _unsupported(
            UnsupportedConstruct.UNKNOWN_WHERE_EXPRESSION,
            f"unsupported comparison: {type(node).__name__}",
        )
    left = node.this
    right = node.expression

    if _is_plain_time_column(left):
        value = _int_literal(right)
        predicate = TimePredicate(operator=operator, value_ms=value)
        return "time", predicate, f"time {operator.value} ?"

    if not isinstance(left, (exp.Column, exp.Dot)):
        _unsupported(
            UnsupportedConstruct.UNKNOWN_WHERE_EXPRESSION,
            f"unsupported filter left-hand side: {type(left).__name__}",
        )
    value = _int_literal(right)
    predicate = _series_filter(left, operator, value)
    return "series", predicate, f"{predicate.path} {operator.value} ?"


def _parse_where(
    select: exp.Select,
) -> tuple[list[TimePredicate], list[SeriesFilter], list[str]]:
    where = select.args.get("where")
    if where is None:
        return [], [], []
    if not isinstance(where, exp.Where) or where.this is None:
        _unsupported(
            UnsupportedConstruct.UNKNOWN_WHERE_EXPRESSION,
            "unsupported WHERE clause",
        )

    time_predicates: list[TimePredicate] = []
    value_filters: list[SeriesFilter] = []
    templates: list[str] = []

    def visit(node: exp.Expression) -> None:
        if isinstance(node, exp.And):
            visit(node.this)
            visit(node.expression)
            return
        kind, predicate, template = _parse_predicate(node)
        templates.append(template)
        if kind == "time":
            time_predicates.append(predicate)  # type: ignore[arg-type]
        else:
            value_filters.append(predicate)  # type: ignore[arg-type]

    visit(where.this)
    return time_predicates, value_filters, templates


def _derive_time_range(
    predicates: list[TimePredicate],
) -> tuple[TimeRange | None, MissingReason | None, int | None, MissingReason | None]:
    if not predicates:
        return None, MissingReason.NOT_PRESENT_IN_SQL, None, MissingReason.NOT_PRESENT_IN_SQL

    equalities = [p for p in predicates if p.operator is ComparisonOperator.EQ]
    if equalities:
        if len(predicates) != 1:
            return (
                None,
                MissingReason.AMBIGUOUS_TIME_RANGE,
                None,
                MissingReason.AMBIGUOUS_TIME_RANGE,
            )
        point = equalities[0].value_ms
        return (
            TimeRange(
                start_ms=point,
                end_ms=point,
                start_inclusive=True,
                end_inclusive=True,
            ),
            None,
            0,
            None,
        )

    lowers = [p for p in predicates if p.operator in (ComparisonOperator.GT, ComparisonOperator.GTE)]
    uppers = [p for p in predicates if p.operator in (ComparisonOperator.LT, ComparisonOperator.LTE)]
    if len(lowers) > 1 or len(uppers) > 1:
        return (
            None,
            MissingReason.AMBIGUOUS_TIME_RANGE,
            None,
            MissingReason.AMBIGUOUS_TIME_RANGE,
        )

    lower = lowers[0] if lowers else None
    upper = uppers[0] if uppers else None
    if lower is None and upper is None:
        return (
            None,
            MissingReason.AMBIGUOUS_TIME_RANGE,
            None,
            MissingReason.AMBIGUOUS_TIME_RANGE,
        )

    time_range = TimeRange(
        start_ms=lower.value_ms if lower else None,
        end_ms=upper.value_ms if upper else None,
        start_inclusive=(
            lower.operator is ComparisonOperator.GTE if lower else None
        ),
        end_inclusive=(
            upper.operator is ComparisonOperator.LTE if upper else None
        ),
    )

    if lower is None or upper is None:
        return time_range, None, None, MissingReason.INCOMPLETE_TIME_RANGE
    if upper.value_ms < lower.value_ms:
        return time_range, None, None, MissingReason.INVALID_TIME_RANGE
    if (
        upper.value_ms == lower.value_ms
        and (
            lower.operator is ComparisonOperator.GT
            or upper.operator is ComparisonOperator.LT
        )
    ):
        return time_range, None, None, MissingReason.INVALID_TIME_RANGE
    return time_range, None, upper.value_ms - lower.value_ms, None


def _parse_order_by(select: exp.Select) -> list[OrderBySpec]:
    order = select.args.get("order")
    if order is None:
        return []
    if not isinstance(order, exp.Order):
        _unsupported(
            UnsupportedConstruct.UNKNOWN_AST_NODE,
            f"unsupported ORDER node: {type(order).__name__}",
        )
    expressions = order.expressions
    if len(expressions) != 1 or not isinstance(expressions[0], exp.Ordered):
        _unsupported(
            UnsupportedConstruct.ORDER_BY,
            "only one ORDER BY expression is supported",
        )
    ordered = expressions[0]
    if ordered.args.get("with_fill") is not None:
        _unsupported(UnsupportedConstruct.ORDER_BY, "WITH FILL is not supported")
    expression = ordered.this
    if not isinstance(expression, exp.Column) or not _is_plain_time_column(expression):
        _unsupported(
            UnsupportedConstruct.ORDER_BY,
            "only ORDER BY time [ASC|DESC] is supported",
        )
    desc = ordered.args.get("desc")
    return [OrderBySpec(direction="DESC" if desc is True else "ASC")]


def _parse_limit(select: exp.Select) -> int | None:
    limit = select.args.get("limit")
    if limit is None:
        return None
    if not isinstance(limit, exp.Limit):
        _unsupported(
            UnsupportedConstruct.UNKNOWN_AST_NODE,
            f"unsupported LIMIT node: {type(limit).__name__}",
        )
    for key in ("this", "limit_options", "expressions"):
        if limit.args.get(key) not in (None, []):
            _unsupported(
                UnsupportedConstruct.LIMIT,
                f"unsupported LIMIT option: {key}",
            )
    expression = limit.args.get("expression")
    if expression is None:
        _unsupported(UnsupportedConstruct.LIMIT, "LIMIT without an integer")
    value = _int_literal(expression, allow_negative=False)
    return value


def _query_types(
    *,
    devices: list[DeviceRef],
    selected_series: list[SeriesSelector] | None,
    has_wildcard: bool,
    aggregations: list[AggregationSpec],
    time_predicates: list[TimePredicate],
    time_range: TimeRange | None,
    time_range_ms: int | None,
    value_filters: list[SeriesFilter],
    order_by: list[OrderBySpec],
    limit: int | None,
) -> list[QueryType]:
    types: list[QueryType] = [QueryType.PROJECTION]
    if aggregations:
        types.append(QueryType.AGGREGATION)
    if time_predicates:
        types.append(QueryType.TIME_FILTER)
        if (
            len(time_predicates) == 1
            and time_predicates[0].operator is ComparisonOperator.EQ
        ):
            types.append(QueryType.TIME_POINT)
        elif (
            time_range_ms is not None
            and time_range is not None
            and time_range.start_ms is not None
            and time_range.end_ms is not None
        ):
            types.append(QueryType.TIME_RANGE)
    if value_filters:
        types.append(QueryType.VALUE_FILTER)
    if len(devices) > 1:
        types.append(QueryType.MULTI_DEVICE)
    if selected_series is not None and len(selected_series) > 1:
        types.append(QueryType.MULTI_SERIES)
    if order_by:
        types.append(QueryType.ORDER_BY)
        if any(item.direction == "DESC" for item in order_by):
            types.append(QueryType.DESCENDING_ORDER)
    if limit is not None:
        types.append(QueryType.LIMIT)
    if any(item.is_star for item in aggregations):
        types.append(QueryType.COUNT_ALL)
    if has_wildcard:
        types.append(QueryType.WILDCARD_PROJECTION)
    return types


def _render_template(
    *,
    devices: list[DeviceRef],
    select_templates: list[str],
    predicate_templates: list[str],
    order_by: list[OrderBySpec],
    limit: int | None,
) -> str:
    pieces = [
        "SELECT " + ", ".join(select_templates),
        "FROM " + ", ".join(device.path for device in devices),
    ]
    if predicate_templates:
        pieces.append("WHERE " + " AND ".join(predicate_templates))
    if order_by:
        pieces.append(
            "ORDER BY "
            + ", ".join(f"{item.column} {item.direction}" for item in order_by)
        )
    if limit is not None:
        pieces.append("LIMIT ?")
    return " ".join(pieces)


def _analyze(statement: exp.Select) -> _Analysis:
    _check_select_args(statement)
    _walk_known_nodes(statement)

    devices, warnings = _parse_from(statement)
    (
        selected_series,
        has_wildcard,
        select_templates,
        aggregations,
        item_warnings,
    ) = _parse_select_items(statement)
    warnings.extend(item_warnings)
    time_predicates, value_filters, predicate_templates = _parse_where(statement)
    (
        time_range,
        time_range_missing,
        time_range_ms,
        time_range_ms_missing,
    ) = _derive_time_range(time_predicates)
    order_by = _parse_order_by(statement)
    limit = _parse_limit(statement)

    query_types = _query_types(
        devices=devices,
        selected_series=selected_series,
        has_wildcard=has_wildcard,
        aggregations=aggregations,
        time_predicates=time_predicates,
        time_range=time_range,
        time_range_ms=time_range_ms,
        value_filters=value_filters,
        order_by=order_by,
        limit=limit,
    )
    template = _render_template(
        devices=devices,
        select_templates=select_templates,
        predicate_templates=predicate_templates,
        order_by=order_by,
        limit=limit,
    )
    return _Analysis(
        devices=devices,
        selected_series=selected_series,
        select_templates=select_templates,
        aggregations=aggregations,
        time_predicates=time_predicates,
        predicate_templates=predicate_templates,
        time_range=time_range,
        time_range_missing=time_range_missing,
        time_range_ms=time_range_ms,
        time_range_ms_missing=time_range_ms_missing,
        value_filters=value_filters,
        filter_count=len(time_predicates) + len(value_filters),
        has_group_by=False,
        order_by=order_by,
        limit=limit,
        query_types=query_types,
        warnings=warnings,
        normalized_template=template,
    )


def _present(value: Any) -> Feature[Any]:
    return Feature(value=value, source=FeatureSource.SQL_AST)


def _absent(reason: MissingReason) -> Feature[Any]:
    return Feature(
        value=None,
        source=FeatureSource.NOT_AVAILABLE,
        missing_reason=reason,
    )


def _ast_absent(reason: MissingReason) -> Feature[Any]:
    return Feature(
        value=None,
        source=FeatureSource.SQL_AST,
        missing_reason=reason,
    )


def _features_from_analysis(
    *,
    query_id: str,
    query_hash: str,
    template_hash: str,
    analysis: _Analysis,
) -> QueryFeatures:
    selected_series = analysis.selected_series
    if selected_series is None:
        selected_feature = _absent(MissingReason.NEEDS_METADATA)
        series_count_feature = _absent(MissingReason.NEEDS_METADATA)
    else:
        selected_feature = _present(selected_series)
        series_count_feature = _present(len(selected_series))

    aggregation_types = list(
        dict.fromkeys(item.function for item in analysis.aggregations)
    )

    return QueryFeatures(
        query_id=query_id,
        query_hash=query_hash,
        template_hash=template_hash,
        normalized_template=analysis.normalized_template,
        query_types=analysis.query_types,
        time_predicates=(
            _present(analysis.time_predicates)
            if analysis.time_predicates
            else _ast_absent(MissingReason.NOT_PRESENT_IN_SQL)
        ),
        time_range=(
            _present(analysis.time_range)
            if analysis.time_range is not None
            else _ast_absent(
                analysis.time_range_missing or MissingReason.NOT_PRESENT_IN_SQL
            )
        ),
        time_range_ms=(
            _present(analysis.time_range_ms)
            if analysis.time_range_ms is not None
            else _ast_absent(
                analysis.time_range_ms_missing or MissingReason.NOT_PRESENT_IN_SQL
            )
        ),
        value_filters=(
            _present(analysis.value_filters)
            if analysis.value_filters
            else _ast_absent(MissingReason.NOT_PRESENT_IN_SQL)
        ),
        filter_count=_present(analysis.filter_count),
        devices=_present(analysis.devices),
        device_count=_present(len(analysis.devices)),
        device_paths_verified=_absent(MissingReason.NEEDS_METADATA),
        selected_series=selected_feature,
        series_count=series_count_feature,
        aggregations=(
            _present(analysis.aggregations)
            if analysis.aggregations
            else _ast_absent(MissingReason.NOT_PRESENT_IN_SQL)
        ),
        aggregation_types=(
            _present(aggregation_types)
            if aggregation_types
            else _ast_absent(MissingReason.NOT_PRESENT_IN_SQL)
        ),
        group_by=_ast_absent(MissingReason.NOT_PRESENT_IN_SQL),
        has_group_by=_present(analysis.has_group_by),
        order_by=(
            _present(analysis.order_by)
            if analysis.order_by
            else _ast_absent(MissingReason.NOT_PRESENT_IN_SQL)
        ),
        has_order_by=_present(bool(analysis.order_by)),
        limit=(
            _present(analysis.limit)
            if analysis.limit is not None
            else _ast_absent(MissingReason.NOT_PRESENT_IN_SQL)
        ),
        has_limit=_present(analysis.limit is not None),
        estimated_rows=_absent(MissingReason.NEEDS_STATISTICS),
        warnings=analysis.warnings,
    )


def _all_missing_features(
    *,
    query_id: str,
    query_hash: str,
    reason: MissingReason,
    warnings: list[str],
) -> QueryFeatures:
    missing = _absent(reason)
    return QueryFeatures(
        query_id=query_id,
        query_hash=query_hash,
        template_hash=None,
        normalized_template=None,
        query_types=[],
        time_predicates=missing,
        time_range=missing,
        time_range_ms=missing,
        value_filters=missing,
        filter_count=missing,
        devices=missing,
        device_count=missing,
        device_paths_verified=_absent(MissingReason.NEEDS_METADATA),
        selected_series=missing,
        series_count=missing,
        aggregations=missing,
        aggregation_types=missing,
        group_by=missing,
        has_group_by=missing,
        order_by=missing,
        has_order_by=missing,
        limit=missing,
        has_limit=missing,
        estimated_rows=_absent(MissingReason.NEEDS_STATISTICS),
        warnings=warnings,
    )


class SQLParserAdapter:
    """Fixture-scoped SQL parser and feature extractor.

    ``parse`` returns ``UNSUPPORTED`` for SQL outside the tested subset.  It
    never rewrites SQL and never claims that a SQLGlot parse implies IoTDB
    acceptance.
    """

    parser_name = "sqlglot"
    parser_dialect = "sqlglot-default (SQLGlot has no IoTDB dialect)"

    def __init__(self, parser_version: str | None = None):
        self.parser_version = parser_version or sqlglot.__version__

    def _memo_key(self, sql):
        return (sql, self.parser_name, self.parser_version, self.parser_dialect,
                sqlglot.parse, Tokenizer, _analyze, _features_from_analysis)

    def parse(self, sql: str, query_id: str | None = None) -> ParseResult:
        from ..request_memo import bucket, remember
        cache = bucket('parse', sql) if type(self) is SQLParserAdapter else None
        key = self._memo_key(sql) if cache is not None else None
        if cache is not None and key in cache:
            # AST is mutable: never expose the retained private parse tree.
            return cache[key].model_copy(deep=True, update={'query_id': query_id or uuid4().hex})
        result = self._parse_uncached(sql, query_id)
        if cache is not None and result.parse_status is ParseStatus.SUCCESS:
            remember(cache, key, result.model_copy(deep=True))
        return result

    def _parse_uncached(self, sql: str, query_id: str | None = None) -> ParseResult:
        if not isinstance(sql, str):
            raise TypeError("sql must be a string")
        qid = query_id or uuid4().hex
        query_hash = _sha256(sql)
        hints: list[UnsupportedConstruct] = []
        tokens: list[Token] = []
        try:
            tokens = Tokenizer().tokenize(sql)
            hints = _construct_hints(tokens)
        except Exception:  # noqa: BLE001 - hints are optional; status stays fail-closed
            hints = []

        if not sql.strip():
            return self._failure(
                query_id=qid,
                sql=sql,
                query_hash=query_hash,
                status=ParseStatus.UNSUPPORTED,
                reason=ParseFailureReason.EMPTY_INPUT,
                constructs=[UnsupportedConstruct.EMPTY_INPUT],
                detail="SQL input is empty",
                statement_count=0,
            )

        try:
            statements = sqlglot.parse(sql)
        except ParseError as exc:
            constructs = [UnsupportedConstruct.SQLGLOT_PARSE_ERROR]
            for hint in hints:
                if hint not in constructs:
                    constructs.append(hint)
            return self._failure(
                query_id=qid,
                sql=sql,
                query_hash=query_hash,
                status=ParseStatus.UNSUPPORTED,
                reason=ParseFailureReason.PARSER_CANNOT_PARSE,
                constructs=constructs,
                detail=f"{type(exc).__name__}: {exc}",
                statement_count=0,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed on parser bugs
            return self._failure(
                query_id=qid,
                sql=sql,
                query_hash=query_hash,
                status=ParseStatus.ERROR,
                reason=ParseFailureReason.INTERNAL_PARSER_ERROR,
                constructs=[UnsupportedConstruct.PARSER_INTERNAL_ERROR],
                detail=f"{type(exc).__name__}: {exc}",
                statement_count=0,
            )

        if not statements:
            return self._failure(
                query_id=qid,
                sql=sql,
                query_hash=query_hash,
                status=ParseStatus.UNSUPPORTED,
                reason=ParseFailureReason.EMPTY_INPUT,
                constructs=[UnsupportedConstruct.EMPTY_INPUT],
                detail="SQL input produced no statement",
                statement_count=0,
            )
        if len(statements) != 1:
            return self._failure(
                query_id=qid,
                sql=sql,
                query_hash=query_hash,
                status=ParseStatus.UNSUPPORTED,
                reason=ParseFailureReason.MULTIPLE_STATEMENTS,
                constructs=[UnsupportedConstruct.MULTIPLE_STATEMENTS],
                detail=f"expected exactly one statement, got {len(statements)}",
                statement_count=len(statements),
            )

        statement = statements[0]
        if not isinstance(statement, exp.Select):
            if isinstance(statement, exp.SetOperation):
                construct = UnsupportedConstruct.SET_OPERATION
                reason = ParseFailureReason.SET_OPERATION
                detail = "set operations (UNION/INTERSECT/EXCEPT) are not supported"
            else:
                construct = UnsupportedConstruct.NOT_A_SELECT
                reason = ParseFailureReason.NOT_SELECT
                detail = f"only SELECT is supported, got {type(statement).__name__}"
            return self._failure(
                query_id=qid,
                sql=sql,
                query_hash=query_hash,
                status=ParseStatus.UNSUPPORTED,
                reason=reason,
                constructs=[construct],
                detail=detail,
                statement_count=1,
            )

        if _has_nulls_ordering(tokens):
            return self._failure(
                query_id=qid,
                sql=sql,
                query_hash=query_hash,
                status=ParseStatus.UNSUPPORTED,
                reason=ParseFailureReason.UNSUPPORTED_SYNTAX,
                constructs=[UnsupportedConstruct.ORDER_BY],
                detail="explicit NULLS FIRST/LAST ordering is not supported",
                statement_count=1,
            )

        try:
            analysis = _analyze(statement)
        except _UnsupportedQuery as exc:
            constructs = list(exc.constructs)
            for hint in hints:
                if hint not in constructs:
                    constructs.append(hint)
            return self._failure(
                query_id=qid,
                sql=sql,
                query_hash=query_hash,
                status=ParseStatus.UNSUPPORTED,
                reason=exc.reason,
                constructs=constructs,
                detail=exc.detail,
                statement_count=1,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed on analyzer bugs
            return self._failure(
                query_id=qid,
                sql=sql,
                query_hash=query_hash,
                status=ParseStatus.ERROR,
                reason=ParseFailureReason.INTERNAL_PARSER_ERROR,
                constructs=[UnsupportedConstruct.PARSER_INTERNAL_ERROR],
                detail=f"{type(exc).__name__}: {exc}",
                statement_count=1,
            )

        # Private deterministic facts, scoped to this request. Never retain the
        # caller's AST or expose nested objects from this analysis.
        from ..request_memo import bucket, remember
        cache = bucket('analysis', sql) if type(self) is SQLParserAdapter else None
        if cache is not None:
            remember(cache, self._memo_key(sql), deepcopy(analysis))
        return ParseResult(
            query_id=qid,
            sql=sql,
            parse_status=ParseStatus.SUCCESS,
            query_hash=query_hash,
            template_hash=_sha256(analysis.normalized_template),
            normalized_template=analysis.normalized_template,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            parser_dialect=self.parser_dialect,
            statement_count=1,
            failure_reason=None,
            unsupported_constructs=[],
            failure_detail=None,
            warnings=analysis.warnings,
            ast=statement,
        )

    def extract_features(
        self,
        parsed: ParseResult,
        metadata: Mapping[str, Any] | None = None,
    ) -> QueryFeatures:
        """Extract features from a parse result.

        Metadata enrichment is not implemented in Phase 2.  Passing a non-None
        ``metadata`` value is rejected rather than silently ignored.
        """

        if metadata is not None:
            raise NotImplementedError("metadata enrichment is not implemented in Phase 2")

        if parsed.parse_status is not ParseStatus.SUCCESS:
            reason = (
                MissingReason.PARSER_LIMITATION
                if parsed.parse_status is ParseStatus.ERROR
                else MissingReason.UNSUPPORTED_SYNTAX
            )
            return _all_missing_features(
                query_id=parsed.query_id,
                query_hash=parsed.query_hash,
                reason=reason,
                warnings=[
                    *parsed.warnings,
                    "No features were extracted because parsing did not succeed.",
                ],
            )

        # Re-parse the same text instead of trusting a possibly mutated AST.
        reparsed = self.parse(parsed.sql, query_id=parsed.query_id)
        if reparsed.parse_status is not ParseStatus.SUCCESS or reparsed.ast is None:
            raise ParserInternalError(
                "re-parsing succeeded earlier but failed during feature extraction"
            )

        if reparsed.normalized_template != parsed.normalized_template:
            raise ParserInternalError(
                "normalized template changed between parse and feature extraction"
            )
        if reparsed.query_hash != parsed.query_hash:
            raise ParserInternalError("query hash changed between parse and feature extraction")
        from ..request_memo import bucket, remember
        cache = bucket('features', parsed.sql) if type(self) is SQLParserAdapter else None
        key = self._memo_key(parsed.sql)
        if cache is not None and key in cache:
            return cache[key].model_copy(deep=True, update={'query_id': parsed.query_id})
        analyses = bucket('analysis', parsed.sql) if type(self) is SQLParserAdapter else None
        analysis = (deepcopy(analyses[key]) if analyses is not None and key in analyses
                    else _analyze(reparsed.ast))
        if analysis.normalized_template != parsed.normalized_template:
            raise ParserInternalError("normalized template changed during feature extraction")
        result = _features_from_analysis(
            query_id=parsed.query_id,
            query_hash=parsed.query_hash,
            template_hash=reparsed.template_hash or _sha256(analysis.normalized_template),
            analysis=analysis,
        )
        if cache is not None:
            remember(cache, key, result.model_copy(deep=True))
        return result

    def _failure(
        self,
        *,
        query_id: str,
        sql: str,
        query_hash: str,
        status: ParseStatus,
        reason: ParseFailureReason,
        constructs: list[UnsupportedConstruct],
        detail: str,
        statement_count: int,
    ) -> ParseResult:
        return ParseResult(
            query_id=query_id,
            sql=sql,
            parse_status=status,
            query_hash=query_hash,
            template_hash=None,
            normalized_template=None,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            parser_dialect=self.parser_dialect,
            statement_count=statement_count,
            failure_reason=reason,
            unsupported_constructs=constructs,
            failure_detail=detail[:2000],
            warnings=[],
            ast=None,
        )


__all__ = ["SQLParserAdapter"]
