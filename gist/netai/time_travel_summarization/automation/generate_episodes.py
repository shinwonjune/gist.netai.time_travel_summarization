"""Headless batch generator for BEV collision episodes (run via `kit --exec`).

Drives the extension's facade programmatically to produce many episodes without
UI clicks: for each episode it picks 4-6 objects, seed-randomizes their start
positions, runs physics (wander) while capturing offscreen video + a 30Hz trace,
and writes everything into a per-episode folder that `utils.build_dataset` (and
`utils.observability`) consume directly.

Per-episode flow (single Kit session, real-time capture):
    set_active_objects(N) -> apply random positions -> set_physics_mode
    -> start_trace(30Hz) -> start_wander -> run_capture_headless(duration)
    -> stop_wander / stop_trace / set_playback_mode -> organize outputs

Run (headless Kit with the extension enabled):
    kit --no-window --enable <ext> --exec "automation/generate_episodes.py -- \
        --episodes 50 --out artifacts/episodes --duration 40"

Pure helpers are importable/testable without Kit:
    python automation/generate_episodes.py --self-test
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class EpisodeConfig:
    idx: int
    seed: int
    n_objects: int
    speed: float
    duration: float


# --------------------------------------------------------------------------- #
# pure helpers (no Kit dependency -> unit-testable)
# --------------------------------------------------------------------------- #
def episode_configs(n_episodes: int, min_obj: int, max_obj: int,
                    speed_range: Tuple[float, float], duration: float,
                    seed: int) -> List[EpisodeConfig]:
    """Deterministically build per-episode configs from a master seed."""
    rng = random.Random(seed)
    cfgs = []
    for i in range(n_episodes):
        cfgs.append(EpisodeConfig(
            idx=i,
            seed=rng.randint(0, 2**31 - 1),
            n_objects=rng.randint(min_obj, max_obj),
            speed=round(rng.uniform(*speed_range), 2),
            duration=duration,
        ))
    return cfgs


def random_positions(bounds: dict, objids: List[str], seed: int,
                     margin_frac: float = 0.1, min_sep_frac: float = 0.15) -> Dict[str, Tuple[float, float, float]]:
    """Seeded random in-bounds start positions, spaced to avoid initial overlap.

    bounds = {center:(cx,cy,cz), size:(sx,sy,sz), is_y_up:bool}. Horizontal axes
    are (x,z) for Y-up else (x,y); the vertical coord is set to the box center.
    """
    cx, cy, cz = bounds["center"]
    sx, sy, sz = bounds["size"]
    if bounds.get("is_y_up", True):
        (h0c, h0s), (h1c, h1s), vert = (cx, sx), (cz, sz), ("y", cy)
        ax = ("x", "z")
    else:
        (h0c, h0s), (h1c, h1s), vert = (cx, sx), (cy, sy), ("z", cz)
        ax = ("x", "y")
    m = margin_frac
    lo0, hi0 = h0c - h0s * (0.5 - m), h0c + h0s * (0.5 - m)
    lo1, hi1 = h1c - h1s * (0.5 - m), h1c + h1s * (0.5 - m)
    min_sep = min_sep_frac * min(h0s, h1s)
    rng = random.Random(seed)
    placed: List[Tuple[float, float]] = []
    out: Dict[str, Tuple[float, float, float]] = {}
    for objid in objids:
        for _ in range(200):
            p0, p1 = rng.uniform(lo0, hi0), rng.uniform(lo1, hi1)
            if all((p0 - q0) ** 2 + (p1 - q1) ** 2 >= min_sep ** 2 for q0, q1 in placed):
                break
        placed.append((p0, p1))
        coord = {ax[0]: p0, ax[1]: p1, vert[0]: vert[1]}
        out[objid] = (coord["x"], coord["y"], coord["z"])
    return out


def organize_outputs(out_root: Path, idx: int, video: Path,
                     collisions: Optional[Path], trace: Optional[Path]) -> Path:
    """Move an episode's video+meta(+collisions+trace) into out_root/ep_XXXX/."""
    ep_dir = out_root / f"ep_{idx:04d}"
    ep_dir.mkdir(parents=True, exist_ok=True)
    moved = {}
    video = Path(video)
    meta = video.with_suffix(".meta.json")
    for label, src in (("video", video), ("meta", meta),
                       ("collisions", collisions), ("trace", trace)):
        if src and Path(src).exists():
            dst = ep_dir / Path(src).name
            shutil.move(str(src), str(dst))
            moved[label] = str(dst)
    return ep_dir


def pick_objids(all_objids: List[str], n: int, seed: int) -> List[str]:
    rng = random.Random(seed)
    n = min(n, len(all_objids))
    return sorted(rng.sample(list(all_objids), n))


# --------------------------------------------------------------------------- #
# Kit-driving parts (import omni lazily so the module loads without Kit)
# --------------------------------------------------------------------------- #
def _get_core():
    from gist.netai.time_travel_summarization.extension import get_active_core
    core = get_active_core()
    if core is None:
        raise RuntimeError("extension not started / no active core")
    return core


