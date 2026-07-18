"""원격/로컬 headless 데이터 생성 잡 제출기 (플랫폼 제어면).

설계 원칙: **잡 스펙(무엇을)과 전송(어디서)을 분리**한다.
- JobSpec — 생성 파라미터 전체의 명시적 집합. extension UI·CLI·agentic 라우터가
  공용으로 쓰는 계약이며, 값은 전부 env 변수로 렌더링되어 러너에 전달된다.
- Transport — 같은 러너(run_job.sh)를 실행하는 위치만 다르다:
    LocalTransport — 같은 머신에서 실행 (서버 상주형)
    SSHTransport   — `ssh <host>`로 원격 실행 (키 기반 인증 전제)
- 실행은 tmux 세션으로 분리(detach)되어 제출 즉시 반환하고 SSH가 끊겨도 지속된다.
- 상태는 러너가 갱신하는 status 파일(KEY=VALUE)을 transport로 읽어 폴링한다.

Kit 의존이 없어 어느 파이썬에서든 임포트·테스트 가능 (self-test: 이 파일을 직접 실행).
"""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import Optional, Tuple

JOB_SCHEMA_VERSION = 2

# 러너의 확장 루트 기준 상대 위치 (원격/로컬 공통) — 잡 타입별로 분리
RUNNER_REL = "gist/netai/time_travel_summarization/VLM_server/l40/run_job.sh"
TRAIN_RUNNER_REL = "gist/netai/time_travel_summarization/VLM_server/l40/run_train.sh"
SERVE_RUNNER_REL = "gist/netai/time_travel_summarization/VLM_server/l40/run_serve.sh"
JOBS_REL = "artifacts/jobs"

JOB_TYPES = ("generate", "train", "serve_start", "serve_stop")


def runner_rel_for(job_type: str) -> str:
    """잡 타입 → 러너 스크립트 상대 경로. 미지 타입은 KeyError(호출부 검증 전제)."""
    return {
        "generate": RUNNER_REL,
        "train": TRAIN_RUNNER_REL,
        "serve_start": SERVE_RUNNER_REL,
        "serve_stop": SERVE_RUNNER_REL,
    }[job_type]


@dataclass
class JobSpec:
    """생성 잡 파라미터. 필드 추가 시 run_job.sh의 env 소비부와 함께 갱신할 것."""

    job_id: str                       # 예: gen-20260708-0001 (호출부가 유일성 보장)
    episodes: int = 5
    duration: float = 30.0
    gpu: int = 1                      # 렌더 GPU 인덱스 (러너가 activeGpu로 고정)
    render_fps: int = 30
    speed_min: float = 120.0
    speed_max: float = 140.0
    min_objects: int = 4
    max_objects: int = 4
    extra_objects: int = 0            # 합성 객체 (keep_positions와 양립 불가)
    camera: str = "Capture_camera"
    stage: str = ""                   # 빈 값 = 빈 스테이지(스모크용)
    app_kit: str = ""                 # 빈 값 = 러너의 자동 발견(1개일 때만)
    upload_uri: str = ""              # 예: s3://time-travel-summarization/episodes/<job_id>
    spawn_plan: str = ""              # "zoneA:2,zoneB:2" (빈 값 = 기본 구역 전원)
    keep_positions: bool = False
    # 마스터 시드 — 에피소드 조건(시각·속도·배치·배회)이 전부 여기서 유도되므로
    # run마다 달라야 한다(같으면 이름만 다른 완전 중복 데이터가 재생성됨).
    seed: int = 42
    # ---- 잡 타입 확장 (generate | train | serve_start | serve_stop) ---------
    # generate 외 타입은 아래 필드만 소비한다. 러너는 runner_rel_for()로 분기.
    job_type: str = "generate"
    dataset: str = ""       # train: 서버측 데이터셋 디렉토리 (build_dataset 산출물)
    train_output: str = ""  # train: 어댑터 출력 디렉토리 (빈 값 = 러너 기본)
    model_path: str = ""    # serve_start: 병합(merged) 모델 디렉토리
    port: int = 38011       # serve: vLLM 포트
    num_frames: int = 20    # serve: 클립당 프레임 예산 (train==infer 정합)

    def to_env(self) -> dict:
        """run_job.sh가 소비하는 env 매핑 (값은 전부 문자열; 빈 값은 전송 생략)."""
        return {
            "JOB_ID": self.job_id,
            "JOB_SCHEMA": str(JOB_SCHEMA_VERSION),
            "EPISODES": str(int(self.episodes)),
            "DURATION": f"{self.duration:g}",
            "GPU": str(int(self.gpu)),
            "RENDER_FPS": str(int(self.render_fps)),
            "SPEED_MIN": f"{self.speed_min:g}",
            "SPEED_MAX": f"{self.speed_max:g}",
            "MIN_OBJECTS": str(int(self.min_objects)),
            "MAX_OBJECTS": str(int(self.max_objects)),
            "EXTRA_OBJECTS": str(int(self.extra_objects)),
            "CAMERA": self.camera,
            "STAGE": self.stage,
            "APP_KIT": self.app_kit,
            "UPLOAD_URI": self.upload_uri,
            "SPAWN_PLAN": self.spawn_plan,
            "KEEP_POSITIONS": "1" if self.keep_positions else "",
            "SEED": str(int(self.seed)),
            "JOB_TYPE": self.job_type,
            "DATASET": self.dataset,
            "TRAIN_OUTPUT": self.train_output,
            "MODEL_PATH": self.model_path,
            "PORT": str(int(self.port)),
            "NUM_FRAMES": str(int(self.num_frames)),
        }


