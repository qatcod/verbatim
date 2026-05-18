#!/usr/bin/env bash
# Run verbatim against the bundled sample transcript.
# Requires: ANTHROPIC_API_KEY in env, and `pip install -e .` from the project root.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "ERROR: ANTHROPIC_API_KEY is not set." >&2
  echo "  export ANTHROPIC_API_KEY=sk-ant-..." >&2
  exit 1
fi

verbatim extract "${SCRIPT_DIR}/sample_transcript.txt" --output-dir "${SCRIPT_DIR}/out"

echo
echo "Output written to ${SCRIPT_DIR}/out/"
ls -la "${SCRIPT_DIR}/out/"
