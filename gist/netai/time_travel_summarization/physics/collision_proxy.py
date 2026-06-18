_PROXY_NAME = "__phys_proxy__"
_MATERIAL_NAME = "__phys_material__"


def _ensure_api(schema_api, prim):
    if not schema_api(prim):
        return schema_api.Apply(prim)
    return schema_api(prim)


def _set_proxy_transform(proxy_prim, local_translate) -> None:
    from pxr import Gf, UsdGeom

    xformable = UsdGeom.Xformable(proxy_prim)
    translate_op = None
    for op in xformable.GetOrderedXformOps():
        if op.GetOpType() == UsdGeom.XformOp.TypeTranslate:
            translate_op = op
            break
    if translate_op is None:
        translate_op = xformable.AddTranslateOp()
    translate_op.Set(Gf.Vec3d(local_translate))
    xformable.SetXformOpOrder([translate_op])


def _default_proxy_translate(stage, shape: str, radius: float, height: float):
    from pxr import Gf, UsdGeom

    up_axis = UsdGeom.GetStageUpAxis(stage)
    if shape == "sphere":
        offset = radius
    elif shape == "cylinder":
        offset = height / 2.0
    else:
        offset = height / 2.0 + radius
    if up_axis == UsdGeom.Tokens.y:
        return Gf.Vec3d(0.0, offset, 0.0)
    return Gf.Vec3d(0.0, 0.0, offset)


def _is_finite_vec(vec) -> bool:
    return all(value == value and value not in (float("inf"), float("-inf")) for value in vec)


def _bind_physics_material(stage, target_prim, proxy_prim, restitution: float) -> None:
    from pxr import UsdPhysics, UsdShade

    material_path = target_prim.GetPath().AppendChild(_MATERIAL_NAME)
    material = UsdShade.Material.Define(stage, material_path)
    material_api = _ensure_api(UsdPhysics.MaterialAPI, material.GetPrim())
    material_api.CreateRestitutionAttr().Set(restitution)
    UsdShade.MaterialBindingAPI.Apply(proxy_prim).Bind(material)


