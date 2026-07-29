"""Replay-fidelity paired comparison driver (WP2 clean 조건 겸용).

같은 에피소드의 physics 원본 영상과 trajectory 재연 렌더 영상을 쌍(pair)으로 만들어
동일 GT로 VLM 검출 성능을 비교한다 — "재연 경로가 VLM 입력으로서 원본과 동등한가"의
측정. 재연 비용(replay cost)이 있으면 그 크기가 교란 실험의 베이스라인 보정값이 된다.

로컬 PC(WSL, stdlib-only)에서 실행하는 자기완결 드라이버:
    - 자체 SSH 터널(-L)로 L40 job_api(8800)·vLLM(38011)에 직결 (GUI 터널과 무관)
    - 데이터 이동은 ssh+tar 스트림 (minIO SDK 불필요; 재연 소스는 L40 로컬 file:// 트레이스)
    - ffmpeg는 PATH의 ffmpeg 또는 Windows ffmpeg.exe(경로는 wslpath로 변환) 사용

단계(모두 멱등 — 산출물이 있으면 건너뛰므로 중단 후 재실행 = 이어하기):
    generate(선택) -> fetch-runs -> plan -> replay -> fetch-replays -> infer -> eval

사용 예 (EXT_ROOT에서):
    python3 -m gist.netai.time_travel_summarization.automation.replay_fidelity \
        --generate 14 --runs gen-20260718-153511 --n 15
    python3 -m ...replay_fidelity --runs gen-20260718-153511 <새 run> --n 15  # 재개

순수 헬퍼 검증: python3 automation/replay_fidelity.py --self-test
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

DEFAULT_SSH_HOST = "netai@10.38.38.40"   # WSL엔 sv4000-2 알리아스가 없어 IP 직결
DEFAULT_REMOTE_EXT = ("/home/netai/wonjune/kit-app-template/source/extensions/"
                      "gist.netai.time_travel_summarization")
API_LOCAL_PORT = 18800    # GUI 터널(8800)과 충돌하지 않는 로컬 포트
VLLM_LOCAL_PORT = 18011
MODEL = "Qwen3-VL-8B-Instruct"
PRESET = "twin_view"
APP_KIT = "my_company.my_usd_composer"   # L40 apps에 .kit 6개 — 러너 자동 발견 불가(GUI 기본값과 동일)
# Capture_camera가 이 스테이지 안에 있다 — 기존 성공 run(gen-20260718-*)의 manifest와 동일 값
STAGE = ("omniverse://10.38.38.32/Projects/Dream-AI_Plus_Twin/Workspace_Personal/"
         "swj/AI-Grad_Building/A_AI-Grad_Building.usd")


# --------------------------------------------------------------------------- #
# pure helpers (Kit/네트워크 무의존 -> --self-test)
# --------------------------------------------------------------------------- #
def hms_to_s(ts: str) -> int:
    """'HH:MM:SS' -> 자정 기준 초. 형식 불일치는 ValueError."""
    h, m, s = ts.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def parse_gt_events(csv_text: str, objid_to_label: Dict[str, str],
                    kinds: Set[str] = frozenset({"object"})) -> Dict[int, Set[int]]:
    """collisions CSV -> {초: 라벨 집합}. 학습 GT와 동일 규칙(kind=object만, 초 단위 합침)."""
    events: Dict[int, Set[int]] = {}
    lines = [ln for ln in csv_text.splitlines() if ln.strip()]
    header = [c.strip() for c in lines[0].split(",")]
    for ln in lines[1:]:
        row = dict(zip(header, (c.strip() for c in ln.split(","))))
        if kinds and row.get("kind") not in kinds:
            continue
        label = objid_to_label.get(row["objid"])
        if label is None:   # 라벨 맵 밖 객체는 GT에 못 들어간다 (build_dataset과 동일)
            continue
        events.setdefault(hms_to_s(row["timestamp"]), set()).add(int(label))
    return events


def parse_pred_events(result: dict) -> Dict[int, Set[int]]:
    """추론 결과 JSON(chunk_responses) -> {초: 라벨 집합}. 같은 초는 합집합."""
    events: Dict[int, Set[int]] = {}
    for chunk in result.get("chunk_responses", []):
        text = (chunk.get("content") or "").strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        if not (text.startswith("[") and text.endswith("]")):
            continue
        try:
            items = json.loads(text)
        except json.JSONDecodeError:
            continue
        for item in items or []:
            if isinstance(item, dict):
                for ts, ids in item.items():
                    try:
                        events.setdefault(hms_to_s(ts), set()).update(int(i) for i in ids)
                    except (ValueError, TypeError):
                        continue
    return events


def match_events(gt: Dict[int, Set[int]], pred: Dict[int, Set[int]],
                 tol: int) -> dict:
    """GT-예측을 ±tol초에서 1:1 그리디 매칭. 검출(시각 일치)과 귀속(ID 집합 일치) 분리.

    반환: events=[{t, ids, detected, attributed, pred_t}], fp=[(t, ids)],
          counts={det_tp, att_tp, fn, fp}
    """
    used: Set[int] = set()
    rows = []
    for t in sorted(gt):
        cands = [pt for pt in pred if pt not in used and abs(pt - t) <= tol]
        best = max(cands, key=lambda pt: (pred[pt] == gt[t], -abs(pt - t)),
                   default=None)
        detected = best is not None
        attributed = detected and pred[best] == gt[t]
        if detected:
            used.add(best)
        rows.append({"t": t, "ids": sorted(gt[t]), "detected": detected,
                     "attributed": attributed, "pred_t": best})
    fp = [(pt, sorted(pred[pt])) for pt in sorted(pred) if pt not in used]
    det_tp = sum(r["detected"] for r in rows)
    att_tp = sum(r["attributed"] for r in rows)
    return {"events": rows, "fp": fp,
            "counts": {"det_tp": det_tp, "att_tp": att_tp,
                       "fn": len(rows) - det_tp, "fp": len(fp)}}


def f1(tp: int, fp: int, fn: int) -> float:
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    return round(2 * p * r / (p + r), 4) if (p + r) else 0.0


def sign_test_p(b: int, c: int) -> float:
    """부호 검정(양측): 불일치 b vs c가 반반(p=0.5) 가설의 이항 p값."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return round(min(1.0, 2 * tail), 4)


