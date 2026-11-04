"""The guide's four agent entry points retain one implementation each."""
import importlib
import pytest


@pytest.mark.parametrize('name,cls', [
    ('query_analysis', 'QueryAnalysisAgent'),
    ('optimization_decision', 'OptimizationDecisionAgent'),
    ('performance_monitor', 'PerformanceMonitoringAgent'),
    ('learning_feedback', 'LearningFeedbackAgent'),
])
def test_agent_entry_point_identity(name, cls):
    public = importlib.import_module('tsdb_optimizer.agents')
    package = importlib.import_module('tsdb_optimizer.agents.'+name)
    implementation = importlib.import_module('tsdb_optimizer.agents.'+name+'.agent')
    assert getattr(public, cls) is getattr(package, cls) is getattr(implementation, cls)


def test_decision_engine_is_not_duplicated():
    from tsdb_optimizer.agents import OptimizationDecisionAgent
    from tsdb_optimizer.decision import DecisionEngine
    from tsdb_optimizer.benchmark.policy import BenchmarkEngine
    assert OptimizationDecisionAgent is DecisionEngine
    assert issubclass(BenchmarkEngine, OptimizationDecisionAgent)
