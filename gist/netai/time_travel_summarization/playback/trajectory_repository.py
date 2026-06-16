import bisect
import csv
import datetime
import io
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .lookup_benchmark import LkvCache, LkvForwardBisectHybrid


class TrajectoryRepository:
    def __init__(self):
        self.clear()

    def clear(self):
        self._data: Dict[str, Dict[str, Tuple[float, float, float]]] = {}
        self._timestamps: List[str] = []
        self._data_start_time: Optional[datetime.datetime] = None
        self._data_end_time: Optional[datetime.datetime] = None
        # lookup mode
        self._lookup_mode: str = "linear"
        self._hybrid = LkvForwardBisectHybrid()
        self._lkv_cache = LkvCache()
        # benchmark instrumentation
        self._bench_active: bool = False
        self._bench_call_count: int = 0
        self._bench_total_seconds: float = 0.0
        self._bench_pattern: str = ""

    def load_from_uri(self, uri: str) -> bool:
        """Load trajectory data from any URI scheme supported by storage.from_uri.

        Supports:
          - file:// or local path -> existing CSV behavior (.csv) or Parquet (.parquet)
          - s3://bucket/key       -> via MinioAdapter, read bytes then parse same as above

        Detection: by URI extension (case-insensitive).
        """
        self.clear()
        if not uri.lower().endswith((".csv", ".parquet")):
            return False

        self._data, self._timestamps = self._rows_to_data(self._read_rows(uri))
        if self._timestamps:
            self._data_start_time = self.parse_timestamp(self._timestamps[0])
            self._data_end_time = self.parse_timestamp(self._timestamps[-1])

        self._hybrid.reset()
        self._lkv_cache.reset()
        return bool(self._timestamps)

    def load_csv(self, csv_path: Path) -> bool:
        return self.load_from_uri(Path(csv_path).resolve().as_uri())

    @property
    def timestamps(self) -> List[str]:
        return self._timestamps

    @property
    def data_start_time(self) -> Optional[datetime.datetime]:
        return self._data_start_time

    @property
    def data_end_time(self) -> Optional[datetime.datetime]:
        return self._data_end_time

    def has_data(self) -> bool:
        return bool(self._timestamps)

    def get_coord_range(self) -> Optional[Tuple[Tuple[float, float, float], Tuple[float, float, float]]]:
        """모든 시점·모든 객체의 (x,y,z) min/max 반환. 데이터 없으면 None."""
        if not self._data:
            return None
        mins = [float("inf")] * 3
        maxs = [float("-inf")] * 3
        for ts_data in self._data.values():
            for xyz in ts_data.values():
                for i in range(3):
                    if xyz[i] < mins[i]:
                        mins[i] = xyz[i]
                    if xyz[i] > maxs[i]:
                        maxs[i] = xyz[i]
        return (tuple(mins), tuple(maxs))

    def set_lookup_mode(self, mode: str) -> None:
        valid = ("linear", "bisect", "hybrid", "lkv_cache")
        if mode not in valid:
            raise ValueError(f"mode must be one of {valid}, got {mode!r}")
        self._lookup_mode = mode
        self._hybrid.reset()
        self._lkv_cache.reset()

    def get_lookup_mode(self) -> str:
        return self._lookup_mode

    def get_data_at_time(self, timestamp: datetime.datetime) -> Dict[str, Tuple[float, float, float]]:
        if self._bench_active:
            t0 = time.perf_counter()
            result = self._do_lookup(timestamp)
            self._bench_total_seconds += time.perf_counter() - t0
            self._bench_call_count += 1
            return result
        return self._do_lookup(timestamp)

    def _do_lookup(self, timestamp: datetime.datetime) -> Dict[str, Tuple[float, float, float]]:
        normalized_time = timestamp.replace(microsecond=(timestamp.microsecond // 1000) * 1000)
        timestamp_str = self.format_timestamp(normalized_time)
        # stateful cache 알고리즘은 hit/miss를 직접 처리 (cache learning 필요)
        if self._lookup_mode in ("hybrid", "lkv_cache"):
            return self._lookup_via_algorithm(timestamp_str)
        # linear/bisect는 floor lookup만 책임. hit fast path는 여기서 처리.
        if timestamp_str in self._data:
            return self._data[timestamp_str]
        return self._get_last_known_value(timestamp_str)

    def _lookup_via_algorithm(self, timestamp_str: str) -> Dict[str, Tuple[float, float, float]]:
        if not self._timestamps:
            return {}
        if self._lookup_mode == "hybrid":
            ts = self._hybrid.query(self._timestamps, timestamp_str)
        elif self._lookup_mode == "lkv_cache":
            ts = self._lkv_cache.query(self._timestamps, timestamp_str)
        else:
            ts = None
        if ts is None:
            return self._data[self._timestamps[0]]
        return self._data[ts]

    def _get_last_known_value(self, timestamp_str: str) -> Dict[str, Tuple[float, float, float]]:
        """target ≤ timestamp_str인 최대 timestamp의 데이터 반환 (floor lookup)."""
        if not self._timestamps:
            return {}
        if self._lookup_mode == "bisect":
            return self._lookup_bisect(timestamp_str)
        return self._lookup_linear(timestamp_str)

    def _lookup_linear(self, timestamp_str: str) -> Dict[str, Tuple[float, float, float]]:
        """원본 선형 floor lookup. O(N), 정렬 가정 + 이른 break로 평균 O(N/2)."""
        previous_timestamp = None
        for current in self._timestamps:
            if current <= timestamp_str:
                previous_timestamp = current
            else:
                break
        if previous_timestamp:
            return self._data[previous_timestamp]
        return self._data[self._timestamps[0]]

    def _lookup_bisect(self, timestamp_str: str) -> Dict[str, Tuple[float, float, float]]:
        idx = bisect.bisect_right(self._timestamps, timestamp_str) - 1
        if idx < 0:
            return self._data[self._timestamps[0]]
        return self._data[self._timestamps[idx]]

    def start_benchmark(self, pattern: str) -> None:
        self._bench_active = True
        self._bench_call_count = 0
        self._bench_total_seconds = 0.0
        self._bench_pattern = pattern

    def stop_benchmark(self) -> dict:
        self._bench_active = False
        n = self._bench_call_count
        return {
            "mode": self._lookup_mode,
            "pattern": self._bench_pattern,
            "call_count": n,
            "total_seconds": self._bench_total_seconds,
            "per_call_us": (self._bench_total_seconds / n * 1e6) if n > 0 else 0.0,
        }

    @staticmethod
    def parse_timestamp(timestamp_str: str) -> datetime.datetime:
        candidate = (timestamp_str or "").strip().replace("Z", "+00:00")
        for parser in (
            datetime.datetime.fromisoformat,
            lambda value: datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f"),
            lambda value: datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S"),
        ):
            try:
                return parser(candidate)
            except ValueError:
                continue
        raise ValueError(f"Unsupported timestamp format: {timestamp_str!r}")

    @staticmethod
    def format_timestamp(dt: datetime.datetime) -> str:
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    @staticmethod
    def _read_rows(uri: str) -> List[dict]:
        """URI(.csv|.parquet)를 읽어 dict row 리스트로 반환. storage.from_uri로 백엔드 자동 선택."""
        from ..storage import from_uri

        adapter = from_uri(uri)
        with adapter.open_read(uri) as stream:
            raw = stream.read()
        if uri.lower().endswith(".csv"):
            return list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
        import pyarrow.parquet as pq  # parquet일 때만 필요 (선택 의존성)

        return pq.read_table(io.BytesIO(raw)).to_pylist()

    @staticmethod
    def _rows_to_data(
        rows: List[dict],
    ) -> Tuple[Dict[str, Dict[str, Tuple[float, float, float]]], List[str]]:
        """row 리스트 -> ({ts: {objid:(x,y,z)}}, 정렬된 ts 리스트). bisect 위해 정렬 보장."""
        data: Dict[str, Dict[str, Tuple[float, float, float]]] = {}
        for row in rows:
            data.setdefault(row["timestamp"], {})[row["objid"]] = (
                float(row["x"]),
                float(row["y"]),
                float(row["z"]),
            )
        return data, sorted(data.keys())

    @staticmethod
    def parse_unique_objids(csv_path: str) -> List[str]:
        objids = set()
        with open(csv_path, "r", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            for row in reader:
                objid = row.get("objid")
                if objid:
                    objids.add(objid)
        return sorted(objids)

    @staticmethod
    def parse_unique_objids_from_uri(uri: str) -> List[str]:
        from ..storage import from_uri

        objids = set()
        adapter = from_uri(uri)
        with adapter.open_read(uri) as stream:
            raw = stream.read()
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8")))
        for row in reader:
            objid = row.get("objid")
            if objid:
                objids.add(objid)
        return sorted(objids)
