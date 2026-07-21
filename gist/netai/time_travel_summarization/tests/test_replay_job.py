"""재연 잡 순수 헬퍼 테스트 (omni 무의존).

replay_range의 구간 파싱·검증·파일명과 remote_generation의 replay 잡 env 렌더링·
러너 디스패치를 Kit 없이 검증한다.
"""
import datetime
import unittest

from gist.netai.time_travel_summarization.automation.replay_range import (
    parse_dt, replay_output_name, validate_window,
)
from gist.netai.time_travel_summarization.automation.remote_generation import (
    JOB_TYPES, JobSpec, build_submit_command, runner_rel_for,
)


class ReplayHelpersTest(unittest.TestCase):
    def test_parse_dt_ok(self):
        d = parse_dt("2026-07-18 15:35:10")
        self.assertEqual((d.year, d.month, d.day, d.hour, d.minute, d.second),
                         (2026, 7, 18, 15, 35, 10))

    def test_parse_dt_rejects_bad(self):
        for bad in ("2026/07/18 15:35:10", "not-a-date", "", "2026-07-18"):
            with self.assertRaises(ValueError):
                parse_dt(bad)

    def test_validate_window(self):
        s = parse_dt("2026-07-18 15:35:10")
        e = parse_dt("2026-07-18 15:35:40")
        ds = parse_dt("2026-07-18 15:00:00")
        de = parse_dt("2026-07-18 16:00:00")
        self.assertIsNone(validate_window(s, e, ds, de))
        self.assertIn("empty", validate_window(e, s, ds, de))
        self.assertIn("empty", validate_window(s, s, ds, de))  # end == start
        self.assertIn("before data start",
                      validate_window(parse_dt("2026-07-18 14:59:59"), e, ds, de))
        self.assertIn("after data end",
                      validate_window(s, parse_dt("2026-07-18 16:00:01"), ds, de))
        # 데이터 범위 미상(None)이면 순서만 검사
        self.assertIsNone(validate_window(s, e, None, None))

    def test_output_name_deterministic(self):
        s = datetime.datetime(2026, 7, 18, 15, 35, 10)
        e = datetime.datetime(2026, 7, 18, 15, 35, 40)
        self.assertEqual(replay_output_name(s, e),
                         "replay_20260718T153510_20260718T153540")


class ReplayJobSpecTest(unittest.TestCase):
    def test_replay_in_job_types(self):
        self.assertIn("replay", JOB_TYPES)
        self.assertTrue(runner_rel_for("replay").endswith("run_replay.sh"))

    def test_to_env_renders_replay_fields(self):
        spec = JobSpec(job_id="replay-1", job_type="replay",
                       replay_start="2026-07-18 15:35:10",
                       replay_end="2026-07-18 15:35:40",
                       data_uri="s3://bucket/ep/_trace.csv")
        env = spec.to_env()
        self.assertEqual(env["JOB_TYPE"], "replay")
        self.assertEqual(env["REPLAY_START"], "2026-07-18 15:35:10")
        self.assertEqual(env["REPLAY_END"], "2026-07-18 15:35:40")
        self.assertEqual(env["DATA_URI"], "s3://bucket/ep/_trace.csv")

    def test_submit_command_dispatch_and_quoting(self):
        spec = JobSpec(job_id="replay-1", job_type="replay",
                       replay_start="2026-07-18 15:35:10",
                       replay_end="2026-07-18 15:35:40")
        cmd = build_submit_command(spec, "/home/x/ext")
        self.assertIn("run_replay.sh", cmd)
        self.assertIn("JOB_TYPE=replay", cmd)
        # 공백 포함 시각은 인용되어 하나의 env 값으로 전달
        self.assertIn("'2026-07-18 15:35:10'", cmd)
        # 빈 값(DATA_URI 등)은 명령에서 생략
        self.assertNotIn("DATA_URI", cmd)


if __name__ == "__main__":
    unittest.main()
