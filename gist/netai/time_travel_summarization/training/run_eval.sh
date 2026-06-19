#!/usr/bin/env bash
# Evaluate a model on the held-out test clips (run ON the L40, after training).
#
# Runs `swift infer` over test.jsonl, converts the result to preds.json, then calls
# utils.compare_results for STRICT (exact id-set per HH:MM:SS) + RELAXED (collision
# presence) metrics. Run it once for the base model and once for the LoRA adapter,
# then compare the two reports.
#
# Frame budget MUST match training/inference (VSS N=20) -> NFRAMES pinned below.
#
# Examples:
#   # base model (no adapter):
#   DATA=artifacts/dataset LABEL=base bash training/run_eval.sh
#   # LoRA adapter:
#   DATA=artifacts/dataset LABEL=lora ADAPTER=artifacts/lora_qwen3vl/checkpoint-XXX \
#     bash training/run_eval.sh
set -euo pipefail

DATA="${DATA:-artifacts/dataset}"
MODEL="${MODEL:-Qwen/Qwen3-VL-8B-Instruct}"
ADAPTER="${ADAPTER:-}"                       # empty -> evaluate the base model
LABEL="${LABEL:-model}"
OUT="${OUT:-artifacts/eval}"
SYSTEM="$(cat "${DATA}/system_prompt.txt")"

export NFRAMES="${NFRAMES:-20}"              # frames/clip the VLM sees (train==infer)
export VIDEO_MAX_PIXELS="${VIDEO_MAX_PIXELS:-$((720 * 480))}"

mkdir -p "${OUT}"
INFER="${OUT}/infer_${LABEL}.jsonl"
PREDS="${OUT}/preds_${LABEL}.json"

# ---- inference ------------------------------------------------------------ #
ADAPTER_ARGS=()
[[ -n "${ADAPTER}" ]] && ADAPTER_ARGS=(--adapters "${ADAPTER}")

swift infer \
    --model "${MODEL}" \
    "${ADAPTER_ARGS[@]}" \
    --val_dataset "${DATA}/test.jsonl" \
    --system "${SYSTEM}" \
    --result_path "${INFER}" \
    --max_new_tokens 256 \
    --temperature 0

# ---- convert + score ------------------------------------------------------ #
python -m utils.swift_infer_to_preds \
    --test-jsonl "${DATA}/test.jsonl" \
    --infer-result "${INFER}" \
    --out "${PREDS}"

python -m utils.compare_results \
    --clips-gt "${DATA}/test_gt.json" \
    --clips-pred "${PREDS}" \
    --label "${LABEL}"

echo "Eval done for '${LABEL}'. preds=${PREDS}"
echo "Compare base vs lora by running this twice (LABEL=base, then LABEL=lora ADAPTER=...)."
