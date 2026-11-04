#!/usr/bin/env bash
# Create a project-local environment with the certified IoTDB client.
#
# Usage:
#   scripts/setup_iotdb206_env.sh [target-venv]
#
# The default target is .venv-iotdb206/ in the repository.  This script never
# modifies the shared conda environment and never reads or prints the IoTDB
# password.  It installs the project's pinned dependencies from pyproject.toml,
# so apache-iotdb must resolve to 2.0.6.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${1:-${ROOT}/.venv-iotdb206}"
PYTHON_BIN="${PYTHON:-python3}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "error: ${PYTHON_BIN} not found; set PYTHON to a Python 3.12 interpreter" >&2
  exit 1
fi

if [ ! -x "${VENV_DIR}/bin/python" ]; then
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

export PIP_DISABLE_PIP_VERSION_CHECK=1
(
  cd "${ROOT}"
  "${VENV_DIR}/bin/python" -m pip install -e ".[dev]"
)

"${VENV_DIR}/bin/python" - <<'PY'
import importlib.metadata as metadata
import sys

version = metadata.version("apache-iotdb")
if version != "2.0.6":
    print(
        f"error: expected apache-iotdb==2.0.6, got {version}; "
        "the certification environment is 2.0.6 only",
        file=sys.stderr,
    )
    raise SystemExit(1)
print(f"apache-iotdb=={version}")
print(f"interpreter={sys.executable}")
PY

echo "Ready: ${VENV_DIR}/bin/python"
