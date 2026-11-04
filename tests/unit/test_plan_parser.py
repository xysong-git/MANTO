from __future__ import annotations

import re
from pathlib import Path

import pytest

from tsdb_optimizer.monitoring.plans import (
    PlanParser,
    compare_plan_hashes,
    compare_plan_results,
)
from tsdb_optimizer.schemas.monitoring import (
    PlanComparisonStatus,
    PlanKind,
    PlanParseStatus,
    PlanUnavailableReason,
)

PLAN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "plans"


def _text(name: str) -> str:
    return (PLAN_DIR / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("fixture", "node_count", "operators"),
    [
        ("native_scan.txt", 1, ["AlignedSeriesScan"]),
        ("aggregation_count.txt", 1, ["AlignedSeriesAggregationScan"]),
        ("last_query.txt", 2, ["LastQuery", "LastQueryScan"]),
        (
            "full_outer_time_join_2devices.txt",
            3,
            ["FullOuterTimeJoin", "AlignedSeriesScan", "AlignedSeriesScan"],
        ),
        (
            "device_view_2devices.txt",
            3,
            ["DeviceView", "AlignedSeriesScan", "AlignedSeriesScan"],
        ),
        (
            "agg_max_time_last_value.txt",
            1,
            ["AlignedSeriesAggregationScan"],
        ),
        ("limit_10.txt", 1, ["AlignedSeriesScan"]),
        ("desc_limit_10.txt", 1, ["AlignedSeriesScan"]),
        ("native_desc_limit_1.txt", 1, ["AlignedSeriesScan"]),
    ],
)
def test_tree_explain_fixtures_parse_available(
    fixture: str, node_count: int, operators: list[str]
) -> None:
    result = PlanParser().parse(
        _text(fixture), model="tree", kind=PlanKind.EXPLAIN
    )
    assert result.status is PlanParseStatus.AVAILABLE
    assert result.model == "tree"
    assert result.plan_kind is PlanKind.EXPLAIN
    assert result.node_count == node_count
    assert result.signature is not None
    assert [node.operator for node in result.signature.nodes] == operators
    assert result.signature_hash is not None
    assert re.fullmatch(r"[0-9a-f]{64}", result.signature_hash)


def test_tree_nodes_have_explicit_parent_and_depth() -> None:
    result = PlanParser().parse(
        _text("device_view_2devices.txt"),
        model="tree",
        kind=PlanKind.EXPLAIN,
    )
    nodes = result.signature.nodes
    assert nodes[0].operator == "DeviceView"
    assert nodes[0].parent_index is None
    assert nodes[0].depth == 0
    assert nodes[1].parent_index == 0
    assert nodes[2].parent_index == 0
    assert nodes[1].depth == nodes[2].depth == 1


def test_plan_hash_ignores_node_suffix_partition_and_attribute_order() -> None:
    parser = PlanParser()
    original = parser.parse(
        _text("native_scan.txt"), model="tree", kind=PlanKind.EXPLAIN
    )
    changed = _text("native_scan.txt")
    changed = changed.replace("AlignedSeriesScan-1", "AlignedSeriesScan-9")
    changed = changed.replace("Partition: 100", "Partition: 999")
    parsed = parser.parse(changed, model="tree", kind=PlanKind.EXPLAIN)

    assert parsed.status is PlanParseStatus.AVAILABLE
    assert parsed.signature_hash == original.signature_hash
    assert "Partition" in parsed.excluded_attributes
    assert parsed.signature.nodes[0].operator == "AlignedSeriesScan"


def test_plan_hash_ignores_sibling_ascii_layout_order() -> None:
    parser = PlanParser()
    text = _text("device_view_2devices.txt")
    original = parser.parse(text, model="tree", kind=PlanKind.EXPLAIN)

    lines = text.splitlines()
    for index in range(6, 13):
        line = lines[index].ljust(89)
        lines[index] = line[45:] + " " + line[:44]
    swapped = parser.parse("\n".join(lines), model="tree", kind=PlanKind.EXPLAIN)

    assert swapped.status is PlanParseStatus.AVAILABLE
    assert swapped.signature_hash == original.signature_hash


def test_plan_hash_changes_for_semantic_difference() -> None:
    parser = PlanParser()
    asc = parser.parse(_text("limit_10.txt"), model="tree", kind=PlanKind.EXPLAIN)
    desc = parser.parse(
        _text("desc_limit_10.txt"), model="tree", kind=PlanKind.EXPLAIN
    )
    assert asc.signature_hash != desc.signature_hash


def test_stable_attributes_and_volatile_fields_are_separate() -> None:
    result = PlanParser().parse(
        _text("agg_max_time_last_value.txt"),
        model="tree",
        kind=PlanKind.EXPLAIN,
    )
    attributes = result.signature.nodes[0].attributes
    assert attributes["Aggregator-0"] == "LAST_VALUE, SINGLE"
    assert attributes["Aggregator-1"] == "MAX_TIME, SINGLE"
    assert attributes["ScanOrder"] == "DESC"
    assert "Partition" not in attributes
    assert result.excluded_attributes == ["Partition"]