def stratify_select(eps: List[dict], n: int) -> List[dict]:
    """GT 이벤트 수 0 / 1 / 2+ 층에서 라운드로빈으로 n개 선택(결정적 순서)."""
    strata: Dict[str, List[dict]] = {"0": [], "1": [], "2+": []}
    for ep in sorted(eps, key=lambda e: e["pair_id"]):
        k = len(ep["gt_events"])
        strata["0" if k == 0 else "1" if k == 1 else "2+"].append(ep)
    picked: List[dict] = []
    while len(picked) < n and any(strata.values()):
        for key in ("2+", "1", "0"):     # 이벤트 있는 에피소드 우선
            if strata[key] and len(picked) < n:
                picked.append(strata[key].pop(0))
    return picked


# --------------------------------------------------------------------------- #
# infra: SSH 터널 / REST / tar 스트림
# --------------------------------------------------------------------------- #
class Tunnel:
    """스크립트 전용 ssh -N -L 터널 — GUI(Connect Server) 터널과 독립."""

    def __init__(self, host: str):
        self.host = host
        self._proc: Optional[subprocess.Popen] = None

    def __enter__(self) -> "Tunnel":
        self._proc = subprocess.Popen(
            ["ssh", "-N", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             "-o", "ExitOnForwardFailure=yes",
             "-L", f"{API_LOCAL_PORT}:localhost:8800",
             "-L", f"{VLLM_LOCAL_PORT}:localhost:38011", self.host])
        for _ in range(30):              # /health가 열릴 때까지 최대 15s
            if self._proc.poll() is not None:
                raise RuntimeError(f"ssh tunnel to {self.host} died (키 인증/포트 확인)")
            try:
                api_get("/health")
                return self
            except Exception:
                time.sleep(0.5)
        raise RuntimeError("job_api /health unreachable through tunnel "
                           "(L40에서 job-api 데몬이 떠 있는지 확인)")

    def __exit__(self, *exc) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()


def _http(method: str, url: str, payload: Optional[dict] = None,
          timeout: float = 30.0) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def api_get(path: str) -> dict:
    return _http("GET", f"http://localhost:{API_LOCAL_PORT}{path}")


