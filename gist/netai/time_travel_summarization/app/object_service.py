"""우주인 프림 생성·활성 부분집합 관리 — 상태는 core, 동작은 여기."""
from pathlib import Path
from typing import Dict

import carb

from ..playback.trajectory_repository import TrajectoryRepository

DEFAULT_ASTRONAUT_USD = str((Path(__file__).resolve().parent.parent / "assets" / "Astronaut.usd").resolve())


def set_active_objects(core, objids) -> int:
    """Restrict physics/capture to the given objids; hide the rest.

    Call BEFORE set_physics_mode so only the active subset gets rigid bodies,
    collision proxies, overlay labels and collision recording. Enables varying
    the object count (4-6) per episode. Returns the active count.
    """
    if not core._prim_map_full:
        core._prim_map_full = dict(core._prim_map)
    want = {str(o) for o in objids}
    new_map = {}
    try:
        import omni.usd
        from pxr import UsdGeom
        stage = omni.usd.get_context().get_stage()
    except Exception:
        stage = None
    for objid, path in core._prim_map_full.items():
        active = str(objid) in want
        if stage is not None:
            prim = stage.GetPrimAtPath(path)
            if prim and prim.IsValid():
                img = UsdGeom.Imageable(prim)
                img.MakeVisible() if active else img.MakeInvisible()
        if active:
            new_map[objid] = path
    core._prim_map = new_map
    return len(new_map)


def spawn_objects(core, count: int) -> Dict[str, str]:
    """씬 프로파일 배치용: 데이터 로드 없이 obj001..objN 프림 풀을 새로 만든다.

    regenerate_astronauts_from_loaded_data()의 "데이터 objid마다 1프림" 대신
    개수만 받아 add_synthetic_objects()의 검증된 스폰 경로를 재사용한다.
    기존 객체가 있으면 먼저 걷어낸다(프레시 배치 기준). 초기 위치는 호출부가
    random_positions로 반드시 배치할 것(생성 직후 원점 겹침 — 일지 #6).
    """
    if not core._config:
        carb.log_error("[TimeTravel] Config must be loaded before spawn_objects")
        return {}
    if not core._config.astronaut_usd:
        core._config.astronaut_usd = DEFAULT_ASTRONAUT_USD
        carb.log_info(f"[TimeTravel] Using default astronaut USD: {DEFAULT_ASTRONAUT_USD}")
    if core._wander:
        core._wander.stop()
        core._wander = None
    core._stage_objects.clear_timetravel_objects()
    core._prim_map.clear()
    if core._prim_map_full:
        core._prim_map_full.clear()
    added = add_synthetic_objects(core, count)
    core.hide_all_cameras()
    carb.log_warn(f"[TimeTravel] spawned {len(added)}/{count} objects without data "
                  f"(scene-profile path)")
    return added


def add_synthetic_objects(core, count: int) -> Dict[str, str]:
    """배치 전용: 궤적 데이터에 없는 추가 우주인을 스폰해 prim_map에 등록.

    physics(wander) 모드는 데이터 좌표가 필요 없으므로 객체 수를 데이터 objid 수
    이상으로 늘릴 수 있다. objid는 기존 개수에 이어 obj{N:03d}로 부여 —
    라벨 규칙(끝자리 숫자)·충돌 기록·오버레이가 prim_map 기준이라 그대로 따라온다.
    주의: 이 객체들은 재현(playback) 데이터가 없다. 호출부가 초기 위치를 반드시
    직접 배치할 것(생성 직후엔 원점 — 겹침 폭발 위험, 일지 #6).
    """
    if count <= 0:
        return {}
    base = dict(core._prim_map_full or core._prim_map)
    start_idx = len(base)
    added: Dict[str, str] = {}
    for k in range(1, int(count) + 1):
        idx = start_idx + k
        objid = f"obj{idx:03d}"
        if objid in base:
            continue
        prim_path = core.create_astronaut_prim(idx)
        if prim_path:
            added[objid] = prim_path
    if added:
        core._prim_map.update(added)
        if core._prim_map_full:
            core._prim_map_full.update(added)
        carb.log_warn(f"[TimeTravel] synthetic objects added: {sorted(added)}")
    return added


