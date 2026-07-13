"""캡처 수명주기 — 사이드카·백그라운드 워커·headless 실행.

상태는 전부 core(TimeTravelCore)에 남기고 여기는 동작만 둔다(분해 원칙:
테스트가 __new__ + 속성 주입으로 core를 만들므로 서비스가 상태를 들면 깨진다).
"""
import datetime
from pathlib import Path
from typing import Optional

import carb

from . import physics_service


def _default_output_path(core) -> str:
    """비디오 기본 출력 경로: lake 모드면 설정 URI, 아니면 로컬 artifacts/video."""
    ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    video_output_uri = core.get_video_output_uri_for_active_mode()
    if video_output_uri:
        return f"{video_output_uri.rstrip('/')}/video_{ts}.mp4"
    output_dir_str = (
        getattr(core._config, "video_output_dir", "artifacts/video")
        if core._config
        else "artifacts/video"
    )
    output_dir = Path(output_dir_str)
    if not output_dir.is_absolute():
        output_dir = core._module_dir / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    return str(output_dir / f"video_{ts}.mp4")


def capture_anchor(core) -> datetime.datetime:
    """캡처 앵커(t0) = 그 모드의 '내부 시계'가 캡처 시작 순간 가리키는 값.

    앵커는 "영상의 0초 = 시계로 몇 시"라는 변환 기준점이다. 오버레이·라벨·VLM
    보고는 전부 내부 시계로 말하므로 앵커도 같은 시계여야 시각 복원이 맞는다.
    - playback(재연): 오버레이가 재연 중인 데이터 시각을 표시 → 앵커 = 현재
      재생 헤드의 데이터 시각. (벽시계를 쓰면 이벤트가 캡처한 날짜에 붙는 오류)
    - physics: 내부 시계 자체가 t0에서 출발 → 벽시계(배치는 무작위 t0 주입).
    """
    if core._playback.get_mode() == "playback":
        data_now = core._playback.get_current_time()
        if data_now is not None:
            return data_now
    return datetime.datetime.now()


def write_capture_sidecar(core, output_path: str, duration_s: float, fps: Optional[int] = None) -> None:
    """Write ``<video>.meta.json`` next to the captured video (local or s3).

    Records the internal-clock anchor (capture_start — the t0 that maps
    timestamp readings to video offsets), the wall clock separately, the
    replay window when capturing a playback session, the active collisions
    CSV, and the objid->numeric-label map. Best-effort: never blocks capture.
    """
    import json as _json
    import re as _re

    from ..video_capture import CaptureRequest

    try:
        collisions_csv = (
            str(core._collisions.output_path) if core._collisions is not None else None
        )
        # objid -> numeric label, same rule as the burned-in overlay labels.
        objid_to_label = {}
        for objid in core._prim_map.keys():
            m = _re.search(r"(\d+)$", str(objid))
            objid_to_label[str(objid)] = str(int(m.group(1))) if m else str(objid)
        # capture_start는 내부 시계 앵커(_capture_start_dt)와 "동일 객체"를 사용.
        # 별도 조회면 수 ms 어긋나 라벨 오프셋이 드리프트한다.
        t0 = core._capture_start_dt or datetime.datetime.now()
        mode = core.get_mode()
        meta = {
            "capture_start": t0.isoformat(),
            # 관리용 벽시계(파일이 실제로 만들어진 시각) — 앵커와 역할 분리.
            "wall_clock": datetime.datetime.now().isoformat(),
            "duration_s": float(duration_s),
            "fps": int(fps) if fps is not None else CaptureRequest.fps,
            "width": CaptureRequest.width,
            "height": CaptureRequest.height,
            "mode": mode,
            "video": output_path.rsplit("/", 1)[-1] if "://" in output_path else str(Path(output_path).name),
            "collisions_csv": collisions_csv,
            "collision_distance": getattr(core, "_collision_distance", None),
            "objid_to_label": objid_to_label,
        }
        if mode == "playback":
            # 재연 창(데이터 시각 기준) — 이벤트 인덱스의 날짜 복원·롤오버 판단 근거.
            rs = core._playback.get_start_time()
            re_end = core._playback.get_end_time()
            meta["replay_start"] = rs.isoformat() if rs else None
            meta["replay_end"] = re_end.isoformat() if re_end else None
        payload = _json.dumps(meta, indent=2, ensure_ascii=False)
        if "://" in output_path and not output_path.startswith("file://"):
            # 원격(s3 등): 영상 옆에 같은 이름의 사이드카를 storage adapter로 기록.
            # (과거엔 skip — 레이크 재연 캡처의 시각 복원이 불가능했다)
            from ..storage import from_uri

            meta_uri = output_path.rsplit(".", 1)[0] + ".meta.json"
            from_uri(meta_uri).put_bytes(meta_uri, payload.encode("utf-8"),
                                         content_type="application/json")
            carb.log_warn(f"[Capture] sidecar -> {meta_uri}")
            return
        local_path = output_path
        if local_path.startswith("file://"):
            from urllib.parse import urlparse
            from urllib.request import url2pathname

            local_path = url2pathname(urlparse(local_path).path)
        meta_path = Path(local_path).with_suffix(".meta.json")
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(payload, encoding="utf-8")
        carb.log_warn(f"[Capture] sidecar -> {meta_path}")
    except Exception as exc:
        carb.log_warn(f"[Capture] sidecar write failed: {exc!r}")


