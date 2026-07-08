#!/usr/bin/env bash
# 잡 API 데몬 기동 — localhost 전용 바인딩 (하이브리드 보안 모델).
#
#   tmux new -s job-api 'bash run_api.sh'
#
# 원격 접속은 SSH 터널로:  ssh -L 8800:localhost:8800 <host>
#   → 클라이언트(GUI)는 Host 칸에 http://localhost:8800 입력.
# 선택: JOB_API_KEY=<키> 로 기동하면 X-API-Key 헤더를 추가로 요구.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
exec uvicorn job_api:app --host 127.0.0.1 --port "${PORT:-8800}"
