#!/usr/bin/env bash

# If invoked via sh or another shell, restart with bash for compatibility.
if [ -z "${BASH_VERSION:-}" ]; then
  exec /usr/bin/env bash "$0" "$@"
fi

set -euo pipefail

# Generic from-scratch Infinity launcher for a single embedding model.
# Example:
#   ./start-infinity-embedding-7990.sh --model BAAI/bge-small-en-v1.5

usage() {
  cat <<'EOF'
Usage:
  ./start-infinity-embedding-7990.sh [options]

Options:
  -m, --model        Hugging Face model id to pull and serve (optional)
  -p, --port         Local port (default: 7990)
  -h, --host         Bind host (default: 127.0.0.1)
      --url-prefix   API prefix (default: /v1)
  --engine       Infinity engine (auto: torch; allowed: torch|ctranslate2|optimum|neuron|debugengine)
      --api-key      Optional API key for Infinity
      --help         Show this help

Environment fallback:
  Resolution order for model id:
    1) --model
    2) INFINITY_MODEL_ID
    3) BAAI/bge-small-en-v1.5
EOF
}

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="${ROOT_DIR}/.runtime/infinity-7990"
VENV_DIR="${RUNTIME_DIR}/.venv"

MODEL_ARG=""
INFINITY_PORT="${INFINITY_PORT:-7990}"
INFINITY_HOST="${INFINITY_HOST:-127.0.0.1}"
INFINITY_URL_PREFIX="${INFINITY_URL_PREFIX:-/v1}"
INFINITY_ENGINE="${INFINITY_ENGINE:-}"
INFINITY_API_KEY="${INFINITY_API_KEY:-}"
DEFAULT_INFINITY_MODEL_ID="BAAI/bge-small-en-v1.5"
PYTHON_CMD="${INFINITY_PYTHON_BIN:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -m|--model)
      [[ $# -lt 2 ]] && { echo "[ERROR] Missing value for $1" >&2; usage; exit 1; }
      MODEL_ARG="$2"
      shift 2
      ;;
    -p|--port)
      [[ $# -lt 2 ]] && { echo "[ERROR] Missing value for $1" >&2; usage; exit 1; }
      INFINITY_PORT="$2"
      shift 2
      ;;
    -h|--host)
      [[ $# -lt 2 ]] && { echo "[ERROR] Missing value for $1" >&2; usage; exit 1; }
      INFINITY_HOST="$2"
      shift 2
      ;;
    --url-prefix)
      [[ $# -lt 2 ]] && { echo "[ERROR] Missing value for $1" >&2; usage; exit 1; }
      INFINITY_URL_PREFIX="$2"
      shift 2
      ;;
    --engine)
      [[ $# -lt 2 ]] && { echo "[ERROR] Missing value for $1" >&2; usage; exit 1; }
      INFINITY_ENGINE="$2"
      shift 2
      ;;
    --api-key)
      [[ $# -lt 2 ]] && { echo "[ERROR] Missing value for $1" >&2; usage; exit 1; }
      INFINITY_API_KEY="$2"
      shift 2
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      echo "[ERROR] Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

INFINITY_MODEL_ID="${MODEL_ARG:-${INFINITY_MODEL_ID:-${DEFAULT_INFINITY_MODEL_ID}}}"

if [[ -z "${INFINITY_ENGINE}" ]]; then
  INFINITY_ENGINE="torch"
fi

shopt -s nocasematch
if [[ "${INFINITY_MODEL_ID}" == *"gguf"* ]]; then
  echo "[ERROR] GGUF model detected: ${INFINITY_MODEL_ID}" >&2
  echo "[ERROR] Current Infinity build does not expose a llama_cpp engine (valid engines: torch|ctranslate2|optimum|neuron|debugengine)." >&2
  echo "[ERROR] Use a non-GGUF embedding model (example: BAAI/bge-small-en-v1.5 or jinaai/jina-embeddings-v5-text-small-retrieval)." >&2
  exit 1
fi
shopt -u nocasematch

mkdir -p "${RUNTIME_DIR}"

if [[ -n "${PYTHON_CMD}" ]] && ! command -v "${PYTHON_CMD}" >/dev/null 2>&1; then
  echo "[WARN] INFINITY_PYTHON_BIN='${PYTHON_CMD}' not found in PATH. Falling back to auto-detection."
  PYTHON_CMD=""
fi

if [[ -z "${PYTHON_CMD}" ]]; then
  for candidate in python3.12 python3.11 python3.10 python3.13 python3; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      candidate_ver="$(${candidate} -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
      if [[ "${candidate_ver}" != "3.14" ]]; then
        PYTHON_CMD="${candidate}"
        break
      fi
    fi
  done
fi

if [[ -z "${PYTHON_CMD}" ]]; then
  echo "[ERROR] No compatible Python found. Install python3.12 (recommended) and retry." >&2
  exit 1
fi

PYTHON_VER="$(${PYTHON_CMD} -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${PYTHON_VER}" == "3.14" ]]; then
  echo "[ERROR] Python 3.14 is not supported by this launcher for Infinity right now." >&2
  echo "[ERROR] Please use python3.12 (set INFINITY_PYTHON_BIN=python3.12)." >&2
  exit 1
fi

echo "[INFO] Using Python ${PYTHON_VER} via ${PYTHON_CMD}"

if ! command -v uv >/dev/null 2>&1; then
  echo "[ERROR] uv is required: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "[INFO] Creating virtual environment in ${VENV_DIR}"
  uv venv --python "${PYTHON_CMD}" "${VENV_DIR}"
elif [[ -x "${VENV_DIR}/bin/python" ]]; then
  VENV_PY_VER="$(${VENV_DIR}/bin/python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  if [[ "${VENV_PY_VER}" == "3.14" ]]; then
    echo "[WARN] Existing venv uses Python 3.14; recreating with ${PYTHON_CMD} (${PYTHON_VER})."
    rm -rf "${VENV_DIR}"
    uv venv --python "${PYTHON_CMD}" "${VENV_DIR}"
  fi
fi

# shellcheck disable=SC1090
source "${VENV_DIR}/bin/activate"

echo "[INFO] Installing/Updating Infinity dependencies..."
# Into this venv's own interpreter, whatever UV_PYTHON / UV_SYSTEM_PYTHON the
# shell sets. uv seeds no setuptools or wheel.
venv_pip() { local cmd="$1"; shift; uv pip "$cmd" --python "${VENV_DIR}/bin/python" "$@"; }
venv_pip install --upgrade setuptools wheel
if [[ "${INFINITY_ENGINE}" == "torch" ]]; then
  venv_pip install --upgrade "infinity-emb[server,torch]>=0.0.77"
elif [[ "${INFINITY_ENGINE}" == "ctranslate2" ]]; then
  venv_pip install --upgrade "infinity-emb[server,ct2]>=0.0.77"
elif [[ "${INFINITY_ENGINE}" == "optimum" ]]; then
  venv_pip install --upgrade "infinity-emb[server,optimum]>=0.0.77"
else
  venv_pip install --upgrade "infinity-emb[server]>=0.0.77"
fi
# infinity_emb CLI (Typer-based) is sensitive to newer Click behavior.
# Pin a known-compatible CLI stack to avoid runtime parser crashes.
venv_pip install --upgrade "click<8.2" "typer<0.13"

if [[ "${INFINITY_ENGINE}" != "optimum" ]]; then
  # For torch/ctranslate2 engines, avoid optimum.bettertransformer import traps
  # and keep a recent transformers for newer architectures like qwen3.
  venv_pip uninstall optimum optimum-onnx >/dev/null 2>&1 || true
  venv_pip install --upgrade "transformers>=4.57,<5"
fi

CMD=(
  infinity_emb
  v2
  --model-id "${INFINITY_MODEL_ID}"
  --port "${INFINITY_PORT}"
  --host "${INFINITY_HOST}"
  --url-prefix "${INFINITY_URL_PREFIX}"
  --engine "${INFINITY_ENGINE}"
  --no-bettertransformer
)

if [[ -n "${INFINITY_API_KEY}" ]]; then
  CMD+=(--api-key "${INFINITY_API_KEY}")
fi

echo "[INFO] Starting Infinity"
echo "[INFO] model_id=${INFINITY_MODEL_ID}"
echo "[INFO] endpoint=http://${INFINITY_HOST}:${INFINITY_PORT}${INFINITY_URL_PREFIX}"
echo "[INFO] health=http://${INFINITY_HOST}:${INFINITY_PORT}/health"
echo "[INFO] models=http://${INFINITY_HOST}:${INFINITY_PORT}${INFINITY_URL_PREFIX}/models"
echo "[INFO] embeddings=http://${INFINITY_HOST}:${INFINITY_PORT}${INFINITY_URL_PREFIX}/embeddings"

exec "${CMD[@]}"
