"""SQLite v7 additive schema; model publication is one transaction."""
LEARNING_SCHEMA = '''
CREATE TABLE IF NOT EXISTS learning_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL
);
INSERT OR IGNORE INTO learning_state VALUES (1,0);
CREATE TABLE IF NOT EXISTS learning_group_state (
    group_key TEXT PRIMARY KEY, revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS experiences (
    experience_id TEXT PRIMARY KEY,
    query_id TEXT NOT NULL REFERENCES queries(query_id),
    decision_id TEXT NOT NULL REFERENCES decisions(decision_id),
    execution_id TEXT NOT NULL UNIQUE REFERENCES executions(execution_id),
    strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
    template_key TEXT NOT NULL, feature_key TEXT NOT NULL,
    context_key TEXT NOT NULL, group_key TEXT NOT NULL,
    sample_status TEXT NOT NULL CHECK(sample_status IN ('VALID','LOW_CONFIDENCE','INVALID')),
    reward REAL, body_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_experience_context
 ON experiences(template_key, feature_key, context_key, strategy_id, strategy_version);
CREATE TABLE IF NOT EXISTS strategy_quarantine (
    strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
    reason TEXT NOT NULL, execution_id TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(strategy_id,strategy_version)
);
CREATE TABLE IF NOT EXISTS learning_models (
    model_version TEXT PRIMARY KEY, group_key TEXT NOT NULL,
    data_revision INTEGER NOT NULL, body_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learning_heads (
    group_key TEXT PRIMARY KEY, model_version TEXT NOT NULL REFERENCES learning_models(model_version)
);
'''
