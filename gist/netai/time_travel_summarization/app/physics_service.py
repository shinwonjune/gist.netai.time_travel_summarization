"""Physics 모드 전환·충돌/궤적 recorder — 상태는 core, 동작은 여기.

core._wander는 extension.py(shutdown 안전 정지)가 직접 참조하므로 core 속성으로 유지.
"""
from pathlib import Path
from typing import Optional

import carb

# 캡처 부하에서 sim이 실시간을 따라잡게 하는 설정(슬로모션·stuck 오발동 완화).
# minFrameRate를 낮추면 느린 프레임에서도 물리가 substep을 더 돌려 elapsed 시간을 따라잡음.
# 낮을수록 catch-up↑(=정확)·캡처 느려짐. 너무 낮으면 한 프레임에 과도한 substep 위험 → 5 정도.
CAPTURE_MIN_FRAME_RATE = 5
MIN_FRAME_RATE_KEY = "/persistent/simulation/minFrameRate"


def lower_min_frame_rate(core) -> None:
    try:
        import carb.settings
        s = carb.settings.get_settings()
        core._saved_min_frame_rate = s.get(MIN_FRAME_RATE_KEY)
        s.set(MIN_FRAME_RATE_KEY, int(CAPTURE_MIN_FRAME_RATE))
        carb.log_warn(
            f"[Physics] minFrameRate {core._saved_min_frame_rate} -> "
            f"{s.get(MIN_FRAME_RATE_KEY)} (캡처 dilation 완화; playback 복귀 시 원복)"
        )
    except Exception as exc:
        carb.log_warn(f"[Physics] minFrameRate 조정 실패: {exc}")


def restore_min_frame_rate(core) -> None:
    if getattr(core, "_saved_min_frame_rate", None) is None:
        return
    try:
        import carb.settings
        s = carb.settings.get_settings()
        s.set(MIN_FRAME_RATE_KEY, core._saved_min_frame_rate)
        carb.log_warn(f"[Physics] minFrameRate 원복 -> {s.get(MIN_FRAME_RATE_KEY)}")
    except Exception as exc:
        carb.log_warn(f"[Physics] minFrameRate 원복 실패: {exc}")
    finally:
        core._saved_min_frame_rate = None


def on_collision_event(core, prim_path: str, position, kind: str) -> None:
    """WanderController callback: persist a collision as a ground-truth label."""
    rec = core._collisions  # capture-thread가 None으로 바꾸는 경쟁 대비 로컬 참조
    if rec is not None:
        # headless 캡처 중엔 sim 클럭으로 스탬프(프레임·오버레이와 동일 시계).
        when = core.get_sim_clock_datetime() if core._use_sim_clock else None
        rec.record(prim_path, position, kind, when=when)


def start_collision_recorder(core) -> None:
    from datetime import datetime as _dt

    from ..physics import CollisionRecorder

    if core._collisions is not None:
        return
    prim_to_objid = {str(path): objid for objid, path in core._prim_map.items()}
    ts = _dt.now().strftime("%Y%m%dT%H%M%S")
    output_path = core._paths.artifacts_dir / "collisions" / f"collisions_{ts}.csv"
    core._collisions = CollisionRecorder(output_path, prim_to_objid)
    core._collisions.start()
    carb.log_warn(f"[Collision] recording started -> {output_path}")


def stop_collision_recorder(core) -> None:
    if core._collisions is None:
        return
    out = core._collisions.stop()
    rows = core._collisions.row_count
    core._collisions = None
    carb.log_warn(f"[Collision] recording stopped ({rows} events) -> {out}")


