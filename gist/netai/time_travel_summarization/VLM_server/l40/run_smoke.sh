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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
EXT_DIR="$REPO_ROOT/source/extensions/gist.netai.time_travel_summarization"
GEN="$EXT_DIR/gist/netai/time_travel_summarization/automation/generate_episodes.py"
KIT="$REPO_ROOT/_build/linux-x86_64/release/kit/kit"
APP="$REPO_ROOT/_build/linux-x86_64/release/apps/my_company.my_usd_composer2.kit"

[ -x "$KIT" ] || { echo "ERROR: $KIT 없음 — ./repo.sh build --release 먼저"; exit 2; }
[ -f "$EXT_DIR/gist/netai/time_travel_summarization/.env" ] || \
  { echo "ERROR: 확장 .env 없음 — extension.env.example을 복사해 작성"; exit 2; }

TS="$(date +%Y%m%dT%H%M%S)"
OUT="$REPO_ROOT/artifacts/episodes/l40-smoke-$TS"
LOG="$REPO_ROOT/artifacts/l40-smoke-$TS.log"
mkdir -p "$(dirname "$OUT")"

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
  --ext-folder "$REPO_ROOT/source/extensions" \
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
