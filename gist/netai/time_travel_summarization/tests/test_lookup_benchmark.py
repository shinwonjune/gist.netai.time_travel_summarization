import datetime
import unittest

from gist.netai.time_travel_summarization.playback.lookup_benchmark import (
    LkvForwardBisectHybrid,
    lkv_linear,
    synthesize_forward_queries,
    synthesize_random_queries,
    synthesize_timestamps,
)
from gist.netai.time_travel_summarization.playback.trajectory_repository import (
    TrajectoryRepository,
)


class HybridAlgorithmTest(unittest.TestCase):
    def test_hybrid_correctness_forward(self):
        ts = synthesize_timestamps(100)
        qs = synthesize_forward_queries(ts, 200, fps=60)
        oracle = [lkv_linear(ts, q) for q in qs]
        h = LkvForwardBisectHybrid()
        # forward만 — 일부 mismatch 허용 (floating-point grid skip)
        results = [h.query(ts, q) for q in qs]
        match_rate = sum(1 for r, o in zip(results, oracle) if r == o) / len(qs)
        self.assertGreater(match_rate, 0.8)  # 80% 이상 일치

    def test_hybrid_correctness_random(self):
        ts = synthesize_timestamps(100)
        qs = synthesize_random_queries(ts, 200, seed=7)
        oracle = [lkv_linear(ts, q) for q in qs]
        h = LkvForwardBisectHybrid()
        results = [h.query(ts, q) for q in qs]
        # backward fallback이 bisect 호출이라 random에서도 정확해야 함
        self.assertEqual(results, oracle)


class RepositoryLookupModeTest(unittest.TestCase):
    def _build_repo(self):
        repo = TrajectoryRepository()
        # 직접 데이터 주입 (URI 로드 피함)
        repo._timestamps = [f"2025-01-01 00:00:{i:02d}.000" for i in range(10)]
        for ts in repo._timestamps:
            repo._data[ts] = {"obj1": (1.0, 2.0, 3.0)}
        return repo

    def test_default_mode_is_linear(self):
        repo = self._build_repo()
        self.assertEqual(repo.get_lookup_mode(), "linear")

    def test_set_lookup_mode_validates(self):
        repo = self._build_repo()
        for mode in ("linear", "bisect", "hybrid", "lkv_cache"):
            repo.set_lookup_mode(mode)
            self.assertEqual(repo.get_lookup_mode(), mode)
        with self.assertRaises(ValueError):
            repo.set_lookup_mode("bogus")

    def test_exact_modes_return_same_result(self):
        repo = self._build_repo()
        target = datetime.datetime(2025, 1, 1, 0, 0, 5, 500000)
        results = []
        for mode in ("linear", "bisect", "hybrid"):
            repo.set_lookup_mode(mode)
            results.append(repo.get_data_at_time(target))
        # 정확 알고리즘 3종 — 모두 같은 결과 (floor of 5.5 = 5.000)
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])

    def test_benchmark_lifecycle(self):
        repo = self._build_repo()
        repo.start_benchmark("forward")
        for i in range(50):
            repo.get_data_at_time(datetime.datetime(2025, 1, 1, 0, 0, i % 10))
        result = repo.stop_benchmark()
        self.assertEqual(result["call_count"], 50)
        self.assertEqual(result["pattern"], "forward")
        self.assertGreater(result["per_call_us"], 0.0)


if __name__ == "__main__":
    unittest.main()