def start_trace(core, output_path: Optional[str] = None) -> str:
    """Start streaming current astronaut coordinates to a trajectory CSV."""
    from datetime import datetime as _dt

    import omni.usd

    from ..physics import TraceRecorder

    if core._trace and core._trace.active:
        carb.log_warn("[TimeTravel] trace already active")
        return str(core._trace.output_path)

    if output_path is None:
        ts = _dt.now().strftime("%Y%m%dT%H%M%S")
        trace_dir = core._paths.artifacts_dir / "trace"
        trace_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(trace_dir / f"physics_trace_{ts}.csv")

    resolved = {}
    stage = omni.usd.get_context().get_stage()
    if stage:
        for objid, prim_path in core._prim_map.items():
            prim = stage.GetPrimAtPath(prim_path)
            if prim and prim.IsValid():
                resolved[objid] = prim
    else:
        carb.log_warn("[Trace] recording without an active stage")

    core._trace = TraceRecorder(resolved, Path(output_path))
    core._trace.start()
    carb.log_warn(f"[Trace] recording started -> {output_path}")
    return output_path


def stop_trace(core) -> Optional[str]:
    if not core._trace or not core._trace.active:
        return None
    out = core._trace.stop()
    rows = core._trace.row_count
    carb.log_warn(f"[Trace] saved {rows} rows to {out}")
    return str(out)


