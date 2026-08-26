"""위상 분해 추출기 v3.2(automation/phase_clips.py) 순수 로직 테스트 — ffmpeg 불필요.

합성 trace(두 객체가 서로 접근 -> 붙어 있다가 -> 이탈)로 설계 약속을 검증한다:
  - kind 필터: collisions CSV에서 ``kind == "object"`` 행만 사건이 된다(벽·stuck 배제)
  - 접촉 구간 실측: 초 단위로 잘린 CSV 시각이 아니라 거리 시계열의 문턱 하회/회복
    순간으로 [t_touch, t_release]를 잡는다
  - 최소 절제: no_contact가 고정 1초가 아니라 실측 접촉 구간만 도려낸다
  - 조건 게이트: 조건이 주장하는 내용을 만족하지 못하는 창은 폐기되고 사유가 집계된다
  - 순도: 창에 다른 쌍의 객체-객체 접촉이 겹치면 폐기, 벽 접촉은 무관
  - 사건 속성: contact_class(short/long)·chained 플래그가 클립까지 전달된다
  - **v3.2 플라토 기준 경계**: no_approach는 완전히 붙은 프레임(플라토 안)에서 열리고
    no_aftermath는 아직 붙어 있는 프레임에서 닫힌다. 문턱 통과 시각만 보던 v3.1이
    이 사건에서 잔재를 남겼을 것임을 함께 고정한다 (WindowBoundaryTest)
  - **플라토가 가드보다 짧으면** 두 조건이 폐기되고 나머지는 영향받지 않는다 (HoldGuardTest)
  - **overlay_overlap**: 절단과 분리된 후처리로 재부여 가능 (OverlayFlagTest)
  - **제3객체 근접 게이트**: 충돌 반응이 없어 collisions CSV에 안 남는 개입도 trace
    기하로 잡는다 — 기준 쌍 두 객체 각각에 대해 잰다 (ThirdObjectGateTest)
  - ffmpeg 명령 구조(단일 구간 = concat n=1, 스플라이스 = n=2)
"""
import datetime
import math
import unittest
from pathlib import Path

from gist.netai.time_travel_summarization.automation.phase_clips import (
    CONTACT_CLASS_BOUNDARY_S,
    DEFAULT_HOLD_GUARD_S,
    DEFAULT_TOUCH_THRESHOLD,
    PLATEAU_MARGIN_CM,
    assign_overlay_flags,
    contact_class,
    contact_events,
    ffmpeg_cmd,
    gate_window,
    mark_chained,
    measure_contact,
    min_pair_distance,
    near_miss_events,
    no_contact_segments,
    object_contact_clusters,
    pair_series,
    plan_episode,
    trace_frames,
    window_specs,
)

_FMT = "%Y-%m-%d %H:%M:%S.%f"
BASE = datetime.datetime(2026, 1, 1, 12, 0, 0)
HZ = 60.0
THR = DEFAULT_TOUCH_THRESHOLD   # 90.0 cm


def _ts(sec: float) -> str:
    return (BASE + datetime.timedelta(seconds=sec)).strftime(_FMT)[:-3]


def _at(sec: float) -> datetime.datetime:
    return BASE + datetime.timedelta(seconds=sec)


def _col_row(sec: float, objid: str, kind: str = "object") -> dict:
    """collisions CSV 한 행. 실물과 같이 **초 단위로 절삭된** 시각을 쓴다."""
    clock = (BASE + datetime.timedelta(seconds=sec)).strftime("%H:%M:%S")
    return {"timestamp": clock, "objid": objid, "x": "0", "y": "90", "z": "0", "kind": kind}


def _profile(t: float, t_touch: float, t_release: float, far: float = 400.0,
             close: float = 72.0, approach_s: float = 4.0) -> float:
    """시각 t에서의 쌍 거리 — 접촉 구간에서 close, 그 밖에서는 approach_s에 걸쳐 far까지.

    접촉 구간 전체가 close로 평평하므로 이 픽스처에서는 플라토 = 접촉 구간이다.
    문턱↔플라토 사이의 진입/이탈 구간을 재현하려면 _plateau_trace를 쓴다.
    """
    if t_touch <= t < t_release:
        return close
    away = (t_touch - t) if t < t_touch else (t - t_release)
    return min(far, THR + (far - THR) * min(1.0, away / approach_s))


def _plateau_trace(t_touch: float, t_hold_start: float, t_hold_end: float, t_release: float,
                   close: float = 72.0, far: float = 400.0, span_s: float = 30.0,
                   pair=("obj001", "obj002"), offset_z: float = 0.0) -> list:
    """문턱 통과 -> 밀착(플라토) -> 이탈이 **구분되는** trace.

    실물의 구조를 재현한다: 90cm 문턱을 지난 뒤에도 한동안 더 다가와야(lead_in) 완전히
    붙고, 플라토가 끝난 뒤에도 한동안 멀어져야(lead_out) 문턱을 회복한다. 이 사이
    구간이 v3.1까지 no_approach/no_aftermath에 새어 들던 접근·분리 움직임이다.

    문턱과 플라토 사이의 "어깨" 구간은 **플라토 판정 띠(d_min+3cm)보다 확실히 위**인
    값으로 둔다 — 그래야 인자로 준 t_hold_start/t_hold_end가 곧 측정될 플라토 경계가
    되어 테스트가 의도를 그대로 표현한다. (선형으로 훑어 내리면 띠를 가로지르는
    지점이 인자값보다 앞서 실측 경계가 인자와 어긋난다.)
    """
    a, b = pair
    shoulder = (close + PLATEAU_MARGIN_CM + THR) / 2.0   # 띠 위 · 문턱 아래
    entry = THR - 0.1                                    # 문턱을 막 지난 값
    rows = []
    for i in range(int(span_s * HZ) + 1):
        t = i / HZ
        if t < t_touch:                       # 문턱 밖 접근
            away = t_touch - t
            d = min(far, THR + (far - THR) * min(1.0, away / 4.0))
        elif t < t_hold_start:                # 문턱 통과 후에도 계속 다가오는 중(어깨)
            frac = (t - t_touch) / max(t_hold_start - t_touch, 1e-9)
            d = entry - (entry - shoulder) * frac
        elif t <= t_hold_end:                 # 완전 밀착(플라토)
            d = close
        elif t < t_release:                   # 아직 문턱 안이지만 떨어지는 중(어깨)
            frac = (t - t_hold_end) / max(t_release - t_hold_end, 1e-9)
            d = shoulder + (entry - shoulder) * frac
        else:                                 # 문턱 회복 후 이탈
            away = t - t_release
            d = min(far, THR + (far - THR) * min(1.0, away / 4.0))
        half = d / 2.0
        for objid, sign in ((a, -1.0), (b, +1.0)):
            rows.append({"timestamp": _ts(t), "objid": objid,
                         "x": f"{sign * half}", "y": "90", "z": f"{offset_z}"})
    return rows


def _chained_trace(first=(15.0, 15.2), second=(16.4, 16.6), span_s: float = 30.0) -> list:
    """세 객체 연쇄 접촉 — obj002가 가만히 있고 obj001, 이어서 obj003이 부딪힌다.

    한 객체의 궤적을 한 곳에서만 정의한다. ``_contact_trace``를 두 번 이어붙이면 공유
    객체(obj002)의 좌표가 같은 타임스탬프에 두 번 기록돼 뒤엣것이 앞엣것을 덮어써
    첫 번째 쌍의 기하가 사라진다 — 연쇄 사건 픽스처는 그래서 따로 만든다.
    """
    rows = []
    for i in range(int(span_s * HZ) + 1):
        t = i / HZ
        d1 = _profile(t, *first)
        d3 = _profile(t, *second)
        for objid, x in (("obj002", 0.0), ("obj001", -d1), ("obj003", +d3)):
            rows.append({"timestamp": _ts(t), "objid": objid,
                         "x": f"{x}", "y": "90", "z": "0"})
    return rows


