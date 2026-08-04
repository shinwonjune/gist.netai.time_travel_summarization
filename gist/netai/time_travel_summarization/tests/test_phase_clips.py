"""위상 분해 추출기(automation/phase_clips.py) 순수 로직 테스트 — ffmpeg 불필요.

검증 대상 (docs/위상분해_실험설계.md §5):
  - 접촉 클러스터링(객체 단위 다중 행 -> 사건 1개)
  - near-miss 극소점 탐지(지면 x,z 거리)
  - 조건별 창 계획: 경계 검사 / 순도(오염 접촉) 필터 / control 배치
  - ffmpeg 명령 구조(단일 구간 = concat n=1, 스플라이스 = n=2)
"""
import datetime
import unittest

from gist.netai.time_travel_summarization.automation.phase_clips import (
    contact_clusters,
    ffmpeg_cmd,
    near_miss_events,
    plan_episode,
)

_FMT = "%Y-%m-%d %H:%M:%S.%f"
BASE = datetime.datetime(2026, 1, 1, 12, 0, 0)


def _ts(sec: float) -> str:
    return (BASE + datetime.timedelta(seconds=sec)).strftime(_FMT)[:-3]


def _col_row(sec: float, objid: str) -> dict:
    return {"timestamp": _ts(sec), "objid": objid,
            "x": "0", "y": "90", "z": "0", "kind": "contact"}


def _trace_two_objects(d_min: float, t_min_s: float = 15.0, span_s: float = 30.0,
                       hz: float = 20.0) -> list:
    """두 객체가 x축에서 접근->극소->이탈. 극소 시각 t_min_s, 최소거리 d_min."""
    rows = []
    speed = 60.0  # cm/s 접근 속도(각자)
    for i in range(int(span_s * hz)):
        t = i / hz
        half = d_min / 2 + speed * abs(t - t_min_s)
        for objid, sign in (("obj001", -1.0), ("obj002", 1.0)):
            rows.append({"timestamp": _ts(t), "objid": objid,
                         "x": f"{sign * half}", "y": "90", "z": "0"})
    return rows


class ContactClusterTest(unittest.TestCase):
    def test_multi_row_single_event(self):
        rows = [_col_row(10.0, "obj001"), _col_row(10.05, "obj002"), _col_row(10.3, "obj001")]
        self.assertEqual(len(contact_clusters(rows)), 1)

    def test_separate_events(self):
        rows = [_col_row(10.0, "obj001"), _col_row(14.0, "obj002")]
        self.assertEqual(len(contact_clusters(rows)), 2)


class NearMissEventTest(unittest.TestCase):
    def test_detects_minimum(self):
        rows = _trace_two_objects(d_min=95.0, t_min_s=15.0)
        events = near_miss_events(rows, gap=95.0)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertAlmostEqual(ev["d_min"], 95.0, delta=1.0)
        self.assertAlmostEqual((ev["t"] - BASE).total_seconds(), 15.0, delta=0.1)
        self.assertEqual(ev["pair"], ("obj001", "obj002"))

    def test_far_pair_no_event(self):
        rows = _trace_two_objects(d_min=400.0)  # enter_thr=190 밖
        self.assertEqual(near_miss_events(rows, gap=95.0), [])


class PlanEpisodeTest(unittest.TestCase):
    def test_collision_all_conditions(self):
        col = [_col_row(15.0, "obj001"), _col_row(15.1, "obj002")]
        plans = plan_episode("collision", [], col, BASE, 30.0, n_control=0)
        conds = sorted(p["condition"] for p in plans)
        self.assertEqual(conds, ["approach_only", "full", "no_aftermath",
                                 "no_approach", "no_contact"])

    def test_bounds_filter(self):
        """접촉이 t=1s면 approach_only([-2.2,-0.2])는 범위 밖 -> 제외."""
        col = [_col_row(1.0, "obj001")]
        plans = plan_episode("collision", [], col, BASE, 30.0, n_control=0)
        conds = [p["condition"] for p in plans]
        self.assertNotIn("approach_only", conds)
        self.assertIn("no_approach", conds)  # [-0.1,+1.9]는 성립

    def test_purity_filter(self):
        """1초 간격 접촉 2건 -> 서로의 창에 끼어들어 다수 조건 폐기."""
        col = [_col_row(15.0, "obj001"), _col_row(16.0, "obj002")]
        plans = plan_episode("collision", [], col, BASE, 30.0, n_control=0)
        # full[t=15]=[14,16]은 16.0 접촉 포함 -> 폐기. no_aftermath[t=15]=[13.1,15.1]은 생존.
        full_refs = [p["t_ref"] for p in plans if p["condition"] == "full"]
        self.assertEqual(full_refs, [])
        na_refs = [p["t_ref"] for p in plans if p["condition"] == "no_aftermath"]
        self.assertTrue(any(abs((t - BASE).total_seconds() - 15.0) < 0.01 for t in na_refs))

    def test_nearmiss_contaminated_dropped(self):
        """near-miss 창 안에 실충돌(3+객체 한계) -> 폐기."""
        trace = _trace_two_objects(d_min=95.0, t_min_s=15.0)
        col = [_col_row(15.5, "obj003")]
        plans = plan_episode("nearmiss", trace, col, BASE, 30.0, n_control=0)
        self.assertEqual([p for p in plans if p["condition"] == "near_miss"], [])

    def test_nearmiss_clean_kept_with_control(self):
        trace = _trace_two_objects(d_min=95.0, t_min_s=15.0)
        plans = plan_episode("nearmiss", trace, [], BASE, 30.0, n_control=1)
        nm = [p for p in plans if p["condition"] == "near_miss"]
        ctrl = [p for p in plans if p["condition"] == "control"]
        self.assertEqual(len(nm), 1)
        self.assertEqual(nm[0]["d_min"], 95.0)
        self.assertEqual(len(ctrl), 1)
        # control은 극소점에서 CONTROL_BUFFER_S 이상 격리
        gap_s = abs((ctrl[0]["t_ref"] - nm[0]["t_ref"]).total_seconds())
        self.assertGreater(gap_s, 3.0)


class FfmpegCmdTest(unittest.TestCase):
    def test_single_segment(self):
        from pathlib import Path
        segs = [(BASE + datetime.timedelta(seconds=14), BASE + datetime.timedelta(seconds=16))]
        cmd = ffmpeg_cmd(Path("v.mp4"), segs, BASE, Path("out.mp4"))
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("trim=start=14.000:duration=2.000", graph)
        self.assertIn("concat=n=1", graph)
        self.assertIn("libx264", cmd)

    def test_splice_two_segments(self):
        from pathlib import Path
        segs = [(BASE + datetime.timedelta(seconds=13.5), BASE + datetime.timedelta(seconds=14.5)),
                (BASE + datetime.timedelta(seconds=15.5), BASE + datetime.timedelta(seconds=16.5))]
        cmd = ffmpeg_cmd(Path("v.mp4"), segs, BASE, Path("out.mp4"))
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("concat=n=2", graph)
        self.assertEqual(graph.count("trim=start="), 2)


if __name__ == "__main__":
    unittest.main()
