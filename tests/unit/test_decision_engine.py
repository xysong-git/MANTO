"""Agent 2 decision modes: deterministic, explainable, fail-closed."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from tsdb_optimizer.agents import QueryAnalysisAgent
from tsdb_optimizer.decision import (
    AdaptiveAdvice,
    CostSource,
    DecisionEngine,
    DecisionMode,
)
from tsdb_optimizer.errors import DecisionError, UnsafeSQLError
from tsdb_optimizer.schemas.common import utc_now
from tsdb_optimizer.schemas.monitoring import (
    MetricUnavailableReason,
    SystemSample,
    SystemSnapshot,
    SystemSourceType,
)
from tsdb_optimizer.strategies import (
    StrategyRegistry,
    build_default_registry,
)
from phase4_support import (
    PARSER_UNSUPPORTED_SQL,
    RaisingCostEstimator,
    SelfApprovingStrategyDouble,
    StaticCostEstimator,
    TEST_ONLY_STRATEGY_ID,
    TEST_SQL,
    PassThroughStrategyDouble,
    make_test_only_spec,
)

QUERY_ID = "q-decision"


def _analysis(sql: str = TEST_SQL, query_id: str = QUERY_ID):
    return QueryAnalysisAgent().analyze(sql, query_id=query_id)


def _decide(
    engine: DecisionEngine,
    *,
    mode: DecisionMode | str = DecisionMode.RULE_BASED,
    sql: str = TEST_SQL,
    query_id: str = QUERY_ID,
    server_version: str | None = "2.0.6",
    database_model: str | None = "tree",
    adaptive_advice: AdaptiveAdvice | None = None,
    system_state: SystemSample | None = None,
):
    analysis = _analysis(sql, query_id)
    return engine.decide(
        query_id=query_id,
        original_sql=sql,
        mode=mode,
        analysis=analysis,
        system_state=system_state,
        server_version=server_version,
        database_model=database_model,
        adaptive_advice=adaptive_advice,
    )


def _snapshot(
    *,
    source_type: SystemSourceType,
    host: str,
    captured_at=None,
    valid: bool = True,
    cpu_percent: float = 10.0,
    memory_percent: float = 20.0,
    disk_usage_percent: float = 30.0,
) -> SystemSnapshot:
    return SystemSnapshot(
        source_host=host,
        source_type=source_type,
        cpu_percent=cpu_percent,
        memory_percent=memory_percent,
        disk_usage_percent=disk_usage_percent,
        disk_read_bytes_per_sec=1.0,
        disk_write_bytes_per_sec=2.0,
        active_queries=0,
        valid=valid,
        captured_at=captured_at or utc_now(),
    )


def _sample(
    *,
    client_captured_at=None,
    client_valid: bool = True,
    client_cpu_percent: float = 10.0,
    db_captured_at=None,
    db_valid: bool = True,
    db_source_type: SystemSourceType = SystemSourceType.DB_HOST,
    db_cpu_percent: float = 10.0,
    db_metrics: SystemSnapshot | None = None,
) -> SystemSample:
    return SystemSample(
        client_host=_snapshot(
            source_type=SystemSourceType.CLIENT_HOST,
            host="client",
            captured_at=client_captured_at,
            valid=client_valid,
            cpu_percent=client_cpu_percent,
        ),
        db_host=_snapshot(
            source_type=db_source_type,
            host="db",
            captured_at=db_captured_at,
            valid=db_valid,
            cpu_percent=db_cpu_percent,
        ),
        db_metrics=db_metrics,
    )


def _unavailable_snapshot(
    *,
    source_type: SystemSourceType,
    host: str,
    captured_at=None,
) -> SystemSnapshot:
    fields = (
        "cpu_percent",
        "memory_percent",
        "disk_usage_percent",
        "disk_read_bytes_per_sec",
        "disk_write_bytes_per_sec",
        "active_queries",
    )
    return SystemSnapshot(
        source_host=host,
        source_type=source_type,
        captured_at=captured_at or utc_now(),
        valid=False,
        unavailable_fields={
            field: MetricUnavailableReason.NO_DB_HOST_AGENT for field in fields
        },
    )


def _fast_registry(
    plugin: PassThroughStrategyDouble | SelfApprovingStrategyDouble | None = None,
) -> StrategyRegistry:
    registry = build_default_registry(allow_uncertified_for_testing=True)
    registry.register(plugin or PassThroughStrategyDouble(spec=make_test_only_spec()))
    return registry


def _selected(decision) -> str:
    return decision.selected_strategy_id


def test_baseline_mode_selects_only_native_with_unknown_cost() -> None:
    decision = _decide(DecisionEngine(build_default_registry()), mode=DecisionMode.BASELINE)
    assert _selected(decision) == "S0_NATIVE"
    assert decision.decision_mode is DecisionMode.BASELINE
    assert decision.candidate_sql == TEST_SQL
    assert decision.cost_estimate.source is CostSource.UNKNOWN
    assert decision.expected_latency_ms is None
    assert decision.confidence is None
    assert decision.model_version is None
    assert [entry.strategy_id for entry in decision.considered] == [
        "S0_NATIVE",
        "S1_REPEAT_CACHE",
    ]
    assert [entry.strategy_id for entry in decision.considered if entry.selected] == [
        "S0_NATIVE"
    ]
    cache_entry = next(
        entry
        for entry in decision.considered
        if entry.strategy_id == "S1_REPEAT_CACHE"
    )
    assert cache_entry.applicable is False
    assert "dataset epoch" in (cache_entry.rejection_reason or "")
    assert "baseline: selected S0_NATIVE" in decision.reason
    assert "NO_VERIFIED_NON_NATIVE_STRATEGY" not in decision.warnings
    assert any(warning.startswith("COST_UNKNOWN") for warning in decision.warnings)
    assert decision.decision_overhead_ms >= 0.0
    assert decision.schema_version == 1
    assert decision.created_at.tzinfo is not None


def test_rule_based_default_registry_stays_on_native_and_explains_why() -> None:
    decision = _decide(DecisionEngine(build_default_registry()))
    assert _selected(decision) == "S0_NATIVE"
    assert decision.decision_mode is DecisionMode.RULE_BASED
    assert "no enabled non-Native strategy" in decision.reason
    assert "NO_VERIFIED_NON_NATIVE_STRATEGY" not in decision.warnings
    assert decision.query_occurrence == 1


def test_adaptive_without_advice_falls_back_to_native() -> None:
    decision = _decide(
        DecisionEngine(build_default_registry()),
        mode=DecisionMode.ADAPTIVE,
    )
    assert _selected(decision) == "S0_NATIVE"
    assert "ADAPTIVE_UNAVAILABLE_NO_ADVICE" in decision.warnings
    assert decision.model_version is None


def test_adaptive_rejects_unknown_advice_and_falls_back() -> None:
    advice = AdaptiveAdvice(
        strategy_id="NOT_REGISTERED",
        model_version="model-1",
        sample_count=10,
        reason="caller recommendation",
    )
    decision = _decide(
        DecisionEngine(build_default_registry()),
        mode=DecisionMode.ADAPTIVE,
        adaptive_advice=advice,
    )
    assert _selected(decision) == "S0_NATIVE"
    assert decision.model_version is None
    assert any(
        warning.startswith("ADAPTIVE_ADVICE_REJECTED") for warning in decision.warnings
    )


def test_unsupported_parser_sql_still_uses_native_passthrough() -> None:
    decision = _decide(
        DecisionEngine(build_default_registry()),
        mode=DecisionMode.BASELINE,
        sql=PARSER_UNSUPPORTED_SQL,
    )
    assert _selected(decision) == "S0_NATIVE"
    assert decision.candidate_sql == PARSER_UNSUPPORTED_SQL
    assert any("parser returned UNSUPPORTED" in warning for warning in decision.warnings)


def test_rule_based_selects_valid_faster_non_native_candidate() -> None:
    registry = _fast_registry()
    engine = DecisionEngine(
        registry,
        cost_estimator=StaticCostEstimator(
            {"S0_NATIVE": 10.0, TEST_ONLY_STRATEGY_ID: 5.0}
        ),
    )
    decision = _decide(engine)
    assert _selected(decision) == TEST_ONLY_STRATEGY_ID
    assert decision.expected_latency_ms == 5.0
    assert decision.cost_estimate.source is CostSource.HISTORICAL_STATISTIC
    assert decision.cost_estimate.sample_count == 3
    assert decision.model_version is None
    assert "historical median 5.000 ms is lower than baseline 10.000 ms" in (
        decision.reason
    )
    assert {entry.strategy_id for entry in decision.considered} == {
        "S0_NATIVE",
        "S1_REPEAT_CACHE",
        TEST_ONLY_STRATEGY_ID,
    }
    assert "NO_VERIFIED_NON_NATIVE_STRATEGY" not in decision.warnings


def test_baseline_mode_does_not_switch_even_when_non_native_is_cheaper() -> None:
    registry = _fast_registry()
    engine = DecisionEngine(
        registry,
        cost_estimator=StaticCostEstimator(
            {"S0_NATIVE": 10.0, TEST_ONLY_STRATEGY_ID: 1.0}
        ),
    )
    decision = _decide(engine, mode=DecisionMode.BASELINE)
    assert _selected(decision) == "S0_NATIVE"
    assert decision.expected_latency_ms == 10.0


def test_rule_based_keeps_native_when_candidate_cost_is_not_lower() -> None:
    registry = _fast_registry()
    engine = DecisionEngine(
        registry,
        cost_estimator=StaticCostEstimator(
            {"S0_NATIVE": 5.0, TEST_ONLY_STRATEGY_ID: 5.0}
        ),
    )
    decision = _decide(engine)
    assert _selected(decision) == "S0_NATIVE"
    assert "strictly lower" in decision.reason


def test_rule_based_refuses_ungrounded_switch_when_baseline_cost_is_unknown() -> None:
    registry = _fast_registry()
    engine = DecisionEngine(
        registry,
        cost_estimator=StaticCostEstimator({TEST_ONLY_STRATEGY_ID: 1.0}),
    )
    decision = _decide(engine)
    assert _selected(decision) == "S0_NATIVE"
    assert "BASELINE_COST_UNKNOWN" in decision.warnings


def test_rule_based_requires_non_native_cost_evidence() -> None:
    registry = _fast_registry()
    engine = DecisionEngine(
        registry,
        cost_estimator=StaticCostEstimator({"S0_NATIVE": 10.0}),
    )
    decision = _decide(engine)
    assert _selected(decision) == "S0_NATIVE"
    assert "NON_NATIVE_COST_UNKNOWN" in decision.warnings


def test_invalid_non_native_candidate_is_rejected_with_evidence() -> None:
    plugin = PassThroughStrategyDouble(
        spec=make_test_only_spec(),
        candidate_options={"unregistered_option": 1},
    )
    engine = DecisionEngine(
        _fast_registry(plugin),
        cost_estimator=StaticCostEstimator(
            {"S0_NATIVE": 10.0, TEST_ONLY_STRATEGY_ID: 1.0}
        ),
    )
    decision = _decide(engine)
    assert _selected(decision) == "S0_NATIVE"
    rejected = next(
        entry
        for entry in decision.considered
        if entry.strategy_id == TEST_ONLY_STRATEGY_ID
    )
    assert rejected.selected is False
    assert rejected.applicable is True
    assert rejected.validation is not None
    assert rejected.validation.passed is False
    assert rejected.rejection_reason and "execution_options" in rejected.rejection_reason


def test_self_approving_plugin_cannot_bypass_central_validation() -> None:
    plugin = SelfApprovingStrategyDouble(
        spec=make_test_only_spec(),
        candidate_options={"unregistered_option": 1},
    )
    engine = DecisionEngine(
        _fast_registry(plugin),
        cost_estimator=StaticCostEstimator(
            {"S0_NATIVE": 10.0, TEST_ONLY_STRATEGY_ID: 1.0}
        ),
    )
    decision = _decide(engine)
    assert _selected(decision) == "S0_NATIVE"
    rejected = next(
        entry
        for entry in decision.considered
        if entry.strategy_id == TEST_ONLY_STRATEGY_ID
    )
    assert rejected.selected is False
    assert rejected.validation is not None
    assert rejected.validation.passed is False
    assert rejected.rejection_reason and "execution_options" in rejected.rejection_reason
    assert any(
        check.name == "execution_options"
        and check.status.value == "FAILED"
        for check in rejected.validation.checks
    )


def test_non_applicable_candidate_is_rejected() -> None:
    plugin = PassThroughStrategyDouble(
        spec=make_test_only_spec(),
        applicable=False,
    )
    engine = DecisionEngine(
        build_default_registry(allow_uncertified_for_testing=True)
    )
    engine.registry.register(plugin)
    decision = _decide(engine)
    assert _selected(decision) == "S0_NATIVE"
    rejected = next(
        entry
        for entry in decision.considered
        if entry.strategy_id == TEST_ONLY_STRATEGY_ID
    )
    assert rejected.applicable is False
    assert rejected.candidate is None
    assert "not applicable" in (rejected.rejection_reason or "")


def test_plugin_exceptions_never_select_the_strategy() -> None:
    plugin = PassThroughStrategyDouble(
        spec=make_test_only_spec(),
        applicability_error=RuntimeError("applicability bug"),
    )
    engine = DecisionEngine(
        _fast_registry(plugin),
        cost_estimator=StaticCostEstimator(
            {"S0_NATIVE": 10.0, TEST_ONLY_STRATEGY_ID: 1.0}
        ),
    )
    decision = _decide(engine)
    assert _selected(decision) == "S0_NATIVE"
    rejected = next(
        entry
        for entry in decision.considered
        if entry.strategy_id == TEST_ONLY_STRATEGY_ID
    )
    assert "STRATEGY_APPLICABILITY_ERROR" in (rejected.rejection_reason or "")


def test_candidate_generation_and_validation_exceptions_fail_closed() -> None:
    build_plugin = PassThroughStrategyDouble(
        spec=make_test_only_spec("TEST_ONLY_BUILD"),
        build_error=RuntimeError("build bug"),
    )
    validate_plugin = PassThroughStrategyDouble(
        spec=make_test_only_spec("TEST_ONLY_VALIDATE"),
        validate_error=RuntimeError("validate bug"),
    )
    registry = _fast_registry(build_plugin)
    registry.register(validate_plugin)
    engine = DecisionEngine(
        registry,
        cost_estimator=StaticCostEstimator(
            {
                "S0_NATIVE": 10.0,
                "TEST_ONLY_BUILD": 1.0,
                "TEST_ONLY_VALIDATE": 1.0,
            }
        ),
    )
    decision = _decide(engine)
    assert _selected(decision) == "S0_NATIVE"
    rejections = {
        entry.strategy_id: entry.rejection_reason
        for entry in decision.considered
        if not entry.selected
    }
    assert "CANDIDATE_BUILD_ERROR" in (rejections["TEST_ONLY_BUILD"] or "")
    assert "VALIDATION_ERROR" in (rejections["TEST_ONLY_VALIDATE"] or "")


def test_cost_estimator_exception_is_unknown_not_invented() -> None:
    engine = DecisionEngine(_fast_registry(), cost_estimator=RaisingCostEstimator())
    decision = _decide(engine)
    assert _selected(decision) == "S0_NATIVE"
    for entry in decision.considered:
        if entry.strategy_id == "S1_REPEAT_CACHE":
            # Not applicable without a declared epoch; no cost is estimated.
            assert entry.applicable is False
            continue
        assert entry.cost.source is CostSource.UNKNOWN
        assert entry.cost.expected_latency_ms is None
        assert "COST_ESTIMATOR_ERROR" in (entry.cost.unknown_reason or "")
    assert any(
        "COST_ESTIMATOR_ERROR" in warning for warning in decision.warnings
    )


def test_repeat_cache_is_selected_only_after_the_first_exact_occurrence() -> None:
    epoch = "epoch-2026-11-01"
    engine = DecisionEngine(build_default_registry(cache_epoch=epoch))

    first = _decide(engine)
    assert first.selected_strategy_id == "S0_NATIVE"
    assert first.query_occurrence == 1
    assert "REPEAT_CACHE_REQUIRES_REPEAT" in first.warnings

    second = _decide(engine)
    assert second.selected_strategy_id == "S1_REPEAT_CACHE"
    assert second.query_occurrence == 2
    assert second.candidate_sql == TEST_SQL
    assert second.execution_options == {"cache_epoch": epoch}
    assert "REPEAT_CACHE_RULE_SELECTED" in second.warnings
    assert "occurrence 2" in second.reason
    assert "eligible only from occurrence 2 onward" in second.reason
    # The reason must not claim what occurrence 1 selected: a custom Registry
    # can select another non-Native strategy at occurrence 1.
    assert "first occurrence stayed Native" not in second.reason

    third = _decide(engine)
    assert third.selected_strategy_id == "S1_REPEAT_CACHE"
    assert third.query_occurrence == 3


def test_repeat_cache_occurrence_is_per_exact_sql() -> None:
    epoch = "epoch-2026-11-01"
    engine = DecisionEngine(build_default_registry(cache_epoch=epoch))
    other_sql = "SELECT s_131 FROM root.test.g_0.d_0"

    assert _decide(engine).selected_strategy_id == "S0_NATIVE"
    first_of_other = _decide(engine, sql=other_sql)
    assert first_of_other.query_occurrence == 1
    assert first_of_other.selected_strategy_id == "S0_NATIVE"

    second = _decide(engine)
    assert second.query_occurrence == 2
    assert second.selected_strategy_id == "S1_REPEAT_CACHE"
    second_other = _decide(engine, sql=other_sql)
    assert second_other.query_occurrence == 2
    assert second_other.selected_strategy_id == "S1_REPEAT_CACHE"


def test_reset_occurrence_tracking_returns_to_native() -> None:
    engine = DecisionEngine(
        build_default_registry(cache_epoch="epoch-2026-11-01")
    )
    assert _decide(engine).selected_strategy_id == "S0_NATIVE"
    assert _decide(engine).selected_strategy_id == "S1_REPEAT_CACHE"
    engine.reset_occurrence_tracking()
    reset = _decide(engine)
    assert reset.query_occurrence == 1
    assert reset.selected_strategy_id == "S0_NATIVE"


def test_baseline_mode_occurrence_still_counts_for_a_later_rule_decision() -> None:
    engine = DecisionEngine(
        build_default_registry(cache_epoch="epoch-2026-11-01")
    )
    baseline = _decide(engine, mode=DecisionMode.BASELINE)
    assert baseline.selected_strategy_id == "S0_NATIVE"
    assert baseline.query_occurrence == 1

    repeated = _decide(engine)
    assert repeated.query_occurrence == 2
    assert repeated.selected_strategy_id == "S1_REPEAT_CACHE"


def test_repeat_cache_rule_has_priority_over_historical_cost_evidence() -> None:
    epoch = "epoch-2026-11-01"
    registry = build_default_registry(
        allow_uncertified_for_testing=True, cache_epoch=epoch
    )
    registry.register(
        PassThroughStrategyDouble(spec=make_test_only_spec())
    )
    engine = DecisionEngine(
        registry,
        cost_estimator=StaticCostEstimator(
            {"S0_NATIVE": 10.0, TEST_ONLY_STRATEGY_ID: 1.0}
        ),
    )
    first = _decide(engine)
    assert first.query_occurrence == 1
    assert first.selected_strategy_id == TEST_ONLY_STRATEGY_ID
    repeated = _decide(engine)
    assert repeated.query_occurrence == 2
    assert repeated.selected_strategy_id == "S1_REPEAT_CACHE"
    assert "REPEAT_CACHE_RULE_SELECTED" in repeated.warnings
    # The first occurrence was TEST_ONLY_FAST, not Native.  The reason must
    # describe the repeat rule without making a false historical claim.
    assert "first occurrence stayed Native" not in repeated.reason


def test_failed_decision_after_reservation_does_not_consume_an_occurrence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch = "epoch-2026-11-01"
    engine = DecisionEngine(build_default_registry(cache_epoch=epoch))

    def fail_after_reservation(*_args, **_kwargs):
        # Called inside _occurrence_scope, i.e. after the occurrence has been
        # reserved.  The earlier fail-closed checks already passed, so this is
        # the failure shape the rollback logic exists for.
        raise DecisionError("injected selection failure after occurrence reservation")

    original_select = engine._select_rule_based
    monkeypatch.setattr(engine, "_select_rule_based", fail_after_reservation)
    with pytest.raises(DecisionError, match="injected selection failure"):
        _decide(engine, query_id="failed-after-reservation")
    monkeypatch.setattr(engine, "_select_rule_based", original_select)

    # The failed decision did not execute anything and must not make the next
    # successful decision look like a repeat.
    first_success = _decide(engine, query_id="first-success")
    assert first_success.query_occurrence == 1
    assert first_success.selected_strategy_id == "S0_NATIVE"
    assert "REPEAT_CACHE_REQUIRES_REPEAT" in first_success.warnings

    second_success = _decide(engine, query_id="second-success")
    assert second_success.query_occurrence == 2
    assert second_success.selected_strategy_id == "S1_REPEAT_CACHE"


def test_concurrent_failed_reservation_does_not_shift_the_first_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch = "epoch-2026-11-01"
    engine = DecisionEngine(build_default_registry(cache_epoch=epoch))
    original_select = engine._select_rule_based
    failing_thread_reserved = threading.Event()
    succeeding_thread_started = threading.Event()
    successes: list = []
    failures: list[BaseException] = []

    def select_with_injected_failure(*args, **kwargs):
        if threading.current_thread().name == "failing-decision":
            failing_thread_reserved.set()
            # Keep the reservation open while the other thread reaches the
            # occurrence scope.  If rollback were not protected by the same
            # lock, that thread would reserve occurrence 2 and be mislabeled.
            assert succeeding_thread_started.wait(timeout=5)
            raise DecisionError("injected concurrent failure after reservation")
        return original_select(*args, **kwargs)

    def failing_decide() -> None:
        try:
            _decide(engine, query_id="failing-thread")
        except BaseException as exc:  # noqa: BLE001 - asserted below
            failures.append(exc)

    def succeeding_decide() -> None:
        succeeding_thread_started.set()
        try:
            successes.append(_decide(engine, query_id="successful-thread"))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            failures.append(exc)

    monkeypatch.setattr(
        engine, "_select_rule_based", select_with_injected_failure
    )
    failing_thread = threading.Thread(target=failing_decide, name="failing-decision")
    succeeding_thread = threading.Thread(
        target=succeeding_decide, name="succeeding-decision"
    )
    failing_thread.start()
    assert failing_thread_reserved.wait(timeout=5)
    succeeding_thread.start()
    succeeding_thread.join(timeout=5)
    failing_thread.join(timeout=5)
    monkeypatch.setattr(engine, "_select_rule_based", original_select)

    assert not failing_thread.is_alive()
    assert not succeeding_thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], DecisionError)
    assert len(successes) == 1
    first_success = successes[0]
    assert first_success.query_occurrence == 1
    assert first_success.selected_strategy_id == "S0_NATIVE"
    assert "REPEAT_CACHE_REQUIRES_REPEAT" in first_success.warnings


def test_adaptive_advice_can_select_an_already_validated_candidate() -> None:
    registry = _fast_registry()
    advice = AdaptiveAdvice(
        strategy_id=TEST_ONLY_STRATEGY_ID,
        model_version="test-model-v1",
        sample_count=100,
        reason="test-only adaptive advice",
    )
    decision = _decide(
        DecisionEngine(registry),
        mode=DecisionMode.ADAPTIVE,
        adaptive_advice=advice,
    )
    assert _selected(decision) == TEST_ONLY_STRATEGY_ID
    assert decision.model_version == "test-model-v1"
    assert decision.decision_mode is DecisionMode.ADAPTIVE
    assert "caller-provided advice" in decision.reason
    assert "no Phase-5 learner was invoked" in decision.reason


def test_adaptive_repeat_cache_advice_waits_for_second_occurrence() -> None:
    epoch = "epoch-2026-11-01"
    engine = DecisionEngine(build_default_registry(cache_epoch=epoch))
    advice = AdaptiveAdvice(
        strategy_id="S1_REPEAT_CACHE",
        model_version="test-model-v1",
        sample_count=1,
        reason="test-only repeat-cache advice",
    )

    first = _decide(
        engine,
        mode=DecisionMode.ADAPTIVE,
        adaptive_advice=advice,
    )
    assert first.selected_strategy_id == "S0_NATIVE"
    assert first.query_occurrence == 1
    assert first.model_version is None
    assert any(
        warning.startswith("REPEAT_CACHE_REQUIRES_REPEAT")
        for warning in first.warnings
    )

    second = _decide(
        engine,
        mode=DecisionMode.ADAPTIVE,
        adaptive_advice=advice,
    )
    assert second.selected_strategy_id == "S1_REPEAT_CACHE"
    assert second.query_occurrence == 2
    assert second.model_version == "test-model-v1"
    assert second.execution_options == {"cache_epoch": epoch}


def test_adaptive_advice_cannot_select_a_non_applicable_candidate() -> None:
    plugin = PassThroughStrategyDouble(spec=make_test_only_spec(), applicable=False)
    registry = _fast_registry(plugin)
    advice = AdaptiveAdvice(
        strategy_id=TEST_ONLY_STRATEGY_ID,
        model_version="test-model-v1",
        sample_count=1,
        reason="bad advice",
    )
    decision = _decide(
        DecisionEngine(registry),
        mode=DecisionMode.ADAPTIVE,
        adaptive_advice=advice,
    )
    assert _selected(decision) == "S0_NATIVE"
    assert decision.model_version is None
    assert any(
        warning.startswith("ADAPTIVE_ADVICE_REJECTED") for warning in decision.warnings
    )


def test_modes_are_explainably_different_for_the_same_input() -> None:
    registry = _fast_registry()
    engine = DecisionEngine(
        registry,
        cost_estimator=StaticCostEstimator(
            {"S0_NATIVE": 10.0, TEST_ONLY_STRATEGY_ID: 1.0}
        ),
    )
    baseline = _decide(engine, mode=DecisionMode.BASELINE)
    rules = _decide(engine, mode=DecisionMode.RULE_BASED)
    assert baseline.selected_strategy_id == "S0_NATIVE"
    assert rules.selected_strategy_id == TEST_ONLY_STRATEGY_ID
    assert baseline.reason != rules.reason


def test_baseline_validation_failure_is_a_decision_error() -> None:
    with pytest.raises(DecisionError, match="baseline S0_NATIVE failed validation"):
        _decide(DecisionEngine(build_default_registry()), database_model="table")


def test_empty_enabled_registry_is_a_decision_error() -> None:
    registry = StrategyRegistry(allow_uncertified_for_testing=True)
    registry.register(
        PassThroughStrategyDouble(
            spec=make_test_only_spec(enabled=False),
        )
    )
    with pytest.raises(DecisionError, match="no enabled strategy"):
        _decide(DecisionEngine(registry))


def test_missing_baseline_is_a_decision_error() -> None:
    registry = StrategyRegistry(allow_uncertified_for_testing=True)
    registry.register(PassThroughStrategyDouble(spec=make_test_only_spec()))
    with pytest.raises(DecisionError, match="baseline S0_NATIVE is not enabled"):
        _decide(DecisionEngine(registry))


def test_analysis_must_describe_the_same_query() -> None:
    engine = DecisionEngine(build_default_registry())
    with pytest.raises(DecisionError, match="query_id"):
        engine.decide(
            query_id=QUERY_ID,
            original_sql=TEST_SQL,
            analysis=_analysis(query_id="different-query"),
        )
    with pytest.raises(DecisionError, match="different SQL"):
        engine.decide(
            query_id=QUERY_ID,
            original_sql=TEST_SQL,
            analysis=_analysis(f"{TEST_SQL} LIMIT 5", QUERY_ID),
        )


def _recording_registry():
    seen = []
    plugin = PassThroughStrategyDouble(spec=make_test_only_spec())
    original = plugin.is_applicable

    def record(features, state):
        seen.append(state)
        return original(features, state)

    plugin.is_applicable = record  # type: ignore[method-assign]
    return _fast_registry(plugin), seen


def test_stale_client_state_is_withheld_from_strategies() -> None:
    registry, seen = _recording_registry()
    stale = _sample(
        client_captured_at=utc_now() - timedelta(minutes=10)
    )

    decision = _decide(DecisionEngine(registry), system_state=stale)

    assert len(seen) == 1
    assert seen[0].client_host.valid is False
    assert seen[0].client_host.cpu_percent is None
    assert seen[0].db_host.valid is True
    assert seen[0].db_host.cpu_percent == 10.0
    assert any(
        warning.startswith("SYSTEM_STATE_STALE_DEGRADED")
        for warning in decision.warnings
    )


def test_invalid_client_state_is_withheld_from_strategies() -> None:
    registry, seen = _recording_registry()
    invalid = _sample(client_valid=False)

    decision = _decide(DecisionEngine(registry), system_state=invalid)

    assert len(seen) == 1
    assert seen[0].client_host.valid is False
    assert seen[0].client_host.cpu_percent is None
    assert seen[0].db_host.valid is True
    assert seen[0].db_host.cpu_percent == 10.0
    assert any(
        warning.startswith("SYSTEM_STATE_INVALID_DEGRADED")
        for warning in decision.warnings
    )


def test_stale_db_state_is_reported_but_client_state_is_still_used() -> None:
    registry, seen = _recording_registry()
    sample = _sample(db_captured_at=utc_now() - timedelta(minutes=10))

    decision = _decide(DecisionEngine(registry), system_state=sample)

    # The client-host snapshot is live and still usable, but every stale
    # DB-side value is replaced by an explicit unavailable marker.
    assert len(seen) == 1
    exposed = seen[0]
    assert exposed is not None
    assert exposed.client_host == sample.client_host
    assert exposed.db_host.valid is False
    assert exposed.db_host.cpu_percent is None
    assert exposed.db_host.memory_percent is None
    assert exposed.db_host.disk_usage_percent is None
    assert exposed.db_host.disk_read_bytes_per_sec is None
    assert exposed.db_host.disk_write_bytes_per_sec is None
    assert exposed.db_host.active_queries is None
    assert exposed.db_host.unavailable_fields
    assert set(exposed.db_host.unavailable_fields.values()) == {
        MetricUnavailableReason.STALE_SNAPSHOT
    }
    assert any(
        warning.startswith("DB_HOST_STATE_STALE_DEGRADED")
        for warning in decision.warnings
    )


def test_fresh_system_state_carries_no_degradation_warning() -> None:
    registry, seen = _recording_registry()
    sample = _sample()

    decision = _decide(DecisionEngine(registry), system_state=sample)

    assert seen == [sample]
    assert not any(
        "DEGRADED" in warning for warning in decision.warnings
    )


def test_rule_based_high_client_resources_force_native_on_a_repeat() -> None:
    epoch = "epoch-2026-11-02"
    low_engine = DecisionEngine(build_default_registry(cache_epoch=epoch))
    _decide(low_engine, system_state=_sample())
    low_repeat = _decide(low_engine, system_state=_sample())
    assert low_repeat.selected_strategy_id == "S1_REPEAT_CACHE"

    high_engine = DecisionEngine(build_default_registry(cache_epoch=epoch))
    high_first = _decide(
        high_engine,
        system_state=_sample(client_cpu_percent=95.0),
    )
    high_repeat = _decide(
        high_engine,
        system_state=_sample(client_cpu_percent=95.0),
    )

    assert high_first.query_occurrence == 1
    assert high_repeat.query_occurrence == 2
    assert high_first.selected_strategy_id == "S0_NATIVE"
    assert high_repeat.selected_strategy_id == "S0_NATIVE"
    assert "resource policy" in high_repeat.reason
    assert any(
        warning.startswith("RESOURCE_THRESHOLD_EXCEEDED")
        and "client_host.cpu_percent=95.000>90.000" in warning
        and "policy_version=" in warning
        for warning in high_repeat.warnings
    )
    cache_evaluation = next(
        entry
        for entry in high_repeat.considered
        if entry.strategy_id == "S1_REPEAT_CACHE"
    )
    assert cache_evaluation.selected is False
    assert "vetoed by resource policy" in (cache_evaluation.rejection_reason or "")


def test_baseline_mode_records_resource_pressure_without_switching() -> None:
    decision = _decide(
        DecisionEngine(build_default_registry()),
        mode=DecisionMode.BASELINE,
        system_state=_sample(client_cpu_percent=99.0),
    )

    assert decision.selected_strategy_id == "S0_NATIVE"
    assert "resource policy" in decision.reason
    assert any(
        warning.startswith("RESOURCE_THRESHOLD_EXCEEDED")
        and "client_host.cpu_percent=99.000>90.000" in warning
        for warning in decision.warnings
    )


def test_adaptive_advice_cannot_bypass_the_resource_gate() -> None:
    advice = AdaptiveAdvice(
        strategy_id=TEST_ONLY_STRATEGY_ID,
        model_version="test-model-v1",
        sample_count=100,
        reason="test-only adaptive advice",
    )
    decision = _decide(
        DecisionEngine(_fast_registry()),
        mode=DecisionMode.ADAPTIVE,
        adaptive_advice=advice,
        system_state=_sample(client_cpu_percent=99.0),
    )

    assert decision.selected_strategy_id == "S0_NATIVE"
    assert decision.model_version is None
    assert "caller advice cannot bypass" in decision.reason
    assert any(
        warning.startswith("RESOURCE_THRESHOLD_EXCEEDED")
        for warning in decision.warnings
    )
    rejected = next(
        entry
        for entry in decision.considered
        if entry.strategy_id == TEST_ONLY_STRATEGY_ID
    )
    assert rejected.selected is False
    assert "vetoed by resource policy" in (rejected.rejection_reason or "")


def test_high_db_host_resources_use_the_db_host_source_threshold() -> None:
    engine = DecisionEngine(
        _fast_registry(),
        cost_estimator=StaticCostEstimator(
            {"S0_NATIVE": 10.0, TEST_ONLY_STRATEGY_ID: 5.0}
        ),
    )
    # Without resource pressure this valid, cheaper candidate is selected.
    assert _decide(engine).selected_strategy_id == TEST_ONLY_STRATEGY_ID

    high_engine = DecisionEngine(
        _fast_registry(),
        cost_estimator=StaticCostEstimator(
            {"S0_NATIVE": 10.0, TEST_ONLY_STRATEGY_ID: 5.0}
        ),
    )
    decision = _decide(
        high_engine,
        system_state=_sample(db_cpu_percent=95.0),
    )

    assert decision.selected_strategy_id == "S0_NATIVE"
    assert any(
        "db_host.cpu_percent=95.000>90.000" in warning
        for warning in decision.warnings
    )
    assert not any(
        "client_host.cpu_percent" in warning
        for warning in decision.warnings
    )


def test_high_db_metrics_resources_use_the_db_metrics_source_threshold() -> None:
    db_metrics = _snapshot(
        source_type=SystemSourceType.DB_METRICS,
        host="db",
        valid=True,
        cpu_percent=95.0,
    )
    decision = _decide(
        DecisionEngine(_fast_registry()),
        mode=DecisionMode.ADAPTIVE,
        adaptive_advice=AdaptiveAdvice(
            strategy_id=TEST_ONLY_STRATEGY_ID,
            model_version="test-model-v1",
            reason="test-only adaptive advice",
        ),
        system_state=_sample(db_metrics=db_metrics),
    )

    assert decision.selected_strategy_id == "S0_NATIVE"
    assert any(
        "db_metrics.cpu_percent=95.000>90.000" in warning
        for warning in decision.warnings
    )


def test_invalid_db_host_values_are_withheld_and_do_not_trigger_threshold() -> None:
    registry, seen = _recording_registry()
    sample = _sample(db_valid=False, db_cpu_percent=99.0)

    decision = _decide(DecisionEngine(registry), system_state=sample)

    exposed = seen[0]
    assert exposed is not None
    assert exposed.client_host == sample.client_host
    assert exposed.db_host.valid is False
    assert exposed.db_host.cpu_percent is None
    assert exposed.db_host.memory_percent is None
    assert exposed.db_host.disk_usage_percent is None
    assert exposed.db_host.disk_read_bytes_per_sec is None
    assert exposed.db_host.disk_write_bytes_per_sec is None
    assert exposed.db_host.active_queries is None
    assert set(exposed.db_host.unavailable_fields.values()) == {
        MetricUnavailableReason.INVALID_VALUE
    }
    assert any(
        warning.startswith("DB_HOST_STATE_INVALID_DEGRADED")
        for warning in decision.warnings
    )
    assert not any(
        warning.startswith("RESOURCE_THRESHOLD_EXCEEDED")
        for warning in decision.warnings
    )


def test_invalid_db_metrics_values_are_withheld_and_do_not_trigger_threshold() -> None:
    registry, seen = _recording_registry()
    db_metrics = _snapshot(
        source_type=SystemSourceType.DB_METRICS,
        host="db",
        valid=False,
        cpu_percent=99.0,
    )
    sample = _sample(db_metrics=db_metrics)

    decision = _decide(DecisionEngine(registry), system_state=sample)

    exposed = seen[0]
    assert exposed is not None
    assert exposed.db_metrics is not None
    assert exposed.db_metrics.valid is False
    assert exposed.db_metrics.cpu_percent is None
    assert exposed.db_metrics.memory_percent is None
    assert exposed.db_metrics.disk_usage_percent is None
    assert set(exposed.db_metrics.unavailable_fields.values()) == {
        MetricUnavailableReason.INVALID_VALUE
    }
    assert any(
        warning.startswith("DB_METRICS_STATE_INVALID_DEGRADED")
        for warning in decision.warnings
    )
    assert not any(
        warning.startswith("RESOURCE_THRESHOLD_EXCEEDED")
        for warning in decision.warnings
    )


def test_unknown_db_source_with_values_is_withheld() -> None:
    registry, seen = _recording_registry()
    sample = _sample(
        db_valid=False,
        db_source_type=SystemSourceType.UNKNOWN,
        db_cpu_percent=99.0,
    )

    decision = _decide(DecisionEngine(registry), system_state=sample)

    exposed = seen[0]
    assert exposed is not None
    assert exposed.db_host.source_type is SystemSourceType.UNKNOWN
    assert exposed.db_host.valid is False
    assert exposed.db_host.cpu_percent is None
    assert any(
        warning.startswith("DB_HOST_STATE_SOURCE_MISMATCH_DEGRADED")
        for warning in decision.warnings
    )
    assert not any(
        warning.startswith("RESOURCE_THRESHOLD_EXCEEDED")
        for warning in decision.warnings
    )


def test_already_unavailable_db_snapshot_does_not_emit_degradation_noise() -> None:
    registry, seen = _recording_registry()
    client = _snapshot(
        source_type=SystemSourceType.CLIENT_HOST,
        host="client",
    )
    db_host = _unavailable_snapshot(
        source_type=SystemSourceType.DB_HOST,
        host="db",
    )
    sample = SystemSample(client_host=client, db_host=db_host)

    decision = _decide(DecisionEngine(registry), system_state=sample)

    assert seen == [sample]
    assert not any(
        warning.startswith("DB_HOST_STATE_")
        for warning in decision.warnings
    )


def test_unsafe_sql_and_unknown_mode_are_rejected() -> None:
    engine = DecisionEngine(build_default_registry())
    with pytest.raises(UnsafeSQLError):
        engine.decide(
            query_id=QUERY_ID,
            original_sql="DROP TABLE root.test",
        )
    with pytest.raises(DecisionError, match="unsupported decision mode"):
        engine.decide(
            query_id=QUERY_ID,
            original_sql=TEST_SQL,
            mode="llm_freeform",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize('mode', ['rule_based', 'adaptive'])
@pytest.mark.parametrize('bad_client', ['invalid', 'stale', 'future'])
@pytest.mark.parametrize('db_source', ['db_host', 'db_metrics'])
def test_bad_client_cannot_hide_high_db_pressure(mode, bad_client, db_source):
    sample = _sample()
    updates = {'valid': False} if bad_client == 'invalid' else {
        'captured_at': utc_now() + timedelta(minutes=10 if bad_client == 'future' else -10)
    }
    client = SystemSnapshot.model_validate({**sample.client_host.model_dump(round_trip=True), **updates})
    high = _snapshot(
        source_type=(SystemSourceType.DB_HOST if db_source == 'db_host'
                     else SystemSourceType.DB_METRICS),
        host='db', cpu_percent=99.0,
    )
    sample = SystemSample(
        client_host=client,
        db_host=high if db_source == 'db_host' else sample.db_host,
        db_metrics=high if db_source == 'db_metrics' else None,
    )
    engine = DecisionEngine(build_default_registry(cache_epoch='review-r1'))
    advice = AdaptiveAdvice(strategy_id='S1_REPEAT_CACHE', model_version='test',
                            sample_count=10, reason='test advice')
    for _ in range(2):
        decision = _decide(engine, mode=mode, system_state=sample, adaptive_advice=advice)
    assert decision.query_occurrence == 2
    assert decision.selected_strategy_id == 'S0_NATIVE'
    assert decision.model_version is None
    assert any('RESOURCE_THRESHOLD_EXCEEDED' in w and f'{db_source}.cpu_percent' in w
               for w in decision.warnings)
    assert any(f'SYSTEM_STATE_{bad_client.upper()}_DEGRADED' in w
               for w in decision.warnings)


@pytest.mark.parametrize('source', ['client_host', 'db_host', 'db_metrics'])
def test_future_source_is_withheld_without_mutating_other_sources(source):
    sample = _sample(db_metrics=_snapshot(source_type=SystemSourceType.DB_METRICS, host='db'))
    future = SystemSnapshot.model_validate({
        **getattr(sample, source).model_dump(round_trip=True),
        'captured_at': utc_now() + timedelta(days=1),
        'cpu_percent': 99.0,
    })
    sample = SystemSample.model_validate({**sample.model_dump(round_trip=True), source: future})
    registry, seen = _recording_registry()
    decision = _decide(DecisionEngine(registry), system_state=sample)
    exposed = getattr(seen[0], source)
    assert exposed.snapshot_id == future.snapshot_id
    assert exposed.captured_at == future.captured_at
    assert exposed.source_type == future.source_type
    assert not exposed.valid
    for field in ('cpu_percent', 'memory_percent', 'disk_usage_percent',
                  'disk_read_bytes_per_sec', 'disk_write_bytes_per_sec', 'active_queries'):
        assert getattr(exposed, field) is None
        assert exposed.unavailable_fields[field] is MetricUnavailableReason.FUTURE_SNAPSHOT
    for other in {'client_host', 'db_host', 'db_metrics'} - {source}:
        assert getattr(seen[0], other) == getattr(sample, other)
    assert getattr(sample, source).cpu_percent == 99.0
    assert any('FUTURE_DEGRADED' in w for w in decision.warnings)
    assert not any('RESOURCE_THRESHOLD_EXCEEDED' in w for w in decision.warnings)
