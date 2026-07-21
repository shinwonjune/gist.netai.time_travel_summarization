"""EventSummaryWindow의 순수 로직 유닛테스트 — omni import 없이 실행.

summary_window 모듈은 omni.ui/carb/task_dispatcher를 메서드 안에서 지연 import하므로
모듈 최상위 순수 헬퍼는 omni 없는 환경에서 그대로 불러 검증할 수 있다.
"""
import datetime

from gist.netai.time_travel_summarization.events.summary_window import (
    GenerationToken,
    event_time_span,
    parse_event_time,
    playback_reached_end,
    sort_events_by_time,
)

T0 = datetime.datetime(2026, 7, 19, 3, 44, 0)


def _ev(sec: int, ids=None):
    return {"time": (T0 + datetime.timedelta(seconds=sec)).isoformat(), "ids": ids or []}


# ---- GenerationToken (폴링 스레드 무효화) ---------------------------------- #


def test_generation_token_bump_invalidates_previous():
    gen = GenerationToken()
    first = gen.bump()
    assert gen.is_current(first)
    second = gen.bump()
    assert not gen.is_current(first)  # 이전 세대 stale
    assert gen.is_current(second)
    assert second == first + 1


def test_generation_token_starts_at_zero():
    gen = GenerationToken()
    assert gen.value == 0
    assert gen.is_current(0)


# ---- 이벤트 시간순 정렬 ---------------------------------------------------- #


def test_sort_events_by_time_orders_ascending():
    events = [_ev(30), _ev(5), _ev(20)]
    ordered = sort_events_by_time(events)
    assert [e["time"] for e in ordered] == [_ev(5)["time"], _ev(20)["time"], _ev(30)["time"]]


def test_sort_events_by_time_pushes_unparseable_to_end_stably():
    bad_a = {"time": "not-a-time", "ids": ["a"]}
    bad_b = {"ids": ["b"]}  # time 키 없음
    events = [bad_a, _ev(10), bad_b, _ev(2)]
    ordered = sort_events_by_time(events)
    assert [e["time"] for e in ordered[:2]] == [_ev(2)["time"], _ev(10)["time"]]
    # 파싱 불가 레코드는 원래 상대순서 유지하며 뒤로
    assert ordered[2] is bad_a
    assert ordered[3] is bad_b


def test_event_time_span_returns_min_max():
    span = event_time_span([_ev(30), _ev(5), _ev(20)])
    assert span == (T0 + datetime.timedelta(seconds=5), T0 + datetime.timedelta(seconds=30))


def test_event_time_span_none_when_all_unparseable():
    assert event_time_span([{"time": "x"}, {"ids": []}]) is None


def test_parse_event_time_bad_record():
    assert parse_event_time({"ids": []}) is None
    assert parse_event_time({"time": "nope"}) is None
    assert parse_event_time(_ev(7)) == T0 + datetime.timedelta(seconds=7)


# ---- 재생 종료 판정 (twin time 기준) --------------------------------------- #


def test_playback_reached_end_before_deadline():
    cur = T0 + datetime.timedelta(seconds=3)
    assert not playback_reached_end(cur, T0, 5.0)


def test_playback_reached_end_at_deadline():
    cur = T0 + datetime.timedelta(seconds=5)
    assert playback_reached_end(cur, T0, 5.0)


def test_playback_reached_end_past_deadline():
    cur = T0 + datetime.timedelta(seconds=8)
    assert playback_reached_end(cur, T0, 5.0)
