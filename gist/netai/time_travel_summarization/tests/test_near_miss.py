"""near-miss 안무(§7-2 충돌 단서 위상 분해) 검증 — GT 0건의 근거를 코드로 고정한다.

세 층을 본다:
  1) WanderController._near_miss_step — 속도 제어만으로 "중심거리 gap 아래로 내려가지
     않는다"는 불변식이 실제로 지켜지는지(가짜 프림 + 수치적분 시뮬레이션).
  2) generate_episodes.check_near_miss_trace — 산출된 trace로 그 불변식과 "접근이
     실제로 일어났다"를 사후 판정하는 오프라인 체크.
  3) generate_episodes.near_miss_events / near_miss_diversity — 조우가 **어디서**
     일어났는지의 분포. 1·2는 조우 한 번의 안전성만 보고 "조우들이 방 중앙에서 같은
     기하로 반복된다"는 문제는 전혀 잡지 못했다(사람 눈으로 발견됐다). 그 축을 숫자로
     고정하는 층이다.
"""
import math
import random
import unittest

from gist.netai.time_travel_summarization.automation.generate_episodes import (
    check_near_miss_diversity, check_near_miss_trace, near_miss_diversity, near_miss_events,
    parse_trace_frames as parse_frames)
from gist.netai.time_travel_summarization.physics import WanderController


class _FakePrim:
    def __init__(self, path: str):
        self._path = path

    def IsValid(self):
        return True

    def GetPath(self):
        return self

    def __str__(self):
        return self._path

    def GetStage(self):
        return None


class _FixedVec:
    def __init__(self, v):
        self._v = v

    def __getitem__(self, i):
        return self._v[i]


class _Sim:
    """가짜 프림 + 오일러 적분: 컨트롤러가 고른 (heading, speed)를 그대로 위치에 반영."""

    def __init__(self, positions: dict, gap: float, speed: float = 240.0, dt: float = 1.0 / 60.0,
                 near_miss_mode: str = "swerve", seed: int = 3, **wc_kwargs):
        self.pos = {p: list(v) for p, v in positions.items()}
        self.dt = dt
        self.applied: dict = {}
        wc = WanderController(
            [_FakePrim(p) for p in self.pos], speed=speed, near_miss_gap=gap,
            near_miss_mode=near_miss_mode, near_miss_hold_s=1.0, near_miss_depart_s=2.0, seed=seed,
            **wc_kwargs,
        )
        wc._world_position = lambda prim: _FixedVec(tuple(self.pos[str(prim.GetPath())]))
        wc._last_dt = dt
        wc._apply_horizontal_velocity = self._record
        wc._set_all_motion_zero = lambda prim, zero=None: self.applied.__setitem__(str(prim.GetPath()), 0.0)
        self.wc = wc

    def _record(self, prim, prim_path, speed=None):
        self.applied[prim_path] = self.wc.get_speed() if speed is None else float(speed)

    def min_distance(self) -> float:
        paths = sorted(self.pos)
        return min(
            ((self.pos[a][0] - self.pos[b][0]) ** 2 + (self.pos[a][2] - self.pos[b][2]) ** 2) ** 0.5
            for i, a in enumerate(paths) for b in paths[i + 1:]
        )

    def run_tracked(self, steps: int):
        """run()의 내부 구현 겸 확장판 — 스텝별 (최소거리, 그 스텝에 적용된 speed dict)도
        함께 반환한다. swerve의 "근접 중에도 감속 없음"·"통과 후 재이격" 검증에 필요.

        v3의 조향률 검증용으로 스텝별 헤딩 각(rad)도 ``self.headings``에 남긴다 —
        반환 튜플을 늘리면 기존 호출부가 전부 바뀌므로 속성으로 둔다."""
        dists, applied_log, phases = [], [], set()
        self.headings = []
        # 30Hz trace와 같은 형태의 프레임도 남긴다(2스텝=1프레임) — 조우 지점 분포를
        # 재는 near_miss_diversity가 실제 trace와 같은 입력을 받게 하려는 것이다.
        self.frames: dict = {}
        for i in range(steps):
            now = i * self.dt
            self.applied.clear()
            self.wc._initialize_directions()
            self.wc._near_miss_step(now)
            phases.add(self.wc._nm_phase)
            for path, v in self.applied.items():
                d = self.wc._direction.get(path) or (0.0, 0.0, 0.0)
                for k in range(3):
                    self.pos[path][k] += d[k] * v * self.dt
            self.headings.append({p: math.atan2(d[2], d[0])
                                  for p in self.applied
                                  for d in [self.wc._direction.get(p) or (1.0, 0.0, 0.0)]})
            if i % 2 == 0:
                self.frames[f"t{i:06d}"] = {p: tuple(v) for p, v in self.pos.items()}
            dists.append(self.min_distance())
            applied_log.append(dict(self.applied))
        return dists, phases, applied_log

    def turn_rates_deg(self):
        """``run_tracked`` 뒤에 호출 — 객체별·스텝별 헤딩 변화량(deg) 목록."""
        out = []
        for i in range(1, len(self.headings)):
            prev, cur = self.headings[i - 1], self.headings[i]
            for p, ang in cur.items():
                if p in prev:
                    out.append(math.degrees(abs((ang - prev[p] + math.pi) % (2 * math.pi) - math.pi)))
        return out

    def run(self, steps: int):
        """반환: (전 스텝 최소거리, 관측된 페이즈 집합)"""
        dists, phases, _ = self.run_tracked(steps)
        return (min(dists) if dists else float("inf")), phases


