_PROXY_NAME = "__phys_proxy__"
_MATERIAL_NAME = "__phys_material__"

# 콜라이더-메시 정합화 이력:
# - v2 규약(2026-08-05): bbox 유도 반지름이 시각 몸통보다 두꺼워(r=35.86, 접촉거리 71.7)
#   접촉 순간 화면에 틈이 보였다 → 너비 −8(반지름 −4)을 육안으로 확정(접촉거리 63.7).
# - regime3(2026-08-08): 그 "두꺼움"의 원인이 에셋 앞면의 튀어나온 끈으로 확인됐다 —
#   bbox가 몸통이 아니라 끈 끝에 접해 있었고, −8은 사실상 끈 보정이었다. 끈 제거본
#   (No_tie_Astronaut.usd)에서는 shrink 없이 0.45×min_dim만으로 접촉이 화면과 일치
#   (사용자 육안 확정). shrink는 폐기하고 상수는 이력 추적용으로만 남긴다.
# 주의: 반지름 규약 변경은 생성 데이터의 접촉 규약을 바꾸므로(train ≠ infer 금지)
# **재생성 + 재학습과 묶어서만** 의미가 있다. regime2 데이터(prod-20260806-v2)는
# 옛 규약(shrink 8, 월드 AABB)으로 생성된 것이다.
_PROXY_WIDTH_SHRINK = 8.0  # regime2까지 사용, regime3에서 폐기(미적용)

# rest 자세 치수 캐시 (regime3 2차 수정, 2026-08-08) — prim 경로 -> (dims, up_idx).
# 1차 수정(지역 bbox)은 실패했다: 5에피소드 진단에서 지역 치수가 84.9→131.1까지
# 변했는데, 131은 정적 단면(84.9x79.7)의 어떤 회전 AABB 상한(116.4)도 넘는 값이라
# **걷기 애니메이션의 자세(팔다리 벌림)가 메시 자체를 변형**시킨다는 증거다 — 지역
# bbox도 "그 순간 자세"를 읽는다. 반지름은 자세·회전 모두와 무관해야 하므로,
# **세션에서 처음 잰 치수(= 씬 로드 직후 rest 자세)를 캐시**하고 에피소드 경계
# 재생성 때 재사용한다. 근거: 진단에서 첫 생성 회차는 8/8건이 정확히 rest 값
# (84.94/35.86)이었다 — 애니메이션이 돌기 전이라 항상 rest다.
_REST_DIMS: dict = {}


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