def _contact_trace(t_touch: float, t_release: float, pair=("obj001", "obj002"),
                   far: float = 400.0, close: float = 72.0, span_s: float = 30.0,
                   approach_s: float = 4.0, offset_z: float = 0.0) -> list:
    """두 객체가 x축을 따라 접근 -> [t_touch, t_release] 동안 밀착 -> 이탈하는 trace.

    거리는 접촉 구간에서 ``close``(실물의 d_min 72cm 수준), 그 바깥에서는 시간에
    비례해 멀어져 approach_s초 전후로 ``far``에 이른다. 문턱(90cm) 하회/회복 순간이
    정확히 t_touch / t_release가 되도록 접촉 구간 경계에서만 값이 바뀐다.
    """
    a, b = pair
    rows = []
    for i in range(int(span_s * HZ) + 1):
        t = i / HZ
        half = _profile(t, t_touch, t_release, far, close, approach_s) / 2.0
        for objid, sign in ((a, -1.0), (b, +1.0)):
            rows.append({"timestamp": _ts(t), "objid": objid,
                         "x": f"{sign * half}", "y": "90", "z": f"{offset_z}"})
    return rows


def _near_miss_trace(d_min: float, t_min_s: float = 15.0, span_s: float = 30.0,
                     speed: float = 60.0) -> list:
    """두 객체가 x축에서 접근 -> 극소(d_min) -> 이탈. 접촉 없음. speed = 각자 cm/s
    (반경 속도 반전폭 = 4·speed: 60 → 240, 정상 스침 범위)."""
    rows = []
    for i in range(int(span_s * HZ) + 1):
        t = i / HZ
        half = d_min / 2 + speed * abs(t - t_min_s)
        for objid, sign in (("obj001", -1.0), ("obj002", +1.0)):
            rows.append({"timestamp": _ts(t), "objid": objid,
                         "x": f"{sign * half}", "y": "90", "z": "0"})
    return rows


class KindFilterTest(unittest.TestCase):
    """v2 결함 1 — 벽 접촉을 사건으로 취급하던 문제가 재발하지 않는지."""

    def test_wall_and_stuck_rows_are_not_events(self):
        rows = [_col_row(5.0, "obj001", "wall"), _col_row(6.0, "obj002", "stuck"),
                _col_row(7.0, "obj003", "wall")]
        events, reasons, ignored = object_contact_clusters(rows, BASE)
        self.assertEqual(events, [])
        self.assertEqual(ignored, 3)
        self.assertEqual(sum(reasons.values()), 0)

    def test_object_pair_becomes_one_event(self):
        rows = [_col_row(5.0, "obj001", "wall"),
                _col_row(15.0, "obj001"), _col_row(15.0, "obj002"),
                _col_row(15.3, "obj001")]
        events, _, ignored = object_contact_clusters(rows, BASE)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["pair"], ("obj001", "obj002"))
        self.assertEqual(ignored, 1)

    def test_three_body_cluster_dropped(self):
        rows = [_col_row(15.0, "obj001"), _col_row(15.0, "obj002"), _col_row(15.1, "obj003")]
        events, reasons, _ = object_contact_clusters(rows, BASE)
        self.assertEqual(events, [])
        self.assertEqual(reasons["pair_not_two_objects"], 1)


class MeasureContactTest(unittest.TestCase):
    """v2 결함 2 — 초 단위 CSV 시각을 기준점으로 쓰던 문제."""

    def setUp(self):
        self.frames = trace_frames(_contact_trace(t_touch=15.4, t_release=16.1))
        self.series = pair_series(self.frames, "obj001", "obj002")

    def test_touch_and_release_measured_from_geometry(self):
        # CSV는 15.4초 접촉을 "15초"로 기록한다 — 기준점이 0.4초 어긋난 상태에서 출발.
        measured, why = measure_contact(self.series, _at(15.0), THR)
        self.assertIsNone(why)
        self.assertAlmostEqual((measured["t_touch"] - BASE).total_seconds(), 15.4, delta=1.5 / HZ)
        self.assertAlmostEqual((measured["t_release"] - BASE).total_seconds(), 16.1, delta=1.5 / HZ)
        self.assertAlmostEqual(measured["contact_len_s"], 0.7, delta=2.0 / HZ)

    def test_touch_may_precede_recorded_second(self):
        """실측 사례처럼 참 접촉이 기록된 초보다 앞설 수 있다 — 뒤쪽 탐색이 필요."""
        frames = trace_frames(_contact_trace(t_touch=14.95, t_release=15.3))
        series = pair_series(frames, "obj001", "obj002")
        measured, why = measure_contact(series, _at(15.0), THR)
        self.assertIsNone(why)
        self.assertLess((measured["t_touch"] - _at(15.0)).total_seconds(), 0.0)

    def test_release_beyond_search_window(self):
        """접촉이 길어 회복이 탐색창(+1.5s) 밖이어도 t_release를 끝까지 따라간다."""
        frames = trace_frames(_contact_trace(t_touch=15.2, t_release=17.4))
        series = pair_series(frames, "obj001", "obj002")
        measured, why = measure_contact(series, _at(15.0), THR)
        self.assertIsNone(why)
        self.assertAlmostEqual(measured["contact_len_s"], 2.2, delta=2.0 / HZ)

    def test_no_release_within_cap_is_dropped(self):
        frames = trace_frames(_contact_trace(t_touch=15.0, t_release=25.0))
        series = pair_series(frames, "obj001", "obj002")
        measured, why = measure_contact(series, _at(15.0), THR, max_contact_s=3.0)
        self.assertIsNone(measured)
        self.assertEqual(why, "no_release_within_cap")

    def test_no_touch_in_window_is_dropped(self):
        """CSV가 사건이 있다고 하는데 그 근처 기하에 접촉이 없으면 사건을 만들지 않는다."""
        frames = trace_frames(_contact_trace(t_touch=25.0, t_release=25.5))
        series = pair_series(frames, "obj001", "obj002")
        measured, why = measure_contact(series, _at(15.0), THR)
        self.assertIsNone(measured)
        self.assertEqual(why, "no_touch_in_search_window")

    def test_contact_events_uses_csv_only_for_pair_and_existence(self):
        col = [_col_row(15.0, "obj001"), _col_row(15.0, "obj002"), _col_row(3.0, "obj001", "wall")]
        events, stats = contact_events(self.frames, col, BASE, THR)
        self.assertEqual(len(events), 1)
        self.assertEqual(stats["passed"], 1)
        self.assertEqual(stats["ignored_rows"], 1)
        self.assertAlmostEqual((events[0]["t_touch"] - BASE).total_seconds(), 15.4, delta=1.5 / HZ)


class ClusteringContaminationTest(unittest.TestCase):
    """진단 리포트 §3의 실제 사례 고정 — 같은 초의 벽 충돌 행이 기준점을 끌어당기던 문제.

    v2에서는 collisions CSV의 행을 kind와 무관하게 시간 근접성만으로 묶어, ep_0000의
    14:16:27 사건(obj002-obj003 접촉이 그 초의 +0.700초)이 같은 초 +0.233초에 기록된
    obj001의 벽 충돌과 한 클러스터가 되고 대표 시각으로 더 이른 벽 충돌 쪽이 채택돼
    기준점이 0.467초 앞으로 밀렸다. 꼬리 오차 12건 중 9건이 이 원인이었다.
    """

    def _episode(self):
        """obj002-obj003이 +0.700초에 접촉하고, 같은 초 +0.233초에 obj001 벽 충돌."""
        trace = _contact_trace(t_touch=15.7, t_release=15.95, pair=("obj002", "obj003"))
        trace += _contact_trace(t_touch=99.0, t_release=99.1, pair=("obj001", "obj004"),
                                offset_z=300.0)   # 관여하지 않는 두 객체(범위 밖 접촉)
        col = [_col_row(15.233, "obj001", "wall"),
               _col_row(15.7, "obj002"), _col_row(15.7, "obj003")]
        return trace, col

    def test_wall_row_does_not_join_the_object_cluster(self):
        _, col = self._episode()
        events, reasons, ignored = object_contact_clusters(col, BASE)
        self.assertEqual(len(events), 1)
        # 쌍이 벽 행의 obj001에 오염되지 않는다 (v2라면 3개 objid가 섞여 들어왔다)
        self.assertEqual(events[0]["pair"], ("obj002", "obj003"))
        self.assertEqual(events[0]["n_rows"], 2)
        self.assertEqual(ignored, 1)
        self.assertEqual(sum(reasons.values()), 0)

    def test_reference_time_is_not_pulled_to_the_wall_row(self):
        trace, col = self._episode()
        events, _ = contact_events(trace_frames(trace), col, BASE, THR)
        self.assertEqual(len(events), 1)
        touch_off = (events[0]["t_touch"] - BASE).total_seconds()
        # 참 접촉(15.700)을 가리켜야 한다 — 벽 충돌 시각(15.233)이 아니라.
        self.assertAlmostEqual(touch_off, 15.7, delta=1.5 / HZ)
        self.assertGreater(abs(touch_off - 15.233), 0.4)

    def test_windows_anchor_on_the_object_contact(self):
        trace, col = self._episode()
        plans, _ = plan_episode("collision", trace, col, BASE, 30.0, n_control=0)
        full = [p for p in plans if p["condition"] == "full"]
        self.assertEqual(len(full), 1)
        # full 창 = [t_touch-1.0, t_touch+1.0] = [14.7, 16.7]. v2였다면 0.467초 앞이었다.
        self.assertAlmostEqual((full[0]["segments"][0][0] - BASE).total_seconds(), 14.7,
                               delta=1.5 / HZ)


