"""Build a Qwen3-VL LoRA training dataset from captured BEV episodes.

An *episode* is one captured wander video plus its sidecar and collision labels:

    <name>.mp4          # BEV video with a burned-in HH:MM:SS overlay
    <name>.meta.json    # capture_start (t0), fps, collisions_csv, objid_to_label
    collisions_*.csv    # [timestamp, objid, x, y, z, kind] (path taken from meta)

Why 2-second clips: at inference the VSS server splits the video into 2s chunks
(``vlm_client/core.py`` default_chunk_duration=2) and the VLM only ever sees ONE
2s chunk per call, reading the burned-in HH:MM:SS overlay to report collisions.
So each training sample = one 2s clip = exactly what the model sees at inference.

Labels: collisions are stamped with ``datetime.now()`` — the SAME wall-clock the
overlay shows — so a collision at wall-clock ``t`` lands in the clip whose window
is ``[t0 + 2i, t0 + 2i + 2)`` and whose overlay displays ``t``. Object-object
collisions emit two rows (one per object) at the same second; grouping object-kind
rows by ``HH:MM:SS`` reproduces the ``{HH:MM:SS: [id, id]}`` sets the evaluator
expects.

Output: ShareGPT-format JSONL (train/val/test) consumable by ms-swift, plus
``test_gt.json`` (clip -> ground-truth target) for evaluation. Splitting is by
EPISODE so clips from one episode never leak across splits.

Usage:
    python -m utils.build_dataset \
        --episodes-dir artifacts/episodes \
        --out-dir artifacts/dataset \
        --clip-sec 2 --neg-ratio 1.0 --preset twin_view --nframes 20
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Reuse the EXACT inference prompts so training input == inference input, and the
# SHARED event-time format so CSV labels match the overlay the VLM reads.
try:
    from vlm_client.prompts import PROMPTS
    from timefmt import format_event_time, parse_event_time
except ImportError:  # running as a loose script: add the extension root to path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from vlm_client.prompts import PROMPTS
    from timefmt import format_event_time, parse_event_time


# --------------------------------------------------------------------------- #
# ffmpeg helpers
# --------------------------------------------------------------------------- #
def ffmpeg_exe() -> str:
    """Path to an ffmpeg binary, preferring imageio-ffmpeg's bundled one."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")


