import datetime

from gist.netai.time_travel_summarization.playback.trajectory_repository import (
    TrajectoryRepository,
)
from gist.netai.time_travel_summarization.playback.visibility import (
    DEFAULT_VISIBILITY_TOL_S,
    compute_object_visibility,
    is_track_visible,
)

T0 = datetime.datetime(2026, 1, 1, 0, 0, 0)


def at(seconds: float) -> datetime.datetime:
    return T0 + datetime.timedelta(seconds=seconds)


# ---- 순수 판정: is_track_visible ------------------------------------------


def test_default_tol_is_one_second():
    assert DEFAULT_VISIBILITY_TOL_S == 1.0


def test_visible_within_span():
    assert is_track_visible(at(5), at(0), at(10))


def test_visible_at_exact_boundaries():
    assert is_track_visible(at(0), at(0), at(10), tol_s=0.0)
    assert is_track_visible(at(10), at(0), at(10), tol_s=0.0)


def test_hidden_after_last_beyond_tol():
    assert not is_track_visible(at(11.5), at(0), at(10), tol_s=1.0)


def test_visible_within_tol_after_last():
    # 1Hz 다운샘플 샘플 간격(1s)은 종료로 오검하지 않는다.
    assert is_track_visible(at(10.5), at(0), at(10), tol_s=1.0)
    assert is_track_visible(at(11.0), at(0), at(10), tol_s=1.0)  # 경계 포함


def test_hidden_before_first_beyond_tol():
    assert not is_track_visible(at(-2), at(0), at(10), tol_s=1.0)


def test_visible_within_tol_before_first():
    assert is_track_visible(at(-0.5), at(0), at(10), tol_s=1.0)
    assert is_track_visible(at(-1.0), at(0), at(10), tol_s=1.0)  # 경계 포함


# ---- 배치 판정: compute_object_visibility ---------------------------------


def test_compute_batch_before_after_and_live():
    ranges = {
        "obj001": (at(0), at(10)),   # 살아있음
        "obj002": (at(20), at(30)),  # 아직 등장 전
    }
    vis = compute_object_visibility(at(12), ranges, tol_s=1.0)
    assert vis == {"obj001": False, "obj002": False}  # 하나는 종료 후, 하나는 시작 전

    vis2 = compute_object_visibility(at(5), ranges, tol_s=1.0)
    assert vis2 == {"obj001": True, "obj002": False}

    vis3 = compute_object_visibility(at(25), ranges, tol_s=1.0)
    assert vis3 == {"obj001": False, "obj002": True}


def test_compute_batch_empty_ranges():
    assert compute_object_visibility(at(0), {}) == {}


# ---- 저장소 범위 계산: get_object_time_ranges ------------------------------


def _repo_with_dead_track() -> TrajectoryRepository:
    """obj001은 0..3초 생존, obj002는 0..1초 후 죽음(track fragmentation)."""
    repo = TrajectoryRepository()
    stamps = [repo.format_timestamp(at(i)) for i in range(4)]
    repo._timestamps = stamps
    repo._data = {
        stamps[0]: {"obj001": (0.0, 0.0, 0.0), "obj002": (1.0, 1.0, 1.0)},
        stamps[1]: {"obj001": (0.0, 0.0, 0.0), "obj002": (1.0, 1.0, 1.0)},
        stamps[2]: {"obj001": (0.0, 0.0, 0.0)},
        stamps[3]: {"obj001": (0.0, 0.0, 0.0)},
    }
    repo._data_start_time = at(0)
    repo._data_end_time = at(3)
    return repo


def test_repository_ranges_capture_dead_track():
    repo = _repo_with_dead_track()
    ranges = repo.get_object_time_ranges()
    assert ranges["obj001"] == (at(0), at(3))
    assert ranges["obj002"] == (at(0), at(1))


def test_repository_ranges_cached_until_clear():
    repo = _repo_with_dead_track()
    first = repo.get_object_time_ranges()
    assert repo.get_object_time_ranges() is first  # 캐시 재사용
    repo.clear()
    assert repo.get_object_time_ranges() == {}


def test_dead_track_hidden_after_its_last_sample():
    """죽은 트랙(obj002)은 마지막 샘플 + TOL을 넘기면 숨겨진다 — 잔상 방지의 핵심."""
    repo = _repo_with_dead_track()
    ranges = repo.get_object_time_ranges()
    vis = compute_object_visibility(at(3), ranges, tol_s=DEFAULT_VISIBILITY_TOL_S)
    assert vis == {"obj001": True, "obj002": False}