class ContactClassTest(unittest.TestCase):
    """진단 §4의 접촉 길이 이봉성을 사건 속성으로 기록한다."""

    def test_boundary_classification(self):
        self.assertEqual(contact_class(0.134), "short")    # 짧은 군 중앙값
        self.assertEqual(contact_class(1.300), "long")     # 긴 군 중앙값
        self.assertEqual(contact_class(CONTACT_CLASS_BOUNDARY_S), "long")
        self.assertEqual(contact_class(CONTACT_CLASS_BOUNDARY_S - 0.001), "short")

    def test_short_contact_recorded_on_every_clip(self):
        trace = _contact_trace(t_touch=15.0, t_release=15.2)
        col = [_col_row(15.0, "obj001"), _col_row(15.0, "obj002")]
        plans, stats = plan_episode("collision", trace, col, BASE, 30.0, n_control=0)
        self.assertTrue(plans)
        for p in plans:
            self.assertEqual(p["contact_class"], "short", p["condition"])
        self.assertEqual(stats["_events"]["long_contacts"], 0)

    def test_long_contact_is_kept_in_no_contact_and_flagged(self):
        """긴 접촉을 no_contact에서 배제하지 않는다 — 클래스로 표시만 한다."""
        trace = _contact_trace(t_touch=15.0, t_release=16.3)   # 1.3초 = 긴 접촉
        col = [_col_row(15.0, "obj001"), _col_row(15.0, "obj002")]
        plans, stats = plan_episode("collision", trace, col, BASE, 30.0, n_control=0)
        nc = [p for p in plans if p["condition"] == "no_contact"]
        self.assertEqual(len(nc), 1)
        self.assertEqual(nc[0]["contact_class"], "long")
        self.assertAlmostEqual(nc[0]["contact_len_s"], 1.3, delta=2.0 / HZ)
        self.assertEqual(stats["_events"]["long_contacts"], 1)
        # 실측 절제라 긴 접촉도 전부 덮인다(총장 2초는 유지).
        self.assertIsNone(gate_window("no_contact", nc[0]["segments"],
                                      pair_series(trace_frames(trace), "obj001", "obj002"), THR))
        total = sum((e - s).total_seconds() for s, e in nc[0]["segments"])
        self.assertAlmostEqual(total, 2.0, places=6)
        # 이음새에서 건너뛰는 시간이 접촉 길이 + 앞 패드만큼 커진다(독스트링의 주의 사항)
        jump = (nc[0]["segments"][1][0] - nc[0]["segments"][0][1]).total_seconds()
        self.assertAlmostEqual(jump, 1.4, delta=2.0 / HZ)


class ChainedFlagTest(unittest.TestCase):
    """진단 §5-5 — 세 물체가 1.5초 안에 연달아 부딪히는 사건은 분석에서 분리한다."""

    def _ev(self, touch: float, release: float, pair):
        return {"pair": pair, "t_touch": _at(touch), "t_release": _at(release)}

    def test_shared_object_within_window_marks_both(self):
        events = [self._ev(15.0, 15.2, ("obj001", "obj002")),
                  self._ev(16.0, 16.2, ("obj002", "obj003"))]
        mark_chained(events)
        self.assertTrue(all(e["chained"] for e in events))

    def test_shared_object_outside_window_is_not_chained(self):
        events = [self._ev(15.0, 15.2, ("obj001", "obj002")),
                  self._ev(20.0, 20.2, ("obj002", "obj003"))]
        mark_chained(events)
        self.assertFalse(any(e["chained"] for e in events))

    def test_disjoint_pairs_are_not_chained(self):
        """객체를 공유하지 않으면 시간이 가까워도 연쇄가 아니다(창 오염은 순도가 본다)."""
        events = [self._ev(15.0, 15.2, ("obj001", "obj002")),
                  self._ev(15.5, 15.7, ("obj003", "obj004"))]
        mark_chained(events)
        self.assertFalse(any(e["chained"] for e in events))

    def test_flag_reaches_clips_and_stats(self):
        """연쇄 사건은 폐기되지 않고 chained=True를 달고 클립까지 실려 나간다."""
        trace = _chained_trace(first=(15.0, 15.2), second=(16.4, 16.6))
        col = [_col_row(15.0, "obj001"), _col_row(15.0, "obj002"),
               _col_row(16.4, "obj002"), _col_row(16.4, "obj003")]
        plans, stats = plan_episode("collision", trace, col, BASE, 30.0, n_control=0)
        self.assertEqual(stats["_events"]["passed"], 2)
        self.assertEqual(stats["_events"]["chained"], 2)
        self.assertTrue(plans)
        self.assertTrue(all(p["chained"] for p in plans))

    def test_isolated_contact_is_not_flagged(self):
        trace = _contact_trace(t_touch=15.0, t_release=15.2)
        col = [_col_row(15.0, "obj001"), _col_row(15.0, "obj002")]
        plans, stats = plan_episode("collision", trace, col, BASE, 30.0, n_control=0)
        self.assertEqual(stats["_events"]["chained"], 0)
        self.assertFalse(any(p["chained"] for p in plans))


class MinimalExcisionTest(unittest.TestCase):
    """v3 설계 3 — 고정 1초 절제 금지, 실측 접촉 구간만 제거.

    v3.1에서 뒤 패드가 0이 되어(뒤 구간이 t_release에서 바로 시작) 절제량은
    접촉 구간 길이 + 앞 패드 0.1초다.
    """

    def test_short_contact_removes_little(self):
        segs = no_contact_segments(_at(15.0), _at(15.2))
        (s0, e0), (s1, e1) = segs
        self.assertAlmostEqual((e0 - BASE).total_seconds(), 14.9, places=6)
        # v3.1: 뒤 구간은 t_release에서 바로 시작한다(v3는 15.3이었다)
        self.assertAlmostEqual((s1 - BASE).total_seconds(), 15.2, places=6)
        # 잘라낸 총 길이 = 접촉 구간(0.2s) + 앞 패드(0.1s) = 0.3s
        self.assertAlmostEqual((s1 - e0).total_seconds(), 0.3, places=6)

    def test_long_contact_removes_exactly_its_length(self):
        segs = no_contact_segments(_at(15.0), _at(16.4))
        (_, e0), (s1, _) = segs
        self.assertAlmostEqual((s1 - BASE).total_seconds(), 16.4, places=6)
        self.assertAlmostEqual((s1 - e0).total_seconds(), 1.5, places=6)

    def test_total_length_is_two_seconds(self):
        segs = no_contact_segments(_at(15.0), _at(16.4))
        total = sum((e - s).total_seconds() for s, e in segs)
        self.assertAlmostEqual(total, 2.0, places=6)


