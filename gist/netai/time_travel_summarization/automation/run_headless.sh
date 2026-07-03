#!/usr/bin/env bash
# Launch Kit HEADLESS and batch-generate physics episodes via generate_episodes.py.
#
# This repo has no bundled Kit launcher (it's an extension loaded by USD Composer),
# so point KIT_APP at a Kit executable that has RTX rendering + omni.replicator.core
# available (e.g. a Composer/kit launcher in your Omniverse install). The offscreen
# capture path needs a GPU.
#
# Required env:
#   KIT_APP   path to the kit executable (e.g. .../omni.usd_composer.kit or kit)
#   EXT_ROOT  folder CONTAINING this extension package (the parent of `gist/`)
# Optional:
#   EXT_NAME  extension id to enable (default: netai.timetravel_dreamai)
#   EPISODES, OUT, DURATION  passed through to generate_episodes.py
#
# Example:
#   KIT_APP=~/.local/share/ov/pkg/composer/kit/kit \
#   EXT_ROOT=/path/to/repo/gist/netai \
#   EPISODES=50 OUT=artifacts/episodes DURATION=40 \
#   bash automation/run_headless.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # extension package root
: "${KIT_APP:?set KIT_APP to your Kit executable}"
: "${EXT_ROOT:?set EXT_ROOT to the folder containing the extension package}"
EXT_NAME="${EXT_NAME:-netai.timetravel_dreamai}"
EPISODES="${EPISODES:-50}"
OUT="${OUT:-artifacts/episodes}"
DURATION="${DURATION:-40}"

SCRIPT="${HERE}/automation/generate_episodes.py"

# Notes:
#  --no-window         : headless (no UI); capture uses the offscreen render product
#  --/app/...          : disable UI/audio for batch stability (adjust per your Kit app)
#  --enable omni.replicator.core : ensure Replicator is loaded for offscreen render
#  --/app/settings/fabricDefaultStageFrameHistoryCount=3 : REQUIRED — SyntheticData
#    (annotator 기반)가 history<3이면 초기화를 거부해 annotator가 영원히 빈 데이터
#    (실측: "needs at least a stageFrameHistoryCount of 3"). 부팅 시에만 설정 가능.
#  NOTE: Kit --exec에는 '--' 구분자를 쓰지 않는다(인자 유실, 실측).
exec "${KIT_APP}" \
  --no-window \
  --ext-folder "${EXT_ROOT}" \
  --enable "${EXT_NAME}" \
  --enable omni.replicator.core \
  --/app/window/hideUi=true \
  --/app/quitAfter=-1 \
  --/app/settings/fabricDefaultStageFrameHistoryCount=3 \
  --/app/content/emptyStageOnStart=true \
  --exec "${SCRIPT} --episodes ${EPISODES} --out ${OUT} --duration ${DURATION} --quit"
# emptyStageOnStart=true 필수: false면 Composer 셋업 확장이 시작 ~16s 뒤 지연 new_stage로
# 현재 스테이지를 교체 → replicator가 stage-closing에서 orchestrator reset + hydra texture
# 전멸 → annotator가 영원히 'not attached'(실측). true면 그 지연 교체가 예약되지 않는다.
