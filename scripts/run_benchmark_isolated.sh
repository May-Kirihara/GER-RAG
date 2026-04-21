#!/usr/bin/env bash
# Run the GER-RAG benchmark suite against an isolated bench DB so the
# user's production memory at ~/.local/share/ger-rag/ is never touched.
#
# Usage:  scripts/run_benchmark_isolated.sh [doc_limit]
#   doc_limit defaults to 200. Pass 0 for the full corpus (slow).
set -euo pipefail

DOC_LIMIT="${1:-200}"
PORT="${BENCH_PORT:-8765}"
BENCH_DIR="${BENCH_DIR:-/tmp/ger-rag-bench}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PROJECT_ROOT}/.venv/bin/python"
URL="http://127.0.0.1:${PORT}"
LOG_FILE="${BENCH_DIR}/server.log"
REPORT_FILE="${BENCH_DIR}/report.json"

mkdir -p "${BENCH_DIR}"

cat <<EOF
================================================================
  GER-RAG isolated benchmark
  bench dir : ${BENCH_DIR}
  port      : ${PORT}
  doc limit : ${DOC_LIMIT}
  prod DB at ~/.local/share/ger-rag/ is NOT touched.
================================================================
EOF

cleanup() {
    if [[ -n "${SERVER_PID:-}" ]] && kill -0 "${SERVER_PID}" 2>/dev/null; then
        echo "Stopping server PID ${SERVER_PID}..."
        kill "${SERVER_PID}" 2>/dev/null || true
        wait "${SERVER_PID}" 2>/dev/null || true
    fi
}
trap cleanup EXIT

cd "${PROJECT_ROOT}"

export GER_RAG_DATA_DIR="${BENCH_DIR}"
unset GER_RAG_CONFIG  # ignore prod config file if any

echo "[1/4] Starting uvicorn on port ${PORT} (model load may take 10-30s)..."
"${PYTHON}" -m uvicorn ger_rag.server.app:app \
    --host 127.0.0.1 --port "${PORT}" \
    >"${LOG_FILE}" 2>&1 &
SERVER_PID=$!

# Wait for readiness
for i in $(seq 1 60); do
    if curl -sf "${URL}/docs" >/dev/null 2>&1; then
        echo "Server ready after ${i}s."
        break
    fi
    if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
        echo "ERROR: server died during startup. Last log lines:"
        tail -30 "${LOG_FILE}"
        exit 1
    fi
    sleep 1
done

if ! curl -sf "${URL}/docs" >/dev/null 2>&1; then
    echo "ERROR: server did not become ready within 60s."
    tail -30 "${LOG_FILE}"
    exit 1
fi

echo "[2/4] Loading up to ${DOC_LIMIT} documents from input/documents.csv..."
"${PYTHON}" scripts/load_csv.py --url "${URL}" --limit "${DOC_LIMIT}"

echo "[3/4] Running benchmark suite (skipping --reset to preserve bench state)..."
"${PYTHON}" scripts/benchmark.py --url "${URL}" --all --output "${REPORT_FILE}"

echo "[4/4] Done. Report saved to ${REPORT_FILE}"
echo "Bench DB left intact at ${BENCH_DIR} for inspection."
echo "To wipe it: rm -rf ${BENCH_DIR}"
