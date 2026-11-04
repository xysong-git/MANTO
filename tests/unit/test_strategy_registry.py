"""StrategySpec and StrategyRegistry fail-closed behavior."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tsdb_optimizer.errors import StrategyCatalogError, StrategyRegistryError
from tsdb_optimizer.strategies import (
    CandidatePlan,
    NativeStrategy,
    StrategyCatalog,
    StrategyRegistry,
    StrategyStatus,
    StrategyCertification,
    ValidationContext,
    build_default_registry,
    load_strategy_catalog,
    native_strategy_spec,
)
from phase4_support import (
    TEST_ONLY_STRATEGY_ID,
    TEST_SQL,
    PassThroughStrategyDouble,
    make_test_only_spec,
)


def _registry_with_native() -> StrategyRegistry:
    registry = StrategyRegistry(allow_uncertified_for_testing=True)
    registry.register(NativeStrategy())
    return registry


def test_default_registry_contains_native_and_repeat_cache_only() -> None:
    registry = build_default_registry()
    assert registry.catalog is not None
    assert [entry.strategy_id for entry in registry.catalog.enabled_non_baseline] == [
        "S1_REPEAT_CACHE"
    ]
    assert registry.selectable_ids() == ("S0_NATIVE", "S1_REPEAT_CACHE")
    assert registry.baseline.spec.strategy_id == "S0_NATIVE"
    assert registry.baseline.spec.verification_status is StrategyStatus.VERIFIED
    assert len(registry.all_entries()) == 2


def test_external_catalog_enables_one_baseline_and_the_repeat_cache() -> None:
    catalog = load_strategy_catalog()
    enabled = [entry for entry in catalog.strategies if entry.enabled]
    assert [entry.strategy_id for entry in enabled] == [
        "S0_NATIVE",
        "S1_REPEAT_CACHE",
    ]
    assert enabled[0].is_baseline is True
    assert enabled[0].implementation_class == (
        "tsdb_optimizer.strategies.native.NativeStrategy"
    )
    assert enabled[1].is_baseline is False
    assert enabled[1].implementation_class == (
        "tsdb_optimizer.strategies.repeat_cache.RepeatCacheStrategy"
    )
    assert catalog.certification_for("CAND_LAST_SINGLE_SERIES") is not None
    assert catalog.certification_for("CAND_LAST_SINGLE_SERIES").enabled is False


def test_registry_requires_a_catalog_unless_test_opt_out_is_explicit() -> None:
    with pytest.raises(StrategyRegistryError, match="certification catalog"):
        StrategyRegistry()


def test_registry_rejects_a_strategy_absent_from_the_catalog() -> None:
    registry = StrategyRegistry(catalog=load_strategy_catalog())
    with pytest.raises(StrategyRegistryError, match="not in the external"):
        registry.register(
            PassThroughStrategyDouble(spec=make_test_only_spec())
        )


def test_registry_rejects_a_catalog_version_mismatch() -> None:
    spec = native_strategy_spec().model_copy(update={"version": "9.9.9"})
    registry = StrategyRegistry(catalog=load_strategy_catalog())
    with pytest.raises(
        StrategyRegistryError,
        match=r"certification catalog: \['version'\]",
    ):
        registry.register(NativeStrategy(spec=spec))


def _baseline_certification(*, implementation_class: str) -> StrategyCertification:
    return StrategyCertification(
        strategy_id="S0_NATIVE",
        version="0.1.0",
        database_model="tree",
        compatible_server_versions=["2.0.6"],
        enabled=True,
        verification_status=StrategyStatus.VERIFIED,
        is_baseline=True,
        implementation_class=implementation_class,
        allowed_execution_options=[],
        reason="test catalog baseline",
    )


def test_registry_rejects_a_plugin_that_merely_claims_the_certified_class() -> None:
    # The spec string matches the catalog, but the actual plugin class does
    # not.  Trusting the metadata alone would let any implementation pass.
    spec = native_strategy_spec().model_copy(
        update={"implementation_class": "tests.FakeCertifiedStrategy"}
    )
    catalog = StrategyCatalog(
        schema_version=1,
        strategies=(
            _baseline_certification(
                implementation_class="tests.FakeCertifiedStrategy"
            ),
        ),
    )
    registry = StrategyRegistry(catalog=catalog)
    with pytest.raises(StrategyRegistryError, match="implementation class"):
        registry.register(NativeStrategy(spec=spec))
    assert registry.selectable_ids() == ()


def test_builder_rejects_an_enabled_catalog_entry_without_an_implementation() -> None:
    candidate = StrategyCertification(
        strategy_id="CAND_CATALOG_ONLY",
        version="0.1.0",
        database_model="tree",
        compatible_server_versions=["2.0.6"],
        enabled=True,
        verification_status=StrategyStatus.VERIFIED,
        implementation_class="tests.FakeStrategy",
        allowed_execution_options=[],
        proof_entry="entry",
        proof_correctness="correctness",
        proof_effect="effect",
        proof_benefit="benefit",
        proof_fallback="fallback",
        reason="test catalog candidate",
    )
    catalog = StrategyCatalog(
        schema_version=1,
        strategies=(
            _baseline_certification(
                implementation_class=(
                    "tsdb_optimizer.strategies.native.NativeStrategy"
                )
            ),
            candidate,
        ),
    )
    with pytest.raises(
        StrategyRegistryError,
        match=r"missing=\['CAND_CATALOG_ONLY'\]",
    ):
        build_default_registry(catalog=catalog)


def test_spec_applicability_rules_are_read_only_and_serializable() -> None:
    spec = native_strategy_spec()
    with pytest.raises(TypeError):
        spec.applicability_rules["new"] = "mutation"
    assert spec.model_dump()["applicability_rules"] == {
        "sql": "single, read-only SELECT",
        "rewrite": "none",
        "execution_count": "exactly one request per attempt",
    }


def test_registry_explicit_test_opt_out_still_works() -> None:
    registry = StrategyRegistry(allow_uncertified_for_testing=True)
    registry.register(PassThroughStrategyDouble(spec=make_test_only_spec()))
    assert TEST_ONLY_STRATEGY_ID in registry.selectable_ids()


def test_missing_catalog_path_fails_closed(tmp_path) -> None:
    with pytest.raises(StrategyCatalogError, match="could not read"):
        build_default_registry(catalog_path=tmp_path / "missing.yaml")


def test_catalog_rejects_enabled_non_native_without_five_proofs() -> None:
    with pytest.raises(ValidationError, match="five proofs"):
        StrategyCertification(
            strategy_id="CAND_TEST",
            version="0.1.0",
            database_model="tree",
            compatible_server_versions=["2.0.6"],
            enabled=True,
            verification_status=StrategyStatus.VERIFIED,
            implementation_class="tests.FakeStrategy",
            allowed_execution_options=[],
            proof_entry="entry",
            proof_correctness="correctness",
            proof_effect="effect",
            proof_benefit="benefit",
            reason="proof_fallback deliberately omitted",
        )


def test_catalog_rejects_disabled_baseline() -> None:
    with pytest.raises(ValidationError, match="baseline entry must be enabled"):
        StrategyCertification(
            strategy_id="S0_NATIVE",
            version="0.1.0",
            database_model="tree",
            compatible_server_versions=["2.0.6"],
            enabled=False,
            verification_status=StrategyStatus.NOT_VERIFIED,
            is_baseline=True,
            implementation_class="tests.FakeStrategy",
            allowed_execution_options=[],
            reason="invalid disabled baseline",
        )


def test_catalog_invalid_yaml_fails_closed(tmp_path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("schema_version: [", encoding="utf-8")
    with pytest.raises(StrategyCatalogError, match="valid YAML"):
        load_strategy_catalog(path)


def test_default_registry_wires_a_parser_for_candidate_validation() -> None:
    registry = build_default_registry()
    assert registry.validator.parser is not None
    assert registry.validator.parser.parse(TEST_SQL).parse_status.value == "SUCCESS"


def test_explicit_parser_is_not_replaced() -> None:
    sentinel = object()
    registry = build_default_registry(parser=sentinel)  # type: ignore[arg-type]
    assert registry.validator.parser is sentinel


def test_enabled_unverified_spec_is_rejected_at_model_construction() -> None:
    with pytest.raises(ValidationError, match="VERIFIED"):
        make_test_only_spec(
            enabled=True,
            verification_status=StrategyStatus.NOT_VERIFIED,
        )


def test_registry_rejects_enabled_unverified_spec_even_if_model_is_forged() -> None:
    spec = make_test_only_spec().model_copy(
        update={
            "enabled": True,
            "verification_status": StrategyStatus.NOT_VERIFIED,
        }
    )
    plugin = PassThroughStrategyDouble(spec=spec)
    registry = _registry_with_native()
    with pytest.raises(StrategyRegistryError, match="unverified"):
        registry.register(plugin)


def test_disabled_unverified_strategy_is_stored_but_never_selectable() -> None:
    spec = make_test_only_spec(
        enabled=False,
        verification_status=StrategyStatus.NOT_VERIFIED,
    )
    plugin = PassThroughStrategyDouble(spec=spec)
    registry = _registry_with_native()
    registry.register(plugin)

    assert TEST_ONLY_STRATEGY_ID in {
        entry.spec.strategy_id for entry in registry.all_entries()
    }
    assert TEST_ONLY_STRATEGY_ID not in registry.selectable_ids()
    with pytest.raises(StrategyRegistryError, match="disabled"):
        registry.get_enabled(TEST_ONLY_STRATEGY_ID)
    with pytest.raises(StrategyRegistryError, match="disabled"):
        registry.assert_selectable(TEST_ONLY_STRATEGY_ID)


def test_registry_rejects_duplicate_strategy_id() -> None:
    registry = _registry_with_native()
    with pytest.raises(StrategyRegistryError, match="already registered"):
        registry.register(NativeStrategy())


def test_registry_rejects_supplied_spec_with_different_plugin_id() -> None:
    plugin = PassThroughStrategyDouble(spec=make_test_only_spec("TEST_ONLY_A"))
    registry = _registry_with_native()
    with pytest.raises(StrategyRegistryError, match="does not match"):
        registry.register(plugin, spec=make_test_only_spec("TEST_ONLY_B"))


def test_registry_rejects_same_id_spec_forged_from_a_disabled_plugin_spec() -> None:
    plugin_spec = make_test_only_spec(
        enabled=False,
        verification_status=StrategyStatus.NOT_VERIFIED,
    )
    plugin = PassThroughStrategyDouble(spec=plugin_spec)
    forged = plugin_spec.model_copy(
        update={"enabled": True, "verification_status": StrategyStatus.VERIFIED}
    )
    registry = _registry_with_native()
    with pytest.raises(StrategyRegistryError, match="does not match"):
        registry.register(plugin, spec=forged)
    assert TEST_ONLY_STRATEGY_ID not in registry.selectable_ids()


def test_registered_spec_cannot_be_mutated_after_registration() -> None:
    plugin = PassThroughStrategyDouble(
        spec=make_test_only_spec(
            enabled=False,
            verification_status=StrategyStatus.NOT_VERIFIED,
        )
    )
    registry = _registry_with_native()
    registry.register(plugin)
    with pytest.raises(ValidationError):
        plugin.spec.enabled = True
    assert TEST_ONLY_STRATEGY_ID not in registry.selectable_ids()


def test_central_validation_rejects_spec_replaced_after_registration() -> None:
    plugin = PassThroughStrategyDouble(spec=make_test_only_spec())
    registry = _registry_with_native()
    entry = registry.register(plugin)
    plugin.spec = make_test_only_spec(version="9.9.9")
    candidate = CandidatePlan(
        strategy_id=TEST_ONLY_STRATEGY_ID,
        strategy_version="0.0.1",
        candidate_sql=TEST_SQL,
    )
    with pytest.raises(StrategyRegistryError, match="no longer matches"):
        registry.validate_candidate(
            entry,
            TEST_SQL,
            candidate,
            ValidationContext(query_id="q-registry-central-check"),
        )


def test_registry_rejects_baseline_with_another_strategy_id() -> None:
    spec = make_test_only_spec("TEST_ONLY_BASELINE", is_baseline=True)
    plugin = PassThroughStrategyDouble(spec=spec)
    registry = _registry_with_native()
    with pytest.raises(StrategyRegistryError, match="baseline must be"):
        registry.register(plugin)


def test_registry_requires_the_enabled_baseline() -> None:
    registry = StrategyRegistry(allow_uncertified_for_testing=True)
    with pytest.raises(StrategyRegistryError, match="not registered"):
        registry.baseline

    spec = make_test_only_spec(TEST_ONLY_STRATEGY_ID)
    registry.register(PassThroughStrategyDouble(spec=spec))
    assert registry.enabled_entries()[0].spec.strategy_id == TEST_ONLY_STRATEGY_ID
    # The configured baseline ID is still absent, so the registry must fail
    # closed rather than silently choosing a non-baseline strategy.
    with pytest.raises(StrategyRegistryError, match="not registered"):
        registry.baseline


def test_registry_get_missing_strategy_fails() -> None:
    with pytest.raises(StrategyRegistryError, match="not registered"):
        build_default_registry().get("CAND_UNKNOWN")
