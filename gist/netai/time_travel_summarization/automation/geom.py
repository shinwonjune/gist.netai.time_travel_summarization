"""3D 중심거리 계산의 단일 구현 (perturb_eval.py·rule_baseline.py 공용).

교란 조건 검출(perturb_eval.py)과 룰 베이스라인 검출(rule_baseline.py)이 각자
복제해 갖고 있던 ``dx**2 + dy**2 + dz**2`` 명시적 덧셈 수식을 여기로 모은다. 두 곳은
"쌍 검출/거리 임계"라는 같은 목적으로 이 값을 쓰므로 수식이 갈라지면 같은 trace에
대해 검출기마다 다른 τ 판정이 나올 수 있다 — 이 모듈은 그 어긋남을 막는다. 이 함수는
두 원본 표현과 **비트 단위로 동일**하다(tests/test_geom.py).

generate_episodes.py(near_miss_events, check_near_miss_trace)는 이 함수를 쓰지 않고
``sum(... for k in range(3)) ** 0.5`` 표현을 독자적으로 유지한다. CPython 3.12부터
float ``sum()``이 보정합(Neumaier summation)을 쓰기 때문에 명시적 덧셈과 마지막 몇
비트가 달라질 수 있고(무작위 좌표 ±1000 20만 쌍 중 약 10%, 최대 절대오차 ~4.5e-13),
생성 코드는 공표 데이터의 재현성을 위해 비트 동일성이 증명될 때만 변경한다는 원칙에
따라 손대지 않았다.
"""
from __future__ import annotations

from typing import Sequence


def center_distance_3d(a: Sequence[float], b: Sequence[float]) -> float:
    """(3D 중심거리) 두 점 a, b(x,y,z) 사이의 유클리드 거리."""
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    dz = a[2] - b[2]
    return (dx ** 2 + dy ** 2 + dz ** 2) ** 0.5
