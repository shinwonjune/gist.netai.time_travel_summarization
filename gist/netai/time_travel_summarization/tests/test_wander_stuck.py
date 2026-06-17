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
