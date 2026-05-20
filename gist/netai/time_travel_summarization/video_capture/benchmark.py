"""
A1 (Movie Capture) vs A2 (viewport texture grab + 비동기 인코더) 벤치마크.

Usage (Omniverse Kit Script Editor):
    from gist.netai.time_travel_summarization.video_capture.benchmark import run
    run(duration_s=60.0, repeat=3)

NOTE: exec(open(...).read())는 사용 금지 — 본 모듈의 relative import(`from .types ...`)가
      __package__ 없이 실행되면서 ImportError를 일으킵니다. 반드시 `import` 경로로 호출하세요.

만약 extension이 enable되지 않은 상태라면 sys.path 추가:
    import sys
    sys.path.insert(0, r"C:\\Users\\wonjune\\workspace\\kit-app-template\\source\\extensions\\gist.netai.time_travel_summarization")
    from gist.netai.time_travel_summarization.video_capture.benchmark import run
    run(duration_s=10.0, repeat=1)

수행 사항:
- A1, A2 runner 각각 repeat 회 실행
- 동일 CaptureRequest (해상도 532×280, fps 30, duration_s=60 기본)
- wall_clock_s / output_size_bytes / dropped_frames / sim_fps_avg / error 기록
- artifacts/benchmarks/capture_<ts>.json 저장
- docs/REALTIME_CAPTURE.md 결과 표 덮어쓰기 (자기소개서 인용용)
"""

import json
import statistics
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .types import CaptureRequest, CaptureResult
from .movie_capture import MovieCaptureRunner
from .realtime_capture import RealtimeCaptureRunner
from ..app.paths import ExtensionPaths


def _module_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _benchmarks_dir() -> Path:
    paths = ExtensionPaths(_module_dir())
    out = paths.artifacts_dir / "benchmarks"
    out.mkdir(exist_ok=True)
    return out


def _videos_dir() -> Path:
    return ExtensionPaths(_module_dir()).videos_dir


def _summarize(label: str, results: list) -> dict:
    successes = [r for r in results if r.success]
    walls = [r.wall_clock_s for r in successes]
    sizes = [r.output_size_bytes for r in successes]
    drops = [r.dropped_frames for r in successes]
    sim_fps = [r.sim_fps_avg for r in successes if r.sim_fps_avg is not None]
    n = len(successes)

    def mean_std(xs):
        if not xs:
            return None, None
        m = statistics.mean(xs)
        s = statistics.pstdev(xs) if len(xs) > 1 else 0.0
        return m, s

    wall_m, wall_s = mean_std(walls)
    size_m, _ = mean_std(sizes)
    drop_m, _ = mean_std(drops)
    fps_m, _ = mean_std(sim_fps)
    return {
        "label": label,
        "runs_total": len(results),
        "runs_success": n,
        "wall_clock_s_mean": wall_m,
        "wall_clock_s_std": wall_s,
        "output_size_bytes_mean": size_m,
        "dropped_frames_mean": drop_m,
        "sim_fps_avg_mean": fps_m,
        "errors": [r.error for r in results if not r.success],
    }


def _markdown_table(summary_a1: dict, summary_a2: dict, duration_s: float, repeat: int, width: int, height: int, fps: int) -> str:
    def fmt(x, suffix=""):
        if x is None:
            return "—"
        if isinstance(x, float):
            return f"{x:.2f}{suffix}"
        return f"{x}{suffix}"

    rows = [
        ("wall_clock_s (mean ± std)", f"{fmt(summary_a1['wall_clock_s_mean'])} ± {fmt(summary_a1['wall_clock_s_std'])}", f"{fmt(summary_a2['wall_clock_s_mean'])} ± {fmt(summary_a2['wall_clock_s_std'])}"),
        ("output_size_bytes (mean)", fmt(summary_a1["output_size_bytes_mean"]), fmt(summary_a2["output_size_bytes_mean"])),
        ("dropped_frames (mean)", fmt(summary_a1["dropped_frames_mean"]), fmt(summary_a2["dropped_frames_mean"])),
        ("sim_fps_avg (mean)", fmt(summary_a1["sim_fps_avg_mean"]), fmt(summary_a2["sim_fps_avg_mean"])),
        ("runs_success / total", f"{summary_a1['runs_success']} / {summary_a1['runs_total']}", f"{summary_a2['runs_success']} / {summary_a2['runs_total']}"),
    ]
    speedup_line = ""
    a1_m = summary_a1.get("wall_clock_s_mean")
    a2_m = summary_a2.get("wall_clock_s_mean")
    if a1_m and a2_m:
        ratio = a1_m / a2_m
        speedup_line = f"\n**Speedup (A1/A2 wall-clock 비)**: **{ratio:.2f}×**\n"

    body = [
        f"# Realtime Capture 벤치마크",
        f"",
        f"- 측정 시각: {datetime.now().isoformat(timespec='seconds')}",
        f"- 통제 변수: 해상도 {width}×{height}, fps {fps}, duration {duration_s}s, repeat {repeat}",
        f"- 환경: Omniverse Kit Python 3.12.x, 단일 viewport",
        f"",
        f"| 메트릭 | A1 (Movie Capture) | A2 (viewport+async) |",
        f"|---|---|---|",
    ]
    for name, a1v, a2v in rows:
        body.append(f"| {name} | {a1v} | {a2v} |")
    body.append("")
    if speedup_line:
        body.append(speedup_line)
    if summary_a1.get("errors"):
        body.append("\n## A1 errors\n")
        for e in summary_a1["errors"]:
            body.append(f"- `{e}`")
    if summary_a2.get("errors"):
        body.append("\n## A2 errors\n")
        for e in summary_a2["errors"]:
            body.append(f"- `{e}`")
    return "\n".join(body) + "\n"


