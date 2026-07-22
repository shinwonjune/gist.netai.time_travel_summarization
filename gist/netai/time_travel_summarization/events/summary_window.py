# summary_window.py - Event Summary Window
#
# Event Search(twin time 구간 조회)를 Event Post Processing 창에서 분리하고,
# 검색 결과 이벤트로 점프 → N초 재생 → 자동 일시정지(twin time 기준) 제어를 붙인다.
# omni.ui / carb / task_dispatcher는 메서드 안에서 지연 import → 이 모듈이 omni 없이
# import 가능(하단 순수 헬퍼를 유닛테스트가 직접 불러 검증).

import datetime
import threading
import time

_DT_FMT = "%Y-%m-%d %H:%M:%S"


# ---- 순수 헬퍼 (omni 불필요, 유닛테스트 대상) ------------------------------- #


class GenerationToken:
    """폴링 스레드 무효화용 세대 카운터.

    새 이벤트를 클릭하면 bump()로 세대를 올린다 → 이전 재생의 폴링 스레드는
    is_current()가 False가 되어 스스로 종료(정지 호출을 넘기지 않음)한다.
    """

    def __init__(self):
        self._value = 0

    def bump(self) -> int:
        self._value += 1
        return self._value

    @property
    def value(self) -> int:
        return self._value

    def is_current(self, token: int) -> bool:
        return token == self._value


def parse_event_time(ev: dict):
    """이벤트 레코드의 'time'을 datetime으로. 실패 시 None."""
    try:
        return datetime.datetime.fromisoformat(ev["time"])
    except (KeyError, TypeError, ValueError):
        return None


def sort_events_by_time(events: list) -> list:
    """이벤트를 시각 오름차순으로. 파싱 불가 레코드는 원래 순서를 지키며 뒤로."""
    def key(item):
        t = parse_event_time(item[1])
        return (t is None, t or datetime.datetime.max, item[0])

    return [ev for _, ev in sorted(enumerate(events), key=key)]


def event_time_span(events: list):
    """이벤트들의 (최소, 최대) 시각. 파싱 가능한 게 없으면 None."""
    times = [t for t in (parse_event_time(ev) for ev in events) if t is not None]
    if not times:
        return None
    return (min(times), max(times))


def playback_reached_end(current_time, event_time, play_length_s: float) -> bool:
    """twin time 기준 재생 종료 판정: 현재 시각이 이벤트+재생길이에 도달/초과."""
    return current_time >= event_time + datetime.timedelta(seconds=play_length_s)


