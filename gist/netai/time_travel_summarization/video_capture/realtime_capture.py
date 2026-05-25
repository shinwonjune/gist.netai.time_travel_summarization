import ctypes
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .encoder import FrameEncoder
from .frame_queue import FrameQueue
import re
from .overlay_composer import CircleLabel, OverlayComposer, OverlayFrame, TextItem


# True로 두면 마커가 반투명 + 4-방향 tick으로 렌더되어 객체 중심과 투영 픽셀의 일치 여부를 시각 확인할 수 있다
DEBUG_OVERLAY_MARKERS = False


def _extract_objid_number(objid: str) -> str:
    """objid 끝의 숫자만 추출. 'obj001' → '1', 'obj042' → '42'. 못 찾으면 원본."""
    m = re.search(r"(\d+)$", objid)
    return str(int(m.group(1))) if m else objid
from .types import CaptureRequest, CaptureResult


# PyCapsule → void* 변환용 (한 번만 시그니처 등록)
_PyCapsule_GetPointer = ctypes.pythonapi.PyCapsule_GetPointer
_PyCapsule_GetPointer.restype = ctypes.c_void_p
_PyCapsule_GetPointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
_PyCapsule_GetName = ctypes.pythonapi.PyCapsule_GetName
_PyCapsule_GetName.restype = ctypes.c_char_p
_PyCapsule_GetName.argtypes = [ctypes.py_object]


def _buf_to_bytes(buf, buf_size: int) -> bytes:
    """capture_viewport_to_buffer의 buf 인자는 Kit 버전에 따라
    PyCapsule / int 주소 / bytes-like가 될 수 있다. 모두 처리."""
    # Case A: PyCapsule (가장 흔함)
    if type(buf).__name__ == "PyCapsule":
        name = _PyCapsule_GetName(buf)
        ptr = _PyCapsule_GetPointer(buf, name)
        return ctypes.string_at(ptr, buf_size)
    # Case B: 정수 주소
    if isinstance(buf, int):
        return ctypes.string_at(buf, buf_size)
    # Case C: bytes-like (메모리뷰, bytes, bytearray)
    try:
        return bytes(buf[:buf_size])
    except TypeError:
        # Case D: ctypes 포인터
        try:
            return ctypes.string_at(ctypes.addressof(buf.contents), buf_size)
        except Exception:
            return ctypes.string_at(int(buf), buf_size)


_MATRIX_LOG_ONCE = {"done": False}


def _matrix_log(msg: str):
    if not _MATRIX_LOG_ONCE["done"]:
        print(f"[A2 matrix] {msg}")
        _MATRIX_LOG_ONCE["done"] = True


def _resolve_camera_path(viewport):
    """다양한 Kit 버전의 active camera path 속성을 탐색."""
    for attr in ("camera_path", "active_camera", "get_camera_path"):
        try:
            v = getattr(viewport, attr, None)
            if callable(v):
                v = v()
            if v:
                return str(v)
        except Exception:
            continue
    return None


def _get_camera_matrices(viewport, stage, width: int, height: int):
    """viewport의 active camera에서 view·projection 매트릭스. 실패 시 None."""
    errors = []

    # camera path 찾기
    cam_path = _resolve_camera_path(viewport)
    if not cam_path:
        _matrix_log(f"no camera path attribute. viewport attrs={[a for a in dir(viewport) if 'cam' in a.lower()][:8]}")
        return None

    try:
        from pxr import UsdGeom
    except Exception as e:
        _matrix_log(f"pxr import failed: {e!r}")
        return None

    camera_prim = stage.GetPrimAtPath(cam_path)
    if not camera_prim or not camera_prim.IsValid():
        _matrix_log(f"prim invalid at {cam_path!r}")
        return None

    # View matrix from camera world transform inverse
    try:
        xformable = UsdGeom.Xformable(camera_prim)
        world = xformable.ComputeLocalToWorldTransform(0)
        view = world.GetInverse()
    except Exception as e:
        errors.append(f"view: {e!r}")

    # Projection matrix — 여러 path 시도, aspect ratio를 viewport에 맞춰 override
    proj = None
    last_proj_err = None
    try:
        usd_cam_obj = UsdGeom.Camera(camera_prim)
        gf_camera = usd_cam_obj.GetCamera(0)
        # FIX A: USD 카메라의 aperture는 종종 4:3 같은 기본값. viewport(720:480 등) 비율과 다르면
        # 투영 좌표 x가 어긋남. horizontalAperture를 viewport aspect에 맞춰 override.
        try:
            target_aspect = width / max(height, 1)
            gf_camera.horizontalAperture = gf_camera.verticalAperture * target_aspect
        except Exception:
            pass
        proj = gf_camera.frustum.ComputeProjectionMatrix()
    except Exception as e:
        last_proj_err = e
        try:
            usd_cam_obj = UsdGeom.Camera(camera_prim)
            gf_camera = usd_cam_obj.GetCamera()
            try:
                target_aspect = width / max(height, 1)
                gf_camera.horizontalAperture = gf_camera.verticalAperture * target_aspect
            except Exception:
                pass
            proj = gf_camera.frustum.ComputeProjectionMatrix()
        except Exception as e2:
            last_proj_err = e2

    if proj is None:
        errors.append(f"proj: {last_proj_err!r}")

    if errors:
        _matrix_log(f"partial fail @ {cam_path}: {errors}")
        return None

    _matrix_log(f"matrices OK from camera path={cam_path}")
    return (view, proj)


