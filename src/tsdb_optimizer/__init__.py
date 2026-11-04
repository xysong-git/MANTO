"""Multi-Agent TSDB Optimizer.

The executed strategy set remains limited: only the verified Native baseline is
enabled.  The package provides configuration, structured logging, an IoTDB
tree-model adapter, SQLite persistence, Agent 1 query analysis, Agent 3
performance monitoring, and the Phase-4 strategy registry, validator,
deterministic decision engine, single-candidate executor, conservative Native
fallback and decision/execution audit trail.  Agent 4, online learning and LLM
SQL generation are not implemented.
"""

__version__ = "0.1.0"
