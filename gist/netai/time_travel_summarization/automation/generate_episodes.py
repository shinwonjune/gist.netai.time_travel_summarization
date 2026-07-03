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


def _ensure_stage(core, stage_url: Optional[str] = None) -> None:
    """Headless bootstrap: open the requested USD (또는 빈 스테이지) + BEV 카메라 재보장.

    stage_url이 주어지면(로컬 경로 또는 omniverse:// Nucleus URL) 그 씬을 연다 —
    GUI에서 쓰던 실제 씬으로 headless 촬영 가능. 없으면 빈 스테이지(바닥/벽은
    set_physics_mode의 create_bounding_box가 생성). summarization 카메라는 확장
    시작 시(스테이지 없음) 생성 실패했을 수 있어 재보장.
    """
    import omni.usd

    ctx = omni.usd.get_context()
    if stage_url:
        ok = ctx.open_stage(stage_url)
        if not ok:
            raise RuntimeError(f"failed to open stage: {stage_url}")
        print(f"[gen] opened stage: {stage_url}")
        # 비동기 페이로드 로딩 완료까지 대기: 끝나기 전에 physics를 켜면 아직 콜라이더가
        # 없는 바닥을 뚫고 무한낙하한다(실측 y=-16594; GUI는 로드 완료 후 조작하므로 정상).
        import time as _time

        import omni.kit.app
        app = omni.kit.app.get_app()
        deadline = _time.time() + 300.0
        settled, loading = 0, None
        while _time.time() < deadline:
            app.update()
            try:
                _msg, _loaded, loading = ctx.get_stage_loading_status()
            except Exception:
                break
            settled = settled + 1 if loading == 0 else 0
            if settled >= 60:  # 로딩 0 상태가 60 update 연속 유지되면 완료로 간주
                break
        print(f"[gen] stage loading settled (loading={loading})")
    elif ctx.get_stage() is None:
        ctx.new_stage()
        print("[gen] no active stage -> created a new empty stage")
    so = getattr(core, "_stage_objects", None)
    if so is not None and hasattr(so, "ensure_summarization_camera"):
        so.ensure_summarization_camera()


def _resolve_camera(camera: Optional[str]) -> Optional[str]:
    """카메라 인자를 프림 경로로 해석. '/'로 시작하면 그대로, 아니면 이름으로 스테이지 검색."""
    if not camera:
        return None
    if camera.startswith("/"):
        return camera
    import omni.usd

    stage = omni.usd.get_context().get_stage()
    if stage is not None:
        for prim in stage.Traverse():
            if prim.GetName() == camera and prim.GetTypeName() == "Camera":
                path = str(prim.GetPath())
                print(f"[gen] camera '{camera}' resolved -> {path}")
                return path
    raise RuntimeError(f"camera named {camera!r} not found in stage")


def run(args, core=None) -> None:
    import omni.kit.app  # noqa: F401  (ensures Kit context)

    core = core or _get_core()
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    _ensure_stage(core, getattr(args, "stage", None))
    camera_path = _resolve_camera(getattr(args, "camera", None))
    ok = core.load_data()
    _repo = getattr(core, "_repository", None)
    print(f"[gen] load_data={ok} err={getattr(core, '_last_data_load_error', '')!r} "
          f"repo_start={getattr(_repo, 'data_start_time', None)}")
    # GUI와 동일 경로: regenerate...는 repo를 보존하지만 auto_generate...는 내부에서
    # clear_timetravel_objects()로 _repository까지 지워버린다(facade.py:978) →
    # 좌표 데이터 소실 → 배치 no-op → 전원 (0,0,0) 겹침 폭발의 근원.
    if hasattr(core, "regenerate_astronauts_from_loaded_data"):
        core.regenerate_astronauts_from_loaded_data()
    elif hasattr(core, "auto_generate_astronauts"):
        core.auto_generate_astronauts()

    # Bounds depend only on the trajectory range (constant) -> compute once.
    core.set_physics_mode()
    bounds = core.get_physics_bounds()
    core.set_playback_mode()
    _repo = getattr(core, "_repository", None)
    print(f"[gen] after playback_mode: repo_start={getattr(_repo, 'data_start_time', None)}")
    all_objids = list(getattr(core, "_prim_map_full", None) or getattr(core, "_prim_map", {}))
    if not all_objids:
        raise RuntimeError("no objects available (auto_generate failed?)")

    cfgs = episode_configs(args.episodes, args.min_objects, args.max_objects,
                           (args.speed_min, args.speed_max), args.duration, args.seed)
    print(f"[gen] {len(cfgs)} episodes, objects available={len(all_objids)}, out={out_root}")

    for cfg in cfgs:
        objids = pick_objids(all_objids, cfg.n_objects, cfg.seed)
        core.set_active_objects(objids)
        if getattr(args, "keep_positions", False):
            # GUI와 동일: 궤적 데이터 첫 시점 좌표로 벌려놓기. 이걸 안 하면 생성 직후
            # 전원이 (0,0,0)에 완전히 겹친 채 physics가 켜져 PhysX 겹침해소 폭발로
            # 벽을 관통해 낙하한다(실측: step30에 z 4->36, 이후 y -13789).
            core.set_to_earliest_time()
            # 배치 검증 로그: repository가 비었으면 위 호출이 조용히 no-op이 된다.
            repo = getattr(core, "_repository", None)
            start_t = getattr(repo, "data_start_time", None)
            first_path = next(iter(getattr(core, "_prim_map", {}).values()), None)
            pos = None
            try:
                import omni.usd
                from pxr import UsdGeom
                stage_now = omni.usd.get_context().get_stage()
                prim = stage_now.GetPrimAtPath(first_path) if first_path else None
                if prim and prim.IsValid():
                    pos = tuple(round(v, 1) for v in
                                UsdGeom.XformCache(0).GetLocalToWorldTransform(prim).ExtractTranslation())
            except Exception:
                pass
            print(f"[gen] keep-positions: data_start={start_t} obj1@{pos}")
        else:
            apply_positions(core, random_positions(bounds, objids, cfg.seed))
        core.set_wander_speed(cfg.speed)
        if hasattr(core, "set_wander_seed"):
            core.set_wander_seed(cfg.seed)  # heading 재현성(페이싱 재현과 별개)
        core.set_physics_mode()
        trace_path = str((out_root / f"_trace_{cfg.idx:04d}.csv").resolve())
        video_path = str((out_root / f"_video_{cfg.idx:04d}.mp4").resolve())
        core.start_trace(trace_path)
        core.start_wander()
        produced = core.run_capture_headless(cfg.duration, video_path, camera_path=camera_path)
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
    ap.add_argument("--stage", type=str, default=None,
                    help="USD to open (local path or omniverse:// URL); default: new empty stage")
    ap.add_argument("--camera", type=str, default=None,
                    help="capture camera: prim path (/World/..) or prim name to search; "
                         "default: /World/summarization_camera")
    ap.add_argument("--keep-positions", action="store_true",
                    help="skip random start positions; keep data-driven positions (GUI와 동일)")
    ap.add_argument("--quit", action="store_true", help="quit Kit after finishing (batch/CI)")
    ap.add_argument("--self-test", action="store_true", help="run pure-helper tests without Kit")
    args = ap.parse_args()
    if args.self_test:
        _self_test()
        return
    try:
        run(args)
    finally:
        if args.quit:
            try:
                import omni.kit.app
                omni.kit.app.get_app().post_quit(0)
            except Exception:
                pass


if __name__ == "__main__":
    main()
