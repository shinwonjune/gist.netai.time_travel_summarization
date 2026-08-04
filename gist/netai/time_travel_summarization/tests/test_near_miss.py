"""near-miss 안무(§7-2 충돌 단서 위상 분해) 검증 — GT 0건의 근거를 코드로 고정한다.

두 층을 본다:
  1) WanderController._near_miss_step — 속도 제어만으로 "중심거리 gap 아래로 내려가지
     않는다"는 불변식이 실제로 지켜지는지(가짜 프림 + 수치적분 시뮬레이션).
  2) generate_episodes.check_near_miss_trace — 산출된 trace로 그 불변식과 "접근이
     실제로 일어났다"를 사후 판정하는 오프라인 체크.
"""
import math
import unittest

from gist.netai.time_travel_summarization.automation.generate_episodes import check_near_miss_trace
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
                 near_miss_mode: str = "swerve", **wc_kwargs):
        self.pos = {p: list(v) for p, v in positions.items()}
        self.dt = dt
        self.applied: dict = {}
        wc = WanderController(
            [_FakePrim(p) for p in self.pos], speed=speed, near_miss_gap=gap,
            near_miss_mode=near_miss_mode, near_miss_hold_s=1.0, near_miss_depart_s=2.0, seed=3,
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
        확인), 상한이 켜진 기본값에서는 상한 안에서 짝 방향으로 수렴한다."""
        far = {"/W/a": [0.0, 90.0, 0.0], "/W/b": [1000.0, 90.0, 0.0]}
        # (1) 조향률 상한 없음(turn_radius_frac=0) — 목표 헤딩이 곧 적용 헤딩
        sim = _Sim(dict(far), gap=95.0, near_miss_turn_radius_frac=0.0)
        sim.wc._initialize_directions()
        sim.wc._near_miss_step(0.0)
        self.assertAlmostEqual(sim.wc._direction["/W/a"][0], 1.0, places=6)   # a는 +x(b쪽)
        self.assertAlmostEqual(sim.wc._direction["/W/b"][0], -1.0, places=6)  # b는 -x(a쪽)
        self.assertEqual(sim.wc._nm_phase, "approach")

        # (2) 기본값 — 한 틱에 스냅하지는 않지만, 회피 반경 밖에서 짝 방향으로 수렴한다.
        sim2 = _Sim(dict(far), gap=95.0)
        sim2.wc._initialize_directions()
        for i in range(60):                    # 1초면 상한 각속도로 180도까지 돈다
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

        # (c) 근접 창(gap의 1.2배 이내) 동안 관측된 모든 적용 speed가 자유주행 속도의
        # 70% 이상 -- 감속 신호(=충돌 인상의 원인)가 없어야 한다는 것의 직접 증거.
        near_speeds = [v for i, d in enumerate(dists) if d <= gap * 1.2 for v in applied_log[i].values()]
        self.assertTrue(near_speeds, "근접 구간이 관측되지 않음 -- 임계값 재조정 필요")
        self.assertGreaterEqual(
            min(near_speeds), 0.7 * speed,
            f"근접 중 감속 발생(min={min(near_speeds):.1f}, 기준={0.7 * speed:.1f}) -- invisible wall 신호")

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


if __name__ == "__main__":
    unittest.main()
