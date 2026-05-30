import omni.kit.app
import omni.ui.scene as sc
import omni.usd
import carb

from .components import PrimLabelRegistry, TimeDisplayOverlay


class ViewOverlay:
    """Manage viewport overlay, label scene lifecycle, and time HUD separately."""

    def __init__(self, viewport_window, ext_id, core):
        self._viewport_window = viewport_window
        self._ext_id = ext_id
        self._core = core
        self._usd_context = omni.usd.get_context()
        self._scene_view = None
        self._registry = PrimLabelRegistry()
        self._time_overlay = TimeDisplayOverlay(viewport_window, "timetravel_time_overlay")
        self._stage_event_sub = None
        self._update_sub = None
        self._visible = True
        self._labels_visible = True
        self._time_visible = True

        self._stage_event_sub = self._usd_context.get_stage_event_stream().create_subscription_to_pop(
            self._on_stage_event, name="ViewOverlayStageEvent"
        )

        self._time_overlay.build(visible=self._visible)
        self._ensure_update_subscription()

        if self._usd_context.get_stage():
            self._build_scene_for_stage()

    def set_visible(self, visible: bool):
        self._visible = visible
        self._labels_visible = visible
        self._time_visible = visible
        self._set_scene_visible(visible)
        self._time_overlay.set_visible(visible)

    def set_labels_visible(self, visible: bool):
        self._labels_visible = visible
        self._set_scene_visible(visible)

    def set_time_visible(self, visible: bool):
        self._time_visible = visible
        self._time_overlay.set_visible(visible)

    def is_visible(self) -> bool:
        return self._visible

    def shutdown(self):
        self._stage_event_sub = None
        self._update_sub = None
        self._cleanup_scene()
        self._time_overlay.clear()

    def _on_stage_event(self, event):
        if event.type == int(omni.usd.StageEventType.OPENED):
            self._build_scene_for_stage()
        elif event.type == int(omni.usd.StageEventType.CLOSED):
            self._cleanup_scene()

    def _cleanup_scene(self):
        self._registry.clear()

        if self._scene_view:
            self._scene_view.visible = False
            if self._viewport_window and hasattr(self._viewport_window, "viewport_api"):
                self._viewport_window.viewport_api.remove_scene_view(self._scene_view)
            self._scene_view = None

    def _build_scene_for_stage(self):
        if self._scene_view:
            self._cleanup_scene()

        stage = self._usd_context.get_stage()
        if not stage:
            carb.log_error("[ViewOverlay] Cannot get stage")
            return

        parent_prim = stage.GetPrimAtPath("/World/TimeTravel_Objects")
        if not parent_prim.IsValid():
            carb.log_warn("[ViewOverlay] '/World/TimeTravel_Objects' prim not found")
            return

        with self._viewport_window.get_frame(self._ext_id):
            self._scene_view = sc.SceneView()
            with self._scene_view.scene:
                self._registry.build_for_parent(parent_prim)

            self._viewport_window.viewport_api.add_scene_view(self._scene_view)

        self._set_scene_visible(self._labels_visible)
        self._ensure_update_subscription()

    def _ensure_update_subscription(self):
        if not self._update_sub:
            self._update_sub = omni.kit.app.get_app().get_update_event_stream().create_subscription_to_pop(
                self._on_update, name="ViewOverlayFrameUpdate"
            )

    def _ensure_scene_current(self):
        stage = self._usd_context.get_stage()
        if not stage:
            return
        parent_prim = stage.GetPrimAtPath("/World/TimeTravel_Objects")
        if not parent_prim.IsValid():
            return
        if not self._scene_view or not self._registry.matches_parent(parent_prim):
            self._build_scene_for_stage()

    def _on_update(self, _event):
        if self._labels_visible:
            self._ensure_scene_current()
            self._registry.update_positions()

        if self._time_visible:
            try:
                sim_time = self._core.get_simulation_time()
                text = sim_time.strftime("%H:%M:%S") if sim_time else "--:--:--"
                self._time_overlay.set_time_text(text)
            except Exception as error:
                carb.log_error(f"[ViewOverlay] Error updating time: {error}")

    def _set_scene_visible(self, visible: bool):
        if self._scene_view:
            self._scene_view.visible = visible
