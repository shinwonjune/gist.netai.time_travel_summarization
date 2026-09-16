#!/usr/bin/env bash
# 객체 수 스케일 스윕 드라이버 (docs/레이크성능_실험설계.md §1-1·§3-1·§6-6).
#
# 한 번의 호출로 ① 합성 데이터셋 빌드 → ② 이음매용 실궤적 데이터셋(aigrad_bev_10hz_30min_v1
# c60/c300) A·B 측정 → ③ N별 합성 데이터셋 A·B 측정을 순서대로 돌리고, 결과 JSON/MD를
# OUT 디렉터리에 남긴다. 후처리는 utils/lake_scale_report.py.
#
# 사용:  tools/run_lake_sweep.sh <python> <EXT_ROOT> <OUT_DIR> [N ...]
#   python    minio + pyarrow가 있는 인터프리터(Windows anaconda/Kit python, L40 Kit pip env)
#   EXT_ROOT  gist/ 패키지가 있는 확장 루트(이 저장소 루트)
#   OUT_DIR   결과 디렉터리(sweep.log / sweep.pid / sweep.done 마커 포함)
#   N ...     객체 수 격자(기본 4 10 20 30 40 50)
# 환경변수: SWEEP_NOTE(측정 위치·회선·사양 — 판정 규약상 필수), LAKE_ROOT(기본 s3://.../trajectory),
#          SKIP_BUILD=1(데이터셋이 이미 있을 때), SKIP_REAL=1(이음매 실궤적 측정 생략)
#
# 측정 위생(설계 §4-3): 같은 머신에 GUI·학습 잡 등 다른 부하가 없는 quiet run으로 돌릴 것.
# c300은 본 측정과 같은 분할 — 1x·backward는 벽시계 360s(경계 통과 보장), 5x·seek는 180s.
# 진행 감시는 프로세스 이름이 아니라 sweep.pid / sweep.log / sweep.done 파일로 한다.
set -uo pipefail
PY=${1:?python}; EXT_ROOT=${2:?ext root}; OUT=${3:?out dir}; shift 3
NS=("$@"); [ ${#NS[@]} -eq 0 ] && NS=(4 10 20 30 40 50)
ROOT=${LAKE_ROOT:-s3://time-travel-summarization/trajectory}
NOTE=${SWEEP_NOTE:-"QUIET RUN object-count sweep (location/link/machine: FILL IN)"}
mkdir -p "$OUT"
LOG="$OUT/sweep.log"
echo $$ > "$OUT/sweep.pid"
rm -f "$OUT/sweep.done"
cd "$EXT_ROOT" || exit 1
log() { printf '[%(%F %T)T] %s\n' -1 "$*" | tee -a "$LOG"; }

log "sweep start: N=${NS[*]} root=$ROOT note=$NOTE"
if [ "${SKIP_BUILD:-0}" != "1" ]; then
  log "build datasets"
  "$PY" -m gist.netai.time_travel_summarization.utils.build_lake_synth_dataset \
      --n-objects "${NS[@]}" --root "$ROOT" 2>&1 | tee -a "$LOG"
  log "build exit=${PIPESTATUS[0]} (manifest가 이미 있으면 빌더가 중단한다 — SKIP_BUILD=1로 재실행)"
fi

bench() {  # <dataset name> <scenarios> <wall_s>
  log "bench: $1 scenarios=$2 wall=$3"
  "$PY" -m gist.netai.time_travel_summarization.tests.lake_benchmark \
      --dataset-uri "$ROOT/$1" --scenarios "$2" --play-wall-s "$3" \
      --out-dir "$OUT" --note "$NOTE" 2>&1 | tee -a "$LOG"
  log "bench exit=${PIPESTATUS[0]}: $1"
}
run_dataset() {  # <name prefix without _cNN>
  bench "${1}_c60"  "1x,5x,seek,backward" 180
  bench "${1}_c300" "1x,backward" 360
  bench "${1}_c300" "5x,seek" 180
}

if [ "${SKIP_REAL:-0}" != "1" ]; then
  run_dataset aigrad_bev_10hz_30min_v1     # 이음매 검사용 실궤적(N=4) — 같은 세션·같은 위치에서
fi
for n in "${NS[@]}"; do
  run_dataset "synth_bev_${n}obj_10hz_30min_v1"
done
log "sweep done"
touch "$OUT/sweep.done"
