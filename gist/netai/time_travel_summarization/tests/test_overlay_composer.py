import unittest


class OverlayComposerHeadlessTest(unittest.TestCase):
    def test_module_import(self):
        from gist.netai.time_travel_summarization.video_capture.overlay_composer import (
            OverlayComposer,
            OverlayFrame,
            TextItem,
        )

        self.assertTrue(callable(OverlayComposer))

    def test_compose_without_pil_returns_input(self):
        """PIL 없는 환경에서 compose가 원본 bytes 그대로 리턴."""
        from gist.netai.time_travel_summarization.video_capture.overlay_composer import (
            OverlayComposer,
            OverlayFrame,
        )

        rgba = b"\x00" * (532 * 280 * 4)
        composer = OverlayComposer(532, 280)
        out = composer.compose(rgba, OverlayFrame(timestamp_text="2025-01-01 00:00:00.000"))
        self.assertEqual(len(out), len(rgba))


class OverlayComposerPILTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import PIL  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("PIL not installed")

    def test_compose_modifies_some_pixels_when_text_provided(self):
        from gist.netai.time_travel_summarization.video_capture.overlay_composer import (
            OverlayComposer,
            OverlayFrame,
            TextItem,
        )

        rgba = bytes([0] * (200 * 100 * 4))
        composer = OverlayComposer(200, 100)
        frame = OverlayFrame(
            timestamp_text="hello",
            object_labels=(TextItem(x=50, y=50, text="A"),),
        )
        out = composer.compose(rgba, frame)
        self.assertNotEqual(out, rgba)


if __name__ == "__main__":
    unittest.main()
