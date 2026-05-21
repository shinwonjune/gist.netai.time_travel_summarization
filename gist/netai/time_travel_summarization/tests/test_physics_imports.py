import unittest


class PhysicsImportsTest(unittest.TestCase):
    def test_package_imports_without_omni(self):
        from gist.netai.time_travel_summarization.physics import (
            WanderController,
            create_bounding_box,
            ensure_physics_scene,
            unwrap,
            wrap_with_collision_proxy,
        )

        self.assertTrue(callable(ensure_physics_scene))
        self.assertTrue(callable(wrap_with_collision_proxy))
        self.assertTrue(callable(create_bounding_box))
        self.assertTrue(callable(unwrap))
        self.assertTrue(callable(WanderController))

    def test_wander_controller_construct_empty(self):
        from gist.netai.time_travel_summarization.physics import WanderController

        wc = WanderController(prims=[])
        self.assertFalse(wc.is_active())


if __name__ == "__main__":
    unittest.main()
