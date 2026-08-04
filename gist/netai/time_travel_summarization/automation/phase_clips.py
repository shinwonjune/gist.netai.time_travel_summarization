"""위상 분해 조건 세트 추출기 — 이벤트 특정 + 6조건 클립 창 계획 + ffmpeg 절단.

설계: docs/위상분해_실험설계.md §5. 단일 도구가 조건 전부를 한 번에 뽑는다:
  충돌 에피소드  -> full / no-approach / no-contact(스플라이스) / no-aftermath /
                    approach-only  (기준점 = collisions CSV의 접촉 클러스터 t_c)
  near-miss 에피소드 -> near-miss    (기준점 = trace 쌍별 거리 극소점 t*)
  양쪽             -> control(무관 구간 — 모든 이벤트에서 ±buffer 이상 떨어진 창)

오염 방어(같은 문서 §4-1): 모든 창은 collisions CSV와 교차 검사 — 창 안에 기준
이벤트가 아닌 접촉이 끼면 폐기. near-miss 에피소드의 실충돌(v3의 3+객체 한계)도
같은 규칙으로 걸러진다.

시계: trace·collisions·영상이 sim-클럭 단일 시계(capture_start + 오프셋)로 정합
이므로, 절대 시각 창 -> 영상 오프셋 환산은 뺄셈 하나다. wall-clock 시절 에피소드는
이 전제가 깨져 입력으로 쓰면 안 된다(스팬 검사로 방어).

사용 (EXT_ROOT에서, ffmpeg 필요 — 절단 단계만):
  python -m gist.netai.time_travel_summarization.automation.phase_clips \
    --collision-run artifacts/episodes/gen-XXXX \
    --nearmiss-run artifacts/episodes/nm-XXXX \
    --out artifacts/phase_ablation_v1 [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import random
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..playback.trajectory_repository import TrajectoryRepository

_parse = TrajectoryRepository.parse_timestamp

# 창 규격(초) — 설계 §5-3. 전부 2.0s 총장(2초/20프레임 학습 계약 정합).
CLIP_S = 2.0
WINDOW_SPECS: Dict[str, List[Tuple[float, float]]] = {
    # 조건: 기준점 대비 (시작, 끝) 오프셋 목록. 2구간이면 스플라이스.
    "full": [(-1.0, +1.0)],
    "no_approach": [(-0.1, +1.9)],
    "no_contact": [(-1.5, -0.5), (+0.5, +1.5)],
    "no_aftermath": [(-1.9, +0.1)],
    "approach_only": [(-2.2, -0.2)],
}
NEARMISS_SPEC = [(-1.0, +1.0)]
CONTROL_BUFFER_S = 3.0     # 무관 창이 이벤트와 유지해야 하는 최소 간격
CONTACT_CLUSTER_S = 0.5    # 접촉 rows를 사건으로 묶는 시간 반경
PURITY_MARGIN_S = 0.25     # 창 순도 검사 시 창 가장자리 여유


# ---------------------------------------------------------------- 이벤트 특정

def contact_clusters(rows: List[dict]) -> List[datetime.datetime]:
    """collisions CSV rows -> 접촉 사건 대표 시각 목록(클러스터 첫 시각).

    라벨이 객체 단위(쌍 미기록)라 같은 사건이 여러 행으로 남는다 —
    CONTACT_CLUSTER_S 내 연속 행을 한 사건으로 묶는다.
    """
    times = sorted(_parse(r["timestamp"]) for r in rows)
    clusters: List[datetime.datetime] = []
    for t in times:
        if not clusters or (t - clusters[-1]).total_seconds() > CONTACT_CLUSTER_S:
            clusters.append(t)
    return clusters


def near_miss_events(
    trace_rows: List[dict], gap: float, enter_frac: float = 2.0,
) -> List[dict]:
    """trace -> 쌍별 지면(x,z) 거리 극소점 목록.

    거리 시계열이 enter_thr(=gap×enter_frac) 아래로 들어온 구간마다 최소 거리
    도달 시각 t*를 1개 뽑는다. d_min < gap은 접촉 의심(near-miss 실패)으로
    표기만 하고 포함한다 — 폐기 여부는 collisions 교차 검사가 결정.
    반환: [{"t": datetime, "d_min": float, "pair": (a, b)}] (t 오름차순).
    """
    enter_thr = gap * enter_frac
    by_time: Dict[str, Dict[str, Tuple[float, float]]] = defaultdict(dict)
    for r in trace_rows:
        by_time[r["timestamp"]][str(r["objid"])] = (float(r["x"]), float(r["z"]))
    stamps = sorted(by_time, key=_parse)

    open_ranges: Dict[Tuple[str, str], dict] = {}
    events: List[dict] = []
    for ts in stamps:
        objs = by_time[ts]
        ids = sorted(objs)
        t = _parse(ts)
        seen_pairs = set()
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                d = math.hypot(objs[a][0] - objs[b][0], objs[a][1] - objs[b][1])
                pair = (a, b)
                seen_pairs.add(pair)
                if d < enter_thr:
                    cur = open_ranges.get(pair)
                    if cur is None or d < cur["d_min"]:
                        open_ranges[pair] = {"t": t, "d_min": d, "pair": pair}
                elif pair in open_ranges:
                    events.append(open_ranges.pop(pair))
        # 트랙이 끊긴 쌍의 열린 구간도 닫는다
        for pair in [p for p in open_ranges if p not in seen_pairs]:
            events.append(open_ranges.pop(pair))
    events += open_ranges.values()
    return sorted(events, key=lambda e: e["t"])


# ---------------------------------------------------------------- 창 계획

def _window_abs(t_ref: datetime.datetime, spec: List[Tuple[float, float]]):
    return [(t_ref + datetime.timedelta(seconds=s), t_ref + datetime.timedelta(seconds=e))
            for s, e in spec]


def _in_bounds(segs, start: datetime.datetime, end: datetime.datetime) -> bool:
    return all(start <= s and e <= end for s, e in segs)


def _contaminated(segs, contacts: List[datetime.datetime],
                  allow: Optional[datetime.datetime] = None) -> bool:
    """창 안(±PURITY_MARGIN_S)에 기준 사건(allow) 외의 접촉이 있으면 True."""
    m = datetime.timedelta(seconds=PURITY_MARGIN_S)
    for c in contacts:
        if allow is not None and abs((c - allow).total_seconds()) <= CONTACT_CLUSTER_S:
            continue
        if any(s - m <= c <= e + m for s, e in segs):
            return True
    return False


def plan_episode(
    kind: str,
    trace_rows: List[dict],
    collision_rows: List[dict],
    capture_start: datetime.datetime,
    duration_s: float,
    gap: float = 95.0,
    n_control: int = 1,
    seed: int = 42,
) -> List[dict]:
    """에피소드 1개 -> 클립 계획 목록.

    kind="collision": 접촉 클러스터마다 5조건(WINDOW_SPECS) + control.
    kind="nearmiss": 거리 극소점마다 near_miss 1조건 + control.
    반환 항목: {"condition", "t_ref", "segments": [(s,e)...], "d_min"?}
    """
    end = capture_start + datetime.timedelta(seconds=duration_s)
    contacts = contact_clusters(collision_rows)
    plans: List[dict] = []

    if kind == "collision":
        for t_c in contacts:
            for cond, spec in WINDOW_SPECS.items():
                segs = _window_abs(t_c, spec)
                if not _in_bounds(segs, capture_start, end):
                    continue
                if _contaminated(segs, contacts, allow=t_c):
                    continue
                plans.append({"condition": cond, "t_ref": t_c, "segments": segs})
    elif kind == "nearmiss":
        for ev in near_miss_events(trace_rows, gap):
            segs = _window_abs(ev["t"], NEARMISS_SPEC)
            if not _in_bounds(segs, capture_start, end):
                continue
            # near-miss 창에는 접촉이 하나라도 있으면 폐기(3+객체 한계 방어)
            if _contaminated(segs, contacts, allow=None):
                continue
            plans.append({"condition": "near_miss", "t_ref": ev["t"],
                          "segments": segs, "d_min": round(ev["d_min"], 1)})
    else:
        raise ValueError(f"unknown kind: {kind!r}")

    # control: 모든 이벤트(접촉 + near-miss 극소점)에서 buffer 이상 떨어진 2초 창
    event_times = list(contacts) + [e["t"] for e in near_miss_events(trace_rows, gap)]
    rng = random.Random(seed)
    tries, made = 0, 0
    while made < n_control and tries < 200:
        tries += 1
        off = rng.uniform(0.0, max(0.0, duration_s - CLIP_S))
        s = capture_start + datetime.timedelta(seconds=off)
        e = s + datetime.timedelta(seconds=CLIP_S)
        if all(abs((t - s).total_seconds()) > CONTROL_BUFFER_S and
               abs((t - e).total_seconds()) > CONTROL_BUFFER_S for t in event_times):
            plans.append({"condition": "control", "t_ref": s, "segments": [(s, e)]})
            made += 1
    return plans


# ---------------------------------------------------------------- 에피소드 IO

def _episode_files(run_dir: Path) -> List[dict]:
    """run 디렉터리(중첩 포함)에서 (trace, video, meta, collisions) 묶음 수집."""
    out = []
    for meta_path in sorted(run_dir.rglob("_video_*.meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        video = meta_path.with_suffix("").with_suffix(".mp4")  # _video_NNNN.meta.json -> .mp4
        if not video.exists():
            video = meta_path.parent / (meta_path.name.replace(".meta.json", ".mp4"))
        idx = meta_path.stem.replace("_video_", "").replace(".meta", "")
        trace = meta_path.parent / f"_trace_{idx}.csv"
        col = meta.get("collisions_csv")
        col_path = (meta_path.parent / col) if col and not Path(col).is_absolute() else \
            (Path(col) if col else None)
        if not (video.exists() and trace.exists()):
            continue
        out.append({"video": video, "trace": trace, "meta": meta,
                    "collisions": col_path if (col_path and col_path.exists()) else None})
    return out


def _read_csv_rows(path: Path) -> List[dict]:
    import csv
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------- ffmpeg 절단

def ffmpeg_cmd(video: Path, segments, capture_start: datetime.datetime,
               out_path: Path) -> List[str]:
    """창(절대 시각) -> ffmpeg 명령. 단일 구간도 스플라이스와 동일하게 필터
    그래프로 재인코딩한다 — 조건 간 인코딩 차이를 없애기 위해(설계 §5-4)."""
    parts, inputs = [], []
    for i, (s, e) in enumerate(segments):
        off = (s - capture_start).total_seconds()
        dur = (e - s).total_seconds()
        parts.append(f"[0:v]trim=start={off:.3f}:duration={dur:.3f},setpts=PTS-STARTPTS[v{i}]")
        inputs.append(f"[v{i}]")
    graph = ";".join(parts) + f";{''.join(inputs)}concat=n={len(segments)}:v=1:a=0[out]"
    return ["ffmpeg", "-y", "-i", str(video), "-filter_complex", graph,
            "-map", "[out]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-an", str(out_path)]


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--collision-run", action="append", default=[],
                    help="충돌 에피소드 run 디렉터리(반복 가능)")
    ap.add_argument("--nearmiss-run", action="append", default=[],
                    help="near-miss 에피소드 run 디렉터리(반복 가능)")
    ap.add_argument("--out", required=True, help="클립 세트 출력 디렉터리")
    ap.add_argument("--gap", type=float, default=95.0, help="near-miss gap(cm)")
    ap.add_argument("--controls-per-episode", type=int, default=1)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dry-run", action="store_true", help="ffmpeg 없이 계획만 출력")
    args = ap.parse_args(argv)

    out_root = Path(args.out)
    manifest: List[dict] = []
    counts: Dict[str, int] = defaultdict(int)

    jobs = [("collision", Path(r)) for r in args.collision_run] + \
           [("nearmiss", Path(r)) for r in args.nearmiss_run]
    if not jobs:
        raise SystemExit("입력 없음 — --collision-run / --nearmiss-run 지정")

    for kind, run_dir in jobs:
        eps = _episode_files(run_dir)
        print(f"[phase_clips] {kind} run {run_dir}: {len(eps)} episodes")
        for ep in eps:
            meta = ep["meta"]
            cap0 = _parse(str(meta.get("capture_start")))
            dur = float(meta.get("duration_s") or 0.0)
            trace_rows = _read_csv_rows(ep["trace"])
            col_rows = _read_csv_rows(ep["collisions"]) if ep["collisions"] else []
            plans = plan_episode(kind, trace_rows, col_rows, cap0, dur,
                                 gap=args.gap, n_control=args.controls_per_episode,
                                 seed=args.seed)
            for p in plans:
                cond = p["condition"]
                counts[cond] += 1
                name = f"{cond}_{ep['video'].parent.name}_{ep['video'].stem}_{counts[cond]:04d}.mp4"
                out_path = out_root / cond / name
                entry = {
                    "condition": cond,
                    "clip": str(out_path.relative_to(out_root)) if not args.dry_run else name,
                    "source_video": str(ep["video"]),
                    "t_ref": p["t_ref"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                    "segments": [[(s - cap0).total_seconds(), (e - cap0).total_seconds()]
                                 for s, e in p["segments"]],
                }
                if "d_min" in p:
                    entry["d_min"] = p["d_min"]
                manifest.append(entry)
                if not args.dry_run:
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    cmd = ffmpeg_cmd(ep["video"], p["segments"], cap0, out_path)
                    res = subprocess.run(cmd, capture_output=True, text=True)
                    if res.returncode != 0:
                        print(f"[phase_clips] ffmpeg FAILED {name}: {res.stderr[-300:]}")
                        manifest[-1]["error"] = "ffmpeg failed"

    print("[phase_clips] counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if not args.dry_run:
        out_root.mkdir(parents=True, exist_ok=True)
        (out_root / "clips_manifest.json").write_text(
            json.dumps({"gap": args.gap, "seed": args.seed, "clips": manifest},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[phase_clips] manifest -> {out_root / 'clips_manifest.json'}")
    else:
        for e in manifest[:10]:
            print(f"  {e['condition']}: t_ref={e['t_ref']} segs={e['segments']}")
        if len(manifest) > 10:
            print(f"  ... ({len(manifest)} total)")


if __name__ == "__main__":
    main()