# 탄성(restitution) 규약 이력:
# - regime2까지 0.8 (prod-20260806-v2가 이 값으로 생성됨).
# - regime3(2026-08-16): 0.9 — 충돌 반동을 키워 사건이 화면에서 더 분명해지게(사용자
#   결정). 0은 완전 비탄성, 1은 무손실이며 1 초과는 충돌마다 에너지가 늘어 발산한다.
# 주의: 반동이 커지면 충돌 후 궤적·재충돌 빈도·사건 간격 분포가 달라지므로 이 값은
# **데이터 규약**이다. regime2 데이터와 물리 조건이 다르다는 점을 비교 시 감안할 것.
# 실효 반동은 두 재질의 조합으로 정해지는데, 전 객체가 같은 값을 쓰므로 그대로 적용된다.
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
    restitution: float = 1,
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
    sx = sy = sz = 1.0  # 아래 진단 try가 실패해도 지역 치수 보정이 동작하게 기본값
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
        # log_warn 레벨 — 프록시 로그와 짝을 이뤄 job.log에 남아야 한다. 콜라이더
        # 반지름이 에피소드마다 달라지는 원인이 "에셋 크기 차이(scale)"인지 "스폰
        # 각도에 따른 월드 AABB 변화(rot_angle_deg)"인지는 이 두 값을 나란히 봐야만
        # 갈린다 — bbox_size만으로는 둘을 구분할 수 없다(실측 2026-08-07: 한 잡의
        # 75 에피소드에서 collision_distance가 51개 값으로 흩어졌으나 원인 미확정).
        carb.log_warn(
            f"[Physics] prim transform diag for {target_prim.GetPath()}: "
            f"rot_axis=({rot_axis[0]:.3f}, {rot_axis[1]:.3f}, {rot_axis[2]:.3f}) "
            f"rot_angle_deg={rot_angle_deg:.2f} scale=({sx:.3f}, {sy:.3f}, {sz:.3f})"
        )
    except Exception as _diag_err:
        carb.log_warn(f"[Physics] prim transform diag failed for {target_prim.GetPath()}: {_diag_err!r}")
    bbox_valid = not range_.IsEmpty() and _is_finite_vec(bb_min) and _is_finite_vec(bb_max)

    proxy_radius = radius_units
    proxy_height = height_units
    local_dims = None  # 지역 bbox 치수(원기둥/캡슐 경로에서 채움) — 로그 스탬프용
    up_idx = None  # local_dims 안에서 높이 축의 인덱스 — 로그·진단용(원기둥/캡슐 경로에서 채움)
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
        # 월드 상축(월드의 '위' 방향 단위벡터)을 프림 "지역" 프레임으로 역변환한다
        # — "월드의 위가 프림 자신의 좌표계에서는 어느 방향인가"를 얻는 계산.
        # 왜 필요한가: 에셋은 눕혀 authored돼 있고 스폰이 프림에 rotateXYZ(-90,0,0)을
        # 얹어 세우므로(stage_object_controller.create_astronaut_prim), 지역 프레임의
        # 높이 축은 월드 상축 인덱스(y-up이면 1)와 다르다(실측: 지역 z). 인덱스를
        # 하드코딩하지 않고 역변환으로 고르므로 에셋 authored 자세가 바뀌어도 자동 추종.
        local_up = inv_rotation.TransformDir(world_up)
        # 역변환된 상축 벡터에서 절대값이 가장 큰 성분의 인덱스 = 지역 높이 축.
        # 걷기 요(yaw)는 월드 상축을 중심으로 도는 회전이라 local_up을 바꾸지
        # 않는다 — 진단②③ 전 기록에서 (0,0,±1) 상수 실측(H4 tilt 기각 근거).
        abs_components = [abs(local_up[0]), abs(local_up[1]), abs(local_up[2])]
        longest_idx = abs_components.index(max(abs_components))
        axis_name = ["X", "Y", "Z"][longest_idx]

        sizes = [abs(bb_size[0]), abs(bb_size[1]), abs(bb_size[2])]
        world_up_idx = 1 if is_y_up else 2
        height_scale = 0.45 if shape == "cylinder" else 0.9
        # --- 치수는 지역(untransformed) bbox에서 유도 (regime3, 2026-08-08) ---- #
        # 월드 AABB는 축 정렬 상자라 객체가 돌면 커진다(45° 정사각형 → 대각선).
        # 프록시는 에피소드 경계마다 재생성되는데 그 시점의 걷던 요(yaw)가 남아 있어,
        # 반지름이 스폰 각도 추첨에 좌우됐다(실측: 2r 63.7~75.3으로 51개 값,
        # 5에피소드 진단으로 회전 이월 확정 — physics일지 #16). 지역 bbox는 월드
        # 회전이 계산에 아예 들어가지 않으므로 반지름이 에셋 고유값으로 고정된다.
        # 배치(bb_center·bb_min)는 그대로 월드 기준 — 몸이 지금 있는 곳에 세운다.
        # 높이 축은 월드 상축이 아니라 "지역 프레임에서 상축에 대응하는 축"
        # (longest_idx = local_up 최대 성분) — 이 에셋은 세우느라 90° 회전이
        # authored돼 있어 지역 높이 축이 월드 상축 인덱스와 다를 수 있다.
        local_dims = None
        cache_key = str(target_prim.GetPath())
        cached = _REST_DIMS.get(cache_key)
        if cached is not None:
            # 에피소드 경계 재생성 — 세션 첫 생성(rest 자세)의 치수를 재사용한다.
            # 지금 다시 재면 걷던 요·걸음 자세가 치수에 새어 든다(_REST_DIMS 주석).
            dims, up_idx = list(cached[0]), cached[1]
            local_dims = tuple(round(d, 2) for d in dims)
        else:
            try:
                local_rng = bbox_cache.ComputeUntransformedBound(target_prim).ComputeAlignedRange()
                l_size = local_rng.GetMax() - local_rng.GetMin()
                axis_scale = (sx, sy, sz)  # 배치 스케일 보정(진단 실측 전부 1.0)
                if any(abs(s - 1.0) > 0.01 for s in axis_scale):
                    carb.log_warn(
                        f"[Physics] proxy dims: non-unit scale {axis_scale} for "
                        f"{target_prim.GetPath()} — 지역 치수에 스케일을 곱해 보정")
                dims = [abs(l_size[i]) * axis_scale[i] for i in range(3)]
                up_idx = longest_idx
                local_dims = tuple(round(d, 2) for d in dims)
                _REST_DIMS[cache_key] = (tuple(dims), up_idx)
            except Exception as dim_err:
                carb.log_warn(f"[Physics] local-bbox dims failed ({dim_err!r}) — 월드 AABB 폴백"
                              f" (회전 의존 반지름으로 퇴행하므로 regime 스탬프 확인 필요)")
                dims = sizes
                up_idx = world_up_idx
        # 진단: 높이 축 선택 검증 — dims[up_idx]는 세 치수 중 최대(키 ~206)여야
        # 한다. 아니면 상축 매핑이 어긋나 반지름이 몸통 폭이 아닌 키에서 유도되는
        # 사고 경로이므로 즉시 경고를 남긴다(job.log로 사후 검증 가능).
        if abs(dims[up_idx] - max(dims)) > 1.0:
            carb.log_warn(
                f"[Physics] proxy dims: up-axis pick suspicious for {target_prim.GetPath()} — "
                f"dims={tuple(round(d, 2) for d in dims)} up_idx={up_idx} "
                f"local_up=({local_up[0]:.4f}, {local_up[1]:.4f}, {local_up[2]:.4f}) "
                f"(height axis is not the largest dim)")
        proxy_height = max(dims[up_idx] * height_scale, 0.1 * m_to_units)
        other_dims = [dims[i] for i in range(3) if i != up_idx]
        # 반지름 계수 0.4542 ≈ 30.0 / 66.04(끈 제거 에셋의 지역 좁은 변) — r=30.0,
        # 접촉거리 2r=60.0으로 규약 확정(사용자, 2026-08-11). 고정 상수 대신 계수를
        # 유지하는 이유: 에셋 치수가 바뀌면 반지름이 자동 추종하고 job.log의
        # radius 스탬프로 사후 검증할 수 있다(끈 제거가 실사례).
        proxy_radius = max(min(other_dims) * 0.4542, 0.05 * m_to_units)
        if shape == "cylinder":
            world_target = [bb_center[0], bb_center[1], bb_center[2]]
            world_target[world_up_idx] = bb_min[world_up_idx] + proxy_height / 2.0
            local_translate = inv_xform.Transform(Gf.Vec3d(world_target[0], world_target[1], world_target[2]))
        else:
            local_translate = inv_xform.Transform(bb_center)

    # log_warn 레벨 — 헤드리스 잡의 job.log(stdout)에 남아야 "이 데이터가 어느 접촉
    # 규약(width_shrink)으로 생성됐는지"를 사후에 판정할 수 있다(데이터 계보 추적).
    # log_info는 Kit 내부 로그로만 가고 job.log엔 안 실린다(실측 2026-08-06).
    carb.log_warn(
        f"[Physics] proxy for {target_prim.GetPath()}: "
        f"prim_world_pos={tuple(prim_world_pos)} "
        f"bbox_size={tuple(bb_size)} bbox_center={tuple(bb_center)} "
        f"local_translate(in-prim-local)={tuple(local_translate)} "
        f"world_up_in_prim_local={tuple(local_up)} "
        f"shape={shape} axis={axis_name} height={proxy_height:.2f} radius={proxy_radius:.2f} "
        f"local_dims={local_dims} up_idx={up_idx} rest_cached={bool(_REST_DIMS)} "
        f"(stage units, regime=r3-restcache shrink=none coef=0.4542)"
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
