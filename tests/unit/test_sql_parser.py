"""Unit tests for the fixture-scoped SQL parser adapter."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError
from sqlglot import exp

import tsdb_optimizer.parsing.sql_parser as parser_module
from tsdb_optimizer.errors import ParserInternalError
from tsdb_optimizer.parsing import SQLParserAdapter
from tsdb_optimizer.schemas.query_features import (
    ComparisonOperator,
    MissingReason,
    ParseFailureReason,
    ParseStatus,
    QueryType,
    UnsupportedConstruct,
)


@pytest.fixture(scope="module")
def parser() -> SQLParserAdapter:
    return SQLParserAdapter()


def _parse(parser: SQLParserAdapter, sql: str, query_id: str = "case"):
    return parser.parse(sql, query_id=query_id)


def _features(parser: SQLParserAdapter, sql: str, query_id: str = "case"):
    return parser.extract_features(_parse(parser, sql, query_id))


def test_basic_projection_and_time_point(parser: SQLParserAdapter) -> None:
    parsed = _parse(
        parser,
        "SELECT s_1666, s_1766 FROM root.db800.g_0.d_0, root.db800.g_0.d_1 "
        "WHERE time = 1640966450000;",
        "db800_q1",
    )
    assert parsed.parse_status is ParseStatus.SUCCESS
    assert parsed.failure_reason is None
    assert parsed.unsupported_constructs == []
    assert parsed.statement_count == 1
    assert parsed.parser_name == "sqlglot"
    assert parsed.parser_version == "30.18.0"
    assert "no IoTDB dialect" in parsed.parser_dialect

    features = parser.extract_features(parsed)
    assert features.query_types == [
        QueryType.PROJECTION,
        QueryType.TIME_FILTER,
        QueryType.TIME_POINT,
        QueryType.MULTI_DEVICE,
        QueryType.MULTI_SERIES,
    ]
    assert [device.path for device in features.devices.value] == [
        "root.db800.g_0.d_0",
        "root.db800.g_0.d_1",
    ]
    assert features.device_count.value == 2
    assert features.selected_series.value is not None
    assert [series.name for series in features.selected_series.value] == [
        "s_1666",
        "s_1766",
    ]
    assert features.series_count.value == 2
    assert features.time_predicates.value is not None
    assert features.time_predicates.value[0].operator is ComparisonOperator.EQ
    assert features.time_predicates.value[0].value_ms == 1640966450000
    assert features.time_range.value.start_ms == 1640966450000
    assert features.time_range.value.end_ms == 1640966450000
    assert features.time_range_ms.value == 0
    assert features.filter_count.value == 1
    assert features.device_paths_verified.value is None
    assert (
        features.device_paths_verified.missing_reason
        is MissingReason.NEEDS_METADATA
    )
    assert features.estimated_rows.value is None
    assert (
        features.estimated_rows.missing_reason is MissingReason.NEEDS_STATISTICS
    )
    assert any("existence is not verified" in warning for warning in features.warnings)


def test_time_range_and_value_filters(parser: SQLParserAdapter) -> None:
    features = _features(
        parser,
        "SELECT s_1666, s_1766 FROM root.db800.g_0.d_0, root.db800.g_0.d_1 "
        "WHERE time >= 1640966400000 AND time <= 1640966650000 "
        "AND root.db800.g_0.d_0.s_1666 > -5 "
        "AND root.db800.g_0.d_1.s_1766 <= 3",
    )
    assert features.time_range.value.start_ms == 1640966400000
    assert features.time_range.value.end_ms == 1640966650000
    assert features.time_range.value.start_inclusive is True
    assert features.time_range.value.end_inclusive is True
    assert features.time_range_ms.value == 250000
    assert features.filter_count.value == 4
    assert features.value_filters.value is not None
    assert len(features.value_filters.value) == 2

    first = features.value_filters.value[0]
    assert first.path == "root.db800.g_0.d_0.s_1666"
    assert first.device == "root.db800.g_0.d_0"
    assert first.series == "s_1666"
    assert first.operator is ComparisonOperator.GT
    assert first.value == -5

    second = features.value_filters.value[1]
    assert second.path == "root.db800.g_0.d_1.s_1766"
    assert second.operator is ComparisonOperator.LTE
    assert second.value == 3
    assert QueryType.VALUE_FILTER in features.query_types
    assert QueryType.TIME_RANGE in features.query_types


def test_count_and_order_by_desc(parser: SQLParserAdapter) -> None:
    features = _features(
        parser,
        "SELECT COUNT(s_1666), COUNT(s_1766) FROM root.db800.g_0.d_0, "
        "root.db800.g_0.d_1 WHERE time >= 1 AND time <= 2 ORDER BY time DESC",
    )
    assert features.aggregations.value is not None
    assert [aggregation.function for aggregation in features.aggregations.value] == [
        "COUNT",
        "COUNT",
    ]
    assert features.aggregation_types.value == ["COUNT"]
    assert features.order_by.value is not None
    assert features.order_by.value[0].column == "time"
    assert features.order_by.value[0].direction == "DESC"
    assert features.has_order_by.value is True
    assert features.query_types == [
        QueryType.PROJECTION,
        QueryType.AGGREGATION,
        QueryType.TIME_FILTER,
        QueryType.TIME_RANGE,
        QueryType.MULTI_DEVICE,
        QueryType.MULTI_SERIES,
        QueryType.ORDER_BY,
        QueryType.DESCENDING_ORDER,
    ]


def test_max_time_and_last_value(parser: SQLParserAdapter) -> None:
    features = _features(
        parser,
        "SELECT MAX_TIME(s_130), LAST_VALUE(s_130) FROM root.test.g_0.d_0",
    )
    assert features.aggregations.value is not None
    assert [aggregation.function for aggregation in features.aggregations.value] == [
        "MAX_TIME",
        "LAST_VALUE",
    ]
    assert features.aggregation_types.value == ["MAX_TIME", "LAST_VALUE"]
    assert features.normalized_template == (
        "SELECT MAX_TIME(s_130), LAST_VALUE(s_130) FROM root.test.g_0.d_0"
    )


def test_count_star_is_supported_with_warning(parser: SQLParserAdapter) -> None:
    parsed = _parse(parser, "SELECT COUNT(*) FROM root.test.g_0.d_0")
    features = parser.extract_features(parsed)

    assert parsed.parse_status is ParseStatus.SUCCESS
    assert QueryType.COUNT_ALL in features.query_types
    assert features.aggregations.value[0].is_star is True
    assert features.selected_series.value is None
    assert features.selected_series.missing_reason is MissingReason.NEEDS_METADATA
    assert features.series_count.value is None
    assert any("expands per series" in warning for warning in features.warnings)


def test_select_star_is_supported_with_warning(parser: SQLParserAdapter) -> None:
    parsed = _parse(
        parser, "SELECT * FROM root.test.g_0.d_0 LIMIT 2", "star-case"
    )
    features = parser.extract_features(parsed)

    assert parsed.parse_status is ParseStatus.SUCCESS
    assert QueryType.WILDCARD_PROJECTION in features.query_types
    assert features.selected_series.value is None
    assert features.series_count.value is None
    assert features.limit.value == 2
    assert features.normalized_template == "SELECT * FROM root.test.g_0.d_0 LIMIT ?"
    assert any("expands to one column per series" in warning for warning in features.warnings)


def test_qualified_series_projection(parser: SQLParserAdapter) -> None:
    features = _features(
        parser,
        "SELECT root.db800.g_0.d_0.s_1666 FROM root.db800.g_0.d_0",
    )
    selector = features.selected_series.value[0]
    assert selector.name == "s_1666"
    assert selector.device == "root.db800.g_0.d_0"
    assert selector.qualified_path == "root.db800.g_0.d_0.s_1666"


def test_wrong_device_path_is_syntactically_supported_but_unverified(
    parser: SQLParserAdapter,
) -> None:
    parsed = _parse(
        parser, "SELECT s_0 FROM root.test.d_0 LIMIT 5", "wrong-device-path"
    )
    features = parser.extract_features(parsed)

    assert parsed.parse_status is ParseStatus.SUCCESS
    assert features.devices.value[0].parts == ["root", "test", "d_0"]
    assert features.device_paths_verified.value is None
    assert (
        features.device_paths_verified.missing_reason
        is MissingReason.NEEDS_METADATA
    )
    assert any("existence is not verified" in warning for warning in features.warnings)


def test_query_hash_and_template_hash_are_deterministic(
    parser: SQLParserAdapter,
) -> None:
    sql_a = "SELECT s_130 FROM root.test.g_0.d_0 WHERE time = 987654321"
    sql_b = "SELECT s_130 FROM root.test.g_0.d_0 WHERE time = 123456789"
    parsed_a = _parse(parser, sql_a)
    parsed_b = _parse(parser, sql_b)

    assert parsed_a.query_hash == hashlib.sha256(sql_a.encode("utf-8")).hexdigest()
    assert parsed_b.query_hash == hashlib.sha256(sql_b.encode("utf-8")).hexdigest()
    assert parsed_a.query_hash != parsed_b.query_hash
    assert parsed_a.normalized_template == (
        "SELECT s_130 FROM root.test.g_0.d_0 WHERE time = ?"
    )
    assert parsed_a.normalized_template == parsed_b.normalized_template
    assert parsed_a.template_hash == parsed_b.template_hash
    assert "987654321" not in parsed_a.normalized_template


def test_whitespace_changes_query_hash_but_not_template(parser: SQLParserAdapter) -> None:
    first = _parse(parser, "SELECT s_130 FROM root.test.g_0.d_0")
    second = _parse(parser, "SELECT  s_130\nFROM root.test.g_0.d_0")
    assert first.query_hash != second.query_hash
    assert first.normalized_template == second.normalized_template
    assert first.template_hash == second.template_hash


def test_time_range_missing_reasons(parser: SQLParserAdapter) -> None:
    incomplete = _features(
        parser, "SELECT s_130 FROM root.test.g_0.d_0 WHERE time > 5"
    )
    assert incomplete.time_range.value.start_ms == 5
    assert incomplete.time_range.value.end_ms is None
    assert incomplete.time_range_ms.value is None
    assert (
        incomplete.time_range_ms.missing_reason
        is MissingReason.INCOMPLETE_TIME_RANGE
    )
    assert QueryType.TIME_RANGE not in incomplete.query_types

    invalid = _features(
        parser,
        "SELECT s_130 FROM root.test.g_0.d_0 WHERE time > 5 AND time < 5",
    )
    assert invalid.time_range.value.start_ms == 5
    assert invalid.time_range.value.end_ms == 5
    assert invalid.time_range_ms.value is None
    assert invalid.time_range_ms.missing_reason is MissingReason.INVALID_TIME_RANGE
    assert QueryType.TIME_RANGE not in invalid.query_types

    ambiguous = _features(
        parser,
        "SELECT s_130 FROM root.test.g_0.d_0 WHERE time = 1 AND time = 2",
    )
    assert ambiguous.time_range.value is None
    assert ambiguous.time_range.missing_reason is MissingReason.AMBIGUOUS_TIME_RANGE


def test_absent_ast_features_are_explicit(parser: SQLParserAdapter) -> None:
    features = _features(parser, "SELECT s_130 FROM root.test.g_0.d_0")
    assert features.time_predicates.value is None
    assert features.time_predicates.missing_reason is MissingReason.NOT_PRESENT_IN_SQL
    assert features.time_predicates.source.value == "SQL_AST"
    assert features.has_limit.value is False
    assert features.has_limit.missing_reason is None
    assert features.limit.value is None
    assert features.limit.missing_reason is MissingReason.NOT_PRESENT_IN_SQL
    assert features.group_by.value is None
    assert features.has_group_by.value is False


@pytest.mark.parametrize(
    ("sql", "failure_reason", "construct"),
    [
        ("", ParseFailureReason.EMPTY_INPUT, UnsupportedConstruct.EMPTY_INPUT),
        (
            "SELECT s FROM root.a.b.c; SELECT s FROM root.a.b.c",
            ParseFailureReason.MULTIPLE_STATEMENTS,
            UnsupportedConstruct.MULTIPLE_STATEMENTS,
        ),
        (
            "DELETE FROM root.a.b.c",
            ParseFailureReason.NOT_SELECT,
            UnsupportedConstruct.NOT_A_SELECT,
        ),
        (
            "SHOW TABLES",
            ParseFailureReason.NOT_SELECT,
            UnsupportedConstruct.NOT_A_SELECT,
        ),
        (
            "SELECT s FROM root.a.b.c UNION SELECT s FROM root.a.b.c",
            ParseFailureReason.SET_OPERATION,
            UnsupportedConstruct.SET_OPERATION,
        ),
        (
            "SELECT 1",
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.MISSING_FROM,
        ),
        (
            "SELECT s FROM root.a.b.c GROUP BY s",
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.GROUP_BY,
        ),
        (
            "SELECT s FROM root.a.b.c HAVING COUNT(s) > 0",
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.HAVING,
        ),
        (
            "SELECT DISTINCT s FROM root.a.b.c",
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.DISTINCT,
        ),
        (
            "SELECT s FROM root.a.b.c LIMIT 1 OFFSET 2",
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.OFFSET,
        ),
        (
            "SELECT s FROM root.a.b.c ORDER BY s",
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.ORDER_BY,
        ),
        (
            "SELECT s FROM root.a.b.c ORDER BY time ASC NULLS LAST",
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.ORDER_BY,
        ),
        (
            "SELECT s AS x FROM root.a.b.c",
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.SELECT_ALIAS,
        ),
        (
            "SELECT s FROM root.a.b.c AS x",
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.TABLE_ALIAS,
        ),
        (
            "SELECT s FROM root.a.b.c JOIN root.a.b.d ON root.a.b.c.time = root.a.b.d.time",
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.EXPLICIT_JOIN,
        ),
        (
            "SELECT s FROM (SELECT s FROM root.a.b.c)",
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.SUBQUERY,
        ),
        (
            "SELECT s FROM root.a.b.c WHERE s > 1 OR s < 2",
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.UNKNOWN_WHERE_EXPRESSION,
        ),
        (
            "SELECT s FROM root.a.b.c WHERE s != 2",
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.UNKNOWN_WHERE_EXPRESSION,
        ),
        (
            "SELECT s FROM root.a.b.c WHERE s > '1'",
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.UNSUPPORTED_LITERAL,
        ),
        (
            "SELECT s FROM root.a.b.c WHERE s > 1.5",
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.UNSUPPORTED_LITERAL,
        ),
        (
            "SELECT s FROM root.a.b.c WHERE s > 1",
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.UNKNOWN_WHERE_EXPRESSION,
        ),
        (
            'SELECT "s" FROM root.a.b.c',
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.QUOTED_IDENTIFIER,
        ),
        (
            "SELECT s FROM root.a.b.c.d.s",
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.INVALID_PATH,
        ),
        (
            "SELECT MAVG(s_130, 7) FROM root.test.g_0.d_0",
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.UNKNOWN_FUNCTION,
        ),
        (
            "SELECT TIME_FLOOR(time, 3600000) FROM root.test.g_0.d_0",
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.UNKNOWN_FUNCTION,
        ),
        (
            "SELECT LAST(s_130) FROM root.test.g_0.d_0",
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.UNKNOWN_FUNCTION,
        ),
        (
            "SELECT s FROM root.a.b.c WHERE time BETWEEN 1 AND 2",
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.UNKNOWN_WHERE_EXPRESSION,
        ),
        (
            "SELECT s FROM root.a.b.c WHERE time IN (1, 2)",
            ParseFailureReason.UNSUPPORTED_SYNTAX,
            UnsupportedConstruct.UNKNOWN_WHERE_EXPRESSION,
        ),
    ],
)
def test_unsupported_sql_fails_closed(
    parser: SQLParserAdapter,
    sql: str,
    failure_reason: ParseFailureReason,
    construct: UnsupportedConstruct,
) -> None:
    parsed = _parse(parser, sql)
    assert parsed.parse_status is ParseStatus.UNSUPPORTED
    assert parsed.failure_reason is failure_reason
    assert construct in parsed.unsupported_constructs
    assert parsed.normalized_template is None
    assert parsed.template_hash is None
    assert parsed.ast is None

    features = parser.extract_features(parsed)
    assert features.query_types == []
    assert features.normalized_template is None
    assert features.template_hash is None
    for name in type(features).model_fields:
        feature = getattr(features, name)
        if hasattr(feature, "value"):
            assert feature.value is None
    assert (
        features.time_predicates.missing_reason
        is MissingReason.UNSUPPORTED_SYNTAX
    )
    assert features.estimated_rows.missing_reason is MissingReason.NEEDS_STATISTICS
    assert (
        features.device_paths_verified.missing_reason is MissingReason.NEEDS_METADATA
    )


def test_last_syntax_is_specifically_detected(parser: SQLParserAdapter) -> None:
    parsed = _parse(
        parser, "SELECT last s_1666, s_1766 FROM root.db800.g_0.d_0"
    )
    assert parsed.parse_status is ParseStatus.UNSUPPORTED
    assert UnsupportedConstruct.LAST_SYNTAX in parsed.unsupported_constructs


def test_group_by_interval_reports_parser_limitation(parser: SQLParserAdapter) -> None:
    parsed = _parse(
        parser,
        "SELECT count(s_1666) FROM root.db800.g_0.d_0 "
        "GROUP BY ([1640966400000, 1640966650000),20000ms)",
    )
    assert parsed.parse_status is ParseStatus.UNSUPPORTED
    assert parsed.failure_reason is ParseFailureReason.PARSER_CANNOT_PARSE
    assert UnsupportedConstruct.GROUP_BY in parsed.unsupported_constructs
    assert UnsupportedConstruct.SQLGLOT_PARSE_ERROR in parsed.unsupported_constructs


def test_wildcard_path_reports_parser_limitation(parser: SQLParserAdapter) -> None:
    parsed = _parse(
        parser,
        "SELECT s_1666 FROM root.dbm.g_0.* "
        "WHERE time >= 1640966441905 AND time <= 1640972441704",
    )
    assert parsed.parse_status is ParseStatus.UNSUPPORTED
    assert parsed.failure_reason is ParseFailureReason.PARSER_CANNOT_PARSE
    assert UnsupportedConstruct.WILDCARD_PATH in parsed.unsupported_constructs


def test_metadata_enrichment_is_explicitly_not_implemented(
    parser: SQLParserAdapter,
) -> None:
    parsed = _parse(parser, "SELECT s_130 FROM root.test.g_0.d_0")
    with pytest.raises(NotImplementedError, match="metadata enrichment"):
        parser.extract_features(parsed, metadata={"device_exists": True})


def test_tampered_parse_result_is_rejected(parser: SQLParserAdapter) -> None:
    parsed = _parse(parser, "SELECT s_130 FROM root.test.g_0.d_0")
    tampered = parsed.model_copy(
        update={
            "normalized_template": "SELECT tampered",
            "template_hash": hashlib.sha256(b"SELECT tampered").hexdigest(),
        }
    )
    with pytest.raises(ParserInternalError, match="normalized template changed"):
        parser.extract_features(tampered)


def test_success_result_whose_sql_stops_parsing_is_rejected(
    parser: SQLParserAdapter,
) -> None:
    parsed = _parse(parser, "SELECT s_130 FROM root.test.g_0.d_0")
    tampered = parsed.model_copy(update={"sql": "SELECT LAST s_130 FROM root.test.g_0.d_0"})
    with pytest.raises(ParserInternalError, match="re-parsing succeeded earlier but failed"):
        parser.extract_features(tampered)


def test_unknown_ast_node_fails_closed(
    parser: SQLParserAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = parser_module.sqlglot.parse_one(
        "SELECT s_130 FROM root.test.g_0.d_0"
    )
    assert isinstance(base, exp.Select)
    base.set("expressions", [exp.Paren(this=base.expressions[0])])
    monkeypatch.setattr(parser_module.sqlglot, "parse", lambda sql: [base])

    parsed = parser.parse("SELECT s_130 FROM root.test.g_0.d_0")
    assert parsed.parse_status is ParseStatus.UNSUPPORTED
    assert parsed.failure_reason is ParseFailureReason.UNKNOWN_AST_NODE
    assert UnsupportedConstruct.UNKNOWN_AST_NODE in parsed.unsupported_constructs


def test_unexpected_analyzer_error_is_error_not_success(
    parser: SQLParserAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(statement: exp.Select):
        raise RuntimeError("boom")

    monkeypatch.setattr(parser_module, "_analyze", boom)
    parsed = parser.parse("SELECT s_130 FROM root.test.g_0.d_0")
    assert parsed.parse_status is ParseStatus.ERROR
    assert parsed.failure_reason is ParseFailureReason.INTERNAL_PARSER_ERROR
    assert "boom" in (parsed.failure_detail or "")


def test_parser_rejects_non_string_input(parser: SQLParserAdapter) -> None:
    with pytest.raises(TypeError, match="sql must be a string"):
        parser.parse(123)  # type: ignore[arg-type]


def test_feature_schema_rejects_unknown_fields(parser: SQLParserAdapter) -> None:
    features = _features(parser, "SELECT s_130 FROM root.test.g_0.d_0")
    payload = features.model_dump(
        exclude={"feature_sources", "missing_features"}
    )
    payload["unknown"] = 1
    with pytest.raises(ValidationError, match="Extra inputs"):
        type(features).model_validate(payload)
