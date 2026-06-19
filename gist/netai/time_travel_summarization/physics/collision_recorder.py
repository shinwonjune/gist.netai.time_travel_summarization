"""Record physics collision events as ground-truth labels.

Schema mirrors the trajectory CSV plus a ``kind`` column so the collisions can
be cross-referenced with trajectory rows and used to label training videos.
"""

import csv
import datetime
from pathlib import Path
from typing import Optional


class CollisionRecorder:
    """Append collision events (timestamp, objid, position, kind) to a CSV."""

    def __init__(self, output_path, prim_to_objid: dict):
        self._output_path = Path(output_path)
        self._prim_to_objid = dict(prim_to_objid)
        self._file = None
        self._writer: Optional[csv.writer] = None
        self._row_count = 0

    @property
    def output_path(self) -> Path:
        return self._output_path

    @property
    def row_count(self) -> int:
        return self._row_count

    def start(self) -> None:
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._output_path, "w", encoding="utf-8", newline="")
        self._writer = csv.writer(self._file)
        self._writer.writerow(["timestamp", "objid", "x", "y", "z", "kind"])
        self._row_count = 0

    def record(self, prim_path: str, position, kind: str) -> None:
        """Write one collision event. Safe to call before start() (no-op)."""
        if self._writer is None:
            return
        objid = self._prim_to_objid.get(prim_path, prim_path)
        # 오버레이와 동일 형식(timefmt.PRECISION) → 추론(오버레이 읽기)↔라벨(CSV) 정합.
        from ..timefmt import format_event_time
        timestamp = format_event_time(datetime.datetime.now())
        x, y, z = position if position is not None else (0.0, 0.0, 0.0)
        self._writer.writerow([timestamp, objid, f"{x:.3f}", f"{y:.3f}", f"{z:.3f}", kind])
        self._row_count += 1
        if self._file:
            self._file.flush()

    def stop(self) -> Path:
        if self._file:
            self._file.flush()
            self._file.close()
            self._file = None
        self._writer = None
        return self._output_path
