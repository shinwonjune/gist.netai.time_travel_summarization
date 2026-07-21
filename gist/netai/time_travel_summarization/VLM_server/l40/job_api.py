"""잡 REST API (FastAPI) — 제어면의 서버측 데몬.

잡 타입 (job_type):
  generate     데이터 생성 (run_job.sh — kit headless 배치)
  train        LoRA 학습 (run_train.sh — training/qwen3vl_lora_swift.sh 래핑)
  serve_start  vLLM 서빙 기동 (run_serve.sh — 상주 프로세스는 러너 밖에서 지속)
  serve_stop   vLLM 서빙 중지

GPU 역할 분리: env `SERVE_GPU`(기본 0)는 서빙 전용. serve_* 잡은 이 GPU로
강제되고, generate/train 잡은 이 GPU를 지정하면 422로 거부된다 —
상주 서빙과 유한 잡의 경합을 큐가 아니라 역할로 차단.

보안 모델 (하이브리드):
  - 기본 바인딩 127.0.0.1 (run_api.sh) — 포트가 머신 밖에 보이지 않는다.
  - 원격 클라이언트는 SSH 터널로 접속: `ssh -L 8800:localhost:8800 <host>`
    → 인증·암호화를 기존 SSH 키가 제공하고, API 코드는 전송과 무관하게 유지.
  - 심층 방어(선택): env `JOB_API_KEY`가 설정돼 있으면 `X-API-Key` 헤더 요구.

GPU 큐: GPU 인덱스별 순차 큐 — 같은 GPU에 잡이 몰려도 한 번에 하나만 실행
(다중 사용자 GPU 경합 방지). 실행은 타입별 러너에 위임하므로
SSH 경로로 제출한 잡과 완전히 동일하게 동작한다.

엔드포인트:
  POST /jobs            JobRequest JSON → 검증 → 큐 적재 → 202 {job_id, gpu}
  GET  /jobs            전체 잡 목록 (status 파일 스캔)
  GET  /jobs/{id}       상태 (state=queued|running|done|failed, episodes_done/total)
  GET  /jobs/{id}/log?tail=50
  GET  /health          큐 현황 + serve_gpu

기동: bash run_api.sh   (tmux 권장: tmux new -s job-api 'bash run_api.sh')
"""
from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

# 확장 루트: .../EXT_ROOT/gist/netai/time_travel_summarization/VLM_server/l40/job_api.py
EXT_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(EXT_ROOT))
from gist.netai.time_travel_summarization.automation.remote_generation import (  # noqa: E402
    JOB_TYPES, JOBS_REL, JobSpec, runner_rel_for,
)

JOBS_DIR = EXT_ROOT / JOBS_REL
API_KEY = os.environ.get("JOB_API_KEY", "")
# GPU 역할 고정: SERVE_GPU는 vLLM 서빙 전용, 나머지가 잡(생성/학습)용.
# 서빙(상주 프로세스)과 잡(유한 실행)의 경합을 큐가 아니라 역할 분리로 차단.
SERVE_GPU = int(os.environ.get("SERVE_GPU", "0"))
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")  # 경로 조작 방지

app = FastAPI(title="TTS Generation Job API", version="1.0")

_queues: dict[int, "queue.Queue[JobSpec]"] = {}
_known: set[str] = set()
_lock = threading.Lock()


