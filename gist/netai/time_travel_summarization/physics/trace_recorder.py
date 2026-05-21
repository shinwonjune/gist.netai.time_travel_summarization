"""Stream astronaut world positions to a trajectory-compatible CSV file."""

import csv
import datetime
from pathlib import Path
from typing import Optional


class TraceRecorder:
    """Record world positions for rigid body prims using the trajectory CSV schema."""

    def __init__(self, prim_map: dict, output_path: Path, subsample_fps: int = 30):
        self._prim_map = prim_map
        self._output_path = Path(output_path)
        self._subsample_dt = 1.0 / max(subsample_fps, 1)
        self._file = None
        self._writer: Optional[csv.writer] = None
        self._row_count = 0
        self._last_tick = None
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def output_path(self) -> Path:
        return self._output_path

    @property
    def row_count(self) -> int:
        return self._row_count

    def start(self) -> None:
        if self._active:
            return
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._output_path, "w", encoding="utf-8", newline="")
        self._writer = csv.writer(self._file)
        self._writer.writerow(["timestamp", "objid", "x", "y", "z"])
        self._active = True
        self._last_tick = None
        self._row_count = 0

    def tick(self, now_dt: Optional[datetime.datetime] = None) -> None:
        """Record one subsampled frame when the recorder is active."""
        if not self._active:
            return

        import time as _time

        now = _time.monotonic()
        if self._last_tick is not None and (now - self._last_tick) < self._subsample_dt:
            return
        self._last_tick = now

        timestamp_str = self._format_timestamp(now_dt or datetime.datetime.now())
        for objid, prim_or_path in self._prim_map.items():
            try:
                pos = self._world_position(prim_or_path)
                if pos is None:
                    continue
                self._writer.writerow(
                    [timestamp_str, objid, f"{pos[0]:.3f}", f"{pos[1]:.3f}", f"{pos[2]:.3f}"]
                )
                self._row_count += 1
            except Exception:
                continue

        if self._file and self._row_count % 100 == 0:
            self._file.flush()

    def stop(self) -> Path:
        if not self._active:
            return self._output_path
        if self._file:
            self._file.flush()
            self._file.close()
            self._file = None
        self._active = False
        return self._output_path

    @staticmethod
    def _format_timestamp(dt: datetime.datetime) -> str:
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    @staticmethod
    def _world_position(prim_or_path):
        """Return the USD prim world translation, or None when it cannot be read."""
        try:
            from pxr import UsdGeom

            xform_cache = UsdGeom.XformCache(0)
            world_xform = xform_cache.GetLocalToWorldTransform(prim_or_path)
            translation = world_xform.ExtractTranslation()
            return (float(translation[0]), float(translation[1]), float(translation[2]))
        except Exception:
            return None
