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
import datetime
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
    base_time_s: int  # 라벨 시계 t0(자정 기준 초). 오버레이/CSV가 이 시각부터 흐른다.


# --------------------------------------------------------------------------- #
# pure helpers (no Kit dependency -> unit-testable)
# --------------------------------------------------------------------------- #
def episode_configs(n_episodes: int, min_obj: int, max_obj: int,
                    speed_range: Tuple[float, float], duration: float,
                    seed: int) -> List[EpisodeConfig]:
    """Deterministically build per-episode configs from a master seed."""
    rng = random.Random(seed)
    cfgs = []
    # 자정 넘김 금지: t0 + duration이 23:59:59를 넘으면 시분초 라벨이 역전된다.
    base_max = max(0, 86400 - int(duration) - 1)
    for i in range(n_episodes):
        cfgs.append(EpisodeConfig(
            idx=i,
            seed=rng.randint(0, 2**31 - 1),
            n_objects=rng.randint(min_obj, max_obj),
            speed=round(rng.uniform(*speed_range), 2),
            duration=duration,
            base_time_s=rng.randint(0, base_max),
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


def sample_floor_positions(bounds: dict, objids: List[str], seed: int, probe_floor,
                           floor_ref: float, spawn_offset: float = 5.0,
                           tol_below: float = 100.0, tol_above: float = 50.0,
                           margin_frac: float = 0.1, min_sep_frac: float = 0.15,
                           tries_per_obj: int = 60) -> Dict[str, Tuple[float, float, float]]:
    """무작위 시작 위치(바닥 검증): 궤적 범위 내 수평 무작위 샘플 중, probe_floor가
    보고한 바닥 높이가 floor_ref 허용창(-tol_below~+tol_above) 안인 지점만 채택하고
    바닥+spawn_offset(cm)에 스폰 — 중력으로 안착. 바닥 없는 지점(무한낙하, 일지 #7-6)과
    타 객체 위 히트를 걸러낸다.

    probe_floor(h0, h1) -> float|None: 수평 좌표 위에서 아래로 쏜 레이 히트의 수직값.
    검증 실패 객체는 결과에서 빠진다(호출부가 데이터 좌표 폴백).
    """
    cx, cy, cz = bounds["center"]
    sx, sy, sz = bounds["size"]
    if bounds.get("is_y_up", True):
        (h0c, h0s), (h1c, h1s) = (cx, sx), (cz, sz)
        make = lambda h0, h1, v: (h0, v, h1)
    else:
        (h0c, h0s), (h1c, h1s) = (cx, sx), (cy, sy)
        make = lambda h0, h1, v: (h0, h1, v)
    m = margin_frac
    lo0, hi0 = h0c - h0s * (0.5 - m), h0c + h0s * (0.5 - m)
    lo1, hi1 = h1c - h1s * (0.5 - m), h1c + h1s * (0.5 - m)
    min_sep = min_sep_frac * min(h0s, h1s)
    rng = random.Random(seed)
    placed: List[Tuple[float, float]] = []
    out: Dict[str, Tuple[float, float, float]] = {}
    for objid in objids:
        for _ in range(tries_per_obj):
            p0, p1 = rng.uniform(lo0, hi0), rng.uniform(lo1, hi1)
            if any((p0 - q0) ** 2 + (p1 - q1) ** 2 < min_sep ** 2 for q0, q1 in placed):
                continue
            hit_v = probe_floor(p0, p1)
            if hit_v is None or not (floor_ref - tol_below <= hit_v <= floor_ref + tol_above):
                continue
            placed.append((p0, p1))
            out[objid] = make(p0, p1, hit_v + spawn_offset)
            break
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


def write_run_manifest(out_root: Path, args_dict: dict, cfgs: List[EpisodeConfig],
                       done_idx: List[int], git_commit: Optional[str] = None) -> Path:
    """배치 재현·역추적용 manifest: 생성 인자, 에피소드별 조건·시드, 성공 여부."""
    done = set(done_idx)
    manifest = {
        "schema_version": 1,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "git_commit": git_commit,
        "args": {k: args_dict[k] for k in sorted(args_dict)},
        "episodes": [
            {"idx": c.idx, "dir": f"ep_{c.idx:04d}", "seed": c.seed,
             "n_objects": c.n_objects, "speed": c.speed, "duration": c.duration,
             "base_time_s": c.base_time_s, "ok": c.idx in done}
            for c in cfgs
        ],
    }
    path = out_root / "_run_manifest.json"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def upload_episode(ep_dir: Path, upload_uri: str) -> None:
    """에피소드 폴더를 {upload_uri}/{ep명}/으로 업로드. 시간·바이트 로그 = 레이크 처리량 실측."""
    import time as _time

    from gist.netai.time_travel_summarization.storage import from_uri

    base = upload_uri.rstrip("/") + "/" + ep_dir.name
    adapter = from_uri(base)
    ctypes = {".mp4": "video/mp4", ".json": "application/json", ".csv": "text/csv"}
    total = 0
    t0 = _time.time()
    for f in sorted(p for p in ep_dir.iterdir() if p.is_file()):
        adapter.put_file(f"{base}/{f.name}", f,
                         content_type=ctypes.get(f.suffix, "application/octet-stream"))
        total += f.stat().st_size
    dt = max(_time.time() - t0, 1e-6)
    print(f"[gen] upload {ep_dir.name}: {total / 1e6:.1f} MB in {dt:.1f}s "
          f"({total / 1e6 / dt:.1f} MB/s) -> {base}")


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


def precompute_floor_positions(core, bounds: dict, cfgs: List[EpisodeConfig],
                               all_objids: List[str]) -> Dict[int, Dict[str, Tuple[float, float, float]]]:
    """startup의 physics 활성 윈도우에서 전 에피소드 시작 위치를 레이캐스트 검증으로 사전 계산.

    시뮬레이션 활성 중 USD 텔레포트는 PhysX에 반영되지 않으므로(run17 실측: 전원 동결·
    trace 0행) 여기서는 좌표 "계산"만 하고, 적용(apply_positions)은 에피소드 루프에서
    physics OFF 상태에 한다(#6의 검증된 순서: 배치 → physics ON).
    바닥 기준(floor_ref) = 데이터 좌표에 서 있는 현재 객체들의 수직 좌표 중앙값.
    """
    import carb
    import omni.kit.app
    from omni.physx import get_physx_scene_query_interface

    app = omni.kit.app.get_app()
    for _ in range(10):  # physics scene/콜라이더 초기화 소진 (scene query 준비)
        app.update()

    is_y_up = bounds.get("is_y_up", True)
    vert = 1 if is_y_up else 2
    cur = core.get_current_object_positions() or {}
    floors = sorted(float(p[vert]) for p in cur.values())
    floor_ref = floors[len(floors) // 2] if floors else float(bounds["center"][vert])
    top = floor_ref + max(float(bounds["size"][vert]), 300.0) + 200.0
    max_dist = (top - floor_ref) + 1000.0

    sq = get_physx_scene_query_interface()

    def probe_floor(h0, h1):
        if is_y_up:
            origin, direction = carb.Float3(h0, top, h1), carb.Float3(0.0, -1.0, 0.0)
        else:
            origin, direction = carb.Float3(h0, h1, top), carb.Float3(0.0, 0.0, -1.0)
        hit = sq.raycast_closest(origin, direction, max_dist)
        if hit and hit.get("hit"):
            return float(hit["position"][vert])
        return None

    out: Dict[int, Dict[str, Tuple[float, float, float]]] = {}
    for cfg in cfgs:
        objids = pick_objids(all_objids, cfg.n_objects, cfg.seed)
        pos = sample_floor_positions(bounds, objids, cfg.seed, probe_floor, floor_ref)
        missing = [o for o in objids if o not in pos]
        if missing:
            print(f"[gen] pre-pos ep{cfg.idx}: no valid floor for {missing} -> data-coord fallback")
        out[cfg.idx] = pos
        print(f"[gen] pre-pos ep{cfg.idx}: {len(pos)}/{len(objids)} floor_ref={floor_ref:.1f} "
              f"{ {k: tuple(round(v, 1) for v in xyz) for k, xyz in pos.items()} }")
    return out


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

    all_objids = list(getattr(core, "_prim_map_full", None) or getattr(core, "_prim_map", {}))
    if not all_objids:
        raise RuntimeError("no objects available (auto_generate failed?)")
    data_objids = set(all_objids)

    # 합성 객체 추가: physics 모드는 궤적 데이터가 불필요하므로 풀을 늘릴 수 있다.
    # 단 keep-positions와는 양립 불가(합성 객체는 데이터 좌표가 없음).
    extra = int(getattr(args, "extra_objects", 0) or 0)
    if extra > 0 and getattr(args, "keep_positions", False):
        print("[gen] --extra-objects ignored with --keep-positions (no data coords for synthetic)")
        extra = 0
    if extra > 0 and hasattr(core, "add_synthetic_objects"):
        added = core.add_synthetic_objects(extra)
        all_objids = list(getattr(core, "_prim_map_full", None) or getattr(core, "_prim_map", {}))
        print(f"[gen] synthetic objects: +{len(added)} -> pool={sorted(all_objids)}")

    cfgs = episode_configs(args.episodes, args.min_objects, args.max_objects,
                           (args.speed_min, args.speed_max), args.duration, args.seed)
    print(f"[gen] {len(cfgs)} episodes, objects available={len(all_objids)}, out={out_root}")
    done_idx: List[int] = []

    # 단일 physics 토글 윈도우: bounds 계산 + (무작위 모드) 위치 사전계산을 함께 끝낸다.
    # 토글을 두 번 거치면 이후 캡처의 phys 스텝에서 timeline play가 안 먹는 상태가
    # 실측됨(run18: 전 phys 스텝 ratio_t=0 → sim이 라벨의 절반 속도) — run13~15와 동일한
    # 단일 토글 구조를 유지한다. 합성 객체는 이 윈도우 동안 원점에 있지만(레이캐스트
    # 허용창이 그 위 히트를 걸러냄) 에피소드 시작 전 반드시 재배치된다.
    core.set_to_earliest_time()  # 데이터 객체를 바닥 위 좌표로 (floor_ref 산출용, #6 산개)
    core.set_physics_mode()
    bounds = core.get_physics_bounds()
    start_positions: Dict[int, Dict[str, Tuple[float, float, float]]] = {}
    if not getattr(args, "keep_positions", False):
        start_positions = precompute_floor_positions(core, bounds, cfgs, all_objids)
    core.set_playback_mode()
    _repo = getattr(core, "_repository", None)
    print(f"[gen] after playback_mode: repo_start={getattr(_repo, 'data_start_time', None)}")

    for cfg in cfgs:
        objids = pick_objids(all_objids, cfg.n_objects, cfg.seed)
        core.set_active_objects(objids)
        # 모드 무관 공통: 궤적 데이터 첫 시점 좌표로 벌려놓기. 이걸 안 하면 생성 직후
        # 전원이 (0,0,0)에 완전히 겹친 채 physics가 켜져 PhysX 겹침해소 폭발로
        # 벽을 관통해 낙하한다(실측: step30에 z 4->36, 이후 y -13789).
        # 무작위 배치도 이 안전 좌표에서 physics를 켠 "뒤" 검증된 위치로 텔레포트한다.
        core.set_to_earliest_time()
        synth_active = [o for o in objids if o not in data_objids]
        if synth_active:
            # 합성 객체는 데이터 좌표가 없음 — 사전 계산 위치가 없더라도 산개는 보장(#6 방지).
            apply_positions(core, random_positions(bounds, synth_active, cfg.seed + 1))
        pre = start_positions.get(cfg.idx) or {}
        if pre:
            # physics OFF 상태 적용 → PhysX가 켜질 때 이 좌표를 초기 포즈로 인식(확실 반영).
            apply_positions(core, pre)
        if getattr(args, "keep_positions", False):
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
        core.set_wander_speed(cfg.speed)
        if hasattr(core, "set_wander_seed"):
            core.set_wander_seed(cfg.seed)  # heading 재현성(페이싱 재현과 별개)
        core.set_physics_mode()
        trace_path = str((out_root / f"_trace_{cfg.idx:04d}.csv").resolve())
        video_path = str((out_root / f"_video_{cfg.idx:04d}.mp4").resolve())
        core.start_trace(trace_path)
        core.start_wander()
        # 라벨 시각 = 무작위 t0 + sim 경과(실행 시각과 무관). 날짜부는 표기 안 되므로 오늘 날짜 사용.
        anchor = datetime.datetime.combine(
            datetime.date.today(), datetime.time()) + datetime.timedelta(seconds=cfg.base_time_s)
        print(f"[gen] ep {cfg.idx}: base_time={anchor.time()}")
        produced = core.run_capture_headless(cfg.duration, video_path, camera_path=camera_path,
                                             capture_start_dt=anchor,
                                             render_fps=getattr(args, "render_fps", None))
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
        done_idx.append(cfg.idx)
        print(f"[gen] ep {cfg.idx}: objs={objids} speed={cfg.speed} -> {ep_dir}")
        if getattr(args, "upload_uri", None):
            try:
                upload_episode(ep_dir, args.upload_uri)
            except Exception as e:
                print(f"[gen] upload FAILED for ep {cfg.idx}: {e!r} (local files kept)")

    git_commit = None
    try:
        import subprocess
        git_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(Path(__file__).resolve().parent),
            text=True, timeout=10).strip()
    except Exception:
        pass
    manifest_path = write_run_manifest(out_root, dict(vars(args)), cfgs, done_idx, git_commit)
    print(f"[gen] manifest -> {manifest_path} (ok {len(done_idx)}/{len(cfgs)})")
    if getattr(args, "upload_uri", None):
        try:
            from gist.netai.time_travel_summarization.storage import from_uri
            uri = args.upload_uri.rstrip("/") + "/_run_manifest.json"
            from_uri(uri).put_file(uri, manifest_path, content_type="application/json")
            print(f"[gen] manifest uploaded -> {uri}")
        except Exception as e:
            print(f"[gen] manifest upload FAILED: {e!r}")

    print("[gen] done.")


def _self_test() -> None:
    """Exercise the pure helpers without Kit (this-session verification)."""
    import tempfile
    cfgs = episode_configs(5, 4, 6, (200.0, 300.0), 40.0, seed=7)
    assert len(cfgs) == 5 and all(4 <= c.n_objects <= 6 for c in cfgs)
    assert all(0 <= c.base_time_s <= 86400 - 41 for c in cfgs)
    assert len({c.base_time_s for c in cfgs}) > 1, "base times should vary"
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
    # sample_floor_positions: 바닥 없는 구역 기각, floor+offset 스폰, 허용창·폴백
    bounds2 = {"center": (0.0, 90.0, 0.0), "size": (1000.0, 300.0, 800.0), "is_y_up": True}
    pos2 = sample_floor_positions(bounds2, ["a", "b", "c"], 7,
                                  lambda h0, h1: 90.0 if h0 >= 0 else None, 90.0)
    assert set(pos2) == {"a", "b", "c"}
    assert all(p[0] >= 0 for p in pos2.values()), "no-floor half must be rejected"
    assert all(abs(p[1] - 95.0) < 1e-9 for p in pos2.values()), "spawn = floor + 5cm"
    assert sample_floor_positions(bounds2, ["a"], 7, lambda h0, h1: None, 90.0) == {}
    assert sample_floor_positions(bounds2, ["a"], 7, lambda h0, h1: 300.0, 90.0) == {}, "tol window"
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        v = root / "_video_0003.mp4"; v.write_text("v")
        v.with_suffix(".meta.json").write_text("{}")
        col = root / "collisions_x.csv"; col.write_text("c")
        tr = root / "_trace_0003.csv"; tr.write_text("t")
        ep = organize_outputs(root, 3, v, col, tr)
        names = sorted(p.name for p in ep.iterdir())
        assert names == ["_trace_0003.csv", "_video_0003.meta.json", "_video_0003.mp4", "collisions_x.csv"], names
        mpath = write_run_manifest(root, {"episodes": 5, "seed": 7}, cfgs, [0, 2], "abc123")
        mj = json.loads(mpath.read_text(encoding="utf-8"))
        assert mj["git_commit"] == "abc123" and len(mj["episodes"]) == 5
        assert [e["ok"] for e in mj["episodes"]] == [True, False, True, False, False]
        assert mj["episodes"][1]["base_time_s"] == cfgs[1].base_time_s
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
    ap.add_argument("--render-fps", type=int, default=30,
                    help="video/render fps (60의 약수; sim은 항상 60Hz). 60=데시메이션 없음. "
                         "30 선택 근거: 일지 #10 — 2배 단축 + 위상정렬·검수성 유지")
    ap.add_argument("--stage", type=str, default=None,
                    help="USD to open (local path or omniverse:// URL); default: new empty stage")
    ap.add_argument("--camera", type=str, default=None,
                    help="capture camera: prim path (/World/..) or prim name to search; "
                         "default: /World/summarization_camera")
    ap.add_argument("--keep-positions", action="store_true",
                    help="skip random start positions; keep data-driven positions (GUI와 동일)")
    ap.add_argument("--extra-objects", type=int, default=0,
                    help="궤적 데이터 외 합성 우주인 추가 수(physics 전용; keep-positions와 양립 불가)")
    ap.add_argument("--upload-uri", type=str, default=None,
                    help="에피소드·manifest 업로드 대상 (예: s3://time-travel-summarization/episodes/prod-YYYYMMDD)")
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
            # post_quit 후에도 비데몬 스레드가 프로세스를 잡아 잔류하는 고질 →
            # 15초 내 정상 종료가 안 되면 강제 탈출(출력은 run()에서 이미 완결).
            # 배치 완료 알림이 "프로세스 종료" 이벤트에 의존하므로 종료 보장이 필수.
            import os as _os
            import threading as _threading
            import time as _time

            def _force_exit():
                _time.sleep(15.0)
                print("[gen] force exit (quit did not complete in 15s)")
                _os._exit(0)

            _threading.Thread(target=_force_exit, daemon=True).start()


if __name__ == "__main__":
    main()
