#!/usr/bin/env bash
# 스펙(env) 구동 데이터 생성 잡 러너 — remote_generation.py의 실행면 짝.
#
# 입력: JobSpec.to_env()가 렌더링한 env 변수들 (JOB_ID, EPISODES, DURATION, GPU,
#       RENDER_FPS, SPEED_MIN/MAX, MIN/MAX/EXTRA_OBJECTS, CAMERA, STAGE, SCENE_PROFILE,
#       APP_KIT, UPLOAD_URI, SPAWN_PLAN, KEEP_POSITIONS, SEED) + NEAR_MISS/NEAR_MISS_GAP/NEAR_MISS_MODE
#       (대조 데이터셋, 기본 모드 swerve) + NEAR_MISS_AVOID/TURN_RADIUS/AIM_FRAC
#       (swerve 회피 곡선의 완만함 — 미지정 시 생성기 기본값) + NEAR_MISS_START_JITTER/
#       SPEED_MIN_FRAC/SPEED_MAX_FRAC/DEPART_SPREAD (조우 지점의 다양성)
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
# 씬 프로파일(scene_profiles.json 이름). 미지정 잡도 있으므로 기본값이 반드시 필요하다
# — set -u에서 정의 없이 참조하면 kit 기동 전에 "unbound variable"로 러너가 죽는다.
SCENE_PROFILE="${SCENE_PROFILE:-}"
UPLOAD_URI="${UPLOAD_URI:-}"
SPAWN_PLAN="${SPAWN_PLAN:-}"
KEEP_POSITIONS="${KEEP_POSITIONS:-}"
# near-miss 대조 데이터셋: 짝끼리 NEAR_MISS_GAP(cm)까지 접근했다 흩어짐 → GT 충돌 0건.
# NEAR_MISS_MODE: swerve(기본, 감속 없이 스침) | stop(v1, 감속+정지+방향전환 — 대조군).
NEAR_MISS="${NEAR_MISS:-}"
NEAR_MISS_GAP="${NEAR_MISS_GAP:-95}"
NEAR_MISS_MODE="${NEAR_MISS_MODE:-swerve}"
# swerve 회피 곡선의 완만함(전부 gap 배수, 빈 값이면 생성기 기본값 3.0 / 1.0 / 1.05).
# TURN_RADIUS_FRAC이 완만함을 좌우하는 값 — 키우면 큰 원을 그리듯 부드러워진다.
NEAR_MISS_AVOID_FRAC="${NEAR_MISS_AVOID_FRAC:-}"
NEAR_MISS_TURN_RADIUS_FRAC="${NEAR_MISS_TURN_RADIUS_FRAC:-}"
NEAR_MISS_AIM_FRAC="${NEAR_MISS_AIM_FRAC:-}"
# 조우 지점의 다양성(빈 값이면 생성기 기본값 2.0초 / 0.7~1.0배 / ±90도). 이 셋이
# 짝의 대칭(동시 출발·같은 속도·대칭 이탈)을 깨서 조우가 방 중앙에서 같은 기하로
# 반복되는 것을 막는다. 전부 0(부채꼴은 0 이하)으로 주면 대칭 안무로 되돌아간다.
NEAR_MISS_START_JITTER="${NEAR_MISS_START_JITTER:-}"
NEAR_MISS_SPEED_MIN_FRAC="${NEAR_MISS_SPEED_MIN_FRAC:-}"
NEAR_MISS_SPEED_MAX_FRAC="${NEAR_MISS_SPEED_MAX_FRAC:-}"
NEAR_MISS_DEPART_SPREAD="${NEAR_MISS_DEPART_SPREAD:-}"
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
[ -n "$SCENE_PROFILE" ] && EXEC_ARGS="$EXEC_ARGS --scene-profile $SCENE_PROFILE"
[ -n "$CAMERA" ] && EXEC_ARGS="$EXEC_ARGS --camera $CAMERA"
[ -n "$UPLOAD_URI" ] && EXEC_ARGS="$EXEC_ARGS --upload-uri $UPLOAD_URI"
[ -n "$SPAWN_PLAN" ] && EXEC_ARGS="$EXEC_ARGS --spawn-plan $SPAWN_PLAN"
[ -n "$KEEP_POSITIONS" ] && EXEC_ARGS="$EXEC_ARGS --keep-positions"
[ -n "$NEAR_MISS" ] && EXEC_ARGS="$EXEC_ARGS --near-miss --near-miss-gap $NEAR_MISS_GAP --near-miss-mode $NEAR_MISS_MODE"
[ -n "$NEAR_MISS_AVOID_FRAC" ] && EXEC_ARGS="$EXEC_ARGS --near-miss-avoid-frac $NEAR_MISS_AVOID_FRAC"
[ -n "$NEAR_MISS_TURN_RADIUS_FRAC" ] && EXEC_ARGS="$EXEC_ARGS --near-miss-turn-radius-frac $NEAR_MISS_TURN_RADIUS_FRAC"
[ -n "$NEAR_MISS_AIM_FRAC" ] && EXEC_ARGS="$EXEC_ARGS --near-miss-aim-frac $NEAR_MISS_AIM_FRAC"
[ -n "$NEAR_MISS_START_JITTER" ] && EXEC_ARGS="$EXEC_ARGS --near-miss-start-jitter $NEAR_MISS_START_JITTER"
[ -n "$NEAR_MISS_SPEED_MIN_FRAC" ] && EXEC_ARGS="$EXEC_ARGS --near-miss-speed-min-frac $NEAR_MISS_SPEED_MIN_FRAC"
[ -n "$NEAR_MISS_SPEED_MAX_FRAC" ] && EXEC_ARGS="$EXEC_ARGS --near-miss-speed-max-frac $NEAR_MISS_SPEED_MAX_FRAC"
[ -n "$NEAR_MISS_DEPART_SPREAD" ] && EXEC_ARGS="$EXEC_ARGS --near-miss-depart-spread $NEAR_MISS_DEPART_SPREAD"

# 워치독 상한: (로드 180s + ep×(D×7.2+30)) ×1.2 — CLAUDE.md 공식(보수적, 60fps 기준)
DUR_INT="${DURATION%.*}"
BOUND=$(( (180 + EPISODES * (DUR_INT * 8 + 30)) * 12 / 10 ))

echo "[job $JOB_ID] episodes=$EPISODES duration=${DURATION}s gpu=$GPU bound=${BOUND}s"
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
  CONTAINER_NAME="ttsum-gen-$JOB_ID"
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
[ "${USE_CONTAINER:-0}" = "1" ] && container_cleanup "$CONTAINER_NAME"  # 안전망 — 강제종료 경로 대비

DONE_EPS=$(grep -c '\[gen\] ep .* -> ' "$LOG" 2>/dev/null || true)
if grep -q '\[gen\] done\.' "$LOG" 2>/dev/null; then
  write_status done "${DONE_EPS:-0}"
  echo "[job $JOB_ID] DONE ($DONE_EPS/$EPISODES) -> $OUT"
else
  write_status failed "${DONE_EPS:-0}"
  echo "[job $JOB_ID] FAILED — 로그: $LOG"
  exit 1
fi
