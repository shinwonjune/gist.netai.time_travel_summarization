"""geom.center_distance_3d가 perturb_eval.py / rule_baseline.py의 원본 수식과

비트 단위로 같은 결과를 내는지 확인하고, generate_episodes.py를 통합 대상에서
제외한 이유(geom.py 모듈 docstring 참조)를 재현 사례로 남겨 둔다.

perturb_eval.py / rule_baseline.py는 ``dx**2 + dy**2 + dz**2`` 명시적 덧셈을
쓰고, geom.center_distance_3d도 같은 형태라 **정확히(==)** 일치해야 한다.
generate_episodes.py(near_miss_events, check_near_miss_trace)는 처음부터
``sum(... for k in range(3))`` 제너레이터를 썼고 지금도 그 표현을 그대로
유지한다(geom.center_distance_3d로 바꾸지 않았다) — 재현성이 필요한 생성
코드라서다. 두 형태는 수학적으로는 같은 식이지만, CPython 3.12부터 float
``sum()``이 보정합(Neumaier summation)을 쓰는 탓에 실측상 마지막 몇 비트에서
갈라진다(아래 test_sum_and_explicit_forms_can_legitimately_differ가 재현) —
"한 함수로 두 형태 모두와 비트 단위로 동일"은 애초에 불가능했다는 뜻이고, 이게
generate_episodes.py를 안 건드린 근거다. 그래서 sum() 쪽과는 근사 동등성만
확인한다(실제 판정 임계값보다 훨씬 작은 오차이므로 무해함의 기록).
"""
import random

from gist.netai.time_travel_summarization.automation.geom import center_distance_3d


def _random_points(n: int, seed: int):
    rng = random.Random(seed)
    pts = []
    for _ in range(n):
        a = tuple(rng.uniform(-1e6, 1e6) for _ in range(3))
        b = tuple(rng.uniform(-1e6, 1e6) for _ in range(3))
        pts.append((a, b))
    return pts


def test_matches_explicit_xyz_expression_10k_random():
    """perturb_eval.min_pair_distance / rule_baseline._pair_events가 쓰던 형태 — 정확히 일치."""
    for pa, pb in _random_points(10_000, seed=1):
        legacy = ((pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2
                  + (pa[2] - pb[2]) ** 2) ** 0.5
        assert center_distance_3d(pa, pb) == legacy, (pa, pb, legacy)


def test_close_to_sum_generator_expression_10k_random():
    """generate_episodes.py가 지금도 쓰는 sum() 형태 — CPython 3.12+ 보정합 탓에

    완전한 비트 동일은 물리적으로 불가능하다(모듈 docstring 참조) — 그래서
    generate_episodes.py는 geom.center_distance_3d로 바꾸지 않고 원래 표현을
    그대로 유지한다. near_miss_events의 실제 임계값(gap·near_eps·tol)이 전부
    단위~수백 단위인데 여기 오차는 1e-9 units 이내라 판정에는 영향이 없다는
    것만 여유를 두고 근사 비교로 기록해 둔다.
    """
    for pa, pb in _random_points(10_000, seed=2):
        legacy = sum((pa[k] - pb[k]) ** 2 for k in range(3)) ** 0.5
        assert abs(center_distance_3d(pa, pb) - legacy) < 1e-9, (pa, pb, legacy)


def test_sum_and_explicit_forms_can_legitimately_differ():
    """왜 하나의 함수가 두 원본 표현 모두와 비트 동일할 수 없는지의 재현 사례.

    2026-09 기준 CPython 3.12에서 재현된 구체적인 반례 하나를 고정해 둔다 — 이
    모듈이 sum() 대신 명시적 덧셈을 표준으로 택한 근거.
    """
    pa = (-731271.5117751976, 694867.4738744653, 527549.237953228)
    pb = (-489861.9485211566, -9129.825816118042, -101017.8704225237)
    explicit = ((pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2 + (pa[2] - pb[2]) ** 2) ** 0.5
    via_sum = sum((pa[k] - pb[k]) ** 2 for k in range(3)) ** 0.5
    assert explicit != via_sum, "이 반례가 더 이상 재현되지 않는다 — 재검토 필요"
    assert center_distance_3d(pa, pb) == explicit


def test_zero_distance():
    p = (1.5, -2.5, 3.5)
    assert center_distance_3d(p, p) == 0.0