def set_physics_mode(core) -> None:
    """Enable PhysX-driven wandering for the configured astronaut prims."""
    import omni.usd
    from pxr import UsdGeom

    from ..physics import WanderController, create_bounding_box, ensure_physics_scene, wrap_with_collision_proxy

    stage = omni.usd.get_context().get_stage()
    if not stage:
        carb.log_warn("[TimeTravel] Cannot enable Physics mode without an active stage")
        return
    lower_min_frame_rate(core)

    if core._wander:
        core._wander.stop()
        core._wander = None

    core._playback.set_mode("physics")
    ensure_physics_scene(stage)

    meters_per_unit = UsdGeom.GetStageMetersPerUnit(stage) or 1.0
    m_to_units = 1.0 / meters_per_unit
    carb.log_info(f"[Physics] stage metersPerUnit={meters_per_unit} m_to_units={m_to_units}")

    # walls 위치·크기를 trajectory 좌표 범위 + margin으로 자동 결정
    # (hardcoded 5×3×5 / origin은 사용자 trajectory 범위와 안 맞을 수 있음)
    is_y_up = UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y
    coord_range = core._repository.get_coord_range()
    min_height = 3.0 * m_to_units      # 최소 천장 높이 (m)
    if coord_range:
        mins, maxs = coord_range
        carb.log_info(f"[Physics] coord_range mins={mins} maxs={maxs}")
        cx = (mins[0] + maxs[0]) / 2.0
        cy = (mins[1] + maxs[1]) / 2.0
        cz = (mins[2] + maxs[2]) / 2.0
        ext_x = (maxs[0] - mins[0])
        ext_y = (maxs[1] - mins[1])
        ext_z = (maxs[2] - mins[2])
        if is_y_up:
            # horizontal = X·Z, vertical = Y
            width, depth = ext_x, ext_z
            height = max(ext_y, min_height)
            # floor가 ground level(mins[1]) 살짝 아래로 가도록 center.y 조정
            box_center = (cx, mins[1] + height / 2.0, cz)
            box_size = (width, height, depth)
        else:
            # horizontal = X·Y, vertical = Z
            width, depth = ext_x, ext_y
            height = max(ext_z, min_height)
            box_center = (cx, cy, mins[2] + height / 2.0)
            box_size = (width, depth, height)
    else:
        # trajectory 미로드 시 기본값
        if is_y_up:
            box_center = (0.0, 1.5, 0.0)
            box_size = (5.0, 3.0, 5.0)
        else:
            box_center = (0.0, 0.0, 1.5)
            box_size = (5.0, 5.0, 3.0)

    carb.log_info(f"[Physics] bounding box center={box_center} size={box_size} up_axis={'Y' if is_y_up else 'Z'}")
    create_bounding_box(stage, center=box_center, size=box_size)
    # Persist bounds so automation can place objects at random in-bounds positions.
    core._physics_bounds = {
        "center": tuple(float(v) for v in box_center),
        "size": tuple(float(v) for v in box_size),
        "is_y_up": bool(is_y_up),
    }

    rigid_prims = []
    proxy_radii = []
    for objid, prim_path in core._prim_map.items():
        prim = stage.GetPrimAtPath(prim_path)
        if not prim or not prim.IsValid():
            carb.log_warn(f"[Physics] skip invalid prim: objid={objid} path={prim_path}")
            continue
        wrapped, proxy_radius = wrap_with_collision_proxy(stage, prim, shape="cylinder", visible=False)
        rigid_prims.append(wrapped)
        proxy_radii.append(proxy_radius)

    # wander는 사용자가 Move 버튼으로 명시 시작. 여기서는 인스턴스만 생성.
    # 벽 근접 탐지를 위해 박스 bounds 전달 (margin = 작은 수평 변의 5% → 벽에 더 붙은 뒤 회전).
    bounds_half = (box_size[0] / 2.0, box_size[1] / 2.0, box_size[2] / 2.0)
    horiz = (box_size[0], box_size[2]) if is_y_up else (box_size[0], box_size[1])
    wall_margin = 0.05 * min(horiz)
    # 객체 간 충돌 거리(거리 기반 fallback 전용): 스치는 접촉까지 잡도록 2.2r.
    # contact report가 기본 탐지원이므로 이 값은 use_contact_reports=False일 때만 쓰임.
    collision_distance = 2.2 * max(proxy_radii) if proxy_radii else 1.0 * m_to_units
    # 라벨/observability용 접촉 정의: contact report는 실제 접촉(중심거리 ≈ 2r)에서
    # 발화하므로 사이드카에는 2.0r을 기록 → 오프라인 recall 분석이 라벨과 같은
    # 규칙을 재현한다. (fallback 탐지 2.2r과 정의가 다름에 주의)
    core._collision_distance = 2.0 * max(proxy_radii) if proxy_radii else 1.0 * m_to_units
    # near-miss: gap이 접촉 거리(2r)보다 넉넉히 커야 GT가 0으로 유지된다.
    # 작으면 안무가 "정지"하기 전에 프록시가 먼저 닿아 contact report(kind=object)가 발화한다.
    near_miss_gap = float(getattr(core, "_near_miss_gap", 0.0) or 0.0)
    near_miss_mode = getattr(core, "_near_miss_mode", "swerve") or "swerve"
    if near_miss_gap > 0.0:
        carb.log_warn(
            f"[Physics] near-miss gap={near_miss_gap:.1f} mode={near_miss_mode} "
            f"(접촉거리 2r={core._collision_distance:.1f})")
        if near_miss_gap <= core._collision_distance * 1.1:
            carb.log_warn(
                f"[Physics] near-miss gap {near_miss_gap:.1f} <= 접촉거리 2r×1.1 "
                f"({core._collision_distance * 1.1:.1f}) — 접촉 발생 위험(GT 오염). gap을 올릴 것"
            )
    core._wander = WanderController(
        rigid_prims,
        speed=core._wander_speed,
        on_collision=core._on_collision_event,
        bounds_center=box_center,
        bounds_half=bounds_half,
        wall_margin=wall_margin,
        collision_distance=collision_distance,
        near_miss_gap=near_miss_gap,
        near_miss_mode=near_miss_mode,
        seed=core._wander_seed,
    )

    try:
        import omni.timeline

        omni.timeline.get_timeline_interface().play()
    except Exception as e:
        carb.log_warn(f"[TimeTravel] Physics mode enabled, but timeline play request failed: {e}")


def set_playback_mode(core) -> None:
    """Return to trajectory playback and remove transient physics controls."""
    import omni.usd

    from ..physics import unwrap

    if core._wander:
        core._wander.stop()
        core._wander = None
    stop_collision_recorder(core)
    restore_min_frame_rate(core)

    stage = omni.usd.get_context().get_stage()
    if stage:
        for prim_path in core._prim_map.values():
            prim = stage.GetPrimAtPath(prim_path)
            if prim and prim.IsValid():
                unwrap(stage, prim)
        if stage.GetPrimAtPath("/World/PhysicsWalls"):
            stage.RemovePrim("/World/PhysicsWalls")

    core._playback.set_mode("playback")
    core.update_stage_objects()
