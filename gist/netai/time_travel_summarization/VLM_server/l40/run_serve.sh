#!/usr/bin/env bash
# vLLM 서빙 제어 러너 (Docker 기반) — serve_start | serve_stop (JOB_TYPE으로 분기).
#
# 설계: vLLM을 공식 이미지(vllm/vllm-openai) 컨테이너로 띄운다. 회사 표준(컨테이너 배포)
# 정합 + 호스트에 vllm 설치 불필요. 컨테이너는 러너가 끝나도 지속(docker run -d)하며
# 큐를 막지 않는다. GPU 역할 분리: job_api가 SERVE_GPU를 강제 지정 → --gpus device=$GPU.
# 보안: 포트를 127.0.0.1에만 매핑 → 원격은 SSH 터널로만 접근(run_api.sh와 동일 모델).
#
# 입력: JOB_ID(필수), JOB_TYPE(serve_start|serve_stop), GPU(기본 0),
#       MODEL_PATH(start 필수 — merge_lora 산출 디렉토리),
#       PORT(기본 38011), NUM_FRAMES(기본 20 — 학습 프레임 예산과 정합, 일지 #6),
#       SERVED_NAME(기본 Qwen3-VL-8B-Instruct — GUI 모델명과 일치해야 404 안 남),
#       VLLM_IMAGE(기본 vllm/vllm-openai:latest — Qwen3-VL 지원 태그여야 함),
#       CONTAINER(기본 ttsum-vllm), READY_BOUND(기동 대기 상한초, 기본 900)
# 상태: $EXT_ROOT/artifacts/jobs/$JOB_ID/status (state=running|done|failed)
# 상주 상태: $EXT_ROOT/artifacts/serve/{container, serve.info, serve.log}
#
# L40 전제(반드시 확인): docker 설치 + netai가 docker 실행 권한(docker 그룹),
#   nvidia-container-toolkit(--gpus 동작), VLLM_IMAGE가 로컬에 있거나 pull 가능.
set -euo pipefail

JOB_ID="${JOB_ID:?JOB_ID 필요}"
JOB_TYPE="${JOB_TYPE:?serve_start|serve_stop}"
GPU="${GPU:-0}"
MODEL_PATH="${MODEL_PATH:-}"
PORT="${PORT:-38011}"
NUM_FRAMES="${NUM_FRAMES:-20}"
# 병합 모델 기본 컨텍스트(262144)는 KV 캐시가 GPU 메모리를 초과해 기동 실패.
# 2초/20프레임 클립엔 과대 → 축소(32768이면 KV ~4.5GiB, 여유롭게 적재).
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
SERVED_NAME="${SERVED_NAME:-Qwen3-VL-8B-Instruct}"
VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:latest}"
CONTAINER="${CONTAINER:-ttsum-vllm}"
READY_BOUND="${READY_BOUND:-900}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
# GUI/REST가 보낸 '~/...' 경로는 리터럴로 도착 → 서버측 $HOME으로 확장
MODEL_PATH="${MODEL_PATH/#\~/$HOME}"
JOB_DIR="$EXT_ROOT/artifacts/jobs/$JOB_ID"
STATUS="$JOB_DIR/status"
SERVE_DIR="$EXT_ROOT/artifacts/serve"
CID_FILE="$SERVE_DIR/container"
INFO_FILE="$SERVE_DIR/serve.info"
SERVE_LOG="$SERVE_DIR/serve.log"
mkdir -p "$JOB_DIR" "$SERVE_DIR"

write_status() {  # write_status <state> [note]
  { echo "state=$1"; echo "job_id=$JOB_ID"; echo "job_type=$JOB_TYPE";
    echo "port=$PORT"; [ -n "${2:-}" ] && echo "note=$2";
    echo "updated=$(date -Is)"; } > "$STATUS.tmp"
  mv "$STATUS.tmp" "$STATUS"
}

container_running() {  # 살아있는 컨테이너 이름 출력, 없으면 빈 문자열
  docker ps --filter "name=^/${CONTAINER}$" --filter "status=running" \
    --format '{{.Names}}' 2>/dev/null
}

