import datetime
from pathlib import Path
from typing import Dict, List, Optional

import carb

from .config import ExtensionConfig
from .paths import ExtensionPaths
from ..event_processing.summary_service import EventSummaryService
from ..playback.controller import PlaybackController
from ..playback.stage_object_controller import StageObjectController
from ..playback.trajectory_repository import TrajectoryRepository




class TimeTravelCore:
    """Facade that preserves the existing public API while delegating to focused services."""

    def __init__(self):
        self._module_dir = Path(__file__).resolve().parent.parent
        self._paths = ExtensionPaths(self._module_dir)
        self._config = None
        self._prim_map = {}
        self._repository = TrajectoryRepository()
        self._playback = PlaybackController()
        self._stage_objects = StageObjectController()
        self._events = EventSummaryService(self._module_dir, self._repository)
        self._stage_objects.ensure_summarization_camera()

    def load_config(self, config_path: str) -> bool:
        try:
            path = Path(config_path)
            if not path.exists():
                carb.log_warn(f"[TimeTravel] Config file not found: {config_path}")
                return False

            self._config = ExtensionConfig.from_file(config_path)
            self._prim_map = dict(self._config.prim_map)
            self._playback.set_event_summary(self._config.event_summary)
            carb.log_info("[TimeTravel] Config loaded")
            return True
        except Exception as e:
            carb.log_error(f"[TimeTravel] Failed to load config: {e}")
            return False

    def load_data(self) -> bool:
        try:
            if not self._config:
                carb.log_error("[TimeTravel] Config must be loaded before data")
                return False

            uri = self._config.data_uri
            carb.log_info(f"[TimeTravel] Looking for data at URI: {uri}")

            loaded = self._repository.load_from_uri(uri)
            if not loaded:
                carb.log_error(f"[TimeTravel] Data load failed for URI: {uri}")
                return False

            self._playback.configure_data_range(
                self._repository.data_start_time,
                self._repository.data_end_time,
            )
            carb.log_info(
                f"[TimeTravel] Data loaded: {len(self._repository.timestamps)} timestamps, "
                f"{self._repository.data_start_time} to {self._repository.data_end_time}"
            )
            return True
        except Exception as e:
            carb.log_error(f"[TimeTravel] Failed to load data: {e}")
            return False

    def _parse_timestamp(self, timestamp_str: str) -> datetime.datetime:
        return self._repository.parse_timestamp(timestamp_str)

    def _format_timestamp(self, dt: datetime.datetime) -> str:
        return self._repository.format_timestamp(dt)

    def set_time_range(self, start_time: datetime.datetime, end_time: datetime.datetime) -> bool:
        updated = self._playback.set_time_range(start_time, end_time)
        if updated:
            self.update_stage_objects()
        return updated

    def get_data_start_time(self) -> datetime.datetime:
        return self._repository.data_start_time or datetime.datetime.now()

    def get_data_end_time(self) -> datetime.datetime:
        return self._repository.data_end_time or datetime.datetime.now()

    def get_data_at_time(self, timestamp: datetime.datetime) -> Dict:
        return self._repository.get_data_at_time(timestamp)

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

    def update(self, dt: float):
        self._playback.update(dt, self._parse_timestamp, self.set_current_time, self._on_event_requested)

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

    def get_simulation_time(self) -> Optional[datetime.datetime]:
        return self._playback.get_current_time()

    def get_stage_time_string(self) -> str:
        current_time = self._playback.get_current_time()
        if current_time:
            return current_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return "No time set"

    def is_playing(self) -> bool:
        return self._playback.is_playing()

    def get_playback_speed(self) -> float:
        return self._playback.get_playback_speed()

    def set_playback_speed(self, speed: float):
        self._playback.set_playback_speed(speed)

    def has_data(self) -> bool:
        return self._repository.has_data()

    def has_events(self) -> bool:
        return len(self._playback.get_event_summary()) > 0

    def set_use_event_summary(self, use: bool):
        self._playback.set_use_event_summary(use)

    def get_summary_events(self) -> List[str]:
        return self._playback.get_event_summary()

    def load_events_from_positions_jsonl(self) -> bool:
        try:
            event_summary = self._events.load_events_from_event_list()
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

    def clear_timetravel_objects(self):
        self._stage_objects.clear_timetravel_objects()
        self._repository.clear()
        self._prim_map.clear()
        self._playback.configure_data_range(None, None)
        self._playback.set_event_summary([])

    def create_astronaut_prim(self, index: int) -> str:
        astronaut_usd = self._config.astronaut_usd if self._config else ""
        if not astronaut_usd:
            carb.log_error("[TimeTravel] astronaut_usd not specified in config")
            return ""
        return self._stage_objects.create_astronaut_prim(index, astronaut_usd)

    def auto_generate_astronauts(self) -> Dict[str, str]:
        if not self._config:
            carb.log_error("[TimeTravel] Config must be loaded before auto-generation")
            return {}

        if not self._config.astronaut_usd:
            self._config.astronaut_usd = DEFAULT_ASTRONAUT_USD
            carb.log_info(f"[TimeTravel] Using default astronaut USD: {DEFAULT_ASTRONAUT_USD}")

        csv_path = self._config.resolve_from_config(self._config.data_path)
        if not csv_path.exists():
            carb.log_error(f"[TimeTravel] Data file not found: {csv_path}")
            return {}

        objids = self.parse_unique_objids(str(csv_path))
        if not objids:
            carb.log_error("[TimeTravel] No objids found in CSV")
            return {}

        self.clear_timetravel_objects()

        prim_map = {}
        for i, objid in enumerate(objids, start=1):
            prim_path = self.create_astronaut_prim(i)
            if prim_path:
                prim_map[objid] = prim_path

        self.hide_all_cameras()
        self._prim_map = prim_map
        return prim_map

    def hide_all_cameras(self):
        self._stage_objects.hide_all_cameras()

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

    def _on_event_requested(self, timestamp: str):
        self._stage_objects.move_camera_to_event(self._events.get_event_position(timestamp))
