import math
import random
from enum import Enum


class PrimState(Enum):
    MOVING = "moving"


class WanderController:
    """Drive rigid body prims with a per-prim horizontal wander.

    The bodies have their horizontal-axis rotation locked at the PhysX level
    (see ``collision_proxy.wrap_with_collision_proxy``), so they only yaw and
    never tip over. On collision -- a PhysX contact event or position-based
    stuck detection -- the body picks a new heading and, when provided, an
    ``on_collision(prim_path, position, kind)`` callback records the event as a
    ground-truth label.
    """

    _VELOCITY_MODES = ("per_tick", "on_enter", "horizontal_per_tick")

    def __init__(
        self,
        prims: list,
        speed: float = 120.0,
        velocity_mode: str = "horizontal_per_tick",
        stuck_ratio: float = 0.3,
        stuck_frames: int = 5,
        collision_cooldown_s: float = 0.5,
        on_collision=None,
        bounds_center=None,
        bounds_half=None,
        wall_margin: float = 0.0,
        wall_frames: int = 5,
        collision_distance: float = 0.0,
        collision_impact_s: float = 0.2,
        collision_pause_s: float = 1.0,
        use_contact_reports: bool = True,
        seed=None,
    ):
        self._prims = list(prims)
        self._speed = float(speed)
        # 에피소드 재현성: seed가 주어지면 heading 선택이 결정적이 됨.
        self._rng = random.Random(seed)
        # sim-time 마스터 클럭: wall-clock(time.time()) 대신 update 이벤트의 고정 dt를
        # 누적해 사용. 렌더/캡처 부하로 루프가 느려져도 물리와 같은 시계를 보므로
        # stuck 오탐(삼각형 진동)·pause 타이밍 왜곡이 사라진다. 단위: sim 초.
        self._sim_now = 0.0
        self._last_dt = 1.0 / 60.0
        self._stuck_ratio = float(stuck_ratio)
        self._stuck_frames = int(stuck_frames)
        self._collision_cooldown_s = max(0.0, float(collision_cooldown_s))
        self._on_collision = on_collision
        # 경계(벽) 근접 탐지: 벽을 따라 미끄러지면 중앙으로 redirect.
        self._bounds_center = tuple(float(v) for v in bounds_center) if bounds_center is not None else None
        self._bounds_half = tuple(float(v) for v in bounds_half) if bounds_half is not None else None
        self._wall_margin = max(0.0, float(wall_margin))
        self._wall_frames = max(1, int(wall_frames))
        self._wall_count: dict = {}
        # 객체-객체 충돌: 중심 간 거리 < collision_distance면 충돌로 간주.
        # 충돌 시 자연 반동을 잠깐 두고 정지한 뒤 서로 멀어지는 방향으로 재출발.
        self._collision_distance = max(0.0, float(collision_distance))
        self._collision_impact_s = max(0.0, float(collision_impact_s))
        self._collision_pause_s = max(0.0, float(collision_pause_s))
        self._paused_until: dict = {}
        self._redirect_heading: dict = {}
        # 객체-객체 충돌 탐지원: True면 PhysX contact report, False면 거리 기반.
        self._use_contact_reports = bool(use_contact_reports)

        if velocity_mode not in self._VELOCITY_MODES:
            self._log_warn(f"[Wander] invalid velocity_mode: {velocity_mode}")
            velocity_mode = "horizontal_per_tick"
        self._velocity_mode = velocity_mode

        self._active = False
        self._update_sub = None
        self._contact_sub = None
        self._contact_warning_logged = False
        self._direction = {}
        self._last_velocity = {}
        self._last_position = {}
        self._stuck_count = {}
        self._last_tick_time = {}
        self._stuck_logged = set()
        self._contact_log_paths = set()
        self._last_blocked_direction: dict = {}
        self._last_collision_time: dict = {}

        self._initialize_directions()

    # ---- configuration ---------------------------------------------------

    def set_velocity_mode(self, mode: str) -> bool:
        if mode not in self._VELOCITY_MODES:
            self._log_warn(f"[Wander] invalid velocity_mode: {mode}")
            return False
        self._velocity_mode = mode
        self._log_info(f"[Wander] velocity_mode set to {mode}")
        return True

    def get_speed(self) -> float:
        return self._speed

    def set_speed(self, speed: float) -> bool:
        try:
            speed = float(speed)
        except (TypeError, ValueError):
            self._log_warn(f"[Wander] invalid speed: {speed!r}")
            return False
        if speed <= 0.0:
            self._log_warn(f"[Wander] invalid speed: {speed:g}")
            return False
        self._speed = speed
        if self._active:
            for prim in self._valid_prims():
                self._apply_current_velocity(prim, str(prim.GetPath()))
        self._log_info(f"[Wander] speed set to {self._speed:g} units/sec")
        return True

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._active:
            return

        import omni.kit.app

        self._active = True
        # 에피소드 시작마다 sim 클럭·충돌 상태 리셋 (seed 재현성 + 이전 run 잔재 제거)
        self._sim_now = 0.0
        self._paused_until.clear()
        self._redirect_heading.clear()
        self._last_collision_time.clear()
        app = omni.kit.app.get_app()
        self._update_sub = app.get_update_event_stream().create_subscription_to_pop(self._on_update)
        if self._use_contact_reports:
            self._subscribe_contact_events()
        self._initialize_directions(reset=True)
        for prim in self._valid_prims():
            self._set_kinematic(prim, False)
            self._apply_current_velocity(prim, str(prim.GetPath()))
        self._log_info(f"[Wander] started (speed={self._speed:g} units/sec, mode={self._velocity_mode})")

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
        self._log_info("[Wander] stopped")

    def is_active(self) -> bool:
        return self._active

    # ---- per-frame update ------------------------------------------------

    def _on_update(self, event) -> None:
        if not self._active:
            return

        # 고정 sim-dt 누적(fixed timestepping이면 payload dt == 고정값). wall-clock 금지.
        try:
            dt = float(event.payload["dt"])
        except Exception:
            dt = self._last_dt
        if dt > 0.0:
            self._last_dt = dt
            self._sim_now += dt
        now = self._sim_now
        self._initialize_directions()
        # contact report ON이면 객체충돌은 콜백이 처리 → 거리 기반은 OFF일 때만.
        if not self._use_contact_reports:
            self._handle_object_collisions(now)
        for prim in self._valid_prims():
            prim_path = str(prim.GetPath())

            # 충돌 직후: 앞 impact 구간은 PhysX 반동(restitution) 그대로 두어 자연스럽게
            # 튕기고, 뒤 pause 구간은 완전 정지.
            paused_until = self._paused_until.get(prim_path, 0.0)
            if now < paused_until:
                if (paused_until - now) <= self._collision_pause_s:
                    self._set_all_motion_zero(prim)
                continue

            # pause 종료 직후: 멀어지는 방향으로 재출발.
            heading = self._redirect_heading.pop(prim_path, None)
            if heading is not None:
                self._direction[prim_path] = heading
                self._apply_current_velocity(prim, prim_path)

            if self._velocity_mode in ("per_tick", "horizontal_per_tick"):
                self._apply_current_velocity(prim, prim_path)
            if self._check_stuck(prim, prim_path, now):
                self._redirect(prim, prim_path, kind="stuck")
            elif self._check_wall_hug(prim, prim_path):
                self._redirect(prim, prim_path, kind="wall", new_direction=self._heading_to_center(prim))

    def _redirect(self, prim, prim_path: str, kind: str, new_direction=None) -> None:
        """Pick a new heading and record the hit.

        ``new_direction`` lets callers steer (e.g. toward the box center for
        wall-hugging); otherwise a random heading away from the block is chosen.
        """
        now = self._sim_now
        # 기본값 -inf: sim 클럭은 0에서 시작하므로 0.0 기본값이면 첫 cooldown 구간의
        # 정당한 첫 충돌까지 억제된다.
        if now - self._last_collision_time.get(prim_path, float("-inf")) < self._collision_cooldown_s:
            return
        self._last_collision_time[prim_path] = now

        if new_direction is not None:
            self._direction[prim_path] = new_direction
        else:
            avoid = self._last_blocked_direction.get(prim_path)
            self._direction[prim_path] = self._random_horizontal_direction(prim.GetStage(), avoid_dir=avoid)
        self._stuck_count[prim_path] = 0
        self._wall_count[prim_path] = 0
        self._last_position.pop(prim_path, None)
        self._last_tick_time.pop(prim_path, None)
        self._stuck_logged.discard(prim_path)
        self._apply_current_velocity(prim, prim_path)
        self._emit_collision(prim, prim_path, kind)

    def _emit_collision(self, prim, prim_path: str, kind: str) -> None:
        if self._on_collision is None:
            return
        pos = self._world_position(prim)
        position = (float(pos[0]), float(pos[1]), float(pos[2])) if pos is not None else None
        try:
            self._on_collision(prim_path, position, kind)
        except Exception as exc:
            self._log_warn(f"[Wander] on_collision callback failed: {exc}")

    # ---- velocity --------------------------------------------------------

    def _apply_current_velocity(self, prim, prim_path: str) -> None:
        if self._velocity_mode == "horizontal_per_tick":
            self._apply_horizontal_velocity(prim, prim_path)
        else:
            self._apply_velocity_once(prim, prim_path)

    def _apply_velocity_once(self, prim, prim_path: str) -> None:
        try:
            velocity = self._velocity_for_direction(prim.GetStage(), self._direction[prim_path])
            self._set_velocity(prim, velocity)
            self._last_velocity[prim_path] = velocity
        except Exception:
            pass

    def _apply_horizontal_velocity(self, prim, prim_path: str) -> None:
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

    def _velocity_for_direction(self, stage, direction):
        from pxr import Gf

        if self._is_y_up(stage):
            return Gf.Vec3f(direction[0] * self._speed, 0.0, direction[2] * self._speed)
        return Gf.Vec3f(direction[0] * self._speed, direction[1] * self._speed, 0.0)

    # ---- stuck detection -------------------------------------------------

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
        # now/last_t 모두 sim-time → dt는 실제 물리 전진량과 동일 구간.
        # (wall-clock이던 시절의 캡처 부하 false-stuck 원인 제거)
        dt = now - last_t
        if dt <= 0.0:
            return False
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
                self._log_info(
                    f"[Wander] STUCK prim={prim_path} progress={progress:.2f}/{expected:.2f} dir={direction}"
                )
            if direction is not None:
                self._last_blocked_direction[prim_path] = direction
            return True
        return False

    # ---- object-object collision ----------------------------------------

    def _horizontal_axes(self, stage):
        return (0, 2) if self._is_y_up(stage) else (0, 1)

    def _handle_object_collisions(self, now: float) -> None:
        """Pairwise proximity check: when two managed prims overlap, bump them.

        Contact reports are unreliable in this Kit build, so we detect
        object-object collisions by center distance using known positions.
        """
        if self._collision_distance <= 0.0:
            return
        prims = list(self._valid_prims())
        if len(prims) < 2:
            return
        a, b = self._horizontal_axes(prims[0].GetStage())
        entries = []
        for prim in prims:
            pos = self._world_position(prim)
            if pos is not None:
                entries.append((str(prim.GetPath()), prim, pos))
        threshold_sq = self._collision_distance * self._collision_distance
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                path_a, prim_a, pos_a = entries[i]
                path_b, prim_b, pos_b = entries[j]
                # pause 종료 후 cooldown 동안은 재발동 금지.
                # (안 그러면 아직 붙어있는 동안 매 틱 재pause되어 영원히 멈춤)
                guard_a = self._paused_until.get(path_a, float("-inf")) + self._collision_cooldown_s
                guard_b = self._paused_until.get(path_b, float("-inf")) + self._collision_cooldown_s
                if now < guard_a or now < guard_b:
                    continue
                da = float(pos_a[a]) - float(pos_b[a])
                db = float(pos_a[b]) - float(pos_b[b])
                if da * da + db * db < threshold_sq:
                    self._begin_object_collision(path_a, prim_a, pos_a, path_b, prim_b, pos_b, a, b, now)

    def _begin_object_collision(self, path_a, prim_a, pos_a, path_b, prim_b, pos_b, a, b, now) -> None:
        self._log_info(f"[Wander] OBJECT-COLLISION {path_a} <-> {path_b}")
        self._pause_and_redirect(path_a, prim_a, self._away_heading(pos_a, pos_b, a, b), now)
        self._pause_and_redirect(path_b, prim_b, self._away_heading(pos_b, pos_a, a, b), now)

    def _pause_and_redirect(self, prim_path, prim, heading, now) -> None:
        """Stop the prim for a beat, then resume along ``heading`` (away from the hit)."""
        self._last_collision_time[prim_path] = now
        # 전체 정지 윈도우 = 반동(impact) + 멈춤(pause).
        self._paused_until[prim_path] = now + self._collision_impact_s + self._collision_pause_s
        self._redirect_heading[prim_path] = heading
        self._stuck_count[prim_path] = 0
        self._wall_count[prim_path] = 0
        self._last_position.pop(prim_path, None)
        self._last_tick_time.pop(prim_path, None)
        self._stuck_logged.discard(prim_path)
        self._emit_collision(prim, prim_path, "object")

    def _away_heading(self, pos_self, pos_other, a, b, jitter_deg: float = 30.0):
        """Horizontal unit heading pointing from ``pos_other`` toward ``pos_self``."""
        da = float(pos_self[a]) - float(pos_other[a])
        db = float(pos_self[b]) - float(pos_other[b])
        if da * da + db * db <= 1e-12:
            return self._random_horizontal_direction()
        angle = math.atan2(db, da) + math.radians(self._rng.uniform(-jitter_deg, jitter_deg))
        vec = [0.0, 0.0, 0.0]
        vec[a] = math.cos(angle)
        vec[b] = math.sin(angle)
        return tuple(vec)

    # ---- wall-hug detection ---------------------------------------------

    def _check_wall_hug(self, prim, prim_path: str) -> bool:
        """True when the prim has hugged a boundary wall for ``wall_frames``.

        Stuck detection misses shallow-angle wall slides (the body keeps making
        progress along its heading while pinned to the wall). Here we instead
        measure distance to the nearest wall using the known box bounds.
        """
        if self._bounds_center is None or self._bounds_half is None or self._wall_margin <= 0.0:
            return False
        pos = self._world_position(prim)
        if pos is None:
            return False
        a, b = (0, 2) if self._is_y_up(prim.GetStage()) else (0, 1)
        nearest = min(
            self._bounds_half[a] - abs(float(pos[a]) - self._bounds_center[a]),
            self._bounds_half[b] - abs(float(pos[b]) - self._bounds_center[b]),
        )
        if nearest < self._wall_margin:
            self._wall_count[prim_path] = self._wall_count.get(prim_path, 0) + 1
        else:
            self._wall_count[prim_path] = 0
        if self._wall_count.get(prim_path, 0) >= self._wall_frames:
            self._log_info(f"[Wander] WALL-HUG prim={prim_path} nearest={nearest:.2f} -> redirect to center")
            return True
        return False

    def _heading_to_center(self, prim, jitter_deg: float = 35.0):
        """Horizontal unit heading from the prim toward the box center, plus jitter."""
        pos = self._world_position(prim)
        if pos is None or self._bounds_center is None:
            return self._random_horizontal_direction(prim.GetStage())
        a, b = (0, 2) if self._is_y_up(prim.GetStage()) else (0, 1)
        da = self._bounds_center[a] - float(pos[a])
        db = self._bounds_center[b] - float(pos[b])
        if da * da + db * db <= 1e-12:
            return self._random_horizontal_direction(prim.GetStage())
        angle = math.atan2(db, da) + math.radians(self._rng.uniform(-jitter_deg, jitter_deg))
        vec = [0.0, 0.0, 0.0]
        vec[a] = math.cos(angle)
        vec[b] = math.sin(angle)
        return tuple(vec)

    # ---- heading ---------------------------------------------------------

    def _initialize_directions(self, reset: bool = False) -> None:
        for prim in self._valid_prims():
            prim_path = str(prim.GetPath())
            if reset or prim_path not in self._direction:
                self._direction[prim_path] = self._random_horizontal_direction(prim.GetStage())

    def _random_horizontal_direction(self, stage=None, avoid_dir=None) -> tuple:
        is_y_up = self._is_y_up(stage)
        for _ in range(5):
            angle = self._rng.uniform(0.0, 2.0 * math.pi)
            a, b = math.cos(angle), math.sin(angle)
            cand = (a, 0.0, b) if is_y_up else (a, b, 0.0)
            if avoid_dir is None:
                return cand
            dot = cand[0] * avoid_dir[0] + cand[1] * avoid_dir[1] + cand[2] * avoid_dir[2]
            if dot <= 0.5:
                return cand
        # 5회 reject 실패 → 정반대로 fallback
        if avoid_dir is None:
            return (1.0, 0.0, 0.0)
        return (-float(avoid_dir[0]), -float(avoid_dir[1]), -float(avoid_dir[2]))

    def _is_y_up(self, stage) -> bool:
        if stage is None:
            return True

        from pxr import UsdGeom

        return UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y

    # ---- USD / PhysX helpers ---------------------------------------------

    def _valid_prims(self):
        for prim in self._prims:
            if prim and prim.IsValid():
                yield prim

    def _world_position(self, prim):
        try:
            from pxr import UsdGeom
            cache = UsdGeom.XformCache(0)
            xform = cache.GetLocalToWorldTransform(prim)
            return xform.ExtractTranslation()
        except Exception:
            return None

    def _set_kinematic(self, prim, enabled: bool) -> None:
        try:
            from pxr import UsdPhysics
            api = UsdPhysics.RigidBodyAPI(prim)
            if not api:
                api = UsdPhysics.RigidBodyAPI.Apply(prim)
            api.CreateKinematicEnabledAttr().Set(bool(enabled))
        except Exception as e:
            self._log_warn(f"[Wander] kinematic toggle failed for {prim.GetPath()}: {e}")

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
                self._set_all_motion_zero(prim, zero)
            except Exception:
                pass

    def _set_all_motion_zero(self, prim, zero=None) -> None:
        if zero is None:
            from pxr import Gf

            zero = Gf.Vec3f(0.0, 0.0, 0.0)
        self._set_velocity(prim, zero)
        self._set_angular_velocity_zero(prim)

    # ---- contact events --------------------------------------------------

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

    def _on_contact_event(self, contact_headers, contact_data) -> None:
        """subscribe_contact_report_events 콜백. 시그니처는 반드시 (headers, data) 2개.

        두 관리 객체끼리의 접촉만 처리(멈춤+분리). 벽 충돌은 거리 기반 wall-hug가
        '중앙으로 redirect'로 따로 처리하므로 여기선 무시.
        """
        from omni.physx.scripts.physicsUtils import PhysicsSchemaTools

        managed = {str(p.GetPath()): p for p in self._valid_prims()}
        now = self._sim_now
        for header in contact_headers:
            # CONTACT_LOST 등 데이터 없는 이벤트 스킵(구버전 crash 방어 겸용)
            if header.num_contact_data == 0:
                continue
            try:
                path_a = str(PhysicsSchemaTools.intToSdfPath(header.actor0))
                path_b = str(PhysicsSchemaTools.intToSdfPath(header.actor1))
            except Exception:
                continue
            prim_a = managed.get(path_a)
            prim_b = managed.get(path_b)
            if prim_a is not None and prim_b is not None:
                self._object_collision_from_contact(prim_a, path_a, prim_b, path_b, now)

    def _object_collision_from_contact(self, prim_a, path_a, prim_b, path_b, now) -> None:
        """contact report로 받은 객체 쌍 충돌 → 거리 기반과 동일한 멈춤+분리 로직 재사용."""
        guard_a = self._paused_until.get(path_a, float("-inf")) + self._collision_cooldown_s
        guard_b = self._paused_until.get(path_b, float("-inf")) + self._collision_cooldown_s
        if now < guard_a or now < guard_b:
            return
        pos_a = self._world_position(prim_a)
        pos_b = self._world_position(prim_b)
        if pos_a is None or pos_b is None:
            return
        a, b = self._horizontal_axes(prim_a.GetStage())
        if len(self._contact_log_paths) < 5:
            self._contact_log_paths.add(path_a)
            self._log_info(f"[Wander] CONTACT(report) {path_a} <-> {path_b}")
        self._begin_object_collision(path_a, prim_a, pos_a, path_b, prim_b, pos_b, a, b, now)

    # ---- logging ---------------------------------------------------------

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

    def _log_info(self, message: str) -> None:
        """일상/이벤트 로그용 — info 레벨(기본 콘솔에 안 보임). carb 없으면 무시."""
        try:
            import carb

            carb.log_info(message)
        except Exception:
            pass