class NearMissChoreographyTest(unittest.TestCase):
    def test_disabled_by_default(self):
        self.assertEqual(WanderController(prims=[])._near_miss_gap, 0.0)
        self.assertEqual(WanderController(prims=[], near_miss_gap=95.0)._near_miss_gap, 95.0)

    def test_swerve_is_default_mode(self):
        # v1(stop)은 GUI 육안 검수에서 "보이지 않는 벽" 인상으로 기각되어 swerve가 기본이다.
        self.assertEqual(WanderController(prims=[])._near_miss_mode, "swerve")
        self.assertEqual(WanderController(prims=[], near_miss_mode="stop")._near_miss_mode, "stop")
        # 잘못된 값은 경고 후 swerve로 폴백(velocity_mode와 동일한 방어 패턴).
        self.assertEqual(WanderController(prims=[], near_miss_mode="bogus")._near_miss_mode, "swerve")

    def test_partners_pair_and_rotate(self):
        wc = WanderController(prims=[], near_miss_gap=95.0)
        paths = ["/W/a", "/W/b", "/W/c", "/W/d"]
        self.assertEqual(wc._near_miss_partners(paths),
                         {"/W/a": "/W/b", "/W/b": "/W/a", "/W/c": "/W/d", "/W/d": "/W/c"})
        wc._nm_cycle = 1                      # 한 칸 회전 → 다른 조합이 만난다
        self.assertEqual(wc._near_miss_partners(paths)["/W/b"], "/W/c")
        # 홀수면 정확히 1개가 짝 없이 남는다
        self.assertEqual(len(wc._near_miss_partners(["/W/a", "/W/b", "/W/c"])), 2)

    def test_approach_heading_points_at_partner(self):
        """approach 페이즈의 목표 헤딩은 짝 방향이다.

        v3에서 조향률 상한이 생기면서 "한 틱 만에 짝 방향으로 스냅"은 더 이상 성립하지
        않는다(초기 랜덤 헤딩에서 목표까지 상한 각속도로 돌아간다). 그래서 검증을 둘로
        나눈다 — 상한을 끄면 예전과 똑같이 정확히 짝을 향하고(목표 자체는 그대로라는
        확인), 상한이 켜진 기본값에서는 상한 안에서 짝 방향으로 수렴한다.

        v4의 대칭 파괴 두 가지는 여기서 꺼둔다. 접근 개시 지터는 "언제 조준을
        시작하는가"를, 비대칭 속도는 "얼마나 빨리 도는가(ω = v / R_min)"를 흔드는
        값이라, 켜두면 이 테스트가 재려는 성질(조준 목표가 짝 방향이라는 것, 그리고
        상한 안에서 거기로 수렴한다는 것)이 아니라 그 두 난수를 재게 된다. 지터와
        속도 비대칭 자체는 아래 전용 테스트에서 따로 검증한다."""
        far = {"/W/a": [0.0, 90.0, 0.0], "/W/b": [1000.0, 90.0, 0.0]}
        sym = dict(near_miss_start_jitter_s=0.0,
                   near_miss_speed_min_frac=1.0, near_miss_speed_max_frac=1.0)
        # (1) 조향률 상한 없음(turn_radius_frac=0) — 목표 헤딩이 곧 적용 헤딩
        sim = _Sim(dict(far), gap=95.0, near_miss_turn_radius_frac=0.0, **sym)
        sim.wc._initialize_directions()
        sim.wc._near_miss_step(0.0)
        self.assertAlmostEqual(sim.wc._direction["/W/a"][0], 1.0, places=6)   # a는 +x(b쪽)
        self.assertAlmostEqual(sim.wc._direction["/W/b"][0], -1.0, places=6)  # b는 -x(a쪽)
        self.assertEqual(sim.wc._nm_phase, "approach")

        # (2) 기본값 — 한 틱에 스냅하지는 않지만, 회피 반경 밖에서 짝 방향으로 수렴한다.
        sim2 = _Sim(dict(far), gap=95.0, **sym)
        sim2.wc._initialize_directions()
        for i in range(60):                    # 1초면 상한 각속도로 145도까지 돈다
            sim2.wc._near_miss_step(i * sim2.dt)
        self.assertGreater(sim2.wc._direction["/W/a"][0], 0.999)
        self.assertLess(sim2.wc._direction["/W/b"][0], -0.999)

    def test_v3_steering_rate_stays_under_cap(self):
        """v3 조향률 상한: 틱당 헤딩 변화가 상한(= speed / 최소 선회 반경 × dt) 이하.

        v2가 GUI에서 기각된 직접 원인이 "gap 코앞에서 한두 틱에 일어나는 급선회"였으므로,
        그 급선회가 없다는 것을 곡률의 상한으로 고정한다. 상한을 넘을 수 있는 유일한
        경로는 불변식을 지키는 하드 캡(``_swerve_direction``)과 응급 이탈인데, 둘 다
        1:1 스침에서는 발동하지 않아야 한다(발동한다면 회피가 너무 늦게 시작했다는 뜻)."""
        gap = 95.0
        sim = _Sim({"/W/a": [0.0, 90.0, 0.0], "/W/b": [900.0, 90.0, 0.0]}, gap=gap)
        sim.run_tracked(600)
        cap_deg = math.degrees(sim.wc._near_miss_turn_rate() * sim.dt)
        self.assertGreater(cap_deg, 0.0, "상한이 계산되지 않음")
        worst = max(sim.turn_rates_deg())
        self.assertLessEqual(worst, cap_deg * 1.01,
                             f"급선회 잔존: {worst:.2f}deg/tick > 상한 {cap_deg:.2f}")

    def test_v3_steering_rate_scales_with_turn_radius(self):
        # 상한은 gap 배수로 준 최소 선회 반경에서 유도된다 — 반경 2배면 각속도 절반.
        base = WanderController(prims=[], near_miss_gap=95.0, speed=240.0,
                                near_miss_turn_radius_frac=1.0)
        wide = WanderController(prims=[], near_miss_gap=95.0, speed=240.0,
                                near_miss_turn_radius_frac=2.0)
        self.assertAlmostEqual(base._near_miss_turn_rate(), 240.0 / 95.0, places=9)
        self.assertAlmostEqual(wide._near_miss_turn_rate(), base._near_miss_turn_rate() / 2.0)
        # 0이면 "상한 없음" — 목표 헤딩을 그대로 쓴다.
        self.assertEqual(
            WanderController(prims=[], near_miss_gap=95.0, near_miss_turn_radius_frac=0.0)
            ._near_miss_turn_rate(), 0.0)

    def test_v3_avoidance_begins_far_outside_gap(self):
        """v3 회피 개시 반경: gap의 2배보다 먼 거리에서 이미 직진 경로를 벗어난다.

        v2는 반경 캡이 gap 코앞에서야 걸려 그때까지 정면으로 직진했다 — "미리 피해
        간다"는 인상이 안 생긴 이유다. 여기서는 두 객체를 정확히 마주보게 놓고(초기
        랜덤 헤딩을 지워 편차의 원인을 회피 하나로 좁힌다) 진행축을 벗어난 성분이
        처음 생기는 순간의 쌍 거리를 잰다."""
        gap = 95.0
        sim = _Sim({"/W/a": [0.0, 90.0, 0.0], "/W/b": [1200.0, 90.0, 0.0]}, gap=gap)
        sim.wc._direction["/W/a"] = (1.0, 0.0, 0.0)     # 서로 정면 — 직진이면 z 성분 0
        sim.wc._direction["/W/b"] = (-1.0, 0.0, 0.0)
        first_dev_dist = None
        for i in range(400):
            sim.applied.clear()
            sim.wc._initialize_directions()
            sim.wc._near_miss_step(i * sim.dt)
            if first_dev_dist is None and abs(sim.wc._direction["/W/a"][2]) > 1e-9:
                first_dev_dist = sim.min_distance()
            for path, v in sim.applied.items():
                d = sim.wc._direction.get(path) or (0.0, 0.0, 0.0)
                for k in range(3):
                    sim.pos[path][k] += d[k] * v * sim.dt
        self.assertIsNotNone(first_dev_dist, "회피가 전혀 시작되지 않음")
        self.assertGreaterEqual(
            first_dev_dist, 2.0 * gap,
            f"회피 개시가 너무 늦음: d={first_dev_dist:.1f} (gap 2배={2*gap:.1f} 이상이어야)")

    def test_v3_tunables_resolve_from_env(self):
        # GUI 육안 검수에서 완만함을 되풀이 조정하므로 코드 수정 없이 env로 돌아가야 한다.
        # 우선순위는 명시 인자 > env > 기본값.
        import os

        keys = ("TTS_NEAR_MISS_AVOID_FRAC", "TTS_NEAR_MISS_TURN_RADIUS_FRAC", "TTS_NEAR_MISS_AIM_FRAC")
        saved = {k: os.environ.get(k) for k in keys}
        try:
            os.environ.update({keys[0]: "4.5", keys[1]: "2.5", keys[2]: "1.2"})
            wc = WanderController(prims=[], near_miss_gap=95.0)
            self.assertEqual((wc._nm_avoid_frac, wc._nm_turn_radius_frac, wc._nm_aim_frac),
                             (4.5, 2.5, 1.2))
            # 명시 인자가 env를 이긴다
            self.assertEqual(WanderController(prims=[], near_miss_avoid_frac=6.0)._nm_avoid_frac, 6.0)
            # 숫자가 아니면 경고 후 기본값
            os.environ[keys[1]] = "not-a-number"
            self.assertEqual(WanderController(prims=[])._nm_turn_radius_frac,
                             WanderController._NEAR_MISS_TURN_RADIUS_FRAC)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_gap_invariant_holds_through_full_cycles(self):
        # 4객체 20초, stop 모드: 접근 → 정지 → 이탈을 여러 사이클 돌아도 gap 아래로 붙지 않아야 한다.
        sim = _Sim({"/W/a": [0.0, 90.0, 0.0], "/W/b": [900.0, 90.0, 0.0],
                    "/W/c": [0.0, 90.0, 900.0], "/W/d": [900.0, 90.0, 900.0]}, gap=95.0,
                    near_miss_mode="stop")
        worst, phases = sim.run(1200)
        self.assertGreaterEqual(worst, 95.0 - 1e-6, f"gap 침범: {worst}")
        self.assertLessEqual(worst, 95.0 * 1.1, f"접근이 안 일어남: {worst}")
        self.assertEqual(phases, {"approach", "hold", "depart"})

    def test_holds_still_during_hold_phase(self):
        sim = _Sim({"/W/a": [0.0, 90.0, 0.0], "/W/b": [200.0, 90.0, 0.0]}, gap=95.0,
                    near_miss_mode="stop")
        sim.run(300)                     # 도착 후 hold에 진입할 만큼 진행
        sim.wc._nm_phase = "hold"
        sim.wc._nm_phase_until = 1e9
        sim.applied.clear()
        sim.wc._near_miss_step(10.0)
        self.assertEqual(set(sim.applied.values()), {0.0})

    def test_swerve_gap_invariant_holds_through_full_cycles(self):
        # swerve도 같은 반경-캡 논증으로 동일 불변식을 지켜야 한다(§ _near_miss_step 독스트링).
        sim = _Sim({"/W/a": [0.0, 90.0, 0.0], "/W/b": [900.0, 90.0, 0.0],
                    "/W/c": [0.0, 90.0, 900.0], "/W/d": [900.0, 90.0, 900.0]}, gap=95.0,
                    near_miss_mode="swerve")
        worst, phases = sim.run(1200)
        self.assertGreaterEqual(worst, 95.0 - 1e-6, f"gap 침범: {worst}")
        self.assertLessEqual(worst, 95.0 * 1.1, f"접근이 안 일어남: {worst}")
        # swerve는 정지 페이즈가 없다 -- "보이지 않는 벽" 인상의 근원을 없앤 지점.
        self.assertNotIn("hold", phases)
        self.assertIn("approach", phases)
        self.assertIn("depart", phases)

    def test_swerve_maintains_speed_and_passes_through(self):
        """swerve 안무의 핵심 성질(anti-"invisible wall") 3종을 한 번에 검증한다:
        (a) 불변식 유지, (b) 실제 근접 발생, (c) 근접 중에도 감속 없음(70% 문턱),
        (d) 최근접 이후 재이격(정지 페이즈 없이 실제로 스쳐 지나감)."""
        gap = 95.0
        sim = _Sim({"/W/a": [0.0, 90.0, 0.0], "/W/b": [900.0, 90.0, 0.0]}, gap=gap,
                    near_miss_mode="swerve")
        speed = sim.wc.get_speed()
        dists, phases, applied_log = sim.run_tracked(600)

        # (a) 어떤 순간도 gap 아래로 붙지 않는다.
        self.assertGreaterEqual(min(dists), gap - 1e-6, f"gap 침범: {min(dists)}")
        # (b) 실제로 gap 근처까지 접근이 일어난다(도착 없이 영원히 배회만 하지 않음).
        self.assertLessEqual(min(dists), gap * 1.1, f"접근이 안 일어남: {min(dists)}")
        self.assertNotIn("hold", phases, "swerve에 정지 페이즈가 있으면 안 됨")

        # (c) 근접 창(gap의 1.2배 이내) 동안 관측된 적용 speed가 그 객체의 순항 속도의
        # 70% 이상 -- 감속 신호(=충돌 인상의 원인)가 없어야 한다는 것의 직접 증거.
        # v4에서 순항 속도가 객체마다 달라졌으므로 기준선을 "지시 속도"가 아니라
        # "그 객체 자신의 순항 속도"(_speed_for)로 바꾼다. 재려는 성질은 그대로다 --
        # 애초에 이 검사가 잡으려는 것은 "제 속도로 가던 놈이 근접에서 느려졌는가"이고,
        # 지시 속도를 기준으로 두면 v4에서는 감속이 아니라 추첨된 순항 속도의 낮음을
        # 잡아버린다(하한 0.7배와 문턱 0.7배가 정확히 겹쳐 우연에 기대는 검사가 된다).
        near_speeds = [(v, sim.wc._speed_for(p))
                       for i, d in enumerate(dists) if d <= gap * 1.2
                       for p, v in applied_log[i].items()]
        self.assertTrue(near_speeds, "근접 구간이 관측되지 않음 -- 임계값 재조정 필요")
        worst_ratio = min(v / cruise for v, cruise in near_speeds)
        self.assertGreaterEqual(
            worst_ratio, 0.7,
            f"근접 중 감속 발생(순항 대비 {worst_ratio:.2f}배, 기준 0.70) -- invisible wall 신호")
        # 그리고 순항 속도 자체는 지시 속도를 넘지 않는다(비율 상한 1.0 클램프의 확인).
        self.assertLessEqual(max(cruise for _, cruise in near_speeds), speed + 1e-9)

        # (d) 최근접 이후 다시 벌어진다 -- 정지·반전 없이 실제로 스쳐 지나감.
        min_i = dists.index(min(dists))
        self.assertLess(min_i, len(dists) - 1, "최근접이 마지막 스텝이라 통과를 관측할 수 없음")
        self.assertGreater(max(dists[min_i:]), dists[min_i] + gap * 0.2,
                            "최근접 후 재이격이 없음(스침 실패 -- 궤도 돌기 등 의심)")

    def test_swerve_invariant_holds_with_multiple_simultaneous_pairs(self):
        """회귀 테스트: 8객체가 동시에 여러 쌍으로 근접하는 상황(무작위 스트레스
        테스트로 찾은 실제 실패 사례를 고정 좌표로 재현)에서도 불변식이 깨지지
        않아야 한다. 이 시나리오는 두 가지를 검증한다 -- (1) swerve의 속도 적용이
        "최근접 하나"가 아니라 이웃 전체에 반경 캡 안전핀을 걸어야 하고, (2) "이미
        gap 안" 응급 이탈도 그 안전핀 없이 전속 이탈하면 제3의 이웃과 새 위반을
        만들 수 있다(둘 다 처음 구현에서는 놓쳤던 부분)."""
        gap = 95.0
        positions = {
            "/W/o0": [344.57460012897667, 90.0, 524.5008644784443],
            "/W/o1": [196.44136725704644, 90.0, 150.5966631672228],
            "/W/o2": [152.43635329453687, 90.0, 613.4860246077772],
            "/W/o3": [635.2967335430031, 90.0, 898.8687726569938],
            "/W/o4": [725.7453071766214, 90.0, 255.03439569476538],
            "/W/o5": [829.9618213575222, 90.0, 11.44048942059256],
            "/W/o6": [420.9842777192231, 90.0, 648.9259041035031],
            "/W/o7": [571.6592349412618, 90.0, 25.353309252895617],
        }
        sim = _Sim(positions, gap=gap, near_miss_mode="swerve")
        dists, _, _ = sim.run_tracked(1200)
        self.assertGreaterEqual(min(dists), gap - 1e-6, f"gap 침범: {min(dists)}")

    def test_recovers_when_spawned_inside_gap(self):
        # 스폰 이격 실패 등으로 이미 gap 안이면 전속 이탈로 불변식을 회복해야 한다.
        sim = _Sim({"/W/a": [0.0, 90.0, 0.0], "/W/b": [40.0, 90.0, 0.0]}, gap=95.0)
        start = sim.min_distance()
        for i in range(120):
            sim.applied.clear()
            sim.wc._initialize_directions()
            sim.wc._near_miss_step(i * sim.dt)
            for path, v in sim.applied.items():
                d = sim.wc._direction.get(path) or (0.0, 0.0, 0.0)
                for k in range(3):
                    sim.pos[path][k] += d[k] * v * sim.dt
        self.assertGreater(sim.min_distance(), start)
        self.assertGreaterEqual(sim.min_distance(), 95.0)


