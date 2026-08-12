"""레이크 성능 실험 도구 테스트 — 전부 오프라인(file:// + CSV), 네트워크 무관.

대상 (레이크성능_실험설계.md):
  - lake_benchmark 데이터셋 모드: transport_bench / seek_bench / run_scenario (A·B 계층)
  - LakeProbe: 링버퍼·덤프 트리거(정지 전이 / 상한 도달)·idle 전용 버퍼 억제 (C 계층)
  - gui_probe_report: 재생/탐색/idle 구간 분류와 구간별 지표 산출
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
from gist.netai.time_travel_summarization.utils.gui_probe_report import (
    analyze,
    classify_regimes,
)

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
            play = analyze(dump)["regimes"]["playback"]
            self.assertEqual(play["frames"], 3)              # 재생 중 프레임만
            self.assertEqual(play["warmup_stall_frames"], 1)  # 첫 stall = 콜드스타트
            self.assertEqual(play["stall_frames_post_warmup"], 1)
            self.assertIn("hitch_rate_pct", play)
            self.assertIn("tick_p50_ms", play)


class IdleDumpSuppressionTest(unittest.TestCase):
    """idle 전용 버퍼는 파일로 남기지 않는다(계측을 켠 채 방치할 때 쌓이던 빈 덤프).

    "의미 있는 프레임"은 재생(playing=True) 또는 탐색(playing=False인데 직전 프레임
    대비 twin_time이 달라짐) 프레임이다. 둘 다 없으면 성능 판정에 쓸 것이 없다.
    """

    def _record(self, probe, playing, sync=0, twin=None):
        probe.record(tick_ms=0.5, twin_time=twin, stats={"sync_loads": sync}, is_playing=playing)

    def test_idle_only_cap_writes_nothing_but_clears_buffer(self):
        """상한에 닿아도 파일은 안 만들되 버퍼는 비워야 한다(메모리 무한 증가 방지)."""
        with tempfile.TemporaryDirectory() as d:
            probe = LakeProbe(out_dir=Path(d), max_frames=5)
            twin = datetime.datetime(2026, 1, 1)
            for _ in range(5):
                self._record(probe, False, twin=twin)  # 정지 + 시계 불변 = idle
            self.assertEqual(list(Path(d).glob("*.json")), [])
            self.assertEqual(len(probe), 0)  # 버퍼는 비워졌다
            self.assertFalse(probe.has_meaningful_frames())
            # 비운 뒤에도 계속 idle이면 여전히 파일이 생기지 않아야 한다
            for _ in range(5):
                self._record(probe, False, twin=twin)
            self.assertEqual(list(Path(d).glob("*.json")), [])

    def test_idle_only_manual_dump_is_noop(self):
        """수동 Dump는 사용자가 누른 경계라 버퍼를 건드리지 않고 no-op으로 끝난다."""
        with tempfile.TemporaryDirectory() as d:
            probe = LakeProbe(out_dir=Path(d), max_frames=100)
            twin = datetime.datetime(2026, 1, 1)
            for _ in range(3):
                self._record(probe, False, twin=twin)
            self.assertIsNone(probe.dump(reason="manual"))
            self.assertEqual(list(Path(d).glob("*.json")), [])
            self.assertEqual(len(probe), 3)

    def test_playback_frame_makes_buffer_dumpable(self):
        """idle 사이에 재생 프레임이 하나라도 섞이면 상한 도달 시 덤프된다."""
        with tempfile.TemporaryDirectory() as d:
            probe = LakeProbe(out_dir=Path(d), max_frames=5)
            twin = datetime.datetime(2026, 1, 1)
            for _ in range(4):
                self._record(probe, False, twin=twin)
            self.assertFalse(probe.has_meaningful_frames())
            self._record(probe, True, twin=twin)  # 재생 프레임 1개 -> 5프레임 상한
            dumps = list(Path(d).glob("gui_probe_*.json"))
            self.assertEqual(len(dumps), 1)
            self.assertEqual(json.loads(dumps[0].read_text(encoding="utf-8"))["reason"], "cap")

    def test_seek_only_buffer_is_dumped(self):
        """재생이 전혀 없어도 스크럽(정지 상태에서 twin_time 변화)만으로 덤프된다.

        순수 스크럽 덤프는 정지 전이가 없어 예전에는 통째로 버려질 뻔한 경우다.
        """
        with tempfile.TemporaryDirectory() as d:
            probe = LakeProbe(out_dir=Path(d), max_frames=4)
            base = datetime.datetime(2026, 1, 1)
            for i in range(4):  # 매 프레임 시계가 달라진다 = 슬라이더 스크럽
                self._record(probe, False, twin=base + datetime.timedelta(seconds=i))
            dumps = list(Path(d).glob("gui_probe_*.json"))
            self.assertEqual(len(dumps), 1)
            payload = json.loads(dumps[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["n_frames"], 4)
            self.assertEqual(payload["frames"]["playing"], [False] * 4)
            # 첫 프레임은 비교 대상이 없어 idle, 나머지 3개가 탐색으로 잡혀야 한다
            groups = classify_regimes(payload["frames"])
            self.assertEqual(groups["playback"], [])
            self.assertEqual(groups["seek"], [1, 2, 3])
            self.assertEqual(groups["idle"], [0])


def _write_probe_dump(dirpath: str, playing, twin, intervals, d_sync) -> Path:
    """구간 분류·지표 검증용 합성 덤프. 프레임 간격을 정확히 지정하려면 실제 계측이
    아니라 이렇게 직접 만들어야 한다(record()는 perf_counter로 간격을 잰다)."""
    n = len(playing)
    path = Path(dirpath) / "gui_probe_synth.json"
    path.write_text(json.dumps({
        "version": 1, "reason": "manual", "scenario": "", "n_frames": n,
        "frames": {
            "wall_ts": [round(i * 0.1, 4) for i in range(n)],
            "twin_time": list(twin),
            "frame_interval_ms": list(intervals),
            "tick_ms": [0.5] * n,
            "d_sync": list(d_sync),
            "d_hit": [0] * n,
            "playing": list(playing),
        },
    }), encoding="utf-8")
    return path


class RegimeReportTest(unittest.TestCase):
    def test_seek_stall_interval_stats(self):
        """탐색의 주 판정 지표 — stall 프레임의 frame_interval p50/p95/최대."""
        with tempfile.TemporaryDirectory() as d:
            #        i0(idle) i1    i2    i3    i4(seek) i5(idle) i6(play)
            playing = [False, False, False, False, False, False, True]
            twin = ["t0", "t1", "t2", "t3", "t4", "t4", "t5"]
            iv = [0.0, 100.0, 200.0, 300.0, 20.0, 16.0, 16.0]
            sync = [0, 1, 1, 1, 0, 0, 0]
            rep = analyze(_write_probe_dump(d, playing, twin, iv, sync))

            seek = rep["regimes"]["seek"]
            self.assertEqual(seek["frames"], 4)          # i1~i4
            self.assertEqual(seek["stall_frames"], 3)
            self.assertEqual(seek["warmup_stall_frames"], 1)
            self.assertEqual(seek["stall_frames_post_warmup"], 2)
            self.assertEqual(seek["stall_frame_rate_pct"], 75.0)
            self.assertEqual(seek["stall_interval_p50_ms"], 200.0)
            self.assertEqual(seek["stall_interval_p95_ms"], 300.0)
            self.assertEqual(seek["stall_interval_max_ms"], 300.0)

            self.assertEqual(rep["regimes"]["playback"]["frames"], 1)
            self.assertEqual(rep["regimes"]["idle"]["frames"], 2)   # i0, i5
            self.assertEqual(rep["n_frames"], 7)

    def test_twin_time_change_separates_seek_from_idle(self):
        """정지 중에도 twin_time이 바뀌면 탐색, 그대로면 idle."""
        with tempfile.TemporaryDirectory() as d:
            playing = [False] * 5
            twin = ["t0", "t0", "t1", "t1", "t2"]
            rep_path = _write_probe_dump(d, playing, twin, [0.0] + [16.0] * 4, [0] * 5)
            groups = classify_regimes(json.loads(rep_path.read_text(encoding="utf-8"))["frames"])
            self.assertEqual(groups["seek"], [2, 4])
            self.assertEqual(groups["idle"], [0, 1, 3])
            self.assertEqual(groups["playback"], [])

    def test_playing_frames_are_playback_even_if_twin_unchanged(self):
        """일시정지 없이 재생 중이면 twin_time이 같아 보여도 재생 구간이다
        (같은 밀리초 안에 두 프레임이 들어가면 문자열이 같을 수 있다)."""
        with tempfile.TemporaryDirectory() as d:
            rep_path = _write_probe_dump(
                d, [True, True, False], ["t0", "t0", "t0"], [0.0, 16.0, 16.0], [0, 0, 0])
            groups = classify_regimes(json.loads(rep_path.read_text(encoding="utf-8"))["frames"])
            self.assertEqual(groups["playback"], [0, 1])
            self.assertEqual(groups["idle"], [2])

    def test_none_twin_time_transition_counts_as_seek(self):
        """twin_time이 None → 값으로 바뀌는 것도 시간축 이동이므로 탐색이다.

        None은 "직전 프레임이 없다"와 값이 겹치므로, 첫 프레임 여부는 값이 아니라
        별도 플래그로 판정해야 한다는 것을 고정하는 테스트다.
        """
        with tempfile.TemporaryDirectory() as d:
            rep_path = _write_probe_dump(
                d, [False, False, False], [None, "t1", "t1"], [0.0, 16.0, 16.0], [0, 0, 0])
            groups = classify_regimes(json.loads(rep_path.read_text(encoding="utf-8"))["frames"])
            self.assertEqual(groups["seek"], [1])
            self.assertEqual(groups["idle"], [0, 2])


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
