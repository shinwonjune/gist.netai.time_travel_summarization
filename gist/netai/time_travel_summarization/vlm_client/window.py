# vlm_client_window.py - UI for VLM Client

import omni.ui as ui
import carb
import threading

from ..ui.task_dispatcher import UiTaskDispatcher


class VLMClientWindow:
    """VLM Client UI Window."""
    
    def __init__(self, vlm_core, ext_id):
        """Initialize VLM Client window."""
        self._vlm_core = vlm_core
        self._ext_id = ext_id
        self._ui_dispatcher = UiTaskDispatcher("VLMClientWindowUiDispatcher")
        
        # Create window
        self._window = ui.Window("VLM Client", width=540, height=270)
        
        with self._window.frame:
            with ui.VStack(spacing=5, style={"margin": 3}):
                # Title
                ui.Label("VLM Video Analysis Client", style={"font_size": 20, "font_weight": "bold"})
                
                # Video input section
                with ui.HStack(height=22, spacing=5):
                    ui.Label("Source:", width=60, style={"font_size": 16, "font_weight": "bold"})
                    ui.Label("Filename or URI (s3://, file://)", width=190)
                    self._video_filename_field = ui.StringField()
                    self._video_filename_field.model.set_value("video_19.mp4")
                
                # Video ID display — 폰트가 행 높이를 넘치면 이웃 줄과 겹친다
                with ui.HStack(height=24, spacing=5):
                    ui.Label("Video ID:", width=60)
                    self._video_id_label = ui.Label("Not uploaded", style={"color": 0xFF888888})
                
                # Action buttons
                with ui.HStack(height=28, spacing=8):
                    self._upload_button = ui.Button("Upload", width=0)
                    self._upload_button.set_clicked_fn(self._on_upload_clicked)
                    
                    self._delete_button = ui.Button("Delete", width=0)
                    self._delete_button.set_clicked_fn(self._on_delete_clicked)
                    self._delete_button.enabled = False
                    
                    self._generate_button = ui.Button("Generate", width=0)
                    self._generate_button.set_clicked_fn(self._on_generate_clicked)
                    self._generate_button.enabled = False
                
                # Separator
                with ui.HStack(height=1):
                    ui.Line(style={"color": 0xFF666666})
                
                # Model and preset selection
                # 모델명은 vLLM 서빙명(run_serve.sh --served-model-name)과 일치해야 한다.
                # VSS 시절의 다중 모델 목록(gpt-4o 등)은 direct 경로에서 무의미해 제거.
                ui.Label("Settings:", style={"font_size": 16, "font_weight": "bold"})

                with ui.HStack(height=22, spacing=5):
                    ui.Label("Model:", width=50)
                    self._model_combo = ui.ComboBox(0, "Qwen3-VL-8B-Instruct")

                with ui.HStack(height=22, spacing=5):
                    ui.Label("Preset:", width=50)
                    self._preset_combo = ui.ComboBox(0, "simple_view", "twin_view")

                # Separator
                with ui.HStack(height=1):
                    ui.Line(style={"color": 0xFF666666})

                # Status display — 긴 메시지(저장 URI 등)는 wrap+스크롤로 가둬 겹침 방지
                with ui.HStack(height=44, spacing=5):
                    ui.Label("Status:", width=50)
                    with ui.ScrollingFrame(height=44):
                        self._status_label = ui.Label("Ready", word_wrap=True,
                                                      style={"color": 0xFF00AA00})

    def _on_upload_clicked(self):
        """Handle Upload button click."""
        video_source = self._video_filename_field.model.get_value_as_string()
        
        if not video_source:
            self._update_status("Please enter video filename or URI", is_error=True)
            return
        
        self._update_status("Uploading video...", is_processing=True)
        
        # Disable upload button during processing
        self._upload_button.enabled = False
        
        # Run upload in separate thread to avoid blocking UI
        def upload_async():
            success = self._vlm_core.upload_video(video_source)
            self._ui_dispatcher.submit(lambda: self._apply_upload_result(success))
        
        thread = threading.Thread(target=upload_async, daemon=True)
        thread.start()
    
    def _on_delete_clicked(self):
        """Handle Delete button click."""
        if not self._vlm_core.has_video_uploaded():
            self._update_status("No video to delete", is_error=True)
            return
        
        self._update_status("Deleting video...", is_processing=True)
        
        # Disable delete button during processing
        self._delete_button.enabled = False
        
        # Run delete in separate thread to avoid blocking UI
        def delete_async():
            success = self._vlm_core.delete_video()
            self._ui_dispatcher.submit(lambda: self._apply_delete_result(success))
        
        thread = threading.Thread(target=delete_async, daemon=True)
        thread.start()
    
    def _on_generate_clicked(self):
        """Handle Generate button click."""
        if not self._vlm_core.has_video_uploaded():
            self._update_status("No video uploaded", is_error=True)
            return
        
        # Get selected model and preset
        model_index = self._model_combo.model.get_item_value_model().as_int
        preset_index = self._preset_combo.model.get_item_value_model().as_int

        models = ["Qwen3-VL-8B-Instruct"]
        presets = ["simple_view", "twin_view"]

        model = models[model_index]
        preset = presets[preset_index]

        self._update_status(f"Generating with {model}...", is_processing=True)

        # Disable generate button during processing
        self._generate_button.enabled = False

        # Get video filename for output naming
        video_filename = self._video_filename_field.model.get_value_as_string()

        # Run generation in separate thread to avoid blocking UI
        def generate_async():
            output_root_uri = None
            try:
                from ..extension import get_active_core

                active_core = get_active_core()
                if active_core is not None:
                    output_root_uri = active_core.get_output_root_uri_for_active_mode()
            except Exception as exc:
                carb.log_warn(f"[VLMClientWindow] Could not resolve Data Lake output root: {exc!r}")

            success, output_filename = self._vlm_core.generate_captions(
                model=model,
                preset_name=preset,
                video_filename=video_filename,
                output_root_uri=output_root_uri,
            )
            self._ui_dispatcher.submit(lambda: self._apply_generate_result(success, output_filename))
        
        thread = threading.Thread(target=generate_async, daemon=True)
        thread.start()
    
    def _update_status(self, message: str, is_error: bool = False, is_processing: bool = False):
        """Update status label with color."""
        self._status_label.text = message
        
        if is_error:
            self._status_label.style = {"color": 0xFFFF0000}  # Red
        elif is_processing:
            self._status_label.style = {"color": 0xFFFFAA00}  # Orange
        else:
            self._status_label.style = {"color": 0xFF00AA00}  # Green

    def _apply_upload_result(self, success: bool):
        self._upload_button.enabled = True
        if success:
            video_id = self._vlm_core.get_current_video_id() or "Unknown"
            self._video_id_label.text = video_id
            self._video_id_label.style = {"color": 0xFF00AA00}
            self._delete_button.enabled = True
            self._generate_button.enabled = True
            self._update_status(f"Upload successful! ID: {video_id[:8]}...", is_error=False)
            return

        self._update_status("Upload failed. Check console for details.", is_error=True)

    def _apply_delete_result(self, success: bool):
        if success:
            self._video_id_label.text = "Not uploaded"
            self._video_id_label.style = {"color": 0xFF888888}
            self._generate_button.enabled = False
            self._update_status("Video deleted successfully", is_error=False)
            return

        self._delete_button.enabled = True
        self._update_status("Delete failed. Check console for details.", is_error=True)

    def _apply_generate_result(self, success: bool, output_filename: str | None):
        self._generate_button.enabled = True
        if success and output_filename:
            self._update_status(f"Saved: {output_filename}", is_error=False)
            cb = getattr(self, "_generate_complete_cb", None)
            if cb:
                try:
                    cb(output_filename)  # Event Post Processing 입력 자동 채움
                except Exception as exc:
                    carb.log_warn(f"[VLMClientWindow] generate callback failed: {exc!r}")
            return

        self._update_status("Generation failed. Check console for details.", is_error=True)

    def set_generate_complete_callback(self, cb) -> None:
        """추론 완료 시 산출 JSON(로컬 파일명 또는 s3 URI)을 넘길 콜백 등록."""
        self._generate_complete_cb = cb

    def set_source_uri(self, uri: str) -> None:
        """메인 창 Capture 완료 시 호출(캡처 워커 스레드) — Source 필드 자동 채움.

        과거 A1/A2 캡처 버튼(realtime_capture 검증기 잔재)을 대체하는 연결:
        캡처는 메인 창 한 곳에서만 하고(라벨·사이드카 보장), 산출 URI만 여기로
        전달받아 Upload→Generate로 이어간다.
        """
        def _apply():
            self._video_filename_field.model.set_value(uri)
            self._update_status("Capture ready — press Upload", is_error=False)

        self._ui_dispatcher.submit(_apply)

    def destroy(self):
        """Clean up the window."""
        if self._ui_dispatcher:
            self._ui_dispatcher.shutdown()
            self._ui_dispatcher = None
        if self._window:
            self._window.destroy()
            self._window = None
