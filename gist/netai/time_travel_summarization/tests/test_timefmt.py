"""timefmt.TIMESTAMP_FMT / format_timestamp가 기존에 각지에 복제돼 있던

``dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]`` 표현식과 바이트 단위로 같은 문자열을
내는지 확인한다 — 통합 전/후 산출물이 달라지면 trajectory dict의 lookup key가
어긋나 floor lookup이 깨진다.
"""
import datetime
import random

from gist.netai.time_travel_summarization import timefmt


def _random_datetimes(n: int, seed: int) -> list:
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        year = rng.randint(1, 9999)
        month = rng.randint(1, 12)
        day = rng.randint(1, 28)  # 모든 달에 유효 — 윤년/월말 분기 회피
        hour = rng.randint(0, 23)
        minute = rng.randint(0, 59)
        second = rng.randint(0, 59)
        microsecond = rng.randint(0, 999_999)
        out.append(datetime.datetime(year, month, day, hour, minute, second, microsecond))
    return out


def test_format_timestamp_matches_legacy_expression_10k_random():
    for dt in _random_datetimes(10_000, seed=20260904):
        legacy = dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        new = timefmt.format_timestamp(dt)
        assert new == legacy, (dt, legacy, new)


def test_timestamp_fmt_constant_is_the_legacy_literal():
    assert timefmt.TIMESTAMP_FMT == "%Y-%m-%d %H:%M:%S.%f"


def test_format_timestamp_matches_trajectory_repository_format_timestamp():
    from gist.netai.time_travel_summarization.playback.trajectory_repository import (
        TrajectoryRepository,
    )

    for dt in _random_datetimes(1_000, seed=7):
        assert TrajectoryRepository.format_timestamp(dt) == timefmt.format_timestamp(dt)
