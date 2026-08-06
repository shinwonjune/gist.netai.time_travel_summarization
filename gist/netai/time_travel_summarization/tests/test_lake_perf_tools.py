"""레이크 성능 실험 도구 테스트 — 전부 오프라인(file:// + CSV), 네트워크 무관.

대상 (레이크성능_실험설계.md):
  - lake_benchmark 데이터셋 모드: transport_bench / seek_bench / run_scenario (A·B 계층)
  - LakeProbe: 링버퍼·덤프 트리거(정지 전이 / 상한 도달) (C 계층)
  - gui_probe_report.analyze: 지표 산출
  - build_lake_perf_dataset: rebase(시간축 연속 배치) + downsample 어댑터
"""
import datetime
import json
import tempfile
import time
import unittest
from pathlib import Path

from gist.netai.time_travel_summarization.app.lake_probe import LakeProbe, sanitize_scenario
from gist.netai.time_travel_summarization.playback.lake_common import ingest_synthetic
from gist.netai.time_travel_summarization.tests.lake_benchmark import (
    run_scenario,
    seek_bench,
    transport_bench,
)
from gist.netai.time_travel_summarization.utils.build_lake_perf_dataset import (
    downsample_rows,
    rebase_episodes,
)
from gist.netai.time_travel_summarization.utils.gui_probe_report import analyze

_FMT = "%Y-%m-%d %H:%M:%S.%f"


def _synthetic_dataset(tmpdir: str) -> str:
    """3청크짜리 소형 csv 데이터셋(10obj × 180s @5Hz, 청크 60s)."""
    uri = (Path(tmpdir) / "ds").resolve().as_uri()
    ingest_synthetic(uri, n_objects=10, duration_s=180.0, hz=5.0,
                     chunk_seconds=60, fmt="csv")
    return uri


class TransportSeekBenchTest(unittest.TestCase):
    def test_transport_and_seek(self):
        with tempfile.TemporaryDirectory() as d:
            uri = _synthetic_dataset(d)
            t = transport_bench(uri)
            self.assertEqual(t["n_chunks_measured"], 3)
            self.assertEqual(t["format"], "csv")
            self.assertIsNotNone(t["first_get_ms"])
            self.assertGreater(t["decode_ms_per_mb"], 0.0)
            self.assertGreater(t["total_mb"], 0.0)

            s = seek_bench(uri, warm_queries=200)
            self.assertEqual(s["cold_seek_n"], 2)  # 청크0은 로드 시 활성
            self.assertGreaterEqual(s["cold_seek_p99_ms"], s["cold_seek_p50_ms"])
            self.assertGreater(s["warm_seek_p50_us"], 0.0)

    def test_max_chunks_cap(self):
        with tempfile.TemporaryDirectory() as d:
            uri = _synthetic_dataset(d)
            t = transport_bench(uri, max_chunks=2)
            self.assertEqual(t["n_chunks_measured"], 2)


class RunScenarioTest(unittest.TestCase):
    def test_forward_fast(self):
        """고배속 forward — 청크 경계 통과, 웜업 1회(로드 시 청크0)."""
        with tempfile.TemporaryDirectory() as d:
            uri = _synthetic_dataset(d)
            r = run_scenario(uri, "1x", speed=60.0, lookup_hz=50.0, play_wall_s=1.5,
                             cache_chunks=4, prefetch_ahead=2)
            self.assertGreater(r["lookups"], 10)
            self.assertEqual(r["warmup_cold_loads"], 1)
            self.assertGreaterEqual(r["stalls"], 0)
            self.assertTrue(0.0 <= r["hit_rate"] <= 1.0)

    def test_backward_warmup_two(self):
        """backward — 로드 시 청크0 + 끝점 진입 seek이 웜업으로 분리돼야 한다."""
        with tempfile.TemporaryDirectory() as d:
            uri = _synthetic_dataset(d)
            r = run_scenario(uri, "backward", speed=-60.0, lookup_hz=50.0, play_wall_s=1.0,
                             cache_chunks=4, prefetch_ahead=2)
            self.assertEqual(r["warmup_cold_loads"], 2)
            self.assertGreater(r["lookups"], 0)

    def test_random_seek(self):
        with tempfile.TemporaryDirectory() as d:
            uri = _synthetic_dataset(d)
            r = run_scenario(uri, "seek", seeks=3, cache_chunks=4, prefetch_ahead=2)
            self.assertEqual(r["seek_n"], 3)
            self.assertEqual(r["lookups"], 3)
            self.assertGreaterEqual(r["seek_p99_ms"], r["seek_p50_ms"])
            self.assertLessEqual(r["seek_cold_n"], 3)


