import tempfile
import unittest
import csv as _csv
from pathlib import Path

from gist.netai.time_travel_summarization.physics import CollisionRecorder, WanderController


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


class WanderStuckTest(unittest.TestCase):
    def test_velocity_mode_param(self):
        wc = WanderController(prims=[], velocity_mode="on_enter")
        self.assertEqual(wc._velocity_mode, "on_enter")

    def test_invalid_velocity_mode_falls_back(self):
        wc = WanderController(prims=[], velocity_mode="bogus")
        self.assertEqual(wc._velocity_mode, "horizontal_per_tick")

    def test_velocity_mode_default_is_horizontal_per_tick(self):
        wc = WanderController(prims=[])
        self.assertEqual(wc._velocity_mode, "horizontal_per_tick")

    def test_set_velocity_mode(self):
        wc = WanderController(prims=[])
        self.assertTrue(wc.set_velocity_mode("on_enter"))
        self.assertEqual(wc._velocity_mode, "on_enter")
        self.assertFalse(wc.set_velocity_mode("nope"))

    def test_set_speed(self):
        wc = WanderController(prims=[], speed=120.0)
        self.assertEqual(wc.get_speed(), 120.0)
        self.assertTrue(wc.set_speed(80.0))
        self.assertEqual(wc.get_speed(), 80.0)
        self.assertFalse(wc.set_speed(0.0))
        self.assertEqual(wc.get_speed(), 80.0)

    def test_check_stuck_triggers_after_k_frames(self):
        prim = _FakePrim("/W/test")
        wc = WanderController(prims=[prim], stuck_frames=3, stuck_ratio=0.3)
        wc._direction[str(prim)] = (1.0, 0.0, 0.0)

        fixed = _FixedVec((0.0, 0.0, 0.0))
        wc._world_position = lambda p: fixed
        # first call: stores baseline, returns False
        self.assertFalse(wc._check_stuck(prim, str(prim), 1.0))
        # counter increments: 1, 2, 3 -> triggers at 3
        self.assertFalse(wc._check_stuck(prim, str(prim), 1.016))
        self.assertFalse(wc._check_stuck(prim, str(prim), 1.032))
        self.assertTrue(wc._check_stuck(prim, str(prim), 1.048))

    def test_check_stuck_resets_on_progress(self):
        prim = _FakePrim("/W/move")
        wc = WanderController(prims=[prim], stuck_frames=3, stuck_ratio=0.3, speed=120.0)
        wc._direction[str(prim)] = (1.0, 0.0, 0.0)
        positions = [(0, 0, 0), (0, 0, 0), (10.0, 0, 0)]
        idx = {"i": 0}

        def _wp(p):
            v = positions[idx["i"]]
            idx["i"] = min(idx["i"] + 1, len(positions) - 1)
            return _FixedVec(v)

        wc._world_position = _wp
        wc._check_stuck(prim, str(prim), 1.0)    # baseline
        wc._check_stuck(prim, str(prim), 1.016)  # stuck +1
        wc._check_stuck(prim, str(prim), 1.032)  # progress=10 -> reset
        self.assertEqual(wc._stuck_count[str(prim)], 0)

    def test_random_direction_avoids_blocked(self):
        wc = WanderController(prims=[])
        avoid = (1.0, 0.0, 0.0)
        for _ in range(100):
            d = wc._random_horizontal_direction(stage=None, avoid_dir=avoid)
            dot = d[0] * avoid[0] + d[1] * avoid[1] + d[2] * avoid[2]
            # 정상 reject 결과는 dot <= 0.5
            # 5회 reject 실패 fallback은 정확히 -avoid (dot = -1.0)
            self.assertTrue(dot <= 0.5 or dot == -1.0, f"unexpected dir={d} dot={dot}")

    def test_set_kinematic_safe_without_pxr(self):
        # pxr import 실패 환경 — 그냥 호출만 하고 throw 안 하면 OK
        wc = WanderController(prims=[])

        class _DummyPrim:
            def GetPath(self):
                return "/dummy"

        try:
            wc._set_kinematic(_DummyPrim(), True)
            wc._set_kinematic(_DummyPrim(), False)
        except Exception as e:
            self.fail(f"_set_kinematic raised in non-pxr environment: {e}")

    def test_redirect_invokes_collision_callback(self):
        prim = _FakePrim("/W/hit")
        events = []
        wc = WanderController(prims=[prim], on_collision=lambda p, pos, kind: events.append((p, pos, kind)))
        wc._apply_current_velocity = lambda *a, **k: None  # avoid pxr in velocity path
        wc._world_position = lambda p: _FixedVec((1.0, 2.0, 3.0))
        wc._redirect(prim, str(prim), kind="stuck")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "/W/hit")
        self.assertEqual(events[0][1], (1.0, 2.0, 3.0))
        self.assertEqual(events[0][2], "stuck")

    def test_redirect_cooldown_suppresses_rapid_repeats(self):
        prim = _FakePrim("/W/cd")
        events = []
        wc = WanderController(
            prims=[prim], collision_cooldown_s=999.0, on_collision=lambda *a: events.append(a)
        )
        wc._apply_current_velocity = lambda *a, **k: None
        wc._world_position = lambda p: None
        wc._redirect(prim, str(prim), "stuck")
        wc._redirect(prim, str(prim), "contact")  # within cooldown -> suppressed
        self.assertEqual(len(events), 1)

    def test_redirect_changes_heading(self):
        prim = _FakePrim("/W/turn")
        wc = WanderController(prims=[prim])
        wc._apply_current_velocity = lambda *a, **k: None
        wc._world_position = lambda p: None
        wc._direction[str(prim)] = (1.0, 0.0, 0.0)
        wc._last_blocked_direction[str(prim)] = (1.0, 0.0, 0.0)
        wc._redirect(prim, str(prim), "stuck")
        new_dir = wc._direction[str(prim)]
        dot = new_dir[0] * 1.0
        self.assertTrue(dot <= 0.5 or dot == -1.0, f"heading not redirected: {new_dir}")

    def test_wall_hug_triggers_near_boundary(self):
        prim = _FakePrim("/W/wall")
        wc = WanderController(
            prims=[prim], bounds_center=(0.0, 0.0, 0.0), bounds_half=(10.0, 5.0, 10.0),
            wall_margin=1.0, wall_frames=2,
        )
        # x=9.5 → +x 벽까지 거리 0.5 < margin 1.0
        wc._world_position = lambda p: _FixedVec((9.5, 0.0, 0.0))
        self.assertFalse(wc._check_wall_hug(prim, str(prim)))  # count=1
        self.assertTrue(wc._check_wall_hug(prim, str(prim)))   # count=2 → trigger

    def test_wall_hug_resets_in_open_space(self):
        prim = _FakePrim("/W/open")
        wc = WanderController(
            prims=[prim], bounds_center=(0.0, 0.0, 0.0), bounds_half=(10.0, 5.0, 10.0),
            wall_margin=1.0, wall_frames=2,
        )
        wc._world_position = lambda p: _FixedVec((0.0, 0.0, 0.0))  # 중앙, 벽에서 멀다
        self.assertFalse(wc._check_wall_hug(prim, str(prim)))
        self.assertEqual(wc._wall_count[str(prim)], 0)

    def test_wall_hug_disabled_without_bounds(self):
        prim = _FakePrim("/W/nb")
        wc = WanderController(prims=[prim])  # bounds 미전달
        wc._world_position = lambda p: _FixedVec((100.0, 0.0, 0.0))
        self.assertFalse(wc._check_wall_hug(prim, str(prim)))

    def test_heading_to_center_points_inward(self):
        prim = _FakePrim("/W/h")
        wc = WanderController(prims=[prim], bounds_center=(0.0, 0.0, 0.0), bounds_half=(10.0, 5.0, 10.0))
        wc._world_position = lambda p: _FixedVec((9.0, 0.0, 0.0))  # +x 벽 근처
        for _ in range(50):
            h = wc._heading_to_center(prim)
            # 중앙(원점)은 -x 방향 → x 성분은 항상 음수(±35° jitter 안에서도)
            self.assertLess(h[0], 0.0, f"heading not inward: {h}")