class GateTest(unittest.TestCase):
    """v3 설계 5 — 조건 의미 검증 게이트. 기준 쌍의 거리 시계열만 본다."""

    def setUp(self):
        self.frames = trace_frames(_contact_trace(t_touch=15.0, t_release=15.6))
        self.series = pair_series(self.frames, "obj001", "obj002")

    def _segs(self, *pairs):
        return [(_at(s), _at(e)) for s, e in pairs]

    # setUp의 접촉 구간은 [15.0, 15.6]이다.

    def test_contact_conditions_require_contact_frames(self):
        """접촉 프레임이 아예 없는 창은 세 조건 모두에서 폐기된다."""
        for cond in ("full", "no_approach", "no_aftermath"):
            self.assertEqual(
                gate_window(cond, self._segs((10.0, 12.0)), self.series, THR),
                "no_contact_frames_in_window")

    def test_full_accepts_contact_in_the_middle(self):
        self.assertIsNone(gate_window("full", self._segs((14.0, 16.0)), self.series, THR))

    def test_no_approach_requires_contact_at_window_start(self):
        """v3.1 — 창이 접촉 시작 뒤에서 열려야 한다(접근 프레임 0)."""
        self.assertIsNone(gate_window("no_approach", self._segs((15.05, 17.05)),
                                      self.series, THR))

    def test_no_approach_rejects_approach_frames_at_start(self):
        """v3식 [t_touch-0.1, ...]은 접근 잔재를 남긴다 — 게이트가 잡아낸다."""
        self.assertEqual(
            gate_window("no_approach", self._segs((14.9, 16.9)), self.series, THR),
            "window_start_outside_plateau")

    def test_no_aftermath_requires_contact_at_window_end(self):
        """v3.1 — 아직 붙어 있는 프레임에서 창이 닫혀야 한다(분리 장면 0)."""
        self.assertIsNone(gate_window("no_aftermath", self._segs((13.55, 15.55)),
                                      self.series, THR))

    def test_no_aftermath_rejects_separation_frames_at_end(self):
        """v3식 [..., t_release+0.1]은 떨어지기 시작하는 장면을 담는다 — 게이트가 잡아낸다."""
        self.assertEqual(
            gate_window("no_aftermath", self._segs((13.7, 15.7)), self.series, THR),
            "window_end_outside_plateau")

    def test_plateau_gate_is_stricter_than_threshold_gate(self):
        """v3.2 핵심 — 문턱은 통과하지만 플라토 밖인 창을 hold_thr가 잡아낸다.

        문턱 통과(15.0) 뒤 0.3초에 걸쳐 더 다가와 15.3에 완전히 붙는 사건에서,
        창을 15.1에 열면 아직 다가오는 중이다. 문턱 기준(v3.1)으로는 이미 접촉이라
        통과하지만, 플라토 기준(v3.2)으로는 폐기된다.
        """
        series = pair_series(
            trace_frames(_plateau_trace(t_touch=15.0, t_hold_start=15.3,
                                        t_hold_end=15.6, t_release=15.9)),
            "obj001", "obj002")
        segs = self._segs((15.1, 17.1))
        self.assertIsNone(gate_window("no_approach", segs, series, THR))          # 문턱 기준
        self.assertEqual(gate_window("no_approach", segs, series, THR, hold_thr=75.0),
                         "window_start_outside_plateau")                          # 플라토 기준

    def test_plateau_gate_accepts_window_opening_inside_plateau(self):
        series = pair_series(
            trace_frames(_plateau_trace(t_touch=15.0, t_hold_start=15.3,
                                        t_hold_end=15.6, t_release=15.9)),
            "obj001", "obj002")
        self.assertIsNone(gate_window("no_approach", self._segs((15.35, 17.35)),
                                      series, THR, hold_thr=75.0))
        self.assertIsNone(gate_window("no_aftermath", self._segs((13.55, 15.55)),
                                      series, THR, hold_thr=75.0))

    def test_aftermath_only_passes_on_separating_window(self):
        # t_release(16.1) 직후 2초 — 접촉 프레임 없음 + 분리 운동(90→~245cm) 충분
        self.assertIsNone(gate_window("aftermath_only", self._segs((16.15, 18.15)), self.series, THR))

    def test_aftermath_only_rejects_contact_frames(self):
        self.assertEqual(
            gate_window("aftermath_only", self._segs((15.5, 17.5)), self.series, THR),
            "contact_frames_in_window")

    def test_aftermath_only_rejects_stationary_aftermath(self):
        # 사건에서 먼 구간은 거리가 far(400)로 평평 — "붙어서 정지"형과 같은 무분리 사후
        self.assertEqual(
            gate_window("aftermath_only", self._segs((24.0, 26.0)), self.series, THR),
            "no_separation_motion")

    def test_approach_only_passes_on_closing_window(self):
        self.assertIsNone(gate_window("approach_only", self._segs((12.8, 14.8)), self.series, THR))

    def test_approach_only_rejects_contact_frames(self):
        self.assertEqual(
            gate_window("approach_only", self._segs((14.5, 16.5)), self.series, THR),
            "contact_frames_in_window")

    def test_approach_only_rejects_receding_window(self):
        """접촉 이후 멀어지는 구간은 '접근'이 아니다 — 종점 거리가 시점보다 크다."""
        self.assertEqual(
            gate_window("approach_only", self._segs((16.0, 18.0)), self.series, THR),
            "not_closing")

    def test_no_contact_passes_when_excision_worked(self):
        segs = no_contact_segments(_at(15.0), _at(15.6))
        self.assertIsNone(gate_window("no_contact", segs, self.series, THR))

    def test_no_contact_rejects_surviving_contact_frames(self):
        """v2식 고정 절제(±0.5s)는 0.6초짜리 접촉을 다 못 지운다 — 게이트가 잡아낸다."""
        segs = self._segs((13.5, 14.5), (15.5, 16.5))
        self.assertEqual(gate_window("no_contact", segs, self.series, THR),
                         "contact_frames_survived_excision")

    def _near_miss_gate(self, d_min: float):
        """실제로 d_min까지 접근하는 trace를 만들어 그 값으로 게이트를 돌린다."""
        series = pair_series(trace_frames(_near_miss_trace(d_min=d_min, t_min_s=15.0)),
                             "obj001", "obj002")
        return gate_window("near_miss", self._segs((14.0, 16.0)), series, THR,
                           gap=95.0, d_min=d_min)

    def test_near_miss_gate_accepts_grazing_pass(self):
        self.assertIsNone(self._near_miss_gate(95.0))

    def test_near_miss_gate_rejects_too_close(self):
        """gap 아래로 들어간 조우는 접촉 의심 — near-miss로 인정하지 않는다."""
        self.assertEqual(self._near_miss_gate(80.0), "d_min_below_gap")

    def test_near_miss_gate_rejects_too_far(self):
        """스쳤다고 볼 수 없을 만큼 먼 조우(문턱+30cm 초과)도 배제한다."""
        self.assertEqual(self._near_miss_gate(150.0), "d_min_too_far")

    def test_near_miss_gate_rejects_velocity_reversal(self):
        """gap 바로 위(d_min ≥ gap)라도 극소점에서 반경 속도가 한 표본에 400cm/s 이상
        뒤집히면 응급 이탈(trace 표본화가 gap 침범을 놓친 경우)로 보고 폐기한다."""
        series = pair_series(trace_frames(_near_miss_trace(d_min=95.2, speed=150.0)),
                             "obj001", "obj002")            # 반전폭 600
        segs = self._segs((14.0, 16.0))
        self.assertEqual(gate_window("near_miss", segs, series, THR, gap=95.0, d_min=95.2),
                         "velocity_reversal")
        # 문턱 0 = 게이트 끔(구세대 원료 등 안무별 재조정용)
        self.assertIsNone(gate_window("near_miss", segs, series, THR, gap=95.0, d_min=95.2,
                                      reversal_jump=0.0))

    def test_near_miss_gate_rejects_window_without_minimum(self):
        """창이 극소점을 담지 않으면(창 안 최솟값이 d_min보다 크면) 폐기."""
        series = pair_series(trace_frames(_near_miss_trace(d_min=95.0, t_min_s=15.0)),
                             "obj001", "obj002")
        self.assertEqual(
            gate_window("near_miss", self._segs((5.0, 7.0)), series, THR, gap=95.0, d_min=95.0),
            "minimum_outside_window")

    def test_control_has_no_gate(self):
        self.assertIsNone(gate_window("control", self._segs((5.0, 7.0)), self.series, THR))


