from typing import Dict, List, Optional, Tuple

import carb
import omni.usd
from pxr import Gf, UsdGeom


class StageObjectController:
    def __init__(self, camera_path: str = "/World/summarization_camera"):
        self._usd_context = omni.usd.get_context()
        self._camera_path = camera_path
        self._camera_start_position = (332.2, 1602.28, -2113)
        self._camera_rotation = (-0.012842645866697922, 89.99956531948999, 88.06146995128398)
        self._camera_focal_length = 18.147562
        self._camera_focus_distance = 400.0
        self._camera_clipping_range = (1.0, 10000000.0)
        self._camera_height = 1602.28

    def get_stage(self):
        return self._usd_context.get_stage()

    def ensure_summarization_camera(self):
        stage = self.get_stage()
        if not stage:
            return

        camera_prim = stage.GetPrimAtPath(self._camera_path)
        if camera_prim.IsValid():
            return

        from pxr import Sdf

        camera_prim = UsdGeom.Camera.Define(stage, self._camera_path)
        prim = camera_prim.GetPrim()
        prim.ApplyAPI("OmniRtxCameraAutoExposureAPI_1")
        prim.ApplyAPI("OmniRtxCameraExposureAPI_1")
        camera_prim.GetClippingRangeAttr().Set(Gf.Vec2f(*self._camera_clipping_range))
        camera_prim.GetFocalLengthAttr().Set(self._camera_focal_length)
        camera_prim.GetFocusDistanceAttr().Set(self._camera_focus_distance)
        prim.CreateAttribute("exposure:responsivity", Sdf.ValueTypeNames.Float).Set(1.1026709)
        prim.CreateAttribute("exposure:time", Sdf.ValueTypeNames.Float).Set(0.02)

        xformable = UsdGeom.Xformable(camera_prim)
        translate_op = xformable.AddTranslateOp()
        rotate_op = xformable.AddRotateYXZOp()
        scale_op = xformable.AddScaleOp()
        translate_op.Set(Gf.Vec3d(*self._camera_start_position))
        rotate_op.Set(Gf.Vec3f(*self._camera_rotation))
        scale_op.Set(Gf.Vec3f(1.0, 1.0, 1.0))
        xformable.SetXformOpOrder([translate_op, rotate_op, scale_op])
        camera_prim.GetVisibilityAttr().Set("invisible")

    def update_stage_objects(
        self,
        prim_map: Dict[str, str],
        data: Dict[str, Tuple[float, float, float]],
        visibility: Optional[Dict[str, bool]] = None,
    ):
        """prim 위치를 data로 갱신하고, visibility가 주어지면 objid별 보임/숨김을 토글한다.

        visibility[objid]=False면 그 prim을 USD invisible로 숨긴다(삭제가 아니라 토글이라
        시간 스크럽 왕복 시 다시 보임). 트랙 시작 전/종료 후 객체를 화면에서 지워
        죽은 트랙 잔상이 가짜 충돌로 읽히는 것을 막는다. data에 없는 objid는 위치를
        건드리지 않아(기존 hold 동작) 트랙 내부 결손 구간은 마지막 좌표를 유지한다.
        """
        stage = self.get_stage()
        if not stage:
            return

        for objid, prim_path in prim_map.items():
            prim = stage.GetPrimAtPath(prim_path)
            if not prim or not prim.IsValid():
                continue

            if visibility is not None:
                is_visible = visibility.get(objid)
                if is_visible is not None:
                    imageable = UsdGeom.Imageable(prim)
                    if is_visible:
                        imageable.MakeVisible()
                    else:
                        imageable.MakeInvisible()

            if not data or objid not in data:
                continue

            xformable = UsdGeom.Xformable(prim)
            translate_op = None
            for op in xformable.GetOrderedXformOps():
                if op.GetOpType() == UsdGeom.XformOp.TypeTranslate:
                    translate_op = op
                    break

            if not translate_op:
                translate_op = xformable.AddTranslateOp()

            x, y, z = data[objid]
            translate_op.Set(Gf.Vec3d(x, y, z))

    def get_world_positions(self, prim_map: Dict[str, str]) -> Dict[str, Tuple[float, float, float]]:
        stage = self.get_stage()
        if not stage:
            return {}

        positions = {}
        xform_cache = UsdGeom.XformCache(0)
        for objid, prim_path in prim_map.items():
            prim = stage.GetPrimAtPath(prim_path)
            if not prim or not prim.IsValid():
                continue
            # 숨긴 객체(트랙 시작 전/종료 후)는 오버레이 라벨에서도 빼야, 재연기가 지운
            # 죽은 트랙에 라벨만 남아 VLM이 가짜 충돌로 읽는 일을 막는다.
            if UsdGeom.Imageable(prim).ComputeVisibility() == UsdGeom.Tokens.invisible:
                continue
            try:
                translation = xform_cache.GetLocalToWorldTransform(prim).ExtractTranslation()
            except Exception as exc:
                carb.log_warn(f"[TimeTravel] Failed to read world position for {objid}: {exc}")
                continue
            positions[objid] = (float(translation[0]), float(translation[1]), float(translation[2]))
        return positions

    def clear_timetravel_objects(self):
        stage = self.get_stage()
        if not stage:
            return

        parent_prim = stage.GetPrimAtPath("/World/TimeTravel_Objects")
        if parent_prim and parent_prim.IsValid():
            for child in parent_prim.GetChildren():
                stage.RemovePrim(child.GetPath())

    def create_astronaut_prim(self, index: int, astronaut_usd: str) -> str:
        stage = self.get_stage()
        if not stage:
            carb.log_warn("[TimeTravel] Cannot create astronaut prim: no active USD stage")
            return ""
        if not astronaut_usd:
            carb.log_error("[TimeTravel] Cannot create astronaut prim: astronaut_usd is empty")
            return ""

        parent_path = "/World/TimeTravel_Objects"
        parent_prim = stage.GetPrimAtPath(parent_path)
        if not parent_prim or not parent_prim.IsValid():
            parent_prim = stage.DefinePrim(parent_path, "Xform")
        if not parent_prim or not parent_prim.IsValid():
            carb.log_error(f"[TimeTravel] Cannot create astronaut parent prim: {parent_path}")
            return ""
        UsdGeom.Imageable(parent_prim).MakeVisible()

        prim_path = f"{parent_path}/Astronaut{index:03d}"
        existing = stage.GetPrimAtPath(prim_path)
        if existing and existing.IsValid():
            stage.RemovePrim(prim_path)
        prim = stage.DefinePrim(prim_path, "Xform")
        if not prim or not prim.IsValid():
            carb.log_error(f"[TimeTravel] Cannot define astronaut prim: {prim_path}")
            return ""
        UsdGeom.Imageable(prim).MakeVisible()

        from pxr import Sdf

        prim.GetReferences().AddReference(assetPath=astronaut_usd, primPath=Sdf.Path("/Root"))
        xformable = UsdGeom.Xformable(prim)
        translate_op = xformable.AddTranslateOp()
        rotate_xyz_op = xformable.AddRotateXYZOp()
        scale_op = xformable.AddScaleOp()
        translate_op.Set(Gf.Vec3d(0, 0, 0))
        rotate_xyz_op.Set(Gf.Vec3f(-90.0, 0.0, 0.0))
        scale_op.Set(Gf.Vec3f(1.0, 1.0, 1.0))
        return prim_path

    def hide_all_cameras(self):
        stage = self.get_stage()
        if not stage:
            return

        for prim in stage.Traverse():
            if prim.IsA(UsdGeom.Camera):
                UsdGeom.Imageable(prim).MakeInvisible()

    def set_visual_complexity(
        self,
        level: str,
        visibility_groups: Dict[str, List[str]],
        complexity_levels: Dict[str, List[str]],
    ) -> bool:
        """level별로 group의 prim들을 visible/invisible 토글."""
        import omni.usd
        from pxr import UsdGeom

        stage = omni.usd.get_context().get_stage()
        if not stage:
            carb.log_warn("[Complexity] no stage")
            return False

        if level not in complexity_levels:
            carb.log_warn(f"[Complexity] unknown level: {level}")
            return False

        invisible_groups = set(complexity_levels[level])
        for group_name, prim_paths in visibility_groups.items():
            should_hide = group_name in invisible_groups
            for path in prim_paths:
                prim = stage.GetPrimAtPath(path)
                if not prim or not prim.IsValid():
                    carb.log_warn(f"[Complexity] prim not found: {path}")
                    continue
                imageable = UsdGeom.Imageable(prim)
                if should_hide:
                    imageable.MakeInvisible()
                else:
                    imageable.MakeVisible()
        carb.log_warn(f"[Complexity] level={level}, hidden_groups={invisible_groups}")
        return True

    def move_camera_to_event(self, event_position: Optional[Tuple[float, float, float]]):
        if not event_position:
            return

        stage = self.get_stage()
        if not stage:
            return

        self.ensure_summarization_camera()
        camera_prim = stage.GetPrimAtPath(self._camera_path)
        if not camera_prim or not camera_prim.IsValid():
            carb.log_warn(f"[TimeTravel] Camera not found: {self._camera_path}")
            return

        xformable = UsdGeom.Xformable(camera_prim)
        translate_op = None
        for op in xformable.GetOrderedXformOps():
            if op.GetOpType() == UsdGeom.XformOp.TypeTranslate:
                translate_op = op
                break

        if not translate_op:
            translate_op = xformable.AddTranslateOp()

        obj_x, _obj_y, obj_z = event_position
        translate_op.Set(Gf.Vec3d(obj_x, self._camera_height, obj_z))
