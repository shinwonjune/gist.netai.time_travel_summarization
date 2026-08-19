"""TimeTravelCore — 확장의 단일 진입 facade.

상태(레포지토리·prim_map·캡처/물리 플래그)는 전부 이 클래스가 소유하고,
동작은 도메인 서비스 모듈(capture/physics/data/object/benchmark_service)에
위임한다. 서비스가 상태를 들지 않는 이유: 테스트가 __new__ + 속성 주입으로
core를 구성하고, extension.py가 shutdown 시 _wander를 직접 만지기 때문.
공개 API는 분해 전과 동일하다.
"""
import datetime
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

import carb

from . import benchmark_service, capture_service, data_service, object_service, physics_service
from .paths import ExtensionPaths
from ..events.summary_service import EventSummaryService
from ..playback.controller import PlaybackController
from ..playback.stage_object_controller import StageObjectController
from ..playback.trajectory_repository import TrajectoryRepository
from ..playback.visibility import DEFAULT_VISIBILITY_TOL_S, compute_object_visibility

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
        # near-miss 안무 간격(cm; 0=비활성) — set_physics_mode가 WanderController에 전달.
        self._near_miss_gap = 0.0
        # near-miss 안무 방식(swerve|stop, 기본 swerve) — GUI 육안 검수에서 stop(v1,
        # 감속+정지+방향전환)이 "보이지 않는 벽" 인상으로 기각돼 swerve가 기본이다.
        # stop은 감속 단서 vs 근접 단서를 분리해 보는 대조군으로 옵션으로 남겨둔다.
        self._near_miss_mode = "swerve"
        # near-miss swerve(v3) 조향 파라미터 — None이면 WanderController가 env
        # (TTS_NEAR_MISS_AVOID_FRAC 등) → 코드 기본값 순으로 해결한다. GUI는 env로,
        # 헤드리스 배치는 generate_episodes의 CLI 인자로 조정한다.
        self._near_miss_avoid_frac = None
        self._near_miss_turn_radius_frac = None
        self._near_miss_aim_frac = None
        # near-miss v4 대칭 파괴 파라미터(조우 지점이 방 중앙에서 반복되던 문제) —
        # 위와 같은 None 규약(env → 코드 기본값). set_near_miss_diversity 참조.
        self._near_miss_start_jitter_s = None
        self._near_miss_speed_min_frac = None
        self._near_miss_speed_max_frac = None
        self._near_miss_depart_spread_deg = None
        # 캡처 완료 알림(성공 시 output URI 전달) — extension.py가 VLM 창을 배선.
        self._capture_complete_cb = None
        # 재생 공백 점프: 데이터 공백이 이 값(초)을 넘으면 시계를 다음 데이터로
        # 순간이동(시계가 주인인 재생 구조에서, 공백 23h를 실시간으로 기지 않게).
        self._gap_skip_s = 10.0
        # GUI E2E 재생 계측(레이크성능_실험설계 §2-C) — env 미설정 시 None(무부하).
        # env는 초기 기본값일 뿐이고, GUI Probe 섹션이 런타임에 켜고 끌 수 있다.
        self._lake_probe = None
        if os.environ.get("TTS_LAKE_PROBE", "0") == "1":
            self.set_lake_probe_enabled(True)

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
        self._stage_objects.update_stage_objects(
            self._prim_map,
            self.get_data_at_time(current_time),
            self._object_visibility_at(current_time),
        )

    def _object_visibility_at(self, current_time: datetime.datetime) -> Optional[Dict[str, bool]]:
        """current_time에서 objid별 보임/숨김. 트랙 범위 정보가 없으면(physics/합성
        객체 등) None — 이 경우 visibility 토글 없이 위치만 갱신한다.

        env ``TTS_DESPAWN_GAP_S``가 설정돼 있으면 **결손 인지 despawn**을 켠다:
        트랙 범위 안이라도 마지막 표본 이후 그 초를 넘게 비면 숨긴다. 기본은 비활성
        (미설정) — 표본이 드문 조건(다운샘플)에서 정상 객체가 깜빡이기 때문이며,
        frag-sameid 계열 렌더에서만 조건별로 켠다(v3 계획서 §4-5).
        """
        ranges_fn = getattr(self._repository, "get_object_time_ranges", None)
        if not callable(ranges_fn):
            return None
        ranges = ranges_fn()
        if not ranges:
            return None
        gap_s = None
        raw = os.environ.get("TTS_DESPAWN_GAP_S", "").strip()
        if raw:
            try:
                gap_s = float(raw)
            except ValueError:
                gap_s = None
        last_samples = None
        if gap_s is not None:
            fn = getattr(self._repository, "get_object_last_sample", None)
            if callable(fn):
                last_samples = fn(current_time)
            else:
                gap_s = None            # 지원 안 하는 레포지토리면 종전 동작 유지
        return compute_object_visibility(current_time, ranges, DEFAULT_VISIBILITY_TOL_S,
                                         last_samples, gap_s)

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

    # ---- 레이크 성능 계측(lake probe) ---------------------------------------

    def set_lake_probe_enabled(self, enabled: bool):
        """계측 인스턴스를 런타임에 만들거나 없앤다. 활성 시 probe, 아니면 None 반환.

        끌 때는 남은 버퍼를 먼저 덤프해 측정분을 잃지 않는다. None인 동안
        update()는 기존과 완전히 같은 무부하 경로를 탄다.
        """
        if enabled:
            if getattr(self, "_lake_probe", None) is None:
                from .lake_probe import LakeProbe
                self._lake_probe = LakeProbe()
                carb.log_warn("[TimeTravel] lake probe enabled")
        else:
            probe = getattr(self, "_lake_probe", None)
            self._lake_probe = None
            if probe is not None:
                probe.dump(reason="disable")
                carb.log_warn("[TimeTravel] lake probe disabled")
        return self._lake_probe

    def get_lake_probe(self):
        """활성 계측 인스턴스(없으면 None). UI가 reset/dump/통계를 직접 호출한다."""
        return getattr(self, "_lake_probe", None)

    def update(self, dt: float):
        probe = getattr(self, "_lake_probe", None)  # __new__ 주입 테스트 대비 getattr
        if probe is None:
            self._playback.update(dt, self._parse_timestamp, self._on_playback_tick, self._on_event_requested)
        else:
            t0 = time.perf_counter()
            self._playback.update(dt, self._parse_timestamp, self._on_playback_tick, self._on_event_requested)
            probe.record(
                tick_ms=(time.perf_counter() - t0) * 1000,
                twin_time=self._playback.get_current_time(),
                stats=getattr(self._repository, "stats", None),
                is_playing=self._playback.is_playing(),
            )
        if self._trace and self._trace.active:
            try:
                # 충돌 CSV·오버레이와 동일 시계 — headless 캡처 중엔 sim 클럭.
                # 인자 없이 부르면 wall clock이 찍혀, 렌더가 sim보다 느린 만큼
                # trace가 늘어진다(30s 에피소드가 ~99s 스팬 → 재연 슬로모션).
                now = self.get_sim_clock_datetime() if self._use_sim_clock else None
                self._trace.tick(now)
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
                             render_fps: Optional[int] = None,
                             replay_start_dt: Optional[datetime.datetime] = None) -> Optional[str]:
        return capture_service.run_capture_headless(
            self, duration_s, output_path,
            camera_path=camera_path,
            capture_start_dt=capture_start_dt,
            render_fps=render_fps,
            replay_start_dt=replay_start_dt,
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

    def get_twin_time_string(self) -> str:
        """Twin Time 창 라벨 전용 — 날짜 포함(YYYY-MM-DD HH:MM:SS).

        오버레이/CSV용 get_stage_time_string(HH:MM:SS, 추론↔라벨 정합)과 달리,
        멀티데이 재생에서 날짜 구분을 보이려고 날짜를 앞에 붙인다. 시각 부분은
        format_event_time을 재사용해 PRECISION을 공유한다.
        """
        from ..timefmt import format_event_time

        if self._playback.get_mode() == "physics":
            dt = self.get_sim_clock_datetime() if self._use_sim_clock else datetime.datetime.now()
        else:
            dt = self._playback.get_current_time()
        if not dt:
            return "No time set"
        return f"{dt.strftime('%Y-%m-%d')} {format_event_time(dt)}"

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

    def set_coord_range_override(self, mins, maxs) -> None:
        """배회 아레나 범위를 명시 지정(씬 프로파일 경로). set_physics_mode 전에
        호출하면 궤적 데이터의 coord_range 대신 이 값으로 bounds를 계산한다 —
        생성이 데이터 로드 없이 돌 수 있게 하는 승격점(2026-08-15)."""
        self._coord_range_override = (
            tuple(float(v) for v in mins), tuple(float(v) for v in maxs))

    def set_near_miss_gap(self, gap: float) -> None:
        """near-miss 안무 활성화(gap>0, cm): 짝끼리 gap까지 접근했다 흩어진다(방식은
        set_near_miss_mode) — 접촉이 없어 GT 충돌 0건. set_physics_mode 전에 호출해야
        반영된다(그때 컨트롤러 생성)."""
        self._near_miss_gap = max(0.0, float(gap))

    def set_near_miss_mode(self, mode: str) -> None:
        """near-miss 안무 방식 선택: "swerve"(기본, 감속 없이 스침) 또는 "stop"(v1,
        감속+정지+방향전환 — 대조군 옵션). set_physics_mode 전에 호출해야 반영된다."""
        mode = str(mode).strip().lower()
        if mode not in ("swerve", "stop"):
            carb.log_warn(f"[TimeTravel] invalid near_miss_mode: {mode!r} -> swerve")
            mode = "swerve"
        self._near_miss_mode = mode

    def set_near_miss_steering(self, avoid_frac=None, turn_radius_frac=None, aim_frac=None) -> None:
        """near-miss swerve(v3)의 회피 곡선 모양을 정하는 세 값. 전부 gap 배수다.

        - ``avoid_frac``: 회피 개시 반경 = gap × 이 값. 크게 하면 더 멀리서부터 휘기
          시작한다(대신 너무 크면 미리 벌어져 근접 자체가 안 일어날 수 있다).
        - ``turn_radius_frac``: 최소 선회 반경 = gap × 이 값. **완만함을 좌우하는 값** —
          크게 할수록 큰 원을 그리듯 부드럽게 돌지만, 너무 크면 제때 못 피해 gap 불변식
          안전망(하드 캡)이 대신 개입하면서 오히려 급선회가 남는다.
        - ``aim_frac``: 목표 통과 간격 = gap × 이 값. gap과 같게 두면 여유가 없어 하드
          캡이 자주 개입하므로 1보다 조금 크게 잡는다.

        None으로 두면 WanderController가 환경변수(TTS_NEAR_MISS_AVOID_FRAC,
        TTS_NEAR_MISS_TURN_RADIUS_FRAC, TTS_NEAR_MISS_AIM_FRAC) → 코드 기본값 순으로
        해결한다. set_physics_mode 전에 호출해야 반영된다(그때 컨트롤러 생성).
        """
        self._near_miss_avoid_frac = avoid_frac
        self._near_miss_turn_radius_frac = turn_radius_frac
        self._near_miss_aim_frac = aim_frac

    def set_near_miss_diversity(self, start_jitter_s=None, speed_min_frac=None,
                                speed_max_frac=None, depart_spread_deg=None) -> None:
        """near-miss 조우의 **다양성**을 정하는 네 값(v4 대칭 파괴).

        v3까지는 짝이 동시에·같은 속도로·서로를 정면 조준해 접근했기 때문에 조우가
        두 스폰 위치의 중점 부근에서 거의 같은 기하로 반복됐다(스폰 구역이 방 중앙
        대칭이므로 결국 방 중앙). 조우 지점을 명시적으로 지정하는 대신 아래 세 축의
        대칭을 깨서 조우 지점이 저절로 흩어지게 한다.

        - ``start_jitter_s``: 접근 페이즈로 넘어간 뒤 객체마다 0~이 초 사이의 무작위
          지연이 지나야 짝을 조준한다. 늦게 도는 쪽이 그 사이 이동한 만큼 조우 지점이
          그쪽으로 끌려간다(기본 2.0초).
        - ``speed_min_frac``/``speed_max_frac``: 사이클마다 객체별 순항 속도를
          speed × [min, max]에서 독립 추출한다(기본 0.7~1.0). 빠른 쪽이 더 많이
          이동하므로 만나는 지점이 느린 쪽으로 치우친다. 상한은 1.0을 넘길 수 없다 —
          speed가 천장이라는 성질에 조향률 상한 계산이 기대고 있다.
        - ``depart_spread_deg``: 스침 뒤 이탈 방향을 "짝의 반대 방향 ±이 각도"에서
          무작위로 뽑는다(기본 90도). 다음 사이클의 시작 배치가 비대칭이 되어 앞의
          두 효과가 사이클마다 누적된다. 0 이하로 주면 이탈 재조준을 끄고 v3처럼
          스침 헤딩을 그대로 이어간다.

        None으로 두면 WanderController가 환경변수(TTS_NEAR_MISS_START_JITTER_S,
        TTS_NEAR_MISS_SPEED_MIN_FRAC, TTS_NEAR_MISS_SPEED_MAX_FRAC,
        TTS_NEAR_MISS_DEPART_SPREAD_DEG) → 코드 기본값 순으로 해결한다.
        set_physics_mode 전에 호출해야 반영된다(그때 컨트롤러 생성).

        어느 값을 어떻게 흔들어도 gap 불변식(=접촉 없음 = GT 충돌 0건)은 영향을 받지
        않는다. 그 보증은 "각 객체가 자기 반경 속도 성분을 (거리-gap)/(2·dt) 이하로
        묶는다"는 형태라, 두 객체의 속도가 서로 달라도 어느 쪽이 언제 출발했어도
        한 스텝의 접근량 합이 (거리-gap)을 넘지 못한다는 논증이 그대로 성립한다.
        """
        self._near_miss_start_jitter_s = start_jitter_s
        self._near_miss_speed_min_frac = speed_min_frac
        self._near_miss_speed_max_frac = speed_max_frac
        self._near_miss_depart_spread_deg = depart_spread_deg

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

    def spawn_objects(self, count: int) -> Dict[str, str]:
        return object_service.spawn_objects(self, count)

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

    def move_camera_to_event_at(self, t: datetime.datetime, ids) -> bool:
        """이벤트 시각의 관련 객체 위치로 summarization 카메라 이동.

        위치를 eventlist(_events) 경유가 아니라 궤적에서 직조회 — Event Search
        인덱스 결과(ids만 있음)로도 카메라가 따라가게 한다. ids는 라벨 번호
        (1→obj001 규약). 위치를 못 찾으면 False(카메라는 best-effort)."""
        data = self._repository.get_data_at_time(t)
        for n in ids or []:
            try:
                pos = data.get(f"obj{int(n):03d}")
            except (TypeError, ValueError):
                continue
            if pos:
                self._stage_objects.move_camera_to_event(pos)
                return True
        return False
