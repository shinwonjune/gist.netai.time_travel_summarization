#!/usr/bin/env bash
# 스펙(env) 구동 재연 렌더 잡 러너 — run_job.sh의 재연판(headless replay capture).
#
# 입력: JobSpec.to_env()가 렌더링한 env (JOB_ID, REPLAY_START, REPLAY_END, DATA_URI,
#       CAMERA, RENDER_FPS, STAGE, APP_KIT, GPU, UPLOAD_URI)
#       - REPLAY_START/END: ISO "YYYY-MM-DD HH:MM:SS" (필수)
#       - DATA_URI: '://' 포함이면 트레이스 URI(--data-path), 아니면 레이크 데이터셋(--lake-dataset),
#                   빈 값이면 config 기본 소스
# 상태: $EXT_ROOT/artifacts/jobs/$JOB_ID/status (state=running|done|failed, note, updated)
#
# 공용 서버 수칙(run_job.sh와 동일): 산출물 repo 안, GPU 1개 격리, 완료 마커 워치독.
set -euo pipefail

JOB_ID="${JOB_ID:?JOB_ID 필요}"
REPLAY_START="${REPLAY_START:-}"
REPLAY_END="${REPLAY_END:-}"
DATA_URI="${DATA_URI:-}"
CAMERA="${CAMERA:-}"
RENDER_FPS="${RENDER_FPS:-30}"
STAGE="${STAGE:-}"
GPU="${GPU:-1}"
UPLOAD_URI="${UPLOAD_URI:-}"

# ---- 경로 해석 (run_job.sh와 동일 규칙) ------------------------------------ #
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
EXT_PKG="$EXT_ROOT/gist/netai/time_travel_summarization"
REPLAY="$EXT_PKG/automation/replay_range.py"

# ---- 잡 상태 파일을 먼저 만든다 (조기 실패도 status·log에 남게) -------------- #
JOB_DIR="$EXT_ROOT/artifacts/jobs/$JOB_ID"
OUT="$EXT_ROOT/artifacts/replays/$JOB_ID"
LOG="$JOB_DIR/job.log"
STATUS="$JOB_DIR/status"
mkdir -p "$JOB_DIR" "$OUT"

write_status() {  # write_status <state> [note]
  { echo "state=$1"; echo "job_id=$JOB_ID"; echo "job_type=replay";
    [ -n "${2:-}" ] && echo "note=$2";
    echo "updated=$(date -Is)"; } > "$STATUS.tmp"
  mv "$STATUS.tmp" "$STATUS"   # 원자적 교체 — 폴링이 반쯤 쓰인 파일을 읽지 않게
}
fail() {  # fail <note> — 조기 실패를 status+log 양쪽에 남기고 종료
  echo "ERROR: $1" | tee -a "$LOG"
  write_status failed "$1"
  exit 2
}
write_status running "resolving kit/app"

[ -n "$REPLAY_START" ] || fail "REPLAY_START 필요"
[ -n "$REPLAY_END" ] || fail "REPLAY_END 필요"

KIT_ROOT="${KIT_ROOT:-}"
if [ -z "$KIT_ROOT" ]; then
  for cand in "$EXT_ROOT/../../.." "$HOME/wonjune/kit-app-template" "$HOME/kit-app-template"; do
    if [ -x "$cand/_build/linux-x86_64/release/kit/kit" ]; then
      KIT_ROOT="$(cd "$cand" && pwd)"; break
    fi
  done
