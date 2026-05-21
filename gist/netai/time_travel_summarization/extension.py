# SPDX-FileCopyrightText: Copyright (c) 2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

import os
from pathlib import Path

_INSTANCE_REGISTRY = {}


def get_active_core():
    """Return the active TimeTravelCore instance, or None if extension not started."""
    return _INSTANCE_REGISTRY.get("core")


# Kit이 extension을 인식하려면 omni.ext.IExt를 상속해야 함.
# headless(WSL) 환경에선 omni.ext 부재 → object로 폴백해서 module import만 가능하게.
def _get_i_ext_base():
    try:
        import omni.ext as omni_ext

        return omni_ext.IExt
    except ImportError:
        return object


class NetAITimetravelDreamAI(_get_i_ext_base()):
    """Time Travel Extension for visualizing object movements over time."""
    
    def on_startup(self, ext_id):
        """Initialize the extension."""
        import carb

        from .app.facade import TimeTravelCore
        from .event_processing.window import EventProcessingWindow
        from .ui.main_window import TimeTravelWindow
        from .vlm_client.core import VLMClientCore
        from .vlm_client.window import VLMClientWindow

        print("[netai.timetravel_dreamai] Extension startup")
        
        # Print current working directory and extension path
        current_dir = os.getcwd()
        extension_file = Path(__file__).absolute()
        extension_dir = extension_file.parent
        
        # Initialize core logic
        self._core = TimeTravelCore() 
        _INSTANCE_REGISTRY["core"] = self._core
        
        # Load configuration
        config_path = extension_dir / "config.json"
        
        if self._core.load_config(str(config_path)):
            if self._core.should_auto_generate():
                self._core.auto_generate_astronauts()
            # Load data
            self._core.load_data()
        
        # Create main TimeTravel UI window (ALWAYS created)
        self._window = TimeTravelWindow(self._core)
        carb.log_info("[Extension] TimeTravel window created")
        
        # Create Event Processing window
        self._event_window = EventProcessingWindow(self._core, ext_id)
        carb.log_info("[Extension] Event Processing window created")
        
        # Create VLM Client
        self._vlm_client_core = VLMClientCore()
        self._vlm_client_window = VLMClientWindow(self._vlm_client_core, ext_id)
        carb.log_info("[Extension] VLM Client window created")
        
        # Try to create overlay components (OPTIONAL - won't break if it fails)
        self._overlay = None
        self._overlay_control = None
        
        try:
            from .overlay.core import ViewOverlay
            from .overlay.window import OverlayControlWindow
            from omni.kit.viewport.utility import get_active_viewport_window

            overlay_available = True
        except Exception as e:
            carb.log_warn(f"[TimeTravel] Overlay components not available: {e}")
            overlay_available = False

        if overlay_available:
            try:
                # Get active viewport window
                viewport_window = get_active_viewport_window()
                
                if viewport_window:
                    # Create viewport overlay (3D labels above prims + time display)
                    self._overlay = ViewOverlay(viewport_window, ext_id, self._core)
                    carb.log_info("[Extension] Viewport overlay created")
                    
                    # Create control window
                    self._overlay_control = OverlayControlWindow(self._overlay)
                    carb.log_info("[Extension] Overlay control window created")
                else:
                    carb.log_warn("[Extension] No active viewport found")
            except Exception as e:
                carb.log_error(f"[Extension] Failed to create overlay: {e}")
                import traceback
                carb.log_error(traceback.format_exc())
                self._overlay = None
                self._overlay_control = None
        else:
            carb.log_info("[Extension] Overlay features disabled")
        
        # Start update loop (Events 2.0)
        import omni.kit.app
        self._update_sub = (
            omni.kit.app.get_app_interface()
            .get_update_event_stream()
            .create_subscription_to_pop(self._on_update)
        )
        
        # Set initial time to earliest timestamp
        if self._core.has_data():
            self._core.set_to_earliest_time()
    
    def _on_update(self, e):
        """Update loop for playback and UI updates."""
        dt = e.payload.get("dt", 0)
        
        # Update core logic (handles playback) - ALWAYS runs
        self._core.update(dt)
        
        # Update main TimeTravel UI - ALWAYS runs
        if self._window:
            self._window.update_ui()
        
        # Note: ViewOverlay updates itself via frame subscription
        # No need to call update() manually
    
    def on_shutdown(self):
        """Clean up the extension."""
        import carb

        print("[netai.timetravel_dreamai] Extension shutdown")
        
        # Clean up subscription
        if hasattr(self, '_update_sub'):
            self._update_sub = None
        
        # Clean up main TimeTravel window (ALWAYS cleanup)
        if hasattr(self, '_window') and self._window:
            try:
                self._window.destroy()
            except Exception as e:
                carb.log_error(f"[Extension] Error destroying window: {e}")
            self._window = None
        
        # Clean up event window
        if hasattr(self, '_event_window') and self._event_window:
            try:
                self._event_window.destroy()
            except Exception as e:
                carb.log_error(f"[Extension] Error destroying event window: {e}")
            self._event_window = None
        
        # Clean up VLM Client window
        if hasattr(self, '_vlm_client_window') and self._vlm_client_window:
            try:
                self._vlm_client_window.destroy()
            except Exception as e:
                carb.log_error(f"[Extension] Error destroying VLM client window: {e}")
            self._vlm_client_window = None
        
        # Clean up VLM Client core
        if hasattr(self, '_vlm_client_core'):
            self._vlm_client_core = None
        
        # Clean up overlay window (OPTIONAL)
        if hasattr(self, '_overlay_control') and self._overlay_control:
            try:
                self._overlay_control.destroy()
            except Exception as e:
                carb.log_error(f"[Extension] Error destroying overlay control: {e}")
            self._overlay_control = None
        
        # Clean up overlay (OPTIONAL)
        if hasattr(self, '_overlay') and self._overlay:
            try:
                self._overlay.shutdown()
            except Exception as e:
                carb.log_error(f"[Extension] Error destroying overlay: {e}")
            self._overlay = None
        
        # Clean up core
        if hasattr(self, '_core') and self._core:
            try:
                if hasattr(self._core, "_wander") and self._core._wander:
                    self._core._wander.stop()
                self._core.clear_timetravel_objects()
                carb.log_info("[Extension] TimeTravel objects cleared")
            except Exception as e:
                carb.log_error(f"[Extension] Error clearing TimeTravel objects: {e}")
            _INSTANCE_REGISTRY.pop("core", None)
            self._core = None
