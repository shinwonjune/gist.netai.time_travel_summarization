"""Headless replay-range renderer (run via `kit --exec`).

좌표 데이터의 한 twin-time 구간(start~end)을 headless로 재연(playback)하며 렌더해
영상+사이드카를 만들고(선택) minIO에 업로드한다. 생성(generate_episodes)이 물리를
돌려 새 데이터를 만드는 것과 달리, 여기서는 기존 좌표 데이터를 프레임마다 재생 헤드로
직접 재연만 한다(§6-1 GUI 캡처 투영 오차를 피하는 camera_params 정합 경로).

Per-run flow (single Kit session):
    open stage -> load coord data (DATA_PATH 또는 LAKE_DATASET) -> regenerate objects
    -> validate window in data range -> load_time_range(start,end)
    -> run_capture_headless(replay_start_dt=start) -> organize + optional upload
    -> print '[replay] done.'  (워치독 완료 마커)

Pure helpers (parse_dt/validate_window/replay_output_name)는 Kit 없이 임포트·테스트 가능:
    python automation/replay_range.py --self-test
"""

from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
from typing import Optional

_DT_FMT = "%Y-%m-%d %H:%M:%S"


# --------------------------------------------------------------------------- #
# pure helpers (no Kit dependency -> unit-testable)
# --------------------------------------------------------------------------- #
def parse_dt(value: str) -> datetime.datetime:
    """ISO 'YYYY-MM-DD HH:MM:SS' → datetime. 형식 불일치는 ValueError(호출부 검증)."""
    return datetime.datetime.strptime(value.strip(), _DT_FMT)


def validate_window(start: datetime.datetime, end: datetime.datetime,
                    data_start: Optional[datetime.datetime],
                    data_end: Optional[datetime.datetime],
                    grace_s: float = 0.0) -> Optional[str]:
    """재연 구간이 유효(end>start)하고 데이터 범위 안인지 검사. 문제 없으면 None,
    아니면 영어 에러 문자열(러너 note·GUI status에 그대로 노출).

    grace_s: end가 데이터 끝을 이 초만큼 넘는 것은 허용. trace 마지막 샘플은
    duration - 1/fps에 찍히는데 API는 초 단위라 "정확히 duration 요청"이
    수십 ms 차이로 거부되는 것을 막는다(재생은 데이터 끝으로 클램프됨).
    """
    if end <= start:
        return f"replay window empty: end {end} <= start {start}"
    if data_start is not None and start < data_start:
        return f"replay start {start} before data start {data_start}"
    if data_end is not None and end > data_end + datetime.timedelta(seconds=grace_s):
        return f"replay end {end} after data end {data_end}"
    return None


def replay_output_name(start: datetime.datetime, end: datetime.datetime) -> str:
    """구간을 담은 결정적 파일명(재현·역추적용). 확장자 제외."""
    return f"replay_{start.strftime('%Y%m%dT%H%M%S')}_{end.strftime('%Y%m%dT%H%M%S')}"


# --------------------------------------------------------------------------- #
# Kit-driving parts (import omni lazily so the module loads without Kit)
# --------------------------------------------------------------------------- #
def _get_core():
    from gist.netai.time_travel_summarization.extension import get_active_core
    core = get_active_core()
    if core is None:
        raise RuntimeError("extension not started / no active core")
    return core


def _activate_data_source(core, data_path: Optional[str], lake_dataset: Optional[str]) -> bool:
    """재연 소스 활성화. DATA_PATH(단일 트레이스 URI) 우선, 없으면 LAKE_DATASET(레이크
    manifest), 둘 다 없으면 config 기본값. env는 config .env 규약대로 재확장한다."""
    import os
    if data_path:
        core._config.data_path = data_path   # 단일 파일 로컬 재연 소스
        return core.set_data_source("local")
    if lake_dataset:
        os.environ["LAKE_DATASET"] = lake_dataset
        # manifest_uri = s3://${MINIO_BUCKET}/trajectory/${LAKE_DATASET}/... → 재확장 필요
        core.load_config(str(core._config.config_path))
        core._config.lake["enabled"] = True
        return core.set_data_source("lake")
    return core.load_data()


def _upload_files(paths, upload_uri: str) -> None:
    """영상+사이드카를 {upload_uri}/{name}으로 업로드(단일 산출물 — 에피소드 폴더 아님)."""
    from gist.netai.time_travel_summarization.storage import from_uri

    base = upload_uri.rstrip("/")
    ctypes = {".mp4": "video/mp4", ".json": "application/json", ".csv": "text/csv"}
    for p in paths:
        p = Path(p)
        if not p.exists():
            continue
        dst = f"{base}/{p.name}"
        from_uri(dst).put_file(dst, p, content_type=ctypes.get(p.suffix, "application/octet-stream"))
        print(f"[replay] upload {p.name} -> {dst}")


