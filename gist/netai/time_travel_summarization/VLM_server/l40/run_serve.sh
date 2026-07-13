#!/usr/bin/env bash
# vLLM 서빙 제어 러너 — serve_start | serve_stop (JOB_TYPE으로 분기).
#
# 설계: "서빙 기동/중지"가 큐 잡이고, vLLM 상주 프로세스는 러너 밖에서 산다
# (setsid 분리 — 러너가 끝나도 서빙은 지속, 큐를 막지 않음).
# GPU 역할 분리: job_api가 SERVE_GPU를 강제 지정해 내려준다(잡 GPU와 불간섭).
# 보안: run_api.sh와 동일 하이브리드 — 127.0.0.1 바인딩 + SSH 터널로만 접근.
#
# 입력: JOB_ID(필수), JOB_TYPE(serve_start|serve_stop), GPU(기본 0),
#       MODEL_PATH(start 필수 — merge_lora 산출 디렉토리 또는 HF id),
#       PORT(기본 38011), NUM_FRAMES(기본 20 — 학습 프레임 예산과 정합, 일지 #6),
#       VENV(기본 $HOME/wonjune/venv), READY_BOUND(기동 대기 상한초, 기본 900)
# 주의: serve_start가 준비 대기 동안 SERVE_GPU 큐를 점유하므로, 뒤이어 제출한
#       serve_stop은 그 뒤에 실행된다(선점 불가). READY_BOUND가 점유 상한.
# 상태: $EXT_ROOT/artifacts/jobs/$JOB_ID/status (state=running|done|failed)
# 상주 상태: $EXT_ROOT/artifacts/serve/{vllm.pid, serve.info, serve.log}
set -euo pipefail

JOB_ID="${JOB_ID:?JOB_ID 필요}"
JOB_TYPE="${JOB_TYPE:?serve_start|serve_stop}"
GPU="${GPU:-0}"
MODEL_PATH="${MODEL_PATH:-}"
PORT="${PORT:-38011}"
NUM_FRAMES="${NUM_FRAMES:-20}"
VENV="${VENV:-$HOME/wonjune/venv}"
READY_BOUND="${READY_BOUND:-900}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
# GUI/REST가 보낸 '~/...' 경로는 리터럴로 도착 → 서버측 $HOME으로 확장
MODEL_PATH="${MODEL_PATH/#\~/$HOME}"
JOB_DIR="$EXT_ROOT/artifacts/jobs/$JOB_ID"
STATUS="$JOB_DIR/status"
SERVE_DIR="$EXT_ROOT/artifacts/serve"
PID_FILE="$SERVE_DIR/vllm.pid"
INFO_FILE="$SERVE_DIR/serve.info"
SERVE_LOG="$SERVE_DIR/serve.log"
mkdir -p "$JOB_DIR" "$SERVE_DIR"

write_status() {  # write_status <state> [note]
  { echo "state=$1"; echo "job_id=$JOB_ID"; echo "job_type=$JOB_TYPE";
    echo "port=$PORT"; [ -n "${2:-}" ] && echo "note=$2";
    echo "updated=$(date -Is)"; } > "$STATUS.tmp"
  mv "$STATUS.tmp" "$STATUS"
}

serving_pid() {  # 살아 있는 vLLM PID를 출력, 없으면 빈 문자열
  if [ -f "$PID_FILE" ]; then
    local pid; pid="$(cat "$PID_FILE")"
    if kill -0 "$pid" 2>/dev/null; then echo "$pid"; return; fi
  fi
  echo ""
}

