"""재생 헤드 시각 -> 객체 보임/숨김 판정 (Kit 무의존, 단위 테스트 대상).

두 가지 숨김 규칙이 있고, 두 번째는 기본 비활성이다.

1. **트랙 범위 밖**(항상 적용): objid의 데이터 범위 [first,last] 밖이면 숨긴다.
   track fragmentation으로 죽은 트랙이 마지막 좌표에 얼어붙어 가짜 충돌을 만드는
   문제를 원천 차단한다.
2. **결손 인지 despawn**(``gap_s``를 준 경우에만): 범위 *안*이라도 마지막 표본
   이후 경과가 gap_s를 넘으면 숨긴다. 기본값은 None(비활성) — 켜면 표본이 드문
   데이터(다운샘플 조건)에서 정상 객체가 깜빡이므로, **조건별 렌더 파라미터**로만
   쓴다(v3 계획서 §4-5).

   왜 조건별인가: frag-sameid 계열은 같은 ID로 중간 행만 지우므로 규칙 1이 발동하지
   않아 hold(마지막 위치 유지)로 "얼어 있는 객체"가 되고, 그건 소멸 대조군이 아니라
   occ-hold의 변형이다. 반대로 dsr1(1초 간격)은 결손이 아니라 정상 표본 간격이라
   숨기면 안 된다. 두 요구가 상반되므로 하나의 전역 임계로는 양립할 수 없다.
"""
import datetime
from typing import Dict, Mapping, Optional, Tuple

TrackRange = Tuple[datetime.datetime, datetime.datetime]

# 다운샘플 1Hz 데이터의 샘플 간격(1s)을 종료로 오검하지 않기 위한 여유(초).
DEFAULT_VISIBILITY_TOL_S = 1.0


def is_track_visible(
    now: datetime.datetime,
    first: datetime.datetime,
    last: datetime.datetime,
    tol_s: float = DEFAULT_VISIBILITY_TOL_S,
    last_sample: Optional[datetime.datetime] = None,
    gap_s: Optional[float] = None,
) -> bool:
    """now가 [first-tol, last+tol] 안이면 True(보임), 밖이면 False(숨김).

    ``gap_s``가 주어지면 범위 안이라도 ``now - last_sample > gap_s``일 때 숨긴다
    (결손 인지 despawn). last_sample이 None이면(그 시각 이전 표본이 아예 없음)
    범위 판정에 맡긴다.
    """
    tol = datetime.timedelta(seconds=max(0.0, tol_s))
    if not (first - tol <= now <= last + tol):
        return False
    if gap_s is not None and last_sample is not None:
        if (now - last_sample).total_seconds() > max(0.0, gap_s):
            return False
    return True


def compute_object_visibility(
    now: datetime.datetime,
    ranges: Mapping[str, TrackRange],
    tol_s: float = DEFAULT_VISIBILITY_TOL_S,
    last_samples: Optional[Mapping[str, datetime.datetime]] = None,
    gap_s: Optional[float] = None,
) -> Dict[str, bool]:
    """objid별 보임/숨김 판정. ranges = {objid: (first_sample_t, last_sample_t)}.

    last_samples = {objid: now 이하의 마지막 표본 시각} — gap_s와 함께 줄 때만 쓰인다.
    """
    ls = last_samples or {}
    return {
        objid: is_track_visible(now, first, last, tol_s, ls.get(objid), gap_s)
        for objid, (first, last) in ranges.items()
    }
