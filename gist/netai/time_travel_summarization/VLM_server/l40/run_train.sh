#!/usr/bin/env bash
# 스펙(env) 구동 LoRA 학습 잡 러너 — run_job.sh와 동일한 status 파일 계약.
#
# 입력: JOB_ID(필수), DATASET(필수 — build_dataset 산출 디렉토리, train/val.jsonl 포함),
#       GPU(기본 1), TRAIN_OUTPUT(빈 값 = $HOME/wonjune/ttsum-data/lora_runs/$JOB_ID),
#       MODEL(기본 Qwen/Qwen3-VL-8B-Instruct), VENV(기본 $HOME/wonjune/venv)
# 상태: $EXT_ROOT/artifacts/jobs/$JOB_ID/status (state=running|done|failed)
#
# 학습 본체는 검증된 training/qwen3vl_lora_swift.sh를 그대로 호출(하이퍼파라미터
# 단일 소스 — rank16/alpha32/freeze_vit/bf16/accum16/2ep). 이 러너는 잡 프로토콜
# (status·로그·GPU 고정·환경)만 담당한다.
set -euo pipefail

JOB_ID="${JOB_ID:?JOB_ID 필요}"
DATASET="${DATASET:?DATASET 필요 (build_dataset 산출 디렉토리)}"
GPU="${GPU:-1}"
MODEL="${MODEL:-Qwen/Qwen3-VL-8B-Instruct}"
TRAIN_OUTPUT="${TRAIN_OUTPUT:-}"
VENV="${VENV:-$HOME/wonjune/venv}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TRAIN_SH="$EXT_ROOT/gist/netai/time_travel_summarization/training/qwen3vl_lora_swift.sh"

# GUI/REST가 보낸 '~/...' 경로는 리터럴로 도착 → 서버측 $HOME으로 확장
DATASET="${DATASET/#\~/$HOME}"
TRAIN_OUTPUT="${TRAIN_OUTPUT/#\~/$HOME}"

JOB_DIR="$EXT_ROOT/artifacts/jobs/$JOB_ID"
LOG="$JOB_DIR/job.log"
STATUS="$JOB_DIR/status"
mkdir -p "$JOB_DIR"
[ -n "$TRAIN_OUTPUT" ] || TRAIN_OUTPUT="$HOME/wonjune/ttsum-data/lora_runs/$JOB_ID"
mkdir -p "$TRAIN_OUTPUT"

write_status() {  # write_status <state>
  { echo "state=$1"; echo "job_id=$JOB_ID"; echo "job_type=train";
    echo "dataset=$DATASET"; echo "output=$TRAIN_OUTPUT";
    echo "updated=$(date -Is)"; } > "$STATUS.tmp"
  mv "$STATUS.tmp" "$STATUS"   # 원자적 교체 (폴링 규약)
}

[ -f "$DATASET/train.jsonl" ] || {
  write_status failed; echo "ERROR: $DATASET/train.jsonl 없음"; exit 2; }

# 자격증명 + 학습 환경: venv(ms-swift) + HF 소스 고정
# (USE_HF=1 — 기본 ModelScope로 새면 모델 전체 재다운로드, 일지 #7)
if [ -f "$HOME/wonjune/.env.l40" ]; then set -a; . "$HOME/wonjune/.env.l40"; set +a; fi
[ -f "$VENV/bin/activate" ] && . "$VENV/bin/activate"
export USE_HF=1

write_status running
echo "[train $JOB_ID] dataset=$DATASET gpu=$GPU out=$TRAIN_OUTPUT"
echo "[train $JOB_ID] log: $LOG"
if CUDA_VISIBLE_DEVICES="$GPU" DATA="$DATASET" OUTPUT="$TRAIN_OUTPUT" MODEL="$MODEL" \
    bash "$TRAIN_SH" > "$LOG" 2>&1; then
  write_status done
  echo "[train $JOB_ID] DONE -> $TRAIN_OUTPUT"
else
  write_status failed
  echo "[train $JOB_ID] FAILED — 로그: $LOG"
  exit 1
fi
