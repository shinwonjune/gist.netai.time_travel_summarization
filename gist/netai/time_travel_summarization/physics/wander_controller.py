import math
import random
import time


class WanderController:
    """Apply periodic horizontal velocities to rigid body prims."""

    def __init__(
        self,
        prims: list,
        speed_range: tuple = (0.5, 2.0),
        interval_s: float = 2.0,
        cooldown_after_collision_s: float = 1.0,
    ):
        self._prims = list(prims)
        self._speed_range = speed_range
        self._interval_s = max(0.1, interval_s)
        self._cooldown_after_collision_s = max(0.0, cooldown_after_collision_s)
        self._update_sub = None
        self._contact_sub = None
        self._elapsed = self._interval_s
        self._last_collision_at = {}
        self._contact_warning_logged = False

    def start(self) -> None:
        """Subscribe to Kit update events and start wandering."""
        if self._update_sub is not None:
            return

        import omni.kit.app

        app = omni.kit.app.get_app()
        self._update_sub = app.get_update_event_stream().create_subscription_to_pop(self._on_update)
        self._subscribe_contact_events()

    def stop(self) -> None:
        """Unsubscribe and zero all managed rigid body velocities."""
        self._set_all_velocities_zero()
        self._update_sub = None
        self._contact_sub = None
        self._elapsed = self._interval_s
        self._last_collision_at.clear()

    def is_active(self) -> bool:
        return self._update_sub is not None

    def _on_update(self, event) -> None:
        dt = 0.0
        if hasattr(event, "payload") and isinstance(event.payload, dict):
            dt = float(event.payload.get("dt", 0.0) or 0.0)

        self._elapsed += dt
        if self._elapsed < self._interval_s:
            return

        self._elapsed = 0.0
        now = time.monotonic()
        for prim in self._valid_prims():
            prim_path = str(prim.GetPath())
            last_collision = self._last_collision_at.get(prim_path)
            if last_collision and now - last_collision < self._cooldown_after_collision_s:
                continue
            self._set_velocity(prim, self._random_horizontal_velocity(prim.GetStage()))

    def _valid_prims(self):
        for prim in self._prims:
            if prim and prim.IsValid():
                yield prim

    def _random_horizontal_velocity(self, stage):
        from pxr import Gf, UsdGeom

        speed = random.uniform(*self._speed_range)
        angle = random.uniform(0.0, 6.283185307179586)
        a = speed * math.cos(angle)
        b = speed * math.sin(angle)

        if UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y:
            return Gf.Vec3f(a, 0.0, b)
        return Gf.Vec3f(a, b, 0.0)

    def _set_velocity(self, prim, velocity) -> None:
        from pxr import Sdf, UsdPhysics

        rigid_body = UsdPhysics.RigidBodyAPI(prim)
        if rigid_body:
            rigid_body.CreateVelocityAttr().Set(velocity)
            return

        attr = prim.GetAttribute("physics:velocity")
        if not attr:
            attr = prim.CreateAttribute("physics:velocity", Sdf.ValueTypeNames.Vector3f)
        attr.Set(velocity)

    def _set_all_velocities_zero(self) -> None:
        from pxr import Gf

        zero = Gf.Vec3f(0.0, 0.0, 0.0)
        for prim in self._valid_prims():
            self._set_velocity(prim, zero)

    def _subscribe_contact_events(self) -> None:
        try:
            import omni.physx

            interface = omni.physx.get_physx_simulation_interface()
            if hasattr(interface, "subscribe_contact_report_events"):
                self._contact_sub = interface.subscribe_contact_report_events(self._on_contact_event)
            elif hasattr(interface, "subscribe_contact_report_events_fn"):
                self._contact_sub = interface.subscribe_contact_report_events_fn(self._on_contact_event)
            else:
                self._log_contact_warning("PhysX contact event subscription API not found")
        except Exception as exc:
            self._log_contact_warning(f"PhysX contact event subscription unavailable: {exc}")

    def _on_contact_event(self, event) -> None:
        now = time.monotonic()
        managed_paths = {str(prim.GetPath()) for prim in self._valid_prims()}
        event_text = str(event)
        for prim_path in managed_paths:
            if prim_path in event_text or prim_path.split("/")[-1] in event_text:
                self._last_collision_at[prim_path] = now

    def _log_contact_warning(self, message: str) -> None:
        if self._contact_warning_logged:
            return
        self._contact_warning_logged = True
        try:
            import carb

            carb.log_warn(f"[TimeTravel] {message}; wander continues without collision cooldown")
        except Exception:
            print(f"[TimeTravel] {message}; wander continues without collision cooldown")