case "$JOB_TYPE" in
  serve_start)
    [ -n "$MODEL_PATH" ] || { write_status failed "MODEL_PATH 필요"; exit 2; }
    EXISTING="$(serving_pid)"
    if [ -n "$EXISTING" ]; then
      # 같은 모델이면 멱등 성공, 다른 모델이면 실패(암묵 교체 방지 — stop 먼저)
      RUNNING_MODEL="$(grep '^model_path=' "$INFO_FILE" 2>/dev/null | cut -d= -f2- || true)"
      if [ "$RUNNING_MODEL" = "$MODEL_PATH" ]; then
        write_status done "already running (pid $EXISTING)"
        echo "[serve $JOB_ID] already running (pid $EXISTING)"; exit 0
      fi
      write_status failed "different model running ($RUNNING_MODEL) — serve_stop 먼저"
      echo "[serve $JOB_ID] FAILED: 다른 모델 서빙 중 ($RUNNING_MODEL)"; exit 1
    fi
    if [ -f "$HOME/wonjune/.env.l40" ]; then set -a; . "$HOME/wonjune/.env.l40"; set +a; fi
    [ -f "$VENV/bin/activate" ] && . "$VENV/bin/activate"
    write_status running "loading model"
    echo "[serve $JOB_ID] starting vLLM model=$MODEL_PATH gpu=$GPU port=$PORT"
    # --media-io-kwargs num_frames: 학습(NFRAMES=20)과 동일해야 train==infer 정합
    # --served-model-name: 클라이언트가 보내는 model 필드를 병합 경로와 무관하게 고정
    #   (vlm_client GUI의 모델명 "Qwen3-VL-8B-Instruct"와 일치해야 404가 안 남)
    SERVED_NAME="${SERVED_NAME:-Qwen3-VL-8B-Instruct}"
    CUDA_VISIBLE_DEVICES="$GPU" setsid vllm serve "$MODEL_PATH" \
      --host 127.0.0.1 --port "$PORT" \
      --served-model-name "$SERVED_NAME" \
      --media-io-kwargs "{\"video\": {\"num_frames\": $NUM_FRAMES}}" \
      > "$SERVE_LOG" 2>&1 < /dev/null &
    VLLM_PID=$!
    echo "$VLLM_PID" > "$PID_FILE"
    { echo "model_path=$MODEL_PATH"; echo "gpu=$GPU"; echo "port=$PORT";
      echo "num_frames=$NUM_FRAMES"; echo "started=$(date -Is)"; } > "$INFO_FILE"
    # 준비 확인: OpenAI 호환 /v1/models가 응답할 때까지 (모델 로드 수 분)
    START=$SECONDS
    while (( SECONDS - START < READY_BOUND )); do
      if ! kill -0 "$VLLM_PID" 2>/dev/null; then
        write_status failed "vLLM died during startup — see serve.log"
        echo "[serve $JOB_ID] FAILED (프로세스 사망) — $SERVE_LOG"; exit 1
      fi
      if curl -sf "http://127.0.0.1:$PORT/v1/models" > /dev/null 2>&1; then
        write_status done "serving pid $VLLM_PID"
        echo "[serve $JOB_ID] READY pid=$VLLM_PID port=$PORT"; exit 0
      fi
      sleep 5
    done
    kill -TERM "$VLLM_PID" 2>/dev/null || true
    write_status failed "not ready within ${READY_BOUND}s"
    echo "[serve $JOB_ID] FAILED (기동 시간 초과)"; exit 1
    ;;
  serve_stop)
    EXISTING="$(serving_pid)"
    if [ -z "$EXISTING" ]; then  # 멱등: 안 떠 있으면 성공 처리
      rm -f "$PID_FILE"
      write_status done "not running"
      echo "[serve $JOB_ID] not running"; exit 0
    fi
    write_status running "stopping pid $EXISTING"
    kill -TERM "$EXISTING" 2>/dev/null || true
    for _ in $(seq 1 30); do
      kill -0 "$EXISTING" 2>/dev/null || break
      sleep 1
    done
    kill -0 "$EXISTING" 2>/dev/null && kill -KILL "$EXISTING" 2>/dev/null || true
    rm -f "$PID_FILE"
    write_status done "stopped pid $EXISTING"
    echo "[serve $JOB_ID] STOPPED (pid $EXISTING)"
    ;;
  *)
    write_status failed "unknown JOB_TYPE=$JOB_TYPE"
    echo "ERROR: unknown JOB_TYPE=$JOB_TYPE"; exit 2
    ;;
esac
