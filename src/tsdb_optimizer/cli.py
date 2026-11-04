"""CLI: legacy query/optimize, plus Phase-6 run/workload orchestration."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .adapters import IoTDBTreeAdapter
from .cache_epoch import normalize_cache_epoch
from .config import Settings, load_settings
from .decision import DecisionMode
from .errors import AdapterError, ConfigError, TSDBOptimizerError, UnsafeSQLError
from .execution import DecisionExecutionService
from .logging import configure_logging
from .schemas import QueryRequest
from .schemas.execution import ExecutionStatus
from .sql_guard import sql_sha256, validate_readonly_sql
from .storage import SQLiteStore
from .strategies import build_default_registry

logger = logging.getLogger(__name__)

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tsdb-optimizer",
        description="IoTDB query execution with limited, verified strategies",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--env-file", type=Path, help="path to a .env file")
    parser.add_argument("--sqlite", type=Path, help="override SQLite database path")
    parser.add_argument("--log-level", help="DEBUG/INFO/WARNING/ERROR/CRITICAL")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="probe connection, version and capabilities")

    query = subparsers.add_parser("query", help="execute one read-only SELECT")
    source = query.add_mutually_exclusive_group(required=True)
    source.add_argument("--sql", help="single SELECT statement")
    source.add_argument("--file", type=Path, help="file containing one SELECT")
    query.add_argument("--source", default="cli", help="source label for the query record")
    query.add_argument(
        "--preview-rows",
        type=int,
        default=None,
        help="rows retained in the JSON preview (all rows are still consumed)",
    )
    query.add_argument(
        "--timeout-ms",
        type=int,
        default=None,
        help=(
            "IoTDB client query timeout in milliseconds; 0 keeps the client "
            "default, omitted means no explicit timeout"
        ),
    )

    optimize = subparsers.add_parser(
        "optimize",
        help="run Agent 2 decision + single-candidate execution with persistence",
    )
    optimize_source = optimize.add_mutually_exclusive_group(required=True)
    optimize_source.add_argument("--sql", help="single SELECT statement")
    optimize_source.add_argument("--file", type=Path, help="file containing one SELECT")
    optimize.add_argument(
        "--source", default="cli:optimize", help="source label for the query record"
    )
    optimize.add_argument(
        "--mode",
        choices=[mode.value for mode in DecisionMode],
        default=DecisionMode.RULE_BASED.value,
        help="decision mode; all modes are LLM-free in Phase 4",
    )
    optimize.add_argument(
        "--preview-rows",
        type=int,
        default=None,
        help="rows retained in the JSON preview (all rows are still consumed)",
    )
    optimize.add_argument(
        "--timeout-ms",
        type=int,
        default=None,
        help=(
            "IoTDB client query timeout in milliseconds; 0 keeps the client "
            "default, omitted means no explicit timeout"
        ),
    )
    optimize.add_argument(
        "--server-version",
        default=None,
        help=(
            "observed IoTDB server version (for example 2.0.6); required for a "
            "non-Native strategy to be applicable"
        ),
    )
    optimize.add_argument(
        "--database-model",
        choices=["tree", "table", "unknown"],
        default=None,
        help="observed IoTDB database model; required for a non-Native strategy",
    )
    optimize.add_argument(
        "--cache-epoch",
        default=None,
        help=(
            "caller-declared immutable dataset epoch for S1_REPEAT_CACHE; "
            "omit to keep the repeat cache disabled"
        ),
    )
    for command in ('run', 'workload'):
        workflow = subparsers.add_parser(command, help='four-agent workflow with full trace')
        if command == 'run':
            inputs = workflow.add_mutually_exclusive_group(required=True)
            inputs.add_argument('--sql')
            inputs.add_argument('--file', type=Path, help='one SELECT statement')
        else:
            workflow.add_argument('--file', type=Path, required=True,
                                  help='UTF-8 JSON array of individual SQL strings; serial execution')
        workflow.add_argument('--mode', choices=[m.value for m in DecisionMode], default='rule_based')
        workflow.add_argument('--source', default='cli:' + command)
        workflow.add_argument('--preview-rows', type=int)
        workflow.add_argument('--timeout-ms', type=int)
        workflow.add_argument('--server-version', default='UNKNOWN', help='previously observed version')
        workflow.add_argument('--server-id', default='UNKNOWN', help='stable opaque server identity')
        workflow.add_argument('--database-model', choices=['tree', 'table', 'unknown'], default='unknown')
        workflow.add_argument('--dataset-epoch', default='UNKNOWN', help='caller-declared dataset identity')
        workflow.add_argument('--cache-condition', default='UNKNOWN', help='declared comparable experiment condition')
        workflow.add_argument('--cache-epoch', default=None)
        workflow.add_argument('--expected-result-rows', type=int, default=None,
                              help='independently known row count for exact-context retrieval; never a prediction')
    return parser


def _settings_from_args(args: argparse.Namespace) -> Settings:
    settings = load_settings(env_file=args.env_file)
    updates: dict[str, object] = {}
    if args.sqlite is not None:
        updates["sqlite_path"] = args.sqlite
    if args.log_level is not None:
        level = args.log_level.strip().upper()
        if level not in _LOG_LEVELS:
            raise ConfigError(f"invalid log level: {args.log_level}")
        updates["log_level"] = level
    if updates:
        settings = settings.model_copy(update=updates)
    return settings


def _read_sql(args: argparse.Namespace) -> str:
    if args.sql is not None:
        return args.sql
    path: Path = args.file
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read SQL file {path}: {exc}") from exc


def _emit_error(error_type: str, message: str) -> None:
    print(
        json.dumps(
            {"ok": False, "error_type": error_type, "error_message": message},
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )


def _cmd_doctor(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    configure_logging(settings.log_level)
    adapter = IoTDBTreeAdapter(settings)
    try:
        report = adapter.doctor()
    finally:
        adapter.close()
    print(report.model_dump_json(indent=2))
    return 0 if report.ok else 1


def _cmd_query(args: argparse.Namespace) -> int:
    sql = _read_sql(args)
    cleaned = validate_readonly_sql(sql)
    settings = _settings_from_args(args)
    configure_logging(settings.log_level)
    preview_rows = (
        args.preview_rows if args.preview_rows is not None else settings.preview_rows
    )
    if preview_rows < 1:
        raise ConfigError("preview rows must be positive")
    timeout_ms = args.timeout_ms
    if timeout_ms is not None and timeout_ms < 0:
        raise ConfigError("timeout-ms must be non-negative")

    request = QueryRequest(
        sql=cleaned,
        source=args.source,
        timeout_ms=timeout_ms,
    )
    store = SQLiteStore(settings.sqlite_path)
    store.initialize()
    store.record_query(request, sql_sha256(cleaned))

    adapter = IoTDBTreeAdapter(settings)
    try:
        result = adapter.execute_readonly(request, preview_rows=preview_rows)
        try:
            store.record_execution(result)
        except Exception as exc:  # noqa: BLE001 - query already completed
            # The database has already executed the query.  A local audit-write
            # failure must be visible but must not turn a successful query into
            # a reported execution failure.
            logger.exception(
                "execution persistence failed",
                extra={
                    "event": "execution_persistence_error",
                    "query_id": result.query_id,
                    "execution_id": result.execution_id,
                    "error_type": type(exc).__name__,
                },
            )
            result = result.model_copy(
                update={
                    "warnings": [
                        *result.warnings,
                        "EXECUTION_PERSISTENCE_FAILED: "
                        f"{type(exc).__name__}: {exc}",
                    ]
                }
            )
    finally:
        adapter.close()

    print(result.model_dump_json(indent=2))
    return 0 if result.status == ExecutionStatus.SUCCESS else 1


def _cmd_optimize(args: argparse.Namespace) -> int:
    sql = validate_readonly_sql(_read_sql(args))
    settings = _settings_from_args(args)
    configure_logging(settings.log_level)
    preview_rows = (
        args.preview_rows if args.preview_rows is not None else settings.preview_rows
    )
    if preview_rows < 1:
        raise ConfigError("preview rows must be positive")
    timeout_ms = args.timeout_ms
    if timeout_ms is not None and timeout_ms < 0:
        raise ConfigError("timeout-ms must be non-negative")

    try:
        cache_epoch = normalize_cache_epoch(
            args.cache_epoch
            if args.cache_epoch is not None
            else settings.cache_epoch
        )
    except ValueError as exc:
        raise ConfigError(f"invalid cache epoch: {exc}") from exc
    registry = build_default_registry(cache_epoch=cache_epoch)
    store = SQLiteStore(settings.sqlite_path)
    adapter = IoTDBTreeAdapter(settings)
    try:
        service = DecisionExecutionService(
            adapter=adapter,
            store=store,
            registry=registry,
            preview_rows=preview_rows,
            server_version=args.server_version,
            database_model=args.database_model,
        )
        run = service.run(
            sql,
            mode=args.mode,
            source=args.source,
            timeout_ms=timeout_ms,
            preview_rows=preview_rows,
            server_version=args.server_version,
            database_model=args.database_model,
        )
    finally:
        adapter.close()
    print(run.model_dump_json(indent=2))
    return 0 if run.execution.final_status == ExecutionStatus.SUCCESS else 1


def _cmd_workflow(args: argparse.Namespace) -> int:
    from .orchestration import Orchestrator, State
    from .schemas.learning import LearningContext

    if args.command == 'workload':
        try:
            sqls = json.loads(args.file.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ConfigError('workload must be a readable JSON array of SQL strings') from exc
        if not isinstance(sqls, list) or not sqls or any(not isinstance(s, str) or not s.strip() for s in sqls):
            raise ConfigError('workload must be a nonempty JSON array of nonblank SQL strings')
    else:
        sqls = [_read_sql(args)]
    settings = _settings_from_args(args)
    configure_logging(settings.log_level)
    preview = args.preview_rows if args.preview_rows is not None else settings.preview_rows
    if preview < 1 or (args.timeout_ms is not None and args.timeout_ms < 0):
        raise ConfigError('preview rows must be positive; timeout must be nonnegative')
    if args.expected_result_rows is not None and args.expected_result_rows < 0:
        raise ConfigError('expected-result-rows must be nonnegative')
    epoch = normalize_cache_epoch(args.cache_epoch if args.cache_epoch is not None else settings.cache_epoch)
    if epoch is not None and epoch != args.dataset_epoch:
        raise ConfigError('cache-epoch must equal the declared immutable dataset-epoch')
    context = LearningContext(server_id=args.server_id, server_version=args.server_version,
        database_model=args.database_model, dataset_epoch=args.dataset_epoch,
        cache_condition=args.cache_condition, result_rows=args.expected_result_rows, load_source='client_host')
    adapter = IoTDBTreeAdapter(settings)
    store = SQLiteStore(settings.sqlite_path)
    try:
        orchestrator = Orchestrator(adapter=adapter, store=store,
            registry=build_default_registry(cache_epoch=epoch), context=context, preview_rows=preview)
        # Establish psutil's first CPU baseline; no query is executed by sampling.
        try:
            orchestrator.monitor.sample_once()
        except Exception as exc:
            logger.warning('monitor initialization unavailable: %s', type(exc).__name__)
        failed = False
        for sql in sqls:
            result = orchestrator.run(sql, mode=args.mode, timeout_ms=args.timeout_ms, source=args.source)
            # Workload is JSONL, one result per request; no SQL string splitting.
            print(result.model_dump_json(indent=2 if args.command == 'run' else None))
            failed |= result.trace.events[-1].state != State.SUCCEEDED
        return 1 if failed else 0
    finally:
        adapter.close()
        store.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in {"run", "workload"}:
            return _cmd_workflow(args)
        if args.command == "doctor":
            return _cmd_doctor(args)
        if args.command == "query":
            return _cmd_query(args)
        if args.command == "optimize":
            return _cmd_optimize(args)
        parser.error(f"unknown command: {args.command}")
        return 2
    except (ConfigError, UnsafeSQLError) as exc:
        _emit_error(type(exc).__name__, str(exc))
        return 2
    except AdapterError as exc:
        _emit_error(type(exc).__name__, str(exc))
        return 1
    except TSDBOptimizerError as exc:
        _emit_error(type(exc).__name__, str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary must not leak secrets
        logger.exception("unexpected CLI failure")
        _emit_error(type(exc).__name__, str(exc)[:4000])
        return 1
