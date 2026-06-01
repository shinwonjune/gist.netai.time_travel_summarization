import datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import carb

from .config import ExtensionConfig
from .paths import ExtensionPaths
from ..event_processing.summary_service import EventSummaryService
from ..playback.controller import PlaybackController
from ..playback.stage_object_controller import StageObjectController
from ..playback.trajectory_repository import TrajectoryRepository


DEFAULT_ASTRONAUT_USD = str((Path(__file__).resolve().parent.parent / "assets" / "Astronaut.usd").resolve())



class TimeTravelCore:
    """Facade that preserves the existing public API while delegating to focused services."""

    def __init__(self):
        self._module_dir = Path(__file__).resolve().parent.parent
        self._paths = ExtensionPaths(self._module_dir)
        self._config = None
        self._prim_map = {}
        self._repository = TrajectoryRepository()
        self._data_source = "local"
        self._last_data_load_error = ""
        self._playback = PlaybackController()
        self._stage_objects = StageObjectController()
        self._events = EventSummaryService(self._module_dir, self._repository)
        self._wander = None
        self._wander_speed = 120.0
        self._trace = None
        self._stage_objects.ensure_summarization_camera()
        self._capture_active: bool = False
        self._capture_start_time = None
        self._capture_duration_s: float = 0.0
        self._capture_output_path = None
        self._capture_pipeline = None

    def load_config(self, config_path: str) -> bool:
        try:
            path = Path(config_path)
            if not path.exists():
                self._last_data_load_error = f"Config file not found: {config_path}"
                carb.log_warn(f"[TimeTravel] {self._last_data_load_error}")
                return False

            self._config = ExtensionConfig.from_file(config_path)
            self._prim_map = dict(self._config.prim_map)
            self._playback.set_event_summary(self._config.event_summary)
            self._last_data_load_error = ""
            carb.log_info("[TimeTravel] Config loaded")
            return True
        except Exception as e:
            self._last_data_load_error = f"Failed to load config: {e}"
            carb.log_error(f"[TimeTravel] {self._last_data_load_error}")
            return False

    def _resolve_uri(self, value: str) -> Optional[str]:
        """'://'가 있으면 그대로, 아니면 config 디렉터리 기준 로컬 경로 -> file:// URI."""
        if not value:
            return None
        if "://" in value:
            return value
        path = Path(value)
        if not path.is_absolute():
            # Path 결합이 './' '../'를 정상 처리. lstrip 문자집합 제거 버그 회피.
            path = self._config.config_dir / value
        return path.resolve().as_uri()

    def _resolve_output_uri_for_mode(self, mode: str, value: str) -> Optional[str]:
        """Return configured output URI only when the active data source is Data Lake."""
        if mode != "lake":
            return None
        return self._resolve_uri(value)

    def get_output_root_uri_for_active_mode(self) -> Optional[str]:
        if not self._config:
            return None
        return self._resolve_output_uri_for_mode(
            self.get_data_source(),
            getattr(self._config, "output_root_uri", ""),
        )

    def get_event_list_uri_for_active_mode(self) -> Optional[str]:
        if not self._config:
            return None
        return self._resolve_output_uri_for_mode(
            self.get_data_source(),
            getattr(self._config, "event_list_uri", ""),
        )

    def get_video_output_uri_for_active_mode(self) -> Optional[str]:
        if not self._config:
            return None
        return self._resolve_output_uri_for_mode(
            self.get_data_source(),
            getattr(self._config, "video_output_uri", ""),
        )

    def _activate_data_source(self, mode: str) -> bool:
        try:
            if not self._config:
                self._last_data_load_error = "Config must be loaded before data"
                carb.log_error(f"[TimeTravel] {self._last_data_load_error}")
                return False

            lake_cfg = getattr(self._config, "lake", {}) or {}
            if mode == "lake":
                direct_data_uri = self._resolve_uri(lake_cfg.get("direct_data_uri", ""))
                if direct_data_uri:
                    repo_factory = TrajectoryRepository
                    uri = direct_data_uri
                    log_message = f"[TimeTravel] Lake mode test direct data URI: {uri}"
                else:
                    from ..playback.lake_repository import LakeTrajectoryRepository

                    manifest_uri = self._resolve_uri(lake_cfg.get("manifest_uri", ""))
                    if not manifest_uri:
                        self._last_data_load_error = "Data Lake manifest URI is not configured"
                        carb.log_warn(f"[TimeTravel] {self._last_data_load_error}")
                        return False
                    cache_chunks = int(lake_cfg.get("cache_chunks", 4))
                    prefetch_ahead = int(lake_cfg.get("prefetch_ahead", 1))
                    repo_factory = lambda: LakeTrajectoryRepository(
                        cache_chunks=cache_chunks,
                        prefetch_ahead=prefetch_ahead,
                    )
                    uri = manifest_uri
                    log_message = f"[TimeTravel] Lake mode: manifest URI: {uri}"
            elif mode == "local":
                repo_factory = TrajectoryRepository
                uri = self._config.data_uri
                log_message = f"[TimeTravel] Looking for data at URI: {uri}"
            else:
                self._last_data_load_error = f"Invalid data source: {mode}"
                carb.log_warn(f"[TimeTravel] {self._last_data_load_error}")
                return False

            output_root_uri = self._resolve_output_uri_for_mode(
                mode,
                getattr(self._config, "output_root_uri", ""),
            )
            repo = repo_factory()
            carb.log_info(log_message)

            loaded = repo.load_from_uri(uri)
            if not loaded:
                self._last_data_load_error = f"Data load failed for URI: {uri}"
                carb.log_error(f"[TimeTravel] {self._last_data_load_error}")
                return False

            old_repository = getattr(self, "_repository", None)
            if old_repository and hasattr(old_repository, "clear"):
                old_repository.clear()

            self._repository = repo
            self._events = EventSummaryService(
                self._module_dir,
                self._repository,
                output_root_uri=output_root_uri,
            )
            self._playback.configure_data_range(
                self._repository.data_start_time,
                self._repository.data_end_time,
            )
            carb.log_info(
                f"[TimeTravel] Data loaded: {len(self._repository.timestamps)} timestamps, "
                f"{self._repository.data_start_time} to {self._repository.data_end_time}"
            )
            self._data_source = mode
            self._last_data_load_error = ""
            if mode == "lake":
                prim_map = self.regenerate_astronauts_from_loaded_data()
                if not prim_map:
                    carb.log_warn("[TimeTravel] Data Lake data loaded, but no astronauts were generated")
                self.update_stage_objects()
                carb.log_warn(
                    f"[TimeTravel] Data Lake activation complete: "
                    f"timestamps={len(self._repository.timestamps)}, "
                    f"objects={self.get_loaded_object_count()}, "
                    f"astronauts={len(self._prim_map)}"
                )
            return True
        except Exception as e:
            self._last_data_load_error = f"Failed to load data: {e}"
            carb.log_error(f"[TimeTravel] {self._last_data_load_error}")
            import traceback

            carb.log_error(traceback.format_exc())
            return False

    def load_data(self) -> bool:
        try:
            if not self._config:
                self._last_data_load_error = "Config must be loaded before data"
                carb.log_error(f"[TimeTravel] {self._last_data_load_error}")
                return False

            lake_cfg = getattr(self._config, "lake", {}) or {}
            initial = "lake" if lake_cfg.get("enabled") and lake_cfg.get("manifest_uri") else "local"
            return self._activate_data_source(initial)
        except Exception as e:
            self._last_data_load_error = f"Failed to load data: {e}"
            carb.log_error(f"[TimeTravel] {self._last_data_load_error}")
            return False

    def set_data_source(self, mode: str) -> bool:
        if mode not in ("local", "lake"):
            self._last_data_load_error = f"Invalid data source: {mode}"
            carb.log_warn(f"[TimeTravel] {self._last_data_load_error}")
            return False
        if mode == "lake":
            lake_cfg = getattr(self._config, "lake", {}) or {}
            if not lake_cfg.get("direct_data_uri") and not lake_cfg.get("manifest_uri"):
                self._last_data_load_error = "Data Lake manifest URI is not configured"
                carb.log_warn(f"[TimeTravel] {self._last_data_load_error}")
                return False
        if self._activate_data_source(mode):
            self.set_to_earliest_time()
            return True
        return False

    def get_data_source(self) -> str:
        return getattr(self, "_data_source", "local")

    def get_last_data_load_error(self) -> str:
        return getattr(self, "_last_data_load_error", "")

    def _parse_timestamp(self, timestamp_str: str) -> datetime.datetime:
        return self._repository.parse_timestamp(timestamp_str)

    def _format_timestamp(self, dt: datetime.datetime) -> str:
        return self._repository.format_timestamp(dt)

    def set_time_range(self, start_time: datetime.datetime, end_time: datetime.datetime) -> bool:
        updated = self._playback.set_time_range(start_time, end_time)
        if updated:
            self.update_stage_objects()
        return updated

    def load_time_range(self, start_time: datetime.datetime, end_time: datetime.datetime) -> bool:
        """[start,end] 구간으로 재생 범위를 설정하고 시작점으로 이동.

        Lake 모드에서는 데이터 전체가 manifest로 인덱싱돼 있고, seek 시 해당 구간 청크만
        윈도우로 로드된다(프리페치로 경계 무지연). 단일 파일 모드에서도 동일 API로 동작.
        """
        full_start = self._repository.data_start_time
        full_end = self._repository.data_end_time
        if not full_start or not full_end:
            carb.log_warn("[TimeTravel] load_time_range: no data loaded")
            return False
        # 전체 범위로 리셋 후 좁혀야 재진입(다른 구간 재입력) 시 정상 동작
        self._playback.configure_data_range(full_start, full_end)
        if not self._playback.set_time_range(start_time, end_time):
            carb.log_warn(f"[TimeTravel] load_time_range: invalid range {start_time}..{end_time}")
            return False
        self._playback.set_current_time(self._playback.get_start_time())
        self.update_stage_objects()
        carb.log_info(
            f"[TimeTravel] Time range loaded: {self._playback.get_start_time()} .. {self._playback.get_end_time()}"
        )
        return True

    def get_data_start_time(self) -> datetime.datetime:
        return self._repository.data_start_time or datetime.datetime.now()

    def get_data_end_time(self) -> datetime.datetime:
        return self._repository.data_end_time or datetime.datetime.now()

    def get_data_at_time(self, timestamp: datetime.datetime) -> Dict:
        return self._repository.get_data_at_time(timestamp)

    def get_current_object_positions(self) -> Dict[str, tuple]:
        """Return live stage object positions, falling back to repository data."""
        positions = self._stage_objects.get_world_positions(self._prim_map)
        if positions:
            return positions
        current_time = self._playback.get_current_time()
        if not current_time:
            return {}
        return self.get_data_at_time(current_time)

    def update_stage_objects(self):
        current_time = self._playback.get_current_time()
        if not current_time:
            return
        self._stage_objects.update_stage_objects(self._prim_map, self.get_data_at_time(current_time))

    def set_to_earliest_time(self):
        if self._repository.data_start_time:
            self._playback.set_current_time(self._repository.data_start_time)
            self.update_stage_objects()

    def set_current_time(self, dt: datetime.datetime):
        self._playback.set_current_time(dt)
        self.update_stage_objects()

    def get_progress(self) -> float:
        return self._playback.get_progress()

    def set_progress(self, progress: float):
        self._playback.set_progress(progress)
        self.update_stage_objects()

    def toggle_playback(self):
        self._playback.toggle_playback()

    def start_capture(self, duration_s: float = 0.0, output_path: Optional[str] = None) -> bool:
        """실시간 viewport 캡처 시작. duration_s=0 이면 default 60초. 중간에 stop_capture로 중단 가능."""
        if self._capture_active:
            carb.log_warn("[Capture] already active")
            return False
        # duration_s 0 이하 → default 60초
        effective_duration = float(duration_s) if duration_s > 0 else 60.0
        if output_path is None:
            from datetime import datetime as _dt
            ts = _dt.now().strftime("%Y%m%dT%H%M%S")
            video_output_uri = self.get_video_output_uri_for_active_mode()
            if video_output_uri:
                output_path = f"{video_output_uri.rstrip('/')}/video_{ts}.mp4"
            else:
                # config의 video_output_dir 사용. 없으면 default "artifacts/video"
                output_dir_str = (
                    getattr(self._config, "video_output_dir", "artifacts/video")
                    if self._config
                    else "artifacts/video"
                )
                output_dir = Path(output_dir_str)
                if not output_dir.is_absolute():
                    output_dir = self._module_dir / output_dir
                output_dir.mkdir(parents=True, exist_ok=True)
                output_path = str(output_dir / f"video_{ts}.mp4")
        self._capture_active = True
        self._capture_duration_s = effective_duration
        self._capture_output_path = output_path
        import threading
        import time as _time
        self._capture_stop_event = threading.Event()
        self._capture_start_time = _time.perf_counter()
        self._start_capture_backend(output_path)
        carb.log_warn(f"[Capture] started duration={effective_duration:g}s output={output_path}")
        return True

    def stop_capture(self) -> Optional[str]:
        if not self._capture_active:
            return None
        out = self._capture_output_path
        # stop_event 신호 → background worker가 capture loop 빠져나오고 인코더 마무리 후 파일 저장
        if getattr(self, "_capture_stop_event", None) is not None:
            self._capture_stop_event.set()
        carb.log_warn(f"[Capture] stop requested -> {out}")
        return out

    def is_capturing(self) -> bool:
        return self._capture_active

    def _start_capture_backend(self, output_path: str) -> None:
        """RealtimeCaptureRunner를 background thread에서 실행. duration은 runner 내부에서 자동 처리."""
        import threading
        from pathlib import Path as _P
        from ..video_capture import CaptureRequest, RealtimeCaptureRunner

        # duration_s=0 (무한)이면 기본 60초로 fallback. runner.capture는 blocking이라 외부 stop 불가.
        duration = self._capture_duration_s if self._capture_duration_s > 0 else 60.0
        output_uri = output_path if "://" in output_path else _P(output_path).resolve().as_uri()

        def _worker():
            try:
                runner = RealtimeCaptureRunner(core=self)
                req = CaptureRequest(duration_s=duration, output_uri=output_uri, label="ui_capture")
                res = runner.capture(req, stop_event=self._capture_stop_event)
                if res.success:
                    meta = res.metadata or {}
                    parsed_output = urlparse(res.output_uri)
                    video_name = Path(parsed_output.path or res.output_uri).name
                    carb.log_warn(
                        f"[Capture] done {res.wall_clock_s:.1f}s "
                        f"{res.output_size_bytes // 1024}KB "
                        f"frames={meta.get('frames_written', '?')}/{meta.get('frames_requested', '?')} "
                        f"completed={meta.get('frames_completed', '?')} "
                        f"dup={meta.get('duplicate_frames', '?')} "
                        f"drop={res.dropped_frames}"
                    )
                    carb.log_warn(f"[Capture] video_uri={res.output_uri}")
                    carb.log_warn(f"[Capture] video_name={video_name}")
                    carb.log_warn(f"[Capture] paste_to_vlm_client={res.output_uri}")
                else:
                    carb.log_warn(f"[Capture] FAILED: {res.error}")
            except Exception as exc:
                carb.log_warn(f"[Capture] worker exception: {exc!r}")
            finally:
                self._capture_active = False
                self._capture_pipeline = None
                self._capture_stop_event = None

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        self._capture_pipeline = thread

    def _stop_capture_backend(self) -> None:
        """RealtimeCaptureRunner.capture는 blocking이라 외부 stop 불가.
        duration_s까지 runner가 자체 종료. 여기서는 flag만 해제."""
        self._capture_pipeline = None

    def update(self, dt: float):
        self._playback.update(dt, self._parse_timestamp, self.set_current_time, self._on_event_requested)
        if self._trace and self._trace.active:
            try:
                self._trace.tick()
            except Exception as e:
                carb.log_warn(f"[Trace] tick failed: {e}")
        # capture auto-stop은 RealtimeCaptureRunner가 내부에서 처리

    def start_wander(self) -> bool:
        """Start PhysX wandering when Physics mode has created a controller."""
        if not self._wander:
            carb.log_warn("[TimeTravel] start_wander: Physics mode is not active")
            return False
        self._wander.start()
        return True

    def set_velocity_mode(self, mode: str) -> bool:
        """콘솔에서 velocity 모드 즉시 토글 (per_tick / on_enter)."""
        if not self._wander:
            carb.log_warn("[TimeTravel] set_velocity_mode: Physics 모드 아님")
            return False
        return self._wander.set_velocity_mode(mode)

    def get_wander_speed(self) -> float:
        if self._wander:
            return self._wander.get_speed()
        return float(getattr(self, "_wander_speed", 120.0))

    def set_wander_speed(self, speed: float) -> bool:
        try:
            speed = float(speed)
        except (TypeError, ValueError):
            carb.log_warn(f"[TimeTravel] invalid wander speed: {speed!r}")
            return False
        if speed <= 0.0:
            carb.log_warn(f"[TimeTravel] invalid wander speed: {speed:g}")
            return False
        self._wander_speed = speed
        if self._wander:
            return self._wander.set_speed(speed)
        return True

    def stop_wander(self) -> bool:
        if not self._wander:
            return False
        self._wander.stop()
        return True

    def is_wandering(self) -> bool:
        return bool(self._wander and getattr(self._wander, "is_active", lambda: False)())

    def start_trace(self, output_path: Optional[str] = None) -> str:
        """Start streaming current astronaut coordinates to a trajectory CSV."""
        from datetime import datetime as _dt

        import omni.usd

        from ..physics import TraceRecorder

        if self._trace and self._trace.active:
            carb.log_warn("[TimeTravel] trace already active")
            return str(self._trace.output_path)

        if output_path is None:
            ts = _dt.now().strftime("%Y%m%dT%H%M%S")
            trace_dir = self._paths.artifacts_dir / "trace"
            trace_dir.mkdir(parents=True, exist_ok=True)
            output_path = str(trace_dir / f"physics_trace_{ts}.csv")

        resolved = {}
        stage = omni.usd.get_context().get_stage()
        if stage:
            for objid, prim_path in self._prim_map.items():
                prim = stage.GetPrimAtPath(prim_path)
                if prim and prim.IsValid():
                    resolved[objid] = prim
        else:
            carb.log_warn("[Trace] recording without an active stage")

        self._trace = TraceRecorder(resolved, output_path)
        self._trace.start()
        carb.log_warn(f"[Trace] recording started -> {output_path}")
        return output_path

    def stop_trace(self) -> Optional[str]:
        if not self._trace or not self._trace.active:
            return None
        out = self._trace.stop()
        rows = self._trace.row_count
        carb.log_warn(f"[Trace] saved {rows} rows to {out}")
        return str(out)

    def is_tracing(self) -> bool:
        return bool(self._trace and self._trace.active)

    def set_physics_mode(self) -> None:
        """Enable PhysX-driven wandering for the configured astronaut prims."""
        import omni.usd
        from pxr import UsdGeom

        from ..physics import WanderController, create_bounding_box, ensure_physics_scene, wrap_with_collision_proxy

        stage = omni.usd.get_context().get_stage()
        if not stage:
            carb.log_warn("[TimeTravel] Cannot enable Physics mode without an active stage")
            return

        if self._wander:
            self._wander.stop()
            self._wander = None

        self._playback.set_mode("physics")
        ensure_physics_scene(stage)

        meters_per_unit = UsdGeom.GetStageMetersPerUnit(stage) or 1.0
        m_to_units = 1.0 / meters_per_unit
        carb.log_warn(f"[Physics] stage metersPerUnit={meters_per_unit} m_to_units={m_to_units}")

        # walls 위치·크기를 trajectory 좌표 범위 + margin으로 자동 결정
        # (hardcoded 5×3×5 / origin은 사용자 trajectory 범위와 안 맞을 수 있음)
        is_y_up = UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y
        coord_range = self._repository.get_coord_range()
        margin = 2.0 * m_to_units          # 영역 바깥 여유 (m)
        min_height = 3.0 * m_to_units      # 최소 천장 높이 (m)
        if coord_range:
            mins, maxs = coord_range
            carb.log_warn(f"[Physics] coord_range mins={mins} maxs={maxs}")
            cx = (mins[0] + maxs[0]) / 2.0
            cy = (mins[1] + maxs[1]) / 2.0
            cz = (mins[2] + maxs[2]) / 2.0
            ext_x = (maxs[0] - mins[0])
            ext_y = (maxs[1] - mins[1])
            ext_z = (maxs[2] - mins[2])
            if is_y_up:
                # horizontal = X·Z, vertical = Y
                width, depth = ext_x, ext_z
                height = max(ext_y, min_height)
                # floor가 ground level(mins[1]) 살짝 아래로 가도록 center.y 조정
                box_center = (cx, mins[1] + height / 2.0, cz)
                box_size = (width, height, depth)
            else:
                # horizontal = X·Y, vertical = Z
                width, depth = ext_x, ext_y
                height = max(ext_z, min_height)
                box_center = (cx, cy, mins[2] + height / 2.0)
                box_size = (width, depth, height)
        else:
            # trajectory 미로드 시 기본값
            if is_y_up:
                box_center = (0.0, 1.5, 0.0)
                box_size = (5.0, 3.0, 5.0)
            else:
                box_center = (0.0, 0.0, 1.5)
                box_size = (5.0, 5.0, 3.0)

        carb.log_info(f"[Physics] bounding box center={box_center} size={box_size} up_axis={'Y' if is_y_up else 'Z'}")
        create_bounding_box(stage, center=box_center, size=box_size)

        rigid_prims = []
        for objid, prim_path in self._prim_map.items():
            prim = stage.GetPrimAtPath(prim_path)
            if not prim or not prim.IsValid():
                carb.log_warn(f"[Physics] skip invalid prim: objid={objid} path={prim_path}")
                continue
            proxy_path = prim.GetPath().AppendChild("__phys_proxy__")
            has_collision_proxy = bool(stage.GetPrimAtPath(proxy_path))
            try:
                xform_cache = UsdGeom.XformCache(0)
                world_xform = xform_cache.GetLocalToWorldTransform(prim)
                translate = world_xform.ExtractTranslation()
                carb.log_warn(
                    f"[Physics] wrap objid={objid} prim={prim_path} "
                    f"world_pos=({translate[0]:.2f}, {translate[1]:.2f}, {translate[2]:.2f}) "
                    f"has_collision_proxy={has_collision_proxy}"
                )
            except Exception as e:
                carb.log_warn(f"[Physics] failed to log world_pos for objid={objid}: {e}")
            rigid_prims.append(wrap_with_collision_proxy(stage, prim, shape="cylinder", visible=True))

        # wander는 사용자가 Move 버튼으로 명시 시작. 여기서는 인스턴스만 생성.
        self._wander = WanderController(rigid_prims, speed=self._wander_speed)

        try:
            import omni.timeline

            omni.timeline.get_timeline_interface().play()
        except Exception as e:
            carb.log_warn(f"[TimeTravel] Physics mode enabled, but timeline play request failed: {e}")

    def set_playback_mode(self) -> None:
        """Return to trajectory playback and remove transient physics controls."""
        import omni.usd

        from ..physics import unwrap

        if self._wander:
            self._wander.stop()
            self._wander = None

        stage = omni.usd.get_context().get_stage()
        if stage:
            for prim_path in self._prim_map.values():
                prim = stage.GetPrimAtPath(prim_path)
                if prim and prim.IsValid():
                    unwrap(stage, prim)
            if stage.GetPrimAtPath("/World/PhysicsWalls"):
                stage.RemovePrim("/World/PhysicsWalls")

        self._playback.set_mode("playback")
        self.update_stage_objects()

    def get_mode(self) -> str:
        return self._playback.get_mode()

    def go_to_next_event(self):
        self._playback.go_to_next_event(
            self._parse_timestamp,
            self.set_current_time,
            self._on_event_requested,
        )

    def get_start_time(self) -> datetime.datetime:
        return self._playback.get_start_time() or datetime.datetime.now()

    def get_end_time(self) -> datetime.datetime:
        return self._playback.get_end_time() or datetime.datetime.now()

    def get_current_time(self) -> datetime.datetime:
        return self._playback.get_current_time() or datetime.datetime.now()

    def get_simulation_time(self) -> Optional[datetime.datetime]:
        return self._playback.get_current_time()

    def get_stage_time_string(self) -> str:
        current_time = self._playback.get_current_time()
        if current_time:
            return current_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return "No time set"

    def is_playing(self) -> bool:
        return self._playback.is_playing()

    def get_playback_speed(self) -> float:
        return self._playback.get_playback_speed()

    def set_playback_speed(self, speed: float):
        self._playback.set_playback_speed(speed)

    def has_data(self) -> bool:
        return self._repository.has_data()

    def get_stage_object_count(self) -> int:
        return len(self._prim_map)

    def get_loaded_object_count(self) -> int:
        get_object_ids = getattr(self._repository, "get_object_ids", None)
        if callable(get_object_ids):
            return len(get_object_ids())
        if self._repository.data_start_time:
            return len(self._repository.get_data_at_time(self._repository.data_start_time))
        return 0

    def has_events(self) -> bool:
        return len(self._playback.get_event_summary()) > 0

    def set_use_event_summary(self, use: bool):
        self._playback.set_use_event_summary(use)

    def get_summary_events(self) -> List[str]:
        return self._playback.get_event_summary()

    def load_events_from_positions_jsonl(self) -> bool:
        try:
            event_list_uri = None
            if self._config:
                event_list_uri = self.get_event_list_uri_for_active_mode()
            event_summary = self._events.load_events_from_event_list(event_list_uri=event_list_uri)
            if not event_summary:
                return False
            self._playback.set_event_summary(event_summary)
            return True
        except Exception as e:
            carb.log_error(f"[TimeTravel] Failed to load events: {e}")
            return False

    def parse_unique_objids(self, csv_path: str) -> List[str]:
        try:
            return self._repository.parse_unique_objids(csv_path)
        except Exception as e:
            carb.log_error(f"[TimeTravel] Failed to parse objids: {e}")
            return []

    def clear_timetravel_objects(self):
        if self._wander:
            self._wander.stop()
            self._wander = None
        self._stage_objects.clear_timetravel_objects()
        self._repository.clear()
        self._prim_map.clear()
        self._playback.configure_data_range(None, None)
        self._playback.set_event_summary([])

    def create_astronaut_prim(self, index: int) -> str:
        astronaut_usd = self._config.astronaut_usd if self._config else ""
        if not astronaut_usd:
            carb.log_error("[TimeTravel] astronaut_usd not specified in config")
            return ""
        return self._stage_objects.create_astronaut_prim(index, astronaut_usd)

    def auto_generate_astronauts(self) -> Dict[str, str]:
        if not self._config:
            carb.log_error("[TimeTravel] Config must be loaded before auto-generation")
            return {}

        if not self._config.astronaut_usd:
            self._config.astronaut_usd = DEFAULT_ASTRONAUT_USD
            carb.log_info(f"[TimeTravel] Using default astronaut USD: {DEFAULT_ASTRONAUT_USD}")

        data_uri = self._config.data_uri
        if "://" in self._config.data_path:
            objids = TrajectoryRepository.parse_unique_objids_from_uri(data_uri)
        else:
            csv_path = self._config.resolve_from_config(self._config.data_path)
            if not csv_path.exists():
                carb.log_error(f"[TimeTravel] Data file not found: {csv_path}")
                return {}
            objids = self.parse_unique_objids(str(csv_path))
        if not objids:
            carb.log_error("[TimeTravel] No objids found in CSV")
            return {}

        self.clear_timetravel_objects()

        prim_map = {}
        for i, objid in enumerate(objids, start=1):
            prim_path = self.create_astronaut_prim(i)
            if prim_path:
                prim_map[objid] = prim_path

        self.hide_all_cameras()
        self._prim_map = prim_map
        return prim_map

    def regenerate_astronauts_from_loaded_data(self) -> Dict[str, str]:
        if not self._config:
            carb.log_error("[TimeTravel] Config must be loaded before auto-generation")
            return {}
        if not self._config.astronaut_usd:
            self._config.astronaut_usd = DEFAULT_ASTRONAUT_USD
            carb.log_info(f"[TimeTravel] Using default astronaut USD: {DEFAULT_ASTRONAUT_USD}")

        objids = []
        get_object_ids = getattr(self._repository, "get_object_ids", None)
        if callable(get_object_ids):
            objids = get_object_ids()
        if not objids and self._repository.data_start_time:
            objids = sorted(self._repository.get_data_at_time(self._repository.data_start_time).keys())
        if not objids:
            carb.log_error("[TimeTravel] No objids found in loaded data")
            return {}

        if self._wander:
            self._wander.stop()
            self._wander = None
        self._stage_objects.clear_timetravel_objects()

        prim_map = {}
        for i, objid in enumerate(objids, start=1):
            prim_path = self.create_astronaut_prim(i)
            if prim_path:
                prim_map[objid] = prim_path
            else:
                carb.log_error(f"[TimeTravel] Failed to create astronaut prim for objid={objid}")

        self.hide_all_cameras()
        self._prim_map = prim_map
        if prim_map:
            carb.log_warn(
                f"[TimeTravel] Regenerated {len(prim_map)} astronauts from loaded data "
                f"(objids={len(objids)})"
            )
        else:
            carb.log_error(
                f"[TimeTravel] Loaded data has {len(objids)} objids, but no astronaut prims were created"
            )
        return prim_map

    def hide_all_cameras(self):
        self._stage_objects.hide_all_cameras()

    def process_event_json(self, json_path: str) -> bool:
        try:
            success = self._events.process_event_json(json_path)
            if success:
                self._playback.set_event_summary(list(self._events._event_positions.keys()))
            return success
        except Exception as e:
            carb.log_error(f"[TimeTravel] Event processing failed: {e}")
            return False

    def should_auto_generate(self) -> bool:
        return bool(self._config and self._config.auto_generate)

    def set_lookup_mode(self, mode: str) -> bool:
        """Lookup 알고리즘 모드 변경. 'linear' | 'bisect' | 'hybrid' | 'invalidate' | 'bidirectional'."""
        try:
            self._repository.set_lookup_mode(mode)
            carb.log_warn(f"[Lookup] mode set to {mode}")
            return True
        except ValueError as e:
            carb.log_warn(f"[Lookup] invalid mode: {e}")
            return False

    def get_lookup_mode(self) -> str:
        return self._repository.get_lookup_mode()

    def start_lookup_benchmark(self, mode: str, pattern: str) -> bool:
        """Live lookup benchmark 시작.

        mode: 'linear' | 'bisect' | 'hybrid' | 'invalidate'
        pattern: 자유 라벨 (예: 'forward', 'backward', 'random_seek')
        """
        try:
            self._repository.set_lookup_mode(mode)
        except ValueError as e:
            carb.log_warn(f"[Benchmark] invalid mode: {e}")
            return False
        self._repository.start_benchmark(pattern)
        carb.log_warn(f"[Benchmark] started mode={mode} pattern={pattern}")
        return True

    def stop_lookup_benchmark(self) -> dict:
        """Live lookup benchmark 종료. 결과 표 + CSV 저장."""
        result = self._repository.stop_benchmark()
        carb.log_warn(
            f"[Benchmark] mode={result['mode']:12s} pattern={result['pattern']:12s} "
            f"calls={result['call_count']:6d} total={result['total_seconds']*1000:.3f}ms "
            f"per_call={result['per_call_us']:.3f}us"
        )
        try:
            out_dir = self._paths.artifacts_dir / "benchmarks"
            out_dir.mkdir(parents=True, exist_ok=True)
            csv_path = out_dir / "lookup_runtime_benchmark.csv"
            is_new = not csv_path.exists()
            with open(csv_path, "a", encoding="utf-8", newline="") as f:
                import csv as _csv_local
                writer = _csv_local.writer(f)
                if is_new:
                    writer.writerow(["timestamp", "mode", "pattern", "call_count",
                                     "total_seconds", "per_call_us"])
                from datetime import datetime as _dt_local
                writer.writerow([_dt_local.now().isoformat(), result["mode"],
                                 result["pattern"], result["call_count"],
                                 result["total_seconds"], result["per_call_us"]])
            carb.log_warn(f"[Benchmark] CSV appended: {csv_path}")
        except Exception as e:
            carb.log_warn(f"[Benchmark] CSV write failed: {e}")
        return result

    def run_lookup_benchmark_suite(self, duration_s: float = 5.0, fps: int = 60) -> list:
        """3 lookup modes × forward/backward = 6 runs 자동 측정.

        각 run 전 timeline을 안전한 시작점으로 reset.
        """
        n_ticks = int(duration_s * fps)
        dt = 1.0 / fps
        runs = [
            ("bisect", "forward", 1.0),
            ("hybrid", "forward", 1.0),
            ("invalidate", "forward", 1.0),
            ("bisect", "backward", -1.0),
            ("hybrid", "backward", -1.0),
            ("invalidate", "backward", -1.0),
        ]
        results = []
        for mode, pattern, speed in runs:
            # 1) Reset timeline to a safe starting point
            if speed > 0:
                self.set_to_earliest_time()
            else:
                end = self._repository.data_end_time
                if end:
                    self.set_current_time(end)
            # 2) Ensure not playing before start
            if self._playback.is_playing():
                self._playback.toggle_playback()
            # 3) Start benchmark + play
            self.start_lookup_benchmark(mode, pattern)
            self._playback.set_playback_speed(speed)
            self._playback.toggle_playback()
            # 4) Drive updates manually (Kit main thread blocked here)
            for _ in range(n_ticks):
                self.update(dt)
            # 5) Stop play + benchmark
            if self._playback.is_playing():
                self._playback.toggle_playback()
            results.append(self.stop_lookup_benchmark())
        return results

    def set_visual_complexity(self, level: str) -> bool:
        if not self._config:
            carb.log_warn("[Complexity] config not loaded")
            return False
        return self._stage_objects.set_visual_complexity(
            level,
            self._config.visibility_groups,
            self._config.complexity_levels,
        )

    def get_complexity_levels(self) -> list:
        if not self._config:
            return ["Full", "Simplified", "Abstract"]
        return list(self._config.complexity_levels.keys())

    def _on_event_requested(self, timestamp: str):
        self._stage_objects.move_camera_to_event(self._events.get_event_position(timestamp))