def start_capture(core, duration_s: float = 0.0, output_path: Optional[str] = None) -> bool:
    """실시간 viewport 캡처 시작. duration_s=0 이면 default 60초. 중간에 stop_capture로 중단 가능."""
    if core._capture_active:
        carb.log_warn("[Capture] already active")
        return False
    # duration_s 0 이하 → default 60초
    effective_duration = float(duration_s) if duration_s > 0 else 60.0
    if output_path is None:
        output_path = _default_output_path(core)
    core._capture_active = True
    core._capture_duration_s = effective_duration
    core._capture_output_path = output_path
    # 충돌 기록 창 == 캡처 창: physics 모드면 캡처와 함께 recorder 시작(사이드카 전에
    # 시작해야 collisions_csv 경로가 링크됨). → CSV 길이가 영상 길이와 일치.
    if core._playback.get_mode() == "physics":
        physics_service.start_collision_recorder(core)
    # Sidecar links this video to its collision labels + an exact t0 so the
    # offline dataset builder can slice clips and assign labels deterministically.
    core._capture_start_dt = capture_anchor(core)
    write_capture_sidecar(core, output_path, effective_duration)
    import threading
    import time as _time
    core._capture_stop_event = threading.Event()
    core._capture_start_time = _time.perf_counter()
    _start_capture_backend(core, output_path)
    carb.log_warn(f"[Capture] started duration={effective_duration:g}s output={output_path}")
    return True


def run_capture_headless(core, duration_s: float = 0.0, output_path: Optional[str] = None,
                         camera_path: Optional[str] = None,
                         capture_start_dt: Optional[datetime.datetime] = None,
                         render_fps: Optional[int] = None) -> Optional[str]:
    """Blocking offscreen capture for headless automation (no viewport).

    Writes the same <video>.meta.json sidecar as start_capture, then runs the
    render-product capture synchronously (it pumps the app so physics advances
    and frames render). Returns the output path on success, else None.
    """
    effective_duration = float(duration_s) if duration_s > 0 else 60.0
    if output_path is None:
        output_path = _default_output_path(core)
    if core._playback.get_mode() == "physics":
        physics_service.start_collision_recorder(core)
    # sim-time 클럭 앵커: 이 시각 + sim 경과가 오버레이/CSV/사이드카의 단일 t0.
    # 배치 생성은 에피소드별 무작위 t0를 주입(숫자 다양성; 실행 시각 비종속).
    core._capture_start_dt = capture_start_dt or capture_anchor(core)
    core._sim_time = 0.0
    core._use_sim_clock = True
    # 실측(프로브): 이 Kit의 app.update() 고정 스텝 = 1/60s (timeCodesPerSecond 무시).
    # → sim은 60Hz 고정. render_fps(60의 약수)를 주면 렌더·인코딩만 데시메이션되어
    #   비디오는 그 fps가 된다(라벨 시각은 스텝 기준이라 정합 불변). 10Hz 데이터셋은
    #   build_dataset --content-hz 10으로 최종 데시메이션(B' 경로).
    headless_fps = 60
    _rfps = int(render_fps) if render_fps else headless_fps
    vid_fps = headless_fps // max(1, int(round(headless_fps / max(1, _rfps))))
    # 사이드카 fps = 실제 비디오 fps여야 build_dataset의 시각→프레임 매핑이 맞는다.
    write_capture_sidecar(core, output_path, effective_duration, fps=vid_fps)
    from ..video_capture import CaptureRequest, RealtimeCaptureRunner
    output_uri = output_path if "://" in output_path else Path(output_path).resolve().as_uri()
    req = CaptureRequest(duration_s=effective_duration, fps=headless_fps,
                         output_uri=output_uri, label="headless_capture",
                         render_fps=vid_fps)
    try:
        res = RealtimeCaptureRunner(core=core).capture_headless(req, camera_path=camera_path)
    finally:
        core._use_sim_clock = False
        physics_service.stop_collision_recorder(core)  # 캡처 종료 == 충돌 기록 종료
    if not res.success:
        carb.log_warn(f"[Capture] headless FAILED: {res.error}")
        return None
    carb.log_warn(f"[Capture] headless done -> {res.output_uri}")
    return output_path


