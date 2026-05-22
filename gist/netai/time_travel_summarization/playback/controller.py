import datetime
from typing import Callable, List, Optional


class PlaybackController:
    def __init__(self):
        self._range_start_time: Optional[datetime.datetime] = None
        self._range_end_time: Optional[datetime.datetime] = None
        self._current_time: Optional[datetime.datetime] = None

        self._is_playing = False
        self._playback_speed = 1.0
        self._accumulated_time = 0.0

        self._use_event_summary = False
        self._event_summary: List[str] = []
        self._current_event_index = 0
        self._event_playback_start_time: Optional[datetime.datetime] = None
        self._event_playback_duration = 1.0
        self._mode = "playback"

    def configure_data_range(
        self,
        start_time: Optional[datetime.datetime],
        end_time: Optional[datetime.datetime],
    ):
        self._range_start_time = start_time
        self._range_end_time = end_time
        self._current_time = start_time
        self._current_event_index = 0
        self._event_playback_start_time = None
        self._accumulated_time = 0.0
        self._is_playing = False

    def set_time_range(self, start_time: datetime.datetime, end_time: datetime.datetime) -> bool:
        if not self._range_start_time or not self._range_end_time or end_time <= start_time:
            return False

        self._range_start_time = max(start_time, self._range_start_time)
        self._range_end_time = min(end_time, self._range_end_time)
        if self._current_time is None:
            self._current_time = self._range_start_time
        else:
            self._current_time = max(self._range_start_time, min(self._current_time, self._range_end_time))
        return True

    def set_current_time(self, dt: datetime.datetime):
        if self._range_start_time and self._range_end_time:
            self._current_time = max(self._range_start_time, min(dt, self._range_end_time))

    def set_progress(self, progress: float):
        if not self._range_start_time or not self._range_end_time:
            return

        clamped = min(1.0, max(0.0, progress))
        duration = (self._range_end_time - self._range_start_time).total_seconds()
        self._current_time = self._range_start_time + datetime.timedelta(seconds=duration * clamped)

    def get_progress(self) -> float:
        if not self._range_start_time or not self._range_end_time or not self._current_time:
            return 0.0

        duration = (self._range_end_time - self._range_start_time).total_seconds()
        if duration <= 0:
            return 0.0

        current = (self._current_time - self._range_start_time).total_seconds()
        return min(1.0, max(0.0, current / duration))

    def toggle_playback(self):
        if self._mode != "playback":
            return
        self._is_playing = not self._is_playing
        self._accumulated_time = 0.0
        if self._is_playing and self._use_event_summary:
            self._event_playback_start_time = None

    def set_mode(self, mode: str) -> None:
        if mode not in ("playback", "physics"):
            raise ValueError("mode must be 'playback' or 'physics'")
        self._mode = mode
        if mode == "physics":
            self._is_playing = False
            self._accumulated_time = 0.0

    def get_mode(self) -> str:
        return self._mode

    def update(
        self,
        dt: float,
        parse_timestamp: Callable[[str], datetime.datetime],
        on_time_changed: Callable[[datetime.datetime], None],
        on_event_requested: Callable[[str], None],
    ):
        if self._mode != "playback":
            return
        if not self._is_playing or not self._current_time:
            return

        self._accumulated_time += dt * self._playback_speed
        if abs(self._accumulated_time) < 0.1:
            return

        seconds_to_add = self._accumulated_time
        self._accumulated_time = 0.0

        if self._use_event_summary and self._event_summary:
            self._update_event_playback(seconds_to_add, parse_timestamp, on_time_changed, on_event_requested)
            return

        self._advance_time(seconds_to_add, on_time_changed)

    def _advance_time(self, seconds_to_add: float, on_time_changed: Callable[[datetime.datetime], None]):
        if not self._current_time or not self._range_end_time or not self._range_start_time:
            return

        next_time = self._current_time + datetime.timedelta(seconds=seconds_to_add)
        if seconds_to_add > 0 and next_time >= self._range_end_time:
            next_time = self._range_end_time
            self._is_playing = False
        elif seconds_to_add < 0 and next_time <= self._range_start_time:
            next_time = self._range_start_time
            self._is_playing = False

        self._current_time = next_time
        on_time_changed(self._current_time)

    def _update_event_playback(
        self,
        dt: float,
        parse_timestamp: Callable[[str], datetime.datetime],
        on_time_changed: Callable[[datetime.datetime], None],
        on_event_requested: Callable[[str], None],
    ):
        if self._event_playback_start_time is None:
            self.go_to_current_event(parse_timestamp, on_time_changed, on_event_requested)
            self._event_playback_start_time = self._current_time
            return

        elapsed = (self._current_time - self._event_playback_start_time).total_seconds()
        if elapsed >= self._event_playback_duration:
            self._current_event_index = (self._current_event_index + 1) % len(self._event_summary)
            if self._current_event_index == 0:
                self._is_playing = False
                return

            self.go_to_current_event(parse_timestamp, on_time_changed, on_event_requested)
            self._event_playback_start_time = self._current_time
            return

        self._advance_time(dt, on_time_changed)

    def go_to_current_event(
        self,
        parse_timestamp: Callable[[str], datetime.datetime],
        on_time_changed: Callable[[datetime.datetime], None],
        on_event_requested: Callable[[str], None],
    ):
        if not self._event_summary:
            return

        event_timestamp = self._event_summary[self._current_event_index]
        self.set_current_time(parse_timestamp(event_timestamp))
        if self._current_time:
            on_time_changed(self._current_time)
        on_event_requested(event_timestamp)

    def go_to_next_event(
        self,
        parse_timestamp: Callable[[str], datetime.datetime],
        on_time_changed: Callable[[datetime.datetime], None],
        on_event_requested: Callable[[str], None],
    ):
        if not self._event_summary:
            return

        self._current_event_index = (self._current_event_index + 1) % len(self._event_summary)
        self.go_to_current_event(parse_timestamp, on_time_changed, on_event_requested)
        self._event_playback_start_time = None

    def set_event_summary(self, event_summary: List[str]):
        self._event_summary = list(event_summary)
        self._current_event_index = 0
        self._event_playback_start_time = None

    def get_event_summary(self) -> List[str]:
        return list(self._event_summary)

    def set_use_event_summary(self, use: bool):
        self._use_event_summary = use
        self._current_event_index = 0
        self._event_playback_start_time = None

    def get_start_time(self) -> Optional[datetime.datetime]:
        return self._range_start_time

    def get_end_time(self) -> Optional[datetime.datetime]:
        return self._range_end_time

    def get_current_time(self) -> Optional[datetime.datetime]:
        return self._current_time

    def is_playing(self) -> bool:
        return self._mode == "playback" and self._is_playing

    def get_playback_speed(self) -> float:
        return self._playback_speed

    def set_playback_speed(self, speed: float):
        """음수 허용 → backward 재생. 0은 차단(정지는 toggle_playback 사용)."""
        if -0.001 < speed < 0.001:
            speed = 1.0  # 0 차단
        self._playback_speed = max(-10.0, min(10.0, speed))