def test_empty_explain_is_empty_not_available() -> None:
    result = PlanParser().parse("", model="tree", kind=PlanKind.EXPLAIN)
    assert result.status is PlanParseStatus.EMPTY
    assert result.signature_hash is None
    assert result.node_count == 0


def test_unrecognized_nonempty_text_is_unavailable() -> None:
    result = PlanParser().parse(
        "not a recognized plan", model="tree", kind=PlanKind.EXPLAIN
    )
    assert result.status is PlanParseStatus.UNAVAILABLE
    assert result.unavailable_reason is (
        PlanUnavailableReason.NO_RECOGNIZED_PLAN_NODES
    )


def test_explain_analyze_fixture_parses_without_runtime_values() -> None:
    parser = PlanParser()
    text = _text("explain_analyze_native.txt")
    result = parser.parse(text, model="tree", kind=PlanKind.EXPLAIN_ANALYZE)
    assert result.status is PlanParseStatus.AVAILABLE
    assert result.plan_kind is PlanKind.EXPLAIN_ANALYZE
    assert [node.operator for node in result.signature.nodes] == [
        "IdentitySinkNode",
        "AlignedSeriesScanNode",
    ]
    assert "PlanNodeId" in result.excluded_attributes
    assert all(not node.attributes for node in result.signature.nodes)

    changed = re.sub(r"PlanNodeId \d+", "PlanNodeId 999", text)
    changed = re.sub(r"\d+\.\d+ ms", "999.999 ms", changed)
    changed = re.sub(r"output: \d+ rows", "output: 999 rows", changed)
    changed = re.sub(r"Estimated Memory Size: : \d+", "Estimated Memory Size: : 1", changed)
    parsed = parser.parse(changed, model="tree", kind=PlanKind.EXPLAIN_ANALYZE)
    assert parsed.status is PlanParseStatus.AVAILABLE
    assert parsed.signature_hash == result.signature_hash


def test_plan_kind_mismatch_fails_closed() -> None:
    parser = PlanParser()
    analyze_text = _text("explain_analyze_native.txt")
    as_explain = parser.parse(
        analyze_text, model="tree", kind=PlanKind.EXPLAIN
    )
    assert as_explain.status is PlanParseStatus.ERROR
    assert as_explain.error_type == "PlanKindMismatch"

    ordinary = _text("native_scan.txt")
    as_analyze = parser.parse(
        ordinary, model="tree", kind=PlanKind.EXPLAIN_ANALYZE
    )
    assert as_analyze.status is PlanParseStatus.ERROR
    assert as_analyze.error_type == "PlanKindMismatch"


def test_table_model_fails_closed_without_verified_fixture() -> None:
    parser = PlanParser()
    empty = parser.parse("", model="table", kind=PlanKind.EXPLAIN)
    assert empty.status is PlanParseStatus.EMPTY
    assert empty.model == "table"

    nonempty = parser.parse("some table plan", model="table", kind=PlanKind.EXPLAIN)
    assert nonempty.status is PlanParseStatus.UNAVAILABLE
    assert nonempty.unavailable_reason is (
        PlanUnavailableReason.TABLE_MODEL_EXPLAIN_NOT_VERIFIED
    )
    assert nonempty.signature is None
    assert nonempty.signature_hash is None


def test_unknown_model_and_bad_rows_fail_closed() -> None:
    parser = PlanParser()
    unknown = parser.parse("whatever", model="object", kind=PlanKind.EXPLAIN)
    assert unknown.status is PlanParseStatus.UNAVAILABLE
    assert unknown.unavailable_reason is PlanUnavailableReason.UNKNOWN_DATABASE_MODEL

    bad_rows = parser.parse_rows(
        [["one", "two"]], model="tree", kind=PlanKind.EXPLAIN
    )
    assert bad_rows.status is PlanParseStatus.ERROR
    assert "expected 1" in (bad_rows.error_message or "")


def test_plan_comparison_uses_stable_hash_only() -> None:
    parser = PlanParser()
    first = parser.parse(_text("native_scan.txt"), model="tree", kind=PlanKind.EXPLAIN)
    second = parser.parse(_text("native_scan.txt"), model="tree", kind=PlanKind.EXPLAIN)
    other = parser.parse(_text("limit_10.txt"), model="tree", kind=PlanKind.EXPLAIN)
    empty = parser.parse("", model="tree", kind=PlanKind.EXPLAIN)

    assert compare_plan_results(first, second).status is PlanComparisonStatus.MATCHED
    assert compare_plan_results(first, other).status is (
        PlanComparisonStatus.MISMATCHED
    )
    assert compare_plan_results(first, empty).status is (
        PlanComparisonStatus.UNAVAILABLE
    )
    assert compare_plan_hashes(first.signature_hash, second.signature_hash).status is (
        PlanComparisonStatus.MATCHED
    )
    assert compare_plan_hashes(first, None).status is (
        PlanComparisonStatus.UNAVAILABLE
    )
