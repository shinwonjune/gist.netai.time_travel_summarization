"""Floor lookup 알고리즘 4종 벤치마크.

목적: timestamp 기반 인덱싱 자료구조는 동일하게 두고, miss 시 가장 가까운
이전 timestamp를 찾는 **알고리즘**만 4가지로 바꿔 성능·정확성 비교.

알고리즘:
- linear: timestamps list를 처음부터 순차 비교. O(N). 정확.
- bisect: sorted timestamps + bisect_right. O(log N). 정확.
- hybrid: grid 일치 시 O(1) hash 반환, miss 시 bisect fallback. 정확.
- lkv_cache: grid 일치 시점에만 cache 갱신, miss 시 cache 그대로 반환.
  O(1) 보장, 정확성은 워크로드의 grid hit 빈도에 의존.
"""

from __future__ import annotations

import bisect
import csv
import datetime as _dt
import random
import time
from pathlib import Path
from typing import Dict, List, Optional


# ---------- pure functions ----------

def lkv_linear(timestamps: List[str], target: str) -> Optional[str]:
    """현재 _get_last_known_value 와 동일 동작. O(N)."""
    prev = None
    for ts in timestamps:
        if ts <= target:
            prev = ts
        else:
            break
    return prev


def lkv_bisect(sorted_timestamps: List[str], target: str) -> Optional[str]:
    """bisect_right - 1. O(log N)."""
    idx = bisect.bisect_right(sorted_timestamps, target) - 1
    return sorted_timestamps[idx] if idx >= 0 else None


# ---------- stateful caches ----------

class LkvForwardBisectHybrid:
    """forward 시 cache (O(1)), backward 감지 시 bisect fallback (O(log N))."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._last: Optional[str] = None
        self._ts_set: Optional[set] = None

    def query(self, timestamps: List[str], target: str) -> Optional[str]:
        if self._ts_set is None:
            self._ts_set = set(timestamps)
        # exact grid hit (forward O(1) fast path)
        if target in self._ts_set:
            self._last = target
            return self._last
        # miss (off-grid or backward) → bisect fallback (O(log N), always correct)
        idx = bisect.bisect_right(timestamps, target) - 1
        self._last = timestamps[idx] if idx >= 0 else None
        return self._last


class LkvCache:
    """grid hit 시 cache 갱신, miss 시 cache 그대로 반환.

    - search 없음. set membership check (O(1)) + 변수 반환.
    - 모든 query O(1) 보장.
    - 정확성:
      - target이 grid에 일치 → 정확
      - forward play: 직전 갱신 시점 ts = 직전 grid (정확한 floor)
      - backward play: 직전 갱신 시점 ts = 직후 grid (한 grid 밀림)
      - 빠른 slider drag (grid 건너뜀) → cache stale (의도된 trade-off)
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._last: Optional[str] = None
        self._ts_set: Optional[set] = None

    def query(self, timestamps: List[str], target: str) -> Optional[str]:
        if self._ts_set is None:
            self._ts_set = set(timestamps)
        if target in self._ts_set:
            self._last = target
        return self._last


# ---------- benchmark harness ----------

ALGORITHMS = {
    "linear": "function",
    "bisect": "function",
    "lkv_cache": LkvCache,
    "hybrid": LkvForwardBisectHybrid,
}


def run_benchmark(
    timestamps: List[str],
    queries: List[str],
    algorithm: str,
    oracle: Optional[List[Optional[str]]] = None,
) -> Dict[str, object]:
    """단일 알고리즘 측정. oracle 주어지면 결과 일치 비율을 correctness로 보고."""
    if algorithm not in ALGORITHMS:
        raise ValueError(f"unknown algorithm: {algorithm}")

    # dispatch
    if algorithm == "linear":
        fn = lambda t, q: lkv_linear(t, q)
    elif algorithm == "bisect":
        fn = lambda t, q: lkv_bisect(t, q)
    else:
        instance = ALGORITHMS[algorithm]()
        fn = lambda t, q, _i=instance: _i.query(t, q)

    # warm-up (1회)
    fn(timestamps, queries[0])

    results: List[Optional[str]] = []
    t0 = time.perf_counter()
    for q in queries:
        results.append(fn(timestamps, q))
    elapsed = time.perf_counter() - t0

    correctness = 1.0
    if oracle is not None:
        matches = sum(1 for r, o in zip(results, oracle) if r == o)
        correctness = matches / max(len(oracle), 1)

    return {
        "algorithm": algorithm,
        "n_timestamps": len(timestamps),
        "n_queries": len(queries),
        "total_seconds": elapsed,
        "per_query_us": elapsed / max(len(queries), 1) * 1e6,
        "correctness_rate": correctness,
    }


# ---------- synthesizers ----------

def synthesize_timestamps(
    n_unique: int,
    interval_s: float = 0.2,
    start: str = "2025-01-01 00:00:00.000",
) -> List[str]:
    """N개의 timestamp string을 interval_s 간격으로 생성."""
    fmt = "%Y-%m-%d %H:%M:%S.%f"
    base = _dt.datetime.strptime(start, fmt)
    step = _dt.timedelta(seconds=interval_s)
    return [(base + step * i).strftime(fmt)[:-3] for i in range(n_unique)]


