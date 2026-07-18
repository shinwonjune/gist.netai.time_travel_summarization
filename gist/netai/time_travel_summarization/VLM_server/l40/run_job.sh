#!/usr/bin/env bash
# 스펙(env) 구동 데이터 생성 잡 러너 — remote_generation.py의 실행면 짝.
#
# 입력: JobSpec.to_env()가 렌더링한 env 변수들 (JOB_ID, EPISODES, DURATION, GPU,
#       RENDER_FPS, SPEED_MIN/MAX, MIN/MAX/EXTRA_OBJECTS, CAMERA, STAGE, APP_KIT,
#       UPLOAD_URI, SPAWN_PLAN, KEEP_POSITIONS)
# 상태: $EXT_ROOT/artifacts/jobs/$JOB_ID/status 에 KEY=VALUE로 주기 갱신
#       (state=running|done|failed, episodes_done, total, updated) — 제어면이 폴링.
#
# 공용 서버 수칙 (run_smoke.sh와 동일):
#  - 산출물은 repo 안에만, 프로세스 정리는 자기 자식 PID만 (이름 기반 pkill 금지)
#  - GPU는 지정 1개만 (--/renderer/activeGpu + multiGpu off + CUDA_VISIBLE_DEVICES)
# 자격증명: $HOME/wonjune/.env.l40 이 있으면 source (MINIO_*, OMNI_USER/PASS —
#           무인 잡의 Nucleus 인증·minIO 업로드에 필요)
set -euo pipefail

JOB_ID="${JOB_ID:?JOB_ID 필요}"
EPISODES="${EPISODES:-5}"
DURATION="${DURATION:-30}"
GPU="${GPU:-1}"
RENDER_FPS="${RENDER_FPS:-30}"
SPEED_MIN="${SPEED_MIN:-120}"
SPEED_MAX="${SPEED_MAX:-140}"
MIN_OBJECTS="${MIN_OBJECTS:-4}"
MAX_OBJECTS="${MAX_OBJECTS:-4}"
EXTRA_OBJECTS="${EXTRA_OBJECTS:-0}"
CAMERA="${CAMERA:-}"
STAGE="${STAGE:-}"
UPLOAD_URI="${UPLOAD_URI:-}"
SPAWN_PLAN="${SPAWN_PLAN:-}"
KEEP_POSITIONS="${KEEP_POSITIONS:-}"
SEED="${SEED:-42}"   # run마다 반드시 다르게 — 같으면 이전 run과 동일 에피소드 재생성

# ---- 경로 해석 (run_smoke.sh와 동일 규칙) ---------------------------------- #
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
EXT_PKG="$EXT_ROOT/gist/netai/time_travel_summarization"
GEN="$EXT_PKG/automation/generate_episodes.py"

# ---- 잡 상태 파일을 먼저 만든다 --------------------------------------------- #
# kit/앱 해석보다 앞이어야 조기 실패(APP_KIT 미지정 등)도 status·log에 남는다.
# (전엔 해석 후 생성이라 tmux 세션만 죽고 GUI엔 아무 흔적이 없는 무음 실패였음.)
JOB_DIR="$EXT_ROOT/artifacts/jobs/$JOB_ID"
OUT="$EXT_ROOT/artifacts/episodes/$JOB_ID"
LOG="$JOB_DIR/job.log"
STATUS="$JOB_DIR/status"
mkdir -p "$JOB_DIR" "$OUT"

write_status() {  # write_status <state> <done> [note]
  { echo "state=$1"; echo "episodes_done=$2"; echo "total=$EPISODES";
    echo "job_id=$JOB_ID"; [ -n "${3:-}" ] && echo "note=$3";
    echo "updated=$(date -Is)"; } > "$STATUS.tmp"
  mv "$STATUS.tmp" "$STATUS"   # 원자적 교체 — 폴링이 반쯤 쓰인 파일을 읽지 않게
}
fail() {  # fail <note> — 조기 실패를 status+log 양쪽에 남기고 종료
  echo "ERROR: $1" | tee -a "$LOG"
  write_status failed 0 "$1"
  exit 2
}
write_status running 0 "resolving kit/app"

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