def probe_duration(video: Path, ffmpeg: str) -> Optional[float]:
    """Return video duration in seconds by parsing ffmpeg stderr, or None."""
    try:
        proc = subprocess.run(
            [ffmpeg, "-i", str(video)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        m = _DURATION_RE.search(proc.stderr)
        if not m:
            return None
        h, mnt, sec = m.groups()
        return int(h) * 3600 + int(mnt) * 60 + float(sec)
    except Exception:
        return None


def slice_clip(ffmpeg: str, src: Path, start: float, dur: float, out: Path,
               content_hz: Optional[float] = None) -> bool:
    """Cut [start, start+dur) from src into out, re-encoded for frame accuracy.

    If ``content_hz`` is set, the clip's distinct content is decimated to that rate
    (ffmpeg ``fps`` filter). This lets one high-rate (e.g. 10Hz) capture produce a
    lower-rate (e.g. 5Hz) variant for a controlled sampling-rate A/B, since the VLM
    still samples a fixed 20 frames per 2s regardless of container fps.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-y",
        "-i", str(src),
        "-ss", f"{start:.3f}",
        "-t", f"{dur:.3f}",
        "-an",
    ]
    if content_hz and content_hz > 0:
        cmd += ["-vf", f"fps={content_hz:g}"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", str(out)]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0 or not out.exists():
        print(f"  ! ffmpeg failed for {out.name}: {proc.stderr.strip().splitlines()[-1:]}")
        return False
    return True


# --------------------------------------------------------------------------- #
# label extraction
# --------------------------------------------------------------------------- #
def _label_for_objid(objid: str, objid_to_label: Dict[str, str]) -> str:
    """objid -> numeric label, matching the overlay's drawn label."""
    if objid in objid_to_label:
        return objid_to_label[objid]
    m = re.search(r"(\d+)$", objid)
    return str(int(m.group(1))) if m else objid


def load_collisions(
    csv_path: Path, kinds: set, objid_to_label: Dict[str, str], t0: datetime
) -> List[Tuple[datetime, str]]:
    """Return [(wall_clock_dt, numeric_label)] for rows whose kind is in ``kinds``.

    Timestamps may be date-less (HH:MM:SS[.mmm]); ``t0`` (capture_start) anchors the
    date so the offset math against the video timeline is correct.
    """
    out: List[Tuple[datetime, str]] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if kinds and row.get("kind") not in kinds:
                continue
            try:
                dt = parse_event_time(row["timestamp"], t0)
            except (ValueError, KeyError):
                continue
            out.append((dt, _label_for_objid(row["objid"], objid_to_label)))
    return out


def clip_targets(
    collisions: List[Tuple[datetime, str]],
    t0: datetime,
    clip_sec: float,
    n_clips: int,
    min_objects: int,
) -> Dict[int, list]:
    """Map clip index -> list of {"HH:MM:SS": [ids]} for collisions in that clip.

    Collisions are grouped by clip, then by HH:MM:SS second (what the overlay
    shows). A second is kept only if >= ``min_objects`` distinct objects collide
    there (object-object overlap), matching the evaluation definition.
    """
    # clip_idx -> second_str -> set(labels)
    grouped: Dict[int, Dict[str, set]] = {}
    for dt, label in collisions:
        offset = (dt - t0).total_seconds()
        if offset < 0:
            continue
        idx = int(offset // clip_sec)
        if idx >= n_clips:
            continue
        sec = format_event_time(dt)  # 라벨 키 = 오버레이 형식(추론 출력과 동일)
        grouped.setdefault(idx, {}).setdefault(sec, set()).add(label)

    targets: Dict[int, list] = {}
    for idx, by_sec in grouped.items():
        entries = []
        for sec in sorted(by_sec):
            ids = sorted(int(x) if x.isdigit() else x for x in by_sec[sec])
            if len(ids) >= min_objects:
                entries.append({sec: ids})
        if entries:
            targets[idx] = entries
    return targets


# --------------------------------------------------------------------------- #
# episode discovery + processing
# --------------------------------------------------------------------------- #
def discover_episodes(episodes_dir: Path) -> List[Path]:
    """Return sidecar meta.json paths that have a sibling video."""
    metas = []
    for meta in sorted(episodes_dir.rglob("*.meta.json")):
        video = meta.with_suffix("").with_suffix(".mp4")
        if not video.exists():
            video = meta.parent / (meta.name[: -len(".meta.json")] + ".mp4")
        if video.exists():
            metas.append(meta)
        else:
            print(f"  ! no video next to {meta.name}; skipping")
    return metas


def process_episode(
    meta_path: Path, clips_dir: Path, ffmpeg: str, args
) -> Tuple[str, List[dict]]:
    """Slice an episode into labeled clip records. Returns (episode_id, records)."""
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    episode_id = meta_path.name[: -len(".meta.json")]
    video = meta_path.parent / meta.get("video", episode_id + ".mp4")
    if not video.exists():
        video = meta_path.with_suffix("").with_suffix(".mp4")

    t0 = datetime.fromisoformat(meta["capture_start"])
    objid_to_label = {str(k): str(v) for k, v in (meta.get("objid_to_label") or {}).items()}

    # Resolve collisions CSV (absolute, or relative to the episode dir).
    csv_rel = meta.get("collisions_csv")
    collisions: List[Tuple[datetime, str]] = []
    if csv_rel:
        csv_path = Path(csv_rel)
        if not csv_path.exists():
            # 폴백: meta 옆의 같은 파일명. 주의 — Windows에서 기록된 절대경로를
            # 리눅스에서 처리하면 백슬래시가 구분자로 안 잘려 Path().name이 통째로
            # 나온다("생성=Windows, 빌드=L40" 조합에서 전 에피소드 negative화 실측).
            # 구분자를 정규화해 파일명만 취한다.
            fname = csv_rel.replace("\\", "/").rsplit("/", 1)[-1]
            csv_path = meta_path.parent / fname
        if csv_path.exists():
            collisions = load_collisions(csv_path, set(args.kinds), objid_to_label, t0)
        else:
            print(f"  ! collisions csv not found for {episode_id}: {csv_rel}")

    duration = probe_duration(video, ffmpeg) or float(meta.get("duration_s", 0))
    n_clips = int(duration // args.clip_sec)
    if n_clips == 0:
        print(f"  ! {episode_id}: duration {duration:.1f}s < clip {args.clip_sec}s; skipping")
        return episode_id, []

    targets = clip_targets(collisions, t0, args.clip_sec, n_clips, args.min_objects)

    prompt = PROMPTS[args.preset]["prompt"]
    system = PROMPTS[args.preset]["system_prompt"]
    records: List[dict] = []
    for i in range(n_clips):
        clip_name = f"{episode_id}_clip{i:04d}.mp4"
        clip_path = clips_dir / clip_name
        target = targets.get(i, [])
        if not slice_clip(ffmpeg, video, i * args.clip_sec, args.clip_sec, clip_path,
                          content_hz=args.content_hz):
            continue
        records.append(
            {
                "system": system,
                "videos": [str(clip_path.resolve())],
                "conversations": [
                    {"from": "human", "value": "<video>\n" + prompt},
                    {"from": "gpt", "value": json.dumps(target, ensure_ascii=False)},
                ],
                # bookkeeping (stripped before writing the swift jsonl)
                "_meta": {"clip": clip_name, "positive": bool(target), "target": target},
            }
        )
    n_pos = sum(1 for r in records if r["_meta"]["positive"])
    print(f"  {episode_id}: {len(records)} clips ({n_pos} positive)")
    return episode_id, records


# --------------------------------------------------------------------------- #
# balancing, splitting, writing
# --------------------------------------------------------------------------- #
def balance(records: List[dict], neg_ratio: float, rng: random.Random) -> List[dict]:
    """Keep all positives; subsample negatives to neg_ratio * n_positives."""
    pos = [r for r in records if r["_meta"]["positive"]]
    neg = [r for r in records if not r["_meta"]["positive"]]
    keep_neg = min(len(neg), int(round(len(pos) * neg_ratio))) if pos else len(neg)
    rng.shuffle(neg)
    out = pos + neg[:keep_neg]
    rng.shuffle(out)
    return out


def write_jsonl(records: List[dict], path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            row = {k: v for k, v in r.items() if not k.startswith("_")}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episodes-dir", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--clip-sec", type=float, default=2.0)
    ap.add_argument("--neg-ratio", type=float, default=1.0, help="negatives per positive (~50:50 at 1.0)")
    ap.add_argument("--preset", default="twin_view", choices=sorted(PROMPTS.keys()))
    ap.add_argument("--nframes", type=int, default=20, help="frames/clip the VLM sees at inference (VSS); informational")
    ap.add_argument("--content-hz", type=float, default=None,
                    help="decimate distinct content to this rate (e.g. 5) for a sampling-rate A/B; default keeps native")
    ap.add_argument("--kinds", nargs="*", default=["object"], help="collision kinds to label (default: object-object only)")
    ap.add_argument("--min-objects", type=int, default=2, help="min distinct objects per second to count as overlap")
    ap.add_argument("--val-ratio", type=float, default=0.1)
    ap.add_argument("--test-ratio", type=float, default=0.1)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    ffmpeg = ffmpeg_exe()
    clips_dir = args.out_dir / "clips"
    args.out_dir.mkdir(parents=True, exist_ok=True)

    metas = discover_episodes(args.episodes_dir)
    if not metas:
        print(f"No episodes (*.meta.json + .mp4) found under {args.episodes_dir}")
        return
    print(f"Found {len(metas)} episode(s). Slicing into {args.clip_sec}s clips...")

    per_episode: List[Tuple[str, List[dict]]] = []
    for meta in metas:
        eid, recs = process_episode(meta, clips_dir, ffmpeg, args)
        if recs:
            per_episode.append((eid, recs))

    # Split by EPISODE to avoid clip leakage across splits.
    rng.shuffle(per_episode)
    n = len(per_episode)
    n_test = max(1, int(round(n * args.test_ratio))) if n > 2 else 0
    n_val = max(1, int(round(n * args.val_ratio))) if n > 2 else 0
    test_eps = per_episode[:n_test]
    val_eps = per_episode[n_test : n_test + n_val]
    train_eps = per_episode[n_test + n_val :]

    splits = {"train": train_eps, "val": val_eps, "test": test_eps}
    test_gt: Dict[str, list] = {}
    summary = {}
    for name, eps in splits.items():
        recs = [r for _, rs in eps for r in rs]
        # Balance train/val; keep test as-is for honest evaluation.
        if name != "test":
            recs = balance(recs, args.neg_ratio, rng)
        n_pos = sum(1 for r in recs if r["_meta"]["positive"])
        write_jsonl(recs, args.out_dir / f"{name}.jsonl")
        if name == "test":
            for r in recs:
                test_gt[r["_meta"]["clip"]] = r["_meta"]["target"]
        summary[name] = {"clips": len(recs), "positive": n_pos, "episodes": len(eps)}

    (args.out_dir / "test_gt.json").write_text(
        json.dumps(test_gt, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.out_dir / "system_prompt.txt").write_text(
        PROMPTS[args.preset]["system_prompt"], encoding="utf-8"
    )
    (args.out_dir / "dataset_summary.json").write_text(
        json.dumps(
            {"preset": args.preset, "clip_sec": args.clip_sec, "nframes": args.nframes,
             "content_hz": args.content_hz, "neg_ratio": args.neg_ratio, "splits": summary},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print("\nDone. Split summary:")
    for name, s in summary.items():
        print(f"  {name:5s}: {s['clips']:5d} clips  {s['positive']:5d} positive  ({s['episodes']} episodes)")
    print(f"Outputs in {args.out_dir} (train/val/test.jsonl, test_gt.json, system_prompt.txt)")


if __name__ == "__main__":
    main()
