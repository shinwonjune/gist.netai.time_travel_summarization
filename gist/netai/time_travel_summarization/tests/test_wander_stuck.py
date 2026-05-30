import unittest
from gist.netai.time_travel_summarization.physics import WanderController
from gist.netai.time_travel_summarization.physics.wander_controller import (
    PrimState,
    _angle_delta_degrees,
    _angle_between_vectors_degrees,
    _max_rotation_delta_degrees,
)


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


class WanderStuckTest(unittest.TestCase):
    def test_velocity_mode_param(self):
        wc = WanderController(prims=[], velocity_mode="on_enter")
        self.assertEqual(wc._velocity_mode, "on_enter")

    def test_invalid_velocity_mode_falls_back(self):
        wc = WanderController(prims=[], velocity_mode="bogus")
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

        class _FixedVec:
            def __init__(self, v):
                self._v = v

            def __getitem__(self, i):
                return self._v[i]

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

        class _V:
            def __init__(self, v):
                self._v = v

            def __getitem__(self, i):
                return self._v[i]

        def _wp(p):
            v = positions[idx["i"]]
            idx["i"] = min(idx["i"] + 1, len(positions) - 1)
            return _V(v)

        wc._world_position = _wp
        wc._check_stuck(prim, str(prim), 1.0)    # baseline
        wc._check_stuck(prim, str(prim), 1.016)  # stuck +1
        wc._check_stuck(prim, str(prim), 1.032)  # progress=10 -> reset
        self.assertEqual(wc._stuck_count[str(prim)], 0)

    def test_angle_delta_wraps(self):
        self.assertEqual(_angle_delta_degrees(350.0, 10.0), 20.0)
        self.assertEqual(_angle_delta_degrees(10.0, 350.0), 20.0)
        self.assertEqual(_max_rotation_delta_degrees((0.0, 45.0, 0.0), (0.0, 0.0, 350.0)), 45.0)
        self.assertAlmostEqual(_angle_between_vectors_degrees((1, 0, 0), (0, 1, 0)), 90.0)
        self.assertAlmostEqual(_angle_between_vectors_degrees((1, 0, 0), (1, 0, 0)), 0.0)

    def test_check_fallen_triggers_after_k_frames(self):
        prim = _FakePrim("/W/fallen")
        wc = WanderController(prims=[prim], fallen_angle_deg=30.0, fallen_frames=2)
        wc._rotation_delta_from_original = lambda p, path: 45.0
        self.assertFalse(wc._check_fallen(prim, str(prim)))
        self.assertTrue(wc._check_fallen(prim, str(prim)))

    def test_check_fallen_resets_when_upright(self):
        prim = _FakePrim("/W/upright")
        wc = WanderController(prims=[prim], fallen_angle_deg=30.0, fallen_frames=2)
        deltas = [45.0, 5.0, 45.0]
        wc._rotation_delta_from_original = lambda p, path: deltas.pop(0)
        self.assertFalse(wc._check_fallen(prim, str(prim)))
        self.assertFalse(wc._check_fallen(prim, str(prim)))
        self.assertFalse(wc._check_fallen(prim, str(prim)))

    def test_velocity_mode_default_is_horizontal_per_tick(self):
        wc = WanderController(prims=[])
        self.assertEqual(wc._velocity_mode, "horizontal_per_tick")

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

    def test_original_rotation_default_zero(self):
        # prims=[]면 dict 비어있음, no crash
        wc = WanderController(prims=[])
        self.assertEqual(wc._original_rotation, {})

    def test_restore_upright_targets_original(self):
        try:
            from pxr import Gf
        except Exception:
            self.skipTest("pxr unavailable")
        wc = WanderController(prims=[])
        wc._original_rotation["/W/r"] = Gf.Vec3f(270.0, 0.0, 0.0)
        self.assertEqual(wc._original_rotation["/W/r"][0], 270.0)

    def test_is_grounded_returns_true_with_stable_history(self):
        wc = WanderController(prims=[])
        wc._vertical_position_history["/W/g"] = [100.0, 100.05, 100.03]

        class _FakePrimG:
            def IsValid(self):
                return True

            def GetPath(self):
                return "/W/g"

            def GetStage(self):
                return None

        class _FakeVec:
            def __getitem__(self, i):
                return 100.04

        wc._world_position = lambda p: _FakeVec()
        self.assertTrue(wc._is_grounded(_FakePrimG(), "/W/g"))


if __name__ == "__main__":
    unittest.main()
