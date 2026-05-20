import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .types import CaptureRequest, CaptureResult


class MovieCaptureRunner:
    """A1 baseline - Omniverse Movie Capture wrapper.

    The capture itself is synchronous from the caller's perspective: capture(req) returns
    only after the mp4 is written to disk (or upload to URI completes).
    """

    def capture(self, req: CaptureRequest) -> CaptureResult:
        start_time = None
        temp_dir = None
        metadata = self._metadata(req)

        try:
            if not req.output_uri:
                raise ValueError("output_uri is required")

            # Defer Kit imports so this module remains importable in headless environments.
            from omni.kit.capture.viewport import CaptureExtension, CaptureOptions, CaptureProgress

            temp_dir = Path(tempfile.mkdtemp(prefix="ttsum_a1_"))
            file_name = f"a1_capture_{datetime.now().strftime('%Y%m%dT%H%M%S')}"

            options = CaptureOptions()
            options.res_width = req.width
            options.res_height = req.height
            options.animation_fps = req.fps
            options.file_type = ".mp4"
            options.start_frame = 0
            options.end_frame = int(req.duration_s * req.fps)
            options.output_folder = str(temp_dir)
            options.file_name = file_name
            options.app_level_capture = True

            start_time = time.perf_counter()
            extension = CaptureExtension.get_instance()
            # API surface varies by Kit version. Try, in order:
            #   1) extension.options = options; extension.start()
            #   2) extension.start_capture(options)
            #   3) extension.start(options)  (rare; fallback)
            start_result = None
            if hasattr(extension, "options") and hasattr(extension, "start"):
                try:
                    extension.options = options
                    start_result = extension.start()
                except TypeError:
                    start_result = None
            if start_result is None and hasattr(extension, "start_capture"):
                start_result = extension.start_capture(options)
            if start_result is None:
                start_result = extension.start(options)
            produced_mp4 = self._wait_for_mp4(temp_dir, extension, start_result, CaptureProgress)
            wall_clock_s = time.perf_counter() - start_time

            from gist.netai.time_travel_summarization.storage import from_uri

            adapter = from_uri(req.output_uri)
            adapter.put_file(req.output_uri, produced_mp4, content_type="video/mp4")
            output_size_bytes = adapter.stat(req.output_uri).size

            return CaptureResult(
                success=True,
                output_uri=req.output_uri,
                wall_clock_s=wall_clock_s,
                output_size_bytes=output_size_bytes,
                metadata=metadata,
            )
        except Exception as exc:
            wall_clock_s = time.perf_counter() - start_time if start_time is not None else 0.0
            return CaptureResult(
                success=False,
                output_uri=req.output_uri,
                wall_clock_s=wall_clock_s,
                output_size_bytes=0,
                error=repr(exc),
                metadata=metadata,
            )
        finally:
            if temp_dir is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)

    @staticmethod
    def _metadata(req: CaptureRequest) -> dict:
        return {
            "runner": "A1_movie_capture",
            "resolution": f"{req.width}x{req.height}",
            "fps": req.fps,
            "duration_s": req.duration_s,
            "kit_capture_api": "omni.kit.capture.viewport",
            "app_level_capture": True,
        }

    def _wait_for_mp4(
        self,
        output_folder: Path,
        extension: Any,
        start_result: Any,
        capture_progress_type: Any,
    ) -> Path:
        progress = self._resolve_progress(extension, start_result, capture_progress_type)
        if progress is not None:
            self._wait_for_progress(progress)

        return self._wait_for_stable_mp4(output_folder)

    @staticmethod
    def _resolve_progress(extension: Any, start_result: Any, capture_progress_type: Any) -> Any:
        get_instance = getattr(capture_progress_type, "get_instance", None)
        singleton_progress = get_instance() if callable(get_instance) else None
        for candidate in (
            start_result,
            getattr(extension, "progress", None),
            getattr(extension, "capture_progress", None),
            singleton_progress,
        ):
            if candidate is not None and hasattr(candidate, "is_capturing"):
                return candidate
        return None

    @staticmethod
    def _wait_for_progress(progress: Any) -> None:
        while bool(getattr(progress, "is_capturing", False)):
            time.sleep(0.1)

    def _wait_for_stable_mp4(self, output_folder: Path) -> Path:
        last_path = None
        last_size = None
        stable_since = None

        while True:
            mp4_path = self._largest_mp4(output_folder)
            if mp4_path is None:
                time.sleep(0.25)
                continue

            current_size = mp4_path.stat().st_size
            now = time.monotonic()
            if mp4_path == last_path and current_size == last_size:
                stable_since = stable_since or now
                if now - stable_since >= 2.0:
                    return mp4_path
            else:
                last_path = mp4_path
                last_size = current_size
                stable_since = None

            time.sleep(0.25)

    @staticmethod
    def _largest_mp4(output_folder: Path) -> Path | None:
        mp4_files = list(output_folder.glob("*.mp4"))
        if not mp4_files:
            return None
        return max(mp4_files, key=lambda path: path.stat().st_size)