class NearMissEventTest(unittest.TestCase):
    def test_detects_minimum(self):
        frames = trace_frames(_near_miss_trace(d_min=95.0, t_min_s=15.0))
        events = near_miss_events(frames, gap=95.0)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertAlmostEqual(ev["d_min"], 95.0, delta=1.0)
        self.assertAlmostEqual((ev["t"] - BASE).total_seconds(), 15.0, delta=0.1)
        self.assertEqual(ev["pair"], ("obj001", "obj002"))

    def test_far_pair_no_event(self):
        frames = trace_frames(_near_miss_trace(d_min=400.0))   # 진입 문턱 190 밖
        self.assertEqual(near_miss_events(frames, gap=95.0), [])


class PlanEpisodeTest(unittest.TestCase):
    def test_collision_all_conditions_pass(self):
        trace = _contact_trace(t_touch=15.4, t_release=16.0)
        col = [_col_row(15.0, "obj001"), _col_row(15.0, "obj002")]
        plans, stats = plan_episode("collision", trace, col, BASE, 30.0, n_control=0)
        conds = sorted(p["condition"] for p in plans)
        # no_contact 반경 계열(bfdf112, NO_CONTACT_RADII)이 기본 조건에 포함된다
        self.assertEqual(conds, ["aftermath_only", "approach_only", "full",
                                 "no_aftermath", "no_approach", "no_contact",
                                 "no_contact-100", "no_contact-110",
                                 "no_contact-120", "no_contact-80", "no_contact-plateau"])
        for cond in conds:
            self.assertEqual(stats[cond]["passed"], 1, cond)
            self.assertEqual(sum(stats[cond]["dropped"].values()), 0, cond)
        self.assertEqual(stats["_events"]["passed"], 1)

    def test_windows_anchor_on_measured_times(self):
        trace = _contact_trace(t_touch=15.4, t_release=16.0)
        col = [_col_row(15.0, "obj001"), _col_row(15.0, "obj002")]
        plans, _ = plan_episode("collision", trace, col, BASE, 30.0, n_control=0)
        by = {p["condition"]: p for p in plans}
        tol = 1.5 / HZ

        def off(t):
            return (t - BASE).total_seconds()

        # v3.1 규격. t_touch=15.4, t_release=16.0.
        # full: [t_touch-1.0, t_touch+1.0] (v3.1에서 불변)
        self.assertAlmostEqual(off(by["full"]["segments"][0][0]), 14.4, delta=tol)
        # no_approach: [t_touch+0.05, t_touch+2.05] — 접촉이 이미 시작된 뒤에서 연다
        self.assertAlmostEqual(off(by["no_approach"]["segments"][0][0]), 15.45, delta=tol)
        self.assertAlmostEqual(off(by["no_approach"]["segments"][0][1]), 17.45, delta=tol)
        # no_aftermath: [t_hold_end-2.05, t_hold_end-0.05]. 이 픽스처는 접촉 구간이
        # 통째로 평평해 플라토 끝 = t_release 직전 프레임이다(기준은 CSV 시각 15.0이
        # 아니라 실측값이라는 점이 요지).
        self.assertEqual(by["no_aftermath"]["anchor"], "t_hold_end")
        hold_end = off(by["no_aftermath"]["t_hold_end"])
        self.assertAlmostEqual(off(by["no_aftermath"]["segments"][0][1]), hold_end - 0.05,
                               delta=1e-6)
        self.assertAlmostEqual(hold_end, 16.0, delta=2.0 / HZ)
        # approach_only(v3.4): [t_touch-2.05+1/30, t_touch-0.05+1/30] — 1프레임 접촉 쪽 이동
        self.assertAlmostEqual(off(by["approach_only"]["segments"][0][1]), 15.35 + 1.0 / 30.0, delta=tol)
        self.assertAlmostEqual(off(by["approach_only"]["segments"][0][0]), 13.35 + 1.0 / 30.0, delta=tol)
        # 접촉 구간 길이가 manifest용으로 실려 있다
        self.assertAlmostEqual(by["full"]["contact_len_s"], 0.6, delta=2.0 / HZ)
        self.assertEqual(by["full"]["pair"], ["obj001", "obj002"])

    def test_wall_only_episode_yields_no_collision_conditions(self):
        """벽 접촉만 있는 에피소드에서는 접촉 기준 조건이 하나도 안 나온다(v2 대비 핵심)."""
        trace = _contact_trace(t_touch=15.4, t_release=16.0)
        col = [_col_row(15.0, "obj001", "wall"), _col_row(15.0, "obj002", "wall")]
        plans, stats = plan_episode("collision", trace, col, BASE, 30.0, n_control=0)
        self.assertEqual(plans, [])
        self.assertEqual(stats["_events"]["planned"], 0)
        self.assertEqual(stats["_events"]["ignored_rows"], 2)

    def test_bounds_filter(self):
        """접촉이 t=1.4s면 approach_only([-2.05,-0.05])는 에피소드 범위 밖 -> 제외."""
        trace = _contact_trace(t_touch=1.4, t_release=1.8)
        col = [_col_row(1.0, "obj001"), _col_row(1.0, "obj002")]
        plans, stats = plan_episode("collision", trace, col, BASE, 30.0, n_control=0)
        conds = [p["condition"] for p in plans]
        self.assertNotIn("approach_only", conds)
        self.assertEqual(stats["approach_only"]["dropped"]["out_of_episode_bounds"], 1)
        self.assertIn("no_approach", conds)

    def test_purity_other_object_contact_drops_window(self):
        """다른 쌍의 객체-객체 접촉이 창에 겹치면 폐기된다."""
        trace = _contact_trace(t_touch=15.2, t_release=15.6)
        trace += _contact_trace(t_touch=16.0, t_release=16.4,
                                pair=("obj003", "obj004"), offset_z=50.0)
        col = [_col_row(15.0, "obj001"), _col_row(15.0, "obj002"),
               _col_row(16.0, "obj003"), _col_row(16.0, "obj004")]
        plans, stats = plan_episode("collision", trace, col, BASE, 30.0, n_control=0)
        full_for_first = [p for p in plans
                          if p["condition"] == "full" and p["pair"] == ["obj001", "obj002"]]
        self.assertEqual(full_for_first, [])
        self.assertGreaterEqual(stats["full"]["dropped"]["purity_other_object_contact"], 1)

    def test_wall_contact_does_not_break_purity(self):
        """같은 창에 벽 접촉이 있어도 순도는 유지된다(모듈 독스트링의 순도 정의)."""
        trace = _contact_trace(t_touch=15.2, t_release=15.6)
        col = [_col_row(15.0, "obj001"), _col_row(15.0, "obj002"),
               _col_row(15.0, "obj003", "wall"), _col_row(16.0, "obj004", "wall")]
        plans, stats = plan_episode("collision", trace, col, BASE, 30.0, n_control=0)
        self.assertEqual(stats["full"]["passed"], 1)
        self.assertTrue(any(p["condition"] == "full" for p in plans))

    def test_nearmiss_contaminated_dropped(self):
        """near-miss 창 안에 실충돌(v3 안무의 3+객체 한계)이 있으면 폐기."""
        trace = _near_miss_trace(d_min=95.0, t_min_s=15.0)
        trace += _contact_trace(t_touch=15.3, t_release=15.7,
                                pair=("obj003", "obj004"), offset_z=50.0)
        col = [_col_row(15.0, "obj003"), _col_row(15.0, "obj004")]
        plans, stats = plan_episode("nearmiss", trace, col, BASE, 30.0, n_control=0)
        self.assertEqual([p for p in plans if p["condition"] == "near_miss"], [])
        self.assertGreaterEqual(stats["near_miss"]["dropped"]["purity_other_object_contact"], 1)

    def test_nearmiss_clean_kept_with_control(self):
        trace = _near_miss_trace(d_min=95.0, t_min_s=15.0)
        plans, stats = plan_episode("nearmiss", trace, [], BASE, 30.0, gap=95.0, n_control=1)
        nm = [p for p in plans if p["condition"] == "near_miss"]
        ctrl = [p for p in plans if p["condition"] == "control"]
        self.assertEqual(len(nm), 1)
        self.assertAlmostEqual(nm[0]["d_min"], 95.0, delta=1.0)
        self.assertEqual(stats["near_miss"]["passed"], 1)
        self.assertEqual(len(ctrl), 1)
        gap_s = abs((ctrl[0]["t_ref"] - nm[0]["t_ref"]).total_seconds())
        self.assertGreater(gap_s, 3.0)

    def test_nearmiss_touching_pair_dropped_by_gate(self):
        """gap보다 훨씬 가까이 지나간 조우는 near-miss가 아니다 -> 게이트가 폐기."""
        trace = _near_miss_trace(d_min=40.0, t_min_s=15.0)
        plans, stats = plan_episode("nearmiss", trace, [], BASE, 30.0, gap=95.0, n_control=0)
        self.assertEqual([p for p in plans if p["condition"] == "near_miss"], [])
        self.assertEqual(stats["near_miss"]["dropped"]["d_min_below_gap"], 1)


