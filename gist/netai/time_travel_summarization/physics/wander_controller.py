import math
import random
import time
from enum import Enum


class PrimState(Enum):
    MOVING = "moving"
    STUNNED = "stunned"
    STANDING_UP = "standing_up"


class WanderController:
    """Drive rigid body prims with a per-prim wander state machine."""

    def __init__(
        self,
        prims: list,
        speed: float = 120.0,
        stun_duration_s: float = 1.0,
        standup_duration_s: float = 1.0,
        velocity_mode: str = "horizontal_per_tick",
        stuck_ratio: float = 0.3,
        stuck_frames: int = 5,
    ):
        self._prims = list(prims)
        self._speed = float(speed)
        self._stun_duration_s = max(0.0, float(stun_duration_s))
        self._standup_duration_s = max(0.0, float(standup_duration_s))
        self._stuck_ratio = float(stuck_ratio)
        self._stuck_frames = int(stuck_frames)

        if velocity_mode not in ("per_tick", "on_enter", "horizontal_per_tick"):
            self._log_warn(f"[Wander] invalid velocity_mode: {velocity_mode}")
            velocity_mode = "horizontal_per_tick"
        self._velocity_mode = velocity_mode

        self._active = False
        self._update_sub = None
        self._contact_sub = None
        self._contact_warning_logged = False
        self._state = {}
        self._state_started_at = {}
        self._direction = {}
        self._last_velocity = {}
        self._last_position = {}
        self._stuck_count = {}
        self._last_tick_time = {}
        self._stuck_logged = set()
        self._contact_log_paths = set()
        self._last_blocked_direction: dict = {}
        self._original_rotation: dict = {}
        self._vertical_position_history: dict = {}

        self._initialize_states()
        self._capture_original_rotations()

    def set_velocity_mode(self, mode: str) -> bool:
        if mode not in ("per_tick", "on_enter", "horizontal_per_tick"):
            self._log_warn(f"[Wander] invalid velocity_mode: {mode}")
            return False
        self._velocity_mode = mode
        self._log_warn(f"[Wander] velocity_mode set to {mode}")
        return True

    def start(self) -> None:
        if self._active:
            return

        import omni.kit.app

        self._active = True
        app = omni.kit.app.get_app()
        self._update_sub = app.get_update_event_stream().create_subscription_to_pop(self._on_update)
        self._subscribe_contact_events()
        self._initialize_states(reset=True)
        for prim in self._valid_prims():
            self._set_kinematic(prim, False)
        if self._velocity_mode == "on_enter":
            for prim in self._valid_prims():
                self._apply_velocity_once(prim, str(prim.GetPath()))
        elif self._velocity_mode == "horizontal_per_tick":
            for prim in self._valid_prims():
                self._apply_horizontal_velocity(prim, str(prim.GetPath()))
        self._log_warn(f"[Wander] started (speed={self._speed:g} units/sec, mode={self._velocity_mode})")

    def stop(self) -> None:
        if not self._active:
            return

        self._active = False
        self._update_sub = None
        self._contact_sub = None
        self._set_all_velocities_zero()
        for prim in self._valid_prims():
            self._set_kinematic(prim, False)
        self._last_velocity.clear()
        self._log_warn("[Wander] stopped")

    def is_active(self) -> bool:
        return self._active

    def _initialize_states(self, reset: bool = False) -> None:
        now = time.time()
        for prim in self._valid_prims():
            prim_path = str(prim.GetPath())
            if reset or prim_path not in self._state:
                self._state[prim_path] = PrimState.MOVING
                self._state_started_at[prim_path] = now
                self._direction[prim_path] = self._random_horizontal_direction(prim.GetStage())

    def _capture_original_rotations(self) -> None:
        try:
            from pxr import Gf, UsdGeom
        except Exception:
            return
        for prim in self._valid_prims():
            prim_path = str(prim.GetPath())
            try:
                xformable = UsdGeom.Xformable(prim)
                for op in xformable.GetOrderedXformOps():
                    if op.GetOpType() == UsdGeom.XformOp.TypeRotateXYZ:
                        val = op.Get()
                        if val is not None:
                            self._original_rotation[prim_path] = Gf.Vec3f(float(val[0]), float(val[1]), float(val[2]))
                            break
                if prim_path not in self._original_rotation:
                    self._original_rotation[prim_path] = Gf.Vec3f(0.0, 0.0, 0.0)
            except Exception:
                self._original_rotation[prim_path] = Gf.Vec3f(0.0, 0.0, 0.0)

    def _is_grounded(self, prim, prim_path: str) -> bool:
        pos = self._world_position(prim)
        if pos is None:
            return True
        vertical_idx = 1 if self._is_y_up(prim.GetStage()) else 2
        history = self._vertical_position_history.setdefault(prim_path, [])
        history.append(float(pos[vertical_idx]))
        if len(history) > 4:
            history.pop(0)
        if len(history) < 3:
            return False
        return (max(history) - min(history)) < 1.0

    def _on_update(self, event) -> None:
        if not self._active:
            return

        now = time.time()
        self._initialize_states()
        for prim in self._valid_prims():
            prim_path = str(prim.GetPath())
            state = self._state.get(prim_path, PrimState.MOVING)
            elapsed = now - self._state_started_at.get(prim_path, now)

            if state == PrimState.MOVING:
                if self._velocity_mode == "per_tick":
                    velocity = self._velocity_for_direction(prim.GetStage(), self._direction[prim_path])
                    self._set_velocity(prim, velocity)
                    self._last_velocity[prim_path] = velocity
                elif self._velocity_mode == "horizontal_per_tick":
                    self._apply_horizontal_velocity(prim, prim_path)
                if self._check_stuck(prim, prim_path, now):
                    self._transition(prim_path, PrimState.STUNNED, prim=prim)
                    continue
            elif state == PrimState.STUNNED:
                if elapsed >= self._stun_duration_s:
                    if self._is_grounded(prim, prim_path):
                        self._transition(prim_path, PrimState.STANDING_UP, prim=prim)
                        self._vertical_position_history.pop(prim_path, None)
                    # else: stay STUNNED, check again next tick
            elif state == PrimState.STANDING_UP:
                duration = self._standup_duration_s
                normalized = 1.0 if duration <= 0.0 else min(elapsed / duration, 1.0)
                self._restore_upright(prim, normalized)
                self._set_angular_velocity_zero(prim)
                if elapsed >= duration:
                    avoid = self._last_blocked_direction.get(prim_path)
                    self._transition(prim_path, PrimState.MOVING, prim=prim)
                    self._direction[prim_path] = self._random_horizontal_direction(prim.GetStage(), avoid_dir=avoid)
                    if self._velocity_mode == "on_enter":
                        self._apply_velocity_once(prim, prim_path)

    def _world_position(self, prim):
        try:
            from pxr import UsdGeom
            cache = UsdGeom.XformCache(0)
            xform = cache.GetLocalToWorldTransform(prim)
            return xform.ExtractTranslation()
        except Exception:
            return None

    def _apply_velocity_once(self, prim, prim_path) -> None:
        try:
            velocity = self._velocity_for_direction(prim.GetStage(), self._direction[prim_path])
            self._set_velocity(prim, velocity)
            self._last_velocity[prim_path] = velocity
        except Exception:
            pass

    def _check_stuck(self, prim, prim_path, now) -> bool:
        pos = self._world_position(prim)
        if pos is None:
            return False
        last_pos = self._last_position.get(prim_path)
        last_t = self._last_tick_time.get(prim_path)
        self._last_position[prim_path] = pos
        self._last_tick_time[prim_path] = now
        if last_pos is None or last_t is None:
            return False
        dt = max(min(now - last_t, 0.1), 1.0 / 240.0)
        expected = self._speed * dt
        if expected <= 0.0:
            return False
        direction = self._direction.get(prim_path)
        if direction is None:
            return False
        delta = (float(pos[0]) - float(last_pos[0]),
                 float(pos[1]) - float(last_pos[1]),
                 float(pos[2]) - float(last_pos[2]))
        progress = delta[0] * direction[0] + delta[1] * direction[1] + delta[2] * direction[2]
        threshold = expected * self._stuck_ratio
        if progress < threshold:
            self._stuck_count[prim_path] = self._stuck_count.get(prim_path, 0) + 1
        else:
            self._stuck_count[prim_path] = 0
            self._stuck_logged.discard(prim_path)
        if self._stuck_count.get(prim_path, 0) >= self._stuck_frames:
            if prim_path not in self._stuck_logged:
                self._stuck_logged.add(prim_path)
                self._log_warn(
                    f"[Wander] STUCK prim={prim_path} progress={progress:.2f}/{expected:.2f} dir={direction}"
                )
            direction = self._direction.get(prim_path)
            if direction is not None:
                self._last_blocked_direction[prim_path] = direction
            return True
        return False

    def _valid_prims(self):
        for prim in self._prims:
            if prim and prim.IsValid():
                yield prim

    def _set_kinematic(self, prim, enabled: bool) -> None:
        try:
            from pxr import UsdPhysics
            api = UsdPhysics.RigidBodyAPI(prim)
            if not api:
                api = UsdPhysics.RigidBodyAPI.Apply(prim)
            api.CreateKinematicEnabledAttr().Set(bool(enabled))
        except Exception as e:
            self._log_warn(f"[Wander] kinematic toggle failed for {prim.GetPath()}: {e}")

    def _apply_horizontal_velocity(self, prim, prim_path) -> None:
        direction = self._direction.get(prim_path)
        if direction is None:
            return
        current = self._get_velocity(prim)
        vertical_idx = 1 if self._is_y_up(prim.GetStage()) else 2
        current_v = [0.0, 0.0, 0.0]
        if current is not None:
            current_v = [float(current[0]), float(current[1]), float(current[2])]
        new_v = [direction[i] * self._speed for i in range(3)]
        new_v[vertical_idx] = current_v[vertical_idx]
        from pxr import Gf
        vel = Gf.Vec3f(new_v[0], new_v[1], new_v[2])
        self._set_velocity(prim, vel)
        self._last_velocity[prim_path] = vel

    def _transition(self, prim_path: str, state: PrimState, prim=None) -> None:
        self._state[prim_path] = state
        self._state_started_at[prim_path] = time.time()
        if state != PrimState.MOVING:
            self._last_velocity.pop(prim_path, None)
        self._last_position.pop(prim_path, None)
        self._last_tick_time.pop(prim_path, None)
        self._stuck_count[prim_path] = 0
        self._stuck_logged.discard(prim_path)
        if state == PrimState.STUNNED:
            self._vertical_position_history.pop(prim_path, None)
        if prim is not None:
            if state == PrimState.STANDING_UP:
                self._set_kinematic(prim, True)
            elif state == PrimState.MOVING:
                self._set_kinematic(prim, False)
                self._set_angular_velocity_zero(prim)
                if self._velocity_mode == "horizontal_per_tick":
                    self._apply_horizontal_velocity(prim, prim_path)

    def _random_horizontal_direction(self, stage=None, avoid_dir=None) -> tuple:
        is_y_up = self._is_y_up(stage)
        for _ in range(5):
            angle = random.uniform(0.0, 2.0 * math.pi)
            a, b = math.cos(angle), math.sin(angle)
            if is_y_up:
                cand = (a, 0.0, b)
            else:
                cand = (a, b, 0.0)
            if avoid_dir is None:
                return cand
            dot = cand[0] * avoid_dir[0] + cand[1] * avoid_dir[1] + cand[2] * avoid_dir[2]
            if dot <= 0.5:
                return cand
        # 5회 reject 실패 → 정반대로 fallback
        if avoid_dir is None:
            return (1.0, 0.0, 0.0)
        return (-float(avoid_dir[0]), -float(avoid_dir[1]), -float(avoid_dir[2]))

    def _velocity_for_direction(self, stage, direction):
        from pxr import Gf

        if self._is_y_up(stage):
            return Gf.Vec3f(direction[0] * self._speed, 0.0, direction[2] * self._speed)
        return Gf.Vec3f(direction[0] * self._speed, direction[1] * self._speed, 0.0)

    def _is_y_up(self, stage) -> bool:
        if stage is None:
            return True

        from pxr import UsdGeom

        return UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y

    def _restore_upright(self, prim, dt_normalized: float) -> None:
        from pxr import Gf, UsdGeom

        t = max(0.0, min(float(dt_normalized), 1.0))
        prim_path = str(prim.GetPath())
        target = self._original_rotation.get(prim_path)
        if target is None:
            target = Gf.Vec3f(0.0, 0.0, 0.0)
        xformable = UsdGeom.Xformable(prim)
        rotate_op = None
        for op in xformable.GetOrderedXformOps():
            if op.GetOpType() == UsdGeom.XformOp.TypeRotateXYZ:
                rotate_op = op
                break
        if rotate_op is None:
            rotate_op = xformable.AddRotateXYZOp()
        current = rotate_op.Get() or Gf.Vec3f(0.0, 0.0, 0.0)
        rotate_op.Set(
            Gf.Vec3f(
                float(current[0]) * (1.0 - t) + float(target[0]) * t,
                float(current[1]) * (1.0 - t) + float(target[1]) * t,
                float(current[2]) * (1.0 - t) + float(target[2]) * t,
            )
        )

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

    def _get_velocity(self, prim):
        attr = prim.GetAttribute("physics:velocity")
        if not attr:
            return None
        return attr.Get()

    def _set_angular_velocity_zero(self, prim) -> None:
        from pxr import Gf, Sdf

        try:
            attr = prim.GetAttribute("physics:angularVelocity")
            if not attr:
                attr = prim.CreateAttribute("physics:angularVelocity", Sdf.ValueTypeNames.Vector3f)
            attr.Set(Gf.Vec3f(0.0, 0.0, 0.0))
        except Exception:
            pass

    def _set_all_velocities_zero(self) -> None:
        from pxr import Gf

        zero = Gf.Vec3f(0.0, 0.0, 0.0)
        for prim in self._valid_prims():
            try:
                self._set_velocity(prim, zero)
                self._set_angular_velocity_zero(prim)
            except Exception:
                pass

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
        for prim_path in self._contact_prim_paths(event):
            if self._state.get(prim_path) == PrimState.MOVING:
                if len(self._contact_log_paths) < 5:
                    self._contact_log_paths.add(prim_path)
                    self._log_warn(f"[Wander] CONTACT prim={prim_path}")
                self._transition(prim_path, PrimState.STUNNED)

    def _on_contact(self, contact_info) -> None:
        self._on_contact_event(contact_info)

    def _contact_prim_paths(self, event):
        managed_paths = {str(prim.GetPath()) for prim in self._valid_prims()}
        event_paths = set()
        for attr_name in ("actor0", "actor1", "prim0", "prim1", "path0", "path1"):
            value = getattr(event, attr_name, None)
            if value:
                event_paths.add(str(value))

        event_text = str(event)
        for prim_path in managed_paths:
            if prim_path in event_paths or prim_path in event_text or prim_path.split("/")[-1] in event_text:
                yield prim_path

    def _vec_length(self, value) -> float:
        return math.sqrt(float(value[0]) ** 2 + float(value[1]) ** 2 + float(value[2]) ** 2)

    def _log_contact_warning(self, message: str) -> None:
        if self._contact_warning_logged:
            return
        self._contact_warning_logged = True
        self._log_warn(f"[TimeTravel] {message}; using velocity-change collision fallback")

    def _log_warn(self, message: str) -> None:
        try:
            import carb

            carb.log_warn(message)
        except Exception:
            print(message)
