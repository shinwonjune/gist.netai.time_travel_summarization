# event_window.py - Event Processing Window

import datetime
import threading
from pathlib import Path
from urllib.parse import urlparse

import carb
import omni.ui as ui

from ..app.paths import ExtensionPaths
from ..ui.task_dispatcher import UiTaskDispatcher

_DT_FMT = "%Y-%m-%d %H:%M:%S"


class EventProcessingWindow:
    """Window for processing VLM event detection results."""

    def __init__(self, core, ext_id: str):
        self._core = core
        self._ext_id = ext_id
        self._window = None
        self._ui_dispatcher = UiTaskDispatcher("EventProcessingWindowUiDispatcher")
        self._paths = ExtensionPaths(Path(__file__).resolve().parent.parent)

        # UI state
        self._json_filename_model = ui.SimpleStringModel("video_18_20251113_232343.json")
        self._status_label = None
        self._process_button = None
        self._search_results: list[dict] = []

        self._build_ui()
    
    def _build_ui(self):
        """Build the event processing window UI."""
        self._window = ui.Window("Event Post Processing", width=440, height=520)
        
        with self._window.frame:
            with ui.VStack(spacing=10, style={"margin": 3}):
                # Title
                ui.Label("Event Post Processing", height=30, style={"font_size": 18, "font_weight": "bold"})
                
                ui.Spacer(height=5)
                
                # JSON File Input / URI
                with ui.VStack(spacing=5):
                    ui.Label("Input JSON File or URI:", height=20)
                    with ui.HStack(spacing=5):
                        ui.Label("artifacts/vlm_outputs/ or s3://...", width=180, style={"font_size": 16})
                        ui.StringField(model=self._json_filename_model, height=25)
                    ui.Label("(local filename, file://, or s3:// MinIO URI)", height=15, style={"color": 0xFF888888, "font_size": 16})
                
                ui.Spacer(height=5)

                # Process Button
                self._process_button = ui.Button("Process Events", height=40, clicked_fn=self._on_process_clicked)
                
                ui.Spacer(height=5)
                
                # Status Display
                with ui.VStack(spacing=5):
                    ui.Label("Status:", height=20, style={"font_weight": "bold","font_size": 16})
                    with ui.ScrollingFrame(height=50):
                        self._status_label = ui.Label(
                            "Ready to process events.",
                            word_wrap=True,
                            style={"color": 0xFFCCCCCC}
                        )

                # ---- Event Search — 이벤트 인덱스(vlm_events) 시간창 조회 ------- #
                # 파일명 릴레이 대신 twin time 구간으로 검색, 선택하면 그 시점 재구축.
                ui.Spacer(height=5)
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
                    ui.Label("(YYYY-MM-DD HH:MM:SS — Data Lake 모드)",
                             style={"color": 0xFF888888})
                self._results_stack = None
                with ui.ScrollingFrame(height=120):
                    self._results_stack = ui.VStack(spacing=2)

                ui.Spacer()
    
    def _on_process_clicked(self):
        """Handle process button click."""
        json_filename = self._json_filename_model.get_value_as_string()
        
        if not json_filename:
            self._update_status("Error: Please specify a JSON filename.", error=True)
            return
        
        json_path = self._resolve_json_input(json_filename)
        if not json_path:
            self._update_status("Error: File not found or unsupported URI.", error=True)
            return

        self._update_status("Processing events...", processing=True)
        self._process_button.enabled = False

        def process_async():
            try:
                success = self._core.process_event_json(str(json_path))
                self._ui_dispatcher.submit(lambda: self._apply_process_result(success))
            except Exception as e:
                carb.log_error(f"[EventWindow] Processing error: {e}")
                import traceback
                carb.log_error(traceback.format_exc())
                error_message = str(e)
                self._ui_dispatcher.submit(lambda message=error_message: self._apply_process_error(message))

        thread = threading.Thread(target=process_async, daemon=True)
        thread.start()

    def _resolve_json_input(self, json_filename: str) -> str | None:
        candidate = json_filename.strip()
        if not candidate:
            return None

        parsed = urlparse(candidate)
        if parsed.scheme in ("s3", "minio", "file"):
            return candidate

        local_path = self._paths.resolve_input_file("vlm_outputs", candidate)
        if local_path.exists():
            return str(local_path)
        return None
    
    def _update_status(self, message: str, error=False, success=False, processing=False):
        """Update status label with color coding."""
        if self._status_label:
            self._status_label.text = message
            
            if error:
                self._status_label.style = {"color": 0xFFFF4444}
            elif success:
                self._status_label.style = {"color": 0xFF44FF44}
            elif processing:
                self._status_label.style = {"color": 0xFFFFAA44}
            else:
                self._status_label.style = {"color": 0xFFCCCCCC}

    def _apply_process_result(self, success: bool):
        self._process_button.enabled = True
        if success:
            self._update_status(
                "Events processed successfully!\n"
                "- JSONL saved\n"
                "- Position data extracted\n"
                "Check artifacts folders for results.",
                success=True,
            )
            return

        self._update_status("✗ Event processing failed. Check console for details.", error=True)

    def _apply_process_error(self, message: str):
        self._process_button.enabled = True
        self._update_status(f"✗ Error: {message}", error=True)

    # ---- Event Search (이벤트 인덱스 시간창 조회 → 선택 시 재구축) ------------ #

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
        try:
            start = datetime.datetime.strptime(
                self._search_start.model.get_value_as_string().strip(), _DT_FMT)
            end = datetime.datetime.strptime(
                self._search_end.model.get_value_as_string().strip(), _DT_FMT)
        except ValueError:
            self._update_status(f"Error: 시각 형식은 {_DT_FMT}", error=True)
            return
        index_root = self._core.get_output_root_uri_for_active_mode()
        if not index_root:
            self._update_status("Error: 이벤트 검색은 Data Lake 모드에서만 가능합니다.", error=True)
            return
        self._update_status("Searching events...", processing=True)
        self._search_button.enabled = False

        def search_async():
            try:
                from .event_index import query_events

                hits = query_events(index_root, start, end)
                self._ui_dispatcher.submit(lambda: self._apply_search_results(hits))
            except Exception as e:
                carb.log_error(f"[EventWindow] search error: {e!r}")
                msg = str(e)
                self._ui_dispatcher.submit(lambda m=msg: self._apply_search_error(m))

        threading.Thread(target=search_async, daemon=True, name="EventSearch").start()

    def _apply_search_results(self, hits: list):
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
        self._update_status(f"{len(hits)} events found.", success=True)

    def _apply_search_error(self, message: str):
        self._search_button.enabled = True
        self._update_status(f"✗ Search error: {message}", error=True)

    def _on_event_selected(self, idx: int):
        """이벤트 시점으로 트윈 재구축: 로드 범위 밖이면 주변 ±5분을 먼저 로드."""
        try:
            ev = self._search_results[idx]
            t = datetime.datetime.fromisoformat(ev["time"])
        except (IndexError, KeyError, TypeError, ValueError) as e:
            self._update_status(f"Error: bad event record ({e})", error=True)
            return
        try:
            if not (self._core.get_start_time() <= t <= self._core.get_end_time()):
                pad = datetime.timedelta(minutes=5)
                if not self._core.load_time_range(t - pad, t + pad):
                    self._update_status(f"Error: no data around {t}", error=True)
                    return
            self._core.set_current_time(t)
            self._update_status(f"Jumped to {t} (obj {ev.get('ids', [])})", success=True)
        except Exception as e:
            carb.log_error(f"[EventWindow] jump error: {e!r}")
            self._update_status(f"✗ Jump error: {e}", error=True)
    
    def destroy(self):
        """Clean up the window."""
        if self._ui_dispatcher:
            self._ui_dispatcher.shutdown()
            self._ui_dispatcher = None
        if self._window:
            self._window.destroy()
            self._window = None
    
    def show(self):
        """Show the window."""
        if self._window:
            self._window.visible = True
    
    def hide(self):
        """Hide the window."""
        if self._window:
            self._window.visible = False
