"""결손 인지 despawn 판정 (Kit 무의존).

배경(v3 계획서 §4-5): frag-sameid 계열은 같은 ID로 중간 행만 지우므로 트랙 생존 창이
결손을 덮어 dead-track despawn이 발동하지 않는다 → hold로 "얼어 있는 객체"가 되어
소멸 대조군이 occ-hold 변형으로 변질된다. 그래서 "범위 안이라도 마지막 표본 이후
gap_s 초과면 부재"라는 판정을 추가했고, 여기서 그 경계와 부작용(다운샘플 조건에서
정상 표본 간격을 소멸로 오판하지 않는가)을 고정한다.
"""
import datetime
import unittest

from gist.netai.time_travel_summarization.playback.trajectory_repository import (
    TrajectoryRepository,
)
from gist.netai.time_travel_summarization.playback.visibility import (
    compute_object_visibility, is_track_visible,
)

T0 = datetime.datetime(2026, 8, 19, 12, 0, 0)


def at(seconds: float) -> datetime.datetime:
    return T0 + datetime.timedelta(seconds=seconds)


class GapAwareDespawnTest(unittest.TestCase):
    def test_disabled_by_default_keeps_previous_behaviour(self):
        """gap_s 미지정이면 범위 안은 항상 보임 — 종전 동작 보존."""
        self.assertTrue(is_track_visible(at(5), T0, at(10)))
        self.assertTrue(is_track_visible(at(5), T0, at(10), last_sample=at(0), gap_s=None))

    def test_hides_when_gap_exceeded(self):
        """마지막 표본 이후 경과가 임계를 넘으면 숨긴다(엄격 초과)."""
        self.assertFalse(is_track_visible(at(5), T0, at(10), last_sample=at(4.5), gap_s=0.3))
        self.assertTrue(is_track_visible(at(5), T0, at(10), last_sample=at(4.9), gap_s=0.3))

    def test_out_of_track_range_hidden_regardless(self):
        self.assertFalse(is_track_visible(at(20), T0, at(10), last_sample=at(10), gap_s=99))

    def test_downsample_1hz_not_hidden(self):
        """dsr1(1초 간격)은 정상 표본이므로 임계 1.5s에서 숨겨지면 안 된다."""
        self.assertTrue(is_track_visible(at(1.0), T0, at(10), last_sample=at(0.0), gap_s=1.5))


class RepositoryLastSampleTest(unittest.TestCase):
    def setUp(self):
        self.repo = TrajectoryRepository()
        self.repo._data = {
            "2026-08-19 12:00:00": {"obj001": (0, 0, 0), "obj002": (5, 0, 0)},
            "2026-08-19 12:00:01": {"obj001": (1, 0, 0)},                     # obj002 결손
            "2026-08-19 12:00:02": {"obj001": (2, 0, 0), "obj002": (6, 0, 0)},  # 복귀
        }
        self.repo._timestamps = sorted(self.repo._data)

    def test_last_sample_per_object(self):
        self.assertEqual(self.repo.get_object_last_sample(at(1)),
                         {"obj001": at(1), "obj002": at(0)})

    def test_visibility_hides_only_the_missing_object(self):
        vis = compute_object_visibility(
            at(1), self.repo.get_object_time_ranges(),
            last_samples=self.repo.get_object_last_sample(at(1)), gap_s=0.5)
        self.assertEqual(vis, {"obj001": True, "obj002": False})

    def test_visibility_unchanged_when_feature_off(self):
        vis = compute_object_visibility(at(1), self.repo.get_object_time_ranges())
        self.assertEqual(vis, {"obj001": True, "obj002": True})


if __name__ == "__main__":
    unittest.main()