case "$JOB_TYPE" in
  serve_start)
    [ -n "$MODEL_PATH" ] || { write_status failed "MODEL_PATH 필요"; exit 2; }
    command -v docker >/dev/null 2>&1 || {
      write_status failed "docker 없음 — L40에 docker 설치·권한 필요"
      echo "ERROR: docker 명령 없음"; exit 2; }
    if [ -n "$(container_running)" ]; then
      # 같은 모델이면 멱등 성공, 다른 모델이면 실패(암묵 교체 방지 — stop 먼저)
      RUNNING_MODEL="$(grep '^model_path=' "$INFO_FILE" 2>/dev/null | cut -d= -f2- || true)"
      if [ "$RUNNING_MODEL" = "$MODEL_PATH" ]; then
        write_status done "already running ($CONTAINER)"
        echo "[serve $JOB_ID] already running (container $CONTAINER)"; exit 0
      fi
      write_status failed "different model running ($RUNNING_MODEL) — serve_stop 먼저"
      echo "[serve $JOB_ID] FAILED: 다른 모델 서빙 중 ($RUNNING_MODEL)"; exit 1
    fi
    # 정지 상태로 남은 동명 컨테이너 정리
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    write_status running "starting container"
    echo "[serve $JOB_ID] docker run image=$VLLM_IMAGE model=$MODEL_PATH gpu=$GPU port=$PORT"
    # -p 127.0.0.1: → 호스트 로컬에만 노출(SSH 터널 전제). --ipc=host → vLLM 공유메모리.
    # 모델은 동일 경로로 read-only 마운트 → 컨테이너 안 --model 경로가 그대로 유효.
    # OFFLINE env → 자기완결 병합본이라 허브 조회 불필요(다운로드 지연·실패 회피).
    # --served-model-name: 클라이언트 model 필드를 병합 경로와 무관하게 고정.
    # --media-io-kwargs num_frames: 학습(NFRAMES=20)과 동일해야 train==infer 정합.
    if ! CID="$(docker run -d --name "$CONTAINER" \
        --gpus "device=${GPU}" --ipc=host \
        -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 \
        -v "${MODEL_PATH}:${MODEL_PATH}:ro" \
        -p "127.0.0.1:${PORT}:${PORT}" \
        "$VLLM_IMAGE" \
        --model "$MODEL_PATH" \
        --host 0.0.0.0 --port "$PORT" \
        --served-model-name "$SERVED_NAME" \
        --max-model-len "$MAX_MODEL_LEN" \
        --media-io-kwargs "{\"video\": {\"num_frames\": $NUM_FRAMES}}" \
        2>>"$SERVE_LOG")"; then
      write_status failed "docker run 실패 — serve.log 확인"
      echo "[serve $JOB_ID] docker run 실패 — $SERVE_LOG"; exit 1
    fi
    echo "$CONTAINER" > "$CID_FILE"
    { echo "model_path=$MODEL_PATH"; echo "gpu=$GPU"; echo "port=$PORT";
      echo "num_frames=$NUM_FRAMES"; echo "image=$VLLM_IMAGE";
      echo "container=$CONTAINER"; echo "cid=$CID"; echo "started=$(date -Is)"; } > "$INFO_FILE"
    # 준비 확인: OpenAI 호환 /v1/models 응답까지 (이미지 pull + 모델 로드 수 분)
    START=$SECONDS
    while (( SECONDS - START < READY_BOUND )); do
      if [ -z "$(container_running)" ]; then
        docker logs "$CONTAINER" >>"$SERVE_LOG" 2>&1 || true
        write_status failed "container exited during startup — serve.log"
        echo "[serve $JOB_ID] FAILED (컨테이너 종료) — $SERVE_LOG"; exit 1
      fi
      if curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null 2>&1; then
        write_status done "serving ($CONTAINER)"
        echo "[serve $JOB_ID] READY container=$CONTAINER port=$PORT"; exit 0
      fi
      sleep 5
    done
    docker logs "$CONTAINER" >>"$SERVE_LOG" 2>&1 || true
    write_status failed "not ready within ${READY_BOUND}s"
    echo "[serve $JOB_ID] FAILED (기동 시간 초과)"; exit 1
    ;;
  serve_stop)
    if [ -z "$(container_running)" ]; then  # 멱등: 안 떠 있으면 성공 처리
      docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
      rm -f "$CID_FILE"; write_status done "not running"
      echo "[serve $JOB_ID] not running"; exit 0
    fi
    write_status running "stopping $CONTAINER"
    docker stop "$CONTAINER" >/dev/null 2>&1 || true
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    rm -f "$CID_FILE"
    write_status done "stopped $CONTAINER"
    echo "[serve $JOB_ID] STOPPED ($CONTAINER)"
    ;;
  *)
    write_status failed "unknown JOB_TYPE=$JOB_TYPE"
    echo "ERROR: unknown JOB_TYPE=$JOB_TYPE"; exit 2
    ;;
esac
