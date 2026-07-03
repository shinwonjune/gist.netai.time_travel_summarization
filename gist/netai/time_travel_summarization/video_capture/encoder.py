import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional


class EncoderError(RuntimeError):
    pass


class FrameEncoder:
    """Background-thread H.264 encoder. Frame format: raw RGBA8 bytes."""

    def __init__(self, output_path: Path, width: int, height: int, fps: int):
        self._output_path = output_path
        self._width = width
        self._height = height
        self._fps = fps
        self._thread: Optional[threading.Thread] = None
        self._queue = None
        self._error: Optional[str] = None
        self._frames_written = 0

    def start(self, queue) -> None:
        self._queue = queue
        self._thread = threading.Thread(target=self._run, name="FrameEncoder", daemon=True)
        self._thread.start()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread:
            self._thread.join(timeout=timeout)

    @property
    def frames_written(self) -> int:
        return self._frames_written

    @property
    def error(self) -> Optional[str]:
        return self._error

    def _run(self) -> None:
        try:
            backend = self._select_backend()
            backend(self._queue)
        except Exception as exc:
            import traceback
            self._error = f"{exc!r}\n{traceback.format_exc()}"

    def _select_backend(self):
        try:
            import imageio  # noqa: F401

            return self._run_imageio
        except ImportError:
            if shutil.which("ffmpeg"):
                return self._run_subprocess
            raise EncoderError("Neither imageio nor system ffmpeg available")

    def _run_imageio(self, queue):
        import imageio
        import numpy as np

        writer = imageio.get_writer(
            str(self._output_path),
            fps=self._fps,
            codec="h264",
            macro_block_size=1,
            quality=10,
            pixelformat="yuv420p",
            # CRF 12 = near-lossless. preset=slow은 동일 CRF에서 압축률·디테일 ↑.
            # tune=animation은 3D 렌더링/합성 영상(우리 케이스)에 최적화된 deblock 설정.
            output_params=["-crf", "12", "-preset", "slow", "-tune", "animation"],
        )
        expected_bytes = self._width * self._height * 4
        first_logged = False
        try:
            while True:
                item = queue.pop(timeout=2.0)
                if item is None:
                    if queue.closed:
                        break
                    continue
                _idx, rgba_bytes, cb_width, cb_height = item
                # callback이 알려준 width/height는 viewport 디스플레이 크기일 수 있어
                # buf bytes 크기와 안 맞음. encoder 자신의 (self._width, self._height)를 신뢰.
                if len(rgba_bytes) != expected_bytes:
                    if not first_logged:
                        print(f"[encoder] buf size {len(rgba_bytes)} != expected {expected_bytes} "
                              f"(self={self._width}x{self._height}, cb={cb_width}x{cb_height}) — frame skipped")
                        first_logged = True
                    continue
                arr = np.frombuffer(rgba_bytes, dtype=np.uint8).reshape(self._height, self._width, 4)
                writer.append_data(arr[:, :, :3])
                self._frames_written += 1
        finally:
            writer.close()

    def _run_subprocess(self, queue):
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "rawvideo",
            "-vcodec",
            "rawvideo",
            "-s",
            f"{self._width}x{self._height}",
            "-pix_fmt",
            "rgba",
            "-r",
            str(self._fps),
            "-i",
            "-",
            "-an",
            "-vcodec",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "12",
            "-preset",
            "slow",
            "-tune",
            "animation",
            str(self._output_path),
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        expected_bytes = self._width * self._height * 4
        try:
            while True:
                item = queue.pop(timeout=2.0)
                if item is None:
                    if queue.closed:
                        break
                    continue
                _idx, rgba_bytes, _w, _h = item
                if len(rgba_bytes) != expected_bytes:
                    continue
                proc.stdin.write(rgba_bytes)
                self._frames_written += 1
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.wait(timeout=30)
