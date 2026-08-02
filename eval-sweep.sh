#!/usr/bin/env bash
# eval-sweep.sh - top up every bundled example eval to 5 runs per task for
# each local LM Studio model, grading as we go.
#
# Usage: ./eval-sweep.sh
#
# Rerunnable: smevals' -n top-up semantics mean a re-run only executes the
# shortfall, so interrupt and re-invoke freely. Models are unloaded between
# phases so LM Studio JIT-loads exactly one model at a time. The pelican
# eval is run ungraded and graded in a single pass at the end, so the
# vision judge model loads once instead of swapping after every run.
set -uo pipefail
cd "$(dirname "$0")"

LMS="$HOME/.lmstudio/bin/lms"
# LM Studio models JIT-load locally; the luna entries are OpenAI-hosted
# aliases registered in llm with baked-in reasoning_effort defaults.
MODELS=(
    "qwythos-27b-v1"
    "ornith-1.0-35b"
    "google/gemma-4-e4b"
    "qwen/qwen3.6-27b"
    "gpt-5.6-luna-xhigh"
    "gpt-5.6-luna-low"
)
FAILURES=0

# Local models load explicitly with a generous context length: JIT loads
# default to an 8192-token budget, which thinking-heavy models exhaust on
# reasoning before emitting anything (finish_reason: length, empty output).
CONTEXT_LENGTH=32768

for model in "${MODELS[@]}"; do
    echo "=============== model: $model ==============="
    "$LMS" unload --all
    if [[ "$model" != gpt-5.6-* ]]; then
        "$LMS" load "$model" -c "$CONTEXT_LENGTH" -y || { FAILURES=$((FAILURES+1)); continue; }
    fi
    for eval in haiku markdown-tables code-authoring/interval-set code-authoring/usage-billing; do
        echo "--- $eval / $model ---"
        uv run smevals run "examples/$eval" -m "$model" -n 5 -g || FAILURES=$((FAILURES+1))
    done
    echo "--- pelican-riding-a-bicycle / $model (grading deferred) ---"
    uv run smevals run examples/pelican-riding-a-bicycle -m "$model" -n 5 || FAILURES=$((FAILURES+1))
done

echo "=============== deferred judge grading ==============="
"$LMS" unload --all
uv run smevals grade examples/pelican-riding-a-bicycle -g local || FAILURES=$((FAILURES+1))
uv run smevals grade examples/pelican-riding-a-bicycle || FAILURES=$((FAILURES+1))
uv run smevals grade examples/haiku -g judge || FAILURES=$((FAILURES+1))
uv run smevals grade examples/code-authoring/interval-set -g judge || FAILURES=$((FAILURES+1))
uv run smevals grade examples/code-authoring/usage-billing -g judge || FAILURES=$((FAILURES+1))

echo "=============== sweep done: $FAILURES phase(s) had failures or failing grades ==============="
