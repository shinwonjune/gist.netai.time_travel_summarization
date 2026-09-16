"""객체 수 스윕 후처리(lake_scale_report) 테스트 — 안전계수 계산과 §4-4 한계 판정."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gist.netai.time_travel_summarization.utils.lake_scale_report import (
    dataset_key, format_a_table, format_b_table, format_verdict, judge,
    load_results, pick_latest, primary_sf, safety_factors, session_tail_ratio,
)


def _res(n, cs, *, lead_p50, lead_min, pf_ms, cold_p50, cold_p99, stalls=0, measured="2026-09-16 10:00:00",
         scenario="1x", get_tail=1.5, name=None):
    return {
        "dataset_uri": f"s3://b/trajectory/{name or f'synth_bev_{n}obj_10hz_30min_v1'}_c{cs}",
        "n_objects": n, "measured_at": measured,
        "transport": {"chunk_seconds": cs, "n_objects": n, "chunk_mean_rows": n * 600, "chunk_mean_mb": 0.04,
                      "get_p50_ms": 10.0, "get_p99_ms": 15.0, "get_tail_ratio": get_tail,
                      "decode_p50_ms": 5.0, "decode_us_per_row": 2.0},
        "seek": {"cold_seek_p50_ms": cold_p50, "cold_seek_p99_ms": cold_p99,
                 "cold_seek_tail_ratio": 1.3, "warm_seek_p50_us": 3.2},
        "scenarios": [{"scenario": scenario, "speed": 1.0, "stalls": stalls, "warmup_cold_loads": 1,
                       "prefetch_lead_p50_s": lead_p50, "prefetch_lead_min_s": lead_min,
                       "prefetch_load_p50_ms": pf_ms, "prefetch_load_n": 3, "sync_load_p50_ms": 17.7}],
    }


class SafetyFactorTest(unittest.TestCase):
    def test_sf_two_denominators(self):
        r = _res(4, 60, lead_p50=120.1, lead_min=60.1, pf_ms=20.0, cold_p50=16.1, cold_p99=21.0)
        row = safety_factors(r)[0]
        self.assertAlmostEqual(row["sf_p50_pf"], 120.1 / 0.020, places=0)
        self.assertAlmostEqual(row["sf_min_pf"], 60.1 / 0.020, places=0)
        self.assertAlmostEqual(row["sf_p50_cold"], 120.1 / 0.0161, places=0)
        self.assertEqual(primary_sf(row, "p50"), row["sf_p50_pf"])

    def test_sf_falls_back_to_cold_when_no_prefetch(self):
        r = _res(4, 60, lead_p50=None, lead_min=None, pf_ms=None, cold_p50=16.1, cold_p99=21.0)
        row = safety_factors(r)[0]
        self.assertIsNone(row["sf_p50_pf"])
        self.assertIsNone(primary_sf(row, "p50"))  # lead 자체가 없으면 판정 불가

    def test_dataset_key_from_name(self):
        r = {"dataset_uri": "file:///x/synth_bev_30obj_10hz_30min_v1_c300", "transport": {"chunk_seconds": 300}}
        self.assertEqual(dataset_key(r), (30, 300, "synth_bev_30obj_10hz_30min_v1_c300"))

    def test_dataset_key_zero_objids_falls_back_to_name(self):
        """objids 없는 레거시 manifest는 n_objects=0/None → 이름의 {N}obj로 폴백."""
        r = {"dataset_uri": "s3://b/t/synth_bev_30obj_10hz_30min_v1_c60", "n_objects": 0,
             "transport": {"chunk_seconds": 60, "n_objects": 0}}
        self.assertEqual(dataset_key(r)[0], 30)

    def test_real_and_synth_n4_are_separate_rows(self):
        """이음매 검사(§1-1): 실궤적 N=4와 합성 N=4가 한 행으로 뭉치면 안 된다."""
        real = _res(4, 60, lead_p50=120.0, lead_min=60.0, pf_ms=18.0, cold_p50=16.0, cold_p99=21.0,
                    name="aigrad_bev_10hz_30min_v1")
        synth = _res(4, 60, lead_p50=120.0, lead_min=60.0, pf_ms=19.0, cold_p50=17.0, cold_p99=22.0)
        latest = pick_latest([real, synth])
        self.assertEqual(len(latest), 2)
        a = format_a_table(latest)
        self.assertIn("aigrad_bev_10hz_30min_v1_c60", a)
        self.assertIn("synth_bev_4obj_10hz_30min_v1_c60", a)
        # 판정은 합성 행만으로
        j = judge(latest)
        self.assertTrue(all(x["max_n"] == 4 for x in j))

    def test_missing_seek_block_does_not_crash(self):
        r = _res(4, 60, lead_p50=120.0, lead_min=60.0, pf_ms=18.0, cold_p50=16.0, cold_p99=21.0)
        del r["seek"]
        latest = pick_latest([r])
        self.assertIn("| - / - |", format_a_table(latest))
        self.assertIsNone(safety_factors(r)[0]["sf_p50_cold"])
        self.assertEqual(session_tail_ratio(latest), 1.5)  # GET 꼬리만 남는다


class JudgeTest(unittest.TestCase):
    def _latest(self):
        rs = [
            _res(4, 60, lead_p50=120.0, lead_min=60.0, pf_ms=20.0, cold_p50=16.0, cold_p99=21.0),
            _res(20, 60, lead_p50=120.0, lead_min=60.0, pf_ms=100.0, cold_p50=80.0, cold_p99=95.0),
            _res(30, 60, lead_p50=119.0, lead_min=59.0, pf_ms=130.0, cold_p50=110.0, cold_p99=130.0),
            _res(50, 60, lead_p50=5.0, lead_min=1.0, pf_ms=800.0, cold_p50=700.0, cold_p99=900.0, stalls=2),
        ]
        return pick_latest(rs)

    def test_limits(self):
        j = judge(self._latest(), sf_threshold=10.0, seek_ms=100.0)
        by = {(x["limit"].split("(")[0], x["scenario"]): x for x in j}
        self.assertEqual(by[("탐색 응답 한계", "-")]["first_n"], 30)     # p99 130 > 100
        self.assertEqual(by[("데이터 경로 실용 한계", "1x")]["first_n"], 50)  # 5/0.8 = 6.25 < 10
        self.assertEqual(by[("데이터 경로 경성 한계", "1x")]["first_n"], 50)

    def test_not_reached_is_reported_as_gt_max(self):
        latest = pick_latest([
            _res(4, 300, lead_p50=600.0, lead_min=300.0, pf_ms=40.0, cold_p50=38.0, cold_p99=41.0),
            _res(10, 300, lead_p50=600.0, lead_min=300.0, pf_ms=90.0, cold_p50=85.0, cold_p99=90.0),
        ])
        j = judge(latest)
        for x in j:
            self.assertIsNone(x["first_n"])
            self.assertEqual(x["max_n"], 10)
        self.assertIn("> 10 (격자 내 미도달)", format_verdict(j))

    def test_pick_latest_and_tables(self):
        old = _res(4, 60, lead_p50=1.0, lead_min=1.0, pf_ms=1.0, cold_p50=1.0, cold_p99=1.0, measured="2026-09-01 00:00:00")
        new = _res(4, 60, lead_p50=120.0, lead_min=60.0, pf_ms=20.0, cold_p50=16.0, cold_p99=21.0)
        latest = pick_latest([old, new])
        key = (4, 60, "synth_bev_4obj_10hz_30min_v1_c60")
        self.assertEqual(latest[key][1], 2)
        self.assertEqual(latest[key][0]["seek"]["cold_seek_p99_ms"], 21.0)
        a, b = format_a_table(latest), format_b_table(latest)
        self.assertIn("| 4 | 60 | synth_bev_4obj_10hz_30min_v1_c60 | 2 |", a)
        self.assertIn("| 4 | 60 | synth_bev_4obj_10hz_30min_v1_c60 | 1x |", b)

    def test_session_tail_raises_threshold(self):
        """§4-4: 문턱 = max(고정, 세션 관측 꼬리 배율) — 꼬리가 25면 SF 20은 실용 한계에 걸린다."""
        rs = [_res(4, 60, lead_p50=120.0, lead_min=60.0, pf_ms=20.0, cold_p50=16.0, cold_p99=21.0, get_tail=25.0),
              _res(20, 60, lead_p50=120.0, lead_min=60.0, pf_ms=6000.0, cold_p50=5000.0, cold_p99=6000.0)]
        latest = pick_latest(rs)
        tail = session_tail_ratio(latest)
        self.assertEqual(tail, 25.0)
        j = judge(latest, sf_threshold=max(10.0, tail))
        by = {(x["limit"].split("(")[0], x["scenario"]): x for x in j}
        self.assertEqual(by[("데이터 경로 실용 한계", "1x")]["first_n"], 20)  # 120/6 = 20 < 25
        self.assertEqual(judge(latest, sf_threshold=10.0)[1]["first_n"], None)  # 고정 10이면 미도달

    def test_load_results_skips_other_json(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "lake_bench_x.json").write_text(json.dumps(
                _res(4, 60, lead_p50=1, lead_min=1, pf_ms=1, cold_p50=1, cold_p99=1)), encoding="utf-8")
            (Path(d) / "gui_probe_x.json").write_text(json.dumps({"frames": {}}), encoding="utf-8")
            rs = load_results([str(Path(d) / "*.json")])
            self.assertEqual(len(rs), 1)
            self.assertEqual(rs[0]["_file"], "lake_bench_x.json")


if __name__ == "__main__":
    unittest.main()