def _run_one(runner, req: CaptureRequest, label: str) -> CaptureResult:
    print(f"  [{label}] start, output={req.output_uri}")
    t0 = time.perf_counter()
    res = runner.capture(req)
    dt = time.perf_counter() - t0
    status = "OK" if res.success else f"FAIL ({res.error})"
    print(f"  [{label}] {status} wall={res.wall_clock_s:.2f}s outer={dt:.2f}s drop={res.dropped_frames}")
    return res


def run(duration_s: float = 60.0,
        repeat: int = 3,
        width: int = 720,
        height: int = 480,
        fps: int = 30,
        write_markdown: bool = True,
        background: bool = True,
        run_a1: bool = True,
        run_a2: bool = True,
        with_overlay: bool = True):
    """Run A1/A2 each `repeat` times and write reports.

    background=True (default): work runs in a daemon thread (non-blocking).
    run_a1 / run_a2: 한쪽만 측정하고 싶으면 다른 쪽을 False로.
                     A1을 수동으로 (Omniverse Movie Capture UI로) 측정한다면 run_a1=False 권장.
    """
    if background:
        import threading
        thread = threading.Thread(
            target=lambda: _run_sync(
                duration_s,
                repeat,
                width,
                height,
                fps,
                write_markdown,
                run_a1,
                run_a2,
                with_overlay,
            ),
            daemon=True,
            name="ttsum_benchmark",
        )
        thread.start()
        print(f"[benchmark] started in background (thread={thread.name}); watch console for progress")
        return thread
    return _run_sync(duration_s, repeat, width, height, fps, write_markdown, run_a1, run_a2, with_overlay)


def _run_sync(duration_s: float, repeat: int, width: int, height: int, fps: int,
              write_markdown: bool, run_a1: bool = True, run_a2: bool = True,
              with_overlay: bool = True) -> dict:
    # Kit 내부에서 asyncio.get_event_loop()가 호출되는 경로가 있어
    # 워커 스레드에도 이벤트 루프가 필요. 없으면 새로 만들어 부착.
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    videos = _videos_dir()
    a1_results = []
    a2_results = []

    print(f"=== Benchmark start (duration={duration_s}s, repeat={repeat}, res={width}x{height}, fps={fps}, "
          f"run_a1={run_a1}, run_a2={run_a2}) ===")

    if run_a1:
        a1_runner = MovieCaptureRunner()
        for i in range(repeat):
            a1_uri = (videos / f"bench_a1_{ts}_{i+1}.mp4").resolve().as_uri()
            req = CaptureRequest(duration_s=duration_s, fps=fps, width=width, height=height,
                                 output_uri=a1_uri, label=f"bench_a1_{i+1}")
            a1_results.append(_run_one(a1_runner, req, f"A1 #{i+1}"))
    else:
        print("  [A1] skipped (run_a1=False) — measure manually via Omniverse Movie Capture UI")

    if run_a2:
        if with_overlay:
            from ..extension import get_active_core

            a2_runner = RealtimeCaptureRunner(core=get_active_core())
        else:
            a2_runner = RealtimeCaptureRunner()
        for i in range(repeat):
            a2_uri = (videos / f"bench_a2_{ts}_{i+1}.mp4").resolve().as_uri()
            req = CaptureRequest(duration_s=duration_s, fps=fps, width=width, height=height,
                                 output_uri=a2_uri, label=f"bench_a2_{i+1}")
            a2_results.append(_run_one(a2_runner, req, f"A2 #{i+1}"))
    else:
        print("  [A2] skipped (run_a2=False)")

    summary_a1 = _summarize("A1_movie_capture", a1_results)
    summary_a2 = _summarize("A2_realtime_capture", a2_results)

    payload = {
        "timestamp": ts,
        "params": {"duration_s": duration_s, "repeat": repeat,
                   "width": width, "height": height, "fps": fps},
        "a1_raw": [asdict(r) for r in a1_results],
        "a2_raw": [asdict(r) for r in a2_results],
        "a1_summary": summary_a1,
        "a2_summary": summary_a2,
    }
    out_json = _benchmarks_dir() / f"capture_{ts}.json"
    out_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[saved] {out_json}")

    if write_markdown:
        md_path = _module_dir().parent.parent / "docs" / "REALTIME_CAPTURE.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(_markdown_table(summary_a1, summary_a2, duration_s, repeat, width, height, fps), encoding="utf-8")
        print(f"[saved] {md_path}")

    print("\n=== Summary ===")
    print(f"A1 wall_clock_s mean: {summary_a1['wall_clock_s_mean']}")
    print(f"A2 wall_clock_s mean: {summary_a2['wall_clock_s_mean']}")
    if summary_a1["wall_clock_s_mean"] and summary_a2["wall_clock_s_mean"]:
        print(f"Speedup (A1/A2): {summary_a1['wall_clock_s_mean'] / summary_a2['wall_clock_s_mean']:.2f}×")

    return payload


if __name__ == "__main__":
    run()
