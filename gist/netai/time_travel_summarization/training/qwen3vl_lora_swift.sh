#!/usr/bin/env bash
# LoRA fine-tuning of Qwen3-VL-8B-Instruct on BEV collision clips (ms-swift).
#
# Trains the model to read a 2s BEV clip (with a burned-in HH:MM:SS overlay) and
# output collision events as [{"HH:MM:SS": [ids]}], matching the inference task.
# Each training sample == one 2s clip == exactly one VSS inference chunk, so the
# frame budget below is pinned to what the VLM sees at inference (VSS N=20).
#
# Prereqs:
#   pip install "ms-swift" qwen-vl-utils decord     # + a recent transformers/accelerate
#   Build the dataset first:  python -m utils.build_dataset --episodes-dir ... --out-dir DATA
#
# NOTE: ms-swift CLI flags evolve between versions. This targets ms-swift 3.x.
#       Run `swift sft -h` to confirm flag names against your installed version.
set -euo pipefail

# ---- paths ---------------------------------------------------------------- #
DATA="${DATA:-artifacts/dataset}"          # output dir from build_dataset.py
OUTPUT="${OUTPUT:-artifacts/lora_qwen3vl}"
MODEL="${MODEL:-Qwen/Qwen3-VL-8B-Instruct}"
SYSTEM="$(cat "${DATA}/system_prompt.txt")"

# ---- video frame budget (MUST match inference) ---------------------------- #
# VSS feeds a FIXED 20 frames per 2s chunk -> train on the same 20 frames/clip.
export NFRAMES=20                           # force exactly 20 sampled frames per clip
export VIDEO_MAX_PIXELS=$((720 * 480))      # keep native 720x480; lower first if OOM
export SIZE_FACTOR=28                        # Qwen patch alignment (default)

# ---- train ---------------------------------------------------------------- #
swift sft \
    --model "${MODEL}" \
    --dataset "${DATA}/train.jsonl" \
    --val_dataset "${DATA}/val.jsonl" \
    --system "${SYSTEM}" \
    --train_type lora \
    --lora_rank 16 \
    --lora_alpha 32 \
    --lora_dropout 0.05 \
    --freeze_vit true \
    --target_modules all-linear \
    --torch_dtype bfloat16 \
    --num_train_epochs 2 \
    --per_device_train_batch_size 1 \
    --per_device_eval_batch_size 1 \
    --gradient_accumulation_steps 16 \
    --learning_rate 1e-4 \
    --lr_scheduler_type cosine \
    --warmup_ratio 0.05 \
    --gradient_checkpointing true \
    --eval_strategy epoch \
    --save_strategy epoch \
    --save_total_limit 2 \
    --logging_steps 5 \
    --dataloader_num_workers 4 \
    --output_dir "${OUTPUT}" \
    --seed 42

echo "LoRA adapter saved under ${OUTPUT}. Merge for serving with:"
echo "  swift export --adapters ${OUTPUT}/<checkpoint> --merge_lora true"

# --------------------------------------------------------------------------- #
# QLoRA fallback (if 40GB OOMs even at bs=1 + grad-ckpt): 4-bit base weights.
# Add these flags to the command above and drop --torch_dtype to bf16 compute:
#   --quant_method bnb --quant_bits 4 --bnb_4bit_compute_dtype bfloat16
# Further OOM levers (apply in order): lower VIDEO_MAX_PIXELS (e.g. 480*360),
#   NFRAMES (only if you ALSO lower VSS N to keep train==infer), lora_rank 8.
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Pipeline sanity (overfit) check — run BEFORE the real job:
#   head -n 50 ${DATA}/train.jsonl > ${DATA}/overfit.jsonl
#   swift sft --model ${MODEL} --dataset ${DATA}/overfit.jsonl --train_type lora \
#       --lora_rank 16 --num_train_epochs 30 --learning_rate 2e-4 --freeze_vit true \
#       --per_device_train_batch_size 1 --gradient_accumulation_steps 4 \
#       --output_dir artifacts/overfit_check --system "${SYSTEM}"
#   # Train loss should approach ~0 / metrics ~100% -> the data+pipeline are wired correctly.
# --------------------------------------------------------------------------- #