def apply_positions(core, positions: Dict[str, Tuple[float, float, float]]) -> None:
    import omni.usd
    from pxr import UsdGeom, Gf

    stage = omni.usd.get_context().get_stage()
    prim_map = getattr(core, "_prim_map", {})
    for objid, xyz in positions.items():
        path = prim_map.get(objid)
        if not path:
            continue
        prim = stage.GetPrimAtPath(path)
        if prim and prim.IsValid():
            UsdGeom.XformCommonAPI(prim).SetTranslate(Gf.Vec3d(float(xyz[0]), float(xyz[1]), float(xyz[2])))


def run(args, core=None) -> None:
    import omni.kit.app  # noqa: F401  (ensures Kit context)

    core = core or _get_core()
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    core.load_data()
    if hasattr(core, "auto_generate_astronauts"):
        core.auto_generate_astronauts()

    # Bounds depend only on the trajectory range (constant) -> compute once.
    core.set_physics_mode()
    bounds = core.get_physics_bounds()
    core.set_playback_mode()
    all_objids = list(getattr(core, "_prim_map_full", None) or getattr(core, "_prim_map", {}))
    if not all_objids:
        raise RuntimeError("no objects available (auto_generate failed?)")

    cfgs = episode_configs(args.episodes, args.min_objects, args.max_objects,
                           (args.speed_min, args.speed_max), args.duration, args.seed)
    print(f"[gen] {len(cfgs)} episodes, objects available={len(all_objids)}, out={out_root}")

    for cfg in cfgs:
        objids = pick_objids(all_objids, cfg.n_objects, cfg.seed)
        core.set_active_objects(objids)
        apply_positions(core, random_positions(bounds, objids, cfg.seed))
        core.set_wander_speed(cfg.speed)
        core.set_physics_mode()
        trace_path = str((out_root / f"_trace_{cfg.idx:04d}.csv").resolve())
        video_path = str((out_root / f"_video_{cfg.idx:04d}.mp4").resolve())
        core.start_trace(trace_path)
        core.start_wander()
        produced = core.run_capture_headless(cfg.duration, video_path)
        core.stop_wander()
        core.stop_trace()
        core.set_playback_mode()
        if not produced:
            print(f"[gen] ep {cfg.idx}: capture failed; skipping")
            continue
        # collisions path comes from the sidecar the capture wrote
        meta = Path(produced).with_suffix(".meta.json")
        collisions = None
        if meta.exists():
            cj = json.loads(meta.read_text(encoding="utf-8")).get("collisions_csv")
            collisions = Path(cj) if cj else None
        ep_dir = organize_outputs(out_root, cfg.idx, Path(produced), collisions, Path(trace_path))
        print(f"[gen] ep {cfg.idx}: objs={objids} speed={cfg.speed} -> {ep_dir}")

    print("[gen] done.")


def _self_test() -> None:
    """Exercise the pure helpers without Kit (this-session verification)."""
    import tempfile
    cfgs = episode_configs(5, 4, 6, (200.0, 300.0), 40.0, seed=7)
    assert len(cfgs) == 5 and all(4 <= c.n_objects <= 6 for c in cfgs)
    assert episode_configs(5, 4, 6, (200, 300), 40, 7) == cfgs, "configs not deterministic"
    bounds = {"center": (0.0, 1.5, 0.0), "size": (100.0, 3.0, 80.0), "is_y_up": True}
    objids = [f"obj{i:03d}" for i in range(1, 6)]
    pos = random_positions(bounds, objids, seed=7)
    assert set(pos) == set(objids)
    for (x, y, z) in pos.values():
        assert -50 <= x <= 50 and -40 <= z <= 40 and y == 1.5
    pts = list((x, z) for x, y, z in pos.values())
    assert all((pts[i][0]-pts[j][0])**2 + (pts[i][1]-pts[j][1])**2 >= (0.15*80)**2
               for i in range(len(pts)) for j in range(i+1, len(pts))), "min-sep violated"
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        v = root / "_video_0003.mp4"; v.write_text("v")
        v.with_suffix(".meta.json").write_text("{}")
        col = root / "collisions_x.csv"; col.write_text("c")
        tr = root / "_trace_0003.csv"; tr.write_text("t")
        ep = organize_outputs(root, 3, v, col, tr)
        names = sorted(p.name for p in ep.iterdir())
        assert names == ["_trace_0003.csv", "_video_0003.meta.json", "_video_0003.mp4", "collisions_x.csv"], names
    print("self-test OK:", [(c.idx, c.n_objects, c.speed) for c in cfgs])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episodes", type=int, default=50)
    ap.add_argument("--out", type=str, default="artifacts/episodes")
    ap.add_argument("--duration", type=float, default=40.0)
    ap.add_argument("--min-objects", type=int, default=4)
    ap.add_argument("--max-objects", type=int, default=6)
    ap.add_argument("--speed-min", type=float, default=200.0)
    ap.add_argument("--speed-max", type=float, default=300.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--self-test", action="store_true", help="run pure-helper tests without Kit")
    args = ap.parse_args()
    if args.self_test:
        _self_test()
        return
    run(args)


if __name__ == "__main__":
    main()
