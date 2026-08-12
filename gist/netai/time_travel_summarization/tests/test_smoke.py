import datetime
from pathlib import Path
import unittest

from gist.netai.time_travel_summarization.app.config import ExtensionConfig
from gist.netai.time_travel_summarization.app.paths import ExtensionPaths
from gist.netai.time_travel_summarization.playback.controller import PlaybackController
from gist.netai.time_travel_summarization.playback.trajectory_repository import TrajectoryRepository


class RefactoringSmokeTest(unittest.TestCase):
    def test_extension_config_loads_current_package_shape(self):
        config_path = Path(__file__).resolve().parents[1] / "config.json"
        config = ExtensionConfig.from_file(str(config_path))

        # 확장자를 못 박지 않는다 — data_path는 단일 파일(.csv/.parquet)일 수도,
        # 청크 데이터셋의 manifest.json일 수도 있고(리더는 URI 모양이 정한다,
        # data_service._repo_factory_for 참조), 머신마다 다른 값을 env로 주입받는다.
        # 여기서 확인할 것은 "config가 현재 패키지 모양대로 읽히는가"이므로
        # data_uri가 URI로 해석되는지만 본다.
        self.assertIn("://", config.data_uri)
        self.assertIsInstance(config.prim_map, dict)

    def test_trajectory_repository_returns_last_known_value(self):
        repository = TrajectoryRepository()
        csv_path = Path(__file__).resolve().parents[1] / "data" / "living_trajectory_1min_0.2s.csv"

        self.assertTrue(repository.load_csv(csv_path))
        between = datetime.datetime(2025, 1, 1, 0, 0, 0, 100000)
        data = repository.get_data_at_time(between)

        self.assertTrue(data)

    def test_playback_controller_clamps_progress_into_range(self):
        playback = PlaybackController()
        start = datetime.datetime(2025, 1, 1, 0, 0, 0)
        end = datetime.datetime(2025, 1, 1, 0, 0, 10)
        playback.configure_data_range(start, end)

        playback.set_progress(2.0)

        self.assertEqual(playback.get_current_time(), end)

    def test_extension_paths_uses_artifacts_root(self):
        import os

        module_dir = Path(__file__).resolve().parents[1]
        paths = ExtensionPaths(module_dir)

        sep = os.sep
        self.assertTrue(str(paths.videos_dir).endswith(f"artifacts{sep}video"))
        self.assertTrue(str(paths.vlm_outputs_dir).endswith(f"artifacts{sep}vlm_outputs"))


if __name__ == "__main__":
    unittest.main()
