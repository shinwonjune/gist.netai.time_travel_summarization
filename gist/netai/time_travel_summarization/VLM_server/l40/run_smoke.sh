#!/usr/bin/env bash
# L40 headless 데이터 생성 스모크 (공용 서버 안전 수칙 준수).
#
#   bash run_smoke.sh [GPU_INDEX] [DURATION_S]
#     기본: GPU 1, 5초 에피소드 1개, 빈 스테이지(Nucleus 불필요)
#   Nucleus 씬 검증:  STAGE='omniverse://10.38.38.32/.../A_AI-Grad_Building.usd' \
#                     CAMERA=Capture_camera bash run_smoke.sh 1 10
#
# 공용 서버 수칙:
#  - 모든 산출물은 repo 안(artifacts/)에만 기록. 시스템 경로 무접촉.
#  - 프로세스 정리는 "이 스크립트가 띄운 PID"만 대상 (이름 기반 pkill 절대 금지 —
#    다른 사용자의 kit 프로세스를 죽일 수 있음).
#  - GPU는 인자로 지정한 1개만 사용 (--/renderer/activeGpu + multiGpu off).
set -euo pipefail

GPU="${1:-1}"
DURATION="${2:-5}"
STAGE="${STAGE:-}"
CAMERA="${CAMERA:-}"

# 경로 해석 — 두 배치 모두 지원:
#  (a) 확장 repo 단독 clone (예: ~/wonjune/gist.netai.time_travel_summarization)
#  (b) kit-app-template 안에 중첩 (source/extensions/<확장>)
# 확장 루트 = 이 스크립트가 속한 git repo. Kit 빌드 위치는 KIT_ROOT env로 지정하거나
# 관례 후보에서 자동 탐색.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
EXT_PKG="$EXT_ROOT/gist/netai/time_travel_summarization"
GEN="$EXT_PKG/automation/generate_episodes.py"

if [ "$(basename "$EXT_ROOT")" != "gist.netai.time_travel_summarization" ]; then
  echo "ERROR: 확장 폴더명이 'gist.netai.time_travel_summarization'이어야 Kit이 인식합니다"
  echo "       (현재: $EXT_ROOT)"; exit 2
fi

KIT_ROOT="${KIT_ROOT:-}"
if [ -z "$KIT_ROOT" ]; then
  for cand in "$EXT_ROOT/../../.." "$HOME/wonjune/kit-app-template" "$HOME/kit-app-template"; do
    if [ -x "$cand/_build/linux-x86_64/release/kit/kit" ]; then
      KIT_ROOT="$(cd "$cand" && pwd)"; break
    fi
  done
fi
[ -n "$KIT_ROOT" ] || { echo "ERROR: kit 빌드를 찾지 못함 — KIT_ROOT=<kit-app-template 경로> 지정"; exit 2; }
KIT="$KIT_ROOT/_build/linux-x86_64/release/kit/kit"
# 앱 정의 선택: APP_KIT(이름 또는 경로) 명시 > 자동 발견(빌드 산출물이 정확히 1개일 때만).
# 여러 개일 때 추측 금지 — 목록을 보여주고 명시를 요구한다(조용한 오선택 방지).
APPS_DIR="$KIT_ROOT/_build/linux-x86_64/release/apps"
APP_KIT="${APP_KIT:-}"
if [ -n "$APP_KIT" ]; then
  case "$APP_KIT" in
    */*) APP="$APP_KIT" ;;                       # 경로로 지정
    *)   APP="$APPS_DIR/${APP_KIT%.kit}.kit" ;;  # 이름으로 지정 (.kit 생략 허용)
  esac
  [ -f "$APP" ] || { echo "ERROR: APP_KIT 해석 실패: $APP"; exit 2; }
else
  mapfile -t _apps < <(ls "$APPS_DIR"/*.kit 2>/dev/null || true)
  if [ "${#_apps[@]}" -eq 1 ]; then
    APP="${_apps[0]}"
    echo "[smoke] app auto-selected: $(basename "$APP")"
  elif [ "${#_apps[@]}" -eq 0 ]; then
    echo "ERROR: $APPS_DIR 에 빌드된 앱(.kit) 없음 — ./repo.sh template new 후 build"; exit 2
  else
    echo "ERROR: 앱이 여러 개입니다 — APP_KIT=<이름>으로 지정하세요:"
    for a in "${_apps[@]}"; do echo "  - $(basename "$a" .kit)"; done
    exit 2
  fi
fi

[ -x "$KIT" ] || { echo "ERROR: $KIT 없음 — kit-app-template에서 ./repo.sh build --release 먼저"; exit 2; }
[ -f "$EXT_PKG/.env" ] || \
  { echo "ERROR: 확장 .env 없음 — extension.env.example을 $EXT_PKG/.env로 복사해 작성"; exit 2; }

TS="$(date +%Y%m%dT%H%M%S)"
OUT="$EXT_ROOT/artifacts/episodes/l40-smoke-$TS"
LOG="$EXT_ROOT/artifacts/l40-smoke-$TS.log"
mkdir -p "$(dirname "$OUT")"
echo "[smoke] EXT_ROOT=$EXT_ROOT"
echo "[smoke] KIT_ROOT=$KIT_ROOT"
echo "[smoke] APP=$APP"

EXEC_ARGS="$GEN --episodes 1 --duration $DURATION --render-fps 30 \
 --speed-min 120 --speed-max 140 --out $OUT --quit"
if [ -n "$STAGE" ]; then
  EXEC_ARGS="$EXEC_ARGS --stage $STAGE"
  [ -n "$CAMERA" ] && EXEC_ARGS="$EXEC_ARGS --camera $CAMERA"
fi

echo "[smoke] GPU=$GPU duration=${DURATION}s stage=${STAGE:-'(빈 스테이지)'}"
echo "[smoke] log: $LOG"

"$KIT" "$APP" \
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

# 워치독: 완료 마커 또는 상한 초과 시 "이 PID만" 종료. (빈 스테이지 15분 / 씬 25분)
if [ -n "$STAGE" ]; then BOUND=1500; else BOUND=900; fi
START=$SECONDS
while kill -0 "$KIT_PID" 2>/dev/null; do
  grep -q '\[gen\] done\.' "$LOG" 2>/dev/null && break
  if (( SECONDS - START > BOUND )); then
    echo "[smoke] TIMEOUT(${BOUND}s) — kit(PID $KIT_PID)만 종료"
    break
  fi
  sleep 15
done
sleep 25   # --quit 자체 종료(15s force-exit) 대기
if kill -0 "$KIT_PID" 2>/dev/null; then
  kill -TERM "$KIT_PID" 2>/dev/null || true; sleep 5
  kill -KILL "$KIT_PID" 2>/dev/null || true
fi
wait "$KIT_PID" 2>/dev/null || true

echo "===== 판정 요약 ====="
grep -E '\[gen\] (zone-pos|pos-verify|ep 0:|done)|HL probe.*seq=[0-3] |stall|forward_one_frame|Using device|error|Error' "$LOG" | head -25 || true
if grep -q '\[gen\] done\.' "$LOG"; then
  echo "[smoke] PASS — 출력: $OUT"
else
  echo "[smoke] FAIL — 로그 확인: $LOG"; exit 1
fi
