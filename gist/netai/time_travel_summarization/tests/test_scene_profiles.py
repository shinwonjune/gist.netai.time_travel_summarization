"""scene_profiles 로더 + aigrad_building_v1 동결값의 드리프트 가드.

aigrad_building_v1의 coord_min/max는 regime1·2 생산이 암묵 의존하던
1분 CSV의 coord_range를 동결한 것이다(2026-08-15). 이 테스트는 그 동결값이
원본 CSV 유도값과 계속 일치하는지 지킨다 — 어긋나면 아레나가 조용히 바뀌어
데이터 세대 간 비교 가능성이 깨진다.
"""
import csv
import unittest
from pathlib import Path

from gist.netai.time_travel_summarization.automation.scene_profiles import (
    coord_range_of, load_profile, registry_path,
)

_PKG = Path(__file__).resolve().parent.parent
_CSV = _PKG / "data" / "living_trajectory_1min_0.2s.csv"


class SceneProfilesTest(unittest.TestCase):
    def test_registry_exists(self):
        self.assertTrue(registry_path().exists())

    def test_unknown_profile_lists_available(self):
        with self.assertRaises(KeyError) as ctx:
            load_profile("no-such-profile")
        self.assertIn("aigrad_building_v1", str(ctx.exception))

    def test_aigrad_profile_required_keys(self):
        prof = load_profile("aigrad_building_v1")
        self.assertTrue(prof["stage"].startswith("omniverse://"))
        self.assertEqual(prof["camera"], "Capture_camera")
        mins, maxs = coord_range_of(prof)
        self.assertEqual(len(mins), 3)
        self.assertTrue(all(a < b for a, b in zip(mins, maxs)))

    def test_frozen_range_matches_source_csv(self):
        """동결값 == CSV 유도값 (아레나 동일성 가드)."""
        xs, ys, zs = [], [], []
        with open(_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                xs.append(float(row["x"]))
                ys.append(float(row["y"]))
                zs.append(float(row["z"]))
        mins, maxs = coord_range_of(load_profile("aigrad_building_v1"))
        self.assertEqual(mins, (min(xs), min(ys), min(zs)))
        self.assertEqual(maxs, (max(xs), max(ys), max(zs)))


if __name__ == "__main__":
    unittest.main()
