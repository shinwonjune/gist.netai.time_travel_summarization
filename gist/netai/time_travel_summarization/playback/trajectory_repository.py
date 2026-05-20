import csv
import datetime
import io
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class TrajectoryRepository:
    def __init__(self):
        self.clear()

    def clear(self):
        self._data: Dict[str, Dict[str, Tuple[float, float, float]]] = {}
        self._timestamps: List[str] = []
        self._data_start_time: Optional[datetime.datetime] = None
        self._data_end_time: Optional[datetime.datetime] = None

    def load_from_uri(self, uri: str) -> bool:
        """Load trajectory data from any URI scheme supported by storage.from_uri.

        Supports:
          - file:// or local path -> existing CSV behavior (.csv) or Parquet (.parquet)
          - s3://bucket/key       -> via MinioAdapter, read bytes then parse same as above

        Detection: by URI extension (case-insensitive).
        """
        from ..storage import from_uri

        self.clear()
        lower_uri = uri.lower()
        if not lower_uri.endswith((".csv", ".parquet")):
            return False

        adapter = from_uri(uri)
        with adapter.open_read(uri) as stream:
            if lower_uri.endswith(".csv"):
                text = stream.read().decode("utf-8")
                reader = csv.DictReader(io.StringIO(text))
                rows = reader
            else:
                import pyarrow.parquet as pq

                table = pq.read_table(io.BytesIO(stream.read()))
                rows = table.to_pylist()

        for row in rows:
            timestamp = row["timestamp"]
            self._data.setdefault(timestamp, {})[row["objid"]] = (
                float(row["x"]),
                float(row["y"]),
                float(row["z"]),
            )

        self._timestamps = list(self._data.keys())
        if self._timestamps:
            self._data_start_time = self.parse_timestamp(self._timestamps[0])
            self._data_end_time = self.parse_timestamp(self._timestamps[-1])

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

    def get_data_at_time(self, timestamp: datetime.datetime) -> Dict[str, Tuple[float, float, float]]:
        normalized_time = timestamp.replace(microsecond=(timestamp.microsecond // 1000) * 1000)
        timestamp_str = self.format_timestamp(normalized_time)
        if timestamp_str in self._data:
            return self._data[timestamp_str]
        return self._get_last_known_value(timestamp_str)

    def _get_last_known_value(self, timestamp_str: str) -> Dict[str, Tuple[float, float, float]]:
        if not self._timestamps:
            return {}

        previous_timestamp = None
        for current in self._timestamps:
            if current <= timestamp_str:
                previous_timestamp = current
            else:
                break

        if previous_timestamp:
            return self._data[previous_timestamp]

        return self._data[self._timestamps[0]]

    @staticmethod
    def parse_timestamp(timestamp_str: str) -> datetime.datetime:
        try:
            return datetime.datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        except ValueError:
            return datetime.datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S.%f")

    @staticmethod
    def format_timestamp(dt: datetime.datetime) -> str:
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

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
