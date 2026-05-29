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
        self._window = ui.Window("VLM Client", width=540, height=285)
        
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
                
                # Video ID display
                with ui.HStack(height=20, spacing=5):
                    ui.Label("Video ID:", width=60, style={"font_size": 15})
                    self._video_id_label = ui.Label("Not uploaded", style={"color": 0xFF888888, "font_size": 15})
                
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
                ui.Label("Settings:", style={"font_size": 16, "font_weight": "bold"})
                
                with ui.HStack(height=22, spacing=5):
                    ui.Label("Model:", width=50)
                    self._model_combo = ui.ComboBox(1, "gpt-4o", "Qwen3-VL-8B-Instruct", "cosmos-reason1", "vila-1.5", "nvila")
                
                with ui.HStack(height=22, spacing=5):
                    ui.Label("Preset:", width=50)
                    self._preset_combo = ui.ComboBox(0, "simple_view", "twin_view")
                
                with ui.HStack(height=22, spacing=5):
                    ui.Label("Overlap:", width=50)
                    self._overlap_field = ui.IntField()
                    self._overlap_field.model.set_value(0)
                    ui.Label("sec", width=30)
                
                # Separator
                with ui.HStack(height=1):
                    ui.Line(style={"color": 0xFF666666})
                
                # Status display
                with ui.HStack(height=20, spacing=5):
                    ui.Label("Status:", width=50, style={"font_size": 16})
                    self._status_label = ui.Label("Ready", style={"color": 0xFF00AA00, "font_size": 16})

                with ui.HStack(height=28, spacing=8):
                    ui.Label("Capture:", width=60, style={"font_size": 15, "font_weight": "bold"})
                    self._a1_capture_button = ui.Button("A1 (baseline)", width=0)
                    self._a1_capture_button.set_clicked_fn(self._on_a1_capture_clicked)
                    self._a1_status_label = ui.Label("Idle", style={"color": 0xFF888888, "font_size": 14})
                    self._a2_capture_button = ui.Button("A2 (realtime)", width=0)
                    self._a2_capture_button.set_clicked_fn(self._on_a2_capture_clicked)
                    self._a2_status_label = ui.Label("Idle", style={"color": 0xFF888888, "font_size": 14})

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
        
        models = ["gpt-4o", "Qwen3-VL-8B-Instruct", "cosmos-reason1", "vila-1.5", "nvila"]
        presets = ["simple_view", "twin_view"]
        
        model = models[model_index]
        preset = presets[preset_index]
        
        self._update_status(f"Generating with {model}...", is_processing=True)
        
        # Disable generate button during processing
        self._generate_button.enabled = False
        
        # Get video filename for output naming
        video_filename = self._video_filename_field.model.get_value_as_string()
        
        # Get chunk overlap duration
        chunk_overlap = self._overlap_field.model.get_value_as_int()
        
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
                chunk_overlap_duration=chunk_overlap,
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
            return

        self._update_status("Generation failed. Check console for details.", is_error=True)

    def _build_a2_output_uri(self, active_core, module_dir, filename: str) -> str:
        output_root = None
        if active_core is not None:
            output_root = active_core.get_video_output_uri_for_active_mode()
        if output_root:
            output_root = output_root.strip()
        if output_root:
            if "://" in output_root:
                return f"{output_root.rstrip('/')}/{filename}"

            from pathlib import Path as _P

            root_path = _P(output_root)
            if not root_path.is_absolute():
                root_path = module_dir / output_root
            return (root_path / filename).resolve().as_uri()

        from ..app.paths import ExtensionPaths

        paths = ExtensionPaths(module_dir)
        return (paths.videos_dir / filename).resolve().as_uri()

    def _apply_a2_capture_result(self, msg: str, output_uri: str | None, upload_success: bool | None):
        self._a2_status_label.text = msg
        self._a2_capture_button.enabled = True

        if output_uri:
            self._video_filename_field.model.set_value(output_uri)

        if upload_success is None:
            return

        if upload_success:
            video_id = self._vlm_core.get_current_video_id() or "Unknown"
            self._video_id_label.text = video_id
            self._video_id_label.style = {"color": 0xFF00AA00}
            self._delete_button.enabled = True
            self._generate_button.enabled = True
            self._update_status(f"A2 capture uploaded. ID: {video_id[:8]}...", is_error=False)
            return

        self._update_status("A2 capture succeeded, VLM upload failed.", is_error=True)

    def _on_a1_capture_clicked(self):
        self._a1_capture_button.enabled = False
        self._a1_status_label.text = "Capturing (A1)..."

        def _worker():
            try:
                from datetime import datetime as _dt
                from pathlib import Path as _P

                from ..app.paths import ExtensionPaths
                from ..video_capture import CaptureRequest, MovieCaptureRunner

                paths = ExtensionPaths(_P(__file__).resolve().parent.parent)
                ts = _dt.now().strftime("%Y%m%dT%H%M%S")
                out = (paths.videos_dir / f"a1_{ts}.mp4").resolve().as_uri()
                req = CaptureRequest(duration_s=10.0, output_uri=out, label="ui_button")
                runner = MovieCaptureRunner()
                res = runner.capture(req)
                msg = (
                    f"A1 OK {res.wall_clock_s:.1f}s {res.output_size_bytes // 1024}KB"
                    if res.success
                    else f"A1 FAIL {res.error}"
                )
            except Exception as exc:
                msg = f"A1 ERROR {exc!r}"

            self._ui_dispatcher.submit(
                lambda: (
                    setattr(self._a1_status_label, "text", msg),
                    setattr(self._a1_capture_button, "enabled", True),
                )
            )

        threading.Thread(target=_worker, daemon=True).start()

    def _on_a2_capture_clicked(self):
        self._a2_capture_button.enabled = False
        self._a2_status_label.text = "Capturing (A2)..."

        def _worker():
            try:
                from datetime import datetime as _dt
                from pathlib import Path as _P

                from ..extension import get_active_core
                from ..video_capture import CaptureRequest, RealtimeCaptureRunner

                active_core = get_active_core()
                module_dir = _P(__file__).resolve().parent.parent
                ts = _dt.now().strftime("%Y%m%dT%H%M%S")
                out = self._build_a2_output_uri(active_core, module_dir, f"a2_{ts}.mp4")
                req = CaptureRequest(duration_s=10.0, output_uri=out, label="ui_button")
                runner = RealtimeCaptureRunner(core=active_core)
                res = runner.capture(req)
                output_uri = res.output_uri if res.success else None
                upload_success = None
                if res.success:
                    carb.log_info(f"[VLMClientWindow] A2 capture succeeded: {res.output_uri}")
                    upload_success = self._vlm_core.upload_video(res.output_uri)
                    if upload_success:
                        carb.log_info(f"[VLMClientWindow] A2 capture auto-uploaded to VLM: {res.output_uri}")
                        msg = (
                            f"A2 OK+VLM {res.wall_clock_s:.1f}s "
                            f"{res.output_size_bytes // 1024}KB drop={res.dropped_frames}"
                        )
                    else:
                        carb.log_error(f"[VLMClientWindow] A2 capture VLM auto-upload failed: {res.output_uri}")
                        msg = (
                            f"A2 OK, VLM FAIL {res.wall_clock_s:.1f}s "
                            f"{res.output_size_bytes // 1024}KB drop={res.dropped_frames}"
                        )
                else:
                    msg = f"A2 FAIL {res.error}"
            except Exception as exc:
                msg = f"A2 ERROR {exc!r}"
                output_uri = None
                upload_success = None

            self._ui_dispatcher.submit(
                lambda: self._apply_a2_capture_result(msg, output_uri, upload_success)
            )

        threading.Thread(target=_worker, daemon=True).start()
    
    def destroy(self):
        """Clean up the window."""
        if self._ui_dispatcher:
            self._ui_dispatcher.shutdown()
            self._ui_dispatcher = None
        if self._window:
            self._window.destroy()
            self._window = None