def api_post(path: str, payload: dict) -> dict:
    return _http("POST", f"http://localhost:{API_LOCAL_PORT}{path}", payload)


def ssh_run(host: str, command: str) -> str:
    proc = subprocess.run(["ssh", "-o", "BatchMode=yes", host, command],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"ssh failed: {command!r}: {proc.stderr.strip()[:200]}")
    return proc.stdout


def ssh_tar_fetch(host: str, remote_parent: str, names: List[str], dest: Path,
                  exclude: Optional[str] = None) -> None:
    """ssh 'tar cz' | tar xz — 다수 파일을 왕복 없이 한 스트림으로 가져온다."""
    dest.mkdir(parents=True, exist_ok=True)
    excl = f"--exclude='{exclude}' " if exclude else ""
    quoted = " ".join(f"'{n}'" for n in names)
    p1 = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", host,
         f"tar -cz -C '{remote_parent}' {excl}{quoted}"], stdout=subprocess.PIPE)
    p2 = subprocess.run(["tar", "-xz", "-C", str(dest)], stdin=p1.stdout)
    p1.wait()
    if p1.returncode != 0 or p2.returncode != 0:
        raise RuntimeError(f"tar fetch failed for {names} (rc={p1.returncode}/{p2.returncode})")


def poll_job(job_id: str, bound_s: float, interval: float = 30.0,
             label: str = "") -> None:
    start = time.time()
    while True:
        st = api_get(f"/jobs/{job_id}")
        state = st.get("state")
        prog = ""
        if st.get("episodes_done"):
            prog = f" ({st['episodes_done']}/{st.get('total', '?')} eps)"
        print(f"[poll] {label or job_id}: {state}{prog} "
              f"({int(time.time() - start)}s)", flush=True)
        if state == "done":
            return
        if state == "failed":
            raise RuntimeError(f"job {job_id} failed: {st.get('note', '')}")
        if time.time() - start > bound_s:
            raise RuntimeError(f"job {job_id} timeout after {bound_s:.0f}s (state={state})")
        time.sleep(interval)


# --------------------------------------------------------------------------- #
# ffmpeg: WSL에서 Windows ffmpeg.exe도 쓸 수 있게 경로 변환
# --------------------------------------------------------------------------- #
def find_ffmpeg() -> Tuple[str, bool]:
    """(실행 파일, is_windows_exe). FFMPEG env > ffmpeg > ffmpeg.exe 순."""
    for cand in (os.environ.get("FFMPEG"), shutil.which("ffmpeg"),
                 shutil.which("ffmpeg.exe")):
        if cand:
            return cand, cand.lower().endswith(".exe")
    raise RuntimeError("ffmpeg not found (PATH 또는 FFMPEG env)")


def wslpath_w(path: Path) -> str:
    return subprocess.run(["wslpath", "-w", str(path)], capture_output=True,
                          text=True, check=True).stdout.strip()


# --------------------------------------------------------------------------- #
# vLLM client: 기존 VLLMClient 재사용, HTTP는 urllib·ffmpeg는 exe-aware로 교체
# --------------------------------------------------------------------------- #
def make_client(work_dir: Path):
    from gist.netai.time_travel_summarization.utils.vllm_client import VLLMClient
    from gist.netai.time_travel_summarization.vlm_client.prompts import PROMPTS

    ffmpeg, is_exe = find_ffmpeg()
    tmp_dir = work_dir / "tmp"          # exe는 WSL /tmp에 못 쓴다 — /mnt/c 아래 고정
    tmp_dir.mkdir(parents=True, exist_ok=True)

    def _p(path: Path) -> str:
        return wslpath_w(path) if is_exe else str(path)

    class Client(VLLMClient):
        def _post(self, payload: dict) -> dict:   # requests 의존 제거 (stdlib urllib)
            return _http("POST", f"{self.base_url}/v1/chat/completions", payload,
                         timeout=self.request_timeout)

        def probe_duration(self, video: Path) -> float:
            from gist.netai.time_travel_summarization.utils.vllm_client import parse_duration_s
            proc = subprocess.run([ffmpeg, "-i", _p(Path(video))],
                                  capture_output=True, text=True)
            dur = parse_duration_s(proc.stderr or "")
            if dur is None:
                raise RuntimeError(f"duration parse failed for {video}")
            return dur

        def _encode_chunk(self, video: Path, start: float, dur: float) -> str:
            import base64
            out = tmp_dir / "chunk.mp4"   # 인코딩 파라미터는 원본 구현과 동일 유지
            cmd = [ffmpeg, "-y", "-i", _p(Path(video)), "-ss", f"{start:.3f}",
                   "-t", f"{dur:.3f}", "-an", "-c:v", "libx264",
                   "-pix_fmt", "yuv420p", "-preset", "veryfast", _p(out)]
            proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, text=True)
            if proc.returncode != 0 or not out.exists():
                tail = (proc.stderr or "").strip().splitlines()[-1:]
                raise RuntimeError(f"ffmpeg slice failed @{start:.1f}s: {tail}")
            return base64.b64encode(out.read_bytes()).decode("ascii")

    return Client(f"http://localhost:{VLLM_LOCAL_PORT}", PROMPTS)


