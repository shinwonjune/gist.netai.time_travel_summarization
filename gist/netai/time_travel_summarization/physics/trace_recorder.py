"""Stream astronaut world positions to a trajectory-compatible CSV file.

좌표 규약 (collider-trace-v1, 2026-08-13):
  수평 성분 = 충돌 프록시(`__phys_proxy__` 자식 프림) 월드 중심,
  수직 성분 = 객체 프림 피벗.
왜 수평만인가 — 재연(playback)이 trace의 (x,y,z)를 프림 translate에 그대로 넣으므로
수직까지 프록시 중심(피벗 위 ~46 유닛)으로 바꾸면 재연에서 객체가 떠 버린다. 객체들의
키가 같아 쌍별 거리는 수직 규약과 무관하고, 접촉거리(2r)는 수평 중심거리이므로 수평만
프록시 중심이면 "라벨과 같은 자"가 성립한다. 프록시가 아직 없으면(배회 시작 전) 피벗으로
폴백한다. 어느 소스로 기록됐는지는 job.log의 "[Trace] source=" 스탬프로 사후 판정한다.
"""

import csv
import datetime
from pathlib import Path
from typing import Optional

_PROXY_CHILD = "__phys_proxy__"   # physics/collision_proxy._PROXY_NAME과 일치해야 한다


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
        self._last_timestamp: Optional[str] = None
        self._active = False
        self._proxy_map: dict = {}          # objid -> proxy prim (해석 성공분만 캐시)
        self._up_idx: Optional[int] = None  # 스테이지 up 축 (0/1/2), 지연 해석
        self._stamp_logged = False

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
        self._last_timestamp = None
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
        # 같은 시각 재기록 방지 — headless 캡처의 렌더 대기 펌프 틱은 sim 클럭이
        # 정지한 채 들어오므로, 여과 없이 쓰면 동일 타임스탬프 행이 중복된다.
        if timestamp_str == self._last_timestamp:
            return
        self._last_timestamp = timestamp_str
        for objid, prim_or_path in self._prim_map.items():
            try:
                pos = self._record_position(objid, prim_or_path)
                if pos is None:
                    continue
                self._writer.writerow(
                    [timestamp_str, objid, f"{pos[0]:.3f}", f"{pos[1]:.3f}", f"{pos[2]:.3f}"]
                )
                self._row_count += 1
            except Exception:
                continue
        self._log_source_stamp()

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

    def _record_position(self, objid, prim_or_path):
        """기록 좌표: 수평 = 프록시 중심, 수직 = 피벗 (모듈 독스트링의 규약).

        프록시 해석 실패(테스트 더블·배회 시작 전·프록시 없음)는 피벗 폴백 —
        폴백 여부는 _log_source_stamp()가 집계해 로그로 남긴다.
        """
        pivot = self._world_position(prim_or_path)
        if pivot is None:
            return None
        proxy = self._resolve_proxy(objid, prim_or_path)
        if proxy is None:
            return pivot
        ppos = self._world_position(proxy)
        if ppos is None:
            return pivot
        up = self._stage_up_idx(prim_or_path)
        pos = list(ppos)
        pos[up] = pivot[up]
        return tuple(pos)

    def _resolve_proxy(self, objid, prim_or_path):
        """objid의 충돌 프록시 자식 프림. 성공분만 캐시(생성 전이면 매 틱 재시도)."""
        cached = self._proxy_map.get(objid)
        if cached is not None:
            return cached
        try:
            child = prim_or_path.GetChild(_PROXY_CHILD)
            if child and child.IsValid():
                self._proxy_map[objid] = child
                return child
        except Exception:
            pass
        return None

    def _stage_up_idx(self, prim_or_path) -> int:
        """스테이지 up 축 인덱스 (Y-up=1 / Z-up=2). 해석 실패 시 Y-up 가정."""
        if self._up_idx is None:
            try:
                from pxr import UsdGeom

                axis = UsdGeom.GetStageUpAxis(prim_or_path.GetStage())
                self._up_idx = 2 if str(axis) == "Z" else 1
            except Exception:
                self._up_idx = 1
        return self._up_idx

    def _log_source_stamp(self) -> None:
        """어떤 소스로 기록 중인지 1회 로그 — job.log 데이터 계보용 (프록시 해석 후)."""
        if self._stamp_logged or not self._proxy_map:
            return
        self._stamp_logged = True
        try:
            import carb

            carb.log_warn(
                f"[Trace] source=collider-center(horiz)+pivot(vert) regime=collider-trace-v1 "
                f"proxies={len(self._proxy_map)}/{len(self._prim_map)} child={_PROXY_CHILD}")
        except Exception:
            pass

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