class LocalTransport:
    """같은 머신에서 실행 (서버 상주 GUI형)."""

    name = "local"

    def run(self, command: str, timeout: float = 30.0) -> Tuple[int, str]:
        p = subprocess.run(["bash", "-lc", command], capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()


class SSHTransport:
    """ssh <host>로 원격 실행. BatchMode=yes — 비밀번호 프롬프트 대신 즉시 실패(무인 전제)."""

    name = "ssh"

    def __init__(self, host: str):
        self.host = host

    def run(self, command: str, timeout: float = 30.0) -> Tuple[int, str]:
        p = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", self.host, command],
            capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()


class RESTTransport:
    """잡 API(job_api.py) HTTP 클라이언트 — 하이브리드 모델의 원격 절반.

    데몬은 서버 localhost에만 바인딩되므로, 원격에서는 SSH 터널을 먼저 연다:
        ssh -L 8800:localhost:8800 <host>   →   base_url = http://localhost:8800
    stdlib urllib만 사용 (Kit python 추가 의존성 없음).
    """

    name = "rest"

    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""

    def _request(self, method: str, path: str, payload: Optional[dict] = None,
                 timeout: float = 15.0) -> dict:
        import json as _json
        import urllib.request

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        req = urllib.request.Request(
            self.base_url + path, method=method, headers=headers,
            data=_json.dumps(payload).encode("utf-8") if payload is not None else None)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _json.loads(resp.read().decode("utf-8"))

    def submit_spec(self, spec: JobSpec) -> Tuple[bool, str]:
        from dataclasses import asdict
        try:
            out = self._request("POST", "/jobs", payload=asdict(spec))
            return True, (f"{out.get('job_id')} queued on gpu {out.get('gpu')}"
                          f" (ahead {out.get('ahead', 0)})")
        except Exception as exc:
            return False, f"REST submit failed: {exc!r}"

    def job_status(self, job_id: str) -> dict:
        try:
            return self._request("GET", f"/jobs/{job_id}")
        except Exception as exc:
            return {"state": "unreachable", "error": repr(exc)}


def transport_from_host(host: str):
    """Host 문자열로 전송을 판별: http(s):// → REST, ''/'local' → 로컬, 그 외 → SSH."""
    host = (host or "").strip()
    if host in ("", "local"):
        return LocalTransport()
    if host.startswith("http://") or host.startswith("https://"):
        return RESTTransport(host)
    return SSHTransport(host)


def _tilde_safe(path: str) -> str:
    """원격 셸용 경로 토큰. '~/...'는 shlex.quote가 통째로 홑따옴표로 감싸
    ~ 확장이 막힌다(bash '~/...' → 리터럴 틸드 파일 없음 → 러너 미실행).
    $HOME으로 치환해 원격 sh -c가 확장하게 한다(뒤 경로는 quote로 안전 처리)."""
    if path.startswith("~/"):
        return "$HOME/" + shlex.quote(path[2:])
    return shlex.quote(path)


def build_submit_command(spec: JobSpec, remote_ext_root: str) -> str:
    """tmux 분리 세션으로 러너를 띄우는 셸 명령 1줄을 조립.

    env는 shlex.quote로 개별 인용 — 값에 공백/특수문자가 있어도 안전.
    tmux new-session -d 라 제출은 즉시 반환되고 잡은 서버에서 계속 돈다.
    """
    runner_tok = _tilde_safe(
        f"{remote_ext_root.rstrip('/')}/{runner_rel_for(spec.job_type)}")
    envs = " ".join(
        f"{k}={shlex.quote(v)}" for k, v in spec.to_env().items() if v != "")
    inner = f"{envs} bash {runner_tok}"
    session = shlex.quote(f"job-{spec.job_id}")
    return f"tmux new-session -d -s {session} {shlex.quote(inner)} && echo SUBMITTED"


def build_status_command(job_id: str, remote_ext_root: str) -> str:
    status = f"{remote_ext_root.rstrip('/')}/{JOBS_REL}/{job_id}/status"
    return f"cat {_tilde_safe(status)} 2>/dev/null || echo state=unknown"


def submit_job(spec: JobSpec, transport, remote_ext_root: str) -> Tuple[bool, str]:
    if hasattr(transport, "submit_spec"):   # REST — 큐잉은 데몬이 담당
        return transport.submit_spec(spec)
    rc, out = transport.run(build_submit_command(spec, remote_ext_root))
    ok = rc == 0 and "SUBMITTED" in out
    return ok, out


def read_status(job_id: str, transport, remote_ext_root: str) -> dict:
    """status 파일(KEY=VALUE 줄들) → dict. 접근 실패 시 state=unreachable."""
    if hasattr(transport, "job_status"):    # REST
        return transport.job_status(job_id)
    try:
        rc, out = transport.run(build_status_command(job_id, remote_ext_root))
    except Exception as exc:  # ssh 타임아웃 등 — 제어면은 죽지 않는다
        return {"state": "unreachable", "error": repr(exc)}
    if rc != 0:
        return {"state": "unreachable", "error": out[-200:]}
    status = {}
    for line in out.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            status[k.strip()] = v.strip()
    return status or {"state": "unknown"}


def _self_test() -> None:
    spec = JobSpec(job_id="gen-20260708-0001", episodes=75, duration=30,
                   stage="omniverse://10.38.38.32/Projects/A B/scene.usd",
                   upload_uri="s3://bucket/episodes/gen-20260708-0001")
    env = spec.to_env()
    assert env["EPISODES"] == "75" and env["DURATION"] == "30"
    assert env["KEEP_POSITIONS"] == ""  # 빈 값 → 전송 생략 대상
    cmd = build_submit_command(spec, "/home/x/ext")
    assert "tmux new-session -d -s job-gen-20260708-0001" in cmd
    assert "KEEP_POSITIONS" not in cmd, "빈 값은 명령에 포함되지 않아야"
    assert "'omniverse://10.38.38.32/Projects/A B/scene.usd'" in cmd, "공백 경로 인용 유지"
    assert cmd.endswith("&& echo SUBMITTED")
    # 잡 타입 → 러너 디스패치
    assert env["JOB_TYPE"] == "generate"
    train_spec = JobSpec(job_id="train-1", job_type="train", dataset="/data/bev-v3")
    assert "run_train.sh" in build_submit_command(train_spec, "/home/x/ext")
    serve_cmd = build_submit_command(
        JobSpec(job_id="serve-1", job_type="serve_start", model_path="/models/m"),
        "/home/x/ext")
    assert "run_serve.sh" in serve_cmd and "JOB_TYPE=serve_start" in serve_cmd
    # 상태 파싱
    class FakeTransport:
        def run(self, command, timeout=30.0):
            return 0, "state=running\nepisodes_done=3\ntotal=75"
    st = read_status("gen-x", FakeTransport(), "/home/x/ext")
    assert st == {"state": "running", "episodes_done": "3", "total": "75"}
    class DeadTransport:
        def run(self, command, timeout=30.0):
            raise TimeoutError("ssh timeout")
    assert read_status("gen-x", DeadTransport(), "/x")["state"] == "unreachable"
    # transport 판별
    assert isinstance(transport_from_host(""), LocalTransport)
    assert isinstance(transport_from_host("local"), LocalTransport)
    assert isinstance(transport_from_host("netai@sv4000-2"), SSHTransport)
    assert isinstance(transport_from_host("http://localhost:8800"), RESTTransport)
    # REST 경로 디스패치 (HTTP는 mock)
    calls = []

    class FakeREST(RESTTransport):
        def _request(self, method, path, payload=None, timeout=15.0):
            calls.append((method, path))
            if method == "POST":
                assert payload["job_id"] == spec.job_id and payload["seed"] == 42
                return {"job_id": payload["job_id"], "gpu": payload["gpu"], "ahead": 0}
            return {"state": "running", "episodes_done": "5", "total": "75"}

    rest = FakeREST("http://localhost:8800/")
    ok, msg = submit_job(spec, rest, "/unused")
    assert ok and "queued on gpu" in msg, msg
    st2 = read_status(spec.job_id, rest, "/unused")
    assert st2["state"] == "running" and calls == [("POST", "/jobs"), ("GET", f"/jobs/{spec.job_id}")]

    class DeadREST(RESTTransport):
        def _request(self, *a, **k):
            raise ConnectionError("tunnel down")
    ok2, msg2 = submit_job(spec, DeadREST("http://localhost:8800"), "/x")
    assert not ok2 and "failed" in msg2
    assert read_status("j", DeadREST("http://localhost:8800"), "/x")["state"] == "unreachable"
    print("remote_generation self-test OK")


if __name__ == "__main__":
    _self_test()
