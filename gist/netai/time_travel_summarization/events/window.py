# event_window.py - Event Processing Window

import threading
from pathlib import Path
from urllib.parse import urlparse

import carb
import omni.ui as ui

from ..app.paths import ExtensionPaths
from ..ui.task_dispatcher import UiTaskDispatcher


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

        self._build_ui()
    
    def _build_ui(self):
        """Build the event processing window UI."""
        self._window = ui.Window("Event Post Processing", width=440, height=260)

        with self._window.frame:
            with ui.VStack(spacing=6, style={"margin": 3}):
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
                self._process_button = ui.Button("Process Events", width=120, height=28, clicked_fn=self._on_process_clicked)
                
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

    def set_source_json(self, uri: str) -> None:
        """VLM 추론 완료 시 호출(워커 스레드 가능) — 입력 JSON 필드 자동 채움.

        캡처→VLM Source 자동 채움과 동일 패턴: 산출물 경로를 사람이 복붙하는
        릴레이를 제거하고, 처리 실행 여부만 사용자가 결정한다.
        """
        def _apply():
            self._json_filename_model.set_value(uri)
            self._update_status("VLM result ready - press Process Events")

        self._ui_dispatcher.submit(_apply)

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
                self._status_label.style = {"color": 0xFF00FFFF}  # 노랑 (omni.ui 색은 ABGR)
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
