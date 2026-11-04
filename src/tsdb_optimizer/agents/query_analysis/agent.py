"""Agent 1: parse SQL and extract structured query features.

This agent is deterministic and LLM-free.  It never rewrites SQL and never
invents statistics; unsupported statements are returned with explicit
``UNSUPPORTED`` status and missing-feature reasons.
"""

from __future__ import annotations

from ...errors import ParserInternalError
from ...parsing import SQLParserAdapter
from ...schemas.query_features import QueryAnalysisResult


class QueryAnalysisAgent:
    """Thin orchestration layer around :class:`SQLParserAdapter`."""

    def __init__(self, parser: SQLParserAdapter | None = None):
        self.parser = parser or SQLParserAdapter()

    def analyze(self, sql: str, query_id: str | None = None) -> QueryAnalysisResult:
        parsed = self.parser.parse(sql, query_id=query_id)
        try:
            features = self.parser.extract_features(parsed)
        except ParserInternalError:
            # An internal extraction failure is a bug, not an unsupported SQL
            # statement; surface it instead of silently emitting empty features.
            raise
        return QueryAnalysisResult(
            query_id=parsed.query_id,
            parse=parsed,
            features=features,
        )


__all__ = ["QueryAnalysisAgent"]
