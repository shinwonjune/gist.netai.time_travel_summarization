"""재생 헤드 시각 -> 객체 보임/숨김 판정 (Kit 무의존, 단위 테스트 대상).

트랙(objid)의 데이터 범위 [first,last] 밖이면(시작 전 / 종료 후) 숨긴다. track
fragmentation으로 죽은 트랙이 마지막 좌표에 얼어붙어 다른 객체와 가짜 충돌을 만드는
문제를 원천 차단하는 순수 판정부다. 트랙 *내부*의 결손(중간 공백)은 범위 안이라 보임을
유지한다 — 그 구간 위치는 기존 hold 동작(floor lookup)이 담당하고, 이 모듈은
트랙 시작 전 / 종료 후만 다룬다.
"""
import datetime
from typing import Dict, Mapping, Tuple

TrackRange = Tuple[datetime.datetime, datetime.datetime]

# 다운샘플 1Hz 데이터의 샘플 간격(1s)을 종료로 오검하지 않기 위한 여유(초).
DEFAULT_VISIBILITY_TOL_S = 1.0


def is_track_visible(
    now: datetime.datetime,
    first: datetime.datetime,
    last: datetime.datetime,
    tol_s: float = DEFAULT_VISIBILITY_TOL_S,
) -> bool:
    """now가 [first-tol, last+tol] 안이면 True(보임), 밖이면 False(숨김)."""
    tol = datetime.timedelta(seconds=max(0.0, tol_s))
    return first - tol <= now <= last + tol


def compute_object_visibility(
    now: datetime.datetime,
    ranges: Mapping[str, TrackRange],
    tol_s: float = DEFAULT_VISIBILITY_TOL_S,
) -> Dict[str, bool]:
    """objid별 보임/숨김 판정. ranges = {objid: (first_sample_t, last_sample_t)}."""
    return {
        objid: is_track_visible(now, first, last, tol_s)
        for objid, (first, last) in ranges.items()
    }