class PlateauMeasurementTest(unittest.TestCase):
    """v3.2-1 — 플라토 경계 실측. 문턱 통과 시각과 구별되는가."""

    def setUp(self):
        self.frames = trace_frames(_plateau_trace(
            t_touch=15.0, t_hold_start=15.3, t_hold_end=15.8, t_release=16.2))
        self.series = pair_series(self.frames, "obj001", "obj002")

    def test_plateau_bounds_differ_from_threshold_bounds(self):
        measured, why = measure_contact(self.series, _at(15.0), THR)
        self.assertIsNone(why)
        off = lambda t: (t - BASE).total_seconds()   # noqa: E731
        self.assertAlmostEqual(off(measured["t_touch"]), 15.0, delta=1.5 / HZ)
        self.assertAlmostEqual(off(measured["t_release"]), 16.2, delta=1.5 / HZ)
        # 플라토는 문턱 구간보다 안쪽이다 — 이 차이가 v3.2가 잡아내려는 잔재다
        self.assertAlmostEqual(off(measured["t_hold_start"]), 15.3, delta=1.5 / HZ)
        self.assertAlmostEqual(off(measured["t_hold_end"]), 15.8, delta=1.5 / HZ)
        self.assertAlmostEqual(measured["hold_len_s"], 0.5, delta=2.0 / HZ)
        self.assertAlmostEqual(measured["contact_len_s"], 1.2, delta=2.0 / HZ)

    def test_hold_thr_is_relative_to_event_dmin(self):
        """접촉 거리가 사건마다 다르므로 플라토 문턱은 d_min 상대값이어야 한다."""
        for close in (72.0, 78.0, 84.0):
            frames = trace_frames(_plateau_trace(
                t_touch=15.0, t_hold_start=15.3, t_hold_end=15.8, t_release=16.2,
                close=close))
            measured, _ = measure_contact(pair_series(frames, "obj001", "obj002"),
                                          _at(15.0), THR)
            self.assertAlmostEqual(measured["d_min"], close, delta=0.5)
            self.assertAlmostEqual(measured["hold_thr"], close + PLATEAU_MARGIN_CM, delta=0.5)
            self.assertAlmostEqual(measured["hold_len_s"], 0.5, delta=2.0 / HZ)

    def test_flat_contact_makes_plateau_equal_contact(self):
        """접촉 구간이 통째로 평평하면 플라토 = 접촉 구간(한 프레임 오차 안)."""
        series = pair_series(trace_frames(_contact_trace(15.0, 15.6)), "obj001", "obj002")
        measured, _ = measure_contact(series, _at(15.0), THR)
        self.assertAlmostEqual(measured["hold_len_s"], measured["contact_len_s"],
                               delta=2.0 / HZ)


class WindowBoundaryTest(unittest.TestCase):
    """v3.2 창 경계가 실제 계획 산출물에서 국면을 정확히 가르는지 확인한다.

    게이트가 같은 성질을 검사하지만, 여기서는 계획된 창의 프레임을 직접 들여다봐
    "경계가 의도한 국면에 놓였다"를 규격 수준에서 고정한다. 픽스처는 문턱 통과와
    완전 밀착이 **구분되는** trace라, v3.1 규격이었다면 no_approach 첫 프레임이
    아직 접근 중이고 no_aftermath 마지막 프레임이 이미 분리 중이었을 사건이다.
    """

    def setUp(self):
        self.t_touch, self.t_hold_start = 15.0, 15.3
        self.t_hold_end, self.t_release = 16.5, 16.9
        trace = _plateau_trace(t_touch=self.t_touch, t_hold_start=self.t_hold_start,
                               t_hold_end=self.t_hold_end, t_release=self.t_release)
        col = [_col_row(15.0, "obj001"), _col_row(15.0, "obj002")]
        plans, self.stats = plan_episode("collision", trace, col, BASE, 30.0, n_control=0)
        self.by = {p["condition"]: p for p in plans}
        self.series = pair_series(trace_frames(trace), "obj001", "obj002")
        self.hold_thr = 72.0 + PLATEAU_MARGIN_CM

    def _samples(self, cond):
        segs = self.by[cond]["segments"]
        return [(t, d) for t, d in self.series if any(s <= t <= e for s, e in segs)]

    def test_no_approach_starts_inside_plateau(self):
        first_t, first_d = self._samples("no_approach")[0]
        self.assertLessEqual(first_d, self.hold_thr)
        self.assertGreaterEqual((first_t - BASE).total_seconds(), self.t_hold_start)
        self.assertEqual(self.by["no_approach"]["anchor"], "t_hold_start")

    def test_no_aftermath_ends_inside_plateau(self):
        last_t, last_d = self._samples("no_aftermath")[-1]
        self.assertLessEqual(last_d, self.hold_thr)
        self.assertLessEqual((last_t - BASE).total_seconds(), self.t_hold_end)
        self.assertEqual(self.by["no_aftermath"]["anchor"], "t_hold_end")

    def test_v31_boundaries_would_have_leaked(self):
        """이 사건에서 v3.1 규격이었다면 잔재가 남았음을 명시적으로 보인다."""
        # v3.1 no_approach 시작 = t_touch + 0.05 = 15.05 → 아직 다가오는 중
        d_at_v31_start = next(d for t, d in self.series
                              if (t - BASE).total_seconds() >= self.t_touch + 0.05)
        self.assertGreater(d_at_v31_start, self.hold_thr)
        # v3.1 no_aftermath 끝 = t_release − 0.05 = 16.85 → 이미 떨어지는 중
        d_at_v31_end = [d for t, d in self.series
                        if (t - BASE).total_seconds() <= self.t_release - 0.05][-1]
        self.assertGreater(d_at_v31_end, self.hold_thr)

    def test_approach_only_one_frame_from_touch_and_anchored(self):
        # v3.4: 창 끝 = t_touch − (0.05 − 1/30) ≈ t_touch − 0.0167 — 접촉 1프레임 앞.
        # trace 기준 접촉 프레임(문턱 하회)은 여전히 0이어야 한다(t_touch가 첫 하회 시각).
        samples = self._samples("approach_only")
        self.assertTrue(all(d >= THR for _, d in samples))
        self.assertEqual(self.by["approach_only"]["anchor"], "t_touch")
        end_off = (self.by["approach_only"]["segments"][0][1] - BASE).total_seconds()
        self.assertAlmostEqual(self.t_touch - end_off, 0.05 - 1.0 / 30.0, delta=1.5 / HZ)

    def test_no_contact_unchanged_and_opens_at_release(self):
        back_start = (self.by["no_contact"]["segments"][1][0] - BASE).total_seconds()
        self.assertAlmostEqual(back_start, self.t_release, delta=1.5 / HZ)
        self.assertTrue(all(d >= THR for _, d in self._samples("no_contact")))

    def test_full_window_unchanged(self):
        start_off = (self.by["full"]["segments"][0][0] - BASE).total_seconds()
        self.assertAlmostEqual(start_off, self.t_touch - 1.0, delta=1.5 / HZ)
        self.assertEqual(self.by["full"]["anchor"], "t_touch")