class EventSummaryWindow:
    """Event Search + 이벤트 재생 제어 창."""

    def __init__(self, core, ext_id: str):
        from ..ui.task_dispatcher import UiTaskDispatcher

        self._core = core
        self._ext_id = ext_id
        self._window = None
        self._ui_dispatcher = UiTaskDispatcher("EventSummaryWindowUiDispatcher")

        self._search_results: list = []
        self._generation = GenerationToken()
        self._play_all_active = False

        # UI handles (build 시 채움)
        self._search_start = None
        self._search_end = None
        self._search_button = None
        self._results_stack = None
        self._status_label = None
        self._play_length_model = None
        self._play_all_button = None

        self._build_ui()

    def _build_ui(self):
        import omni.ui as ui

        from ..ui.workspace import close_existing_window
        close_existing_window("Event Summary")  # 핫리로드 유령 창 방지
        self._window = ui.Window("Event Summary", width=460, height=380)
        with self._window.frame:
            with ui.VStack(spacing=6, style={"margin": 3}):
                ui.Label("Event Summary", height=30, style={"font_size": 18, "font_weight": "bold"})

                ui.Label("Event Search (twin time range):", height=20,
                         style={"font_weight": "bold", "font_size": 16})
                with ui.HStack(height=25, spacing=5):
                    ui.Label("Start:", width=40)
                    self._search_start = ui.StringField()
                    ui.Label("End:", width=35)
                    self._search_end = ui.StringField()
                self._prefill_search_range()
                with ui.HStack(height=28, spacing=8):
                    self._search_button = ui.Button("Search Events", width=120,
                                                    clicked_fn=self._on_search_clicked)
                    ui.Label("(YYYY-MM-DD HH:MM:SS -- Data Lake mode)",
                             style={"color": 0xFF888888})

                with ui.HStack(height=28, spacing=8):
                    ui.Label("Play (s):", width=55)
                    play_field = ui.FloatField(width=60)
                    play_field.model.set_value(5.0)
                    self._play_length_model = play_field.model
                    self._play_all_button = ui.Button("Play All", width=110,
                                                      clicked_fn=self._on_play_all_clicked)

                with ui.VStack(spacing=4):
                    ui.Label("Status:", height=18, style={"font_weight": "bold", "font_size": 16})
                    with ui.ScrollingFrame(height=44):
                        self._status_label = ui.Label(
                            "Ready. Search a twin time range.",
                            word_wrap=True, style={"color": 0xFFCCCCCC})

                with ui.ScrollingFrame(height=150):
                    self._results_stack = ui.VStack(spacing=2)

                ui.Spacer()

    # ---- status ------------------------------------------------------------ #

    def _update_status(self, message: str, error=False, success=False, processing=False):
        if not self._status_label:
            return
        self._status_label.text = message
        if error:
            self._status_label.style = {"color": 0xFF00FFFF}  # 노랑 (omni.ui 색은 ABGR)
        elif success:
            self._status_label.style = {"color": 0xFF44FF44}
        elif processing:
            self._status_label.style = {"color": 0xFFFFAA44}
        else:
            self._status_label.style = {"color": 0xFFCCCCCC}

    def _set_status_threadsafe(self, message: str, **kw):
        self._ui_dispatcher.submit(lambda: self._update_status(message, **kw))

    # ---- Event Search (이벤트 인덱스 시간창 조회) --------------------------- #

    def _prefill_search_range(self):
        """로드된 데이터 범위를 기본 검색 창으로 — 사용자가 형식을 안 외워도 되게."""
        try:
            if self._core.has_data():
                self._search_start.model.set_value(
                    self._core.get_data_start_time().strftime(_DT_FMT))
                self._search_end.model.set_value(
                    self._core.get_data_end_time().strftime(_DT_FMT))
                return
        except Exception:
            pass
        today = datetime.date.today().isoformat()
        self._search_start.model.set_value(f"{today} 00:00:00")
        self._search_end.model.set_value(f"{today} 23:59:59")

    def _on_search_clicked(self):
        import carb

        try:
            start = datetime.datetime.strptime(
                self._search_start.model.get_value_as_string().strip(), _DT_FMT)
            end = datetime.datetime.strptime(
                self._search_end.model.get_value_as_string().strip(), _DT_FMT)
        except ValueError:
            self._update_status(f"Error: time format must be {_DT_FMT}", error=True)
            return
        index_root = self._core.get_output_root_uri_for_active_mode()
        if not index_root:
            self._update_status("Error: event search requires Data Lake mode.", error=True)
            return
        self._update_status("Searching events...", processing=True)
        self._search_button.enabled = False

        def search_async():
            try:
                from .event_index import query_events

                hits = query_events(index_root, start, end)
                self._ui_dispatcher.submit(lambda: self._apply_search_results(hits))
            except Exception as e:
                carb.log_error(f"[EventSummary] search error: {e!r}")
                msg = str(e)
                self._ui_dispatcher.submit(lambda m=msg: self._apply_search_error(m))

        threading.Thread(target=search_async, daemon=True, name="EventSearch").start()

    def _apply_search_results(self, hits: list):
        import omni.ui as ui

        self._search_button.enabled = True
        self._search_results = list(hits)
        self._results_stack.clear()
        with self._results_stack:
            if not hits:
                ui.Label("(no events in range)", style={"color": 0xFF888888})
            for i, ev in enumerate(hits):
                label = (f"{ev.get('time', '?')}  obj {ev.get('ids', [])}  "
                         f"[{ev.get('video', '?')}]")
                btn = ui.Button(label, height=22)
                btn.set_clicked_fn(lambda idx=i: self._on_event_selected(idx))
        if not hits:
            self._update_status("Found 0 events in range.")
            return
        span = event_time_span(hits)
        if span:
            lo, hi = span
            self._update_status(
                f"Found {len(hits)} events ({lo.strftime('%H:%M:%S')} ~ {hi.strftime('%H:%M:%S')})",
                success=True)
        else:
            self._update_status(f"Found {len(hits)} events.", success=True)

    def _apply_search_error(self, message: str):
        self._search_button.enabled = True
        self._update_status(f"Search error: {message}", error=True)

    # ---- 이벤트 재생 (점프 → N초 재생 → 자동 일시정지) --------------------- #

    def _seek_to_event(self, t: datetime.datetime, ids=None) -> bool:
        """이벤트 시점으로 트윈 재구축: 로드 범위 밖이면 주변 ±5분을 먼저 로드."""
        import carb

        try:
            if not (self._core.get_start_time() <= t <= self._core.get_end_time()):
                pad = datetime.timedelta(minutes=5)
                if not self._core.load_time_range(t - pad, t + pad):
                    self._update_status(f"Error: no data around {t}", error=True)
                    return False
            self._core.set_current_time(t)
            try:
                self._core.move_camera_to_event_at(t, ids)  # 카메라는 best-effort
            except Exception as e:
                carb.log_warn(f"[EventSummary] camera move failed: {e!r}")
            return True
        except Exception as e:
            carb.log_error(f"[EventSummary] jump error: {e!r}")
            self._update_status(f"Jump error: {e}", error=True)
            return False

    def _pause_if_playing(self):
        if self._core.is_playing():
            self._core.toggle_playback()

    def _start_playback(self):
        if not self._core.is_playing():
            self._core.toggle_playback()

    def _on_event_selected(self, idx: int):
        try:
            ev = self._search_results[idx]
        except IndexError:
            self._update_status("Error: event no longer available.", error=True)
            return
        t = parse_event_time(ev)
        if t is None:
            self._update_status("Error: bad event record (time)", error=True)
            return
        play_length = self._play_length_model.get_value_as_float()
        # 진행 중 Play All / 이전 폴링을 무효화(세대 bump).
        self._play_all_active = False
        gen = self._generation.bump()
        if not self._seek_to_event(t, ev.get("ids")):
            return
        self._start_playback()
        self._update_status(f"Playing {t.strftime('%H:%M:%S')} (obj {ev.get('ids', [])})", success=True)
        threading.Thread(target=self._poll_and_pause, args=(t, play_length, gen),
                         daemon=True, name="EventPlayback").start()

    def _poll_and_pause(self, event_time: datetime.datetime, play_length: float, generation: int):
        """twin time 0.2s 폴링: 이벤트+길이 도달 또는 로드 창 끝 도달 시 일시정지.

        세대가 바뀌면(다른 이벤트 클릭) 정지 호출 없이 종료 — 새 선택이 재생을 이어받는다.
        """
        while self._generation.is_current(generation):
            cur = self._core.get_current_time()
            if playback_reached_end(cur, event_time, play_length) or cur >= self._core.get_end_time():
                self._ui_dispatcher.submit(self._pause_if_playing)
                return
            time.sleep(0.2)

    # ---- Play All (검색 결과 시간순 순차 재생) ----------------------------- #

    def _on_play_all_clicked(self):
        if self._play_all_active:
            # 진행 중 재클릭 → 중단: 세대 bump로 워커 종료. 정지는 여기서 직접 —
            # 워커는 세대가 무효면 정지를 안 보낸다(이벤트 클릭 인수인계와 구분).
            self._play_all_active = False
            self._generation.bump()
            self._pause_if_playing()
            self._update_status("Stopped.")
            return
        if not self._search_results:
            self._update_status("No events to play. Search first.", error=True)
            return
        play_length = self._play_length_model.get_value_as_float()
        events = sort_events_by_time(self._search_results)
        self._play_all_active = True
        self._play_all_button.text = "Stop"
        gen = self._generation.bump()
        threading.Thread(target=self._play_all_worker, args=(events, play_length, gen),
                         daemon=True, name="EventPlayAll").start()

    def _play_all_worker(self, events: list, play_length: float, generation: int):
        total = len(events)
        for i, ev in enumerate(events, start=1):
            if not self._play_all_active or not self._generation.is_current(generation):
                break
            t = parse_event_time(ev)
            if t is None:
                continue
            done = threading.Event()

            def _jump(ev=ev, t=t, done=done):
                if self._seek_to_event(t, ev.get("ids")):
                    self._start_playback()
                done.set()

            self._ui_dispatcher.submit(_jump)
            done.wait(timeout=10.0)
            self._set_status_threadsafe(f"Playing {i}/{total} - {t.strftime('%H:%M:%S')}")
            while self._play_all_active and self._generation.is_current(generation):
                cur = self._core.get_current_time()
                if playback_reached_end(cur, t, play_length) or cur >= self._core.get_end_time():
                    break
                time.sleep(0.2)
            # 세대가 무효면(이벤트 클릭 인수인계/Stop) 정지 금지 — 새 재생을 끄게 된다.
            if self._generation.is_current(generation):
                self._ui_dispatcher.submit(self._pause_if_playing)

        self._play_all_active = False
        self._ui_dispatcher.submit(self._on_play_all_finished)

    def _on_play_all_finished(self):
        self._play_all_button.text = "Play All"

    # ---- lifecycle --------------------------------------------------------- #

    def destroy(self):
        self._play_all_active = False
        self._generation.bump()  # 잔여 폴링 스레드 무효화
        if self._ui_dispatcher:
            self._ui_dispatcher.shutdown()
            self._ui_dispatcher = None
        if self._window:
            self._window.destroy()
            self._window = None

    def show(self):
        if self._window:
            self._window.visible = True

    def hide(self):
        if self._window:
            self._window.visible = False