class NearMissDiversityTest(unittest.TestCase):
    """v4 대칭 파괴 — 조우가 방 중앙에서 같은 기하로 반복되던 문제의 회귀 방어.

    v3까지는 짝이 동시에·같은 속도로·서로를 정면 조준해 접근했기 때문에 두 궤적이
    거울상이 되고 최근접점이 두 스폰 위치의 중점에 고정됐다(스폰 구역이 방 중앙
    대칭이라 결국 방 중앙). 여기서는 대칭을 깨는 세 장치가 각각 의도대로 동작하는지,
    그리고 그 결과로 조우 지점 분포가 실제로 넓어지는지를 고정한다. 세 장치를 전부
    끈 설정이 v3의 안무이므로, 그 설정이 이 테스트들의 기준선 역할을 한다.
    """

    SYMMETRIC = dict(near_miss_start_jitter_s=0.0, near_miss_speed_min_frac=1.0,
                     near_miss_speed_max_frac=1.0, near_miss_depart_spread_deg=-1.0)

    def test_v4_tunables_resolve_from_env_and_clamp(self):
        import os

        keys = ("TTS_NEAR_MISS_START_JITTER_S", "TTS_NEAR_MISS_SPEED_MIN_FRAC",
                "TTS_NEAR_MISS_SPEED_MAX_FRAC", "TTS_NEAR_MISS_DEPART_SPREAD_DEG")
        saved = {k: os.environ.get(k) for k in keys}
        try:
            os.environ.update({keys[0]: "3.5", keys[1]: "0.4", keys[2]: "0.9", keys[3]: "45"})
            wc = WanderController(prims=[], near_miss_gap=95.0)
            self.assertEqual((wc._nm_start_jitter_s, wc._nm_speed_min_frac,
                              wc._nm_speed_max_frac, wc._nm_depart_spread_deg),
                             (3.5, 0.4, 0.9, 45.0))
            # 명시 인자가 env를 이긴다(기존 조향 파라미터와 같은 우선순위 규약).
            self.assertEqual(WanderController(prims=[], near_miss_start_jitter_s=0.0)
                             ._nm_start_jitter_s, 0.0)
            # 속도 비율 상한은 1.0을 넘길 수 없다 -- self._speed가 천장이라는 성질에
            # 조향률 상한(ω = v / R_min)과 하위 캡 계산들이 기대고 있기 때문이다.
            hot = WanderController(prims=[], near_miss_speed_min_frac=2.0, near_miss_speed_max_frac=3.0)
            self.assertEqual((hot._nm_speed_min_frac, hot._nm_speed_max_frac), (1.0, 1.0))
            # min > max로 주면 max가 min으로 끌어올려져 범위가 뒤집히지 않는다.
            inv = WanderController(prims=[], near_miss_speed_min_frac=0.8, near_miss_speed_max_frac=0.2)
            self.assertEqual((inv._nm_speed_min_frac, inv._nm_speed_max_frac), (0.8, 0.8))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_start_jitter_staggers_when_each_object_aims_at_its_mate(self):
        """A(접근 개시 지터): 접근 페이즈여도 객체마다 자기 차례가 와야 짝을 조준한다.

        조향률 상한을 꺼서(turn_radius_frac=0) "목표 헤딩 = 적용 헤딩"으로 만들면 조준
        시점이 헤딩에 그대로 드러난다. 두 객체를 멀리(1000) 떨어뜨려 회피 로직이 끼어
        들지 않게 하고, 먼저 도는 쪽의 차례와 나중 쪽의 차례 사이 시각에서 정확히 한
        쪽만 짝을 향하고 있는지를 본다."""
        far = {"/W/a": [0.0, 90.0, 0.0], "/W/b": [1000.0, 90.0, 0.0]}
        sim = _Sim(far, gap=95.0, seed=7, near_miss_turn_radius_frac=0.0,
                   near_miss_start_jitter_s=2.0, near_miss_depart_spread_deg=-1.0)
        sim.wc._initialize_directions()
        initial = dict(sim.wc._direction)
        sim.wc._near_miss_step(0.0)
        delays = dict(sim.wc._nm_approach_at)
        self.assertEqual(len(delays), 2)
        self.assertTrue(all(0.0 <= v <= 2.0 for v in delays.values()), delays)
        early, late = sorted(delays, key=lambda p: delays[p])
        self.assertGreater(delays[late] - delays[early], 0.1,
                           f"두 객체의 접근 개시 시각이 사실상 같다: {delays}")

        # 먼저 도는 쪽의 차례는 지났고 나중 쪽은 아직인 시각
        sim.wc._near_miss_step(0.5 * (delays[early] + delays[late]))
        toward_mate = 1.0 if early == "/W/a" else -1.0
        self.assertAlmostEqual(sim.wc._direction[early][0], toward_mate, places=6)
        self.assertEqual(sim.wc._direction[late], initial[late],
                         "차례가 오기 전인데 이미 짝을 조준했다(지터가 안 먹음)")
        # 나중 쪽의 차례가 지나면 그쪽도 짝을 향한다.
        sim.wc._near_miss_step(delays[late] + 0.01)
        self.assertAlmostEqual(sim.wc._direction[late][0], -toward_mate, places=6)

    def test_pair_cruise_speeds_are_drawn_independently_within_range(self):
        """B(비대칭 속도): 짝의 두 객체가 서로 다른 순항 속도를 갖고, 둘 다 범위 안이다."""
        sim = _Sim({"/W/a": [0.0, 90.0, 0.0], "/W/b": [900.0, 90.0, 0.0]}, gap=95.0, speed=240.0,
                   seed=5, near_miss_speed_min_frac=0.6, near_miss_speed_max_frac=1.0)
        sim.wc._near_miss_step(0.0)
        va, vb = sim.wc._speed_for("/W/a"), sim.wc._speed_for("/W/b")
        for v in (va, vb):
            self.assertGreaterEqual(v, 240.0 * 0.6 - 1e-9)
            self.assertLessEqual(v, 240.0 + 1e-9)
        self.assertNotAlmostEqual(va, vb, places=3, msg="두 객체 속도가 같다(독립 추출이 아님)")
        # 범위를 한 점으로 좁히면 비대칭이 꺼지고 둘 다 지시 속도가 된다.
        sym = _Sim({"/W/a": [0.0, 90.0, 0.0], "/W/b": [900.0, 90.0, 0.0]}, gap=95.0, speed=240.0,
                   **self.SYMMETRIC)
        sym.wc._near_miss_step(0.0)
        self.assertEqual(sym.wc._speed_for("/W/a"), 240.0)
        self.assertEqual(sym.wc._speed_for("/W/b"), 240.0)

    def test_depart_heading_is_random_within_cone_away_from_mate(self):
        """C(이탈 방향 무작위화): 이탈 목표는 짝의 반대 방향 ±spread 안에서 뽑힌다.

        부채꼴의 중심을 반대 방향으로 두는 이유는 완전 무작위면 방금 스친 상대 쪽으로
        되돌아가는 방향이 섞여 조우가 끝나지 않고 늘어지기 때문이다. 여기서는 목표가
        그 부채꼴 안이라는 것과, 두 객체가 서로 다른 방향을 뽑았다는 것(대칭 반대
        방향으로 갈라지는 v3와 달라졌다는 것)을 본다."""
        spread = 60.0
        sim = _Sim({"/W/a": [0.0, 90.0, 0.0], "/W/b": [900.0, 90.0, 0.0]}, gap=95.0, seed=9,
                   near_miss_depart_spread_deg=spread)
        for i in range(900):                       # 도착 후 depart로 넘어갈 때까지
            sim.applied.clear()
            sim.wc._initialize_directions()
            sim.wc._near_miss_step(i * sim.dt)
            for path, v in sim.applied.items():
                d = sim.wc._direction.get(path) or (0.0, 0.0, 0.0)
                for k in range(3):
                    sim.pos[path][k] += d[k] * v * sim.dt
            if sim.wc._nm_phase == "depart":
                break
        self.assertEqual(sim.wc._nm_phase, "depart", "이탈 페이즈에 도달하지 못함")
        self.assertEqual(len(sim.wc._nm_depart_dir), 2)
        angles = {}
        for path in ("/W/a", "/W/b"):
            other = "/W/b" if path == "/W/a" else "/W/a"
            away = sim.wc._away_heading(sim.pos[path], sim.pos[other], 0, 2, jitter_deg=0.0)
            off = math.degrees(abs(sim.wc._wrap_pi(
                sim.wc._horizontal_angle(sim.wc._nm_depart_dir[path], 0, 2)
                - sim.wc._horizontal_angle(away, 0, 2))))
            self.assertLessEqual(off, spread + 1e-6, f"{path} 이탈 목표가 부채꼴 밖: {off:.1f}deg")
            angles[path] = off
        self.assertNotAlmostEqual(angles["/W/a"], angles["/W/b"], places=3,
                                  msg="두 객체가 같은 각도로 이탈(대칭이 안 깨짐)")
        # spread를 끄면 이탈 재조준 자체를 하지 않는다(v3 동작 그대로).
        off_sim = _Sim({"/W/a": [0.0, 90.0, 0.0], "/W/b": [900.0, 90.0, 0.0]}, gap=95.0,
                       near_miss_depart_spread_deg=-1.0)
        off_sim.run_tracked(900)
        self.assertEqual(off_sim.wc._nm_depart_dir, {})

    def test_v4_keeps_gap_invariant_and_curvature_cap(self):
        """대칭 파괴가 GT 무오염(gap 불변식)도 곡률 상한도 건드리지 않는다.

        이것이 v4에서 가장 중요한 회귀 방어다. 불변식의 보증은 "각 객체가 자기 반경
        속도 성분을 (거리-gap)/(2·dt) 이하로 묶는다"는 형태라 두 객체의 속도가 달라도,
        어느 쪽이 언제 출발했어도 한 스텝의 접근량 합이 (거리-gap)을 넘지 못한다.
        곡률 쪽은 이탈 목표를 아무리 크게 꺾어도 실제 회전이 _rate_limit_heading을
        거치므로 틱당 헤딩 변화가 상한 아래에 머문다는 것이 요지다(v3의 GUI 승인
        근거였던 성질). 조향률 상한은 객체 속도에 비례하므로 가장 빠를 수 있는
        지시 속도 기준의 상한으로 검사하면 모든 객체를 덮는다.

        곡률 검사의 강도를 객체 수에 따라 다르게 두는 데는 이유가 있다. 회피 목표는
        반경 안 이웃 중 **하나**를 골라 접선을 잡으므로, 3객체 이상이 몰리면 고른
        이웃은 피하면서 다른 이웃과 가까워질 수 있고 그때는 gap 불변식을 지키는
        하드 캡(``_swerve_direction``)이 마지막에 개입해 상한을 넘는 회전을 한다.
        이것은 v4가 만든 문제가 아니라 v3부터 있던 한계이고(일지 #13 "남은 한계 ①"),
        near-miss 대조 데이터가 2객체 구성으로 생성되는 것도 그 때문이다. 그래서
        2객체(실제 생성 구성)에서는 상한을 엄격히 걸고, 4객체에서는 "대칭 기준선인
        v3보다 나빠지지 않았다"를 건다 — 실측으로는 오히려 개선됐다(같은 배치·시드에서
        상한 초과 틱 13/7196 → 3/7196, 최대 회전 93.7도 → 18.9도). 대칭이 깨지면서
        네 객체가 동시에 같은 지점으로 몰리는 상황 자체가 줄어든 효과로 보인다."""
        two = {"/W/a": [120.0, 90.0, 700.0], "/W/b": [800.0, 90.0, 160.0]}
        four = {"/W/a": [100.0, 90.0, 100.0], "/W/b": [800.0, 90.0, 120.0],
                "/W/c": [140.0, 90.0, 780.0], "/W/d": [820.0, 90.0, 800.0]}

        def run(positions, **kwargs):
            sim = _Sim({k: list(v) for k, v in positions.items()}, gap=95.0, seed=4, **kwargs)
            dists, _, _ = sim.run_tracked(1800)
            cap_deg = math.degrees(sim.wc._near_miss_turn_rate() * sim.dt)
            turns = sim.turn_rates_deg()
            return min(dists), max(turns), sum(1 for t in turns if t > cap_deg * 1.01), cap_deg

        min_d, worst_turn, _, cap = run(two)
        self.assertGreaterEqual(min_d, 95.0 - 1e-6, f"2객체 gap 침범: {min_d}")
        self.assertLessEqual(worst_turn, cap * 1.01,
                             f"2객체 급선회 잔존: {worst_turn:.2f}deg/tick > 상한 {cap:.2f}")

        v4_min_d, v4_worst, v4_over, _ = run(four)
        v3_min_d, v3_worst, v3_over, _ = run(four, **self.SYMMETRIC)
        self.assertGreaterEqual(v4_min_d, 95.0 - 1e-6, f"4객체 gap 침범: {v4_min_d}")
        self.assertGreaterEqual(v3_min_d, 95.0 - 1e-6, f"4객체(v3) gap 침범: {v3_min_d}")
        self.assertLessEqual(v4_over, v3_over,
                             f"4객체 상한 초과 틱이 v3보다 늘었다: {v4_over} > {v3_over}")
        self.assertLessEqual(v4_worst, v3_worst,
                             f"4객체 최대 회전이 v3보다 커졌다: {v4_worst:.1f} > {v3_worst:.1f}")

    def test_v4_is_seed_deterministic(self):
        """같은 시드 = 같은 에피소드. 대칭 파괴에 쓰는 난수가 전부 컨트롤러의 시드
        RNG에서 나오므로 재현성이 깨지지 않아야 한다(에피소드 재생성·디버깅의 전제)."""
        pos = {"/W/a": [150.0, 90.0, 640.0], "/W/b": [760.0, 90.0, 220.0]}
        a1 = _Sim(dict(pos), gap=95.0, seed=21)
        a2 = _Sim(dict(pos), gap=95.0, seed=21)
        b = _Sim(dict(pos), gap=95.0, seed=22)
        for s in (a1, a2, b):
            s.run_tracked(900)
        self.assertEqual(a1.pos, a2.pos, "같은 시드인데 결과가 다르다")
        self.assertNotEqual(a1.pos, b.pos, "시드를 바꿔도 결과가 같다(난수가 안 쓰임)")

    def test_encounters_scatter_more_than_symmetric_baseline(self):
        """핵심 지표: 조우 지점의 분포가 대칭 기준선(v3)보다 넓어진다.

        여러 시드의 에피소드를 돌려 조우 지점을 뽑고(near_miss_events), 흩어짐을
        near_miss_diversity로 잰다. 기준선은 세 장치를 전부 끈 설정 — 그 설정에서는
        짝이 동시에 같은 속도로 서로를 조준하므로 조우가 매 사이클 거의 같은 자리에서
        반복되어 RMS 반경이 0에 가깝게 나온다.

        여기(``_Sim``)는 벽이 없는 환경이라 대칭이 순수하게 드러나고 개선폭도 크게
        나온다. 실제 생성 조건(2객체·약 1400cm 방·속도 130·40초·벽 있음, 20시드
        스윕)에서는 벽에 닿아 중앙으로 redirect되는 것 자체가 약한 무작위화로 작용해
        기준선이 완전한 한 점은 아니고, 그 조건의 실측 개선폭은 흩어짐 42 → 232
        (5.5배), 조우 간 최소 이격 0.1 → 87, 사건 수 정규화 커버리지 26.7% → 62.0%다.
        문턱은 그 실측보다 한참 낮게 잡아 시드 조합에 따른 흔들림으로 깨지지 않게
        하되, "거의 한 점"과 "흩어짐"을 가르기에는 충분하다."""
        box = 900.0
        base_r, v4_r = [], []
        for seed in range(6):
            rng = random.Random(seed * 7919 + 13)
            pos = {f"/W/o{i}": [rng.uniform(90.0, 810.0), 90.0, rng.uniform(90.0, 810.0)]
                   for i in range(2)}
            for kwargs, sink in ((self.SYMMETRIC, base_r), ({}, v4_r)):
                sim = _Sim({k: list(v) for k, v in pos.items()}, gap=95.0, seed=seed, **kwargs)
                sim.run_tracked(1800)          # 30초
                sink.append(near_miss_diversity(sim.frames, 95.0,
                                                bounds=((0.0, 0.0), (box, box)), grid=4))

        def mean(rows, key):
            vals = [r[key] for r in rows if r[key] is not None]
            return sum(vals) / len(vals) if vals else 0.0

        base_spread, v4_spread = mean(base_r, "rms_radius"), mean(v4_r, "rms_radius")
        self.assertGreater(v4_spread, 3.0 * base_spread + 50.0,
                           f"조우 지점이 여전히 몰려 있다: v3={base_spread:.0f} v4={v4_spread:.0f}")
        # 사건 수로 정규화한 커버리지(사건이 서로 다른 칸에 떨어졌는가)도 올라간다.
        self.assertGreater(mean(v4_r, "coverage_eff"), mean(base_r, "coverage_eff"))
        # 그리고 조우 자체가 사라지지는 않는다 -- 흩어지기만 하고 안 만나면 무의미하다.
        self.assertGreater(mean(v4_r, "events"), 2.0)


