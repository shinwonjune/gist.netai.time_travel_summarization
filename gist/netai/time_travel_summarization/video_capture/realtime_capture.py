import ctypes
import os
import shutil
import tempfile
import threading
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


def _apply_radial_overlay_scale(px: float, py: float, width: int, height: int, scale: float):
    """Move projected overlay points radially from the frame center.

    Default scale 1.0 preserves current projection. Values greater than 1.0
    compensate labels that are biased toward the center of the captured image.
    """
    scale = float(scale)
    if abs(scale - 1.0) <= 1e-9:
        return px, py
    cx = float(width) * 0.5
    cy = float(height) * 0.5
    return cx + (float(px) - cx) * scale, cy + (float(py) - cy) * scale


def _overlay_radial_scale() -> float:
    raw = os.environ.get("TTS_OVERLAY_RADIAL_SCALE", "1.0")
    try:
        scale = float(raw)
    except (TypeError, ValueError):
        return 1.0
    if scale <= 0.0:
        return 1.0
    return scale


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
    px, py = _apply_radial_overlay_scale(px, py, width, height, _overlay_radial_scale())
    return (int(px), int(py))


def _configure_deterministic_clock(stage, fps: float, duration_s: float) -> None:
    """오프라인 결정론 캡처용 클럭 정합 (4.6).

    headless 루프는 ``app.update()`` 1회 = 인코딩 프레임 1개 구조다. timeCodesPerSecond를
    fps에 맞추고 fixed timestepping을 켜면 ``app.update()`` 1회 = 1/fps sim 전진 = 프레임 1개가
    되어, 렌더 부하와 무관하게 프레임당 동일한 물리 전진이 보장된다(슬로모션·드리프트 제거,
    재현성 확보). 물리 substep 레이트(timeStepsPerSecond)는 physics scene에서 별도 60Hz로
    두어 정확도를 유지한다.

    타임라인 재생 구간도 명시한다: headless의 새 빈 스테이지는 end time이 사실상 0이라
    한 프레임 전진 후 루프로 되감겨 물리가 멈춘다(프로브 ratio_t: 1.0 → 0 → -1.0 패턴).
    start=0, end=duration+여유, looping off, play 재보장으로 캡처 내내 전진을 보장.
    """
    try:
        if stage is not None:
            stage.SetTimeCodesPerSecond(float(fps))
    except Exception as e:
        print(f"[HL] set timeCodesPerSecond({fps}) failed: {e!r}")
    try:
        import omni.timeline
        tl = omni.timeline.get_timeline_interface()
        if hasattr(tl, "set_time_codes_per_second"):
            tl.set_time_codes_per_second(float(fps))
        tl.set_start_time(0.0)
        tl.set_end_time(float(duration_s) + 3600.0)
        if hasattr(tl, "set_looping"):
            tl.set_looping(False)
        tl.play()
    except Exception as e:
        print(f"[HL] timeline range/play setup failed: {e!r}")
    try:
        import carb.settings
        carb.settings.get_settings().set("/app/player/useFixedTimeStepping", True)
    except Exception as e:
        print(f"[HL] useFixedTimeStepping set failed: {e!r}")


