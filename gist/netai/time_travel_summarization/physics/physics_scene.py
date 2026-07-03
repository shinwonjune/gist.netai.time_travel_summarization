def ensure_physics_scene(stage, scene_path: str = "/World/PhysicsScene",
                         time_steps_per_second: int = 60):
    """Create or update the stage physics scene and return its prim."""
    from pxr import Gf, UsdGeom, UsdPhysics

    scene_prim = stage.GetPrimAtPath(scene_path)
    if scene_prim and scene_prim.IsValid():
        scene = UsdPhysics.Scene(scene_prim)
    else:
        scene = UsdPhysics.Scene.Define(stage, scene_path)
        scene_prim = scene.GetPrim()

    up_axis = UsdGeom.GetStageUpAxis(stage)
    if up_axis == UsdGeom.Tokens.y:
        gravity_direction = Gf.Vec3f(0.0, -1.0, 0.0)
    else:
        gravity_direction = Gf.Vec3f(0.0, 0.0, -1.0)

    scene.CreateGravityDirectionAttr().Set(gravity_direction)
    meters_per_unit = UsdGeom.GetStageMetersPerUnit(stage) or 1.0
    gravity_magnitude = 9.81 / meters_per_unit
    scene.CreateGravityMagnitudeAttr().Set(gravity_magnitude)
    try:
        import carb
        carb.log_warn(f"[Physics] gravity magnitude={gravity_magnitude:.2f} units/s^2 (metersPerUnit={meters_per_unit})")
    except Exception:
        pass

    # 결정론적 오프라인 캡처: 물리 substep 레이트를 명시(렌더 fps와 독립). 프레임당 물리
    # 스텝 수를 고정해 부하와 무관하게 동일한 sim 전진을 보장한다. timeCodesPerSecond(=fps,
    # 프레임 샘플 레이트)와 분리 → 물리 60Hz 정확도 유지 + 저fps 샘플/인코딩 가능.
    try:
        from pxr import PhysxSchema
        physx_scene = PhysxSchema.PhysxSceneAPI.Apply(scene_prim)
        physx_scene.CreateTimeStepsPerSecondAttr().Set(int(time_steps_per_second))
    except Exception as e:
        try:
            import carb
            carb.log_warn(f"[Physics] timeStepsPerSecond 설정 실패(계속 진행): {e}")
        except Exception:
            pass
    return scene_prim
