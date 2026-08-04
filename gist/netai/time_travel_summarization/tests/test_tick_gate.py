"""PlaybackController 데이터 갱신 게이트(TTS_TICK_MIN_S) 테스트 — 순수 파이썬.

게이트: 재생 누적 시간이 문턱(기본 0.1s) 미만이면 그 프레임엔 lookup을 건너뛴다.
레이크 성능 실험에서 GUI lookup 밀도를 올리기 위해 env로 조정 가능해야 한다
(레이크성능_실험설계 §0-3). env는 컨트롤러 생성 시점에 1회 읽힌다.
"""
import datetime
import os
import unittest

from gist.netai.time_travel_summarization.playback.controller import PlaybackController

_ENV = "TTS_TICK_MIN_S"


def _make_controller(tick_env=None) -> PlaybackController:
    """env를 지정 값으로 두고 컨트롤러를 만든 뒤 env를 원상복구한다(테스트 간 격리)."""
    saved = os.environ.pop(_ENV, None)
    if tick_env is not None:
        os.environ[_ENV] = tick_env
    try:
        c = PlaybackController()
    finally:
        os.environ.pop(_ENV, None)
        if saved is not None:
            os.environ[_ENV] = saved
    start = datetime.datetime(2026, 1, 1, 0, 0, 0)
    c.configure_data_range(start, start + datetime.timedelta(seconds=60))
    c.toggle_playback()
    return c


def _drive(c: PlaybackController, dts) -> list:
    """dt 시퀀스로 update를 돌리고 on_time_changed 발화 시각들을 수집."""
    fired: list = []
    for dt in dts:
        c.update(dt, lambda s: s, fired.append, lambda e: None)
    return fired


class TickGateTest(unittest.TestCase):
    def test_default_gate_is_10hz(self):
        # 기본 0.1s: 0.05s 프레임 1개로는 미발동, 2개 누적(0.1s)이면 발동.
        c = _make_controller()
        self.assertEqual(len(_drive(c, [0.05])), 0)
        self.assertEqual(len(_drive(c, [0.05])), 1)

    def test_env_lowers_gate(self):
        # 0.02s로 낮추면 0.05s 프레임마다 발동 → 갱신 밀도 상승(30/60Hz 실험 경로).
        c = _make_controller("0.02")
        self.assertEqual(len(_drive(c, [0.05, 0.05, 0.05])), 3)

    def test_zero_means_every_frame(self):
        c = _make_controller("0")
        self.assertEqual(len(_drive(c, [0.001, 0.001])), 2)

    def test_invalid_env_falls_back_to_default(self):
        c = _make_controller("abc")
        self.assertEqual(len(_drive(c, [0.05])), 0)   # 기본 0.1 유지
        self.assertEqual(len(_drive(c, [0.05])), 1)

    def test_negative_env_clamped_to_zero(self):
        c = _make_controller("-1")
        self.assertEqual(len(_drive(c, [0.001])), 1)  # 0으로 클램프 → 매 프레임


if __name__ == "__main__":
    unittest.main()