class RealtimeCaptureRunner:
    def __init__(
        self,
        overlay_provider: Optional[Callable[[int, int], OverlayFrame]] = None,
        core: Optional[object] = None,
    ):
        self._overlay_provider = overlay_provider
        self._core = core

    def _default_provider_from_core(self, viewport, stage, matrices_fn=None):
        """오버레이 프로바이더. ``matrices_fn(w,h)``가 주어지면(headless: camera_params
        annotator = 렌더러가 실제 쓴 view/projection) 그것을 우선 사용 — 재구성 행렬의
        conform 정책 추측 오차(라벨이 객체에서 밀리는 문제)가 원천 제거된다."""
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

            get_live_positions = getattr(self._core, "get_current_object_positions", None)
            if callable(get_live_positions):
                data = get_live_positions()
            else:
                data = self._core.get_data_at_time(sim_time)
            if not data:
                _log_once(f"data empty at sim_time={sim_time}")
                return OverlayFrame(timestamp_text=ts_text, object_labels=(), misc_text=())

            matrices = matrices_fn(width, height) if matrices_fn is not None else None
            if matrices is not None:
                _log_once("overlay matrices: renderer camera_params (exact)")
            else:
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
            "frame_clock": "async_capture_reordered",
        }
        tmp_dir = Path(tempfile.mkdtemp(prefix="ttsum_a2_"))
        tmp_mp4 = tmp_dir / f"a2_{datetime.now().strftime('%Y%m%dT%H%M%S')}.mp4"
        queue = FrameQueue(maxsize=32, drop_oldest=False)
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
            requested = 0
            completed_count = 0
            failed_count = 0
            encoded_input_count = 0
            duplicate_count = 0
            frame_interval = 1.0 / req.fps
            callback_timeout_s = max(2.0, frame_interval * 10.0)
            reorder_wait_s = max(0.05, frame_interval * 3.0)
            next_due = time.perf_counter()
            completed_frames = {}
            failed_frames = set()
            completed_cond = threading.Condition()
            next_to_encode = 0
            last_encoded_item = None

            def _make_on_frame(seq, overlay_snapshot):
                def _on_frame(buf, buf_size, width, height, fmt):
                    nonlocal completed_count, failed_count
                    item = None
                    failed = False
                    try:
                        try:
                            rgba = _buf_to_bytes(buf, buf_size)
                        except Exception as e:
                            # 첫 프레임에서 한 번만 로그
                            if seq == 0:
                                print(f"[A2] frame buffer conversion failed: {e!r} (type={type(buf).__name__})")
                            failed = True
                            return
                        if overlay_snapshot is not None:
                            try:
                                rgba = composer.compose(rgba, overlay_snapshot)
                            except Exception as e:
                                if seq == 0:
                                    print(f"[A2] overlay compose failed: {e!r}; continuing without overlay")
                        item = (seq, rgba, width, height)
                    finally:
                        with completed_cond:
                            if item is not None:
                                if seq >= next_to_encode:
                                    completed_frames[seq] = item
                                completed_count += 1
                            elif failed:
                                if seq >= next_to_encode:
                                    failed_frames.add(seq)
                                failed_count += 1
                            completed_cond.notify_all()

                return _on_frame

            def _make_duplicate_item(seq, source_item):
                return (seq, source_item[1], source_item[2], source_item[3])

            def _flush_ordered_ready():
                nonlocal next_to_encode, last_encoded_item, encoded_input_count
                flushed = 0
                while True:
                    with completed_cond:
                        item = completed_frames.pop(next_to_encode, None)
                        if item is None:
                            break
                    queue.push(item)
                    last_encoded_item = item
                    next_to_encode += 1
                    encoded_input_count += 1
                    flushed += 1
                return flushed

            stopped_early = False
            request_started_at = time.perf_counter()
            request_deadline = request_started_at + req.duration_s
            while requested < target_frames:
                if stop_event is not None and stop_event.is_set():
                    stopped_early = True
                    break
                now = time.perf_counter()
                if now >= request_deadline:
                    break
                if now < next_due:
                    _flush_ordered_ready()
                    time.sleep(min(0.005, next_due - now))
                    continue

                overlay_snapshot = None
                if provider is not None:
                    try:
                        # Tie overlay state to request order, not callback completion order.
                        overlay_snapshot = provider(req.width, req.height)
                    except Exception as e:
                        if requested == 0:
                            print(f"[A2] overlay snapshot failed: {e!r}; continuing without overlay")

                capture_viewport_to_buffer(viewport, _make_on_frame(requested, overlay_snapshot))
                requested += 1
                _flush_ordered_ready()
                next_due += frame_interval
            request_finished_at = time.perf_counter()

            def _handle_failed_head():
                nonlocal next_to_encode, last_encoded_item, encoded_input_count, duplicate_count
                with completed_cond:
                    if next_to_encode not in failed_frames:
                        return False
                    failed_frames.discard(next_to_encode)
                if last_encoded_item is not None:
                    queue.push(_make_duplicate_item(next_to_encode, last_encoded_item))
                    last_encoded_item = _make_duplicate_item(next_to_encode, last_encoded_item)
                    duplicate_count += 1
                    encoded_input_count += 1
                next_to_encode += 1
                return True

            while next_to_encode < requested:
                if _flush_ordered_ready():
                    continue
                if _handle_failed_head():
                    continue

                wait_deadline = time.perf_counter() + (
                    callback_timeout_s if last_encoded_item is None else reorder_wait_s
                )
                while time.perf_counter() < wait_deadline:
                    if _flush_ordered_ready() or _handle_failed_head():
                        break
                    with completed_cond:
                        completed_cond.wait(timeout=min(0.01, max(0.0, wait_deadline - time.perf_counter())))
                else:
                    if last_encoded_item is not None:
                        queue.push(_make_duplicate_item(next_to_encode, last_encoded_item))
                        last_encoded_item = _make_duplicate_item(next_to_encode, last_encoded_item)
                        duplicate_count += 1
                        encoded_input_count += 1
                    next_to_encode += 1

            target_output_frames = requested if stopped_early else target_frames
            while next_to_encode < target_output_frames and last_encoded_item is not None:
                queue.push(_make_duplicate_item(next_to_encode, last_encoded_item))
                last_encoded_item = _make_duplicate_item(next_to_encode, last_encoded_item)
                duplicate_count += 1
                encoded_input_count += 1
                next_to_encode += 1

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
                    "frames_received": completed_count,
                    "frames_requested": requested,
                    "frames_completed": completed_count,
                    "frames_failed": failed_count,
                    "frames_queued_for_encoding": encoded_input_count,
                    "frames_written": encoder.frames_written,
                    "duplicate_frames": duplicate_count,
                    "drop_rate": queue.dropped / max(encoded_input_count, 1),
                    "request_wall_clock_s": request_finished_at - request_started_at,
                    "stopped_early": stopped_early,
                }
            )
            return CaptureResult(
                success=True,
                output_uri=req.output_uri,
                wall_clock_s=wall,
                output_size_bytes=size,
                sim_fps_avg=encoder.frames_written / max(wall, 1e-9),
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

    def capture_headless(self, req: CaptureRequest, stop_event=None,
                         camera_path: Optional[str] = None) -> CaptureResult:
        """Offscreen capture for headless Kit (no active viewport).

        Renders ``/World/summarization_camera`` via a Replicator render product and
        reads frames from an LdrColor annotator. Runs SYNCHRONOUSLY on the calling
        thread, pumping ``omni.kit.app`` once per frame so physics advances and the
        frame renders. Reuses the same overlay (timestamp + projected ID labels via
        a camera-path shim), encoder, and real-time pacing as ``capture()``.

        NOTE: requires Kit + GPU + omni.replicator.core; verify on hardware.
        """
        import asyncio
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

        start_wall = time.perf_counter()
        metadata = {
            "runner": "A2_headless_render_product",
            "resolution": f"{req.width}x{req.height}",
            "fps": req.fps,
            "duration_s": req.duration_s,
            "frame_clock": "headless_sync_app_pump",
        }
        tmp_dir = Path(tempfile.mkdtemp(prefix="ttsum_hl_"))
        tmp_mp4 = tmp_dir / f"hl_{datetime.now().strftime('%Y%m%dT%H%M%S')}.mp4"
        queue = FrameQueue(maxsize=32, drop_oldest=False)
        encoder = FrameEncoder(tmp_mp4, req.width, req.height, req.fps)
        camera_path = camera_path or "/World/summarization_camera"
        rp = None
        annot = None
        try:
            import numpy as np
            import omni.usd
            import omni.kit.app
            import omni.replicator.core as rep

            stage = omni.usd.get_context().get_stage()
            # replicator가 자체 스케줄링으로 timeline을 정지시키지 않게 (실측: render
            # product 초기화 직후 timeline stop → ratio_t 1→0→-1 패턴으로 물리 정지).
            try:
                rep.orchestrator.set_capture_on_play(False)
            except Exception:
                pass
            rp = rep.create.render_product(camera_path, (req.width, req.height))
            annot = rep.AnnotatorRegistry.get_annotator("LdrColor")
            annot.attach([rp])
            # 렌더러가 실제 사용한 view/projection을 프레임마다 제공 → 오버레이 투영 정확화.
            cam_annot = None
            try:
                cam_annot = rep.AnnotatorRegistry.get_annotator("camera_params")
                cam_annot.attach([rp])
            except Exception as e:
                print(f"[HL] camera_params annotator unavailable: {e!r} (재구성 행렬로 폴백)")
                cam_annot = None

            _cam_mtx_state = {"warned": False}

            def _renderer_matrices(_w, _h):
                if cam_annot is None:
                    return None
                try:
                    d = cam_annot.get_data()
                    view = d.get("cameraViewTransform")
                    proj = d.get("cameraProjection")
                    if view is None or proj is None:
                        raise KeyError(f"keys={list(d.keys())[:8]}")
                    from pxr import Gf
                    vals_v = [float(x) for x in np.asarray(view, dtype=np.float64).reshape(16)]
                    vals_p = [float(x) for x in np.asarray(proj, dtype=np.float64).reshape(16)]
                    return (Gf.Matrix4d(*vals_v), Gf.Matrix4d(*vals_p))
                except Exception as e:
                    if not _cam_mtx_state["warned"]:
                        _cam_mtx_state["warned"] = True
                        print(f"[HL] camera_params read failed: {e!r} (재구성 행렬로 폴백)")
                    return None

            # Shim exposing camera_path so the overlay's _get_camera_matrices works
            # without a viewport (matrices come from the camera prim, not the viewport).
            _shim_camera_path = camera_path

            class _CamShim:
                camera_path = _shim_camera_path

            composer = OverlayComposer(req.width, req.height, debug=DEBUG_OVERLAY_MARKERS)
            provider = self._overlay_provider or (
                self._default_provider_from_core(_CamShim(), stage, matrices_fn=_renderer_matrices)
                if self._core else None
            )
            encoder.start(queue)

            app = omni.kit.app.get_app()
            # 결정론 클럭 정합: app.update() 1회 = 1/fps sim 전진 = 프레임 1개 (부하 무관).
            # 실측(4070): 이 Kit의 고정 스텝은 1/60로 고정(timeCodesPerSecond로 안 바뀜)
            # → 캡처 fps=60이 정합 조건. 저레이트 데이터셋은 build_dataset --content-hz로.
            _configure_deterministic_clock(stage, req.fps, req.duration_s)

            # 프레임 전진자: Replicator 2.x에서는 맨 app.update()로 annotator 데이터가
            # 흐르지 않는다(실측: 60초 펌프에도 빈 배열, 이후 'not attached' 에러).
            # 정석은 orchestrator.step(delta_time=1/fps) — sim을 정확히 delta_time 전진시키고
            # 렌더+annotator 데이터 준비까지 동기로 보장(결정론적 오프라인 캡처의 표준 경로).
            # 구버전 호환을 위해 시그니처를 단계적으로 폴백한다.
            try:
                import omni.timeline as _otl
                _wtl = _otl.get_timeline_interface()
            except Exception:
                _wtl = None
            _expected_dt_adv = 1.0 / req.fps

            # Kit 내부에서는 동기 step() 금지(실측 OrchestratorError) → step_async 코루틴을
            # 걸고 완료될 때까지 app.update()로 런루프를 펌프하는 표준 패턴 사용.
            def _step_via_async(kwargs):
                fut = asyncio.ensure_future(rep.orchestrator.step_async(**kwargs))
                guard = time.perf_counter() + 30.0
                while not fut.done() and time.perf_counter() < guard:
                    app.update()
                if not fut.done():
                    fut.cancel()
                    raise RuntimeError("step_async timeout (30s)")
                exc = fut.exception()
                if exc is not None:
                    raise exc

            _adv_state = {"mode": "discover", "kwargs": None}

            def _advance():
                if _adv_state["mode"] == "step_async":
                    try:
                        _step_via_async(_adv_state["kwargs"])
                        return "step_async"
                    except Exception as e:
                        print(f"[HL] step_async failed mid-run: {e!r} -> app.update fallback")
                        _adv_state["mode"] = "app_update"
                elif _adv_state["mode"] == "discover":
                    step_async = getattr(getattr(rep, "orchestrator", None), "step_async", None)
                    if callable(step_async):
                        # 버전별 시그니처 차이를 단계적으로 시도, 성공한 kwargs를 캐시.
                        # pause_timeline=True 필수: False면 step의 delta_time 전진에 더해
                        # 재생 중인 타임라인이 렌더 대기 update 동안 자유 전진 → 실측 2배속.
                        for kw in ({"delta_time": _expected_dt_adv, "pause_timeline": True},
                                   {"delta_time": _expected_dt_adv},
                                   {}):
                            try:
                                _step_via_async(kw)
                                _adv_state["mode"] = "step_async"
                                _adv_state["kwargs"] = kw
                                print(f"[HL] advance mode=step_async kwargs={kw}")
                                return "step_async"
                            except TypeError:
                                continue
                            except Exception as e:
                                print(f"[HL] step_async{tuple(kw.items())} error: {e!r}")
                                continue
                    _adv_state["mode"] = "app_update"
                    print("[HL] orchestrator.step_async unavailable -> app.update fallback")
                if _wtl is not None and not _wtl.is_playing():
                    _wtl.play()
                app.update()
                return "app_update"

            # 워밍업: annotator가 실제 픽셀을 줄 때까지 전진(렌더러/그래프 초기화 소진).
            # 실측: RTX 준비 전에 만든 render product는 무효가 되어(attach가 안 붙어
            # 'not attached') 영원히 빈 데이터 → 감지 시 render product를 재생성한다.
            cam_prim = stage.GetPrimAtPath(camera_path) if stage else None
            print(f"[HL] camera {camera_path} valid={bool(cam_prim and cam_prim.IsValid())}")

            def _recreate_rp():
                nonlocal rp, annot
                try:
                    if annot is not None and rp is not None:
                        annot.detach([rp])
                except Exception:
                    pass
                try:
                    if rp is not None:
                        rp.destroy()
                except Exception:
                    pass
                name = ("LdrColor", "rgb")[(_warm_count // 120) % 2]
                rp = rep.create.render_product(camera_path, (req.width, req.height))
                annot = rep.AnnotatorRegistry.get_annotator(name)
                try:
                    annot.attach([rp])
                except TypeError:
                    annot.attach(rp)
                print(f"[HL] render product recreated (annotator={name}) at warmup #{_warm_count}")

            _warm_deadline = time.perf_counter() + 60.0
            _warm_count = 0
            _adv_mode = None
            while True:
                _adv_mode = _advance()
                _warm_count += 1
                _not_attached = False
                try:
                    _warm_arr = np.asarray(annot.get_data(), dtype=np.uint8)
                except Exception as e:
                    if _warm_count == 1:
                        print(f"[HL] warmup first get_data: {e!r}")
                    _not_attached = "not attached" in str(e).lower()
                    _warm_arr = np.zeros(0, dtype=np.uint8)
                if _warm_arr.size:
                    print(f"[HL] warmup done: {_warm_count} advances (mode={_adv_mode}) -> "
                          f"{_warm_arr.size} bytes")
                    break
                if _not_attached and _warm_count % 120 == 0:
                    try:
                        _recreate_rp()
                    except Exception as e:
                        print(f"[HL] render product recreate failed: {e!r}")
                if time.perf_counter() > _warm_deadline:
                    print(f"[HL] warmup TIMEOUT after {_warm_count} advances (mode={_adv_mode}) — "
                          f"annotator still empty; capture will likely produce no frames")
                    break

            target_frames = int(req.duration_s * req.fps)
            pushed = 0

            # 프로브(첫 N프레임): 정합·비용을 실기로 판정.
            #  ratio_t  = 타임라인 전진 / (1/fps)   → 1.0이면 클럭 정합
            #  ratio_d  = 객체 최대 변위 / (speed × 1/fps) → 1.0이면 물리도 정속
            #             (타임라인만 정합이고 물리가 substep을 못 돌면 여기서 <1로 검출)
            #  upd/read/enc ms = 프레임당 비용 분해 → "몇 배 단축 가능한지" 실측 근거
            _PROBE_FRAMES = 10
            try:
                import omni.timeline
                _timeline = omni.timeline.get_timeline_interface()
            except Exception:
                _timeline = None
            _expected_dt = 1.0 / req.fps
            _speed = float(getattr(self._core, "_wander_speed", 0.0) or 0.0) if self._core else 0.0
            _get_pos = getattr(self._core, "get_current_object_positions", None) if self._core else None
            _prev_pos = None

            for seq in range(target_frames):
                if stop_event is not None and stop_event.is_set():
                    break
                probing = seq < _PROBE_FRAMES
                # 프레임의 sim 시각을 먼저 고정: 이 update 중의 충돌(in-step)과 직후
                # 오버레이(post-step)가 같은 sim-time(seq/fps)으로 스탬프된다.
                if self._core is not None and hasattr(self._core, "set_sim_time"):
                    self._core.set_sim_time(seq * _expected_dt)
                # advance simulation + render one frame (wall-clock 페이싱 없음, orchestrator 우선)
                t_before = _timeline.get_current_time() if (probing and _timeline is not None) else None
                _t0 = time.perf_counter()
                _advance()
                _t1 = time.perf_counter()
                rgba = None
                try:
                    arr = np.asarray(annot.get_data(), dtype=np.uint8)
                    if arr.size:
                        rgba = arr.tobytes()
                except Exception as e:
                    if seq == 0:
                        print(f"[HL] annotator get_data failed: {e!r}")
                _t2 = time.perf_counter()
                if rgba is not None:
                    overlay_snapshot = provider(req.width, req.height) if provider else None
                    if overlay_snapshot is not None:
                        try:
                            rgba = composer.compose(rgba, overlay_snapshot)
                        except Exception as e:
                            if seq == 0:
                                print(f"[HL] overlay compose failed: {e!r}; continuing")
                    queue.push((seq, rgba, req.width, req.height))
                    pushed += 1
                if probing:
                    _t3 = time.perf_counter()
                    ratio_t = None
                    if _timeline is not None and t_before is not None:
                        ratio_t = (_timeline.get_current_time() - t_before) / _expected_dt
                    ratio_d = None
                    if callable(_get_pos) and _speed > 0.0:
                        try:
                            cur = _get_pos()
                            if cur and _prev_pos:
                                disp = max(
                                    (sum((float(cur[k][i]) - float(_prev_pos[k][i])) ** 2 for i in range(3)) ** 0.5
                                     for k in cur.keys() & _prev_pos.keys()),
                                    default=0.0,
                                )
                                ratio_d = disp / (_speed * _expected_dt)
                            if cur:  # 빈 dict로 prev를 덮으면 이후 내내 n/a가 됨
                                _prev_pos = cur
                        except Exception:
                            pass
                    rt = f"{ratio_t:.2f}" if ratio_t is not None else "n/a"
                    rd = f"{ratio_d:.2f}" if ratio_d is not None else "n/a"
                    print(
                        f"[HL probe] seq={seq} ratio_t={rt} ratio_d={rd}"
                        f" upd={(_t1 - _t0) * 1000:.1f}ms read={(_t2 - _t1) * 1000:.1f}ms"
                        f" enc={(_t3 - _t2) * 1000:.1f}ms"
                    )

            queue.close()
            encoder.join(timeout=30.0)
            if encoder.error:
                return CaptureResult(
                    success=False, output_uri=req.output_uri,
                    wall_clock_s=time.perf_counter() - start_wall, output_size_bytes=0,
                    error=encoder.error, dropped_frames=queue.dropped, metadata=metadata,
                )

            from gist.netai.time_travel_summarization.storage import from_uri

            adapter = from_uri(req.output_uri)
            adapter.put_file(req.output_uri, tmp_mp4, content_type="video/mp4")
            size = adapter.stat(req.output_uri).size
            wall = time.perf_counter() - start_wall
            metadata.update({"frames_written": encoder.frames_written, "frames_pushed": pushed})
            return CaptureResult(
                success=True, output_uri=req.output_uri, wall_clock_s=wall,
                output_size_bytes=size, sim_fps_avg=encoder.frames_written / max(wall, 1e-9),
                dropped_frames=queue.dropped, metadata=metadata,
            )
        except Exception as exc:
            queue.close()
            try:
                encoder.join(timeout=5.0)
            except Exception:
                pass
            return CaptureResult(
                success=False, output_uri=req.output_uri,
                wall_clock_s=time.perf_counter() - start_wall, output_size_bytes=0,
                error=repr(exc), dropped_frames=queue.dropped, metadata=metadata,
            )
        finally:
            try:
                if annot is not None and rp is not None:
                    annot.detach([rp])
                _ca = locals().get("cam_annot")
                if _ca is not None and rp is not None:
                    _ca.detach([rp])
                if rp is not None:
                    rp.destroy()
            except Exception:
                pass
            shutil.rmtree(tmp_dir, ignore_errors=True)