# --------------------------------------------------------------------------- #
# phases
# --------------------------------------------------------------------------- #
def phase_generate(args) -> Optional[str]:
    """신규 생성 잡 제출·완주 대기. 반환: run 이름(=job_id)."""
    if not args.generate:
        return None
    resp = api_post("/jobs", {"job_type": "generate", "episodes": args.generate,
                              "duration": 30.0, "gpu": args.gpu, "app_kit": APP_KIT,
                              "render_fps": 30, "camera": "Capture_camera",
                              "stage": STAGE})
    job_id = resp["job_id"]
    bound = (180 + args.generate * (30 * 7.2 + 30)) * 1.2
    print(f"[generate] submitted {job_id} ({args.generate} eps, bound {bound / 60:.0f}min)")
    poll_job(job_id, bound, interval=60.0, label="generate")
    return job_id


def phase_fetch_runs(args, runs: List[str], out: Path) -> None:
    """run 메타데이터(영상 제외)를 tar 스트림으로 로컬 복제 (있으면 스킵)."""
    ep_remote = f"{args.remote_ext_root}/artifacts/episodes"
    missing = [r for r in runs if not (out / "episodes" / r).is_dir()]
    if missing:
        print(f"[fetch-runs] {missing} (metadata, no videos)")
        ssh_tar_fetch(args.ssh_host, ep_remote, missing, out / "episodes",
                      exclude="*.mp4")


def phase_plan(args, runs: List[str], out: Path) -> List[dict]:
    """에피소드 파싱·sim-clock 게이트·층화 선택 -> pairs.json (있으면 그대로 재사용)."""
    pairs_path = out / "pairs.json"
    if pairs_path.exists():
        pairs = json.loads(pairs_path.read_text(encoding="utf-8"))
        print(f"[plan] reuse pairs.json ({len(pairs)} pairs)")
        return pairs

    candidates = []
    for run in runs:
        run_dir = out / "episodes" / run
        manifest = json.loads((run_dir / "_run_manifest.json").read_text(encoding="utf-8"))
        run_args = manifest.get("args", {})
        for ep_dir in sorted(run_dir.glob("ep_*")):
            metas = list(ep_dir.glob("_video_*.meta.json"))
            gts = list(ep_dir.glob("collisions_*.csv"))
            traces = list(ep_dir.glob("_trace_*.csv"))
            if not (metas and gts and traces):
                print(f"[plan] skip {run}/{ep_dir.name}: incomplete files")
                continue
            meta = json.loads(metas[0].read_text(encoding="utf-8"))
            cap_start = datetime.datetime.fromisoformat(meta["capture_start"])
            duration = float(meta["duration_s"])

            # sim-clock 게이트: 트레이스 시각이 캡처 앵커와 어긋나면 wall-clock 시절 산출
            rows = traces[0].read_text(encoding="utf-8").splitlines()
            t_first = datetime.datetime.fromisoformat(rows[1].split(",")[0])
            t_last = datetime.datetime.fromisoformat(rows[-1].split(",")[0])
            span = (t_last - t_first).total_seconds()
            if abs((t_first - cap_start).total_seconds()) > 5 or span > duration * 1.3:
                print(f"[plan] EXCLUDE {run}/{ep_dir.name}: wall-clock trace "
                      f"(start off {abs((t_first - cap_start).total_seconds()):.0f}s, "
                      f"span {span:.0f}s vs {duration:.0f}s)")
                continue

            gt = parse_gt_events(gts[0].read_text(encoding="utf-8"),
                                 {str(k): str(v) for k, v in
                                  (meta.get("objid_to_label") or {}).items()})
            candidates.append({
                "pair_id": f"{run}-{ep_dir.name}".replace("_", "-"),
                "run": run, "ep": ep_dir.name,
                "video": meta["video"], "trace": traces[0].name,
                "capture_start": meta["capture_start"], "duration_s": duration,
                "fps": int(meta.get("fps", 30)),
                "stage": run_args.get("stage") or "",
                "camera": run_args.get("camera") or "Capture_camera",
                "gt_events": {str(t): sorted(ids) for t, ids in sorted(gt.items())},
            })

    pairs = stratify_select(candidates, args.n)
    if not pairs:
        raise RuntimeError("no usable episodes (sim-clock 트레이스 run이 필요 — --generate)")
    pairs_path.write_text(json.dumps(pairs, indent=1, ensure_ascii=False), encoding="utf-8")
    dist = [len(p["gt_events"]) for p in pairs]
    print(f"[plan] selected {len(pairs)}/{len(candidates)} pairs, "
          f"gt-event counts {sorted(dist)}")
    return pairs