fi
[ -n "$KIT_ROOT" ] || fail "kit 빌드 없음 — KIT_ROOT 지정"
KIT="$KIT_ROOT/_build/linux-x86_64/release/kit/kit"
APPS_DIR="$KIT_ROOT/_build/linux-x86_64/release/apps"
APP_KIT="${APP_KIT:-}"
if [ -n "$APP_KIT" ]; then
  case "$APP_KIT" in */*) APP="$APP_KIT" ;; *) APP="$APPS_DIR/${APP_KIT%.kit}.kit" ;; esac
else
  mapfile -t _apps < <(ls "$APPS_DIR"/*.kit 2>/dev/null || true)
  [ "${#_apps[@]}" -eq 1 ] || fail "APP_KIT 지정 필요 (앱 ${#_apps[@]}개)"
  APP="${_apps[0]}"
fi
[ -f "$APP" ] || fail "앱 없음: $APP"

# 자격증명 (무인 Nucleus/minIO)
if [ -f "$HOME/wonjune/.env.l40" ]; then
  set -a; . "$HOME/wonjune/.env.l40"; set +a
fi

write_status running

# ---- 재연 인자 조립 --------------------------------------------------------- #
# env는 최종적으로 --exec 문자열 안에서 셸이 재파싱하므로, 공백 포함 인자(시각)는
# 홑따옴표로 감싼다(replay_range.py argparse가 하나의 토큰으로 받게).
EXEC_ARGS="$REPLAY --replay-start '$REPLAY_START' --replay-end '$REPLAY_END' \
 --render-fps $RENDER_FPS --out $OUT --quit"
if [ -n "$DATA_URI" ]; then
  case "$DATA_URI" in
    *://*) EXEC_ARGS="$EXEC_ARGS --data-path '$DATA_URI'" ;;   # 트레이스 URI
    *)     EXEC_ARGS="$EXEC_ARGS --lake-dataset '$DATA_URI'" ;;  # 레이크 데이터셋 이름
  esac
fi
[ -n "$STAGE" ] && EXEC_ARGS="$EXEC_ARGS --stage $STAGE"
[ -n "$CAMERA" ] && EXEC_ARGS="$EXEC_ARGS --camera $CAMERA"
[ -n "$UPLOAD_URI" ] && EXEC_ARGS="$EXEC_ARGS --upload-uri $UPLOAD_URI"

# 워치독 상한: 재연 창(초) 계산 → (로드 180s + DUR×8 + 60) ×1.2 (보수적).
START_S=$(date -d "$REPLAY_START" +%s 2>/dev/null || echo 0)
END_S=$(date -d "$REPLAY_END" +%s 2>/dev/null || echo 0)
DUR=$(( END_S > START_S ? END_S - START_S : 60 ))
BOUND=$(( (180 + DUR * 8 + 60) * 12 / 10 ))

echo "[job $JOB_ID] replay ${REPLAY_START}..${REPLAY_END} (${DUR}s) gpu=$GPU bound=${BOUND}s"
echo "[job $JOB_ID] log: $LOG"

# 공용 kit 인자 (베어메탈/컨테이너 공통) — activeGpu만 분기별로 따로 붙인다
# (컨테이너 안에선 --gpus device=N이 그 GPU 하나를 인덱스 0으로 remap하므로
#  호스트 GPU 인덱스가 아니라 항상 0을 넘겨야 한다. docker/container_lib.sh 참고).
COMMON_ARGS=(--no-window \
  --ext-folder "$(dirname "$EXT_ROOT")" \
  --enable gist.netai.time_travel_summarization \
  --enable omni.replicator.core \
  --/app/settings/fabricDefaultStageFrameHistoryCount=3 \
  --/app/content/emptyStageOnStart=true)

if [ "${USE_CONTAINER:-0}" = "1" ]; then
  # shellcheck source=docker/container_lib.sh
  source "$SCRIPT_DIR/docker/container_lib.sh"
  CONTAINER_NAME="ttsum-replay-$JOB_ID"
  container_kit_launch "$CONTAINER_NAME" "$KIT_ROOT" "$EXT_ROOT" "$GPU" "$KIT" "$APP" \
    "${COMMON_ARGS[@]}" \
    --/renderer/multiGpu/enabled=false \
    --exec "$EXEC_ARGS" \
    > "$LOG" 2>&1 &
else
  CUDA_VISIBLE_DEVICES="$GPU" "$KIT" "$APP" \
    "${COMMON_ARGS[@]}" \
    --/renderer/activeGpu="$GPU" \
    --/renderer/multiGpu/enabled=false \
    --exec "$EXEC_ARGS" \
    > "$LOG" 2>&1 &
fi
KIT_PID=$!
trap 'kill -TERM "$KIT_PID" 2>/dev/null || true; [ "${USE_CONTAINER:-0}" = "1" ] && container_cleanup "$CONTAINER_NAME"' INT TERM

START=$SECONDS
while kill -0 "$KIT_PID" 2>/dev/null; do
  grep -q '\[replay\] done\.' "$LOG" 2>/dev/null && break
  if (( SECONDS - START > BOUND )); then
    echo "[job $JOB_ID] TIMEOUT(${BOUND}s) — kit(PID $KIT_PID)만 종료"
    break
  fi
  write_status running
  sleep 30
done
sleep 25   # --quit 자체 종료(force-exit 15s) 대기
if kill -0 "$KIT_PID" 2>/dev/null; then
  kill -TERM "$KIT_PID" 2>/dev/null || true; sleep 5
  kill -KILL "$KIT_PID" 2>/dev/null || true
fi
wait "$KIT_PID" 2>/dev/null || true
[ "${USE_CONTAINER:-0}" = "1" ] && container_cleanup "$CONTAINER_NAME"  # 안전망 — 강제종료 경로 대비

if grep -q '\[replay\] done\.' "$LOG" 2>/dev/null; then
  write_status done
  echo "[job $JOB_ID] DONE -> $OUT"
else
  write_status failed "no completion marker (see job.log)"
  echo "[job $JOB_ID] FAILED — 로그: $LOG"
  exit 1
fi