def _project_world_to_pixel(world_xyz, view, proj, width: int, height: int):
    """월드 좌표 (x,y,z) → 픽셀 좌표 (px, py). 카메라 뒤이면 None."""
    from pxr import Gf

    p_world = Gf.Vec4d(world_xyz[0], world_xyz[1], world_xyz[2], 1.0)
    p_view = view.Transform(Gf.Vec3d(p_world[0], p_world[1], p_world[2]))
    if p_view[2] >= 0:
        return None

    p_clip = proj.Transform(p_view)
    px = (p_clip[0] * 0.5 + 0.5) * width
    py = (1.0 - (p_clip[1] * 0.5 + 0.5)) * height
    return (int(px), int(py))


class RealtimeCaptureRunner:
    def __init__(
        self,
        overlay_provider: Optional[Callable[[int, int], OverlayFrame]] = None,
        core: Optional[object] = None,
    ):
        self._overlay_provider = overlay_provider
        self._core = core

    def _default_provider_from_core(self, viewport, stage):
        diag = {"first_logged": False}

        def _log_once(msg: str):
            if not diag["first_logged"]:
                print(f"[A2 overlay diag] {msg}")
                diag["first_logged"] = True

        def _provider(width: int, height: int) -> OverlayFrame:
            ts_text = self._core.get_stage_time_string() if self._core else None
            misc = []
            if not self._core:
                _log_once("core=None")
                return OverlayFrame(timestamp_text=ts_text, object_labels=(), misc_text=())

            sim_time = self._core.get_simulation_time()
            if sim_time is None:
                _log_once("sim_time=None (playback not started?)")
                return OverlayFrame(timestamp_text=ts_text, object_labels=(), misc_text=())

            data = self._core.get_data_at_time(sim_time)
            if not data:
                _log_once(f"data empty at sim_time={sim_time}")
                return OverlayFrame(timestamp_text=ts_text, object_labels=(), misc_text=())

            matrices = _get_camera_matrices(viewport, stage, width, height)
            if matrices is None:
                _log_once(f"camera matrices unavailable (Kit API mismatch) — falling back to corner list. data keys: {list(data.keys())[:5]}")
                # Fallback: 화면 좌상단에 객체 ID 리스트 박스로 표기 (3D anchor 없음)
                ids = ", ".join(sorted(data.keys()))
                misc.append(
                    TextItem(
                        x=8, y=8, text=f"Objects: {ids}",
                        align="left", vertical_align="top",
                        font_size=12,
                    )
                )
                return OverlayFrame(timestamp_text=ts_text, object_labels=(), misc_text=tuple(misc))

            view, proj = matrices
            # 객체 좌표 그대로(offset 없음). 숫자 ID를 원형 라벨로 그 위치에 표시.
            circles = []
            projected_count = 0
            for objid, world_xyz in data.items():
                px = _project_world_to_pixel(world_xyz, view, proj, width, height)
                if px is None:
                    continue
                projected_count += 1
                circles.append(
                    CircleLabel(
                        x=px[0],
                        y=px[1],
                        text=_extract_objid_number(objid),
                        radius=12,
                        font_size=12,
                    )
                )

            if projected_count == 0:
                _log_once(f"projection produced 0 labels (모두 카메라 뒤로 판정?). data keys: {list(data.keys())[:5]}")
                # 동일 fallback
                ids = ", ".join(sorted(data.keys()))
                misc.append(
                    TextItem(
                        x=8, y=8, text=f"Objects: {ids}",
                        align="left", vertical_align="top",
                        font_size=12,
                    )
                )
            else:
                _log_once(f"projected {projected_count} labels OK")

            return OverlayFrame(
                timestamp_text=ts_text,
                object_labels=(),
                misc_text=tuple(misc),
                circle_markers=tuple(circles),
            )

        return _provider

    def capture(self, req: CaptureRequest, stop_event=None) -> CaptureResult:
        # Kit 내부에서 asyncio.get_event_loop()가 호출되는 경로가 있어
        # 워커 스레드에도 이벤트 루프가 필요. 없으면 새로 부착.
        import asyncio
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

        start_wall = time.perf_counter()
        metadata = {
            "runner": "A2_realtime_capture",
            "resolution": f"{req.width}x{req.height}",
            "fps": req.fps,
            "duration_s": req.duration_s,
        }
        tmp_dir = Path(tempfile.mkdtemp(prefix="ttsum_a2_"))
        tmp_mp4 = tmp_dir / f"a2_{datetime.now().strftime('%Y%m%dT%H%M%S')}.mp4"
        queue = FrameQueue(maxsize=8)
        encoder = FrameEncoder(tmp_mp4, req.width, req.height, req.fps)

        try:
            from omni.kit.viewport.utility import capture_viewport_to_buffer, get_active_viewport
            import omni.usd

            viewport = get_active_viewport()
            if viewport is None:
                return CaptureResult(
                    success=False,
                    output_uri=req.output_uri,
                    wall_clock_s=0.0,
                    output_size_bytes=0,
                    error="no active viewport",
                    metadata=metadata,
                )

            # 원래 viewport 해상도 저장 (capture 끝나면 복원 → 디스플레이 화질 보호)
            original_resolution = None
            for _attr in ("texture_resolution", "full_size", "resolution"):
                try:
                    _val = getattr(viewport, _attr, None)
                    if _val and hasattr(_val, "__iter__"):
                        _vals = list(_val)
                        if len(_vals) >= 2:
                            original_resolution = (int(_vals[0]), int(_vals[1]))
                            break
                except Exception:
                    continue
            if original_resolution is None:
                print("[A2] viewport 원래 해상도 조회 실패 — 캡처 후 복원 못함 (Kit 재시작으로 복구)")
            try:
                viewport.set_texture_resolution((req.width, req.height))
            except Exception:
                pass

            stage = omni.usd.get_context().get_stage()
            composer = OverlayComposer(req.width, req.height, debug=DEBUG_OVERLAY_MARKERS)
            provider = self._overlay_provider or (
                self._default_provider_from_core(viewport, stage) if self._core else None
            )

            encoder.start(queue)

            target_frames = int(req.duration_s * req.fps)
            received = 0
            frame_interval = 1.0 / req.fps
            next_due = time.perf_counter()

            def _on_frame(buf, buf_size, width, height, fmt):
                nonlocal received
                try:
                    rgba = _buf_to_bytes(buf, buf_size)
                except Exception as e:
                    # 첫 프레임에서 한 번만 로그
                    if received == 0:
                        print(f"[A2] frame buffer conversion failed: {e!r} (type={type(buf).__name__})")
                    return
                if provider is not None:
                    try:
                        # callback의 width/height는 viewport 디스플레이 크기(고DPI 시 2×)일 수 있어
                        # 실제 buf와 안 맞음. 우리가 강제한 req.width/req.height를 사용.
                        overlay_frame = provider(req.width, req.height)
                        rgba = composer.compose(rgba, overlay_frame)
                    except Exception as e:
                        if received == 0:
                            print(f"[A2] overlay compose failed: {e!r}; continuing without overlay")
                queue.push((received, rgba, width, height))
                received += 1

            stopped_early = False
            while received < target_frames:
                if stop_event is not None and stop_event.is_set():
                    stopped_early = True
                    break
                now = time.perf_counter()
                if now < next_due:
                    time.sleep(min(0.005, next_due - now))
                    continue
                capture_viewport_to_buffer(viewport, _on_frame)
                next_due += frame_interval

            queue.close()
            encoder.join(timeout=30.0)
            if encoder.error:
                return CaptureResult(
                    success=False,
                    output_uri=req.output_uri,
                    wall_clock_s=time.perf_counter() - start_wall,
                    output_size_bytes=0,
                    error=encoder.error,
                    dropped_frames=queue.dropped,
                    metadata=metadata,
                )

            from gist.netai.time_travel_summarization.storage import from_uri

            adapter = from_uri(req.output_uri)
            adapter.put_file(req.output_uri, tmp_mp4, content_type="video/mp4")
            size = adapter.stat(req.output_uri).size

            wall = time.perf_counter() - start_wall
            metadata.update(
                {
                    "frames_received": received,
                    "frames_written": encoder.frames_written,
                    "drop_rate": queue.dropped / max(received, 1),
                }
            )
            return CaptureResult(
                success=True,
                output_uri=req.output_uri,
                wall_clock_s=wall,
                output_size_bytes=size,
                sim_fps_avg=received / max(wall, 1e-9),
                dropped_frames=queue.dropped,
                metadata=metadata,
            )
        except Exception as exc:
            queue.close()
            try:
                encoder.join(timeout=5.0)
            except Exception:
                pass
            return CaptureResult(
                success=False,
                output_uri=req.output_uri,
                wall_clock_s=time.perf_counter() - start_wall,
                output_size_bytes=0,
                error=repr(exc),
                dropped_frames=queue.dropped,
                metadata=metadata,
            )
        finally:
            # viewport 해상도 복원 (캡처 시 작게 바꿔놓은 것을 원상 복귀)
            try:
                if "viewport" in locals() and viewport is not None and "original_resolution" in locals() and original_resolution is not None:
                    viewport.set_texture_resolution(original_resolution)
            except Exception as _e:
                print(f"[A2] viewport 해상도 복원 실패: {_e!r}")
            shutil.rmtree(tmp_dir, ignore_errors=True)
