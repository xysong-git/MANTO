"""Agent 2 entry point backed by the existing validated decision engine.

An identity alias preserves subclasses, injected engines, and the public
DecisionEngine API; there is no second decision algorithm or extra execution.
"""
from ...decision.engine import DecisionEngine

OptimizationDecisionAgent = DecisionEngine

__all__ = ["OptimizationDecisionAgent"]