def wrap_with_collision_proxy(
    stage,
    target_prim,
    shape: str = "cylinder",
    radius: float = 0.4,
    height: float = 1.7,
    mass: float = 80.0,
    restitution: float = 0.8,
    visible: bool = False,
):
    """Create an invisible child collider and make the target prim a rigid body."""
    import carb
    from pxr import Gf, UsdGeom, UsdPhysics

    meters_per_unit = UsdGeom.GetStageMetersPerUnit(stage) or 1.0
    m_to_units = 1.0 / meters_per_unit
    radius_units = radius * m_to_units
    height_units = height * m_to_units

    bbox_cache = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_, UsdGeom.Tokens.render], useExtentsHint=True)
    bbox = bbox_cache.ComputeWorldBound(target_prim)
    range_ = bbox.ComputeAlignedRange()
    bb_min = range_.GetMin()
    bb_max = range_.GetMax()
    bb_size = bb_max - bb_min
    bb_center = (bb_min + bb_max) * 0.5
    xform_cache = UsdGeom.XformCache(0)
    prim_world_xform = xform_cache.GetLocalToWorldTransform(target_prim)
    prim_world_pos = prim_world_xform.ExtractTranslation()
    try:
        rotation = prim_world_xform.ExtractRotation()
        rot_axis = rotation.GetAxis()
        rot_angle_deg = rotation.GetAngle()
        col0 = prim_world_xform.GetRow(0)
        col1 = prim_world_xform.GetRow(1)
        col2 = prim_world_xform.GetRow(2)
        sx = (col0[0] ** 2 + col0[1] ** 2 + col0[2] ** 2) ** 0.5
        sy = (col1[0] ** 2 + col1[1] ** 2 + col1[2] ** 2) ** 0.5
        sz = (col2[0] ** 2 + col2[1] ** 2 + col2[2] ** 2) ** 0.5
        carb.log_info(
            f"[Physics] prim transform diag for {target_prim.GetPath()}: "
            f"rot_axis=({rot_axis[0]:.3f}, {rot_axis[1]:.3f}, {rot_axis[2]:.3f}) "
            f"rot_angle_deg={rot_angle_deg:.2f} scale=({sx:.3f}, {sy:.3f}, {sz:.3f})"
        )
    except Exception as _diag_err:
        carb.log_info(f"[Physics] prim transform diag failed for {target_prim.GetPath()}: {_diag_err!r}")
    bbox_valid = not range_.IsEmpty() and _is_finite_vec(bb_min) and _is_finite_vec(bb_max)

    proxy_radius = radius_units
    proxy_height = height_units
    local_translate = _default_proxy_translate(stage, shape, radius_units, height_units)
    up_axis = UsdGeom.GetStageUpAxis(stage)
    is_y_up = up_axis == UsdGeom.Tokens.y
    world_up = Gf.Vec3d(0, 1, 0) if is_y_up else Gf.Vec3d(0, 0, 1)
    local_up = world_up
    axis_name = "Y" if is_y_up else "Z"
    if shape in ("capsule", "cylinder") and bbox_valid:
        inv_xform = prim_world_xform.GetInverse()

        rotation = prim_world_xform.ExtractRotation()
        inv_rotation = rotation.GetInverse()
        local_up = inv_rotation.TransformDir(world_up)
        abs_components = [abs(local_up[0]), abs(local_up[1]), abs(local_up[2])]
        longest_idx = abs_components.index(max(abs_components))
        axis_name = ["X", "Y", "Z"][longest_idx]

        sizes = [abs(bb_size[0]), abs(bb_size[1]), abs(bb_size[2])]
        world_up_idx = 1 if is_y_up else 2
        height_scale = 0.45 if shape == "cylinder" else 0.9
        proxy_height = max(sizes[world_up_idx] * height_scale, 0.1 * m_to_units)
        other_dims = [sizes[i] for i in range(3) if i != world_up_idx]
        proxy_radius = max(min(other_dims) * 0.45, 0.05 * m_to_units)

        if shape == "cylinder":
            world_target = [bb_center[0], bb_center[1], bb_center[2]]
            world_target[world_up_idx] = bb_min[world_up_idx] + proxy_height / 2.0
            local_translate = inv_xform.Transform(Gf.Vec3d(world_target[0], world_target[1], world_target[2]))
        else:
            local_translate = inv_xform.Transform(bb_center)

    carb.log_info(
        f"[Physics] proxy for {target_prim.GetPath()}: "
        f"prim_world_pos={tuple(prim_world_pos)} "
        f"bbox_size={tuple(bb_size)} bbox_center={tuple(bb_center)} "
        f"local_translate(in-prim-local)={tuple(local_translate)} "
        f"world_up_in_prim_local={tuple(local_up)} "
        f"shape={shape} axis={axis_name} height={proxy_height:.2f} radius={proxy_radius:.2f} (stage units)"
    )

    if shape not in ("sphere", "capsule", "cylinder"):
        raise ValueError("shape must be 'sphere', 'capsule', or 'cylinder'")

    proxy_path = target_prim.GetPath().AppendChild(_PROXY_NAME)
    proxy_prim = stage.GetPrimAtPath(proxy_path)
    if not proxy_prim or not proxy_prim.IsValid():
        if shape == "sphere":
            proxy_geom = UsdGeom.Sphere.Define(stage, proxy_path)
            proxy_geom.CreateRadiusAttr().Set(radius_units)
            proxy_prim = proxy_geom.GetPrim()
        else:
            if shape == "cylinder":
                try:
                    proxy_geom = UsdGeom.Cylinder.Define(stage, str(proxy_path))
                    proxy_geom.CreateRadiusAttr().Set(proxy_radius)
                    proxy_geom.CreateHeightAttr().Set(proxy_height)
                    if hasattr(proxy_geom, "CreateAxisAttr"):
                        proxy_geom.CreateAxisAttr().Set(axis_name)
                except Exception as exc:
                    carb.log_warn(f"[Physics] Cylinder unsupported, falling back to Capsule: {exc}")
                    proxy_geom = UsdGeom.Capsule.Define(stage, str(proxy_path))
                    proxy_geom.CreateRadiusAttr().Set(proxy_radius)
                    proxy_geom.CreateHeightAttr().Set(proxy_height)
                    if hasattr(proxy_geom, "CreateAxisAttr"):
                        proxy_geom.CreateAxisAttr().Set(axis_name)
            else:
                proxy_geom = UsdGeom.Capsule.Define(stage, proxy_path)
                proxy_geom.CreateRadiusAttr().Set(proxy_radius)
                proxy_geom.CreateHeightAttr().Set(proxy_height)
                if hasattr(proxy_geom, "CreateAxisAttr"):
                    proxy_geom.CreateAxisAttr().Set(axis_name)
            proxy_prim = proxy_geom.GetPrim()
    elif shape == "sphere":
        proxy_geom = UsdGeom.Sphere(proxy_prim)
        proxy_geom.CreateRadiusAttr().Set(radius_units)
    else:
        if shape == "cylinder":
            try:
                proxy_geom = UsdGeom.Cylinder.Define(stage, str(proxy_path))
                proxy_geom.CreateRadiusAttr().Set(proxy_radius)
                proxy_geom.CreateHeightAttr().Set(proxy_height)
                if hasattr(proxy_geom, "CreateAxisAttr"):
                    proxy_geom.CreateAxisAttr().Set(axis_name)
            except Exception as exc:
                carb.log_warn(f"[Physics] Cylinder unsupported, falling back to Capsule: {exc}")
                proxy_geom = UsdGeom.Capsule(proxy_prim)
                proxy_geom.CreateRadiusAttr().Set(proxy_radius)
                proxy_geom.CreateHeightAttr().Set(proxy_height)
                if hasattr(proxy_geom, "CreateAxisAttr"):
                    proxy_geom.CreateAxisAttr().Set(axis_name)
        else:
            proxy_geom = UsdGeom.Capsule(proxy_prim)
            proxy_geom.CreateRadiusAttr().Set(proxy_radius)
            proxy_geom.CreateHeightAttr().Set(proxy_height)
            if hasattr(proxy_geom, "CreateAxisAttr"):
                proxy_geom.CreateAxisAttr().Set(axis_name)

    _set_proxy_transform(proxy_prim, local_translate)
    imageable = UsdGeom.Imageable(proxy_prim)
    if visible:
        proxy_geom.CreateDisplayColorAttr().Set([Gf.Vec3f(1.0, 0.2, 0.8)])
        proxy_geom.CreateDisplayOpacityAttr().Set([1.0])
        imageable.MakeVisible()
    else:
        imageable.MakeInvisible()

    _ensure_api(UsdPhysics.CollisionAPI, proxy_prim)
    rigid_body = _ensure_api(UsdPhysics.RigidBodyAPI, target_prim)
    rigid_body.CreateRigidBodyEnabledAttr().Set(True)
    mass_api = _ensure_api(UsdPhysics.MassAPI, target_prim)
    mass_api.CreateMassAttr().Set(mass)
    try:
        from pxr import PhysxSchema
        physx_api = PhysxSchema.PhysxRigidBodyAPI.Apply(target_prim)
        physx_api.CreateLinearDampingAttr().Set(0.1)
        physx_api.CreateAngularDampingAttr().Set(0.3)
        # 수평축 회전 잠금 → 바닥을 도는 에이전트가 절대 넘어지지 않음(yaw만 허용).
        # lockedRotAxis 비트마스크: X=1, Y=2, Z=4. Y-up이면 X+Z(=5), Z-up이면 X+Y(=3).
        lock_rot_mask = 5 if is_y_up else 3
        physx_api.CreateLockedRotAxisAttr().Set(lock_rot_mask)
    except Exception as exc:
        carb.log_warn(f"[Physics] PhysxRigidBodyAPI damping/lock setup failed: {exc}")
    # PhysxContactReportAPI: rigid body prim에 적용해야 contact 콜백이 동작함.
    # threshold=0 → 모든 접촉 보고. Kit 109+에서 abort 없음 확인됨.
    try:
        from pxr import PhysxSchema
        contact_api = PhysxSchema.PhysxContactReportAPI.Apply(target_prim)
        # threshold 메서드명이 Kit 버전마다 다름 → 있는 것만 0(=모두 보고) 설정.
        for fn_name in ("CreateThresholdAttr", "CreatePhysxContactReportThresholdAttr"):
            fn = getattr(contact_api, fn_name, None)
            if fn is not None:
                fn().Set(0)
                break
    except Exception as exc:
        carb.log_warn(f"[Physics] PhysxContactReportAPI setup failed: {exc}")
    _bind_physics_material(stage, target_prim, proxy_prim, restitution)
    # proxy_radius(스테이지 단위)를 함께 반환 → 호출부가 객체 간 충돌 거리 산정에 사용.
    return target_prim, proxy_radius


def unwrap(stage, target_prim) -> None:
    """Remove the generated proxy and disable physics APIs on the target prim."""
    from pxr import UsdPhysics

    proxy_path = target_prim.GetPath().AppendChild(_PROXY_NAME)
    if stage.GetPrimAtPath(proxy_path):
        stage.RemovePrim(proxy_path)

    material_path = target_prim.GetPath().AppendChild(_MATERIAL_NAME)
    if stage.GetPrimAtPath(material_path):
        stage.RemovePrim(material_path)

    if UsdPhysics.RigidBodyAPI(target_prim):
        UsdPhysics.RigidBodyAPI(target_prim).CreateRigidBodyEnabledAttr().Set(False)
    if target_prim.HasAPI(UsdPhysics.RigidBodyAPI):
        target_prim.RemoveAPI(UsdPhysics.RigidBodyAPI)
    if target_prim.HasAPI(UsdPhysics.MassAPI):
        target_prim.RemoveAPI(UsdPhysics.MassAPI)
