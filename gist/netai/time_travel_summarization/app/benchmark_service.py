"""Lookup 벤치마크 — 상태는 core(repository), 동작은 여기."""
import carb


def start_lookup_benchmark(core, mode: str, pattern: str) -> bool:
    """Live lookup benchmark 시작.

    mode: 'linear' | 'bisect' | 'hybrid' | 'invalidate'
    pattern: 자유 라벨 (예: 'forward', 'backward', 'random_seek')
    """
    try:
        core._repository.set_lookup_mode(mode)
    except ValueError as e:
        carb.log_warn(f"[Benchmark] invalid mode: {e}")
        return False
    core._repository.start_benchmark(pattern)
    carb.log_warn(f"[Benchmark] started mode={mode} pattern={pattern}")
    return True


def stop_lookup_benchmark(core) -> dict:
    """Live lookup benchmark 종료. 결과 표 + CSV 저장."""
    result = core._repository.stop_benchmark()
    carb.log_warn(
        f"[Benchmark] mode={result['mode']:12s} pattern={result['pattern']:12s} "
        f"calls={result['call_count']:6d} total={result['total_seconds']*1000:.3f}ms "
        f"per_call={result['per_call_us']:.3f}us"
    )
    try:
        out_dir = core._paths.artifacts_dir / "benchmarks"
        out_dir.mkdir(parents=True, exist_ok=True)
        csv_path = out_dir / "lookup_runtime_benchmark.csv"
        is_new = not csv_path.exists()
        with open(csv_path, "a", encoding="utf-8", newline="") as f:
            import csv as _csv_local
            writer = _csv_local.writer(f)
            if is_new:
                writer.writerow(["timestamp", "mode", "pattern", "call_count",
                                 "total_seconds", "per_call_us"])
            from datetime import datetime as _dt_local
            writer.writerow([_dt_local.now().isoformat(), result["mode"],
                             result["pattern"], result["call_count"],
                             result["total_seconds"], result["per_call_us"]])
        carb.log_warn(f"[Benchmark] CSV appended: {csv_path}")
    except Exception as e:
        carb.log_warn(f"[Benchmark] CSV write failed: {e}")
    return result


def run_lookup_benchmark_suite(core, duration_s: float = 5.0, fps: int = 60) -> list:
    """3 lookup modes × forward/backward = 6 runs 자동 측정.

    각 run 전 timeline을 안전한 시작점으로 reset.
    """
    n_ticks = int(duration_s * fps)
    dt = 1.0 / fps
    runs = [
        ("bisect", "forward", 1.0),
        ("hybrid", "forward", 1.0),
        ("invalidate", "forward", 1.0),
        ("bisect", "backward", -1.0),
        ("hybrid", "backward", -1.0),
        ("invalidate", "backward", -1.0),
    ]
    results = []
    for mode, pattern, speed in runs:
        # 1) Reset timeline to a safe starting point
        if speed > 0:
            core.set_to_earliest_time()
        else:
            end = core._repository.data_end_time
            if end:
                core.set_current_time(end)
        # 2) Ensure not playing before start
        if core._playback.is_playing():
            core._playback.toggle_playback()
        # 3) Start benchmark + play
        start_lookup_benchmark(core, mode, pattern)
        core._playback.set_playback_speed(speed)
        core._playback.toggle_playback()
        # 4) Drive updates manually (Kit main thread blocked here)
        for _ in range(n_ticks):
            core.update(dt)
        # 5) Stop play + benchmark
        if core._playback.is_playing():
            core._playback.toggle_playback()
        results.append(stop_lookup_benchmark(core))
    return results
