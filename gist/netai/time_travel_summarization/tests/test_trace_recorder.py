import csv
import datetime
import tempfile
import unittest
from pathlib import Path

from gist.netai.time_travel_summarization.physics import TraceRecorder


class _FakePrim:
    """USD prim test double; position extraction is monkeypatched in the test."""

    def __init__(self, path):
        self._path = path


class TraceRecorderTest(unittest.TestCase):
    def test_module_import(self):
        self.assertTrue(callable(TraceRecorder))

    def test_start_stop_writes_header_only_if_no_tick(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "trace.csv"
            rec = TraceRecorder(prim_map={}, output_path=out)

            rec.start()
            rec.stop()

            self.assertTrue(out.exists())
            with open(out) as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0], ["timestamp", "objid", "x", "y", "z"])
            self.assertEqual(len(rows), 1)

    def test_tick_writes_rows(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "trace.csv"
            prims = {"obj001": _FakePrim("/W/obj001"), "obj002": _FakePrim("/W/obj002")}
            rec = TraceRecorder(prim_map=prims, output_path=out, subsample_fps=1000)
            rec._world_position = staticmethod(lambda p: (1.0, 2.0, 3.0))

            rec.start()
            t0 = datetime.datetime(2025, 1, 1, 0, 0, 0)
            for i in range(3):
                rec.tick(t0 + datetime.timedelta(milliseconds=33 * i))
                rec._last_tick = None
            rec.stop()

            with open(out) as f:
                rows = list(csv.reader(f))
            self.assertEqual(len(rows), 1 + 3 * 2)
            self.assertEqual(rows[1][1], "obj001")
            self.assertEqual(rows[1][2], "1.000")

    def test_tick_skips_repeated_timestamp(self):
        """sim 클럭이 정지한 펌프 틱(같은 시각 재호출)은 중복 기록하지 않는다."""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "trace.csv"
            rec = TraceRecorder(prim_map={"obj001": _FakePrim("/W/obj001")},
                                output_path=out, subsample_fps=1000)
            rec._world_position = staticmethod(lambda p: (1.0, 2.0, 3.0))

            rec.start()
            same = datetime.datetime(2025, 1, 1, 0, 0, 0)
            for _ in range(3):        # 동일 시각 3회 → 1회만 기록
                rec.tick(same)
                rec._last_tick = None
            rec.tick(same + datetime.timedelta(milliseconds=33))  # 전진 → 기록
            rec.stop()

            with open(out) as f:
                rows = list(csv.reader(f))
            self.assertEqual(len(rows), 1 + 2)


if __name__ == "__main__":
    unittest.main()
