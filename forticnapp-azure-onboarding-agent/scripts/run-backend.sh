#!/usr/bin/env bash
# Run the web chat app against one of several LLM backends, without having to
# remember/re-export the right env vars each time.
#
# Usage:
#   scripts/run-backend.sh bifrost
#   scripts/run-backend.sh llamacpp
#   scripts/run-backend.sh vllm
#   scripts/run-backend.sh <backend> -- <extra uvicorn args>
#
# Backend-specific overrides (optional, set before calling this script):
#   bifrost:  BIFROST_BASE_URL, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_DEFAULT_OPUS_MODEL
#   llamacpp: LLAMACPP_BASE_URL (default http://localhost:8080), LLAMACPP_MODEL
#   vllm:     VLLM_BASE_URL (default http://localhost:8000), VLLM_MODEL
set -euo pipefail
cd "$(dirname "$0")/.."

BACKEND="${1:-}"; shift || true
case "$BACKEND" in
  bifrost)
    export BACKEND=bifrost
    export LLM_API=anthropic
    export BIFROST_BASE_URL="${BIFROST_BASE_URL:-https://bifrost.fabriclab.ca/anthropic}"
    export DEFAULT_MODEL="${DEFAULT_MODEL:-${ANTHROPIC_DEFAULT_OPUS_MODEL:-qwen3.8-27b-anthropic}}"
    ;;
  llamacpp)
    export BACKEND=llamacpp
    export LLM_API=openai
    export BIFROST_BASE_URL="${LLAMACPP_BASE_URL:-http://localhost:8080}"
    export DEFAULT_MODEL="${LLAMACPP_MODEL:-bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M}"
    ;;
  vllm)
    export BACKEND=vllm
    export LLM_API=openai
    export BIFROST_BASE_URL="${VLLM_BASE_URL:-http://localhost:8000}"
    : "${VLLM_MODEL:?set VLLM_MODEL to the model id vLLM is serving (see curl \$VLLM_BASE_URL/v1/models)}"
    export DEFAULT_MODEL="$VLLM_MODEL"
    ;;
  *)
    echo "Usage: $0 bifrost|llamacpp|vllm [-- extra uvicorn args]" >&2
    exit 1
    ;;
esac

echo "Backend: $BACKEND"
echo "  LLM_API=$LLM_API"
echo "  BIFROST_BASE_URL=$BIFROST_BASE_URL"
echo "  DEFAULT_MODEL=$DEFAULT_MODEL"

exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 "$@"