class ObjectCollisionTest(unittest.TestCase):
    def _wc(self, posmap, **kw):
        prims = [_FakePrim(p) for p in posmap]
        wc = WanderController(prims=prims, collision_distance=kw.pop("collision_distance", 1.0), **kw)
        wc._world_position = lambda p: _FixedVec(posmap[str(p)])
        return wc, prims

    def test_pairwise_collision_pauses_and_redirects_both(self):
        events = []
        wc, _ = self._wc(
            {"/W/a": (0.0, 0.0, 0.0), "/W/b": (0.5, 0.0, 0.0)},  # 0.5 < 1.0 → 충돌
            on_collision=lambda *a: events.append(a),
        )
        wc._handle_object_collisions(100.0)
        self.assertIn("/W/a", wc._paused_until)
        self.assertIn("/W/b", wc._paused_until)
        # 서로 반대 방향(a는 -x, b는 +x)으로 재출발
        self.assertLess(wc._redirect_heading["/W/a"][0], 0.0)
        self.assertGreater(wc._redirect_heading["/W/b"][0], 0.0)
        self.assertEqual(len(events), 2)

    def test_far_apart_no_collision(self):
        wc, _ = self._wc({"/W/a": (0.0, 0.0, 0.0), "/W/b": (5.0, 0.0, 0.0)})
        wc._handle_object_collisions(100.0)
        self.assertEqual(wc._paused_until, {})

    def test_collision_disabled_when_distance_zero(self):
        wc, _ = self._wc({"/W/a": (0.0, 0.0, 0.0), "/W/b": (0.1, 0.0, 0.0)}, collision_distance=0.0)
        wc._handle_object_collisions(100.0)
        self.assertEqual(wc._paused_until, {})

    def test_no_retrigger_during_post_pause_cooldown(self):
        # pause가 끝나도 cooldown 동안은 재발동 금지 → 서로 멀어질 시간 확보(무한 정지 방지).
        wc, _ = self._wc(
            {"/W/a": (0.0, 0.0, 0.0), "/W/b": (0.5, 0.0, 0.0)},
            collision_cooldown_s=1.0, collision_impact_s=0.0, collision_pause_s=0.5,
        )
        wc._handle_object_collisions(100.0)             # 최초 충돌: pause_until=100.5
        wc._redirect_heading.clear()                    # 재발동 감지용 초기화
        wc._handle_object_collisions(100.6)             # pause 끝났지만 guard=101.5 → 재발동 X
        self.assertEqual(wc._redirect_heading, {})
        wc._handle_object_collisions(101.6)             # cooldown 경과 + 여전히 근접 → 재발동 O
        self.assertIn("/W/a", wc._redirect_heading)

    def test_already_paused_pair_not_retriggered(self):
        wc, _ = self._wc({"/W/a": (0.0, 0.0, 0.0), "/W/b": (0.5, 0.0, 0.0)})
        wc._paused_until["/W/a"] = 200.0  # 아직 pause 중
        wc._handle_object_collisions(100.0)
        # b는 새로 pause되지 않아야 함 (a가 pause 중이므로 쌍 스킵)
        self.assertNotIn("/W/b", wc._paused_until)


    def test_use_contact_reports_defaults_true(self):
        self.assertTrue(WanderController(prims=[])._use_contact_reports)
        self.assertFalse(WanderController(prims=[], use_contact_reports=False)._use_contact_reports)

    def test_contact_collision_pauses_and_redirects_both(self):
        # contact report 경유 충돌도 거리 기반과 동일한 멈춤+분리를 내야 함.
        events = []
        wc, prims = self._wc(
            {"/W/a": (0.0, 0.0, 0.0), "/W/b": (0.5, 0.0, 0.0)},
            on_collision=lambda *a: events.append(a),
        )
        pa, pb = prims
        wc._object_collision_from_contact(pa, "/W/a", pb, "/W/b", 100.0)
        self.assertIn("/W/a", wc._paused_until)
        self.assertIn("/W/b", wc._paused_until)
        self.assertLess(wc._redirect_heading["/W/a"][0], 0.0)
        self.assertGreater(wc._redirect_heading["/W/b"][0], 0.0)
        self.assertEqual(len(events), 2)


class CollisionRecorderTest(unittest.TestCase):
    def test_writes_rows_with_objid_mapping(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c.csv"
            rec = CollisionRecorder(path, {"/W/a": "obj1"})
            rec.start()
            rec.record("/W/a", (1.0, 2.0, 3.0), "stuck")
            rec.record("/W/unknown", None, "contact")
            out = rec.stop()
            self.assertEqual(out, path)
            with path.open() as f:
                rows = list(_csv.reader(f))
        self.assertEqual(rec.row_count, 2)
        self.assertEqual(rows[0], ["timestamp", "objid", "x", "y", "z", "kind"])
        self.assertEqual(rows[1][1], "obj1")
        self.assertEqual(rows[1][2:6], ["1.000", "2.000", "3.000", "stuck"])
        self.assertEqual(rows[2][1], "/W/unknown")  # unmapped path falls back to itself

    def test_record_before_start_is_noop(self):
        rec = CollisionRecorder("/tmp/never.csv", {})
        rec.record("/W/a", (0.0, 0.0, 0.0), "stuck")  # no writer yet
        self.assertEqual(rec.row_count, 0)


if __name__ == "__main__":
    unittest.main()