def _resolve_replay_job(pair_id: str) -> Tuple[str, str]:
    """이 쌍의 유효 job_id와 서버 상태를 찾는다.

    실패한 job_id는 재제출이 불가(409 — status 파일이 남음)하므로, failed면
    -r2, -r3.. 접미사로 다음 시도 id를 찾는다. 반환 상태: "new"(미제출) 또는
    서버 state(queued|starting|running|done).
    """
    for attempt in range(1, 10):
        jid = f"fid-{pair_id}" + ("" if attempt == 1 else f"-r{attempt}")
        try:
            st = api_get(f"/jobs/{jid}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return jid, "new"
            raise
        if st.get("state") != "failed":
            return jid, str(st.get("state"))
    raise RuntimeError(f"{pair_id}: r9까지 전부 실패 — 잡 로그 확인 필요")


def phase_replay(args, pairs: List[dict], out: Path) -> None:
    """쌍마다 replay 잡 제출(file:// 트레이스, 원본과 동일 stage/camera/fps) 후 완주 대기."""
    todo = []
    for p in pairs:
        job_id, state = _resolve_replay_job(p["pair_id"])
        p["job_id"] = job_id
        if (out / "replays" / job_id).is_dir():   # 이미 가져온 쌍은 통째로 스킵
            continue
        if state == "done":
            todo.append((p, job_id, True))        # 렌더 완료 — fetch만 남음
            continue
        if state == "new":
            start = datetime.datetime.fromisoformat(p["capture_start"])
            end = start + datetime.timedelta(seconds=p["duration_s"])
            fmt = "%Y-%m-%d %H:%M:%S"
            trace_uri = (f"file://{args.remote_ext_root}/artifacts/episodes/"
                         f"{p['run']}/{p['ep']}/{p['trace']}")
            try:
                api_post("/jobs", {
                    "job_type": "replay", "job_id": job_id, "gpu": args.gpu,
                    "replay_start": start.strftime(fmt), "replay_end": end.strftime(fmt),
                    "data_uri": trace_uri, "render_fps": p["fps"], "app_kit": APP_KIT,
                    "camera": p["camera"], "stage": p["stage"]})
                print(f"[replay] submitted {job_id}")
            except urllib.error.HTTPError as e:
                if e.code != 409:                  # 409 = 경합 제출 — 폴링으로 합류
                    raise
        todo.append((p, job_id, False))           # queued/starting/running 포함

    (out / "pairs.json").write_text(json.dumps(pairs, indent=1, ensure_ascii=False),
                                    encoding="utf-8")
    per_job_bound = (180 + 30 * 8 + 60) * 1.2 + 60
    skipped = []
    for i, (p, job_id, done) in enumerate(todo):
        if done:
            continue
        try:                                       # 순차 큐 — 대기분까지 상한에 반영
            poll_job(job_id, per_job_bound * (len(todo) - i), label=job_id)
            continue
        except RuntimeError as e:
            print(f"[replay] {job_id} failed ({e}); 재시도")
        ok = False
        for _ in range(2):                         # 새 -rN id로 재제출·재폴링
            nxt, _st = _resolve_replay_job(p["pair_id"])
            if nxt == job_id:
                break
            start = datetime.datetime.fromisoformat(p["capture_start"])
            end = start + datetime.timedelta(seconds=p["duration_s"])
            fmt = "%Y-%m-%d %H:%M:%S"
            try:
                api_post("/jobs", {
                    "job_type": "replay", "job_id": nxt, "gpu": args.gpu,
                    "replay_start": start.strftime(fmt), "replay_end": end.strftime(fmt),
                    "data_uri": (f"file://{args.remote_ext_root}/artifacts/episodes/"
                                 f"{p['run']}/{p['ep']}/{p['trace']}"),
                    "render_fps": p["fps"], "app_kit": APP_KIT,
                    "camera": p["camera"], "stage": p["stage"]})
            except urllib.error.HTTPError as ex:
                if ex.code != 409:
                    raise
            job_id = nxt
            p["job_id"] = nxt
            try:
                poll_job(nxt, per_job_bound, label=nxt)
                ok = True
                break
            except RuntimeError as e2:
                print(f"[replay] {nxt} 재시도 실패 ({e2})")
        if not ok:
            p["_skip"] = True
            skipped.append(p["pair_id"])
    if skipped:
        print(f"[replay] SKIPPED {len(skipped)}: {skipped}")
        pairs[:] = [p for p in pairs if not p.get("_skip")]   # 다운스트림에서 제외


def phase_fetch_replays(args, pairs: List[dict], out: Path) -> None:
    remote = f"{args.remote_ext_root}/artifacts/replays"
    missing = [p["job_id"] for p in pairs if not (out / "replays" / p["job_id"]).is_dir()]
    if missing:
        print(f"[fetch-replays] {missing}")
        ssh_tar_fetch(args.ssh_host, remote, missing, out / "replays")


def phase_fetch_videos(args, pairs: List[dict], out: Path) -> None:
    """선택된 쌍의 physics 원본 영상만 가져온다 (plan 후라 선별 다운로드 가능)."""
    ep_remote = f"{args.remote_ext_root}/artifacts/episodes"
    names = [f"{p['run']}/{p['ep']}/{p['video']}" for p in pairs
             if not (out / "episodes" / p["run"] / p["ep"] / p["video"]).exists()]
    if names:
        print(f"[fetch-videos] {len(names)} physics videos")
        ssh_tar_fetch(args.ssh_host, ep_remote, names, out / "episodes")


def _replay_video(out: Path, p: dict) -> Path:
    vids = list((out / "replays" / p["job_id"]).glob("*.mp4"))
    if len(vids) != 1:
        raise RuntimeError(f"{p['job_id']}: expected 1 replay mp4, got {len(vids)}")
    return vids[0]


def phase_infer(args, pairs: List[dict], out: Path) -> None:
    client = make_client(out)
    infer_dir = out / "infer"
    infer_dir.mkdir(exist_ok=True)
    for p in pairs:
        for side, video in (("physics", out / "episodes" / p["run"] / p["ep"] / p["video"]),
                            ("replay", _replay_video(out, p))):
            dst = infer_dir / f"{p['pair_id']}_{side}.json"
            if dst.exists():
                continue
            print(f"[infer] {p['pair_id']} {side}")
            result = client.analyze_video(str(video), model=MODEL, preset_name=PRESET)
            if result["num_errors"]:
                raise RuntimeError(f"{dst.name}: {result['num_errors']} chunk errors "
                                   "(서빙 상태 확인 — 결과 미저장, 재실행 시 재시도)")
            client.save_json(result, str(dst))


def phase_eval(pairs: List[dict], out: Path) -> dict:
    per_ep, agree = [], {"both": 0, "physics_only": 0, "replay_only": 0, "neither": 0}
    for p in pairs:
        gt = {int(t): set(ids) for t, ids in p["gt_events"].items()}
        sides = {}
        for side in ("physics", "replay"):
            result = json.loads((out / "infer" / f"{p['pair_id']}_{side}.json")
                                .read_text(encoding="utf-8"))
            pred = parse_pred_events(result)
            sides[side] = {
                "num_chunks": result["num_chunks"],
                "strict": match_events(gt, pred, tol=0),
                "tol1": match_events(gt, pred, tol=1),
            }
        if sides["physics"]["num_chunks"] != sides["replay"]["num_chunks"]:
            print(f"[eval] WARN {p['pair_id']}: chunk count mismatch "
                  f"{sides['physics']['num_chunks']} vs {sides['replay']['num_chunks']}")
        # GT 이벤트별 일치표(±1s 검출 기준) — 체계적 차이는 physics_only/replay_only 비대칭으로 드러난다
        for ev_p, ev_r in zip(sides["physics"]["tol1"]["events"],
                              sides["replay"]["tol1"]["events"]):
            key = ("both" if ev_p["detected"] and ev_r["detected"] else
                   "physics_only" if ev_p["detected"] else
                   "replay_only" if ev_r["detected"] else "neither")
            agree[key] += 1
        row = {"pair_id": p["pair_id"], "n_gt": len(gt)}
        for side in ("physics", "replay"):
            for tol in ("strict", "tol1"):
                c = sides[side][tol]["counts"]
                row[f"{side}_{tol}"] = dict(c)
                row[f"{side}_{tol}_f1_det"] = f1(c["det_tp"], c["fp"], c["fn"])
                row[f"{side}_{tol}_f1_att"] = f1(c["att_tp"],
                                                 c["fp"] + c["det_tp"] - c["att_tp"],
                                                 c["fn"])
            row[f"{side}_fp_events"] = sides[side]["tol1"]["fp"]
        row["delta_f1_det_tol1"] = round(row["replay_tol1_f1_det"]
                                         - row["physics_tol1_f1_det"], 4)
        per_ep.append(row)

    def agg(side: str, tol: str) -> dict:
        keys = ("det_tp", "att_tp", "fn", "fp")
        tot = {k: sum(r[f"{side}_{tol}"][k] for r in per_ep) for k in keys}
        tot["f1_det"] = f1(tot["det_tp"], tot["fp"], tot["fn"])
        tot["f1_att"] = f1(tot["att_tp"], tot["fp"] + tot["det_tp"] - tot["att_tp"],
                           tot["fn"])
        return tot

    deltas = [r["delta_f1_det_tol1"] for r in per_ep if r["delta_f1_det_tol1"] != 0]
    summary = {
        "n_pairs": len(per_ep),
        "gt_events_total": sum(r["n_gt"] for r in per_ep),
        "aggregate": {f"{s}_{t}": agg(s, t) for s in ("physics", "replay")
                      for t in ("strict", "tol1")},
        "gt_event_agreement_tol1": agree,
        "mcnemar_discordant": {"physics_only": agree["physics_only"],
                               "replay_only": agree["replay_only"],
                               "sign_test_p": sign_test_p(agree["physics_only"],
                                                          agree["replay_only"])},
        "episode_delta_f1": {"replay_better": sum(d > 0 for d in deltas),
                             "physics_better": sum(d < 0 for d in deltas),
                             "tied": len(per_ep) - len(deltas),
                             "sign_test_p": sign_test_p(sum(d > 0 for d in deltas),
                                                        sum(d < 0 for d in deltas))},
        "per_episode": per_ep,
    }
    (out / "results.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False),
                                      encoding="utf-8")

    lines = ["# Replay fidelity - paired comparison", "",
             f"pairs: {summary['n_pairs']}, GT events: {summary['gt_events_total']}", "",
             "| metric | physics | replay |", "|---|---|---|"]
    for tol in ("strict", "tol1"):
        a, b = summary["aggregate"][f"physics_{tol}"], summary["aggregate"][f"replay_{tol}"]
        lines += [f"| {tol} F1 (detection) | {a['f1_det']} | {b['f1_det']} |",
                  f"| {tol} F1 (attribution) | {a['f1_att']} | {b['f1_att']} |",
                  f"| {tol} FP | {a['fp']} | {b['fp']} |"]
    ag, mc = summary["gt_event_agreement_tol1"], summary["mcnemar_discordant"]
    lines += ["", f"GT-event agreement (tol1): both={ag['both']} "
              f"physics_only={ag['physics_only']} replay_only={ag['replay_only']} "
              f"neither={ag['neither']}  (sign test p={mc['sign_test_p']})",
              f"episode-level dF1(det,tol1): replay_better="
              f"{summary['episode_delta_f1']['replay_better']} physics_better="
              f"{summary['episode_delta_f1']['physics_better']} "
              f"tied={summary['episode_delta_f1']['tied']} "
              f"(p={summary['episode_delta_f1']['sign_test_p']})"]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return summary


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", nargs="*", default=[],
                    help="비교에 쓸 기존 run 이름들 (L40 artifacts/episodes/ 아래)")
    ap.add_argument("--generate", type=int, default=0,
                    help="신규 생성 잡 에피소드 수 (0=생성 안 함)")
    ap.add_argument("--n", type=int, default=15, help="비교 쌍 수 (층화 선택)")
    ap.add_argument("--gpu", type=int, default=1, help="생성·재연 잡 GPU (serve GPU 제외)")
    ap.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    ap.add_argument("--remote-ext-root", default=DEFAULT_REMOTE_EXT)
    ap.add_argument("--out", default="artifacts/replay_fidelity",
                    help="산출 루트 (/mnt/c 아래여야 ffmpeg.exe가 쓸 수 있다)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        _self_test()
        return

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    with Tunnel(args.ssh_host):
        new_run = phase_generate(args)
        runs = list(args.runs) + ([new_run] if new_run else [])
        if not runs:
            raise SystemExit("--runs 또는 --generate 필요")
        phase_fetch_runs(args, runs, out)
        pairs = phase_plan(args, runs, out)
        phase_replay(args, pairs, out)
        phase_fetch_replays(args, pairs, out)
        phase_fetch_videos(args, pairs, out)
        phase_infer(args, pairs, out)
        phase_eval(pairs, out)
    print(f"\n[done] results: {out / 'report.md'}")


def _self_test() -> None:
    assert hms_to_s("03:44:18") == 3 * 3600 + 44 * 60 + 18
    # GT 파싱: object만, 초 단위 합침, 라벨 맵 적용
    csv_text = ("timestamp,objid,x,y,z,kind\n"
                "03:44:18,obj002,1,2,3,wall\n"
                "03:44:20,obj001,1,2,3,object\n"
                "03:44:20,obj002,1,2,3,object\n"
                "03:44:25,obj003,1,2,3,object\n")
    gt = parse_gt_events(csv_text, {"obj001": "1", "obj002": "2", "obj003": "3"})
    assert gt == {hms_to_s("03:44:20"): {1, 2}, hms_to_s("03:44:25"): {3}}
    # 예측 파싱: 코드블록·평문 배열·쓰레기 혼재
    result = {"chunk_responses": [
        {"content": '[{"03:44:20": [1, 2]}]'},
        {"content": '```json\n[{"03:44:26": [3]}]\n```'},
        {"content": "no json"}, {"content": "[]"}]}
    pred = parse_pred_events(result)
    assert pred == {hms_to_s("03:44:20"): {1, 2}, hms_to_s("03:44:26"): {3}}
    # 매칭: strict는 26!=25 미검출, tol1은 검출·귀속 성공
    m0 = match_events(gt, pred, tol=0)
    assert m0["counts"] == {"det_tp": 1, "att_tp": 1, "fn": 1, "fp": 1}
    m1 = match_events(gt, pred, tol=1)
    assert m1["counts"] == {"det_tp": 2, "att_tp": 2, "fn": 0, "fp": 0}
    # 귀속 분리: 시각은 맞고 ID가 틀리면 detected=True, attributed=False
    m2 = match_events({100: {1, 2}}, {100: {1, 3}}, tol=0)
    assert m2["counts"] == {"det_tp": 1, "att_tp": 0, "fn": 0, "fp": 0}
    assert f1(2, 0, 0) == 1.0 and f1(0, 3, 2) == 0.0
    assert sign_test_p(0, 0) == 1.0 and sign_test_p(5, 5) == 1.0
    assert sign_test_p(8, 0) < 0.01
    # 층화: 이벤트 많은 층 우선 라운드로빈
    eps = [{"pair_id": f"e{i}", "gt_events": {str(t): [1] for t in range(k)}}
           for i, k in enumerate([0, 0, 1, 2, 3])]
    sel = stratify_select(eps, 3)
    assert [len(e["gt_events"]) for e in sel] == [2, 1, 0]
    print("replay_fidelity self-test OK")


if __name__ == "__main__":
    main()
