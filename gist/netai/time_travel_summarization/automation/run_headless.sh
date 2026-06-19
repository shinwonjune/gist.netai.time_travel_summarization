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
exec "${KIT_APP}" \
  --no-window \
  --ext-folder "${EXT_ROOT}" \
  --enable "${EXT_NAME}" \
  --enable omni.replicator.core \
  --/app/window/hideUi=true \
  --/app/quitAfter=-1 \
  --exec "${SCRIPT} -- --episodes ${EPISODES} --out ${OUT} --duration ${DURATION}"
