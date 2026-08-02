#!/usr/bin/env bash
# sweep-code-authoring.sh - top up the two code-authoring evals to 5 runs
# per model for every local LM Studio model plus gpt-5.6-luna at low and
# xhigh effort, grading deterministically inline, then judge-grade both.
#
# Usage: ./sweep-code-authoring.sh
#
# Rerunnable: -n top-up semantics execute only the shortfall. Local models
# are unloaded between phases so LM Studio JIT-loads one at a time; the
# hosted luna aliases carry baked-in reasoning_effort defaults in llm.
set -uo pipefail
cd "$(dirname "$0")"

LMS="$HOME/.lmstudio/bin/lms"
MODELS=(
    "qwythos-27b-v1"
    "ornith-1.0-35b"
    "qwen/qwen3.6-27b"
    "google/gemma-4-e4b"
    "google/gemma-4-31b-qat"
    "prism-ml/bonsai-27b"
    "qwopus3.6-27b-fusion"
    "gpt-5.6-luna-xhigh"
    "gpt-5.6-luna-low"
)
EVALS=(code-authoring/interval-set code-authoring/usage-billing)
FAILURES=0

# Local models must be loaded explicitly with a generous context length:
# JIT loads default to an 8192-token budget, which thinking-heavy models
# exhaust on reasoning before emitting any code (finish_reason: length,
# empty output).
CONTEXT_LENGTH=32768

for model in "${MODELS[@]}"; do
    echo "=============== model: $model ==============="
    "$LMS" unload --all
    if [[ "$model" != gpt-5.6-* ]]; then
        "$LMS" load "$model" -c "$CONTEXT_LENGTH" -y || { FAILURES=$((FAILURES+1)); continue; }
    fi
    for eval in "${EVALS[@]}"; do
        echo "--- $eval / $model ---"
        uv run smevals run "examples/$eval" -m "$model" -n 5 -g || FAILURES=$((FAILURES+1))
    done
done

echo "=============== judge grading (gpt-4.1) ==============="
"$LMS" unload --all
for eval in "${EVALS[@]}"; do
    uv run smevals grade "examples/$eval" -g judge || FAILURES=$((FAILURES+1))
done

echo "=============== sweep done: $FAILURES phase(s) had failures or failing grades ==============="
