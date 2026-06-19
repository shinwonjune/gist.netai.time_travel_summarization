#!/usr/bin/env bash
# Orchestrate a remote L40 training run FROM your local workstation (SSH + tmux).
#
# Data is generated locally (Omniverse Kit) but the dataset must be BUILT on the L40
# because build_dataset.py writes ABSOLUTE clip paths into the jsonl. So this script:
#   1) rsyncs the repo + raw episodes to the L40,
#   2) builds the dataset on the L40,
#   3) launches training inside a detached tmux session (survives SSH drops).
#
# Training keeps running after this script returns. Attach any time with:
#   ssh -t "$L40_HOST" tmux attach -t "$TMUX_SESSION"
#
# Required env:
#   L40_HOST       ssh target (e.g. user@l40.example.com, or a ~/.ssh/config alias)
# Optional env:
#   REMOTE_DIR     remote repo path        (default: ~/ttsum)
#   EPISODES_DIR   local raw episodes dir  (default: artifacts/episodes)
#   TMUX_SESSION   remote tmux name        (default: train)
#   PRESET         build_dataset preset    (default: twin_view)
#   CONTENT_HZ     optional --content-hz   (e.g. 5 or 10; default: unset = native)
#
# Example:
#   L40_HOST=me@l40 EPISODES_DIR=artifacts/episodes CONTENT_HZ=10 \
#     bash training/remote_train.sh
set -euo pipefail

: "${L40_HOST:?set L40_HOST to your ssh target (user@host or ssh-config alias)}"
REMOTE_DIR="${REMOTE_DIR:-ttsum}"            # relative to remote $HOME
EPISODES_DIR="${EPISODES_DIR:-artifacts/episodes}"
TMUX_SESSION="${TMUX_SESSION:-train}"
PRESET="${PRESET:-twin_view}"
CONTENT_HZ="${CONTENT_HZ:-}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # extension package root
HZ_ARG=""
[[ -n "${CONTENT_HZ}" ]] && HZ_ARG="--content-hz ${CONTENT_HZ}"

echo ">> 1/3 syncing repo -> ${L40_HOST}:${REMOTE_DIR}"
rsync -avzP --delete \
  --exclude '.git' --exclude '__pycache__' --exclude 'artifacts' \
  "${REPO_ROOT}/" "${L40_HOST}:${REMOTE_DIR}/"

echo ">> 2/3 syncing episodes -> ${L40_HOST}:${REMOTE_DIR}/artifacts/episodes"
rsync -avzP "${EPISODES_DIR}/" "${L40_HOST}:${REMOTE_DIR}/artifacts/episodes/"

echo ">> 3/3 launching build + train in tmux '${TMUX_SESSION}'"
# Single remote command: build the dataset, then train. Logs to artifacts/train.log.
REMOTE_CMD=$(cat <<EOF
set -euo pipefail
cd "\${HOME}/${REMOTE_DIR}"
python -m utils.build_dataset --episodes-dir artifacts/episodes \
    --out-dir artifacts/dataset --preset ${PRESET} --nframes 20 ${HZ_ARG}
DATA=artifacts/dataset OUTPUT=artifacts/lora_qwen3vl bash training/qwen3vl_lora_swift.sh
EOF
)

ssh -t "${L40_HOST}" \
  "tmux new-session -d -s ${TMUX_SESSION} \"bash -lc '${REMOTE_CMD//\"/\\\"} 2>&1 | tee artifacts/train.log'\" && echo started"

cat <<EOF

Launched. Training runs on the L40 inside tmux '${TMUX_SESSION}'.
  attach:   ssh -t ${L40_HOST} tmux attach -t ${TMUX_SESSION}
  log:      ssh ${L40_HOST} 'tail -f ${REMOTE_DIR}/artifacts/train.log'
  gpu:      ssh ${L40_HOST} 'watch -n2 nvidia-smi'
After it finishes, evaluate (on the L40) with training/run_eval.sh, then pull results:
  rsync -avzP ${L40_HOST}:${REMOTE_DIR}/artifacts/lora_qwen3vl ./artifacts/
EOF
