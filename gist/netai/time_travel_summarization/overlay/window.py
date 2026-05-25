# overlay_control.py - Simple control window for ViewOverlay

import omni.ui as ui
import carb


class OverlayControlWindow:
    """Simple control window to toggle object ID labels and time display."""
    
    def __init__(self, overlay):
        """Initialize the control window.
        
        Args:
            overlay: ViewOverlay instance to control
        """
        self._overlay = overlay
        
        # Create compact control window
        self._window = ui.Window(
            "View Overlay", 
            width=180, 
            height=85,  # Reduced height for tighter fit
            visible=True
        )
        
        with self._window.frame:
            with ui.VStack(spacing=3, style={"margin": 3}):  # Tighter spacing and margin
                # Title
                ui.Label("Display Options", style={"font_size": 20, "font_weight": "bold"})
                
                # Object ID labels toggle
                with ui.HStack(height=20):
                    self._labels_checkbox = ui.CheckBox(width=16)
                    self._labels_checkbox.model.set_value(True)
                    self._labels_checkbox.model.add_value_changed_fn(self._on_labels_visibility_changed)
                    ui.Label("Object IDs", style={"font_size": 18})
                
                # Time display toggle
                with ui.HStack(height=20):
                    self._time_checkbox = ui.CheckBox(width=16)
                    self._time_checkbox.model.set_value(True)
                    self._time_checkbox.model.add_value_changed_fn(self._on_time_visibility_changed)
                    ui.Label("Timestamp", style={"font_size": 18})
                
                # Visual Complexity section
                ui.Spacer(height=5)
                ui.Label("Visual Complexity", style={"font_size": 20, "font_weight": "bold"})
                with ui.HStack(height=25, spacing=4):
                    for level in ["Full", "Simplified", "Abstract"]:
                        btn = ui.Button(level, width=70)
                        btn.set_clicked_fn(lambda _l=level: self._on_complexity_clicked(_l))

                # Push everything to top
                ui.Spacer()

        carb.log_info("[OverlayControl] Control window created")

    def _get_core(self):
        try:
            from ..extension import get_active_core
            return get_active_core()
        except Exception:
            return None

    def _on_complexity_clicked(self, level: str):
        core = self._get_core()
        if core:
            core.set_visual_complexity(level)

    def _on_labels_visibility_changed(self, model):
        """Handle object ID labels checkbox change."""
        visible = model.get_value_as_bool()
        self._overlay.set_labels_visible(visible)
        carb.log_info(f"[OverlayControl] Object IDs {'enabled' if visible else 'disabled'}")
    
    def _on_time_visibility_changed(self, model):
        """Handle time display checkbox change."""
        visible = model.get_value_as_bool()
        self._overlay.set_time_visible(visible)
        carb.log_info(f"[OverlayControl] Time display {'enabled' if visible else 'disabled'}")
    
    def destroy(self):
        """Clean up the window."""
        if self._window:
            self._window.destroy()
            self._window = None
        carb.log_info("[OverlayControl] Control window destroyed")
