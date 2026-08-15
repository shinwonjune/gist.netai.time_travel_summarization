"""scene_profiles 로더 + 스폰 구역이 프로파일 아레나를 따르는지 검증.

아레나 초기값은 1분 CSV의 coord_range에서 동결했으나(2026-08-15), 2026-08-16에
사용자가 실제 physics 벽 위치로 조정했다 — 아레나의 정답은 씬의 벽이지 옛 CSV가
아니므로 "CSV 유도값과 일치" 가드는 제거했다.
"""
import unittest
from pathlib import Path

from gist.netai.time_travel_summarization.automation.scene_profiles import (
    coord_range_of, load_profile, registry_path,
)

_PKG = Path(__file__).resolve().parent.parent


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

    def test_spawn_zone_follows_profile_arena(self):
        """스폰 구역 = 프로파일 아레나 범위, 표집은 margin 10% 안쪽 (벽 끼임 방지).

        구역 소스가 프로파일이어야 한다 — 예전에는 궤적 데이터(repo)를 읽어서
        벽은 프로파일, 스폰은 CSV로 어긋났다(2026-08-16 확인·수정).
        """
        from types import SimpleNamespace

        from gist.netai.time_travel_summarization.automation.generate_episodes import (
            load_spawn_zones, parse_spawn_plan, sample_zone_positions,
        )

        prof = load_profile("aigrad_building_v1")
        zones = load_spawn_zones(SimpleNamespace(spawn_zones=None, spawn_floor=89.5),
                                 core=None, profile=prof)
        z = next(iter(zones.values()))
        cmin, cmax = prof["coord_min"], prof["coord_max"]
        self.assertEqual(z["min"], [cmin[0], cmin[2]])
        self.assertEqual(z["max"], [cmax[0], cmax[2]])

        # 표집 위치는 각 변 10% 안쪽 — 아레나 경계에 붙지 않는다
        mx = (z["max"][0] - z["min"][0]) * 0.1
        mz = (z["max"][1] - z["min"][1]) * 0.1
        objids = [f"obj{i:03d}" for i in range(1, 5)]
        for seed in range(5):
            plan = parse_spawn_plan(None, zones, len(objids))
            pos = sample_zone_positions(zones, plan, objids, seed)
            self.assertEqual(len(pos), len(objids))
            for x, _y, zz in pos.values():
                self.assertGreaterEqual(x, z["min"][0] + mx)
                self.assertLessEqual(x, z["max"][0] - mx)
                self.assertGreaterEqual(zz, z["min"][1] + mz)
                self.assertLessEqual(zz, z["max"][1] - mz)
