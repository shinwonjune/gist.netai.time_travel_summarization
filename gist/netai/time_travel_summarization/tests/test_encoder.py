import builtins
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

from gist.netai.time_travel_summarization.video_capture.encoder import EncoderError, FrameEncoder


def _has_imageio() -> bool:
    try:
        import imageio  # noqa: F401

        return True
    except ImportError:
        return False


class FrameEncoderTest(unittest.TestCase):
    def test_encoder_importable_headless(self):
        self.assertFalse(any(name == "omni" or name.startswith("omni.") for name in sys.modules))

    @unittest.skipUnless(_has_imageio(), "imageio is not installed")
    def test_encoder_select_backend_imageio_when_available(self):
        encoder = FrameEncoder(Path("/tmp/out.mp4"), 532, 280, 30)

        backend = encoder._select_backend()

        self.assertEqual(backend, encoder._run_imageio)

    def test_encoder_select_backend_subprocess_when_no_imageio(self):
        encoder = FrameEncoder(Path("/tmp/out.mp4"), 532, 280, 30)
        real_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "imageio":
                raise ImportError("imageio unavailable")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=_fake_import):
            if shutil.which("ffmpeg"):
                self.assertEqual(encoder._select_backend(), encoder._run_subprocess)
            else:
                with self.assertRaises(EncoderError):
                    encoder._select_backend()


if __name__ == "__main__":
    unittest.main()
