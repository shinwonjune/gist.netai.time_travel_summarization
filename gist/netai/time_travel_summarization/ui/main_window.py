# window.py - UI for Time Travel Extension

import datetime
import time

import carb

from .task_dispatcher import UiTaskDispatcher


class TimeTravelWindow:
    """Time Travel UI Window."""
    
    def __init__(self, core):
        """Initialize the Time Travel window."""
        import omni.ui as ui

        self._core = core
        self._updating_slider = False  # Flag to prevent infinite loops
        self._source_status_message = ""
        self._source_switch_pending = False
        self._ui_dispatcher = UiTaskDispatcher("TimeTravelWindowUiDispatcher")
        # Probe 상태 라벨 갱신 스로틀(매 프레임 갱신하면 계측 대상 자체를 흔든다)
        self._probe_status_next = 0.0
        self._probe_message = ""
        self._probe_message_until = 0.0

        # Create window
        from .workspace import close_existing_window
        close_existing_window("Time Travel")  # 핫리로드 유령 창 방지
        self._window = ui.Window("Time Travel", width=500, height=510)
        
        with self._window.frame:
            with ui.VStack(spacing=5):
                # Title
                with ui.HStack(height=30):
                    ui.Label("Time Travel Control", style={"font_size": 18, "font_weight": "bold"})
                
                with ui.HStack(height=30, spacing=8):
                    ui.Label("Data Source:", width=85)
                    self._source_local_button = ui.Button("Local", width=90)
                    self._source_local_button.set_clicked_fn(self._on_source_local_clicked)
                    self._source_lake_button = ui.Button("Data Lake", width=90)
                    self._source_lake_button.set_clicked_fn(self._on_source_lake_clicked)
                    self._source_status = ui.Label("", style={"color": 0xFF888888})

                ui.Label("Load Time Range:", style={"font_size": 14, "font_weight": "bold"})
                with ui.HStack(height=55, spacing=8):
                    with ui.VStack(spacing=3):
                        with ui.HStack(height=25):
                            ui.Label("Start:", width=40)
                            self._range_start_year = ui.IntField(width=50)
                            ui.Label("/", width=8)
                            self._range_start_month = ui.IntField(width=34)
                            ui.Label("/", width=8)
                            self._range_start_day = ui.IntField(width=34)
                            ui.Spacer(width=10)
                            self._range_start_hour = ui.IntField(width=34)
                            ui.Label(":", width=8)
                            self._range_start_minute = ui.IntField(width=34)
                            ui.Label(":", width=8)
                            self._range_start_second = ui.IntField(width=34)
                        with ui.HStack(height=25):
                            ui.Label("End:", width=40)
                            self._range_end_year = ui.IntField(width=50)
                            ui.Label("/", width=8)
                            self._range_end_month = ui.IntField(width=34)
                            ui.Label("/", width=8)
                            self._range_end_day = ui.IntField(width=34)
                            ui.Spacer(width=10)
                            self._range_end_hour = ui.IntField(width=34)
                            ui.Label(":", width=8)
                            self._range_end_minute = ui.IntField(width=34)
                            ui.Label(":", width=8)
                            self._range_end_second = ui.IntField(width=34)
                    ui.Spacer(width=10) # 1) 간격을 줄이기 위해 Spacer를 고정 크기로 둡니다.
                    self._load_range_button = ui.Button("Load Range", width=90)
                    self._load_range_button.set_clicked_fn(self._on_load_range_clicked)
                    ui.Spacer() # 2) 버튼 우측에 동적 Spacer를 두어 여백을 오른쪽으로 밀어냅니다.
                self._set_range_fields(self._core.get_start_time(), self._core.get_end_time())

                ui.Spacer(height=5)
                with ui.HStack(height=2):
                    ui.Line(style={"color": 0xFF666666})
                
                # Go to time controls
                ui.Label("Go to Time:", style={"font_size": 14, "font_weight": "bold"})
                with ui.HStack(height=25):
                    # Date inputs
                    self._goto_year = ui.IntField(width=50)
                    self._goto_year.model.set_value(self._core.get_current_time().year)
                    ui.Label("/", width=10)
                    self._goto_month = ui.IntField(width=35)
                    self._goto_month.model.set_value(self._core.get_current_time().month)
                    ui.Label("/", width=10)
                    self._goto_day = ui.IntField(width=35)
                    self._goto_day.model.set_value(self._core.get_current_time().day)
                    
                    ui.Spacer(width=20)
                    
                    # Time inputs
                    self._goto_hour = ui.IntField(width=35)
                    self._goto_hour.model.set_value(self._core.get_current_time().hour)
                    ui.Label(":", width=10)
                    self._goto_minute = ui.IntField(width=35)
                    self._goto_minute.model.set_value(self._core.get_current_time().minute)
                    ui.Label(":", width=10)
                    self._goto_second = ui.IntField(width=35)
                    self._goto_second.model.set_value(self._core.get_current_time().second)
                    
                    ui.Spacer(width=10)
                    self._goto_button = ui.Button("Go", width=50)
                    self._goto_button.set_clicked_fn(self._on_goto_clicked)

                # Event Summary checkbox with Next Event button
                with ui.HStack(height=25, spacing=10):
                    self._event_checkbox = ui.CheckBox(width=20)
                    self._event_checkbox.model.set_value(False)
                    self._event_checkbox.model.add_value_changed_fn(self._on_event_checkbox_changed)
                    
                    if self._core.has_events():
                        self._event_label = ui.Label(f"Event based Summary Mode ({len(self._core.get_summary_events())} events)", width=0)
                    else:
                        self._event_label = ui.Label("Event based Summary (Check to load events)", width=0, style={"color": 0xFF888888})
                    
                    self._next_event_button = ui.Button("Next Event", width=100)
                    self._next_event_button.set_clicked_fn(self._on_next_event_clicked)
                    self._next_event_button.enabled = False  # Initially disabled
                
                # Separator
                ui.Spacer(height=5)
                with ui.HStack(height=2):
                    ui.Line(style={"color": 0xFF666666})
                
                # twin time = 디지털 트윈 세계의 현재 시각(재연=데이터 시각,
                # physics=t0+경과). USD 타임라인의 'stage time'(초)과 구분하는 용어.
                with ui.HStack(height=25):
                    ui.Label("Twin Time:", width=80, style={"font_size": 14, "font_weight": "bold"})
                    self._stage_time_label = ui.Label("", style={"font_size": 20, "color": 0xFFFFFFFF})
                
                # Playback controls
                with ui.HStack(height=30):
                    self._play_button = ui.Button("▶ Play", width=80)
                    self._play_button.set_clicked_fn(self._on_play_clicked)
                    
                    ui.Spacer(width=20)
                    
                    ui.Label("Speed:", width=50)
                    self._speed_field = ui.FloatField(width=60)
                    self._speed_field.model.set_value(self._core.get_playback_speed())
                    self._speed_field.model.add_end_edit_fn(self._on_speed_changed)
                    ui.Label("x", width=20)

                    # Capture controls
                    ui.Spacer(width=10)
                    self._capture_button = ui.Button("Capture", width=90)
                    self._capture_button.set_clicked_fn(self._on_capture_clicked)

                    ui.Label("Length(s):", width=70)
                    self._capture_length_field = ui.FloatField(width=60)
                    self._capture_length_field.model.set_value(0.0)

                with ui.HStack(height=30, spacing=8):
                    ui.Label("Mode:", width=50)
                    self._playback_mode_button = ui.Button("Playback", width=90)
                    self._playback_mode_button.set_clicked_fn(self._on_playback_mode_clicked)
                    self._physics_mode_button = ui.Button("Physics", width=90)
                    self._physics_mode_button.set_clicked_fn(self._on_physics_mode_clicked)
                    self._mode_label = ui.Label("", style={"color": 0xFF888888})

                with ui.HStack(height=25, spacing=8):
                    ui.Label("Move Speed:", width=85)
                    self._wander_speed_field = ui.FloatField(width=70)
                    self._wander_speed_field.model.set_value(self._core.get_wander_speed())
                    self._wander_speed_field.model.add_end_edit_fn(self._on_wander_speed_changed)
                    ui.Label("units/s", width=55)
                    # near-miss 안무(GUI 육안 확인용): gap cm, 0이면 기존 wander.
                    # 컨트롤러 생성 시점에만 반영되므로 값 변경 후 Physics를 다시 눌러야 한다.
                    # 방식(swerve 기본|stop)은 여기 콤보로 노출하지 않는다 — facade
                    # 기본값(swerve)을 그대로 쓰며, stop을 보려면 코드/env로 지정
                    # (_on_near_miss_gap_changed 독스트링 참조).
                    ui.Label("Near-miss gap:", width=95)
                    self._near_miss_field = ui.FloatField(width=60)
                    self._near_miss_field.model.set_value(0.0)
                    self._near_miss_field.model.add_end_edit_fn(self._on_near_miss_gap_changed)
                    ui.Label("cm (0=off, re-click Physics; mode=swerve default)", width=250,
                             style={"color": 0xFF888888})
                    ui.Spacer()

                with ui.HStack(height=28, spacing=8):
                    self._move_button = ui.Button("Move", width=0)
                    self._move_button.set_clicked_fn(self._on_move_toggle)
                    self._trace_button = ui.Button("Trace", width=0)
                    self._trace_button.set_clicked_fn(self._on_trace_toggle)
                    self._trace_status = ui.Label("Idle", style={"color": 0xFF888888})
                
                # Separator
                ui.Spacer(height=5)
                with ui.HStack(height=2):
                    ui.Line(style={"color": 0xFF666666})
                
                # Time slider
                ui.Label("Timeline Slider:", style={"font_size": 16, "font_weight": "bold"})
                with ui.HStack(height=30):
                    ui.Spacer(width=10)
                    self._time_slider = ui.FloatSlider(min=0.0, max=1.0)
                    self._time_slider.model.set_value(0.0)
                    self._time_slider.model.add_value_changed_fn(self._on_slider_changed)
                    ui.Spacer(width=10)
                
                # Progress percentage
                with ui.HStack(height=20):
                    ui.Spacer()
                    self._progress_label = ui.Label("0.0%", style={"font_size": 12})
                    ui.Spacer()

                # 레이크 성능 계측(레이크성능_실험설계 §2-C). 평소 창을 짧게 두려고
                # 기본 접힘 — 측정할 때만 펼친다.
                # height=0 = 내용 크기에 맞춤(Kit 관용) — 접힌 상태가 남은 공간을
                # 다 먹지 않게 한다.
                self._probe_frame = ui.CollapsableFrame("Probe", collapsed=True, height=0)
                with self._probe_frame:
                    with ui.VStack(spacing=4, height=0):
                        with ui.HStack(height=25, spacing=8):
                            self._probe_checkbox = ui.CheckBox(width=20)
                            self._probe_checkbox.model.set_value(self._get_probe() is not None)
                            self._probe_checkbox.model.add_value_changed_fn(
                                self._on_probe_enable_changed)
                            ui.Label("Record playback timing", width=160)
                            ui.Spacer()
                        with ui.HStack(height=25, spacing=8):
                            ui.Label("Scenario:", width=70)
                            self._probe_scenario_field = ui.StringField(width=110)
                            self._probe_scenario_field.model.set_value("")
                            self._probe_scenario_field.model.add_end_edit_fn(
                                self._on_probe_scenario_changed)
                            self._probe_start_button = ui.Button("Start", width=60)
                            self._probe_start_button.set_clicked_fn(self._on_probe_start_clicked)
                            self._probe_dump_button = ui.Button("Dump", width=60)
                            self._probe_dump_button.set_clicked_fn(self._on_probe_dump_clicked)
                            ui.Label("1x / 5x / -1x / scrub", width=0,
                                     style={"color": 0xFF888888})
                            ui.Spacer()
                        self._probe_status = ui.Label("probe: off",
                                                      style={"color": 0xFF888888})

                self._update_source_controls()
    
    def _on_goto_clicked(self):
        """Handle Go button click - go to user-specified time."""
        try:
            goto_time = datetime.datetime(
                self._goto_year.model.get_value_as_int(),
                self._goto_month.model.get_value_as_int(),
                self._goto_day.model.get_value_as_int(),
                self._goto_hour.model.get_value_as_int(),
                self._goto_minute.model.get_value_as_int(),
                self._goto_second.model.get_value_as_int()
            )
            
            # Always go to the specified time
            self._core.set_current_time(goto_time)
            
            # Update slider
            self._time_slider.model.set_value(self._core.get_progress())
            
        except Exception as e:
            carb.log_error(f"[TimeTravel] Error setting time: {e}")
    
    def _on_load_range_clicked(self):
        """Handle Load Range click - narrow playback to [start,end] and seek to start.

        Lake 모드에서는 해당 구간 청크만 minIO에서 윈도우로 로드된다(프리페치로 무지연).
        """
        try:
            start = self._get_range_time("start")
            end = self._get_range_time("end")
        except ValueError as e:
            carb.log_error(f"[TimeTravel] Invalid range: {e}")
            return
        if self._core.load_time_range(start, end):
            self._time_slider.model.set_value(self._core.get_progress())
            self._update_goto_fields()

    def _on_source_local_clicked(self):
        """Switch trajectory loading to the configured local data file."""
        self._request_source_switch("local")

    def _on_source_lake_clicked(self):
        """Switch trajectory loading to the configured Data Lake manifest."""
        carb.log_warn("[TimeTravel] Data Lake button clicked")
        self._request_source_switch("lake")

    def _request_source_switch(self, mode: str):
        if self._source_switch_pending:
            carb.log_warn(f"[TimeTravel] Source switch already pending; ignored mode={mode}")
            return
        self._source_switch_pending = True
        label = "Data Lake" if mode == "lake" else "Local"
        self._source_status_message = f"{label} loading..."
        self._update_source_controls()
        self._ui_dispatcher.submit(lambda mode=mode: self._apply_source_switch(mode))

    def _apply_source_switch(self, mode: str):
        try:
            carb.log_warn(f"[TimeTravel] Applying source switch on update tick: {mode}")
            if mode == "lake":
                self._apply_lake_source_switch()
            else:
                self._apply_local_source_switch()
        except Exception as exc:
            carb.log_error(f"[TimeTravel] Source switch crashed before completion: {exc!r}")
            import traceback

            carb.log_error(traceback.format_exc())
            self._source_status_message = f"Source switch failed: {exc}"
            self._update_source_controls()
        finally:
            self._source_switch_pending = False
            self._update_source_controls()

    def _apply_local_source_switch(self):
        if self._core.set_data_source("local"):
            self._source_status_message = ""
            self._refresh_after_source_switch()
            return
        self._source_status_message = self._core.get_last_data_load_error() or "Local load failed"
        carb.log_warn(f"[TimeTravel] {self._source_status_message}")

    def _apply_lake_source_switch(self):
        if self._core.set_data_source("lake"):
            objects = self._core.get_loaded_object_count() if hasattr(self._core, "get_loaded_object_count") else 0
            astronauts = self._core.get_stage_object_count() if hasattr(self._core, "get_stage_object_count") else 0
            self._source_status_message = f"Data Lake loaded: objects={objects}, astronauts={astronauts}"
            carb.log_warn(f"[TimeTravel] {self._source_status_message}")
            self._refresh_after_source_switch()
            return

        self._source_status_message = "Data Lake not configured (config lake.manifest_uri required)"
        if hasattr(self._core, "get_last_data_load_error"):
            self._source_status_message = self._core.get_last_data_load_error() or self._source_status_message
        carb.log_warn(f"[TimeTravel] {self._source_status_message}")
        self._update_source_controls()

    def _on_next_event_clicked(self):
        """Handle Next Event button click - jump to next event."""
        if self._core.has_events():
            self._core.go_to_next_event()
            
            # Update slider
            self._time_slider.model.set_value(self._core.get_progress())
            
            # Update goto fields to reflect new time
            self._update_goto_fields()
    
    def _on_play_clicked(self):
        """Handle Play/Pause button click."""
        self._core.toggle_playback()
        self._update_play_button()

    def _on_playback_mode_clicked(self):
        """Switch back to trajectory playback mode."""
        self._core.set_playback_mode()
        self._update_mode_controls()
        self._update_play_button()

    def _on_physics_mode_clicked(self):
        """Switch to PhysX wandering mode."""
        self._core.set_physics_mode()
        if self._core.is_wandering():
            self._core.stop_wander()
        self._update_mode_controls()
        self._update_move_trace_controls()
        self._update_play_button()

    def _on_move_toggle(self):
        if self._core.is_wandering():
            self._core.stop_wander()
            self._move_button.text = "Move"
        else:
            if self._core.start_wander():
                self._move_button.text = "Stop Move"

    def _on_capture_clicked(self):
        if self._core.is_capturing():
            self._core.stop_capture()
            self._capture_button.text = "Capture"
        else:
            duration_s = self._capture_length_field.model.get_value_as_float()
            if duration_s < 0:
                duration_s = 0.0
            self._core.start_capture(duration_s=duration_s)
            self._capture_button.text = "Stop"

    def _on_trace_toggle(self):
        if self._core.is_tracing():
            out = self._core.stop_trace()
            self._trace_button.text = "Trace"
            self._trace_status.text = f"Saved: {out}"
        else:
            self._core.start_trace()
            self._trace_button.text = "Stop Trace"
            self._trace_status.text = "Recording..."
    
    def _on_slider_changed(self, model):
        """Handle slider value change."""
        # Prevent infinite loop when updating slider programmatically
        if self._updating_slider:
            return
            
        progress = model.get_value_as_float()
        self._core.set_progress(progress)
        self._update_goto_fields()
    
    def _on_speed_changed(self, model):
        """Handle speed value change."""
        speed = model.get_value_as_float()
        self._core.set_playback_speed(speed)

    def _on_wander_speed_changed(self, model):
        """Handle physics wander speed value change."""
        speed = model.get_value_as_float()
        if not self._core.set_wander_speed(speed):
            model.set_value(self._core.get_wander_speed())
    
    def _on_near_miss_gap_changed(self, model):
        """near-miss 안무 간격(cm; 0=끔). 다음 Physics 클릭부터 적용.

        방식(swerve|stop)은 GUI에 별도 토글이 없다 — facade 기본값 "swerve"(감속
        없이 스침) 그대로 쓰인다. GUI로 stop(v1, 감속+정지+방향전환 대조군)을 보고
        싶으면 코드에서 ``core.set_near_miss_mode("stop")``을 gap 설정 전에 호출하거나,
        headless 배치는 ``NEAR_MISS_MODE=stop`` 환경변수(run_job.sh)를 쓴다 — GUI
        토글은 간단하지 않아(콤보 하나 추가+상태 배선) 스코프에서 제외했다.

        회피 곡선의 완만함(``TTS_NEAR_MISS_AVOID_FRAC``·``..._TURN_RADIUS_FRAC``·
        ``..._AIM_FRAC``)과 조우 지점의 다양성(``TTS_NEAR_MISS_START_JITTER_S``·
        ``..._SPEED_MIN_FRAC``·``..._SPEED_MAX_FRAC``·``..._DEPART_SPREAD_DEG``)은
        전부 환경변수로 조정한다 — Kit을 그 환경변수와 함께 띄우고 Physics를 다시
        누르면 반영되므로, GUI에서 눈으로 보며 되풀이 튜닝할 때 코드 수정·재빌드가
        필요 없다. 각 값의 의미는 facade의 set_near_miss_steering /
        set_near_miss_diversity 독스트링에 있다."""
        self._core.set_near_miss_gap(model.get_value_as_float())

    # ---- Probe(레이크 성능 계측) ------------------------------------------

    def _get_probe(self):
        """활성 계측 인스턴스 또는 None. 구버전 core에도 안전하도록 getattr."""
        getter = getattr(self._core, "get_lake_probe", None)
        return getter() if callable(getter) else None

    def _set_probe_message(self, text: str, seconds: float = 5.0):
        """일회성 안내(구간 시작·덤프 파일명)를 잠깐 띄운 뒤 라이브 통계로 복귀."""
        self._probe_message = text
        self._probe_message_until = time.monotonic() + seconds
        self._probe_status_next = 0.0  # 다음 update_ui에서 즉시 반영

    def _on_probe_enable_changed(self, model):
        """체크박스 — facade가 계측 인스턴스를 생성/파괴한다(끌 때 잔여 버퍼 덤프)."""
        enabled = model.get_value_as_bool()
        setter = getattr(self._core, "set_lake_probe_enabled", None)
        if not callable(setter):
            self._set_probe_message("probe: not supported by this build")
            return
        probe = setter(enabled)
        if probe is not None:
            probe.set_scenario(self._probe_scenario_field.model.get_value_as_string())
            self._set_probe_message("probe: on")
        else:
            self._set_probe_message("probe: off (buffer dumped)")

    def _on_probe_scenario_changed(self, model):
        """시나리오 라벨 — 덤프 JSON과 파일명에 들어간다(사후 조건 매칭용)."""
        probe = self._get_probe()
        if probe is None:
            return
        self._set_probe_message(f"probe: scenario {probe.set_scenario(model.get_value_as_string()) or '(none)'}")

    def _on_probe_start_clicked(self):
        """Start — 파일을 쓰지 않고 버퍼만 비워 측정 구간의 시작을 찍는다."""
        probe = self._get_probe()
        if probe is None:
            self._set_probe_message("probe: off - check the box first")
            return
        probe.set_scenario(self._probe_scenario_field.model.get_value_as_string())
        dropped = probe.reset()
        self._set_probe_message(f"probe: segment started (dropped {dropped} frames)")

    def _on_probe_dump_clicked(self):
        """Dump — 지금까지의 버퍼를 파일로 저장(스크럽 구간의 유일한 종료 경계).

        버퍼에 재생 프레임도 탐색 프레임도 없으면(정지한 채 시간축도 건드리지 않은
        idle 상태) 파일을 만들지 않는다. 이때는 "버퍼가 비었다"와 구분되는 안내를
        띄운다 — 프레임은 쌓였지만 성능 판정에 쓸 것이 없다는 뜻이기 때문이다.
        """
        probe = self._get_probe()
        if probe is None:
            self._set_probe_message("probe: off - nothing to dump")
            return
        n_before = len(probe)
        path = probe.dump(reason="manual")
        if path is not None:
            msg = f"probe: wrote {path.name}"
        elif n_before:
            msg = f"probe: idle-only buffer ({n_before} frames), nothing written"
        else:
            msg = "probe: buffer empty, nothing written"
        self._set_probe_message(msg, seconds=8.0)

    def _update_probe_status(self):
        """~2Hz만 갱신 — 매 프레임 문자열을 만들면 측정 대상 프레임에 부하가 섞인다."""
        now = time.monotonic()
        if now < self._probe_status_next:
            return
        self._probe_status_next = now + 0.5
        if self._probe_message:
            if now < self._probe_message_until:
                self._probe_status.text = self._probe_message
                return
            self._probe_message = ""
        probe = self._get_probe()
        if probe is None:
            self._probe_status.text = "probe: off"
            return
        s = probe.live_stats()
        label = f" [{s['scenario']}]" if s["scenario"] else ""
        self._probe_status.text = (
            f"probe{label}: {s['frames']} frames | stalls {s['stalls']} | {s['fps']:.0f} fps"
        )

    def _on_event_checkbox_changed(self, model):
        """Handle event summary checkbox change."""
        requested_value = model.get_value_as_bool()
        
        if requested_value:
            # User wants to enable event mode - check if events exist
            if not self._core.has_events():
                # Try to load events from Events directory
                if self._core.load_events_from_positions_jsonl():
                    # Successfully loaded events
                    self._core.set_use_event_summary(True)
                    carb.log_info("[TimeTravel] Event based Summary Mode enabled")
                    # Update label to show event count
                    self._update_event_label()
                    # Enable Next Event button
                    self._next_event_button.enabled = True
                else:
                    # No events found - revert checkbox
                    model.set_value(False)
                    carb.log_warn("[TimeTravel] No events available - Event based Summary Mode disabled")
                    self._next_event_button.enabled = False
            else:
                # Events already exist
                self._core.set_use_event_summary(True)
                carb.log_info("[TimeTravel] Event based Summary Mode enabled")
                self._next_event_button.enabled = True
        else:
            # User wants to disable event mode
            self._core.set_use_event_summary(False)
            self._next_event_button.enabled = False
            carb.log_info("[TimeTravel] Event based Summary Mode disabled")
    
    def _update_event_label(self):
        """Update event label with current event count."""
        if self._core.has_events():
            event_count = len(self._core.get_summary_events())
            self._event_label.text = f"Event based Summary Mode ({event_count} events)"
            self._event_label.style = {"color": 0xFFFFFFFF}
        else:
            self._event_label.text = "Event based Summary (Check to load events)"
            self._event_label.style = {"color": 0xFF888888}
    
    def _update_play_button(self):
        """Update play button text."""
        if self._core.is_playing():
            self._play_button.text = "Pause"
        else:
            self._play_button.text = "Play"

    def _update_mode_controls(self):
        """Update current mode label and button enabled states."""
        mode = self._core.get_mode()
        self._mode_label.text = mode.capitalize()
        self._playback_mode_button.enabled = mode != "playback"
        self._physics_mode_button.enabled = mode != "physics"

    def _update_source_controls(self):
        """Update current data source label and button enabled states."""
        if not hasattr(self, "_source_local_button"):
            return
        source = self._core.get_data_source()
        pending = getattr(self, "_source_switch_pending", False)
        self._source_local_button.enabled = not pending and source != "local"
        self._source_lake_button.enabled = not pending and source != "lake"
        if self._source_status_message:
            self._source_status.text = self._source_status_message
        else:
            self._source_status.text = "Data Lake" if source == "lake" else "Local"

    def _update_move_trace_controls(self):
        """Update Move and Trace controls from facade state."""
        self._move_button.text = "Stop Move" if self._core.is_wandering() else "Move"
        if self._core.is_tracing():
            self._trace_button.text = "Stop Trace"
            self._trace_status.text = "Recording..."
        else:
            self._trace_button.text = "Trace"
    
    def _update_goto_fields(self):
        """Update goto time fields with current time."""
        current = self._core.get_current_time()
        self._goto_year.model.set_value(current.year)
        self._goto_month.model.set_value(current.month)
        self._goto_day.model.set_value(current.day)
        self._goto_hour.model.set_value(current.hour)
        self._goto_minute.model.set_value(current.minute)
        self._goto_second.model.set_value(current.second)

    def _get_range_time(self, prefix: str) -> datetime.datetime:
        return datetime.datetime(
            getattr(self, f"_range_{prefix}_year").model.get_value_as_int(),
            getattr(self, f"_range_{prefix}_month").model.get_value_as_int(),
            getattr(self, f"_range_{prefix}_day").model.get_value_as_int(),
            getattr(self, f"_range_{prefix}_hour").model.get_value_as_int(),
            getattr(self, f"_range_{prefix}_minute").model.get_value_as_int(),
            getattr(self, f"_range_{prefix}_second").model.get_value_as_int(),
        )

    def _set_range_time(self, prefix: str, value: datetime.datetime):
        getattr(self, f"_range_{prefix}_year").model.set_value(value.year)
        getattr(self, f"_range_{prefix}_month").model.set_value(value.month)
        getattr(self, f"_range_{prefix}_day").model.set_value(value.day)
        getattr(self, f"_range_{prefix}_hour").model.set_value(value.hour)
        getattr(self, f"_range_{prefix}_minute").model.set_value(value.minute)
        getattr(self, f"_range_{prefix}_second").model.set_value(value.second)

    def _set_range_fields(self, start_time: datetime.datetime, end_time: datetime.datetime):
        self._set_range_time("start", start_time)
        self._set_range_time("end", end_time)

    def _refresh_after_source_switch(self):
        """Refresh time controls after changing repository-backed data."""
        start_time = self._core.get_start_time()
        end_time = self._core.get_end_time()
        self._set_range_fields(start_time, end_time)
        self._time_slider.model.set_value(self._core.get_progress())
        self._update_goto_fields()
        self._update_source_controls()
    
    def update_ui(self):
        """Update UI elements (called every frame)."""
        # Update stage time display
        self._stage_time_label.text = self._core.get_twin_time_string()
        
        # Update slider if playing (but don't interfere with user dragging)
        if self._core.is_playing():
            progress = self._core.get_progress()
            self._updating_slider = True  # Prevent triggering _on_slider_changed
            self._time_slider.model.set_value(progress)
            self._updating_slider = False
            # self._update_goto_fields()
        
        # Update progress percentage
        progress_pct = self._core.get_progress() * 100
        self._progress_label.text = f"{progress_pct:.1f}%"
        
        # Update play button
        self._update_play_button()
        self._update_mode_controls()
        self._update_source_controls()
        self._update_move_trace_controls()
        self._update_probe_status()
        # sync capture button label with facade state
        if hasattr(self, "_capture_button"):
            expected = "Stop" if self._core.is_capturing() else "Capture"
            if self._capture_button.text != expected:
                self._capture_button.text = expected
    
    def destroy(self):
        """Clean up the window."""
        if hasattr(self, "_ui_dispatcher") and self._ui_dispatcher:
            self._ui_dispatcher.shutdown()
            self._ui_dispatcher = None
        if self._window:
            self._window.destroy()
            self._window = None
