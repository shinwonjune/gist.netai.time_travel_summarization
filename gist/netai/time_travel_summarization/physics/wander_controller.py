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

    ``near_miss_gap > 0``이면 안무가 near-miss 모드로 바뀐다: 짝끼리 접근하다
    중심거리 gap 근처에서 흩어지기를 반복하며, 어떤 쌍도 gap 아래로 붙지 않는다
    → 접촉 없음 → GT 충돌 0건 (근접만으로 오탐하는지 보는 대조 데이터셋용).

    ``near_miss_mode``가 실제 안무를 고른다(기본 ``"swerve"``):
      - ``"swerve"`` (기본): 속도는 항상 유지하고 방향만 굽힌다 — 접근하다 gap
        근처에서 스치듯 커브를 그리며 지나간다. 감속·정지가 없어 "보이지 않는
        벽에 부딪힌" 인상이 없다.
      - ``"stop"``: v1 안무 — gap 근처에서 감속·정지·재출발. 상호 감속+정지+
        방향전환이 실제 충돌의 운동 신호와 닮아 GUI 육안 검수에서 기각되었으나,
        "감속 단서"와 "근접 단서"를 분리해 보는 대조군으로 남겨둔다(실험적 유용성).

    자세한 보증 방식은 ``_near_miss_step`` 참조.
    """

    _VELOCITY_MODES = ("per_tick", "on_enter", "horizontal_per_tick")
    _NEAR_MISS_MODES = ("swerve", "stop")
    # near-miss: gap의 이 비율만큼 여유가 남으면 "도착"으로 보고 정지 페이즈로 넘어간다
    # (속도 상한이 gap을 점근 접근시키므로 정확히 gap이 되는 순간은 오지 않는다).
    _NEAR_MISS_ARRIVE_FRAC = 0.05
    # 벽·모서리에 막혀 짝이 못 만나도 안무가 멈추지 않도록 접근 페이즈에 상한을 둔다.
    _NEAR_MISS_APPROACH_TIMEOUT_S = 20.0

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
        near_miss_gap: float = 0.0,
        near_miss_mode: str = "swerve",
        near_miss_hold_s: float = 1.0,
        near_miss_depart_s: float = 3.0,
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
        self._prev_timeline_t = None
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
        # near-miss 모드(>0이면 활성): 짝끼리 접근 → gap에서 정지 → 이탈을 반복하되
        # 어떤 쌍도 중심거리 gap 아래로 내려가지 않는다(=접촉 없음 → GT 0건).
        self._near_miss_gap = max(0.0, float(near_miss_gap))
        if near_miss_mode not in self._NEAR_MISS_MODES:
            self._log_warn(f"[Wander] invalid near_miss_mode: {near_miss_mode!r} -> swerve")
            near_miss_mode = "swerve"
        self._near_miss_mode = near_miss_mode
        self._near_miss_hold_s = max(0.0, float(near_miss_hold_s))
        self._near_miss_depart_s = max(0.0, float(near_miss_depart_s))
        self._nm_phase = "approach"
        # 첫 페이즈 마감은 0이 아니라 접근 상한 — 0이면 sim 클럭 0초에 곧바로 정지로 넘어간다.
        self._nm_phase_until = self._NEAR_MISS_APPROACH_TIMEOUT_S
        self._nm_cycle = 0

        if velocity_mode not in self._VELOCITY_MODES:
            self._log_warn(f"[Wander] invalid velocity_mode: {velocity_mode}")
            velocity_mode = "horizontal_per_tick"
        self._velocity_mode = velocity_mode

        self._active = False
        self._update_sub = None
        self._physx_step_sub = None
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
        # 실효 속도 콘솔 표기: 창(sim 초)마다 객체별 경로누적/시간을 로그로 출력.
        # 지시속도와 실제 이동속도(damping·충돌 pause 포함)를 눈으로 비교하는 용도.
        self._speed_log_interval = 2.0
        self._speed_window_start = 0.0
        self._speed_accum: dict = {}
        self._speed_last_pos: dict = {}

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
        self._prev_timeline_t = None
        self._paused_until.clear()
        self._redirect_heading.clear()
        self._last_collision_time.clear()
        self._speed_window_start = 0.0
        self._speed_accum.clear()
        self._speed_last_pos.clear()
        self._nm_phase = "approach"
        self._nm_phase_until = self._NEAR_MISS_APPROACH_TIMEOUT_S
        self._nm_cycle = 0
        # 1순위: PhysX 물리 스텝 이벤트. 물리가 1스텝 돌 때마다 정확히 1회 발화하므로
        # 러너의 펌프 패턴(60fps/렌더 데시메이션/GUI)과 무관하게 제어 주기 = 물리 주기.
        # update 스트림은 "물리 스텝 ≠ update 틱" 구조(렌더 대기 펌프, 데시메이션의
        # orchestrator 전진)에서 velocity 재주장을 놓쳐 감쇠 누적 → 실효 속도 저하(#9 계열).
        self._physx_step_sub = None
        try:
            import omni.physx

            self._physx_step_sub = omni.physx.get_physx_interface().subscribe_physics_step_events(
                self._on_physics_step)
        except Exception as e:
            self._log_warn(f"[Wander] physics-step subscription unavailable ({e!r}) -> update-stream fallback")
        if self._physx_step_sub is None:
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
        self._physx_step_sub = None
        self._contact_sub = None
        self._set_all_velocities_zero()
        for prim in self._valid_prims():
            self._set_kinematic(prim, False)
        self._last_velocity.clear()
        self._log_info("[Wander] stopped")

    def is_active(self) -> bool:
        return self._active

    # ---- per-frame update ------------------------------------------------

    def _on_physics_step(self, dt) -> None:
        # 1순위 경로: PhysX가 물리를 1스텝 돌릴 때마다 정확히 1회 호출됨(러너 무관).
        # 이 dt 누적이 곧 물리 진실의 시계 — update 틱과 물리 스텝의 불일치 문제가
        # 원천적으로 없다.
        if not self._active:
            return
        try:
            dt = float(dt)
        except (TypeError, ValueError):
            dt = self._last_dt
        if dt > 0.0:
            self._last_dt = dt
            self._sim_now += dt
        self._tick(self._sim_now)

    def _on_update(self, event) -> None:
        # 폴백 경로(physx 스텝 이벤트 미지원/유닛테스트). update 틱은 물리 스텝과
        # 1:1이 아니므로(렌더 대기 펌프, 데시메이션의 orchestrator 전진) 시계는
        # 타임라인 직독으로 맞추되, 물리 스텝 누락 가능성은 이 경로의 한계.
        if not self._active:
            return

        # 시계 = 타임라인 현재 시각 직독(물리 진실). headless 캡처는 렌더 완료 대기
        # 동안 app.update()가 여러 번 돌고 그 헛바퀴에도 payload dt>0이 오므로, dt 누적
        # 시계로는 기대이동만 쌓여 false-stuck 폭증(실측 10초에 468회 → 진동 랜덤워크).
        # 타임라인이 실제 전진한 틱만 판정하면 기대/실이동이 항상 같은 구간이 된다.
        now = None
        try:
            import omni.timeline

            now = float(omni.timeline.get_timeline_interface().get_current_time())
        except Exception:
            pass
        if now is None:
            # 폴백(omni 없는 유닛테스트 등): 기존 dt 누적 방식
            try:
                dt = float(event.payload["dt"])
            except Exception:
                dt = self._last_dt
            if dt > 0.0:
                self._last_dt = dt
                self._sim_now += dt
            now = self._sim_now
        else:
            prev = self._prev_timeline_t
            self._prev_timeline_t = now
            if prev is not None and now == prev:
                return  # 물리 미전진 틱(렌더 대기 펌프) — 스킵
            if prev is not None and now > prev:
                self._last_dt = now - prev
            self._sim_now = now
        self._tick(now)

    def _tick(self, now: float) -> None:
        self._initialize_directions()
        if self._near_miss_gap > 0.0:
            self._near_miss_step(now)
        else:
            self._wander_step(now)
        self._log_effective_speed(now)

    def _wander_step(self, now: float) -> None:
        """기본 안무: 랜덤 헤딩 유지 + 충돌/끼임/벽 감지 시 재방향(=GT 이벤트 발생 경로)."""
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

    # ---- near-miss choreography -----------------------------------------

    def _near_miss_partners(self, paths: list) -> dict:
        """짝짓기: 정렬된 경로를 사이클마다 한 칸씩 회전시켜 인접끼리 묶는다 —
        한 에피소드 안에서 매 사이클 다른 조합이 만난다. 홀수면 1개는 짝 없음
        (그 객체는 기존 랜덤 헤딩 유지)."""
        order = sorted(paths)
        if len(order) < 2:
            return {}
        r = self._nm_cycle % len(order)
        order = order[r:] + order[:r]
        out: dict = {}
        for i in range(0, len(order) - 1, 2):
            out[order[i]] = order[i + 1]
            out[order[i + 1]] = order[i]
        return out

    def _near_miss_step(self, now: float) -> None:
        """near-miss 안무: 접근 → gap 근처에서 (모드에 따라) 정지 또는 스침 → 이탈, 반복.

        불변식(양쪽 모드 공통): 어떤 쌍도 수평 중심거리가 gap 아래로 내려가지 않는다
        (=접촉 없음). GT(collisions CSV의 kind="object")는 PhysX contact report에서만
        생기므로, 콜라이더를 키워 간격을 만들면 그 자체가 contact를 발화시켜 GT를
        오염시킨다 → 대신 기존 이동 방식과 같은 "속도 제어"로 접촉 전에 세운다.

        ``near_miss_mode="stop"`` 보증 방식(v1): 각 객체의 "전체 속도"를
        (최근접 이웃 거리 - gap) / (2·dt) 이하로 묶는다. 둘이 마주 달려도 한 스텝의
        접근량 ≤ (d - gap)이라 d < gap이 될 수 없다 — 이 부등식은 두 객체의 이동
        방향과 무관하게 "속도 크기의 합"만으로 성립하는 삼각부등식이라, 최근접이
        아닌 제3의 쌍에 대해서도 안전하다(최근접 거리 ≤ 다른 어떤 쌍과의 거리이므로
        캡이 항상 더 타이트함). 대가는 gap에 가까워질수록 속도가 줄어드는 자연스러운
        감속 — 상호 감속+정지+방향전환이 실제 충돌의 운동 신호와 닮아 GUI 육안
        검수에서 기각되었다(§ near_miss_mode 주석 참조).

        ``near_miss_mode="swerve"`` 보증 방식(기본, v2): 전체 속도는 항상 self._speed로
        유지하고, "이웃 방향(r̂)에 대한 반경 성분 v_r"만 캡을 씌운다 — 캡이 빼앗은
        만큼을 접선 성분 v_t로 돌려 |v|=self._speed를 보존한다(``_swerve_direction``
        참조). 거리 변화율은 두 객체의 속도 중 r̂ 방향 성분에만 의존하므로(접선
        성분은 순간적으로 거리에 기여하지 않는다) 캡이 걸린 반경 성분만으로 stop과
        동일한 삼각부등식이 성립 — d(pair) ≥ gap이 똑같이 보장된다.

        주의: 이 캡을 "최근접 이웃 하나"에만 걸고 끝내면 안 된다 — stop의 전체속도
        캡은 (최근접 거리-gap)이 다른 모든 쌍의 (거리-gap)보다 작다는 사실 덕분에
        코시-슈바르츠로 "모든 방향"에 대해 자동 일반화되지만, 반경 성분 캡은 그
        일반화가 안 된다(접선으로 돌린 여분 속도가 하필 제3의 이웃 방향과 겹치면
        그 쌍은 캡이 안 걸린 것과 같다 — 실제로 5~6객체 무작위 스폰에서 지정 짝이
        아닌 제3자와의 gap 침범이 재현됐다). 그래서 속도 적용 루프는 2단계다:
          1) 곡선 선택 — 최근접 이웃 r̂ 하나만 기준으로 ``_swerve_direction``을
             적용해 매끄러운 단일 커브 방향을 고른다(여러 이웃을 순서대로 굽히면
             뒤쪽 보정이 앞쪽 캡을 다시 무너뜨릴 수 있어, 방향 자체는 최근접
             하나로 고정한다).
          2) 안전핀(전체 이웃 검증) — 그 방향의 반경 성분을 이웃 "전체"에 대해
             다시 확인해, 자기 몫의 캡(``(d_k-gap)/(2dt)``)을 넘는 이웃이 있으면
             방향은 그대로 두고 "전체 속력"을 가장 타이트한 위반 비율만큼
             줄인다(``scale = min(1, allowed_k/v_r_k)`` 중 최소). 크기만 균등하게
             줄이므로 이미 캡 안에 있던 다른 모든 성분도 함께 작아져 재위반이
             생기지 않는다 — 이 스케일은 pair(i,k) 양쪽 객체가 "같은" 거리
             d_ik에서 유도한 같은 allowed_k를 각자 독립적으로 만족시키므로,
             stop의 삼각부등식 논증이 쌍 단위로 그대로 성립한다(양쪽 반경 성분의
             합 ≤ (d_ik-gap) ⇒ d_ik(t+dt) ≥ gap). 보통(사이클당 짝 하나만 근접)은
             scale=1.0 그대로라 감속이 없다 — 실제로 이 안전핀이 발동하는 경우는
             여러 쌍이 동시에 gap 근처로 몰리는 드문 상황뿐이며, 그때만 부분
             감속으로 우아하게 물러난다(완전한 무정지 보장보다 gap 불변식이
             우선이라는 명시적 트레이드오프).

        페이즈는 "approach"(짝 방향으로 접근) → "hold"(stop 전용: 완전 정지) →
        "depart"(멀어짐) → 반복. swerve는 hold가 없다 — approach에서 도착 조건이
        차면 곧장 depart로 넘어가되, 이 때 방향을 되돌리지 않는다(이미 스침 과정에서
        접선 쪽으로 굽어 있는 헤딩을 그대로 이어간다) — stop처럼 away_heading으로
        홱 꺾으면 그 자체가 반전 신호가 되어 "감속 없는 스침"이라는 목적이 깨진다.

        "이미 gap 안" 응급 이탈(아래 속도 적용 루프 맨 앞)도 같은 안전핀을 쓴다 —
        원래는 near_path 하나로부터 무조건 전속 이탈했는데, 그 이탈 방향이 "제3의"
        이웃 쪽을 향하면(그 제3자와는 이 tick 전까지 gap 이상 떨어져 있어 안전했던
        경우) 전속 이탈 자체가 그 제3자와의 불변식을 새로 깨는 사례가 4~8객체·
        무작위 스폰 스트레스 테스트에서 재현됐다(양쪽 모드 공유 경로라 stop에도
        잠재했던 결함). 그래서 이 응급 이탈도 near_path를 제외한 나머지 이웃
        전체에 대해 동일한 반경 캡+속력 스케일을 적용한다 — near_path로부터는
        여전히 무조건 전속(그게 응급 이탈의 목적), 제3자에 대해서만 필요시 감속.
        """
        entries = []
        for prim in self._valid_prims():
            pos = self._world_position(prim)
            if pos is not None:
                entries.append((str(prim.GetPath()), prim, pos))
        if len(entries) < 2:
            return
        a, b = self._horizontal_axes(entries[0][1].GetStage())
        gap = self._near_miss_gap
        dt = max(self._last_dt, 1e-6)
        swerve = self._near_miss_mode == "swerve"

        # 이웃 전체(수평 거리, 오름차순) — 최근접은 nearest[p][0]. swerve는 최근접
        # 하나만이 아니라 전체 이웃에 대해 순서대로 반경 캡을 걸어야 한다: 최근접
        # 기준 캡은 그 상대와의 쌍만 보증하고, 사이클이 여러 번 돌면 지정 짝이 아닌
        # 제3의 객체와 우연히 가까워질 수 있어(그 제3자 쪽은 캡이 안 걸릴 수 있음)
        # 전체 이웃을 훑어야 어떤 쌍도 gap 아래로 안 내려간다는 불변식이 유지된다.
        neighbors: dict = {p: [] for p, _, _ in entries}
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                pa, _, posa = entries[i]
                pb, _, posb = entries[j]
                da = float(posa[a]) - float(posb[a])
                db = float(posa[b]) - float(posb[b])
                d = (da * da + db * db) ** 0.5
                neighbors[pa].append((d, pb))
                neighbors[pb].append((d, pa))
        for p in neighbors:
            neighbors[p].sort(key=lambda t: t[0])
        nearest: dict = {p: (ns[0] if ns else (float("inf"), None)) for p, ns in neighbors.items()}

        pos_by = {p: pos for p, _, pos in entries}
        partner = self._near_miss_partners(list(pos_by))
        arrive = gap * (1.0 + self._NEAR_MISS_ARRIVE_FRAC)

        # ---- 페이즈 전이 -------------------------------------------------
        if self._nm_phase == "approach":
            pair_d = [nearest[p][0] for p in partner]
            arrived = bool(pair_d) and all(d <= arrive for d in pair_d)
            if arrived or now >= self._nm_phase_until:
                if swerve:
                    # hold 없이 곧장 이탈 — 헤딩은 건드리지 않는다(이미 굽어 있음).
                    self._nm_phase = "depart"
                    self._nm_phase_until = now + self._near_miss_depart_s
                    self._log_warn(
                        f"[Wander] near-miss SWERVE pass (min pair d={min(pair_d or [0.0]):.1f}, gap={gap:.1f})")
                else:
                    self._nm_phase = "hold"
                    self._nm_phase_until = now + self._near_miss_hold_s
                    self._log_warn(
                        f"[Wander] near-miss HOLD (min pair d={min(pair_d or [0.0]):.1f}, gap={gap:.1f})")
        elif self._nm_phase == "hold":  # stop 모드에서만 도달
            if now >= self._nm_phase_until:
                self._nm_phase = "depart"
                self._nm_phase_until = now + self._near_miss_depart_s
                for path, prim, pos in entries:
                    other = partner.get(path) or nearest[path][1]
                    if other is not None and not self._is_near_wall(prim):
                        self._direction[path] = self._away_heading(pos, pos_by[other], a, b)
                    else:
                        self._direction[path] = self._heading_to_center(prim)
        elif now >= self._nm_phase_until:
            self._nm_phase = "approach"
            self._nm_phase_until = now + self._NEAR_MISS_APPROACH_TIMEOUT_S
            self._nm_cycle += 1

        # ---- 속도 적용 ---------------------------------------------------
        for path, prim, pos in entries:
            d_near, near_path = nearest[path]
            if d_near < gap and near_path is not None:
                # 이미 gap 안(스폰 간격 부족·부동소수 경계 등) — 페이즈보다 우선해
                # near_path로부터 전속 이탈, 불변식 회복. 단 이 탈출 방향이 "제3의"
                # 이웃 쪽을 향할 수 있으므로(그 이웃과는 아직 gap 이상 떨어져 있어
                # 이 tick 전까지는 안전했던 경우) near_path를 제외한 나머지 전체
                # 이웃에 대해서도 반경 캡 안전핀을 똑같이 적용한다 — 그렇지 않으면
                # "전속 이탈"이 그 자체로 제3자와의 불변식을 깨는 새 위반이 된다
                # (실측: 4~8객체 무작위 스폰 스트레스 테스트에서 재현됨).
                direction = self._away_heading(pos, pos_by[near_path], a, b)
                scale = 1.0
                for d_k, other_path in neighbors.get(path, []):
                    if other_path == near_path:
                        continue  # 이 상대로부터는 무조건 전속 이탈 -- 캡 대상 아님
                    r_hat_k = self._toward_heading(pos, pos_by[other_path], a, b)
                    r_dot_k = self._speed * (
                        direction[0] * r_hat_k[0] + direction[1] * r_hat_k[1] + direction[2] * r_hat_k[2])
                    if r_dot_k <= 0.0:
                        continue
                    allowed_k = max(0.0, (d_k - gap) / (2.0 * dt))
                    if r_dot_k > allowed_k:
                        scale = min(scale, allowed_k / r_dot_k)
                self._direction[path] = direction
                self._apply_horizontal_velocity(prim, path, speed=self._speed * scale)
                continue
            if self._nm_phase == "hold":
                self._set_all_motion_zero(prim)
                continue
            if self._nm_phase == "approach":
                mate = partner.get(path)
                if mate is not None:
                    self._direction[path] = self._toward_heading(pos, pos_by[mate], a, b)
                elif self._is_near_wall(prim):
                    self._direction[path] = self._heading_to_center(prim)
            elif self._is_near_wall(prim):
                self._direction[path] = self._heading_to_center(prim)

            if swerve:
                # 1) 최근접 이웃 기준으로 굽힌다 — 접선 방향을 하나만 고르므로 매끄러운
                #    단일 커브가 나온다(여러 이웃을 순서대로 굽히면 뒤쪽 보정이 앞쪽을
                #    다시 무너뜨릴 수 있어 커브 선택 자체는 최근접 하나로 충분).
                direction = self._direction[path]
                if near_path is not None:
                    allowed_near = (d_near - gap) / (2.0 * dt)
                    r_hat_near = self._toward_heading(pos, pos_by[near_path], a, b)
                    direction = self._swerve_direction(direction, r_hat_near, allowed_near, a, b)
                self._direction[path] = direction
                # 2) 안전핀(전체 이웃 검증): 위 커브가 "최근접이 아닌" 제3의 이웃 쪽으로
                #    우연히 접선을 돌린 경우를 잡는다 — 그 이웃에 대한 반경 성분이 자기
                #    몫의 캡(allowed_k)을 넘으면, 방향은 그대로 두고 "전체 속력"을 가장
                #    타이트한 위반 비율만큼 줄인다. 크기만 줄이므로 이미 캡 안에 있던
                #    다른 모든 성분도 함께 작아져(비율 유지) 재위반이 생기지 않는다 —
                #    stop 모드의 삼각부등식과 동일 논증을 "쌍마다" 적용하는 것과 같다.
                #    보통(짝 하나만 근접)은 scale=1.0 그대로라 감속이 없다.
                scale = 1.0
                for d_k, other_path in neighbors.get(path, []):
                    r_hat_k = self._toward_heading(pos, pos_by[other_path], a, b)
                    r_dot_k = self._speed * (
                        direction[0] * r_hat_k[0] + direction[1] * r_hat_k[1] + direction[2] * r_hat_k[2])
                    if r_dot_k <= 0.0:
                        continue
                    allowed_k = max(0.0, (d_k - gap) / (2.0 * dt))
                    if r_dot_k > allowed_k:
                        scale = min(scale, allowed_k / r_dot_k)
                self._apply_horizontal_velocity(prim, path, speed=self._speed * scale)
            else:
                allowed = (d_near - gap) / (2.0 * dt)
                self._apply_horizontal_velocity(prim, path, speed=min(self._speed, max(0.0, allowed)))

    def _swerve_direction(self, direction: tuple, r_hat: tuple, allowed: float, a: int, b: int) -> tuple:
        """반경 성분(r̂ 방향)만 ``allowed``로 캡하고, 줄어든 만큼 접선 성분을 키워
        전체 속력(self._speed)을 보존한다 — swerve 근접 회피의 핵심 연산.

        ``direction``은 현재(이전 틱) 헤딩 단위벡터, ``r_hat``은 최근접 이웃을
        향하는 단위벡터. v = speed·direction을 r̂ 축으로 분해해 v_r(접근 성분,
        r̂ 방향 내적)과 그 나머지(접선 성분 크기 = sqrt(speed² - v_r²))로 나눈 뒤,
        v_r을 ``min(v_r, max(0, allowed))``로 캡한다 — 이미 멀어지는 중(v_r≤0)이면
        캡할 필요가 없어 그대로 반환한다(=depart 이후 자연 감쇠 없이 직진).

        접선 방향은 r̂에 수직인 두 후보(``_perp_horizontal`` 및 그 반대) 중 기존
        ``direction``과 내적이 더 큰(더 정렬된) 쪽을 골라 매 틱 같은 쪽으로만
        굽도록 한다 — 그렇지 않으면 좌/우가 틱마다 뒤집혀 진동하는 경로가 된다.
        """
        speed = self._speed
        if speed <= 1e-9:
            return direction
        r_dot = direction[0] * r_hat[0] + direction[1] * r_hat[1] + direction[2] * r_hat[2]
        r_dot *= speed
        if r_dot <= 0.0:
            return direction  # 이미 멀어지는 중 -- 캡 불필요, 헤딩 유지
        v_r = min(r_dot, max(0.0, allowed))
        if v_r >= r_dot - 1e-9:
            return direction  # 캡이 실제로 걸리지 않음(여유 충분)
        v_t_mag = max(0.0, speed * speed - v_r * v_r) ** 0.5
        tangent = self._perp_horizontal(r_hat, a, b)
        dot_pos = direction[0] * tangent[0] + direction[1] * tangent[1] + direction[2] * tangent[2]
        sign = 1.0 if dot_pos >= 0.0 else -1.0
        new_v = [v_r * r_hat[i] + sign * v_t_mag * tangent[i] for i in range(3)]
        norm = (new_v[0] ** 2 + new_v[1] ** 2 + new_v[2] ** 2) ** 0.5
        if norm <= 1e-9:
            return direction
        return tuple(c / norm for c in new_v)

    def _perp_horizontal(self, vec: tuple, a: int, b: int) -> tuple:
        """수평면에서 ``vec``을 90도 회전한 단위벡터. ``a``/``b``는 활성 수평 축
        인덱스(y-up이면 (0,2), z-up이면 (0,1)) — ``vec``이 이미 그 두 축만
        쓰는 단위벡터라는 전제이므로 회전 결과도 그대로 단위벡터다."""
        out = [0.0, 0.0, 0.0]
        out[a] = -vec[b]
        out[b] = vec[a]
        return tuple(out)

    def _toward_heading(self, pos_self, pos_other, a, b) -> tuple:
        """상대를 향하는 수평 단위 헤딩(접근 페이즈). 겹치면 임의 방향."""
        da = float(pos_other[a]) - float(pos_self[a])
        db = float(pos_other[b]) - float(pos_self[b])
        norm = (da * da + db * db) ** 0.5
        if norm <= 1e-9:
            return self._random_horizontal_direction()
        vec = [0.0, 0.0, 0.0]
        vec[a] = da / norm
        vec[b] = db / norm
        return tuple(vec)

    def _is_near_wall(self, prim) -> bool:
        """벽 margin 안인지(즉시 판정 — _check_wall_hug의 프레임 카운트 없는 버전)."""
        if self._bounds_center is None or self._bounds_half is None or self._wall_margin <= 0.0:
            return False
        pos = self._world_position(prim)
        if pos is None:
            return False
        a, b = self._horizontal_axes(prim.GetStage())
        nearest = min(
            self._bounds_half[a] - abs(float(pos[a]) - self._bounds_center[a]),
            self._bounds_half[b] - abs(float(pos[b]) - self._bounds_center[b]),
        )
        return nearest < self._wall_margin

    def _log_effective_speed(self, now: float) -> None:
        # 실효 속도 표기: 스텝마다 경로를 누적하고, 창이 차면 객체별 units/s 출력.
        for prim in self._valid_prims():
            p = str(prim.GetPath())
            pos = self._world_position(prim)
            if pos is None:
                continue
            last = self._speed_last_pos.get(p)
            if last is not None:
                d = sum((float(pos[i]) - float(last[i])) ** 2 for i in range(3)) ** 0.5
                self._speed_accum[p] = self._speed_accum.get(p, 0.0) + d
            self._speed_last_pos[p] = tuple(float(pos[i]) for i in range(3))
        span = now - self._speed_window_start
        if span >= self._speed_log_interval:
            if self._speed_accum:
                parts = ", ".join(
                    f"{p.rsplit('/', 1)[-1]} {self._speed_accum[p] / span:.0f}"
                    for p in sorted(self._speed_accum))
                self._log_warn(
                    f"[Wander] 실효속도 units/s (지시 {self._speed:g}, 창 {span:.1f}s): {parts}")
            self._speed_accum.clear()
            self._speed_window_start = now

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

    def _apply_horizontal_velocity(self, prim, prim_path: str, speed=None) -> None:
        direction = self._direction.get(prim_path)
        if direction is None:
            return
        # speed 지정 시 그 크기로 — near-miss의 감속(gap 앞 제동)이 쓰는 경로.
        mag = self._speed if speed is None else max(0.0, float(speed))
        current = self._get_velocity(prim)
        vertical_idx = 1 if self._is_y_up(prim.GetStage()) else 2
        current_v = [0.0, 0.0, 0.0]
        if current is not None:
            current_v = [float(current[0]), float(current[1]), float(current[2])]
        new_v = [direction[i] * mag for i in range(3)]
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
