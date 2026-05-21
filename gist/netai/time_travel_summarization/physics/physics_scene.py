def ensure_physics_scene(stage, scene_path: str = "/World/PhysicsScene"):
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
    return scene_prim
