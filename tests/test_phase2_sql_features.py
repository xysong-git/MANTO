"""Fixture-driven Phase-2 parser acceptance tests.

The workload fixture contains 47 real IoTDB 2.0.6 SQL statements captured in
Phase 0.  The negative fixture contains 24 statements the server rejects or
that need a warning.  Expectations here are explicit, not derived from the
parser under test.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from tsdb_optimizer.agents import QueryAnalysisAgent
from tsdb_optimizer.schemas.query_features import (
    Feature,
    MissingReason,
    ParseFailureReason,
    ParseStatus,
    QueryType,
    UnsupportedConstruct,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SQL_FIXTURES = FIXTURES / "sql"
PLAN_FIXTURES = FIXTURES / "plans"

# 13/47 real IoTDB statements use constructs outside the Phase-2 subset:
# eight interval GROUP BY cases, four bare LAST cases, one wildcard path.
UNSUPPORTED_WORKLOAD_IDS = {
    "db800_q7",
    "db800_q8",
    "db800_q11",
    "dbs_q7",
    "dbs_q8",
    "dbs_q11",
    "dbl_q7",
    "dbl_q8",
    "dbl_q11",
    "dbm_q7",
    "dbm_q8",
    "dbm_q11",
    "dbm_q12_union",
}

NEGATIVE_EXPECTATIONS: dict[str, tuple[ParseStatus, set[UnsupportedConstruct]]] = {
    "union_q12": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.SET_OPERATION},
    ),
    "q08_subquery_lag_mavg_exact_file": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.SQLGLOT_PARSE_ERROR},
    ),
    "q08_subquery_lag_mavg_cleaned": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.SQLGLOT_PARSE_ERROR},
    ),
    "lag_window": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.SQLGLOT_PARSE_ERROR},
    ),
    "mavg_unknown_function": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.SQLGLOT_PARSE_ERROR},
    ),
    "time_floor_unknown_function": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.SQLGLOT_PARSE_ERROR},
    ),
    "select_without_from": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.MISSING_FROM},
    ),
    "show_tables_tree": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.NOT_A_SELECT},
    ),
    "explain_format_json": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.NOT_A_SELECT},
    ),
    "explain_analyze_format_json": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.NOT_A_SELECT},
    ),
    "explain_verbose": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.NOT_A_SELECT},
    ),
    "last_value_filter": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.LAST_SYNTAX},
    ),
    "series_path_in_from": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.INVALID_PATH},
    ),
    "wrong_device_path": (ParseStatus.SUCCESS, set()),
    "group_by_time_without_range": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.GROUP_BY},
    ),
    "count_star_semantics": (ParseStatus.SUCCESS, set()),
    "select_star_result_size": (ParseStatus.SUCCESS, set()),
    "last_single_float_precision": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.LAST_SYNTAX},
    ),
    "last_multi_series_shape": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.LAST_SYNTAX},
    ),
    "align_by_device_shape": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.ALIGN_BY_DEVICE},
    ),
    "table_model_show_tables": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.NOT_A_SELECT},
    ),
    "table_model_select_without_from": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.MISSING_FROM},
    ),
    "ddl_dml_policy": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.NOT_A_SELECT},
    ),
    "union_to_single_range_not_equivalent": (ParseStatus.SUCCESS, set()),
}

PLAN_QUERY_EXPECTATIONS = {
    "native_scan": (ParseStatus.SUCCESS, set()),
    "aggregation_count": (ParseStatus.SUCCESS, set()),
    "last_query": (ParseStatus.UNSUPPORTED, {UnsupportedConstruct.LAST_SYNTAX}),
    "native_desc_limit_1": (ParseStatus.SUCCESS, set()),
    "full_outer_time_join_2devices": (ParseStatus.SUCCESS, set()),
    "device_view_2devices": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.ALIGN_BY_DEVICE},
    ),
    "agg_max_time_last_value": (ParseStatus.SUCCESS, set()),
    "limit_10": (ParseStatus.SUCCESS, set()),
    "desc_limit_10": (ParseStatus.SUCCESS, set()),
    "empty_plan_series_path": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.INVALID_PATH},
    ),
    "empty_plan_wrong_device_path": (ParseStatus.SUCCESS, set()),
    "empty_plan_mavg": (
        ParseStatus.UNSUPPORTED,
        {UnsupportedConstruct.SQLGLOT_PARSE_ERROR},
    ),
}


def _load(name: str) -> dict:
    with (SQL_FIXTURES / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def workload_cases() -> list[dict]:
    return _load("workload_cases.json")["cases"]


@pytest.fixture(scope="module")
def negative_cases() -> list[dict]:
    return _load("negative_cases.json")["cases"]


@pytest.fixture(scope="module")
def agent() -> QueryAnalysisAgent:
    return QueryAnalysisAgent()


def _strip_explain(query: str) -> str:
    head, _, rest = query.partition(" ")
    if head.upper() != "EXPLAIN":
        return query
    if rest.upper().startswith("ANALYZE "):
        return rest[len("ANALYZE ") :]
    return rest


def test_workload_expectation_table_is_complete(workload_cases: list[dict]) -> None:
    case_ids = {case["case_id"] for case in workload_cases}
    assert len(workload_cases) == 47
    assert UNSUPPORTED_WORKLOAD_IDS <= case_ids
    assert len(UNSUPPORTED_WORKLOAD_IDS) == 13


@pytest.mark.parametrize(
    "case_id",
    [case["case_id"] for case in _load("workload_cases.json")["cases"]],
)
def test_real_workload_sql(
    agent: QueryAnalysisAgent,
    workload_cases: list[dict],
    case_id: str,
) -> None:
    case = next(case for case in workload_cases if case["case_id"] == case_id)
    result = agent.analyze(case["sql"], query_id=case_id)

    if case_id in UNSUPPORTED_WORKLOAD_IDS:
        assert result.parse_status is ParseStatus.UNSUPPORTED
        assert result.normalized_template is None
        assert result.features.query_types == []
        assert all(
            getattr(result.features, name).value is None
            for name in type(result.features).model_fields
            if isinstance(getattr(result.features, name), Feature)
        )
        if case_id.endswith("_q7") or case_id.endswith("_q11"):
            assert result.parse.failure_reason is ParseFailureReason.PARSER_CANNOT_PARSE
            assert UnsupportedConstruct.GROUP_BY in result.unsupported_constructs
        elif case_id.endswith("_q8"):
            assert UnsupportedConstruct.LAST_SYNTAX in result.unsupported_constructs
        else:
            assert case_id == "dbm_q12_union"
            assert UnsupportedConstruct.WILDCARD_PATH in result.unsupported_constructs
    else:
        assert result.parse_status is ParseStatus.SUCCESS
        assert result.features.query_types
        assert result.normalized_template
        assert result.features.template_hash == result.parse.template_hash
        assert result.features.estimated_rows.value is None
        assert (
            result.features.estimated_rows.missing_reason
            is MissingReason.NEEDS_STATISTICS
        )
        assert result.features.device_paths_verified.value is None
        assert (
            result.features.device_paths_verified.missing_reason
            is MissingReason.NEEDS_METADATA
        )
        assert result.features.devices.value
        assert result.features.device_count.value == len(result.features.devices.value)
        assert result.features.filter_count.value is not None
        assert result.features.filter_count.value >= 0
        assert result.features.query_hash == result.parse.query_hash

    # Every result must serialize, including failures.
    payload = json.loads(result.model_dump_json())
    assert payload["query_id"] == case_id
    assert "ast" not in payload["parse"]


def test_workload_status_counts(
    agent: QueryAnalysisAgent, workload_cases: list[dict]
) -> None:
    counts = Counter(
        agent.analyze(case["sql"], query_id=case["case_id"]).parse_status.value
        for case in workload_cases
    )
    assert counts == {"SUCCESS": 34, "UNSUPPORTED": 13}


@pytest.mark.parametrize(
    "case_id", [case["case_id"] for case in _load("negative_cases.json")["cases"]]
)
def test_negative_fixture_sql(
    agent: QueryAnalysisAgent,
    negative_cases: list[dict],
    case_id: str,
) -> None:
    case = next(case for case in negative_cases if case["case_id"] == case_id)
    expected_status, expected_constructs = NEGATIVE_EXPECTATIONS[case_id]
    result = agent.analyze(case["sql"], query_id=case_id)

    assert result.parse_status is expected_status
    assert expected_constructs <= set(result.unsupported_constructs)
    if expected_status is ParseStatus.SUCCESS:
        assert result.normalized_template
        assert result.features.query_types
        assert result.features.estimated_rows.value is None
    else:
        assert result.normalized_template is None
        assert result.features.query_types == []
        assert result.features.missing_features


def test_negative_successes_have_the_right_semantics(
    agent: QueryAnalysisAgent, negative_cases: list[dict]
) -> None:
    cases = {case["case_id"]: case for case in negative_cases}

    count_star = agent.analyze(cases["count_star_semantics"]["sql"])
    assert QueryType.COUNT_ALL in count_star.query_types
    assert any("expands per series" in warning for warning in count_star.features.warnings)

    select_star = agent.analyze(cases["select_star_result_size"]["sql"])
    assert QueryType.WILDCARD_PROJECTION in select_star.query_types
    assert any(
        "one column per series" in warning for warning in select_star.features.warnings
    )

    wrong_device = agent.analyze(cases["wrong_device_path"]["sql"])
    assert wrong_device.parse_status is ParseStatus.SUCCESS
    assert wrong_device.features.device_paths_verified.value is None
    assert (
        wrong_device.features.device_paths_verified.missing_reason
        is MissingReason.NEEDS_METADATA
    )


@pytest.mark.parametrize("fixture_id", sorted(PLAN_QUERY_EXPECTATIONS))
def test_queries_from_plan_fixture_index(
    agent: QueryAnalysisAgent, fixture_id: str
) -> None:
    index = _load_json(PLAN_FIXTURES / "index.json")["fixtures"]
    sql = _strip_explain(index[fixture_id]["query"])
    expected_status, expected_constructs = PLAN_QUERY_EXPECTATIONS[fixture_id]
    result = agent.analyze(sql, query_id=f"plan-{fixture_id}")

    assert result.parse_status is expected_status
    assert expected_constructs <= set(result.unsupported_constructs)
    if expected_status is ParseStatus.SUCCESS:
        assert result.normalized_template
    else:
        assert result.normalized_template is None
