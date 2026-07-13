"""시간대 윈도우 + 백그라운드 프리페치 기반 궤적 레포지토리.

minIO(또는 file://)의 시간 분할 청크를 manifest로 인덱싱하고, 재생 헤드 주변
청크만 메모리에 올린다. 청크 경계를 넘기 전에 다음 청크를 백그라운드로 미리
디코드해 두어 '딜레이 없이' 재생되게 한다.

TrajectoryRepository를 상속해 floor-lookup(linear/bisect/hybrid/lkv), 벤치마크
계측, parse/format 등을 그대로 재사용한다. 차이점은 self._data/_timestamps가
"현재 활성 청크" 한 개만 담는다는 점이다(전체 데이터가 아님).
"""

from __future__ import annotations

import bisect
import datetime
import json
import queue
import threading
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

try:
    import carb
except ImportError:  # pragma: no cover - used by headless tests outside Kit
    class _CarbFallback:
        @staticmethod
        def log_warn(*_args, **_kwargs):
            pass

    carb = _CarbFallback()

from .lake_common import MANIFEST_NAME, dataset_uri_from_manifest, join_uri, manifest_uri
from .trajectory_repository import TrajectoryRepository

_Chunk = Tuple[Dict[str, Dict[str, Tuple[float, float, float]]], List[str]]


