import datetime
import sys
import types


def _install_carb_stub():
    carb = types.ModuleType("carb")
    carb.log_info = lambda *args, **kwargs: None
    carb.log_warn = lambda *args, **kwargs: None
    carb.log_error = lambda *args, **kwargs: None
    sys.modules["carb"] = carb

    stage_module = types.ModuleType(
        "gist.netai.time_travel_summarization.playback.stage_object_controller"
    )
    stage_module.StageObjectController = object
    sys.modules[
        "gist.netai.time_travel_summarization.playback.stage_object_controller"
    ] = stage_module


_install_carb_stub()

from gist.netai.time_travel_summarization.app.facade import TimeTravelCore  # noqa: E402
from gist.netai.time_travel_summarization.playback.controller import PlaybackController  # noqa: E402
from gist.netai.time_travel_summarization.playback.trajectory_repository import (  # noqa: E402
    TrajectoryRepository,
)

D1 = datetime.datetime(2026, 1, 1)
D2 = datetime.datetime(2026, 1, 2)


def _two_segment_repo() -> TrajectoryRepository:
    """01-01 00:00~00:00:02 + 01-02 00:00~00:00:02, 사이 하루 공백."""
    repo = TrajectoryRepository()
    stamps = [D1, D1 + datetime.timedelta(seconds=1), D1 + datetime.timedelta(seconds=2),
              D2, D2 + datetime.timedelta(seconds=1), D2 + datetime.timedelta(seconds=2)]
    repo._timestamps = [repo.format_timestamp(t) for t in stamps]
    repo._data = {k: {"obj001": (0.0, 0.0, 0.0)} for k in repo._timestamps}
    repo._data_start_time = stamps[0]
    repo._data_end_time = stamps[-1]
    return repo


def test_next_and_prev_data_time():
    repo = _two_segment_repo()
    in_gap = D1 + datetime.timedelta(hours=1)
    assert repo.next_data_time(in_gap) == D2
    assert repo.prev_data_time(in_gap) == D1 + datetime.timedelta(seconds=2)
    # 데이터 위: 다음 = 자기 자신(공백 아님 판정의 근거)
    assert repo.next_data_time(D1) == D1
    # 경계 밖
    assert repo.next_data_time(D2 + datetime.timedelta(seconds=3)) is None
    assert repo.prev_data_time(D1 - datetime.timedelta(seconds=1)) is None


def _core_with(repo) -> TimeTravelCore:
    core = TimeTravelCore.__new__(TimeTravelCore)
    core._repository = repo
    core._playback = PlaybackController()
    core._playback.configure_data_range(repo._data_start_time, repo._data_end_time)
    core._gap_skip_s = 10.0
    core._prim_map = {}
    return core


def test_maybe_skip_gap_jumps_forward_over_day_gap():
    core = _core_with(_two_segment_repo())
    core._playback.set_playback_speed(1.0)
    t = D1 + datetime.timedelta(seconds=2.5)  # 세그먼트1 끝 직후 → 공백
    assert core._maybe_skip_gap(t) == D2


def test_maybe_skip_gap_no_jump_within_dense_data():
    core = _core_with(_two_segment_repo())
    t = D1 + datetime.timedelta(seconds=1)
    assert core._maybe_skip_gap(t) == t


def test_maybe_skip_gap_backward_jumps_to_segment_end():
    core = _core_with(_two_segment_repo())
    core._playback.set_playback_speed(-1.0)
    t = D2 - datetime.timedelta(hours=1)  # 역재생으로 공백 진입
    assert core._maybe_skip_gap(t) == D1 + datetime.timedelta(seconds=2)


def test_maybe_skip_gap_disabled_by_zero_threshold():
    core = _core_with(_two_segment_repo())
    core.set_gap_skip_threshold(0)
    t = D1 + datetime.timedelta(hours=1)
    assert core._maybe_skip_gap(t) == t


def test_maybe_skip_gap_tolerates_repo_without_api():
    bare = types.SimpleNamespace(  # next/prev_data_time 없는 저장소 → 그대로 통과
        _data_start_time=D1, _data_end_time=D2)
    core = _core_with(bare)
    t = D1
    assert core._maybe_skip_gap(t) == t
