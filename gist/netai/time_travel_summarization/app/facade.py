"""TimeTravelCore — 확장의 단일 진입 facade.

상태(레포지토리·prim_map·캡처/물리 플래그)는 전부 이 클래스가 소유하고,
동작은 도메인 서비스 모듈(capture/physics/data/object/benchmark_service)에
위임한다. 서비스가 상태를 들지 않는 이유: 테스트가 __new__ + 속성 주입으로
core를 구성하고, extension.py가 shutdown 시 _wander를 직접 만지기 때문.
공개 API는 분해 전과 동일하다.
"""
import datetime
from pathlib import Path
from typing import Dict, List, Optional

import carb

from . import benchmark_service, capture_service, data_service, object_service, physics_service
from .paths import ExtensionPaths
from ..event_processing.summary_service import EventSummaryService
from ..playback.controller import PlaybackController
from ..playback.stage_object_controller import StageObjectController
from ..playback.trajectory_repository import TrajectoryRepository

DEFAULT_ASTRONAUT_USD = object_service.DEFAULT_ASTRONAUT_USD


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
        self._wander_speed = 240.0
        self._trace = None
        self._collisions = None
        self._collision_distance = None
        self._physics_bounds = None
        self._prim_map_full = {}
        self._stage_objects.ensure_summarization_camera()
        self._capture_active: bool = False
        self._capture_start_time = None
        self._capture_duration_s: float = 0.0
        self._capture_output_path = None
        self._capture_pipeline = None
        # sim-time 마스터 클럭: headless 캡처가 프레임마다 set_sim_time(seq/fps)로 전진.
        # 라벨 시각 = _capture_start_dt + _sim_time → 오버레이·CSV·클립 경계가 한 시계.
        self._capture_start_dt = None
        self._sim_time: float = 0.0
        self._use_sim_clock: bool = False
        self._wander_seed = None
        # 캡처 완료 알림(성공 시 output URI 전달) — extension.py가 VLM 창을 배선.
        self._capture_complete_cb = None
        # 재생 공백 점프: 데이터 공백이 이 값(초)을 넘으면 시계를 다음 데이터로
        # 순간이동(시계가 주인인 재생 구조에서, 공백 23h를 실시간으로 기지 않게).
        self._gap_skip_s = 10.0

    # ---- 데이터 소스 / config (data_service) --------------------------------

    def load_config(self, config_path: str) -> bool:
        return data_service.load_config(self, config_path)

    def _resolve_uri(self, value: str) -> Optional[str]:
        return data_service.resolve_uri(self, value)

    def _resolve_output_uri_for_mode(self, mode: str, value: str) -> Optional[str]:
        return data_service.resolve_output_uri_for_mode(self, mode, value)

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

    def load_data(self) -> bool:
        return data_service.load_data(self)

    def set_data_source(self, mode: str) -> bool:
        return data_service.set_data_source(self, mode)

    def get_data_source(self) -> str:
        return getattr(self, "_data_source", "local")

    def get_last_data_load_error(self) -> str:
        return getattr(self, "_last_data_load_error", "")

    # ---- 재생 (PlaybackController 패스스루) ----------------------------------

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

    def set_gap_skip_threshold(self, seconds: float) -> None:
        """공백 점프 임계값(초). 0 = 기능 끔. 실데이터 Hz가 불규칙하면 크게."""
        self._gap_skip_s = max(0.0, float(seconds))

    def _maybe_skip_gap(self, t: datetime.datetime) -> datetime.datetime:
        """재생 진행 시각 t 앞(역재생이면 뒤)에 데이터 공백이 있으면 점프 목적지 반환."""
        thr = getattr(self, "_gap_skip_s", 0.0)
        if not thr:
            return t
        forward = self._playback.get_playback_speed() >= 0
        fn = getattr(self._repository, "next_data_time" if forward else "prev_data_time", None)
        if not callable(fn):
            return t
        try:
            target = fn(t)
        except Exception:
            return t
        if target is None:
            return t
        gap = (target - t).total_seconds() if forward else (t - target).total_seconds()
        if gap > thr:
            carb.log_warn(f"[TimeTravel] data gap {gap:.0f}s at {t} -> jump to {target}")
            return target
        return t

    def _on_playback_tick(self, t: datetime.datetime) -> None:
        self.set_current_time(self._maybe_skip_gap(t))

    def update(self, dt: float):
        self._playback.update(dt, self._parse_timestamp, self._on_playback_tick, self._on_event_requested)
        if self._trace and self._trace.active:
            try:
                self._trace.tick()
            except Exception as e:
                carb.log_warn(f"[Trace] tick failed: {e}")
        # capture auto-stop은 RealtimeCaptureRunner가 내부에서 처리

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

    def is_playing(self) -> bool:
        return self._playback.is_playing()

    def get_playback_speed(self) -> float:
        return self._playback.get_playback_speed()

    def set_playback_speed(self, speed: float):
        self._playback.set_playback_speed(speed)

    # ---- 캡처 (capture_service) ---------------------------------------------

    def start_capture(self, duration_s: float = 0.0, output_path: Optional[str] = None) -> bool:
        return capture_service.start_capture(self, duration_s, output_path)

    def run_capture_headless(self, duration_s: float = 0.0, output_path: Optional[str] = None,
                             camera_path: Optional[str] = None,
                             capture_start_dt: Optional[datetime.datetime] = None,
                             render_fps: Optional[int] = None) -> Optional[str]:
        return capture_service.run_capture_headless(
            self, duration_s, output_path,
            camera_path=camera_path,
            capture_start_dt=capture_start_dt,
            render_fps=render_fps,
        )

    def stop_capture(self) -> Optional[str]:
        return capture_service.stop_capture(self)

    def is_capturing(self) -> bool:
        return self._capture_active

    def set_capture_complete_callback(self, cb) -> None:
        """UI 캡처 성공 시 output URI로 호출될 콜백 등록(캡처 워커 스레드에서 호출됨)."""
        self._capture_complete_cb = cb

    def _write_capture_sidecar(self, output_path: str, duration_s: float, fps: Optional[int] = None) -> None:
        capture_service.write_capture_sidecar(self, output_path, duration_s, fps=fps)

    # ---- sim-time master clock (headless 캡처의 단일 시계) -----------------
    # headless 루프가 프레임마다 set_sim_time(seq/fps)를 호출 → 그 프레임의
    # 오버레이·충돌 CSV가 전부 capture_start + sim_time으로 스탬프된다.
    # 렌더 속도(wall-clock)와 무관하므로 클립 슬라이싱과 라벨이 항상 정합.

    def set_sim_time(self, seconds: float) -> None:
        self._sim_time = max(0.0, float(seconds))

    def get_sim_time(self) -> float:
        return self._sim_time

    def get_sim_clock_datetime(self) -> datetime.datetime:
        if self._capture_start_dt is None:
            return datetime.datetime.now()
        return self._capture_start_dt + datetime.timedelta(seconds=self._sim_time)

    def get_simulation_time(self) -> Optional[datetime.datetime]:
        # Physics 모드: headless 캡처 중엔 sim 클럭(라벨과 동일 시계), 그 외(인터랙티브
        # 프리뷰)는 wall-clock 폴백 — 프리뷰 오버레이가 실시간으로 흐르게.
        if self._playback.get_mode() == "physics":
            if self._use_sim_clock:
                return self.get_sim_clock_datetime()
            return datetime.datetime.now()
        return self._playback.get_current_time()

    def get_stage_time_string(self) -> str:
        # 오버레이 형식 = timefmt.PRECISION (collisions CSV와 공유 → 추론↔라벨 정합).
        from ..timefmt import format_event_time

        if self._playback.get_mode() == "physics":
            if self._use_sim_clock:
                return format_event_time(self.get_sim_clock_datetime())
            return format_event_time(datetime.datetime.now())
        current_time = self._playback.get_current_time()
        if current_time:
            return format_event_time(current_time)
        return "No time set"

    # ---- physics / wander (physics_service) ---------------------------------

    def set_physics_mode(self) -> None:
        physics_service.set_physics_mode(self)

    def set_playback_mode(self) -> None:
        physics_service.set_playback_mode(self)

    def get_physics_bounds(self) -> Optional[dict]:
        """Bounds computed at set_physics_mode: {center, size, is_y_up}. None if not set."""
        return self._physics_bounds

    def set_wander_seed(self, seed) -> None:
        """에피소드 재현성: set_physics_mode 전에 호출하면 wander heading이 seed 결정적."""
        self._wander_seed = seed

    def start_wander(self) -> bool:
        """Start PhysX wandering when Physics mode has created a controller."""
        if not self._wander:
            carb.log_warn("[TimeTravel] start_wander: Physics mode is not active")
            return False
        # 충돌 기록은 Capture가 소유(start_capture에서 시작) → 여기선 시작하지 않음.
        # Move만 누르면 객체는 움직이지만 CSV는 안 남고, Capture를 눌러야 기록 시작.
        self._wander.start()
        return True

    def stop_wander(self) -> bool:
        if not self._wander:
            return False
        self._wander.stop()
        # recorder는 Capture가 소유 → 여기서 멈추지 않음(캡처 창과 분리되지 않게).
        return True

    def is_wandering(self) -> bool:
        return bool(self._wander and getattr(self._wander, "is_active", lambda: False)())

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

    def _on_collision_event(self, prim_path: str, position, kind: str) -> None:
        physics_service.on_collision_event(self, prim_path, position, kind)

    def _start_collision_recorder(self) -> None:
        physics_service.start_collision_recorder(self)

    def _stop_collision_recorder(self) -> None:
        physics_service.stop_collision_recorder(self)

    def start_trace(self, output_path: Optional[str] = None) -> str:
        return physics_service.start_trace(self, output_path)

    def stop_trace(self) -> Optional[str]:
        return physics_service.stop_trace(self)

    def is_tracing(self) -> bool:
        return bool(self._trace and self._trace.active)

    # ---- 객체 생성/선택 (object_service) -------------------------------------

    def create_astronaut_prim(self, index: int) -> str:
        astronaut_usd = self._config.astronaut_usd if self._config else ""
        if not astronaut_usd:
            carb.log_error("[TimeTravel] astronaut_usd not specified in config")
            return ""
        return self._stage_objects.create_astronaut_prim(index, astronaut_usd)

    def set_active_objects(self, objids) -> int:
        return object_service.set_active_objects(self, objids)

    def add_synthetic_objects(self, count: int) -> Dict[str, str]:
        return object_service.add_synthetic_objects(self, count)

    def auto_generate_astronauts(self) -> Dict[str, str]:
        return object_service.auto_generate_astronauts(self)

    def regenerate_astronauts_from_loaded_data(self) -> Dict[str, str]:
        return object_service.regenerate_astronauts_from_loaded_data(self)

    def clear_timetravel_objects(self):
        object_service.clear_timetravel_objects(self)

    def hide_all_cameras(self):
        self._stage_objects.hide_all_cameras()

    # ---- 데이터/이벤트 조회 ---------------------------------------------------

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

    # ---- lookup / 벤치마크 (benchmark_service) --------------------------------

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
        return benchmark_service.start_lookup_benchmark(self, mode, pattern)

    def stop_lookup_benchmark(self) -> dict:
        return benchmark_service.stop_lookup_benchmark(self)

    def run_lookup_benchmark_suite(self, duration_s: float = 5.0, fps: int = 60) -> list:
        return benchmark_service.run_lookup_benchmark_suite(self, duration_s, fps)

    # ---- 표시 복잡도 / 이벤트 카메라 ------------------------------------------

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
