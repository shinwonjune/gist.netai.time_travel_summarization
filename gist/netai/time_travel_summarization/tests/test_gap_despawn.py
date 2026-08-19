"""결손 인지 despawn 단위 테스트 (Kit 무의존)."""
import sys, datetime
sys.path.insert(0, '/mnt/c/Users/wonjune/workspace/kit-app-template/source/extensions/gist.netai.time_travel_summarization')
from gist.netai.time_travel_summarization.playback.visibility import (
    compute_object_visibility, is_track_visible,
)
from gist.netai.time_travel_summarization.playback.trajectory_repository import TrajectoryRepository

T0 = datetime.datetime(2026, 8, 19, 12, 0, 0)
def at(s): return T0 + datetime.timedelta(seconds=s)

# 1) gap 미지정이면 종전 동작 (범위 안은 항상 보임)
assert is_track_visible(at(5), T0, at(10)) is True
assert is_track_visible(at(5), T0, at(10), last_sample=at(0), gap_s=None) is True

# 2) gap 지정: 마지막 표본 이후 gap 초과면 숨김
assert is_track_visible(at(5), T0, at(10), last_sample=at(4.5), gap_s=0.3) is False  # 0.5s 결손 > 0.3
assert is_track_visible(at(5), T0, at(10), last_sample=at(4.9), gap_s=0.3) is True   # 0.1s 간격은 정상

# 3) 범위 밖은 gap과 무관하게 숨김
assert is_track_visible(at(20), T0, at(10), last_sample=at(10), gap_s=99) is False

# 4) dsr1(1초 간격) 보호: gap 1.5면 정상 표본 간격은 안 숨겨진다
assert is_track_visible(at(1.0), T0, at(10), last_sample=at(0.0), gap_s=1.5) is True

# 5) repo의 objid별 마지막 표본 조회 + 통합 판정
repo = TrajectoryRepository()
repo._data = {
    "2026-08-19 12:00:00": {"obj001": (0, 0, 0), "obj002": (5, 0, 0)},
    "2026-08-19 12:00:01": {"obj001": (1, 0, 0)},                      # obj002 결손 시작
    "2026-08-19 12:00:02": {"obj001": (2, 0, 0), "obj002": (6, 0, 0)},  # obj002 복귀
}
repo._timestamps = sorted(repo._data)
ls = repo.get_object_last_sample(at(1))
assert ls == {"obj001": at(1), "obj002": at(0)}, ls
vis = compute_object_visibility(at(1), repo.get_object_time_ranges(),
                                last_samples=ls, gap_s=0.5)
assert vis == {"obj001": True, "obj002": False}, vis   # 결손 중인 obj002만 숨김
vis_off = compute_object_visibility(at(1), repo.get_object_time_ranges())
assert vis_off == {"obj001": True, "obj002": True}, vis_off  # 비활성이면 종전대로 보임
print("gap-despawn self-test OK")
