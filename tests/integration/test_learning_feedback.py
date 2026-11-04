"""True IoTDB executions + Agent-3 records + Agent-4 statistics, explicitly invoked."""
import os
import pytest
from tsdb_optimizer.config import load_settings
from tsdb_optimizer.adapters.iotdb.tree_adapter import IoTDBTreeAdapter
from tsdb_optimizer.agents import PerformanceMonitoringAgent
from tsdb_optimizer.agents.learning_feedback import LearningFeedbackAgent
from tsdb_optimizer.execution import DecisionExecutionService
from tsdb_optimizer.learning.repository import ExperienceRepository
from tsdb_optimizer.schemas.learning import LearningContext, load_identity, SampleStatus
from tsdb_optimizer.storage import SQLiteStore
from tsdb_optimizer.strategies import build_default_registry
from tsdb_optimizer.agents import QueryAnalysisAgent
from tsdb_optimizer.agents.learning_feedback import feature_identity

pytestmark = pytest.mark.integration


def test_real_execution_records_update_l1_without_dual_execution(tmp_path):
    if os.environ.get('IOTDB_INTEGRATION') != '1':
        pytest.skip('set IOTDB_INTEGRATION=1 to run real IoTDB tests')
    settings = load_settings()
    adapter = IoTDBTreeAdapter(settings)
    store = SQLiteStore(tmp_path/'learning.sqlite')
    registry = build_default_registry()
    monitor = PerformanceMonitoringAgent(registry=registry)
    repo = ExperienceRepository(store)
    learner = LearningFeedbackAgent(repo, registry)
    sql = 'SELECT s_1666, s_1766 FROM root.db800.g_0.d_0, root.db800.g_0.d_1 WHERE time = 1640966450000'
    try:
        server = adapter.doctor().server
        assert server.version == '2.0.6'
        service = DecisionExecutionService(adapter=adapter, store=store, registry=registry,
                                           server_version=server.version, database_model='tree')
        monitor.sample_once()
        context = None
        statuses = []
        for _ in range(3):
            before = monitor.sample_once()
            run = service.run(sql, mode='baseline', timeout_ms=30000)
            result = run.execution.attempts[0].execution
            assert result.status.value == 'SUCCESS' and len(run.execution.attempts) == 1
            record = monitor.record_execution(result, decision_id=run.decision.decision_id, snapshots=before)
            assert record.runtime_result_check == 'NOT_CHECKED'
            host, bucket = load_identity(before, 'client_host', result.started_at)
            context = LearningContext(server_id=f'{settings.iotdb_host}:{settings.iotdb_port}',
                server_version=server.version, database_model='tree', dataset_epoch='integration-static-20261102',
                result_rows=result.row_count, cache_condition='warm-serial',
                load_source='client_host', load_host=host, load_bucket=bucket)
            feedback = learner.observe(run, context, record=record, before=before)
            assert not feedback.warnings
            assert feedback.sample_status == SampleStatus.VALID
            statuses.append(feedback)
        assert store.count('executions') == 3
        analysis = QueryAnalysisAgent().analyze(sql)
        template, features = feature_identity(analysis)
        rows = repo.retrieve(template_key=template, feature_key=features, context_key=context.key)
        assert len(rows) == 3  # A real load bucket change must not be silently mixed.
        model = repo.current_model(rows[0].group_key)
        assert model.statistics[0].valid_count == 3
        assert statuses[-1].model_updated
        assert learner.recommend(analysis, context) is None  # only one observed strategy
    finally:
        adapter.close()
