"""Four logical agents; optional imports avoid decision/learning cycles."""

from .performance_monitor import PerformanceMonitoringAgent
from .query_analysis import QueryAnalysisAgent

__all__ = ["PerformanceMonitoringAgent", "QueryAnalysisAgent",
           "OptimizationDecisionAgent", "LearningFeedbackAgent"]


def __getattr__(name):
    if name == "OptimizationDecisionAgent":
        from .optimization_decision import OptimizationDecisionAgent
        return OptimizationDecisionAgent
    if name == "LearningFeedbackAgent":
        from .learning_feedback import LearningFeedbackAgent
        return LearningFeedbackAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