class LakeTrajectoryRepository(TrajectoryRepository):
    def __init__(self, cache_chunks: int = 4, prefetch_ahead: int = 1):
        self._prefetch_ahead = max(0, int(prefetch_ahead))
        # 프리페치가 켜져 있으면 활성 청크 + 선로드분 + LRU 여유를 보장해야
        # 방금 프리페치한 청크가 곧바로 evict되지 않는다.
        min_cache = self._prefetch_ahead + 2 if self._prefetch_ahead > 0 else 1
        self._cache_chunks = max(int(cache_chunks), min_cache)
        super().__init__()  # -> self.clear() -> _reset_lake_state()

    # ---- lifecycle ----

    def clear(self):
        super().clear()
        self._reset_lake_state()

    def _reset_lake_state(self):
        self._stop_prefetch()
        self._dataset_uri: Optional[str] = None
        self._ext: str = "csv"
        self._chunk_seconds: Optional[int] = None
        self._chunks: List[dict] = []
        self._chunk_starts: List[datetime.datetime] = []
        self._objids: List[str] = []
        self._total_rows: int = 0
        self._coord_min = None
        self._coord_max = None
        self._active_idx: int = -1
        self._cache: "OrderedDict[int, _Chunk]" = OrderedDict()
        self._cache_lock = threading.RLock()
        # prefetch worker
        self._pf_queue: Optional[queue.Queue] = None
        self._pf_thread: Optional[threading.Thread] = None
        self._pf_inflight: set = set()
        self._pf_stop: Optional[threading.Event] = None
        self.stats = {
            "chunk_loads": 0, "sync_loads": 0, "prefetch_loads": 0,
            "cache_hits": 0, "cache_misses": 0,
        }

    def load_from_uri(self, uri: str) -> bool:
        """manifest URI(또는 dataset 디렉터리 URI)를 받아 인덱스만 메모리에 올린다."""
        from ..storage import from_uri

        self.clear()
        muri = uri if uri.lower().endswith(MANIFEST_NAME) else manifest_uri(uri)
        self._dataset_uri = dataset_uri_from_manifest(muri)
        adapter = from_uri(muri)
        with adapter.open_read(muri) as stream:
            manifest = json.loads(stream.read().decode("utf-8"))
        self._load_manifest(manifest)
        carb.log_warn(
            f"[TimeTravel] Lake manifest loaded: "
            f"format={self._ext}, rows={self._total_rows}, chunks={len(self._chunks)}, "
            f"objects={len(self._objids)}, range={self._data_start_time}..{self._data_end_time}"
        )
        if not self._chunks:
            return False
        self._start_prefetch()
        self._activate(0)  # 첫 프레임 즉시 가능하도록 첫 청크 동기 로드
        return True

    def _load_manifest(self, m: dict):
        self._ext = m.get("format", "csv")
        self._chunk_seconds = m.get("chunk_seconds")
        self._objids = list(m.get("objids", []))
        self._total_rows = int(m.get("rows", 0))
        self._coord_min = m.get("coord_min")
        self._coord_max = m.get("coord_max")
        self._chunks = list(m.get("chunks", []))
        self._chunk_starts = [self.parse_timestamp(c["start"]) for c in self._chunks]
        self._data_start_time = self.parse_timestamp(m["start"]) if m.get("start") else None
        self._data_end_time = self.parse_timestamp(m["end"]) if m.get("end") else None

    # ---- query path (called on main/update thread) ----

    def _do_lookup(self, timestamp: datetime.datetime) -> Dict[str, Tuple[float, float, float]]:
        if not self._chunks:
            return {}
        idx = self._chunk_for_time(timestamp)
        if idx != self._active_idx:
            self._activate(idx)
        self._schedule_prefetch(idx)
        return super()._do_lookup(timestamp)

    def next_data_time(self, timestamp):
        """공백 탐지(청크 해상도): manifest의 청크 [start,end] 커버리지 안이면
        timestamp 그대로(공백 아님), 밖이면 다음 청크 시작. 탐색은 메모리의
        chunk_starts 이진 탐색 — minIO 조회 없음. 청크 내부의 미세 공백은
        작성기가 연속 데이터만 청크화하므로 무시해도 안전."""
        if not self._chunks:
            return None
        idx = bisect.bisect_right(self._chunk_starts, timestamp) - 1
        if idx >= 0:
            end = self.parse_timestamp(self._chunks[idx]["end"])
            if timestamp <= end:
                return timestamp
        nxt = idx + 1
        if nxt < len(self._chunk_starts):
            return self._chunk_starts[nxt]
        return None

    def prev_data_time(self, timestamp):
        """역재생용: 커버리지 안이면 timestamp, 공백이면 직전 청크의 end."""
        if not self._chunks:
            return None
        idx = bisect.bisect_right(self._chunk_starts, timestamp) - 1
        if idx < 0:
            return None
        end = self.parse_timestamp(self._chunks[idx]["end"])
        return timestamp if timestamp <= end else end

    def _chunk_for_time(self, timestamp: datetime.datetime) -> int:
        idx = bisect.bisect_right(self._chunk_starts, timestamp) - 1
        if idx < 0:
            return 0
        if idx >= len(self._chunks):
            return len(self._chunks) - 1
        return idx

    def _activate(self, idx: int):
        # 로드 전에 active로 표시 → 동기 로드 중 prefetch 워커가 이 청크를 evict하지 못함
        self._active_idx = idx
        data, ts = self._ensure_loaded(idx)
        self._data = data
        self._timestamps = ts
        # 활성 청크의 timestamp 집합이 바뀌었으므로 stateful 캐시 무효화
        self._hybrid.reset()
        self._lkv_cache.reset()

    def _ensure_loaded(self, idx: int) -> _Chunk:
        with self._cache_lock:
            if idx in self._cache:
                self._cache.move_to_end(idx)
                self.stats["cache_hits"] += 1
                return self._cache[idx]
            self.stats["cache_misses"] += 1
        chunk = self._load_chunk(idx)  # 동기 로드 (캐시 미스 = stall 비용)
        with self._cache_lock:
            self.stats["sync_loads"] += 1
            self.stats["chunk_loads"] += 1
        self._cache_put(idx, chunk)
        return chunk

    def _load_chunk(self, idx: int) -> _Chunk:
        ch = self._chunks[idx]
        uri = join_uri(self._dataset_uri, ch["key"])
        carb.log_warn(f"[TimeTravel] Lake chunk load start: idx={idx} uri={uri} rows={ch.get('rows')}")
        chunk = TrajectoryRepository._rows_to_data(TrajectoryRepository._read_rows(uri))
        carb.log_warn(
            f"[TimeTravel] Lake chunk load done: idx={idx} timestamps={len(chunk[1])}"
        )
        return chunk

    def _cache_put(self, idx: int, chunk: _Chunk):
        with self._cache_lock:
            self._cache[idx] = chunk
            self._cache.move_to_end(idx)
            while len(self._cache) > self._cache_chunks:
                evicted = False
                for k in list(self._cache.keys()):
                    if k != self._active_idx:
                        del self._cache[k]
                        evicted = True
                        break
                if not evicted:  # 활성 청크만 남은 경우
                    break

    # ---- background prefetch ----

    def _start_prefetch(self):
        self._pf_stop = threading.Event()
        self._pf_queue = queue.Queue()
        self._pf_inflight = set()
        self._pf_thread = threading.Thread(target=self._pf_loop, name="lake-prefetch", daemon=True)
        self._pf_thread.start()

    def _stop_prefetch(self):
        stop = getattr(self, "_pf_stop", None)
        if stop is not None:
            stop.set()
            if self._pf_queue is not None:
                self._pf_queue.put(None)  # sentinel
            if self._pf_thread is not None:
                self._pf_thread.join(timeout=1.0)
        self._pf_stop = None
        self._pf_queue = None
        self._pf_thread = None

    def _pf_loop(self):
        while not self._pf_stop.is_set():
            try:
                idx = self._pf_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if idx is None:
                break
            try:
                if 0 <= idx < len(self._chunks):
                    with self._cache_lock:
                        present = idx in self._cache
                    if not present:
                        chunk = self._load_chunk(idx)
                        with self._cache_lock:
                            self.stats["prefetch_loads"] += 1
                            self.stats["chunk_loads"] += 1
                        self._cache_put(idx, chunk)
            finally:
                with self._cache_lock:
                    self._pf_inflight.discard(idx)

    def _schedule_prefetch(self, active_idx: int):
        if not self._pf_queue:
            return
        targets = [active_idx + d for d in range(1, self._prefetch_ahead + 1)]
        targets.append(active_idx - 1)  # 역방향 재생 대비
        for t in targets:
            if not (0 <= t < len(self._chunks)):
                continue
            with self._cache_lock:
                if t in self._cache or t in self._pf_inflight:
                    continue
                self._pf_inflight.add(t)
            self._pf_queue.put(t)

    # ---- overrides for full-dataset metadata ----

    def has_data(self) -> bool:
        return bool(self._chunks)

    @property
    def total_rows(self) -> int:
        return self._total_rows

    def get_object_ids(self) -> List[str]:
        return list(self._objids)

    def get_coord_range(self):
        if self._coord_min is not None and self._coord_max is not None:
            return (tuple(self._coord_min), tuple(self._coord_max))
        return super().get_coord_range()