class HoldGuardTest(unittest.TestCase):
    """플라토가 가드보다 짧으면 두 조건이 폐기된다 — 실측에서 확인된 대가."""

    def _plan(self, hold_len: float, hold_guard_s: float = DEFAULT_HOLD_GUARD_S):
        trace = _plateau_trace(t_touch=15.0, t_hold_start=15.3,
                               t_hold_end=15.3 + hold_len, t_release=15.3 + hold_len + 0.3)
        col = [_col_row(15.0, "obj001"), _col_row(15.0, "obj002")]
        return plan_episode("collision", trace, col, BASE, 30.0, n_control=0,
                            hold_guard_s=hold_guard_s)

    def test_plateau_shorter_than_guard_drops_both_conditions(self):
        plans, stats = self._plan(hold_len=0.02)      # 가드 0.05보다 짧다
        conds = {p["condition"] for p in plans}
        self.assertNotIn("no_approach", conds)
        self.assertNotIn("no_aftermath", conds)
        self.assertEqual(stats["no_approach"]["dropped"]["window_start_outside_plateau"], 1)
        self.assertEqual(stats["no_aftermath"]["dropped"]["window_end_outside_plateau"], 1)
        # 나머지 조건은 영향받지 않는다
        self.assertIn("full", conds)
        self.assertIn("approach_only", conds)

    def test_smaller_guard_recovers_them(self):
        """--hold-guard를 낮추면 같은 사건이 살아난다(수율/엄밀성 트레이드오프)."""
        plans, _ = self._plan(hold_len=0.02, hold_guard_s=0.0)
        conds = {p["condition"] for p in plans}
        self.assertIn("no_approach", conds)
        self.assertIn("no_aftermath", conds)

    def test_long_plateau_passes(self):
        plans, _ = self._plan(hold_len=0.5)
        conds = {p["condition"] for p in plans}
        self.assertIn("no_approach", conds)
        self.assertIn("no_aftermath", conds)


def _third_object_rows(pos_at, objid: str = "obj003", span_s: float = 30.0) -> list:
    """제3객체 하나를 시간 -> (x, z) 위치 함수로 만들어 trace에 덧붙일 행 목록.

    기준 쌍은 접촉 구간에서만 ±36cm에 있고 그 밖에서는 훨씬 벌어져 있으므로,
    "C가 얼마나 가까운가"를 창 전체에서 예측 가능하게 하려면 개입 구간을 접촉 구간
    **안쪽**으로 잡거나 z축으로 떨어뜨려야 한다. 아래 테스트들이 그렇게 쓴다.
    """
    rows = []
    for i in range(int(span_s * HZ) + 1):
        t = i / HZ
        x, z = pos_at(t)
        rows.append({"timestamp": _ts(t), "objid": objid,
                     "x": f"{x}", "y": "90", "z": f"{z}"})
    return rows


FAR = (3000.0, 0.0)     # 어떤 창에서도 문턱 밖인 대기 위치


class ThirdObjectGateTest(unittest.TestCase):
    """v3.3 — 충돌 반응 없이 사이를 지나가는 제3객체를 trace 기하로 잡는다.

    실물 증상: A-B 충돌 클립인데 C가 두 물체 사이를 밀고 지나갔다. C에 충돌 반응이
    없어 collisions CSV에 행이 남지 않았고, 기록 기반인 순도 검사는 통과해 버렸다.
    """

    # 기준 쌍은 접촉 구간 [15.0, 15.6] 동안 x=±36(거리 72cm)에 있고, 그 밖에서는
    # 시간에 비례해 벌어진다. 그래서 개입은 접촉 구간 안쪽 [15.1, 15.4]에 둔다.
    INTRUDE = (15.1, 15.4)

    def _episode(self, pos_at, n_control: int = 0, **kw):
        trace = _contact_trace(t_touch=15.0, t_release=15.6)
        trace += _third_object_rows(pos_at)
        col = [_col_row(15.0, "obj001"), _col_row(15.0, "obj002")]
        return plan_episode("collision", trace, col, BASE, 30.0,
                            n_control=n_control, **kw)

    def _during(self, pos):
        """개입 구간에만 pos, 그 밖에는 멀리."""
        lo, hi = self.INTRUDE
        return lambda t: pos if lo <= t <= hi else FAR

    # 개입 구간 [15.1, 15.4]를 창 안에 담는 조건들. approach_only([12.95, 14.95])와
    # no_contact(접촉 구간을 도려내 [13.9,14.9]+[15.6,16.6])는 개입 시간을 포함하지
    # 않으므로 폐기 대상이 아니다 — 게이트가 사건 단위가 아니라 **창 단위**로 판정한다.
    AFFECTED = ("full", "no_approach", "no_aftermath")

    def test_intruder_inside_window_drops_it(self):
        """창 동안 C가 두 물체 사이(x=0)로 들어오면 그 창이 폐기된다."""
        plans, stats = self._episode(self._during((0.0, 0.0)))
        conds = {p["condition"] for p in plans}
        for cond in self.AFFECTED:
            self.assertNotIn(cond, conds)
            self.assertGreaterEqual(stats[cond]["dropped"]["third_object_intrusion"], 1)

    def test_gate_is_window_scoped_not_event_scoped(self):
        """같은 사건이라도 개입 시간을 담지 않는 창은 살아남는다."""
        plans, _ = self._episode(self._during((0.0, 0.0)))
        conds = {p["condition"] for p in plans}
        self.assertIn("approach_only", conds)   # [12.95, 14.95] — 개입 전에 끝난다
        self.assertIn("no_contact", conds)      # 개입 구간이 절제된 사이에 들어간다

    def test_distant_third_object_is_fine(self):
        """멀리 있는 제3객체는 아무 영향이 없다."""
        plans, stats = self._episode(lambda t: FAR)
        self.assertTrue(any(p["condition"] == "full" for p in plans))
        self.assertEqual(stats["full"]["passed"], 1)
        self.assertEqual(sum(stats["full"]["dropped"].values()), 0)

    def test_intrusion_outside_window_does_not_drop(self):
        """창 밖에서만 접근하면 그 창은 무관하다 — full[14.0,16.0] 밖인 t>=17에서 접근."""
        plans, _ = self._episode(lambda t: (0.0, 0.0) if t >= 17.0 else FAR)
        self.assertTrue(any(p["condition"] == "full" for p in plans))

    def test_measured_against_both_pair_members(self):
        """한쪽에만 가까운 비스듬한 개입도 잡는다 — 중점만 봤다면 놓쳤을 경우.

        x=110이면 obj002(+36)와 74cm로 문턱 안이지만, 두 물체의 중점(0)과는 110cm로
        문턱 밖이다. 중점 기준 검사였다면 이 개입을 통과시켰을 것이다.
        """
        plans, stats = self._episode(self._during((110.0, 0.0)))
        conds = {p["condition"] for p in plans}
        for cond in self.AFFECTED:
            self.assertNotIn(cond, conds)
            self.assertGreaterEqual(stats[cond]["dropped"]["third_object_intrusion"], 1)

    def test_threshold_zero_disables_gate(self):
        plans, _ = self._episode(self._during((0.0, 0.0)), third_object_cm=0.0)
        self.assertTrue(any(p["condition"] == "full" for p in plans))

    def test_threshold_is_configurable(self):
        """C가 obj002와 100cm까지만 접근 — 기본 90cm는 통과, 문턱 120이면 폐기."""
        pos_at = self._during((136.0, 0.0))       # obj002(+36)와 100cm
        plans, _ = self._episode(pos_at)
        self.assertTrue(any(p["condition"] == "full" for p in plans))
        plans, stats = self._episode(pos_at, third_object_cm=120.0)
        conds = {p["condition"] for p in plans}
        for cond in self.AFFECTED:
            self.assertNotIn(cond, conds)
            self.assertGreaterEqual(stats[cond]["dropped"]["third_object_intrusion"], 1)

    def test_passing_clip_records_min_distance(self):
        """통과한 창에도 최소 거리를 남겨 나중에 문턱을 조일 수 있게 한다.

        C를 z축으로 3000cm 떼어 두면 쌍이 벌어지든 말든 최소 거리가 접촉 중
        (half=36)의 sqrt(36^2 + 3000^2) = 3000.2로 안정적이다.
        """
        plans, _ = self._episode(lambda t: (0.0, 3000.0))
        full = [p for p in plans if p["condition"] == "full"][0]
        self.assertIn("third_object_min_cm", full)
        self.assertAlmostEqual(full["third_object_min_cm"], 3000.2, delta=1.0)

    def test_two_object_episode_has_no_third_object_field(self):
        trace = _contact_trace(t_touch=15.0, t_release=15.6)
        col = [_col_row(15.0, "obj001"), _col_row(15.0, "obj002")]
        plans, _ = plan_episode("collision", trace, col, BASE, 30.0, n_control=0)
        full = [p for p in plans if p["condition"] == "full"][0]
        self.assertNotIn("third_object_min_cm", full)

    def test_applies_to_near_miss(self):
        """접촉이 없는 near_miss 조건에도 적용된다."""
        trace = _near_miss_trace(d_min=95.0, t_min_s=15.0)
        trace += _third_object_rows(lambda t: (0.0, 30.0) if 14.9 <= t <= 15.1 else FAR)
        plans, stats = plan_episode("nearmiss", trace, [], BASE, 30.0, gap=95.0, n_control=0)
        self.assertEqual([p for p in plans if p["condition"] == "near_miss"], [])
        self.assertGreaterEqual(stats["near_miss"]["dropped"]["third_object_intrusion"], 1)

    def test_applies_to_control(self):
        """control은 기준 쌍이 없으므로 전 쌍의 최소 거리로 같은 검사를 한다."""
        # 두 객체가 내내 40cm 간격으로 붙어 다니는 에피소드 — 사건은 없지만 대조군 자격도 없다
        rows = _third_object_rows(lambda t: (0.0, 0.0), objid="obj001")
        rows += _third_object_rows(lambda t: (40.0, 0.0), objid="obj002")
        plans, stats = plan_episode("nearmiss", rows, [], BASE, 30.0, gap=95.0, n_control=1)
        self.assertEqual([p for p in plans if p["condition"] == "control"], [])
        self.assertGreaterEqual(stats["control"]["dropped"]["third_object_intrusion"], 1)

    def test_clean_control_still_produced(self):
        rows = _third_object_rows(lambda t: (0.0, 0.0), objid="obj001")
        rows += _third_object_rows(lambda t: (900.0, 0.0), objid="obj002")
        plans, _ = plan_episode("nearmiss", rows, [], BASE, 30.0, gap=95.0, n_control=1)
        ctrl = [p for p in plans if p["condition"] == "control"]
        self.assertEqual(len(ctrl), 1)
        self.assertAlmostEqual(ctrl[0]["third_object_min_cm"], 900.0, delta=1.0)


