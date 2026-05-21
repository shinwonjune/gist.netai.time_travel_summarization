_WALL_NAMES = ("Floor", "North", "South", "East", "West")


def _ensure_api(schema_api, prim):
    if not schema_api(prim):
        return schema_api.Apply(prim)
    return schema_api(prim)


def _set_cube_transform(cube_prim, translate, scale) -> None:
    from pxr import UsdGeom

    xformable = UsdGeom.Xformable(cube_prim)
    translate_op = None
    scale_op = None
    for op in xformable.GetOrderedXformOps():
        if op.GetOpType() == UsdGeom.XformOp.TypeTranslate and translate_op is None:
            translate_op = op
        elif op.GetOpType() == UsdGeom.XformOp.TypeScale and scale_op is None:
            scale_op = op

    if translate_op is None:
        translate_op = xformable.AddTranslateOp()
    if scale_op is None:
        scale_op = xformable.AddScaleOp()

    translate_op.Set(translate)
    scale_op.Set(scale)
    xformable.SetXformOpOrder([translate_op, scale_op])


def _wall_specs(center, size, thickness, is_y_up):
    cx, cy, cz = center
    width, height, depth = size
    half_w = width / 2.0
    half_h = height / 2.0
    half_d = depth / 2.0
    half_t = thickness / 2.0

    if is_y_up:
        return {
            "Floor": ((cx, cy - half_h - half_t, cz), (width, thickness, depth)),
            "North": ((cx, cy, cz + half_d + half_t), (width, height, thickness)),
            "South": ((cx, cy, cz - half_d - half_t), (width, height, thickness)),
            "East": ((cx + half_w + half_t, cy, cz), (thickness, height, depth)),
            "West": ((cx - half_w - half_t, cy, cz), (thickness, height, depth)),
        }

    return {
        "Floor": ((cx, cy, cz - half_h - half_t), (width, depth, thickness)),
        "North": ((cx, cy + half_d + half_t, cz), (width, thickness, height)),
        "South": ((cx, cy - half_d - half_t, cz), (width, thickness, height)),
        "East": ((cx + half_w + half_t, cy, cz), (thickness, depth, height)),
        "West": ((cx - half_w - half_t, cy, cz), (thickness, depth, height)),
    }


def create_bounding_box(
    stage,
    center: tuple = (0.0, 0.0, 0.0),
    size: tuple = (5.0, 3.0, 5.0),
    thickness: float = 0.1,
    root_path: str = "/World/PhysicsWalls",
) -> None:
    """Create or update visible static collider walls around the physics area."""
    from pxr import Gf, UsdGeom, UsdPhysics

    root = stage.GetPrimAtPath(root_path)
    if not root or not root.IsValid():
        root = stage.DefinePrim(root_path, "Xform")

    is_y_up = UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y
    specs = _wall_specs(center, size, thickness, is_y_up)
    # 시각 식별을 위해 색·opacity 강화. Floor는 더 진한 회색, 측면 4 walls는 빨강 반투명
    wall_styles = {
        "Floor": (Gf.Vec3f(0.35, 0.35, 0.35), 0.85),     # 진한 회색, 거의 불투명
        "North": (Gf.Vec3f(0.9, 0.25, 0.25), 0.55),
        "South": (Gf.Vec3f(0.9, 0.25, 0.25), 0.55),
        "East":  (Gf.Vec3f(0.25, 0.55, 0.9), 0.55),
        "West":  (Gf.Vec3f(0.25, 0.55, 0.9), 0.55),
    }

    for name in _WALL_NAMES:
        wall_path = f"{root_path}/{name}"
        cube = UsdGeom.Cube.Define(stage, wall_path)
        cube.CreateSizeAttr().Set(1.0)
        wall_prim = cube.GetPrim()
        translate, scale = specs[name]
        _set_cube_transform(wall_prim, Gf.Vec3d(*translate), Gf.Vec3f(*scale))
        color_vec, opacity_val = wall_styles[name]
        cube.CreateDisplayColorAttr().Set([color_vec])
        cube.CreateDisplayOpacityAttr().Set([opacity_val])
        UsdGeom.Imageable(wall_prim).MakeVisible()
        _ensure_api(UsdPhysics.CollisionAPI, wall_prim)
        # wall은 static collider (RigidBodyAPI 없음). PhysxContactReportAPI는 RigidBody 쪽(우주인)에 붙어 있어 wall 충돌도 자동 수신됨.
