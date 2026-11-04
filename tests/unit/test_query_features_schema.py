"""Schema invariants for Agent 1's query-feature contracts."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from tsdb_optimizer.agents import QueryAnalysisAgent
from tsdb_optimizer.schemas.query_features import (
    AggregationSpec,
    Feature,
    FeatureSource,
    MissingReason,
    ParseFailureReason,
    ParseResult,
    ParseStatus,
    QueryAnalysisResult,
    SeriesSelector,
    TimeRange,
)


def test_feature_missing_value_requires_reason() -> None:
    with pytest.raises(ValidationError, match="missing_reason"):
        Feature(value=None, source=FeatureSource.SQL_AST)


def test_feature_present_value_must_not_have_reason() -> None:
    with pytest.raises(ValidationError, match="missing_reason must be None"):
        Feature(
            value=1,
            source=FeatureSource.SQL_AST,
            missing_reason=MissingReason.NOT_PRESENT_IN_SQL,
        )


def test_feature_present_value_requires_real_source() -> None:
    with pytest.raises(ValidationError, match="source must be identified"):
        Feature(value=1)


def test_time_range_needs_at_least_one_bound() -> None:
    with pytest.raises(ValidationError, match="at least one bound"):
        TimeRange()


def test_star_aggregation_is_count_only() -> None:
    with pytest.raises(ValidationError, match="only COUNT"):
        AggregationSpec(function="SUM", is_star=True)
    with pytest.raises(ValidationError, match="must not carry a series"):
        AggregationSpec(
            function="COUNT",
            is_star=True,
            series=SeriesSelector(name="s_130"),
        )


def test_parse_result_success_invariants() -> None:
    with pytest.raises(ValidationError, match="failure reason"):
        ParseResult(
            query_id="q",
            sql="SELECT 1",
            parse_status=ParseStatus.SUCCESS,
            query_hash="0" * 64,
            template_hash="0" * 64,
            normalized_template="SELECT ?",
            parser_name="p",
            parser_version="1",
            parser_dialect="d",
            statement_count=1,
            failure_reason=ParseFailureReason.NOT_SELECT,
        )


def test_parse_result_failure_invariants() -> None:
    with pytest.raises(ValidationError, match="must not expose a template"):
        ParseResult(
            query_id="q",
            sql="DELETE",
            parse_status=ParseStatus.UNSUPPORTED,
            query_hash="0" * 64,
            normalized_template="DELETE",
            parser_name="p",
            parser_version="1",
            parser_dialect="d",
            statement_count=1,
            failure_reason=ParseFailureReason.NOT_SELECT,
        )


def test_agent_output_serializes_to_json_without_ast() -> None:
    agent = QueryAnalysisAgent()
    result = agent.analyze(
        "SELECT s_130 FROM root.test.g_0.d_0 WHERE time >= 1 AND time < 2 LIMIT 10",
        query_id="json-case",
    )

    payload = json.loads(result.model_dump_json())
    assert payload["query_id"] == "json-case"
    assert payload["parse_status"] == "SUCCESS"
    assert "ast" not in payload["parse"]
    assert "ast" not in json.dumps(payload)
    assert payload["query_hash"] == result.query_hash
    assert payload["normalized_template"] == (
        "SELECT s_130 FROM root.test.g_0.d_0 WHERE time >= ? AND time < ? LIMIT ?"
    )
    assert payload["features"]["estimated_rows"]["value"] is None
    assert (
        payload["features"]["estimated_rows"]["missing_reason"]
        == "NEEDS_STATISTICS"
    )
    assert payload["features"]["device_paths_verified"]["value"] is None
    assert (
        payload["features"]["device_paths_verified"]["missing_reason"]
        == "NEEDS_METADATA"
    )
    assert "estimated_rows" in payload["features_missing"]
    assert "device_paths_verified" in payload["features_missing"]


def test_agent_output_has_provenance_for_every_feature() -> None:
    agent = QueryAnalysisAgent()
    result = agent.analyze(
        "SELECT COUNT(s_130) FROM root.test.g_0.d_0 WHERE time = 1",
        query_id="provenance-case",
    )
    features = result.features

    feature_names = {
        name
        for name in type(features).model_fields
        if isinstance(getattr(features, name), Feature)
    }
    assert set(features.feature_sources) == feature_names
    assert features.feature_sources["time_range"] == "SQL_AST"
    assert features.feature_sources["estimated_rows"] == "NOT_AVAILABLE"
    assert features.feature_sources["device_paths_verified"] == "NOT_AVAILABLE"
    assert features.time_range.value is not None
    assert features.time_range.value.start_ms == 1
    assert features.time_range_ms.value == 0
    assert features.aggregation_types.value == ["COUNT"]


def test_agent_rejects_mismatched_query_ids() -> None:
    agent = QueryAnalysisAgent()
    result = agent.analyze(
        "SELECT s_130 FROM root.test.g_0.d_0", query_id="first"
    )
    with pytest.raises(ValidationError, match="query_id does not match"):
        QueryAnalysisResult(
            query_id="second",
            parse=result.parse,
            features=result.features,
        )