class NearMissTraceCheckTest(unittest.TestCase):
    @staticmethod
    def _trace(dists) -> str:
        out = ["timestamp,objid,x,y,z"]
        for i, d in enumerate(dists):
            ts = f"2026-07-28 10:00:{i // 10:02d}.{(i % 10) * 100:03d}"
            out.append(f"{ts},obj001,0.000,90.000,0.000")
            out.append(f"{ts},obj002,{d:.3f},90.000,0.000")
        return "\n".join(out) + "\n"

    def test_pass_requires_both_no_contact_and_real_approach(self):
        res = check_near_miss_trace(self._trace([400, 200, 97, 200, 400]), gap=95.0)
        self.assertTrue(res["ok"])
        self.assertEqual(res["min_dist"], 97.0)
        self.assertEqual(res["approached"], 1)

    def test_contact_is_reported_with_pair(self):
        res = check_near_miss_trace(self._trace([400, 60, 400]), gap=95.0)
        self.assertFalse(res["ok"])
        self.assertEqual(res["violations"], [{"pair": ("obj001", "obj002"), "min_dist": 60.0}])

    def test_no_approach_fails(self):
        res = check_near_miss_trace(self._trace([400, 300, 400]), gap=95.0)
        self.assertFalse(res["ok"])
        self.assertEqual(res["approached"], 0)
        self.assertEqual(res["violations"], [])

    def test_sampling_tolerance(self):
        # 30Hz가 최근접 순간을 놓쳐 gap을 tol만큼 밑도는 것은 통과, 그 밖은 실패
        self.assertTrue(check_near_miss_trace(self._trace([400, 93.5, 400]), gap=95.0, tol=2.0)["ok"])
        self.assertFalse(check_near_miss_trace(self._trace([400, 92.0, 400]), gap=95.0, tol=2.0)["ok"])

    def test_empty_trace_is_not_ok(self):
        res = check_near_miss_trace("timestamp,objid,x,y,z\n", gap=95.0)
        self.assertFalse(res["ok"])
        self.assertIsNone(res["min_pair"])