class JobRequest(BaseModel):
    """JobSpec의 HTTP 입면 — 검증 규칙 포함. 미지정 job_id/seed는 서버가 발급."""

    job_id: Optional[str] = None
    job_type: str = "generate"          # generate | train | serve_start | serve_stop
    dataset: str = ""                   # train: 데이터셋 디렉토리 (서버 경로)
    train_output: str = ""              # train: 어댑터 출력 디렉토리 (빈 값 = 러너 기본)
    model_path: str = ""                # serve_start: 병합 모델 디렉토리
    port: int = Field(38011, ge=1024, le=65535)
    num_frames: int = Field(20, ge=1, le=64)
    episodes: int = Field(5, ge=1, le=1000)
    duration: float = Field(30.0, gt=0, le=600)
    gpu: int = Field(1, ge=0, le=15)
    render_fps: int = Field(30, ge=1, le=60)
    speed_min: float = Field(120.0, gt=0)
    speed_max: float = Field(140.0, gt=0)
    min_objects: int = Field(4, ge=1)
    max_objects: int = Field(4, ge=1)
    extra_objects: int = Field(0, ge=0)
    camera: str = "Capture_camera"
    stage: str = ""
    app_kit: str = ""
    upload_uri: str = ""
    spawn_plan: str = ""
    keep_positions: bool = False
    seed: Optional[int] = None
    replay_start: str = ""              # replay: ISO "YYYY-MM-DD HH:MM:SS"
    replay_end: str = ""                # replay: ISO "YYYY-MM-DD HH:MM:SS"
    data_uri: str = ""                  # replay: 트레이스 URI 또는 레이크 데이터셋 (빈 값=config 기본)


def _check_key(x_api_key: Optional[str]) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


