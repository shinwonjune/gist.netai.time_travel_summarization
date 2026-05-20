import tempfile
import unittest
from pathlib import Path

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    pa = None
    pq = None

from gist.netai.time_travel_summarization.app.config import ExtensionConfig
from gist.netai.time_travel_summarization.playback.trajectory_repository import TrajectoryRepository


class RepositoryUriTest(unittest.TestCase):
    @property
    def _package_dir(self) -> Path:
        return Path(__file__).resolve().parents[1]

    @property
    def _csv_path(self) -> Path:
        return self._package_dir / "data" / "living_trajectory_1min_0.2s.csv"

    def test_load_csv_path_still_works(self):
        repository = TrajectoryRepository()

        self.assertTrue(repository.load_csv(self._csv_path))
        self.assertTrue(repository.timestamps)

    def test_load_from_uri_file_csv(self):
        repository = TrajectoryRepository()

        self.assertTrue(repository.load_from_uri(self._csv_path.resolve().as_uri()))
        self.assertTrue(repository.timestamps)

    @unittest.skipIf(pa is None or pq is None, "pyarrow is not installed")
    def test_load_from_uri_parquet(self):
        repository = TrajectoryRepository()
        rows = [
            {
                "timestamp": "2025-01-01 00:00:00.000",
                "objid": "obj001",
                "x": 1.0,
                "y": 2.0,
                "z": 3.0,
            },
            {
                "timestamp": "2025-01-01 00:00:00.200",
                "objid": "obj001",
                "x": 4.0,
                "y": 5.0,
                "z": 6.0,
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            parquet_path = Path(tmpdir) / "trajectory.parquet"
            pq.write_table(pa.Table.from_pylist(rows), parquet_path)

            self.assertTrue(repository.load_from_uri(parquet_path.resolve().as_uri()))

        self.assertEqual(repository.timestamps, [row["timestamp"] for row in rows])
        self.assertEqual(
            repository.get_data_at_time(repository.parse_timestamp(rows[-1]["timestamp"]))["obj001"],
            (4.0, 5.0, 6.0),
        )

    def test_load_from_uri_unsupported_extension(self):
        repository = TrajectoryRepository()

        self.assertFalse(repository.load_from_uri("file:///nonexistent/foo.txt"))

    def test_extension_config_data_uri_local(self):
        config = ExtensionConfig.from_file(str(self._package_dir / "config.json"))

        self.assertTrue(config.data_uri.startswith("file://"))
        self.assertTrue(config.data_uri.endswith(".csv"))


if __name__ == "__main__":
    unittest.main()