class LakeProbeTest(unittest.TestCase):
    def _record(self, probe, playing, sync=0, hit=0, twin=None):
        stats = {"sync_loads": sync, "cache_hits": hit}
        probe.record(tick_ms=0.5, twin_time=twin, stats=stats, is_playing=playing)

    def test_dump_on_stop_transition(self):
        with tempfile.TemporaryDirectory() as d:
            probe = LakeProbe(out_dir=Path(d), max_frames=100)
            twin = datetime.datetime(2026, 1, 1)
            self._record(probe, True, sync=1, hit=0, twin=twin)   # 웜업 stall
            self._record(probe, True, sync=1, hit=5, twin=twin)
            self._record(probe, True, sync=2, hit=8, twin=twin)   # stall 프레임
            self._record(probe, False, sync=2, hit=9, twin=twin)  # 정지 -> 덤프
            self.assertEqual(len(probe), 0)  # 덤프 후 버퍼 리셋
            dumps = list(Path(d).glob("gui_probe_*.json"))
            self.assertEqual(len(dumps), 1)
            payload = json.loads(dumps[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["reason"], "stop")
            self.assertEqual(payload["n_frames"], 4)
            self.assertEqual(payload["frames"]["d_sync"], [1, 0, 1, 0])
            self.assertEqual(payload["frames"]["playing"], [True, True, True, False])

    def test_dump_on_cap(self):
        with tempfile.TemporaryDirectory() as d:
            probe = LakeProbe(out_dir=Path(d), max_frames=5)
            for _ in range(5):
                self._record(probe, True)
            dumps = list(Path(d).glob("gui_probe_*.json"))
            self.assertEqual(len(dumps), 1)
            self.assertEqual(json.loads(dumps[0].read_text(encoding="utf-8"))["reason"], "cap")
            self.assertEqual(len(probe), 0)

    def test_scenario_sanitize(self):
        """파일명에 그대로 들어가므로 ASCII 영숫자·대시·언더스코어만 남아야 한다."""
        self.assertEqual(sanitize_scenario("1x"), "1x")
        self.assertEqual(sanitize_scenario("-1x"), "-1x")           # 역방향 부호 보존
        self.assertEqual(sanitize_scenario("scrub fast"), "scrub-fast")
        self.assertEqual(sanitize_scenario("a/b\\c.json"), "a-b-c-json")
        self.assertEqual(sanitize_scenario("스크럽"), "")           # 비ASCII 전부 제거
        self.assertEqual(sanitize_scenario(None), "")
        self.assertLessEqual(len(sanitize_scenario("x" * 100)), 32)

    def test_reset_clears_without_writing(self):
        """GUI Start = 구간 시작 — 버퍼만 비우고 파일은 남기지 않는다."""
        with tempfile.TemporaryDirectory() as d:
            probe = LakeProbe(out_dir=Path(d), max_frames=100)
            for _ in range(3):
                self._record(probe, True, sync=1)
            self.assertEqual(probe.reset(), 3)
            self.assertEqual(len(probe), 0)
            self.assertEqual(list(Path(d).glob("*.json")), [])
            self.assertEqual(probe.live_stats()["stalls"], 0)
            # 리셋 후 정지 전이를 다시 잡아야 한다(재생 중 리셋했더라도)
            self._record(probe, True, sync=2)
            self._record(probe, False, sync=2)
            self.assertEqual(len(list(Path(d).glob("gui_probe_*.json"))), 1)

    def test_scenario_in_filename_and_payload(self):
        with tempfile.TemporaryDirectory() as d:
            probe = LakeProbe(out_dir=Path(d), max_frames=100)
            self.assertEqual(probe.set_scenario("scrub fast"), "scrub-fast")
            self._record(probe, True, sync=1)
            path = probe.dump(reason="manual")
            self.assertIsNotNone(path)
            self.assertTrue(path.name.startswith("gui_probe_"))
            self.assertTrue(path.name.endswith("_scrub-fast.json"), path.name)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["scenario"], "scrub-fast")
            self.assertEqual(payload["reason"], "manual")
            self.assertEqual(payload["n_frames"], 1)
            self.assertEqual(len(probe), 0)
            self.assertIsNone(probe.dump(reason="manual"))  # 빈 버퍼는 no-op

    def test_live_stats(self):
        with tempfile.TemporaryDirectory() as d:
            probe = LakeProbe(out_dir=Path(d), max_frames=100)
            self.assertEqual(probe.live_stats(),
                             {"frames": 0, "stalls": 0, "fps": 0.0, "scenario": ""})
            for i in range(5):
                self._record(probe, True, sync=i)  # 매 프레임 stall
            s = probe.live_stats()
            self.assertEqual(s["frames"], 5)
            self.assertEqual(s["stalls"], 4)  # 첫 프레임은 증분 0
            # fps는 실제 벽시계 간격에서 나온다 — 프레임을 20ms 띄워 확인
            for _ in range(3):
                time.sleep(0.02)
                self._record(probe, True, sync=4)
            self.assertGreater(probe.live_stats()["fps"], 0.0)

    def test_analyze_report(self):
        with tempfile.TemporaryDirectory() as d:
            probe = LakeProbe(out_dir=Path(d), max_frames=100)
            twin = datetime.datetime(2026, 1, 1)
            self._record(probe, True, sync=1, twin=twin)
            self._record(probe, True, sync=1, twin=twin)
            self._record(probe, True, sync=2, twin=twin)
            self._record(probe, False, sync=2, twin=twin)
            dump = list(Path(d).glob("gui_probe_*.json"))[0]
            rep = analyze(dump, playing_only=True)
            self.assertEqual(rep["frames"], 3)              # 재생 중 프레임만
            self.assertEqual(rep["warmup_stall_frames"], 1)  # 첫 stall = 콜드스타트
            self.assertEqual(rep["stall_frames_post_warmup"], 1)
            self.assertIn("hitch_rate_pct", rep)
            self.assertIn("tick_p50_ms", rep)