def _load_scene_profile(name):
    """씬 프로파일 이름 -> dict (미지정/실패는 None). 절대 임포트 — kit이 이 파일을
    패키지 밖 스크립트로 실행하므로 상대 임포트는 즉사한다(generate_episodes와 동일)."""
    if not name:
        return None
    from gist.netai.time_travel_summarization.automation.scene_profiles import load_profile
    return load_profile(name)


def run(args, core=None) -> None:
    import omni.kit.app  # noqa: F401  (ensures Kit context)

    # generate_episodes의 검증된 부팅/카메라 해석을 재사용(중복 방지).
    # 절대 임포트 필수: kit --exec는 이 파일을 패키지 밖 일반 스크립트로 실행하므로
    # 상대 임포트(from .)는 "no known parent package"로 즉사한다(L40 실측).
    from gist.netai.time_travel_summarization.automation.generate_episodes import (
        _ensure_stage, _resolve_camera,
    )

    core = core or _get_core()
    # 씬 프로파일: 생성 잡과 같은 레지스트리에서 stage/camera를 받는다. regime3 생산은
    # stage를 명시하지 않고 프로파일 이름만 주므로(run_manifest.args.stage = None),
    # 이 경로가 없으면 재연이 빈 스테이지를 열어 아무것도 렌더되지 않는다.
    # 명시 --stage/--camera가 있으면 그쪽이 우선(생성 경로와 같은 우선순위 규약).
    prof = _load_scene_profile(getattr(args, "scene_profile", None))
    if prof:
        if not getattr(args, "stage", None):
            args.stage = prof.get("stage") or None
        if not getattr(args, "camera", None):
            args.camera = prof.get("camera") or None
        print(f"[replay] scene profile={args.scene_profile} stage={args.stage} "
              f"camera={args.camera}")
    if not getattr(args, "camera", None):
        args.camera = "Capture_camera"   # 프로파일·명시 지정이 없을 때의 종전 기본값
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    start = parse_dt(args.replay_start)
    end = parse_dt(args.replay_end)

    _ensure_stage(core, getattr(args, "stage", None))
    camera_path = _resolve_camera(getattr(args, "camera", None))

    ok = _activate_data_source(core, getattr(args, "data_path", None),
                               getattr(args, "lake_dataset", None))
    _repo = getattr(core, "_repository", None)
    print(f"[replay] data activate={ok} err={getattr(core, '_last_data_load_error', '')!r} "
          f"range={getattr(_repo, 'data_start_time', None)}..{getattr(_repo, 'data_end_time', None)}")
    if not ok:
        raise RuntimeError(f"data load failed: {getattr(core, '_last_data_load_error', '')}")

    # repo 보존 경로로 객체 재생성(로컬 모드는 activate가 안 부름 — lake는 이미 부름).
    if hasattr(core, "regenerate_astronauts_from_loaded_data"):
        core.regenerate_astronauts_from_loaded_data()
    core.set_playback_mode()

    data_end = core.get_data_end_time()
    err = validate_window(start, end, core.get_data_start_time(), data_end, grace_s=1.0)
    if err:
        raise RuntimeError(err)

    # 재생 범위=[start, min(end, 데이터 끝)]으로 설정(grace로 통과한 수십 ms 초과분
    # 클램프). 영상 duration은 요청값 유지 — physics 원본과 청크 수 정합.
    range_end = min(end, data_end) if data_end is not None else end
    if not core.load_time_range(start, range_end):
        raise RuntimeError(f"load_time_range failed for {start}..{range_end}")

    duration = (end - start).total_seconds()
    name = replay_output_name(start, end)
    video_path = str((out_root / f"{name}.mp4").resolve())
    render_fps = int(getattr(args, "render_fps", 30) or 30)
    print(f"[replay] window {start}..{end} dur={duration:g}s fps={render_fps} camera={camera_path} "
          f"-> {video_path}")

    produced = core.run_capture_headless(
        duration, video_path, camera_path=camera_path,
        capture_start_dt=start, render_fps=render_fps, replay_start_dt=start)
    if not produced:
        raise RuntimeError("headless replay capture failed (see log)")

    meta = Path(produced).with_suffix(".meta.json")
    print(f"[replay] produced {produced} meta={meta.exists()}")

    if getattr(args, "upload_uri", None):
        try:
            _upload_files([produced, meta], args.upload_uri)
        except Exception as e:
            print(f"[replay] upload FAILED: {e!r} (local files kept)")

    # 산출물 요약(역추적) — 사이드카가 앵커·재연 창을 이미 담고 있어 별도 manifest 불필요.
    if meta.exists():
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
            print(f"[replay] sidecar: capture_start={m.get('capture_start')} "
                  f"replay_start={m.get('replay_start')} replay_end={m.get('replay_end')} "
                  f"fps={m.get('fps')}")
        except Exception:
            pass

    print("[replay] done.")