def _write_status(job_id: str, state: str, extra: Optional[dict] = None) -> None:
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    lines = {"state": state, "job_id": job_id,
             "updated": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")}
    lines.update(extra or {})
    tmp = job_dir / "status.tmp"
    tmp.write_text("".join(f"{k}={v}\n" for k, v in lines.items()), encoding="utf-8")
    tmp.replace(job_dir / "status")  # 원자적 교체 (run_job.sh와 동일 규약)


def _read_status(job_id: str) -> Optional[dict]:
    path = JOBS_DIR / job_id / "status"
    if not path.exists():
        return None
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def _worker(gpu: int) -> None:
    q = _queues[gpu]
    while True:
        spec = q.get()
        try:
            _write_status(spec.job_id, "starting", {"gpu": gpu, "job_type": spec.job_type})
            env = dict(os.environ)
            env.update({k: v for k, v in spec.to_env().items() if v != ""})
            runner = EXT_ROOT / runner_rel_for(spec.job_type)
            out_path = JOBS_DIR / spec.job_id / "runner.out"
            with open(out_path, "w", encoding="utf-8") as f:
                # 러너가 status를 running→done|failed로 갱신하며 동기 완주.
                # serve_start도 "vLLM 기동 + 준비 확인"까지가 잡이고 상주 프로세스는
                # 러너 밖에서 계속 산다(큐를 막지 않음).
                subprocess.run(["bash", str(runner)], env=env,
                               stdout=f, stderr=subprocess.STDOUT)
        except Exception as exc:  # 워커는 절대 죽지 않는다 (다음 잡 계속)
            _write_status(spec.job_id, "failed", {"error": repr(exc)})
        finally:
            q.task_done()


def _enqueue(spec: JobSpec) -> int:
    """GPU별 큐에 적재. 첫 잡이면 그 GPU 전담 워커 스레드를 시작. 대기 수 반환."""
    with _lock:
        q = _queues.get(spec.gpu)
        if q is None:
            q = _queues[spec.gpu] = queue.Queue()
            threading.Thread(target=_worker, args=(spec.gpu,),
                             daemon=True, name=f"gpu{spec.gpu}-worker").start()
        waiting = q.qsize()
        _write_status(spec.job_id, "queued", {"gpu": spec.gpu, "ahead": waiting})
        q.put(spec)
        return waiting


@app.get("/health")
def health():
    return {"ok": True, "queues": {g: q.qsize() for g, q in _queues.items()},
            "serve_gpu": SERVE_GPU, "job_types": list(JOB_TYPES)}


_ID_PREFIX = {"generate": "gen", "train": "train", "serve_start": "serve",
              "serve_stop": "serve", "replay": "replay"}


def _validate_request(req: JobRequest) -> int:
    """타입별 검증 + GPU 역할 분리 적용. 실행 GPU를 반환."""
    if req.job_type not in JOB_TYPES:
        raise HTTPException(422, f"job_type must be one of {JOB_TYPES}")
    if req.job_type in ("serve_start", "serve_stop"):
        if req.job_type == "serve_start" and not req.model_path:
            raise HTTPException(422, "serve_start requires model_path")
        return SERVE_GPU  # 서빙은 전용 GPU 고정 (요청 gpu 무시)
    # 생성/학습/재연 잡은 서빙 GPU를 쓸 수 없다 (역할 분리 — kit 렌더가 서빙과 경합)
    if req.gpu == SERVE_GPU:
        raise HTTPException(422, f"gpu {SERVE_GPU} is reserved for serving (SERVE_GPU)")
    if req.job_type == "train":
        if not req.dataset:
            raise HTTPException(422, "train requires dataset (server-side dir)")
        return req.gpu
    if req.job_type == "replay":
        if not req.replay_start or not req.replay_end:
            raise HTTPException(422, "replay requires replay_start and replay_end")
        return req.gpu
    # generate 고유 검증
    if req.max_objects < req.min_objects:
        raise HTTPException(422, "max_objects < min_objects")
    if req.extra_objects and req.keep_positions:
        raise HTTPException(422, "extra_objects는 keep_positions와 양립 불가")
    return req.gpu


@app.post("/jobs", status_code=202)
def create_job(req: JobRequest, x_api_key: Optional[str] = Header(default=None)):
    _check_key(x_api_key)
    gpu = _validate_request(req)
    prefix = _ID_PREFIX[req.job_type]
    job_id = req.job_id or f"{prefix}-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(422, "job_id: 영숫자 . _ - 만 허용")
    with _lock:
        if job_id in _known or (JOBS_DIR / job_id / "status").exists():
            raise HTTPException(409, f"job_id {job_id!r} already exists")
        _known.add(job_id)
    seed = req.seed if req.seed is not None else int(time.time()) % 2_000_000_000
    spec = JobSpec(
        job_id=job_id, episodes=req.episodes, duration=req.duration, gpu=gpu,
        render_fps=req.render_fps, speed_min=req.speed_min, speed_max=req.speed_max,
        min_objects=req.min_objects, max_objects=req.max_objects,
        extra_objects=req.extra_objects, camera=req.camera, stage=req.stage,
        app_kit=req.app_kit, upload_uri=req.upload_uri, spawn_plan=req.spawn_plan,
        keep_positions=req.keep_positions, seed=seed,
        job_type=req.job_type, dataset=req.dataset, train_output=req.train_output,
        model_path=req.model_path, port=req.port, num_frames=req.num_frames,
        replay_start=req.replay_start, replay_end=req.replay_end, data_uri=req.data_uri,
    )
    ahead = _enqueue(spec)
    return {"job_id": job_id, "state": "queued", "gpu": spec.gpu,
            "job_type": spec.job_type, "ahead": ahead, "seed": seed}


@app.get("/jobs")
def list_jobs(x_api_key: Optional[str] = Header(default=None)):
    _check_key(x_api_key)
    jobs = []
    if JOBS_DIR.exists():
        for d in sorted(JOBS_DIR.iterdir()):
            st = _read_status(d.name)
            if st:
                jobs.append({"job_id": d.name, "state": st.get("state"),
                             "episodes_done": st.get("episodes_done"),
                             "total": st.get("total")})
    return {"jobs": jobs}


@app.get("/jobs/{job_id}")
def get_job(job_id: str, x_api_key: Optional[str] = Header(default=None)):
    _check_key(x_api_key)
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(422, "bad job_id")
    st = _read_status(job_id)
    if st is None:
        raise HTTPException(404, f"unknown job {job_id!r}")
    return st


@app.get("/jobs/{job_id}/log")
def get_log(job_id: str, tail: int = 50, x_api_key: Optional[str] = Header(default=None)):
    _check_key(x_api_key)
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(422, "bad job_id")
    log = JOBS_DIR / job_id / "job.log"
    if not log.exists():
        raise HTTPException(404, "no log yet")
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = max(1, min(int(tail), 1000))
    return {"job_id": job_id, "lines": lines[-tail:]}