class NearMissEventStatsTest(unittest.TestCase):
    """조우 사건 추출·다양성 지표의 순수 함수 검증(trace 텍스트만으로 판정)."""

    @staticmethod
    def _trace(points) -> str:
        """``points``는 프레임별 ``{objid: (x, y, z)}`` 목록."""
        out = ["timestamp,objid,x,y,z"]
        for i, objs in enumerate(points):
            ts = f"2026-07-28 10:{i // 600:02d}:{(i // 10) % 60:02d}.{(i % 10) * 100:03d}"
            for objid in sorted(objs):
                x, y, z = objs[objid]
                out.append(f"{ts},{objid},{x:.3f},{y:.3f},{z:.3f}")
        return "\n".join(out) + "\n"

    def _pass_by(self, at, sep_min, n=40):
        """한 객체는 ``at``에 고정, 다른 객체가 다가왔다 멀어지는 프레임 목록."""
        pts = []
        for i in range(n):
            d = sep_min + abs(i - n // 2) * 30.0
            pts.append({"obj001": (at[0], 90.0, at[1]),
                        "obj002": (at[0] + d, 90.0, at[1])})
        return pts

    def test_event_is_the_local_minimum_at_the_pair_midpoint(self):
        text = self._trace(self._pass_by((300.0, 400.0), 100.0))
        events = near_miss_events(parse_frames(text), gap=95.0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["pair"], ("obj001", "obj002"))
        self.assertAlmostEqual(events[0]["dist"], 100.0, places=3)
        # 위치는 두 객체의 중점 — 어느 쪽에서 보느냐에 따라 어긋나지 않게.
        self.assertAlmostEqual(events[0]["pos"][0], 350.0, places=3)
        self.assertAlmostEqual(events[0]["pos"][2], 400.0, places=3)

    def test_far_approach_is_not_an_event(self):
        # 문턱(gap × event_frac = 190) 밖에서만 오가면 조우로 세지 않는다.
        text = self._trace(self._pass_by((300.0, 400.0), 400.0))
        self.assertEqual(near_miss_events(parse_frames(text), gap=95.0), [])

    def test_refractory_merges_one_encounter_sampled_as_several_dips(self):
        # 한 번의 스침이 좌표 흔들림으로 극소점 두 개로 잡히는 상황 — 불응기가 합친다.
        pts = []
        for d in (200.0, 150.0, 100.0, 101.0, 100.5, 150.0, 200.0):
            pts.append({"obj001": (0.0, 90.0, 0.0), "obj002": (d, 90.0, 0.0)})
        frames = parse_frames(self._trace(pts))
        self.assertEqual(len(near_miss_events(frames, gap=95.0, refractory=30)), 1)
        # 불응기를 없애면 둘 다 별개 사건으로 잡힌다(장치가 실제로 일하고 있다는 확인).
        self.assertEqual(len(near_miss_events(frames, gap=95.0, refractory=1)), 2)

    def test_repeated_encounters_at_one_spot_score_far_lower_than_scattered(self):
        """지표의 본질 검증 — 같은 자리 반복과 흩어진 조우를 실제로 가른다.

        v3의 실패 양상(방 중앙에서 같은 조우 반복)과 v4가 노리는 양상(방 곳곳)을
        합성 trace로 만들어 두 지표가 반대 방향을 가리키는지 본다."""
        room = ((0.0, 0.0), (900.0, 900.0))
        same = [f for _ in range(4) for f in self._pass_by((450.0, 450.0), 100.0)]
        spread = [f for spot in ((150.0, 150.0), (700.0, 200.0), (200.0, 750.0), (750.0, 700.0))
                  for f in self._pass_by(spot, 100.0)]
        a = check_near_miss_diversity(self._trace(same), gap=95.0, bounds=room)
        b = check_near_miss_diversity(self._trace(spread), gap=95.0, bounds=room)
        self.assertEqual((a["events"], b["events"]), (4, 4))
        self.assertEqual(a["rms_radius"], 0.0)          # 네 조우가 완전히 같은 자리
        self.assertEqual(a["min_sep"], 0.0)
        self.assertGreater(b["rms_radius"], 300.0)
        self.assertGreater(b["min_sep"], 300.0)
        # 사건 수가 같으므로 커버리지도 그대로 비교 가능하다(1칸 vs 4칸 / 16칸).
        self.assertEqual((a["coverage"], b["coverage"]), (0.0625, 0.25))
        self.assertEqual((a["coverage_eff"], b["coverage_eff"]), (0.25, 1.0))

    def test_bounds_default_to_the_traced_extent(self):
        text = self._trace(self._pass_by((300.0, 400.0), 100.0))
        stats = check_near_miss_diversity(text, gap=95.0)
        self.assertEqual(stats["events"], 1)
        # 경계를 안 주면 궤적의 외접 사각형에서 유도 — 방 크기가 0이 되지 않는다.
        self.assertGreater(stats["room"][0], 0.0)
        self.assertIsNone(stats["min_sep"])             # 사건이 하나면 이격이 정의 안 됨

    def test_empty_trace_yields_zero_events(self):
        stats = check_near_miss_diversity("timestamp,objid,x,y,z\n", gap=95.0)
        self.assertEqual(stats["events"], 0)
        self.assertIsNone(stats["rms_radius"])


if __name__ == "__main__":
    unittest.main()
