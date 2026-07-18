#!/usr/bin/env bash
# 잡 API 데몬 기동 — localhost 전용 바인딩 (하이브리드 보안 모델).
#
#   tmux new -s job-api 'bash run_api.sh'   # GUI의 Connect Server 버튼도 이 경로
#
# 원격 접속은 SSH 터널로:  ssh -L 8800:localhost:8800 <host>
#   → 클라이언트(GUI)는 Host 칸에 http://localhost:8800 입력.
# 선택: JOB_API_KEY=<키> 로 기동하면 X-API-Key 헤더를 추가로 요구.
set -euo pipefail
PORT="${PORT:-8800}"

# 멱등 가드: 이미 떠 있으면 통과 — 재실행 시 데몬 2개가 포트를 두고 싸우는 것 방지.
if curl -sf -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "already running on :$PORT"
  exit 0
fi

# tmux 비대화형 셸엔 venv PATH가 없을 수 있어 직접 활성화 (run_train.sh와 동일 규칙)
VENV="${VENV:-$HOME/wonjune/venv}"
[ -f "$VENV/bin/activate" ] && . "$VENV/bin/activate"

cd "$(dirname "${BASH_SOURCE[0]}")"
exec uvicorn job_api:app --host 127.0.0.1 --port "$PORT"
