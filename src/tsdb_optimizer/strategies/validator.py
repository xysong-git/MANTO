"""Fail-closed validation for strategy candidates.

The validator performs no database execution: it checks the read-only SQL
guard, parser scope for rewritten SQL, version/model compatibility, execution
option ownership, and the declared plan-effect contract.  An unknown version or
model is reported explicitly; only the Native baseline may proceed without a
known server version because it performs no rewrite.
"""

from __future__ import annotations

from ..errors import ParserInternalError, UnsafeSQLError
from ..parsing import SQLParserAdapter
from ..schemas.query_features import ParseStatus
from ..sql_guard import validate_readonly_sql
from .models import (
    CandidatePlan,
    StrategySpec,
    ValidationCheck,
    ValidationCheckStatus,
    ValidationContext,
    ValidationResult,
)


class StrategyValidator:
    """Validate one candidate without contacting IoTDB."""

    def __init__(self, parser: SQLParserAdapter | None = None):
        self.parser = parser

    def validate(
        self,
        spec: StrategySpec,
        original_sql: str,
        candidate: CandidatePlan,
        context: ValidationContext,
    ) -> ValidationResult:
        checks: list[ValidationCheck] = []
        cleaned_original: str | None = None
        cleaned_candidate: str | None = None

        try:
            cleaned_original = validate_readonly_sql(original_sql)
            checks.append(
                ValidationCheck(
                    name="original_read_only",
                    status=ValidationCheckStatus.PASSED,
                    detail="original SQL is a single read-only SELECT",
                )
            )
        except UnsafeSQLError as exc:
            checks.append(
                ValidationCheck(
                    name="original_read_only",
                    status=ValidationCheckStatus.FAILED,
                    detail=str(exc),
                )
            )

        try:
            cleaned_candidate = validate_readonly_sql(candidate.candidate_sql)
            checks.append(
                ValidationCheck(
                    name="candidate_read_only",
                    status=ValidationCheckStatus.PASSED,
                    detail="candidate SQL is a single read-only SELECT",
                )
            )
        except UnsafeSQLError as exc:
            checks.append(
                ValidationCheck(
                    name="candidate_read_only",
                    status=ValidationCheckStatus.FAILED,
                    detail=str(exc),
                )
            )

        checks.append(
            self._check_identity(spec, candidate)
        )
        checks.append(self._check_version(spec, context))
        checks.append(self._check_database_model(spec, context))
        checks.append(self._check_execution_options(spec, candidate))
        checks.append(self._check_plan_effect(spec, candidate))
        checks.append(
            self._check_parser_scope(
                spec=spec,
                candidate=candidate,
                context=context,
                cleaned_original=cleaned_original,
                cleaned_candidate=cleaned_candidate,
            )
        )
        return ValidationResult.from_checks(checks)

    @staticmethod
    def _check_identity(
        spec: StrategySpec, candidate: CandidatePlan
    ) -> ValidationCheck:
        if candidate.strategy_id != spec.strategy_id:
            return ValidationCheck(
                name="strategy_identity",
                status=ValidationCheckStatus.FAILED,
                detail=(
                    f"candidate strategy_id {candidate.strategy_id!r} does not "
                    f"match registered spec {spec.strategy_id!r}"
                ),
            )
        if candidate.strategy_version != spec.version:
            return ValidationCheck(
                name="strategy_identity",
                status=ValidationCheckStatus.FAILED,
                detail=(
                    f"candidate strategy version {candidate.strategy_version!r} "
                    f"does not match registered version {spec.version!r}"
                ),
            )
        return ValidationCheck(
            name="strategy_identity",
            status=ValidationCheckStatus.PASSED,
            detail="candidate identity and version match the registry spec",
        )

    @staticmethod
    def _check_version(spec: StrategySpec, context: ValidationContext) -> ValidationCheck:
        if context.server_version is None:
            if spec.is_baseline:
                return ValidationCheck(
                    name="server_version_compatibility",
                    status=ValidationCheckStatus.WARNING,
                    detail=(
                        "server version is unknown; Native passthrough is allowed "
                        "without a version claim"
                    ),
                )
            return ValidationCheck(
                name="server_version_compatibility",
                status=ValidationCheckStatus.FAILED,
                detail=(
                    "server version is unknown; a non-Native strategy cannot be "
                    "certified compatible"
                ),
            )
        if context.server_version not in spec.compatible_server_versions:
            return ValidationCheck(
                name="server_version_compatibility",
                status=ValidationCheckStatus.FAILED,
                detail=(
                    f"server version {context.server_version!r} is not in "
                    f"verified versions {spec.compatible_server_versions}"
                ),
            )
        return ValidationCheck(
            name="server_version_compatibility",
            status=ValidationCheckStatus.PASSED,
            detail=f"server version {context.server_version!r} is verified",
        )

    @staticmethod
    def _check_database_model(
        spec: StrategySpec, context: ValidationContext
    ) -> ValidationCheck:
        if context.database_model is None:
            if spec.is_baseline:
                return ValidationCheck(
                    name="database_model_compatibility",
                    status=ValidationCheckStatus.WARNING,
                    detail=(
                        "database model is unknown; Native passthrough is allowed "
                        "without a model claim"
                    ),
                )
            return ValidationCheck(
                name="database_model_compatibility",
                status=ValidationCheckStatus.FAILED,
                detail=(
                    "database model is unknown; a non-Native strategy cannot be "
                    "certified compatible"
                ),
            )
        if spec.database_model != context.database_model:
            return ValidationCheck(
                name="database_model_compatibility",
                status=ValidationCheckStatus.FAILED,
                detail=(
                    f"strategy database model {spec.database_model!r} does not "
                    f"match observed model {context.database_model!r}"
                ),
            )
        return ValidationCheck(
            name="database_model_compatibility",
            status=ValidationCheckStatus.PASSED,
            detail=f"database model {context.database_model!r} matches the strategy",
        )

    @staticmethod
    def _check_execution_options(
        spec: StrategySpec, candidate: CandidatePlan
    ) -> ValidationCheck:
        unknown = sorted(set(candidate.execution_options) - set(spec.allowed_execution_options))
        if unknown:
            return ValidationCheck(
                name="execution_options",
                status=ValidationCheckStatus.FAILED,
                detail=f"unregistered execution options: {unknown}",
            )
        if not candidate.execution_options:
            return ValidationCheck(
                name="execution_options",
                status=ValidationCheckStatus.PASSED,
                detail="no execution options; only the verified native execution path is used",
            )
        # ``cache_epoch`` is the only execution option implemented by the
        # Phase-4 tree adapter.  It enables the process-local repeat cache and
        # is passed through the executor to the adapter.  Any other declared
        # option is still rejected: there is no proof that it is applied.
        unimplemented = sorted(
            set(candidate.execution_options) - {"cache_epoch"}
        )
        if unimplemented:
            return ValidationCheck(
                name="execution_options",
                status=ValidationCheckStatus.FAILED,
                detail=(
                    "execution options are not implemented by the Phase-4 "
                    f"executor: {unimplemented}; refusing to claim an unapplied "
                    "option"
                ),
            )
        epoch = candidate.execution_options.get("cache_epoch")
        if not isinstance(epoch, str) or not epoch.strip():
            return ValidationCheck(
                name="execution_options",
                status=ValidationCheckStatus.FAILED,
                detail="cache_epoch must be a non-blank string",
            )
        if len(epoch.strip()) > 200:
            return ValidationCheck(
                name="execution_options",
                status=ValidationCheckStatus.FAILED,
                detail="cache_epoch must be at most 200 characters",
            )
        return ValidationCheck(
            name="execution_options",
            status=ValidationCheckStatus.PASSED,
            detail=(
                "cache_epoch is implemented by the Phase-4 tree adapter as the "
                "process-local repeat-cache key and opt-in"
            ),
        )

    @staticmethod
    def _check_plan_effect(
        spec: StrategySpec, candidate: CandidatePlan
    ) -> ValidationCheck:
        declared = spec.plan_effect_expectation
        if candidate.expected_plan_effect is not None:
            if declared is None:
                return ValidationCheck(
                    name="plan_effect_contract",
                    status=ValidationCheckStatus.FAILED,
                    detail=(
                        "candidate predicts a plan effect but the registered spec "
                        "declares none"
                    ),
                )
            if candidate.expected_plan_effect != declared:
                return ValidationCheck(
                    name="plan_effect_contract",
                    status=ValidationCheckStatus.FAILED,
                    detail=(
                        "candidate plan-effect expectation does not match the "
                        "registered spec"
                    ),
                )
        if declared is not None:
            return ValidationCheck(
                name="plan_effect_contract",
                status=ValidationCheckStatus.FAILED,
                detail=(
                    "the Phase-4 validator has no verified plan-effect verifier; "
                    "a strategy that needs one cannot be selected"
                ),
            )
        return ValidationCheck(
            name="plan_effect_contract",
            status=ValidationCheckStatus.PASSED,
            detail="no plan effect is claimed; no EXPLAIN request is made",
        )

    def _check_parser_scope(
        self,
        *,
        spec: StrategySpec,
        candidate: CandidatePlan,
        context: ValidationContext,
        cleaned_original: str | None,
        cleaned_candidate: str | None,
    ) -> ValidationCheck:
        if cleaned_original is None or cleaned_candidate is None:
            return ValidationCheck(
                name="candidate_parser_scope",
                status=ValidationCheckStatus.FAILED,
                detail="candidate parser scope cannot be checked because SQL failed the guard",
            )
        if spec.is_baseline and cleaned_candidate != cleaned_original:
            return ValidationCheck(
                name="candidate_parser_scope",
                status=ValidationCheckStatus.FAILED,
                detail=(
                    "the Native baseline must execute the original SQL exactly; "
                    "the candidate differs from the original"
                ),
            )
        if cleaned_candidate == cleaned_original:
            if spec.is_baseline:
                if context.analysis is None:
                    return ValidationCheck(
                        name="candidate_parser_scope",
                        status=ValidationCheckStatus.WARNING,
                        detail=(
                            "native baseline has no Agent-1 analysis; server "
                            "remains the authority and no rewrite is attempted"
                        ),
                    )
                if context.analysis.parse_status is ParseStatus.SUCCESS:
                    return ValidationCheck(
                        name="candidate_parser_scope",
                        status=ValidationCheckStatus.PASSED,
                        detail=(
                            "native baseline SQL is within the fixture-verified "
                            "parser subset"
                        ),
                    )
                return ValidationCheck(
                    name="candidate_parser_scope",
                    status=ValidationCheckStatus.WARNING,
                    detail=(
                        "native baseline SQL is outside the fixture-verified "
                        "parser subset; no rewrite is attempted"
                    ),
                )
            # A non-baseline candidate that executes the original cleaned SQL
            # exactly does not need parser coverage.  There is no rewrite whose
            # semantics the parser would have to prove: the server remains the
            # authority, and the strategy-specific guards still apply.  This is
            # the explicit EXACT_SQL_NO_REWRITE policy used by S1.
            return ValidationCheck(
                name="candidate_parser_scope",
                status=ValidationCheckStatus.PASSED,
                detail=(
                    "EXACT_SQL_NO_REWRITE: non-baseline candidate executes the "
                    "original cleaned SQL exactly; parser coverage is not "
                    "required because no rewrite is attempted and the server "
                    "remains the semantic authority"
                ),
            )
        if self.parser is None:
            return ValidationCheck(
                name="candidate_parser_scope",
                status=ValidationCheckStatus.FAILED,
                detail=(
                    "rewritten candidate SQL requires a parser, but no parser is "
                    "configured; refusing to execute an unparsed rewrite"
                ),
            )
        try:
            parsed = self.parser.parse(candidate.candidate_sql)
        except ParserInternalError as exc:
            return ValidationCheck(
                name="candidate_parser_scope",
                status=ValidationCheckStatus.FAILED,
                detail=f"candidate parser failed closed: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 - parser failures must fail closed
            return ValidationCheck(
                name="candidate_parser_scope",
                status=ValidationCheckStatus.FAILED,
                detail=f"candidate parser raised {type(exc).__name__}: {exc}",
            )
        if parsed.parse_status is not ParseStatus.SUCCESS:
            return ValidationCheck(
                name="candidate_parser_scope",
                status=ValidationCheckStatus.FAILED,
                detail=(
                    "rewritten candidate SQL is outside the fixture-verified parser "
                    f"subset ({parsed.failure_reason})"
                ),
            )
        return ValidationCheck(
            name="candidate_parser_scope",
            status=ValidationCheckStatus.PASSED,
            detail="rewritten candidate SQL parses within the fixture-verified subset",
        )
