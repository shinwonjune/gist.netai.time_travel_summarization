"""GUI probe 덤프(JSON) → 지표 표 후처리 (레이크성능_실험설계.md §2-C).

지표:
  - hitch rate: frame_interval > 2×중앙값인 프레임 비율(재생 중 프레임만) —
    GUI는 twin 시계가 실시간에 묶여 있어 hitch가 곧 사용자 체감 끊김이다.
  - stall frame rate: d_sync>0 프레임 비율(재생 중). 웜업(첫 stall 프레임)은
    콜드스타트로 별도 집계·제외한다(판정 규약 §4).
  - tick_ms p50/p99: controller.update 소요(게이트 스킵 프레임 ~0 포함).
  - frame_interval p50/p99: 렌더 루프 자체의 주기.

사용:
  python -m gist.netai.time_travel_summarization.utils.gui_probe_report \
      artifacts/benchmarks/gui_probe_20260804-*.json [--all-frames]

여러 파일을 주면 파일별 행으로 표를 만든다. 기본은 재생 중(playing=True)
프레임만 판정하고, --all-frames로 전체(스크럽 포함)를 볼 수 있다.
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics
from pathlib import Path
from typing import List


def _percentile(xs: List[float], q: float) -> float:
    xs = sorted(xs)
    if not xs:
        return 0.0
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def analyze(path: Path, playing_only: bool = True) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    f = d["frames"]
    idx = range(len(f["wall_ts"]))
    if playing_only:
        idx = [i for i in idx if f["playing"][i]]
    else:
        idx = list(idx)

    intervals = [f["frame_interval_ms"][i] for i in idx if f["frame_interval_ms"][i] > 0]
    ticks = [f["tick_ms"][i] for i in idx]
    syncs = [f["d_sync"][i] for i in idx]

    n = len(idx)
    med = statistics.median(intervals) if intervals else 0.0
    hitches = sum(1 for v in intervals if med > 0 and v > 2 * med)

    # 웜업 분리: 재생 구간 첫 stall 프레임은 콜드스타트(불가피)로 제외
    stall_frames = [i for i, s in enumerate(syncs) if s > 0]
    warmup = 1 if stall_frames else 0
    stalls_post = max(0, len(stall_frames) - warmup)

    return {
        "file": path.name,
        "reason": d.get("reason"),
        "frames": n,
        "span_s": round(f["wall_ts"][idx[-1]] - f["wall_ts"][idx[0]], 1) if n > 1 else 0.0,
        "interval_p50_ms": round(_percentile(intervals, 0.50), 2),
        "interval_p99_ms": round(_percentile(intervals, 0.99), 2),
        "hitch_rate_pct": round(100.0 * hitches / max(1, len(intervals)), 2),
        "stall_frames_post_warmup": stalls_post,
        "warmup_stall_frames": warmup,
        "stall_frame_rate_pct": round(100.0 * stalls_post / max(1, n), 3),
        "tick_p50_ms": round(_percentile(ticks, 0.50), 3),
        "tick_p99_ms": round(_percentile(ticks, 0.99), 3),
    }


def format_table(rows: List[dict]) -> str:
    head = ["file", "frames", "span_s", "interval p50/p99 (ms)", "hitch %",
            "stalls (post-warmup)", "stall %", "tick p50/p99 (ms)"]
    lines = ["| " + " | ".join(head) + " |",
             "|" + "|".join("---" for _ in head) + "|"]
    for r in rows:
        lines.append(
            f"| {r['file']} | {r['frames']} | {r['span_s']} | "
            f"{r['interval_p50_ms']} / {r['interval_p99_ms']} | {r['hitch_rate_pct']} | "
            f"{r['stall_frames_post_warmup']} (+{r['warmup_stall_frames']} warmup) | "
            f"{r['stall_frame_rate_pct']} | {r['tick_p50_ms']} / {r['tick_p99_ms']} |"
        )
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+", help="gui_probe_*.json (glob 허용)")
    ap.add_argument("--all-frames", action="store_true",
                    help="재생 중 프레임만이 아니라 전체 프레임(스크럽 포함) 판정")
    args = ap.parse_args(argv)

    files: List[Path] = []
    for p in args.paths:
        hits = sorted(glob.glob(p))
        files += [Path(h) for h in hits] if hits else [Path(p)]

    rows = []
    for fp in files:
        if not fp.exists():
            print(f"[gui_probe_report] missing: {fp}")
            continue
        rows.append(analyze(fp, playing_only=not args.all_frames))

    if not rows:
        raise SystemExit("no probe files")
    print(format_table(rows))
    print("\n판정(설계 §4): 연속 재생에서 stall frame rate=0(웜업 제외) + hitch rate < 1% = 끊김없음 합격.")


if __name__ == "__main__":
    main()
