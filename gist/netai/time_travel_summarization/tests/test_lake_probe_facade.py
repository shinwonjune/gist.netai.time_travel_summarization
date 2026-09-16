"""facade의 apply_ms 누적기(headless) — 프로브가 꺼져 있으면 무부하, 켜져 있으면 프레임 단위 합산·리셋.

Kit 없이 돈다: conftest.install_carb_stub + TimeTravelCore.__new__ (test_gap_skip 패턴).
"""
import datetime
import tempfile
import unittest
from pathlib import Path

from gist.netai.time_travel_summarization.tests.conftest import install_carb_stub

install_carb_stub(with_stage_object_controller=True)

from gist.netai.time_travel_summarization.app.facade import TimeTravelCore  # noqa: E402
from gist.netai.time_travel_summarization.app.lake_probe import LakeProbe  # noqa: E402
from gist.netai.time_travel_summarization.playback.controller import PlaybackController  # noqa: E402
from gist.netai.time_travel_summarization.playback.trajectory_repository import (  # noqa: E402
    TrajectoryRepository,
)

D1 = datetime.datetime(2026, 1, 1)


class _StageStub:
    def __init__(self):
        self.calls = 0

    def update_stage_objects(self, prim_map, data, visibility=None):
        self.calls += 1


def _core(probe):
    repo = TrajectoryRepository()
    stamps = [D1 + datetime.timedelta(seconds=i) for i in range(3)]
    repo._timestamps = [repo.format_timestamp(t) for t in stamps]
    repo._data = {k: {"obj001": (0.0, 0.0, 0.0)} for k in repo._timestamps}
    repo._data_start_time, repo._data_end_time = stamps[0], stamps[-1]
    core = TimeTravelCore.__new__(TimeTravelCore)
    core._repository = repo
    core._playback = PlaybackController()
    core._playback.configure_data_range(repo._data_start_time, repo._data_end_time)
    core._playback.set_current_time(stamps[0])
    core._gap_skip_s = 10.0
    core._prim_map = {"obj001": "/World/A", "obj002": "/World/B"}
    core._stage_objects = _StageStub()
    core._trace = None
    core._lake_probe = probe
    core._apply_ms_acc = 0.0
    return core


class ApplyMsAccumulatorTest(unittest.TestCase):
    def test_disabled_probe_accumulates_nothing(self):
        core = _core(None)
        core.update_stage_objects()
        core.update_stage_objects()
        self.assertEqual(core._apply_ms_acc, 0.0)
        self.assertEqual(core._stage_objects.calls, 2)

    def test_enabled_probe_sums_per_frame_and_resets(self):
        with tempfile.TemporaryDirectory() as d:
            probe = LakeProbe(out_dir=Path(d), max_frames=100)
            core = _core(probe)
            core.update_stage_objects()   # 슬라이더 콜백 경유 등 — 같은 프레임 안의 두 번째 적용
            core.update_stage_objects()
            self.assertGreater(core._apply_ms_acc, 0.0)
            core.update(0.0)              # 프레임 경계: 누적분을 record로 넘기고 0으로
            self.assertEqual(core._apply_ms_acc, 0.0)
            self.assertEqual(len(probe), 1)
            self.assertGreater(probe._apply_ms[0], 0.0)
            self.assertEqual(probe._n_objects, 2)
            core.update(0.0)              # 적용 없는 프레임은 0
            self.assertEqual(probe._apply_ms[1], 0.0)


if __name__ == "__main__":
    unittest.main()
