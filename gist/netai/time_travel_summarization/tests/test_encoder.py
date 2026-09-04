import builtins
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gist.netai.time_travel_summarization.video_capture.encoder import EncoderError, FrameEncoder
from gist.netai.time_travel_summarization.video_capture.frame_queue import FrameQueue


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

    def test_run_subprocess_ffmpeg_failure_logs_and_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            fake_ffmpeg = tmp_dir / "ffmpeg"
            fake_ffmpeg.write_text("#!/bin/sh\ncat > /dev/null\nexit 1\n")
            fake_ffmpeg.chmod(0o755)

            output_path = tmp_dir / "out.mp4"
            encoder = FrameEncoder(output_path, 4, 4, 30)
            frame_queue = FrameQueue()
            frame_queue.push((0, b"x" * (4 * 4 * 4), 4, 4))
            frame_queue.close()

            patched_path = f"{tmp_dir}:{os.environ.get('PATH', '')}"
            with mock.patch.dict(os.environ, {"PATH": patched_path}), mock.patch(
                "gist.netai.time_travel_summarization.video_capture.encoder.carb"
            ) as mock_carb:
                encoder._run_subprocess(frame_queue)  # must not raise

            mock_carb.log_error.assert_called_once()
            self.assertFalse(output_path.exists() and output_path.stat().st_size > 0)


if __name__ == "__main__":
    unittest.main()
