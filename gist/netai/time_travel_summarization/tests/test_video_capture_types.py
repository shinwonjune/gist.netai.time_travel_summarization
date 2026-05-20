import unittest

from gist.netai.time_travel_summarization.video_capture import CaptureRequest, CaptureResult


class CaptureTypesTest(unittest.TestCase):
    def test_default_resolution_is_720_480(self):
        req = CaptureRequest(duration_s=1.0, output_uri="file:///tmp/x.mp4")
        self.assertEqual((req.width, req.height), (720, 480))

    def test_default_fps_is_30(self):
        req = CaptureRequest(duration_s=1.0, output_uri="file:///tmp/x.mp4")
        self.assertEqual(req.fps, 30)

    def test_result_failure_default(self):
        res = CaptureResult(success=False, output_uri="x", wall_clock_s=0.0, output_size_bytes=0, error="boom")
        self.assertFalse(res.success)
        self.assertEqual(res.dropped_frames, 0)
        self.assertEqual(res.sim_fps_avg, None)

    def test_runner_importable(self):
        from gist.netai.time_travel_summarization.video_capture import MovieCaptureRunner

        runner = MovieCaptureRunner()
        self.assertTrue(callable(getattr(runner, "capture", None)))


if __name__ == "__main__":
    unittest.main()