def synthesize_random_queries(
    timestamps: List[str],
    n_queries: int,
    seed: int = 42,
    offset_max_s: float = 0.5,
) -> List[str]:
    """timestamp 범위 내 임의 시점 N_QUERIES개.

    각 query는 timestamps에서 임의로 하나 고른 후 [-offset_max_s, +offset_max_s) 사이
    랜덤 offset을 더해 grid에 없는 ts도 생성. forward/backward seek 혼합.
    """
    rng = random.Random(seed)
    fmt = "%Y-%m-%d %H:%M:%S.%f"
    parsed = [_dt.datetime.strptime(ts, fmt) for ts in timestamps]
    out: List[str] = []
    for _ in range(n_queries):
        base = rng.choice(parsed)
        offset_s = rng.uniform(-offset_max_s, offset_max_s)
        ts = base + _dt.timedelta(seconds=offset_s)
        out.append(ts.strftime(fmt)[:-3])
    return out


def synthesize_forward_queries(
    timestamps: List[str],
    n_queries: int,
    fps: int = 60,
) -> List[str]:
    """60fps wall-clock 단조증가 forward play."""
    fmt = "%Y-%m-%d %H:%M:%S.%f"
    start = _dt.datetime.strptime(timestamps[0], fmt)
    step = _dt.timedelta(seconds=1.0 / fps)
    return [(start + step * i).strftime(fmt)[:-3] for i in range(n_queries)]


def synthesize_backward_queries(
    timestamps: List[str],
    n_queries: int,
    fps: int = 60,
) -> List[str]:
    """60fps wall-clock 단조감소 backward play."""
    fmt = "%Y-%m-%d %H:%M:%S.%f"
    start = _dt.datetime.strptime(timestamps[-1], fmt)
    step = _dt.timedelta(seconds=-1.0 / fps)
    return [(start + step * i).strftime(fmt)[:-3] for i in range(n_queries)]


def synthesize_slider_drag_queries(
    timestamps: List[str],
    n_queries: int,
    step_s: float = 0.1,
    direction: str = "forward",
) -> List[str]:
    """slider drag 시나리오. 단조 진행, query 간 일정 step.

    direction="forward"면 timestamps[0]에서 시작 step_s씩 증가.
    direction="backward"면 timestamps[-1]에서 시작 step_s씩 감소.
    """
    if direction not in ("forward", "backward"):
        raise ValueError("direction must be forward|backward")
    fmt = "%Y-%m-%d %H:%M:%S.%f"
    if direction == "forward":
        base = _dt.datetime.strptime(timestamps[0], fmt)
        step = _dt.timedelta(seconds=step_s)
    else:
        base = _dt.datetime.strptime(timestamps[-1], fmt)
        step = _dt.timedelta(seconds=-step_s)
    return [(base + step * i).strftime(fmt)[:-3] for i in range(n_queries)]


# ---------- top-level driver ----------

def benchmark_all(
    n_timestamps_list: List[int] = (300, 3_000, 300_000),
    n_queries: int = 10_000,
    seed: int = 42,
    pattern: str = "play_forward",
    slider_step_s: float = 0.1,
) -> List[Dict[str, object]]:
    """N 스케일 × 5 알고리즘 측정. linear 결과를 oracle로 사용해 정확도 측정.

    pattern 옵션:
      - "play_forward":     60fps 단조증가
      - "play_backward":    60fps 단조감소
      - "slider_forward":   천천히 우측 drag (step_s=0.1초)
      - "slider_backward":  천천히 좌측 drag
      - "random_jump":      slider를 임의 위치로 던지는 경우 (기존 random)
    """
    all_results: List[Dict[str, object]] = []
    for n in n_timestamps_list:
        timestamps = synthesize_timestamps(n)
        if pattern == "random_jump":
            queries = synthesize_random_queries(timestamps, n_queries, seed=seed)
        elif pattern == "play_forward":
            queries = synthesize_forward_queries(timestamps, n_queries)
        elif pattern == "play_backward":
            queries = synthesize_backward_queries(timestamps, n_queries)
        elif pattern == "slider_forward":
            queries = synthesize_slider_drag_queries(timestamps, n_queries, slider_step_s, "forward")
        elif pattern == "slider_backward":
            queries = synthesize_slider_drag_queries(timestamps, n_queries, slider_step_s, "backward")
        else:
            raise ValueError(f"unknown pattern: {pattern}")
        oracle = [lkv_linear(timestamps, q) for q in queries]
        for algo in ALGORITHMS:
            res = run_benchmark(timestamps, queries, algo, oracle=oracle)
            res["pattern"] = pattern
            all_results.append(res)
    return all_results


def format_results_table(results: List[Dict[str, object]]) -> str:
    """Markdown table로 결과 포맷."""
    lines = [
        "| N_timestamps | N_queries | Algorithm           | Total (ms) | Per-query (μs) | Correctness |",
        "|--------------|-----------|---------------------|------------|----------------|-------------|",
    ]
    for r in results:
        lines.append(
            f"| {r['n_timestamps']:>12} | {r['n_queries']:>9} | {r['algorithm']:<19} | "
            f"{r['total_seconds']*1000:>10.3f} | {r['per_query_us']:>14.3f} | "
            f"{r['correctness_rate']*100:>10.1f}% |"
        )
    return "\n".join(lines)


def save_results_csv(results: List[Dict[str, object]], out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["n_timestamps", "n_queries", "algorithm",
                       "total_seconds", "per_query_us", "correctness_rate"],
        )
        writer.writeheader()
        for r in results:
            writer.writerow(r)
    return out_path
