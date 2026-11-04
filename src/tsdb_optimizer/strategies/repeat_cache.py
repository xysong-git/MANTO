"""S1_REPEAT_CACHE: fixed-epoch, process-local exact-SQL result cache.

This is an execution-level strategy, not a SQL rewrite.  It is applicable only
when the caller supplies a non-blank dataset epoch and the query is inside a
conservative deterministic token subset.  A cache miss executes the cleaned SQL
exactly once; a cache hit is served by the adapter without dispatching a
request to IoTDB.

There is no server-side invalidation or data-version API in the verified 2.0.6
environment.  The strategy therefore does not claim general freshness: the
caller's epoch is the correctness boundary.  Unknown functions, string
literals, placeholders, joins, unions and other tokens outside the subset are
rejected rather than guessed.

Agent-1 parser coverage is not a prerequisite: S1 never rewrites SQL, so an
UNSUPPORTED parse status does not make the exact cleaned SQL unsafe to cache.
The read-only guard and the deterministic token subset are the cache safety
boundary; the server remains the semantic authority for the executed SQL.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot.tokens import TokenType, Tokenizer

from ..cache_epoch import normalize_cache_epoch
from ..errors import UnsafeSQLError
from ..schemas.monitoring import SystemSample
from ..schemas.query_features import ParseStatus, QueryAnalysisResult
from ..sql_guard import validate_readonly_sql
from .models import (
    ApplicabilityResult,
    CandidateContext,
    CandidatePlan,
    SelectionPolicy,
    StrategySpec,
    StrategyStatus,
    ValidationCheck,
    ValidationCheckStatus,
    ValidationContext,
    ValidationResult,
)
from .validator import StrategyValidator

CACHE_STRATEGY_ID = "S1_REPEAT_CACHE"
CACHE_EPOCH_OPTION = "cache_epoch"

# Conservative SQL token subset.  It contains only tokens observed in the
# fixture-verified `query/` workload plus deterministic scalar operators.  A
# token outside this set (including PLACEHOLDER and STRING) makes the statement
# non-cacheable; it remains executable as Native SQL.
_ALLOWED_TOKENS = frozenset(
    {
        TokenType.SELECT,
        TokenType.FROM,
        TokenType.WHERE,
        TokenType.AND,
        TokenType.OR,
        TokenType.GROUP_BY,
        TokenType.ORDER_BY,
        TokenType.ASC,
        TokenType.DESC,
        TokenType.LIMIT,
        TokenType.OFFSET,
        TokenType.COMMA,
        TokenType.DOT,
        TokenType.L_PAREN,
        TokenType.R_PAREN,
        TokenType.L_BRACKET,
        TokenType.R_BRACKET,
        TokenType.STAR,
        TokenType.EQ,
        TokenType.NEQ,
        TokenType.GT,
        TokenType.GTE,
        TokenType.LT,
        TokenType.LTE,
        TokenType.PLUS,
        TokenType.DASH,
        TokenType.SLASH,
        TokenType.NUMBER,
        TokenType.VAR,
        TokenType.TIME,
        TokenType.SEMICOLON,
        TokenType.NULL,
        TokenType.TRUE,
        TokenType.FALSE,
    }
)

# Function names followed by '(' that have deterministic aggregate semantics
# for a fixed dataset epoch.  This allowlist is intentionally small.  It can be
# extended only after a real 2.0.6 semantic check, not from documentation alone.
_ALLOWED_FUNCTIONS = frozenset({"count", "sum", "avg", "min", "max"})


@dataclass(frozen=True)
class CacheabilityDecision:
    cacheable: bool
    reason: str
    cleaned_sql: str | None = None


def check_cacheable_sql(sql: str) -> CacheabilityDecision:
    """Return whether one SQL statement is in the conservative cache subset."""

    try:
        cleaned = validate_readonly_sql(sql)
    except UnsafeSQLError as exc:
        return CacheabilityDecision(
            cacheable=False,
            reason=f"read-only guard rejected the SQL: {exc}",
        )
    try:
        tokens = Tokenizer().tokenize(cleaned)
    except Exception as exc:  # noqa: BLE001 - tokenizer failure is fail-closed
        return CacheabilityDecision(
            cacheable=False,
            reason=f"SQL could not be tokenized safely: {type(exc).__name__}: {exc}",
        )
    if not tokens:
        return CacheabilityDecision(cacheable=False, reason="SQL has no tokens")
    for index, token in enumerate(tokens):
        if token.token_type not in _ALLOWED_TOKENS:
            return CacheabilityDecision(
                cacheable=False,
                reason=(
                    f"token {token.token_type.name} ({token.text!r}) is outside "
                    "the deterministic repeat-cache subset"
                ),
            )
        if token.token_type is TokenType.VAR and index + 1 < len(tokens):
            following = tokens[index + 1]
            if following.token_type is TokenType.L_PAREN:
                name = token.text.strip().lower()
                if name not in _ALLOWED_FUNCTIONS:
                    return CacheabilityDecision(
                        cacheable=False,
                        reason=(
                            f"function {token.text!r} is not in the verified "
                            f"deterministic allowlist {sorted(_ALLOWED_FUNCTIONS)}"
                        ),
                    )
    return CacheabilityDecision(
        cacheable=True,
        reason=(
            "single read-only SELECT inside the deterministic token subset; "
            "the exact cleaned SQL is the cache identity"
        ),
        cleaned_sql=cleaned,
    )


def repeat_cache_strategy_spec() -> StrategySpec:
    """Return the audited S1 declaration used by the registry."""

    return StrategySpec(
        strategy_id=CACHE_STRATEGY_ID,
        name="Fixed-epoch in-process repeat-result cache",
        database_model="tree",
        compatible_server_versions=("2.0.6",),
        enabled=True,
        verification_status=StrategyStatus.VERIFIED,
        is_baseline=False,
        implementation_class=(
            "tsdb_optimizer.strategies.repeat_cache.RepeatCacheStrategy"
        ),
        applicability_rules={
            "selection_policy": "exact SQL repeats within a caller-declared epoch",
            "requires_execution_option": CACHE_EPOCH_OPTION,
            "cache_identity": "epoch + exact cleaned SQL SHA-256 + strategy id",
            "cache_scope": "process-local, bounded LRU",
            "cache_hit_server_requests": "0",
            "cache_miss_server_requests": "1",
            "rewrite": "none",
        },
        execution_control=(
            "in-process exact-SQL full-result cache; a miss executes the cleaned "
            "SQL once, a hit dispatches no IoTDB request"
        ),
        semantic_test_suite=(
            "tests/unit/test_repeat_cache_strategy.py; "
            "tests/integration/test_repeat_cache_certification.py"
        ),
        plan_effect_expectation=None,
        allowed_execution_options=(CACHE_EPOCH_OPTION,),
        known_limitations=(
            "process-local only; the cache is lost when the process exits",
            "correctness depends on the caller-declared immutable dataset epoch",
            "no server-side invalidation or data-version API is claimed",
            "Agent-1 parser coverage is not required because no SQL rewrite is performed; the exact cleaned SQL is executed and the token subset is the cache guard",
            "not applicable when external writes or schema changes can occur in an epoch",
            "only complete results within explicit row and byte limits are cached",
            "the first exact occurrence is not served by this strategy; a later exact repeat may be served from cache",
            "no cold-cache or general production freshness claim is made",
        ),
        version="1.0.0",
        selection_policy=SelectionPolicy.REPEAT_CACHE,
    )


class RepeatCacheStrategy:
    """Fixed-epoch cache strategy with a fail-closed SQL subset guard.

    The strategy only supplies the option; the adapter owns the actual cache.
    It never executes SQL and never changes the SQL text.
    """

    def __init__(
        self,
        cache_epoch: str | None = None,
        *,
        spec: StrategySpec | None = None,
        validator: StrategyValidator | None = None,
    ):
        self.spec = spec or repeat_cache_strategy_spec()
        if (
            self.spec.strategy_id != CACHE_STRATEGY_ID
            or self.spec.is_baseline
            or self.spec.selection_policy is not SelectionPolicy.REPEAT_CACHE
        ):
            raise ValueError(
                "RepeatCacheStrategy requires the S1_REPEAT_CACHE repeat-policy spec"
            )
        self.cache_epoch = normalize_cache_epoch(cache_epoch)
        self.validator = validator or StrategyValidator()

    def is_applicable(
        self,
        query_features: QueryAnalysisResult | None,
        system_state: SystemSample | None,
    ) -> ApplicabilityResult:
        del system_state  # The cache does not depend on a live system snapshot.
        if query_features is None:
            return ApplicabilityResult(
                strategy_id=self.spec.strategy_id,
                applicable=False,
                reasons=["no Agent-1 analysis is available for the query"],
                missing_features=["query_analysis"],
            )
        if self.cache_epoch is None:
            return ApplicabilityResult(
                strategy_id=self.spec.strategy_id,
                applicable=False,
                reasons=[
                    "no caller-declared dataset epoch was supplied; the repeat "
                    "cache has no server-side invalidation protocol and cannot "
                    "prove freshness"
                ],
                missing_features=[CACHE_EPOCH_OPTION],
            )
        cacheability = check_cacheable_sql(query_features.parse.sql)
        if not cacheability.cacheable:
            return ApplicabilityResult(
                strategy_id=self.spec.strategy_id,
                applicable=False,
                reasons=[cacheability.reason],
            )
        warnings: list[str] = []
        if query_features.parse_status is not ParseStatus.SUCCESS:
            warnings.append(
                "Agent-1 parser does not cover this SQL subset; repeat cache "
                "executes the exact cleaned SQL without rewriting it"
            )
        return ApplicabilityResult(
            strategy_id=self.spec.strategy_id,
            applicable=True,
            reasons=[
                f"dataset epoch declared: {self.cache_epoch!r}",
                cacheability.reason,
                "repeat-cache strategies are eligible only from occurrence 2 "
                "onward; a later exact repeat may be served without a server "
                "request",
            ],
            warnings=warnings,
        )

    def build_candidate(
        self,
        original_sql: str,
        context: CandidateContext,
    ) -> CandidatePlan:
        del context  # The candidate depends only on the SQL and configured epoch.
        if self.cache_epoch is None:
            raise ValueError(
                "repeat cache cannot build a candidate without cache_epoch"
            )
        cacheability = check_cacheable_sql(original_sql)
        if not cacheability.cacheable or cacheability.cleaned_sql is None:
            raise ValueError(
                f"SQL is not cacheable: {cacheability.reason}"
            )
        return CandidatePlan(
            strategy_id=self.spec.strategy_id,
            strategy_version=self.spec.version,
            candidate_sql=cacheability.cleaned_sql,
            execution_options={CACHE_EPOCH_OPTION: self.cache_epoch},
            expected_plan_effect=None,
            warnings=[cacheability.reason],
        )

    def validate(
        self,
        original_sql: str,
        candidate: CandidatePlan,
        context: ValidationContext,
    ) -> ValidationResult:
        central = self.validator.validate(
            self.spec, original_sql, candidate, context
        )
        checks: list[ValidationCheck] = []
        if self.cache_epoch is None:
            checks.append(
                ValidationCheck(
                    name="cache_epoch_configured",
                    status=ValidationCheckStatus.FAILED,
                    detail=(
                        "no caller-declared dataset epoch is configured; refusing "
                        "an unbounded freshness claim"
                    ),
                )
            )
        else:
            checks.append(
                ValidationCheck(
                    name="cache_epoch_configured",
                    status=ValidationCheckStatus.PASSED,
                    detail=f"dataset epoch is configured as {self.cache_epoch!r}",
                )
            )
        expected_options = (
            {CACHE_EPOCH_OPTION: self.cache_epoch}
            if self.cache_epoch is not None
            else {}
        )
        if candidate.execution_options != expected_options:
            checks.append(
                ValidationCheck(
                    name="cache_option_identity",
                    status=ValidationCheckStatus.FAILED,
                    detail=(
                        "candidate execution options do not exactly match the "
                        f"strategy's configured epoch: {candidate.execution_options}"
                    ),
                )
            )
        else:
            checks.append(
                ValidationCheck(
                    name="cache_option_identity",
                    status=ValidationCheckStatus.PASSED,
                    detail="candidate carries exactly the configured cache epoch",
                )
            )
        try:
            cleaned_original = validate_readonly_sql(original_sql)
            cleaned_candidate = validate_readonly_sql(candidate.candidate_sql)
        except UnsafeSQLError as exc:
            checks.append(
                ValidationCheck(
                    name="cache_sql_identity",
                    status=ValidationCheckStatus.FAILED,
                    detail=f"SQL failed the read-only guard: {exc}",
                )
            )
        else:
            if cleaned_candidate != cleaned_original:
                checks.append(
                    ValidationCheck(
                        name="cache_sql_identity",
                        status=ValidationCheckStatus.FAILED,
                        detail=(
                            "repeat cache must execute the original cleaned SQL "
                            "exactly; the candidate differs"
                        ),
                    )
                )
            else:
                checks.append(
                    ValidationCheck(
                        name="cache_sql_identity",
                        status=ValidationCheckStatus.PASSED,
                        detail="candidate SQL is the exact cleaned original SQL",
                    )
                )
        cacheability = check_cacheable_sql(candidate.candidate_sql)
        checks.append(
            ValidationCheck(
                name="cacheable_sql_subset",
                status=(
                    ValidationCheckStatus.PASSED
                    if cacheability.cacheable
                    else ValidationCheckStatus.FAILED
                ),
                detail=cacheability.reason,
            )
        )
        return ValidationResult.combine(central, ValidationResult.from_checks(checks))


__all__ = [
    "CACHE_EPOCH_OPTION",
    "CACHE_STRATEGY_ID",
    "CacheabilityDecision",
    "RepeatCacheStrategy",
    "check_cacheable_sql",
    "repeat_cache_strategy_spec",
]