def auto_generate_astronauts(core) -> Dict[str, str]:
    if not core._config:
        carb.log_error("[TimeTravel] Config must be loaded before auto-generation")
        return {}

    if not core._config.astronaut_usd:
        core._config.astronaut_usd = DEFAULT_ASTRONAUT_USD
        carb.log_info(f"[TimeTravel] Using default astronaut USD: {DEFAULT_ASTRONAUT_USD}")

    data_uri = core._config.data_uri
    if "://" in core._config.data_path:
        objids = TrajectoryRepository.parse_unique_objids_from_uri(data_uri)
    else:
        csv_path = core._config.resolve_from_config(core._config.data_path)
        if not csv_path.exists():
            carb.log_error(f"[TimeTravel] Data file not found: {csv_path}")
            return {}
        objids = core.parse_unique_objids(str(csv_path))
    if not objids:
        carb.log_error("[TimeTravel] No objids found in CSV")
        return {}

    core.clear_timetravel_objects()

    prim_map = {}
    for i, objid in enumerate(objids, start=1):
        prim_path = core.create_astronaut_prim(i)
        if prim_path:
            prim_map[objid] = prim_path

    core.hide_all_cameras()
    core._prim_map = prim_map
    return prim_map


def _prim_index_for(objid: str, fallback: int, used: set) -> int:
    """prim 인덱스 = objid의 숫자 — 화면 라벨(prim 이름 끝 3자리)이 데이터 ID를
    그대로 따라가게 한다. enumerate 순번을 쓰면 objid가 불연속일 때(예: obj001·
    obj003만 존재) 라벨이 다른 ID로 어긋난다. 숫자가 아니거나 충돌하면 폴백."""
    suffix = objid[3:] if objid.startswith("obj") else objid[-3:]
    if suffix.isdigit() and int(suffix) not in used:
        return int(suffix)
    idx = fallback
    while idx in used:
        idx += 1
    return idx


def regenerate_astronauts_from_loaded_data(core) -> Dict[str, str]:
    if not core._config:
        carb.log_error("[TimeTravel] Config must be loaded before auto-generation")
        return {}
    if not core._config.astronaut_usd:
        core._config.astronaut_usd = DEFAULT_ASTRONAUT_USD
        carb.log_info(f"[TimeTravel] Using default astronaut USD: {DEFAULT_ASTRONAUT_USD}")

    objids = []
    get_object_ids = getattr(core._repository, "get_object_ids", None)
    if callable(get_object_ids):
        objids = get_object_ids()
    if not objids and core._repository.data_start_time:
        objids = sorted(core._repository.get_data_at_time(core._repository.data_start_time).keys())
    if not objids:
        carb.log_error("[TimeTravel] No objids found in loaded data")
        return {}

    if core._wander:
        core._wander.stop()
        core._wander = None
    core._stage_objects.clear_timetravel_objects()

    prim_map = {}
    used_indices: set = set()
    for pos, objid in enumerate(sorted(objids), start=1):
        idx = _prim_index_for(str(objid), fallback=pos, used=used_indices)
        used_indices.add(idx)
        prim_path = core.create_astronaut_prim(idx)
        if prim_path:
            prim_map[objid] = prim_path
        else:
            carb.log_error(f"[TimeTravel] Failed to create astronaut prim for objid={objid}")

    core.hide_all_cameras()
    core._prim_map = prim_map
    if prim_map:
        carb.log_warn(
            f"[TimeTravel] Regenerated {len(prim_map)} astronauts from loaded data "
            f"(objids={len(objids)})"
        )
    else:
        carb.log_error(
            f"[TimeTravel] Loaded data has {len(objids)} objids, but no astronaut prims were created"
        )
    return prim_map


def clear_timetravel_objects(core) -> None:
    if core._wander:
        core._wander.stop()
        core._wander = None
    core._stage_objects.clear_timetravel_objects()
    core._repository.clear()
    core._prim_map.clear()
    core._playback.configure_data_range(None, None)
    core._playback.set_event_summary([])