write_status running 0

# ---- 생성 인자 조립 --------------------------------------------------------- #
EXEC_ARGS="$GEN --episodes $EPISODES --duration $DURATION --render-fps $RENDER_FPS \
 --speed-min $SPEED_MIN --speed-max $SPEED_MAX \
 --min-objects $MIN_OBJECTS --max-objects $MAX_OBJECTS --seed $SEED --out $OUT --quit"
[ "$EXTRA_OBJECTS" != "0" ] && EXEC_ARGS="$EXEC_ARGS --extra-objects $EXTRA_OBJECTS"
[ -n "$STAGE" ] && EXEC_ARGS="$EXEC_ARGS --stage $STAGE"
[ -n "$CAMERA" ] && EXEC_ARGS="$EXEC_ARGS --camera $CAMERA"
[ -n "$UPLOAD_URI" ] && EXEC_ARGS="$EXEC_ARGS --upload-uri $UPLOAD_URI"
[ -n "$SPAWN_PLAN" ] && EXEC_ARGS="$EXEC_ARGS --spawn-plan $SPAWN_PLAN"
[ -n "$KEEP_POSITIONS" ] && EXEC_ARGS="$EXEC_ARGS --keep-positions"

# 워치독 상한: (로드 180s + ep×(D×7.2+30)) ×1.2 — CLAUDE.md 공식(보수적, 60fps 기준)
DUR_INT="${DURATION%.*}"
BOUND=$(( (180 + EPISODES * (DUR_INT * 8 + 30)) * 12 / 10 ))

echo "[job $JOB_ID] episodes=$EPISODES duration=${DURATION}s gpu=$GPU bound=${BOUND}s"
echo "[job $JOB_ID] log: $LOG"

CUDA_VISIBLE_DEVICES="$GPU" "$KIT" "$APP" \
  --no-window \
  --ext-folder "$(dirname "$EXT_ROOT")" \
  --enable gist.netai.time_travel_summarization \
  --enable omni.replicator.core \
  --/app/settings/fabricDefaultStageFrameHistoryCount=3 \
  --/app/content/emptyStageOnStart=true \
  --/renderer/activeGpu="$GPU" \
  --/renderer/multiGpu/enabled=false \
  --exec "$EXEC_ARGS" \
  > "$LOG" 2>&1 &
KIT_PID=$!
trap 'kill -TERM "$KIT_PID" 2>/dev/null || true' INT TERM

START=$SECONDS
while kill -0 "$KIT_PID" 2>/dev/null; do
  grep -q '\[gen\] done\.' "$LOG" 2>/dev/null && break
  if (( SECONDS - START > BOUND )); then
    echo "[job $JOB_ID] TIMEOUT(${BOUND}s) — kit(PID $KIT_PID)만 종료"
    break
  fi
  DONE_EPS=$(grep -c '\[gen\] ep .* -> ' "$LOG" 2>/dev/null || true)
  write_status running "${DONE_EPS:-0}"
  sleep 30
done
sleep 25   # --quit 자체 종료(force-exit 15s) 대기
if kill -0 "$KIT_PID" 2>/dev/null; then
  kill -TERM "$KIT_PID" 2>/dev/null || true; sleep 5
  kill -KILL "$KIT_PID" 2>/dev/null || true
fi
wait "$KIT_PID" 2>/dev/null || true

DONE_EPS=$(grep -c '\[gen\] ep .* -> ' "$LOG" 2>/dev/null || true)
if grep -q '\[gen\] done\.' "$LOG" 2>/dev/null; then
  write_status done "${DONE_EPS:-0}"
  echo "[job $JOB_ID] DONE ($DONE_EPS/$EPISODES) -> $OUT"
else
  write_status failed "${DONE_EPS:-0}"
  echo "[job $JOB_ID] FAILED — 로그: $LOG"
  exit 1
fi
