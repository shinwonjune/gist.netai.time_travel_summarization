import omni.ui as ui
import omni.ui.scene as sc
from pxr import UsdGeom


class ObjectIdManipulator(sc.Manipulator):
    # 라벨을 객체 좌표에 정확히 배치 (PIL 합성 마커와 동일 3D 점). 0이 아니면 Y로 띄움.
    _LABEL_Y_OFFSET = 0.0

    def __init__(self, prim_path: str, label_text: str, **kwargs):
        super().__init__(**kwargs)
        self._prim_path = prim_path
        self._label_text = label_text
        self._stage = None
        self._prim = None
        self._transform = None
        self._last_position = None

    def bind_stage(self, stage):
        self._stage = stage
        self._prim = stage.GetPrimAtPath(self._prim_path) if stage else None

    def on_build(self):
        if not self._prim or not self._prim.IsValid():
            return

        translation = self._get_world_translation()
        self._transform = sc.Transform(
            transform=sc.Matrix44.get_translation_matrix(
                translation[0],
                translation[1] + self._LABEL_Y_OFFSET,
                translation[2],
            )
        )

        with self._transform:
            with sc.Transform(look_at=sc.Transform.LookAt.CAMERA):
                sc.Arc(radius=30, color=0xFFFFFFFF, thickness=40)
                sc.Label(
                    self._label_text,
                    color=0xFF000000,
                    size=30,
                    alignment=ui.Alignment.CENTER,
                )

        self._last_position = tuple(translation)

    def update_position(self):
        if not self._prim or not self._prim.IsValid() or not self._transform:
            return

        translation = self._get_world_translation()
        current_position = tuple(translation)
        if self._last_position == current_position:
            return

        self._transform.transform = sc.Matrix44.get_translation_matrix(
            translation[0],
            translation[1] + self._LABEL_Y_OFFSET,
            translation[2],
        )
        self._last_position = current_position

    def _get_world_translation(self):
        xform_cache = UsdGeom.XformCache()
        return xform_cache.GetLocalToWorldTransform(self._prim).ExtractTranslation()


class PrimLabelRegistry:
    def __init__(self):
        self._manipulators = []

    def clear(self):
        for manipulator in self._manipulators:
            if hasattr(manipulator, "invalidate"):
                manipulator.invalidate()
        self._manipulators = []

    def build_for_parent(self, parent_prim):
        self.clear()
        if not parent_prim or not parent_prim.IsValid():
            return []

        stage = parent_prim.GetStage()
        for prim in parent_prim.GetChildren():
            label_id = self._extract_id(prim.GetName())
            if not label_id:
                continue

            manipulator = ObjectIdManipulator(prim_path=str(prim.GetPath()), label_text=label_id)
            manipulator.bind_stage(stage)
            self._manipulators.append(manipulator)

        return self._manipulators

    def update_positions(self):
        for manipulator in self._manipulators:
            manipulator.update_position()

    @staticmethod
    def _extract_id(prim_name: str) -> str | None:
        if len(prim_name) < 3:
            return None

        suffix = prim_name[-3:]
        if not suffix.isdigit():
            return None

        return str(int(suffix))


class TimeDisplayOverlay:
    def __init__(self, viewport_window, frame_name: str):
        self._viewport_window = viewport_window
        self._frame_name = frame_name
        self._frame = None
        self._label = None

    def build(self, visible: bool = True):
        if not self._viewport_window:
            return

        with self._viewport_window.get_frame(self._frame_name):
            self._frame = ui.Frame(separate_window=False)
            with self._frame:
                with ui.HStack():
                    ui.Spacer()
                    with ui.VStack(width=0):
                        ui.Spacer()
                        with ui.ZStack(width=0, height=40):
                            ui.Rectangle(
                                style={
                                    "background_color": 0xFF1A1A1A,
                                    "border_color": 0xFF00FF00,
                                    "border_width": 2,
                                    "border_radius": 5,
                                }
                            )
                            with ui.VStack(height=20):
                                ui.Spacer()
                                with ui.HStack():
                                    ui.Spacer(width=5)
                                    self._label = ui.Label(
                                        "00:00:00",
                                        style={
                                            "font_size": 24,
                                            "color": 0xFFFFFFFF,
                                            "font_weight": "bold",
                                        },
                                    )
                                    ui.Spacer(width=5)
                                ui.Spacer()
                        ui.Spacer(height=0)

        self.set_visible(visible)

    def set_visible(self, visible: bool):
        if self._frame:
            self._frame.visible = visible

    def set_time_text(self, text: str):
        if self._label:
            self._label.text = text

    def clear(self):
        if self._frame:
            self._frame.clear()
            self._frame = None
        self._label = None