def _episode(start: str, seconds: float, hz: float, objid: str) -> list:
    base = datetime.datetime.strptime(start, _FMT)
    step = 1.0 / hz
    out = []
    for i in range(int(seconds * hz)):
        ts = (base + datetime.timedelta(seconds=i * step)).strftime(_FMT)[:-3]
        out.append({"timestamp": ts, "objid": objid, "x": float(i), "y": 90.0, "z": 0.0})
    return out


class BuildDatasetLogicTest(unittest.TestCase):
    def test_rebase_continuous(self):
        """절대 시각이 동떨어진 두 에피소드가 base부터 연속 배치돼야 한다."""
        ep1 = _episode("2026-07-01 10:00:00.000000", 5.0, 20.0, "obj001")
        ep2 = _episode("2026-07-15 22:30:00.000000", 5.0, 20.0, "obj001")
        base = datetime.datetime(2026, 1, 1)
        placed, used = rebase_episodes([ep1, ep2], base, gap_s=0.1, target_span_s=8.0)
        self.assertEqual(used, 2)
        self.assertEqual(placed[0]["timestamp"], "2026-01-01 00:00:00.000")
        times = [datetime.datetime.strptime(r["timestamp"] + "000", _FMT) for r in placed]
        span = (max(times) - min(times)).total_seconds()
        self.assertAlmostEqual(span, 5.0 - 0.05 + 0.1 + 5.0 - 0.05, delta=0.01)
        # 동일 objid 시각 충돌 없음(gap이 경계 겹침을 막는다)
        self.assertEqual(len(times), len(set(times)))

    def test_rebase_target_cutoff(self):
        """목표 스팬 도달 후의 에피소드는 버린다."""
        ep1 = _episode("2026-07-01 10:00:00.000000", 5.0, 20.0, "obj001")
        ep2 = _episode("2026-07-15 22:30:00.000000", 5.0, 20.0, "obj001")
        placed, used = rebase_episodes(
            [ep1, ep2], datetime.datetime(2026, 1, 1), gap_s=0.1, target_span_s=4.0)
        self.assertEqual(used, 1)
        self.assertEqual(len(placed), len(ep1))

    def test_downsample_adapter(self):
        """20Hz -> 5Hz: 행 수가 약 1/4로 줄고 timestamp 문자열 왕복이 보존된다."""
        ep = _episode("2026-07-01 10:00:00.000000", 10.0, 20.0, "obj001")
        sampled = downsample_rows(ep, 5.0)
        ratio = len(sampled) / len(ep)
        self.assertTrue(0.2 <= ratio <= 0.35, f"ratio={ratio}")
        self.assertIn("timestamp", sampled[0])
        self.assertIsInstance(sampled[0]["x"], float)


if __name__ == "__main__":
    unittest.main()