def _self_test() -> None:
    """Kit 없이 순수 헬퍼 검증(이 세션 검증용)."""
    s = parse_dt("2026-07-18 15:35:10")
    e = parse_dt("2026-07-18 15:35:40")
    assert s.year == 2026 and s.hour == 15 and s.second == 10
    for bad in ("2026/07/18 15:35:10", "not-a-date", ""):
        try:
            parse_dt(bad)
            raise AssertionError(f"parse_dt({bad!r}) should fail")
        except ValueError:
            pass
    # 창 검증: 정상 / 역전 / 범위 밖(앞·뒤) / 데이터 범위 None(무검증)
    ds = parse_dt("2026-07-18 15:00:00")
    de = parse_dt("2026-07-18 16:00:00")
    assert validate_window(s, e, ds, de) is None
    assert "empty" in validate_window(e, s, ds, de)
    assert "before data start" in validate_window(parse_dt("2026-07-18 14:59:00"), e, ds, de)
    assert "after data end" in validate_window(s, parse_dt("2026-07-18 16:00:01"), ds, de)
    assert validate_window(s, e, None, None) is None
    # grace: 데이터 끝을 1초 이내로 넘는 end는 허용(trace 마지막 샘플 17ms 부족 실측)
    de_short = de - datetime.timedelta(milliseconds=17)
    assert "after data end" in validate_window(s, de, ds, de_short)
    assert validate_window(s, de, ds, de_short, grace_s=1.0) is None
    assert "after data end" in validate_window(
        s, de + datetime.timedelta(seconds=2), ds, de_short, grace_s=1.0)
    # 결정적 파일명
    assert replay_output_name(s, e) == "replay_20260718T153510_20260718T153540"
    print("replay_range self-test OK")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--replay-start", type=str, help="ISO 'YYYY-MM-DD HH:MM:SS' (필수)")
    ap.add_argument("--replay-end", type=str, help="ISO 'YYYY-MM-DD HH:MM:SS' (필수)")
    ap.add_argument("--data-path", type=str, default=None,
                    help="트레이스 CSV/parquet URI(단일 파일 로컬 재연 소스)")
    ap.add_argument("--lake-dataset", type=str, default=None,
                    help="레이크 데이터셋 이름(config manifest ${LAKE_DATASET} 확장). "
                         "--data-path와 둘 중 하나")
    ap.add_argument("--camera", type=str, default=None,
                    help="capture camera: prim path(/World/..) 또는 prim 이름; "
                         "default: /World/summarization_camera")
    ap.add_argument("--render-fps", type=int, default=30, help="video/render fps (재연은 데시메이션 없음)")
    ap.add_argument("--scene-profile", type=str, default=None,
                    help="scene_profiles.json 이름 — stage/camera를 프로파일에서 받는다 "
                         "(명시 --stage/--camera가 우선)")
    ap.add_argument("--stage", type=str, default=None,
                    help="USD to open (local path or omniverse:// URL); default: new empty stage")
    ap.add_argument("--out", type=str, default="artifacts/replays")
    ap.add_argument("--upload-uri", type=str, default=None,
                    help="영상+사이드카 업로드 대상 (예: s3://time-travel-summarization/replays/<job_id>)")
    ap.add_argument("--quit", action="store_true", help="quit Kit after finishing (batch/CI)")
    ap.add_argument("--self-test", action="store_true", help="run pure-helper tests without Kit")
    args = ap.parse_args()
    if args.self_test:
        _self_test()
        return
    if not args.replay_start or not args.replay_end:
        ap.error("--replay-start and --replay-end are required")
    try:
        run(args)
    finally:
        if args.quit:
            try:
                import omni.kit.app
                omni.kit.app.get_app().post_quit(0)
            except Exception:
                pass
            # generate_episodes와 동일한 force-exit fallback: post_quit 후 비데몬 스레드가
            # 프로세스를 잡아 잔류하는 고질 → 15초 내 미종료 시 강제 탈출(워치독 계약 보장).
            import os as _os
            import threading as _threading
            import time as _time

            def _force_exit():
                _time.sleep(15.0)
                print("[replay] force exit (quit did not complete in 15s)")
                _os._exit(0)

            _threading.Thread(target=_force_exit, daemon=True).start()


if __name__ == "__main__":
    main()
