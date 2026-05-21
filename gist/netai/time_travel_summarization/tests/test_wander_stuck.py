import unittest
from gist.netai.time_travel_summarization.physics import WanderController
from gist.netai.time_travel_summarization.physics.wander_controller import PrimState


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
        self.assertEqual(wc._velocity_mode, "per_tick")

    def test_set_velocity_mode(self):
        wc = WanderController(prims=[])
        self.assertTrue(wc.set_velocity_mode("on_enter"))
        self.assertEqual(wc._velocity_mode, "on_enter")
        self.assertFalse(wc.set_velocity_mode("nope"))

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


if __name__ == "__main__":
    unittest.main()
