#!/usr/bin/env bash
# Operator-run validation, no retries, no changes to IoTDB or foreign processes.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $# -ne 2 || ! "$2" =~ ^[0-9]+$ ]]; then
    echo 'Usage: bash scripts/phase7_run_staged_validation.sh NEW_OUTPUT_DIRECTORY DATANODE_PID' >&2
    exit 2
fi
MANTO_RUN_ROOT="$1"
MANTO_DB_PID="$2"
mkdir -p "$(dirname "$MANTO_RUN_ROOT")"
mkdir "$MANTO_RUN_ROOT"  # Existing evidence is never overwritten or resumed.
export PYTHONPATH=src:scripts
run_logged() {
    "$@" 2>&1 | tee -a "$MANTO_RUN_ROOT/console.log"
}
run_logged conda run --no-capture-output -n MANTO python -m pytest -q \
    tests/unit/test_staggered_load.py tests/unit/test_exclusive_instance.py \
    tests/unit/test_delayed_batch.py tests/unit/test_sequence_e2e.py \
    tests/unit/test_request_memo.py tests/unit/test_wal_request_session.py \
    tests/unit/test_orchestrator.py tests/unit/test_phase7_cost_diagnosis.py \
    tests/unit/test_sql_parser.py tests/unit/test_query_features_schema.py \
    tests/unit/test_batch_statistics.py tests/unit/test_delayed_batch_restore.py \
    tests/unit/test_bandit_prediction_cost.py
run_logged conda run --no-capture-output -n MANTO python scripts/phase7_wait_exclusive.py \
    --pid "$MANTO_DB_PID" --quiet-seconds 300 --output "$MANTO_RUN_ROOT/occupancy-wait.jsonl"
run_logged conda run --no-capture-output -n MANTO python scripts/phase7_staggered_pilot.py \
    --output "$MANTO_RUN_ROOT/pilot" --local-db-pid "$MANTO_DB_PID" --cpu-cotenant-authorized
run_logged conda run --no-capture-output -n MANTO python scripts/phase7_delayed_batch.py \
    --output "$MANTO_RUN_ROOT/study" --epoch heavy-complete-20261102-v2 \
    --local-db-pid "$MANTO_DB_PID" --immutable-epoch-confirmed --cpu-cotenant-authorized
run_logged conda run --no-capture-output -n MANTO python scripts/phase7_analyze_delayed_batch.py \
    "$MANTO_RUN_ROOT/study/journal.jsonl" --output "$MANTO_RUN_ROOT/report.json"
run_logged conda run --no-capture-output -n MANTO python scripts/phase7_diagnose_batch_costs.py \
    "$MANTO_RUN_ROOT/study/journal.jsonl" --output "$MANTO_RUN_ROOT/cost-diagnosis.json"
run_logged env IOTDB_INTEGRATION=1 \
    IOTDB_TEST_SQL='SELECT s_355 FROM root.small_scale.g_0.d_1 WHERE time >= 1640968700000 AND time < 1640978355000 LIMIT 5' \
    conda run --no-capture-output -n MANTO python -m pytest -q
printf 'Execution and independent replay completed. Benefit/acceptance are determined by %s/report.json, not this exit code.\n' "$MANTO_RUN_ROOT"