class WindowSpecsTest(unittest.TestCase):
    def test_anchors_and_lengths(self):
        specs = window_specs()
        self.assertEqual(specs["no_approach"][0], "t_hold_start")
        self.assertEqual(specs["no_aftermath"][0], "t_hold_end")
        self.assertEqual(specs["full"][0], "t_touch")
        self.assertEqual(specs["approach_only"][0], "t_touch")
        for cond, (_, spec) in specs.items():
            total = sum(e - s for s, e in spec)
            self.assertAlmostEqual(total, 2.0, places=6, msg=cond)

    def test_guard_shifts_only_plateau_conditions(self):
        a, b = window_specs(0.05), window_specs(0.2)
        self.assertEqual(a["full"], b["full"])
        self.assertEqual(a["approach_only"], b["approach_only"])
        self.assertNotEqual(a["no_approach"], b["no_approach"])
        self.assertNotEqual(a["no_aftermath"], b["no_aftermath"])


class OverlayFlagTest(unittest.TestCase):
    """v3.2-4 — 화면 겹침 플래그. 절단과 분리된 후처리로 재부여 가능해야 한다."""

    def _manifest(self):
        return {"clips": [
            {"condition": "near_miss", "d_min_in_window": 95.0},
            {"condition": "control", "d_min_in_window": 300.0},
            {"condition": "full", "pair": ["obj001", "obj002"]},   # 대상 아님
        ]}

    def test_unassigned_when_threshold_is_zero(self):
        m = self._manifest()
        self.assertEqual(assign_overlay_flags(m, 0.0), 0)
        self.assertTrue(all("overlay_overlap" not in c for c in m["clips"]))
        self.assertEqual(m["overlay_touch_cm"], 0.0)

    def test_flags_only_clips_with_recorded_distance(self):
        m = self._manifest()
        n = assign_overlay_flags(m, 120.0)
        self.assertEqual(n, 1)
        self.assertTrue(m["clips"][0]["overlay_overlap"])    # 95 <= 120
        self.assertFalse(m["clips"][1]["overlay_overlap"])   # 300 > 120
        self.assertNotIn("overlay_overlap", m["clips"][2])   # 충돌 조건은 대상 아님

    def test_reassignment_without_recutting(self):
        """문턱이 바뀌면 같은 manifest에 다시 부여만 하면 된다 — 클립은 그대로."""
        m = self._manifest()
        assign_overlay_flags(m, 120.0)
        self.assertTrue(m["clips"][0]["overlay_overlap"])
        assign_overlay_flags(m, 80.0)
        self.assertFalse(m["clips"][0]["overlay_overlap"])   # 95 > 80
        self.assertEqual(m["overlay_touch_cm"], 80.0)

    def test_min_pair_distance_scans_all_pairs(self):
        rows = []
        for i in range(10):
            t = i / HZ
            rows += [{"timestamp": _ts(t), "objid": "obj001", "x": "0", "y": "0", "z": "0"},
                     {"timestamp": _ts(t), "objid": "obj002", "x": "500", "y": "0", "z": "0"},
                     {"timestamp": _ts(t), "objid": "obj003", "x": f"{40 + i}", "y": "0", "z": "0"}]
        frames = trace_frames(rows)
        d = min_pair_distance(frames, [(_at(0.0), _at(9 / HZ))])
        self.assertAlmostEqual(d, 40.0, places=6)   # obj001-obj003 최소

    def test_control_clip_carries_window_distance(self):
        trace = _near_miss_trace(d_min=95.0, t_min_s=15.0)
        plans, _ = plan_episode("nearmiss", trace, [], BASE, 30.0, gap=95.0, n_control=1)
        ctrl = [p for p in plans if p["condition"] == "control"]
        self.assertEqual(len(ctrl), 1)
        self.assertIn("d_min_in_window", ctrl[0])
        nm = [p for p in plans if p["condition"] == "near_miss"][0]
        self.assertAlmostEqual(nm["d_min_in_window"], nm["d_min"], places=6)


class WindowIntegrityTest(unittest.TestCase):
    """모든 조건 창의 총장이 2.0초라는 학습 계약을 지키는지."""

    def test_every_condition_is_two_seconds(self):
        trace = _contact_trace(t_touch=15.4, t_release=16.0)
        col = [_col_row(15.0, "obj001"), _col_row(15.0, "obj002")]
        plans, _ = plan_episode("collision", trace, col, BASE, 30.0, n_control=1)
        self.assertTrue(plans)
        for p in plans:
            total = sum((e - s).total_seconds() for s, e in p["segments"])
            self.assertAlmostEqual(total, 2.0, places=6, msg=p["condition"])


class FfmpegCmdTest(unittest.TestCase):
    def test_single_segment(self):
        segs = [(_at(14.0), _at(16.0))]
        cmd = ffmpeg_cmd(Path("v.mp4"), segs, BASE, Path("out.mp4"))
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("trim=start=14.000:duration=2.000", graph)
        self.assertIn("concat=n=1", graph)
        self.assertIn("libx264", cmd)

    def test_splice_two_segments(self):
        segs = no_contact_segments(_at(15.0), _at(15.6))
        cmd = ffmpeg_cmd(Path("v.mp4"), segs, BASE, Path("out.mp4"))
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("concat=n=2", graph)
        self.assertEqual(graph.count("trim=start="), 2)
        self.assertIn("trim=start=13.900:duration=1.000", graph)
        # v3.1: 뒤 구간은 t_release(15.6)에서 바로 시작한다
        self.assertIn("trim=start=15.600:duration=1.000", graph)


class TraceHelpersTest(unittest.TestCase):
    def test_pair_series_uses_ground_plane_only(self):
        rows = [{"timestamp": _ts(0.0), "objid": "obj001", "x": "0", "y": "0", "z": "0"},
                {"timestamp": _ts(0.0), "objid": "obj002", "x": "30", "y": "999", "z": "40"}]
        series = pair_series(trace_frames(rows), "obj001", "obj002")
        self.assertEqual(len(series), 1)
        self.assertAlmostEqual(series[0][1], math.hypot(30.0, 40.0), places=6)


if __name__ == "__main__":
    unittest.main()
