---
license: apache-2.0
base_model: Qwen/Qwen3-VL-8B-Instruct
library_name: peft
pipeline_tag: video-text-to-text
tags:
- lora
- qwen3-vl
- video
- event-detection
- collision-detection
- digital-twin
- omniverse
---

# Qwen3-VL-8B BEV Collision Detection (LoRA)

LoRA adapter for **Qwen/Qwen3-VL-8B-Instruct** that detects **object collision events in bird's-eye-view (BEV) digital-twin videos** and reports them as structured JSON with on-screen timestamps and object IDs.

Trained as part of an end-to-end spatiotemporal analysis platform: physics-based data generation in NVIDIA Omniverse (PhysX wander + contact reports) → MinIO data lake → LoRA fine-tuning → vLLM serving → digital-twin event search & replay.
Project repository: <https://github.com/ShinWonJune/gist.netai.time_travel_summarization>

## Task

Input: a **2-second BEV video clip** (20 frames) rendered from a digital twin. Numbered white circular labels mark each moving object; a `HH:MM:SS` clock is burned into the bottom-right corner.

Output: a JSON list of collision events, keyed by the **timestamp shown on screen**:

```json
[
  {"03:44:27": [1, 4]},
  {"03:44:29": [2, 3]}
]
```

An empty list `[]` means no collision in the clip.

## Evaluation

Held-out test clips (natural class distribution, temperature 0). Two metrics:

- **STRICT** — exact match of `HH:MM:SS` *and* the full object-ID set (detection + attribution)
- **RELAXED** — binary collision presence per 2-s clip (detection only)

| Model | positive clips trained | STRICT F1 | RELAXED F1 |
|---|---|---|---|
| base (zero-shot) | 0 | 0.065 | 0.338 |
| LoRA v1 | 136 | 0.250 | 0.548 |
| LoRA v2 | ~450 | 0.503 | 0.784 |
| **LoRA v3 (this adapter)** | **~900** | **0.699** | **0.837** |

Data scaling was the dominant factor across v1→v3 (same recipe, more episodes).
Error analysis: 57.7% of v3 false negatives are **exactly 1 second off** on an otherwise correct event; with a ±1 s tolerance STRICT F1 rises to **0.791** — residual error is dominated by second-boundary alignment, not detection failure.

## Training

- Framework: [ms-swift](https://github.com/modelscope/ms-swift) (`swift sft`), single NVIDIA L40
- LoRA: rank 16, alpha 32, `freeze_vit`, bf16, gradient accumulation 16
- Frame budget: **20 frames per 2-s clip** (`NFRAMES=20`) — must match at inference
- This checkpoint: epoch 1 (`checkpoint-133`, best eval loss 0.0759; epoch 2 showed mild overfitting)
- Data: headless Omniverse Kit captures of physics-driven agents (collision ground truth from PhysX contact reports, labels time-aligned to a simulation master clock), built into 2-s clips with a 1:1 negative ratio

## How to use

> **The prompt is part of the model contract.** This adapter was trained with one specific system/user prompt pair (`twin_view`). Using a different system prompt can degrade output or cause degenerate repetition. The exact prompts ship in the project repo (`vlm_client/prompts.py`, preset `twin_view`) and are summarized below.

<details>
<summary>Training/inference system prompt (twin_view) — click to expand</summary>

```
You are a vision-language reasoning model specialized in video understanding.
You are given a video generated from a digital twin simulation viewed from a bird's-eye view (BEV).
In the video:
- Multiple numbered objects move freely in a shared space.
- Each object has a visible numeric label.
- A timestamp (date and time) is displayed at the bottom-right corner of the video.
- Occasionally, objects visually overlap or intersect.

Your task is to detect all frames or time periods where two or more numbered objects overlap (i.e., their bounding areas visually intersect).

When an overlap occurs, extract and return:
1. The exact timestamp displayed on screen.
2. The list of object numbers involved in the overlap.

Format the final answer as a structured JSON array with this schema:
[
  {"HH:MM:SS": [object_number_1, object_number_2, ...]},
...
]

Be concise, accurate, and consistent. Only report actual overlaps (not near contacts).
If multiple overlaps occur at the same timestamp, list them all in the same entry.
Do not include any explanatory text or reasoning in the output.
```
</details>

### A) Adapter inference (ms-swift)

```bash
NFRAMES=20 VIDEO_MAX_PIXELS=$((720*480)) swift infer \
  --model Qwen/Qwen3-VL-8B-Instruct \
  --adapters <this-repo-or-local-path> \
  --system "$(cat system_prompt.txt)" \
  --max_new_tokens 256 --temperature 0
```

### B) Merge + vLLM serving (OpenAI-compatible)

```bash
USE_HF=1 swift export --adapters <adapter-path> --merge_lora true

docker run -d --gpus device=0 --ipc=host \
  -v <merged-path>:<merged-path>:ro -p 127.0.0.1:38011:38011 \
  vllm/vllm-openai:latest \
  --model <merged-path> --host 0.0.0.0 --port 38011 \
  --served-model-name Qwen3-VL-8B-Instruct \
  --max-model-len 32768 \
  --media-io-kwargs '{"video": {"num_frames": 20}}'
```

`--max-model-len 32768` keeps the KV cache within a 48 GB GPU (the model's 262 144 default needs ~36 GiB of KV cache alone); `num_frames: 20` preserves the train==inference frame budget.
Sanity gate after any merge/deploy: ask `"What is 2+2?"` — a healthy checkpoint answers `Four`; degenerate repetition means a broken merge.

## Limitations

- Trained on a **single collision choreography** (free-wander agents in one indoor scene); behavioral generalization is unmeasured.
- Reads the burned-in clock: timestamp accuracy is limited by 1-second overlay precision (see ±1 s analysis above).
- Collision labels are per-object (pairwise partner identity not supervised).
- Synthetic BEV domain only — no real CCTV footage in training.

## License & attribution

Apache-2.0, same as the base model **Qwen/Qwen3-VL-8B-Instruct** (© Alibaba Cloud / Qwen team). This repository distributes a LoRA adapter fine-tuned on synthetic digital-twin data; base weights are not redistributed here.
