"""Integrity checks for the Phase-0 SQL and EXPLAIN fixtures.

These tests do not connect to IoTDB. They guard the checked-in evidence:
schema completeness, file/JSON consistency, empty-plan hazards, and accidental
credential or byte-string leakage.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SQL_DIR = FIXTURES / "sql"
PLAN_DIR = FIXTURES / "plans"
LAST_DIR = FIXTURES / "last"

WORKLOAD_CLASSIFICATIONS = {
    "SUPPORTED",
    "SUPPORTED_WITH_SHAPE_WARNING",
    "SUPPORTED_HEAVY_WILDCARD",
    "SUPPORTED_SILENT_EMPTY_WARNING",
    "SUPPORTED_CORRECTED_VARIANT",
}
EXECUTION_STATUSES = {"SUCCESS", "SUCCESS_EMPTY", "SUCCESS_LARGE"}
EXPLAIN_STATUSES = {"AVAILABLE", "EMPTY_PLAN"}

REQUIRED_WORKLOAD_FIELDS = {
    "case_id",
    "source_file",
    "database",
    "sql",
    "dialect",
    "classification",
    "server_acceptance",
    "execution_status",
    "row_count",
    "column_count",
    "columns",
    "result_digest_sha256",
    "rows_preview",
    "elapsed_ms",
    "explain_status",
    "explain_line_count",
    "notes",
}
REQUIRED_NEGATIVE_FIELDS = {
    "case_id",
    "sql",
    "dialect",
    "classification",
    "observed",
    "expected_optimizer_behavior",
    "notes",
}
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\b\s*[:=]\s*\S+"
)
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def workload() -> dict:
    return load_json(SQL_DIR / "workload_cases.json")


@pytest.fixture(scope="module")
def negative() -> dict:
    return load_json(SQL_DIR / "negative_cases.json")


@pytest.fixture(scope="module")
def plan_index() -> dict:
    return load_json(PLAN_DIR / "index.json")


@pytest.fixture(scope="module")
def last_summary() -> dict:
    return load_json(LAST_DIR / "last_certification_summary.json")


@pytest.fixture(scope="module")
def last_edge_summary() -> dict:
    return load_json(LAST_DIR / "last_edge_certification_summary.json")


@pytest.fixture(scope="module")
def last_recheck_summary() -> dict:
    return load_json(LAST_DIR / "last_readonly_recheck_20261101_summary.json")


def test_workload_fixture_metadata(workload: dict) -> None:
    assert workload["server_version"] == "2.0.6"
    assert workload["server_build_info"] == "c6ecbde"
    assert workload["dialect"] == "tree"
    assert workload["authentication"] == "local test account; credentials not stored"
    assert len(workload["cases"]) == 47
    assert set(workload["dataset_snapshot"]) == {
        "root.db200.g_0",
        "root.db800.g_0",
        "root.dbl.g_0",
        "root.dbm.g_0",
        "root.dbs.g_0",
        "root.test.g_0",
    }


def test_workload_cases_are_well_formed(workload: dict) -> None:
    case_ids = [case["case_id"] for case in workload["cases"]]
    assert len(case_ids) == len(set(case_ids)), "duplicate workload case_id"

    for case in workload["cases"]:
        missing = REQUIRED_WORKLOAD_FIELDS - set(case)
        assert not missing, f"{case['case_id']}: missing {sorted(missing)}"
        assert case["dialect"] == "tree"
        assert case["server_acceptance"] == "ACCEPTED"
        assert case["classification"] in WORKLOAD_CLASSIFICATIONS
        assert case["execution_status"] in EXECUTION_STATUSES
        assert case["explain_status"] in EXPLAIN_STATUSES
        assert case["sql"].strip(), f"{case['case_id']}: empty SQL"
        assert case["database"]
        assert case["source_file"].startswith(("MAPO/", "MATAOF/"))
        assert not case["source_file"].startswith("/")
        assert len(case["result_digest_sha256"]) == 64
        assert re.fullmatch(r"[0-9a-f]{64}", case["result_digest_sha256"])
        assert case["column_count"] == len(case["columns"])
        assert case["row_count"] >= 0
        assert len(case["rows_preview"]) <= case["row_count"]
        assert case["elapsed_ms"] >= 0
        if case["explain_status"] == "AVAILABLE":
            assert case["explain_line_count"] > 0
        else:
            assert case["explain_line_count"] == 0


def test_workload_sql_files_match_json(workload: dict) -> None:
    sql_dir = SQL_DIR / "workload"
    files = sorted(sql_dir.glob("*.sql"))
    assert len(files) == 47
    assert {path.stem for path in files} == {
        case["case_id"] for case in workload["cases"]
    }

    for case in workload["cases"]:
        path = sql_dir / f"{case['case_id']}.sql"
        assert path.exists(), f"missing SQL fixture: {path}"
        assert path.read_text(encoding="utf-8").strip() == case["sql"].strip()


def test_negative_cases_are_well_formed(negative: dict) -> None:
    assert negative["server_version"] == "2.0.6"
    assert len(negative["cases"]) == 24
    case_ids = [case["case_id"] for case in negative["cases"]]
    assert len(case_ids) == len(set(case_ids)), "duplicate negative case_id"

    for case in negative["cases"]:
        missing = REQUIRED_NEGATIVE_FIELDS - set(case)
        assert not missing, f"{case['case_id']}: missing {sorted(missing)}"
        assert case["sql"].strip()
        assert case["classification"]
        assert isinstance(case["observed"], dict) and case["observed"]
        assert case["expected_optimizer_behavior"].strip()
        assert case["notes"] == list(case["notes"])

    by_id = {case["case_id"]: case for case in negative["cases"]}
    assert by_id["union_q12"]["classification"] == "UNSUPPORTED_BY_SERVER"
    assert by_id["series_path_in_from"]["classification"] == "INVALID_SILENT_EMPTY"
    assert (
        by_id["union_to_single_range_not_equivalent"]["classification"]
        == "REWRITE_NOT_EQUIVALENT"
    )


def test_plan_index_and_text_fixtures(plan_index: dict) -> None:
    fixtures = plan_index["fixtures"]
    assert len(fixtures) == 13
    assert plan_index["server_version"] == "2.0.6"
    assert "do not hash raw text" in plan_index["note"].lower()

    for name, meta in fixtures.items():
        path = PLAN_DIR / meta["file"]
        assert path.exists(), f"{name}: missing {path}"
        if not meta["file"].endswith(".txt"):
            continue

        text = path.read_text(encoding="utf-8")
        assert text.strip(), f"{name}: empty plan text"
        assert not text.lstrip().startswith("b'"), f"{name}: byte-string wrapper"
        assert "b'" not in text, f"{name}: byte-string wrapper"
        assert not IPV4.search(text), f"{name}: raw IPv4 address"
        assert "PlanNodeId" not in text or name == "explain_analyze_native"
        assert len(text.splitlines()) == meta["line_count"]


def test_empty_plan_fixtures_are_explicitly_empty() -> None:
    for name in (
        "empty_plan_series_path.json",
        "empty_plan_wrong_device_path.json",
        "empty_plan_mavg.json",
    ):
        data = load_json(PLAN_DIR / name)
        assert set(data) == {"query", "observed", "source_probe"}
        assert data["query"].strip()
        assert "0 plan lines" in data["observed"]
        assert "0 rows" in data["observed"]


def test_last_certification_summary(last_summary: dict) -> None:
    assert last_summary["fixture_version"] == "last-cert-summary-v1"
    assert last_summary["server_version"] == "2.0.6"
    assert last_summary["server_build"] == "c6ecbde"
    assert last_summary["client_version"] == "2.0.6"
    assert last_summary["dialect"] == "tree"
    assert last_summary["password_in_output"] is False
    assert last_summary["connect_ok"] is True
    assert last_summary["close_ok"] is True
    assert last_summary["sweep_connect_ok"] is True
    assert last_summary["sweep_close_ok"] is True
    assert "read-only" in last_summary["scope"]
    assert "no DDL/DML" in last_summary["scope"]

    expected_types = {
        "float": "FLOAT",
        "double": "DOUBLE",
        "int64": "INT64",
        "int32": "INT32",
        "text": "TEXT",
        "boolean": "BOOLEAN",
    }
    assert set(last_summary["dataset"]) == set(expected_types)
    for name, data_type in expected_types.items():
        assert last_summary["dataset"][name]["data_type"] == data_type
        assert last_summary["dataset"][name]["point_count"] in {3_000, 10_000}
        assert last_summary["dataset"][name]["min_time"] < last_summary["dataset"][name]["max_time"]

    assert set(last_summary["semantic"]) == set(expected_types)
    for name in expected_types:
        item = last_summary["semantic"][name]
        assert item["data_type"] == expected_types[name]
        assert item["no_filter"]["pass"] is True
        assert item["before_max"]["pass"] is True
        for condition in ("no_filter", "before_max"):
            normalized = item[condition]["normalized"]
            assert normalized is not None
            assert normalized["last"][0] == normalized["native"][0]

    assert set(last_summary["sweep"]["cases"]) == set(expected_types)
    for name in expected_types:
        item = last_summary["sweep"]["cases"][name]
        assert item["sample_count"] == 200
        assert item["pass_count"] == 200
        assert item["fail_count"] == 0
        assert item["empty_pair_count"] == 1
        assert item["failure_examples"] == []

    assert len(last_summary["boundary"]) == 8
    assert all(item["pass"] for item in last_summary["boundary"].values())
    assert {item["reason"] for item in last_summary["boundary"].values()} == {
        "timestamp and normalized value match",
        "both empty",
    }

    negative = last_summary["negative"]
    assert set(negative) == {
        "multi_series",
        "multi_device",
        "value_filter",
        "wrong_device_path",
        "limit_not_one",
        "order_asc",
    }
    assert all(item["expected_decision"] == "REJECT_REWRITE" for item in negative.values())
    assert len(negative["multi_series"]["last_rows"]) == 2
    assert len(negative["multi_series"]["native_rows"]) == 1
    assert len(negative["multi_device"]["last_rows"]) == 2
    assert len(negative["multi_device"]["native_rows"]) == 1
    assert "701" in negative["value_filter"]["last_error"]
    assert negative["wrong_device_path"]["last_rows"] == []
    assert negative["wrong_device_path"]["native_rows"] == []
    assert len(negative["limit_not_one"]["native_rows"]) == 2
    assert len(negative["order_asc"]["native_rows"]) == 1

    explain = last_summary["explain"]
    assert explain["native"]["line_count"] == 8
    assert explain["last"]["line_count"] == 11
    assert explain["native"]["error"] is None
    assert explain["last"]["error"] is None
    assert any("ScanOrder: DESC" in row[-1] for row in explain["native"]["rows"])
    assert any("LastQuery" in row[-1] for row in explain["last"]["rows"])
    assert any("LastQueryScan" in row[-1] for row in explain["last"]["rows"])

    assert set(last_summary["performance"]) == set(expected_types)
    for name in expected_types:
        summary = last_summary["performance"][name]["summary"]
        assert summary["paired_n"] == 30
        assert summary["native"]["n"] == 30
        assert summary["last"]["n"] == 30
        assert summary["native"]["errors"] == 0
        assert summary["last"]["errors"] == 0
        assert summary["native_errors"] == 0
        assert summary["paired_diff_mean_ms"] > 0
        assert summary["paired_diff_ci95_mean_ms"][0] > 0
        assert summary["mean_relative_reduction_pct"] > 0

    fallback = last_summary["fallback_contract"]["cases"]
    assert fallback["valid"]["selected"] == "LAST"
    assert fallback["valid"]["fallback"] is False
    assert fallback["valid"]["native_run_count"] == 0
    for name in (
        "wrong_timestamp",
        "wrong_timeseries",
        "wrong_datatype",
        "wrong_value",
        "empty",
        "exception",
    ):
        assert fallback[name]["selected"] == "NATIVE_FALLBACK"
        assert fallback[name]["fallback"] is True
        assert fallback[name]["native_run_count"] == 1

    conclusion = last_summary["conclusion"]
    assert conclusion["strategy_id"] == "CAND_LAST_SINGLE_SERIES"
    assert conclusion["status"] == "NOT_VERIFIED"
    assert set(conclusion["submarkers"]) == {
        "EDGE_CASES_UNTESTED",
        "DEMAND_UNPROVEN",
        "FALLBACK_CONTRACT_ONLY",
    }
    assert len(last_summary["limitations"]) >= 8


def test_last_readonly_recheck_summary(last_recheck_summary: dict) -> None:
    summary = last_recheck_summary
    assert summary["fixture_version"] == "last-readonly-recheck-summary-v1"
    assert summary["server_version"] == "2.0.6"
    assert summary["server_build"] == "c6ecbde"
    assert summary["client_version"] == "2.0.6"
    assert summary["dialect"] == "tree"
    assert summary["read_only"] is True
    assert summary["ddl_dml"] is False
    assert summary["password_in_output"] is False
    assert summary["status"] == "NOT_VERIFIED"
    assert set(summary["submarkers"]) == {
        "DEMAND_UNPROVEN",
        "FALLBACK_CONTRACT_ONLY",
    }

    demand = summary["workload_demand_scan"]
    assert demand["files"] == 12
    assert demand["statements"] == 24_000
    assert demand["parse_supported"] == 12_000
    assert demand["parse_unsupported"] == 12_000
    assert demand["native_order_by_time_desc_limit_1"] == 0
    assert demand["eligible_single_series_single_device_latest_value"] == 0
    assert demand["select_last_statements"] == 2_000
    assert "DEMAND_UNPROVEN" in demand["conclusion"]

    parser_boundaries = summary["parser_boundaries"]
    assert parser_boundaries["last_baseline"]["parse_status"] == "UNSUPPORTED"
    assert "LAST_SYNTAX" in parser_boundaries["last_baseline"]["unsupported_constructs"]
    assert parser_boundaries["no_order_limit"]["parse_status"] == "SUCCESS"
    assert parser_boundaries["order_no_limit"]["parse_status"] == "SUCCESS"
    assert parser_boundaries["offset_zero"]["parse_status"] == "UNSUPPORTED"
    assert "OFFSET" in parser_boundaries["offset_zero"]["unsupported_constructs"]
    assert parser_boundaries["time_range"]["parse_status"] == "SUCCESS"
    assert parser_boundaries["wildcard"]["parse_status"] == "SUCCESS"

    variants = summary["variant_certification"]
    for name in ("baseline", "last_with_time_range", "last_with_time_eq"):
        assert variants[name]["normalized_equivalent"] is True
        assert variants[name]["same_result_shape"] is False
    assert variants["last_with_empty_range"]["both_empty"] is True
    assert variants["last_with_empty_range"]["normalized_equivalent"] is True
    assert variants["native_offset_zero"]["normalized_equivalent"] is True
    assert variants["native_offset_one"]["normalized_equivalent"] is False
    assert variants["native_no_order_limit_one"]["normalized_equivalent"] is False
    assert variants["last_wildcard"]["normalized_equivalent"] is False
    assert variants["last_value_filter"]["last_accepted"] is False

    contract = summary["result_contract"]
    assert contract["native_columns"][0] == "Time"
    assert contract["last_columns"] == ["Time", "Timeseries", "Value", "DataType"]
    assert contract["native_row"][0] == contract["last_row"][0]
    assert "Path can not start with root" in contract["last_qualified_series_error"]
    assert contract["last_value_row"][0] == 0
    assert contract["last_value_row"][1] == contract["native_row"][1]
    assert len(contract["max_time_last_value_columns"]) == 2

    benchmark = summary["benchmark"]
    assert set(benchmark["fresh_30_block"]) == {
        "float",
        "double",
        "int64",
        "int32",
        "text",
        "boolean",
    }
    for name in benchmark["expanded_120_block_seed_20261101"]:
        expanded = benchmark["expanded_120_block_seed_20261101"][name]
        replication = benchmark["replication_120_block_seed_20261102"][name]
        assert expanded["paired_n"] == 120
        assert replication["paired_n"] == 120
        assert expanded["errors"] == 0
        assert replication["errors"] == 0
        assert expanded["diff_mean_ms"] > 0
        assert replication["diff_mean_ms"] > 0
        assert expanded["diff_ci95_mean_ms"][0] > 0
        assert replication["diff_ci95_mean_ms"][0] > 0
    assert benchmark["fresh_30_block"]["text"]["diff_ci95_mean_ms"][0] < 0

    fallback = summary["fallback_contract"]["cases"]
    assert fallback["valid"]["selected"] == "LAST"
    assert fallback["valid"]["native_run_count"] == 0
    for name in (
        "wrong_timestamp",
        "wrong_timeseries",
        "wrong_datatype",
        "wrong_value",
        "empty",
        "exception",
    ):
        assert fallback[name]["selected"] == "NATIVE_FALLBACK"
        assert fallback[name]["native_run_count"] == 1

    conclusion = summary["conclusion"]
    assert conclusion["strategy_id"] == "CAND_LAST_SINGLE_SERIES"
    assert conclusion["status"] == "NOT_VERIFIED"
    assert conclusion["not_enabled"] is True


def test_last_edge_certification_summary(last_edge_summary: dict) -> None:
    assert last_edge_summary["fixture_version"] == "last-edge-cert-summary-v1"
    assert last_edge_summary["server_version"] == "2.0.6"
    assert last_edge_summary["server_build"] == "c6ecbde"
    assert last_edge_summary["client_version"] == "2.0.6"
    assert last_edge_summary["dialect"] == "tree"
    assert last_edge_summary["authorized_by_user"] is True
    assert last_edge_summary["password_in_output"] is False
    assert last_edge_summary["connect_ok"] is True
    assert last_edge_summary["close_ok"] is True
    assert "writable dedicated database" in last_edge_summary["scope"]
    assert "cleanup" in last_edge_summary["scope"]
    assert last_edge_summary["database"] == "root.last_cert_20261101"

    ddl = last_edge_summary["ddl"]
    assert len(ddl) == 9
    assert all(item["ok"] for item in ddl)
    assert ddl[0]["sql"] == "CREATE DATABASE root.last_cert_20261101"
    assert sum(1 for item in ddl if item["sql"].startswith("CREATE ALIGNED TIMESERIES")) == 8

    dml = last_edge_summary["dml"]
    assert len(dml) == 19
    assert all(item["ok"] for item in dml if item["label"] != "api_none_all_none_4000")
    all_none_dml = [item for item in dml if item["label"] == "api_none_all_none_4000"]
    assert len(all_none_dml) == 1
    assert all_none_dml[0]["ok"] is False
    assert all_none_dml[0]["error_type"] == "ValueError"

    case_summary = last_edge_summary["case_summary"]
    assert case_summary["total"] == 79
    assert case_summary["passed"] == 79
    assert case_summary["failed"] == 0
    assert case_summary["by_scenario"] == {
        "main_clean": {"total": 18, "passed": 18, "failed": 0},
        "missing_measurement": {"total": 32, "passed": 32, "failed": 0},
        "duplicate_timestamp": {"total": 3, "passed": 3, "failed": 0},
        "out_of_order": {"total": 4, "passed": 4, "failed": 0},
        "gap": {"total": 9, "passed": 9, "failed": 0},
        "empty_series": {"total": 3, "passed": 3, "failed": 0},
        "api_none": {"total": 6, "passed": 6, "failed": 0},
        "explicit_sql_null": {"total": 4, "passed": 4, "failed": 0},
    }
    assert last_edge_summary["edge_cases_status"] == "PASS"
    assert len(last_edge_summary["cases"]) == 79
    assert all(case["pass"] for case in last_edge_summary["cases"])
    assert all(
        case.get("normalized") is not None
        for case in last_edge_summary["cases"]
        if case["reason"] != "both empty"
    )

    observations = last_edge_summary["observations"]
    duplicate = observations["duplicate_timestamp"]
    assert duplicate["accepted_count"] == 2
    assert duplicate["behavior"] == "DUPLICATE_ACCEPTED_SINGLE_STORED_VALUE"
    assert duplicate["count"] == 1
    assert duplicate["raw_rows"] == [[1000, 2.0]]

    out_of_order = observations["out_of_order"]
    assert out_of_order["accepted_count"] == 3
    assert out_of_order["count"] == 3
    assert out_of_order["stored_timestamps"] == [1000, 2000, 3000]
    assert out_of_order["sorted_after_write"] is True
    assert out_of_order["raw_rows"] == [[1000, 100.0], [2000, 200.0], [3000, 300.0]]

    gap = observations["gap"]
    assert gap["accepted_count"] == 3
    assert gap["count"] == 3
    assert gap["raw_rows"] == [[1000, 10.0], [3000, 30.0], [5000, 50.0]]

    all_none = observations["api_all_none"]
    assert all_none["insert_ok"] is False
    assert all_none["error_type"] == "ValueError"
    assert all_none["counts_before"] == {"s_float": 2, "s_double": 2}
    assert all_none["counts_after"] == {"s_float": 2, "s_double": 2}
    assert all_none["counts_unchanged"] is True

    sql_null = observations["explicit_sql_null"]
    assert sql_null["accepted"] is True
    assert sql_null["s_float_count"] == 0
    assert sql_null["s_double_count"] == 1
    assert sql_null["s_float_rows"] == []
    assert sql_null["s_double_rows"] == [[1000, 2.5]]

    counts = last_edge_summary["counts"]
    assert counts["main"] == {
        "s_float": 3,
        "s_double": 3,
        "s_int32": 3,
        "s_int64": 3,
        "s_text": 3,
        "s_bool": 3,
    }
    assert counts["missing"] == {
        "s_float": 2,
        "s_double": 2,
        "s_int32": 1,
        "s_int64": 1,
        "s_text": 2,
        "s_bool": 1,
    }
    assert counts["api_none"] == {
        "s_float": 2,
        "s_double": 2,
        "s_int32": 0,
        "s_int64": 0,
        "s_text": 0,
        "s_bool": 0,
    }
    assert counts["gap"] == {"s_float": 3}
    assert counts["out_of_order"] == {"s_float": 3}
    assert counts["duplicate"] == {"s_float": 1}
    assert counts["absent"] == {"s_float": 0}
    assert counts["sql_null"] == {"s_float": 0, "s_double": 1}
    assert last_edge_summary["count_mismatches"] == []

    conclusion = last_edge_summary["conclusion"]
    assert conclusion["strategy_id"] == "CAND_LAST_SINGLE_SERIES"
    assert conclusion["status"] == "NOT_VERIFIED"
    assert conclusion["edge_case_coverage"] == "PASS"
    assert conclusion["submarkers"] == ["DEMAND_UNPROVEN", "FALLBACK_CONTRACT_ONLY"]
    assert len(last_edge_summary["limitations"]) >= 9
    limitations = " ".join(last_edge_summary["limitations"])
    assert "single host" in limitations
    assert "TTL" in limitations
    assert "production Executor" in limitations
    assert "workload" in limitations

    cleanup = last_edge_summary["cleanup"]
    assert len(cleanup["drop_attempts"]) == 1
    assert cleanup["drop_attempts"][0]["sql"] == "DROP DATABASE root.last_cert_20261101"
    assert cleanup["drop_attempts"][0]["ok"] is True
    assert cleanup["absent_after_drop"] is True
    assert cleanup["databases_unchanged_except_target"] is True
    assert "root.last_cert_20261101" not in cleanup["databases_after"]
    assert len(cleanup["databases_after"]) == 7


def test_fixtures_do_not_embed_credentials() -> None:
    candidates = [
        *SQL_DIR.glob("*.json"),
        *SQL_DIR.glob("workload/*.sql"),
        *PLAN_DIR.glob("*.json"),
        *PLAN_DIR.glob("*.txt"),
        *LAST_DIR.glob("*.json"),
    ]
    assert candidates
    for path in candidates:
        text = path.read_text(encoding="utf-8")
        assert not SECRET_ASSIGNMENT.search(text), f"possible credential in {path}"
