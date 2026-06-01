import unittest

from gist.netai.time_travel_summarization.video_capture.realtime_capture import (
    _apply_radial_overlay_scale,
)


class RealtimeProjectionTest(unittest.TestCase):
    def test_radial_overlay_scale_keeps_default_projection(self):
        self.assertEqual(_apply_radial_overlay_scale(100.0, 50.0, 400, 200, 1.0), (100.0, 50.0))

    def test_radial_overlay_scale_pushes_points_outward(self):
        self.assertEqual(_apply_radial_overlay_scale(100.0, 50.0, 400, 200, 1.2), (80.0, 40.0))
        self.assertEqual(_apply_radial_overlay_scale(300.0, 150.0, 400, 200, 1.2), (320.0, 160.0))


if __name__ == "__main__":
    unittest.main()