def stop_capture(core) -> Optional[str]:
    if not core._capture_active:
        return None
    out = core._capture_output_path
    # stop_event 신호 → background worker가 capture loop 빠져나오고 인코더 마무리 후 파일 저장
    if getattr(core, "_capture_stop_event", None) is not None:
        core._capture_stop_event.set()
    carb.log_warn(f"[Capture] stop requested -> {out}")
    return out


def _start_capture_backend(core, output_path: str) -> None:
    """RealtimeCaptureRunner를 background thread에서 실행. duration은 runner 내부에서 자동 처리."""
    import threading
    from urllib.parse import urlparse

    from ..video_capture import CaptureRequest, RealtimeCaptureRunner

    # duration_s=0 (무한)이면 기본 60초로 fallback. runner.capture는 blocking이라 외부 stop 불가.
    duration = core._capture_duration_s if core._capture_duration_s > 0 else 60.0
    output_uri = output_path if "://" in output_path else Path(output_path).resolve().as_uri()

    def _worker():
        try:
            runner = RealtimeCaptureRunner(core=core)
            req = CaptureRequest(duration_s=duration, output_uri=output_uri, label="ui_capture")
            res = runner.capture(req, stop_event=core._capture_stop_event)
            if res.success:
                meta = res.metadata or {}
                parsed_output = urlparse(res.output_uri)
                video_name = Path(parsed_output.path or res.output_uri).name
                carb.log_warn(
                    f"[Capture] done {res.wall_clock_s:.1f}s "
                    f"{res.output_size_bytes // 1024}KB "
                    f"frames={meta.get('frames_written', '?')}/{meta.get('frames_requested', '?')} "
                    f"completed={meta.get('frames_completed', '?')} "
                    f"dup={meta.get('duplicate_frames', '?')} "
                    f"drop={res.dropped_frames}"
                )
                carb.log_warn(f"[Capture] video_uri={res.output_uri}")
                carb.log_warn(f"[Capture] video_name={video_name}")
                # VLM 창 Source 자동 채움(수동 복붙 대체). 콜백 실패가 캡처를 깨지 않게.
                cb = getattr(core, "_capture_complete_cb", None)
                if cb is not None:
                    try:
                        cb(res.output_uri)
                    except Exception as exc:
                        carb.log_warn(f"[Capture] complete callback failed: {exc!r}")
            else:
                carb.log_warn(f"[Capture] FAILED: {res.error}")
        except Exception as exc:
            carb.log_warn(f"[Capture] worker exception: {exc!r}")
        finally:
            core._capture_active = False
            core._capture_pipeline = None
            core._capture_stop_event = None
            physics_service.stop_collision_recorder(core)  # 캡처 종료 == 충돌 기록 종료

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    core._capture_pipeline = thread
