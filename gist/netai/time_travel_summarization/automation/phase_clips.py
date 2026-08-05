"""위상 분해 조건 세트 추출기 v3.1 — 접촉 구간 실측 기준 + 조건 의미 검증 게이트.

설계: docs/위상분해_실험설계.md §5. 단일 도구가 조건 전부를 한 번에 뽑는다:
  충돌 에피소드    -> full / no_approach / no_contact(스플라이스) / no_aftermath /
                      approach_only  (기준 = 실측 접촉 구간 [t_touch, t_release])
  near-miss 에피소드 -> near_miss    (기준 = trace 쌍별 거리 극소점 t*)
  양쪽              -> control(무관 구간 — 모든 이벤트에서 ±buffer 이상 떨어진 창)

--------------------------------------------------------------------------
v2 -> v3에서 무엇을 왜 바꿨나 (v2의 확정된 결함 3개)
--------------------------------------------------------------------------
1. **벽 접촉을 사건으로 취급했다.** collisions CSV의 ``kind`` 열에는 객체끼리
   부딪힌 ``object``, 벽에 닿아 방향을 튼 ``wall``, 제자리에 낀 채 재배치된
   ``stuck``이 뒤섞여 들어온다. v2는 kind를 보지 않고 모든 행을 접촉 사건으로
   묶었고, 실제 파일에서 wall 행이 다수를 차지하기 때문에(로컬 60개 CSV 집계:
   wall 2683 / object 850 / stuck 466) 만들어진 클립의 대부분이 "객체-객체 충돌"과
   무관한 벽 접촉 순간을 기준으로 잘렸다. v3는 ``kind == "object"``인 행만
   사건으로 인정한다(상수 ``OBJECT_KIND``).
   이 필터는 기준점 정확도에도 직접 기여한다. 진단 리포트 §3에 따르면 v2에서 기준점이
   자기 사건이 아닌 더 이른 접촉 시각으로 정해진 사건 12건 중 9건의 원인이 **같은 초에
   기록된 벽 충돌 행이 객체 접촉과 한 클러스터로 묶여 대표 시각을 앞으로 끌어당긴 것**
   이었다(예: ep_0000의 14:16:27 사건은 obj002-obj003 접촉이 +0.700초 지점인데 같은 초
   obj001의 벽 충돌 +0.233초가 함께 묶여 기준점이 0.467초 앞으로 밀렸다). kind로 먼저
   거르면 이 꼬리는 구조적으로 사라진다 — 회귀 방지 테스트로 고정해 두었다.
2. **기준 시각이 collisions CSV의 초 단위 시각이었다.** CSV의 timestamp는 초까지만
   기록되므로(예: ``14:16:16``) 참 접촉 순간은 그 초 이후 최대 1초 안 어딘가다.
   창 규격은 0.1초 단위인데 기준점이 최대 1초 어긋나면 조건의 의미 자체가 무너진다
   — 실제 샘플(gen-20260804-phasecol-v2/ep_0000)에서 참 접촉은 기록된 초로부터
   +0.233s / +0.633s / +0.450s / +0.666s / +0.633s 지점이었고, ep_0001에는
   기록된 초보다 **앞선** −0.050s 사례도 있었다. v3는 CSV를 "사건이 존재한다는
   확인 + kind 필터 + 접촉 쌍 식별"에만 쓰고, 기준 시각은 그 쌍의 지면(x,z) 거리
   시계열(trace, 실측 약 58.8Hz)에서 문턱 하회/회복 순간으로 **직접 측정**한다.
3. **조건의 의미를 기계 검증하는 장치가 없었다.** 창을 잘라낸 뒤 그 창이 실제로
   조건이 주장하는 내용(접촉 프레임 포함 / 접근만 있고 접촉 없음 / 접촉 절제 등)을
   만족하는지 아무도 확인하지 않았다. v3는 기준 쌍의 거리 시계열에 대해 조건별
   게이트를 적용해 불합격 창을 폐기하고, 조건별 (계획 수, 통과 수, 폐기 사유 집계)를
   표준 출력과 manifest에 남긴다.

--------------------------------------------------------------------------
접촉 구간 실측 방법
--------------------------------------------------------------------------
collisions CSV에서 ``kind == "object"`` 행을 0.5초 반경으로 묶어 사건 1건을 만들고,
그 클러스터에 등장한 두 objid를 접촉 쌍으로 삼는다(PhysX contact report가 접촉한
두 객체 각각에 대해 행을 남기므로 정상 사건의 클러스터에는 objid가 정확히 2개다).
그 쌍의 지면 거리 시계열에서

  t_touch   = CSV 클러스터 시각 ±SEARCH 창 안에서 거리가 문턱을 처음 하회한 순간
  t_release = t_touch 이후 거리가 문턱 이상으로 처음 회복한 순간

을 찾는다. 문턱 기본값 90cm은 물리 쪽 접촉 정의(사이드카 ``collision_distance``
= 2r ≈ 71.7cm)보다 넉넉히 크게 잡은 값이다 — contact report는 중심 거리가 2r에
닿는 순간 발화하므로 거리 시계열의 최저값이 2r을 크게 밑돌지 않고(실측 d_min은
72.2~75.3cm), 문턱을 2r에 딱 맞추면 샘플링 격자 탓에 하회 순간을 놓친다.
접촉 구간 길이는 사건마다 다르다(실측 0.20s~1.42s) — v2가 쓰던 고정 1초 절제가
틀린 이유이자, 이 값을 manifest에 ``contact_len_s``로 남기는 이유다.

--------------------------------------------------------------------------
v3 -> v3.1 창 경계 조정 (사용자 게이트 피드백 4건)
--------------------------------------------------------------------------
v3는 기준 시각을 실측으로 바로잡았지만 창 경계에는 v2 시절의 넉넉한 가드가 남아
있었다. 기준 시각이 프레임 수준으로 정확해진 이상(진단 실측 절대오차 중앙값
0.016초) 그 가드는 안전장치가 아니라 **조건이 겨냥한 국면 대신 인접 국면의 프레임을
창에 남기는 오염원**이다. 필요한 가드는 기준 시각 오차 + 프레임 양자화 1개,
즉 0.05초(1.5프레임)면 충분하다(``EDGE_GUARD_S``).

1. approach_only  [t_touch−2.2, −0.2]   -> [t_touch−2.05, −0.05]
   접촉 직전 0.15초를 되돌린다. 접근의 마지막 국면은 궤적 수렴이 가장 뚜렷한
   구간인데 과한 가드가 그것을 잘라내고 있었다.
2. no_approach    [t_touch−0.1, +1.9]   -> [t_touch+0.05, +2.05]
   창을 접촉 시작 **뒤에서** 연다. v3는 접촉 직전 프레임을 최대 0.1초 남겨
   "접근 동역학 배제"라는 조건의 정의가 완전히 지켜지지 않았다.
3. no_aftermath   [t_release−1.9, +0.1] -> [t_release−2.05, −0.05]
   아직 붙어 있는 프레임에서 창을 닫는다. v3는 회복 시각을 0.1초 넘겨 두 물체가
   떨어지기 시작하는 분리 장면을 담았는데, 그것이 바로 배제 대상인 사후 신호다.
4. no_contact 뒤 구간 [t_release+0.1, +1.1] -> [t_release, +1.0]
   튕겨 나가는 첫 0.1초를 복원한다. 배제 대상은 접촉 프레임이지 충돌 직후의
   반응이 아니고, t_release는 이미 문턱을 회복한 시각이라 접촉 프레임이 들어오지
   않는다(자세한 근거는 ``no_contact_segments`` 독스트링).

full·near_miss·control의 창은 v3.1에서 바뀌지 않았다. 조건 검증 게이트도 새 경계에
맞춰 강화했다 — no_approach는 창 첫 프레임이 접촉 중인지, no_aftermath는 창 마지막
프레임이 접촉 중인지를 추가로 확인한다.

--------------------------------------------------------------------------
사건 속성 기록 — 폐기하지 않고 분석에서 분리할 수 있게 (진단 리포트 §4·§5)
--------------------------------------------------------------------------
클립마다 두 속성을 manifest에 남긴다. 둘 다 **폐기 기준이 아니라 분석 분리용 표식**이다.

- ``contact_class`` ("short" | "long", 경계 0.7초): 접촉 구간 길이가 이봉 분포라
  짧은 접촉(스치고 떨어짐)과 긴 접촉(붙어서 함께 움직임)은 같은 조건이라도 화면에
  보이는 장면의 성격이 다르다. 특히 no_aftermath·no_contact의 해석이 두 군에서
  갈릴 수 있어 채점 단계에서 나눠 볼 수 있어야 한다.
- ``chained`` (bool): 같은 객체가 끼는 다른 객체-객체 접촉이 ±1.5초 안에 또 있는 사건.
  순도 검사는 창이 실제로 겹칠 때만 폐기하는데, 겹치지 않아도 세 물체가 짧은 시간에
  몰리면 "이 클립이 보여 주는 사건이 무엇인가"가 기하적으로 모호해진다.

--------------------------------------------------------------------------
순도 검사에서 벽 접촉을 무시하는 결정
--------------------------------------------------------------------------
한 창 안에 **다른 쌍의 객체-객체 접촉 구간**이 겹치면 그 창은 폐기한다. 조건이
주장하는 사건이 하나여야 발화/침묵을 그 사건에 귀속시킬 수 있기 때문이다.
반면 **벽 접촉은 순도 검사에서 무시한다.** 벽 접촉은 이 실험이 측정하려는 사건
(객체-객체 충돌)이 아니고, wander 안무에서 벽 접촉은 거의 항상 어딘가에서 일어나고
있어 이것까지 오염으로 치면 남는 창이 거의 없어진다. 즉 "창 안에 벽 접촉이 있어도
객체-객체 접촉이 기준 사건 하나뿐이면 순수하다"가 v3의 순도 정의다.

--------------------------------------------------------------------------
시계
--------------------------------------------------------------------------
trace·collisions·영상이 sim-클럭 단일 시계(capture_start + 오프셋)로 정합이므로,
절대 시각 창 -> 영상 오프셋 환산은 뺄셈 하나다. wall-clock 시절 에피소드는 이
전제가 깨져 입력으로 쓰면 안 된다.

사용 (EXT_ROOT에서, ffmpeg 필요 — 절단 단계만):
  python -m gist.netai.time_travel_summarization.automation.phase_clips \
    --collision-run artifacts/episodes/gen-20260804-phasecol-v2 \
    --nearmiss-run artifacts/episodes/gen-20260804-phasenm-v2 \
    --out artifacts/phase_ablation_v3 [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import math
import random
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ..playback.trajectory_repository import TrajectoryRepository

_parse = TrajectoryRepository.parse_timestamp

# (시각, 거리cm) 오름차순 목록 — 한 쌍의 지면 거리 시계열.
Series = List[Tuple[datetime.datetime, float]]
# (시각, {objid: (x, z)}) — 한 프레임의 지면 좌표.
Frames = List[Tuple[datetime.datetime, Dict[str, Tuple[float, float]]]]
Segments = List[Tuple[datetime.datetime, datetime.datetime]]

# --- 사건 정의 -------------------------------------------------------------
OBJECT_KIND = "object"      # collisions CSV에서 객체-객체 접촉만 고른다(v3 변경 1)
CONTACT_CLUSTER_S = 0.5     # 접촉 rows를 사건 하나로 묶는 시간 반경

# --- 접촉 구간 실측 --------------------------------------------------------
DEFAULT_TOUCH_THRESHOLD = 90.0   # 지면 거리 문턱(cm). 물리 접촉 정의 2r≈71.7cm보다 여유
DEFAULT_SEARCH_BACK_S = 1.5      # CSV 시각 기준 뒤쪽 탐색 여유(초 단위 절삭 대비)
DEFAULT_SEARCH_FWD_S = 1.5       # CSV 시각 기준 앞쪽 탐색 여유
DEFAULT_MAX_CONTACT_S = 3.0      # 이 안에 문턱을 회복 못 하면 사건으로 인정하지 않음

# --- 사건 속성 분류(진단 리포트 §4·§5 반영) -------------------------------
# 접촉 구간 길이는 단일 분포가 아니라 뚜렷한 이봉이다(진단 실측 113건):
# 짧은 접촉 74건 중앙값 0.134s(스치고 바로 떨어짐), 긴 접촉 39건 중앙값 1.300s
# (부딪힌 뒤 한동안 붙어 함께 움직임). 0.4~1.0s 구간은 사건이 하나도 없는 공백이라
# 경계를 그 한가운데인 0.7s에 둔다. 두 군은 조건 클립이 실제로 보여 주는 장면의
# 성격이 달라(특히 no_aftermath·no_contact) 채점 분석에서 분리해 볼 수 있어야 한다.
CONTACT_CLASS_BOUNDARY_S = 0.7
# 같은 객체가 끼는 다른 객체-객체 접촉이 이 시간 안에 또 있으면 연쇄 접촉으로 표기한다.
# 순도 검사는 창이 겹치는 경우만 걸러내지만, 겹치지 않더라도 세 물체가 짧은 시간에
# 몰리면 "이 클립의 사건이 무엇인가"가 기하적으로 모호해진다(진단 §2-1: 연쇄 6건이
# 최대 오차를 혼자 만들어 냈다). 폐기하지 않고 플래그만 달아 분석에서 분리하게 한다.
CHAIN_WINDOW_S = 1.5

# --- 창 규격 ---------------------------------------------------------------
CLIP_S = 2.0                # 모든 창의 총장(2초/20프레임 학습 계약 정합)
# 창 경계를 접촉 구간 경계에서 얼마나 떼어 둘지(v3.1). 0.05초는 1.5프레임에 해당하며
# 기준 시각의 남은 불확실성을 덮는 최소 가드다 — t_touch 실측 오차(진단 절대오차
# 중앙값 0.016초)에 60Hz 프레임 양자화(0.033초 = 1프레임)를 더한 값보다 크다.
# v3의 0.1~0.2초 가드는 이보다 과했고, 그만큼 조건이 겨냥한 국면 대신 인접 국면의
# 프레임을 창 안에 남기거나 필요한 프레임을 잘라냈다(아래 v3.1 변경 근거 참조).
EDGE_GUARD_S = 0.05
EXCISE_PAD_S = 0.1          # no_contact 앞 구간을 접촉 시작에서 떼어 두는 여유
NO_CONTACT_SEG_S = 1.0      # no_contact 스플라이스 각 구간 길이
CONTROL_BUFFER_S = 3.0      # 무관 창이 모든 이벤트와 유지해야 하는 최소 간격
PURITY_MARGIN_S = 0.25      # 순도 검사 시 창 가장자리 여유

# --- near-miss -------------------------------------------------------------
NEARMISS_ENTER_FRAC = 2.0        # 극소점 탐지 진입 문턱 = gap × 이 값
NEARMISS_DMIN_LO_MARGIN = 5.0    # d_min 하한 = gap − 이 값 (접촉 의심 배제)
NEARMISS_DMIN_HI_MARGIN = 30.0   # d_min 상한 = 문턱 + 이 값 (너무 먼 조우 배제)

# --- 게이트 ---------------------------------------------------------------
APPROACH_DECREASE_FRAC = 0.8     # approach_only에서 요구하는 "거리 비증가" 프레임 비율

# 창 규격 — (기준점 이름, [(시작 오프셋, 끝 오프셋)...]). 전부 총장 2.0s.
ANCHOR_TOUCH = "t_touch"
ANCHOR_RELEASE = "t_release"
WINDOW_SPECS: Dict[str, Tuple[str, List[Tuple[float, float]]]] = {
    # full: 접촉을 가운데 두고 접근·접촉·사후를 모두 포함(대조 조건). v3.1에서 불변.
    "full": (ANCHOR_TOUCH, [(-1.0, +1.0)]),
    # no_approach: 접촉이 이미 시작된 뒤에서 창을 열어 접근 동역학을 배제.
    # v3.1: 시작을 t_touch−0.1에서 t_touch+0.05로 옮겨 접근 프레임이 한 장도 남지
    # 않게 했다(v3는 접촉 직전 프레임을 최대 0.1초 남겨 접근 잔재가 섞였다).
    "no_approach": (ANCHOR_TOUCH, [(+EDGE_GUARD_S, +EDGE_GUARD_S + CLIP_S)]),
    # no_aftermath: 아직 붙어 있는 프레임에서 창을 닫아 사후 상호작용을 배제.
    # v3.1: 끝을 t_release+0.1에서 t_release−0.05로 옮겼다. v3는 회복 시각을 지나
    # 0.1초를 더 담아 두 물체가 떨어지기 시작하는 분리 장면이 들어갔는데, 그것이
    # 바로 이 조건이 배제하려는 사후 신호다.
    "no_aftermath": (ANCHOR_RELEASE, [(-EDGE_GUARD_S - CLIP_S, -EDGE_GUARD_S)]),
    # approach_only: 접촉 직전에 창을 닫아 접촉 프레임을 원천 배제.
    # v3.1: 끝을 t_touch−0.2에서 t_touch−0.05로 옮겨 접촉에 0.15초 더 붙였다.
    # 0.2초 가드는 기준 시각 불확실성(0.016초 + 1프레임)에 비해 과하게 보수적이라
    # 접근의 마지막 국면(가장 정보량이 큰 구간)을 불필요하게 잘라내고 있었다.
    "approach_only": (ANCHOR_TOUCH, [(-EDGE_GUARD_S - CLIP_S, -EDGE_GUARD_S)]),
    # no_contact는 t_touch·t_release 양쪽에 걸쳐 있어 고정 오프셋으로 표현할 수
    # 없다 — no_contact_segments()가 실측 구간에서 직접 만든다.
}
NEARMISS_SPEC: List[Tuple[float, float]] = [(-1.0, +1.0)]


# ---------------------------------------------------------------- trace 읽기

def trace_frames(trace_rows: Sequence[dict]) -> Frames:
    """trace rows -> [(시각, {objid: (x, z)})...] (시각 오름차순).

    y(높이)는 쓰지 않는다 — 접촉 판정은 지면 평면 위의 중심 간 거리로 하고,
    시뮬레이터의 접촉 규칙(수평 중심 거리 < collision_distance)과 정의를 맞춘다.
    """
    by_ts: Dict[str, Dict[str, Tuple[float, float]]] = defaultdict(dict)
    for r in trace_rows:
        by_ts[str(r["timestamp"])][str(r["objid"])] = (float(r["x"]), float(r["z"]))
    return [(_parse(ts), by_ts[ts]) for ts in sorted(by_ts, key=_parse)]


def pair_series(frames: Frames, a: str, b: str) -> Series:
    """두 객체가 모두 관측된 프레임만 모아 지면 거리 시계열을 만든다."""
    out: Series = []
    for t, objs in frames:
        pa, pb = objs.get(a), objs.get(b)
        if pa is None or pb is None:
            continue
        out.append((t, math.hypot(pa[0] - pb[0], pa[1] - pb[1])))
    return out


# ---------------------------------------------------------------- 사건 특정

def parse_event_time(
    raw: str, capture_start: Optional[datetime.datetime] = None,
) -> datetime.datetime:
    """collisions CSV의 timestamp -> datetime.

    collisions CSV는 날짜 없이 ``HH:MM:SS``만 남기는 반면 trace·meta는 날짜를 포함한
    전체 시각을 쓴다. 둘 다 같은 sim-클럭이므로, 날짜가 없는 표기는 capture_start의
    날짜를 붙여 복원한다(에피소드가 자정을 넘겼으면 하루 보정).
    """
    text = (raw or "").strip()
    try:
        return _parse(text)
    except ValueError:
        pass
    if capture_start is None:
        raise ValueError(f"날짜 없는 시각인데 기준 시각이 없다: {raw!r}")
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S"):
        try:
            clock = datetime.datetime.strptime(text, fmt).time()
        except ValueError:
            continue
        stamped = datetime.datetime.combine(capture_start.date(), clock)
        if (stamped - capture_start).total_seconds() < -43200:
            stamped += datetime.timedelta(days=1)
        return stamped
    raise ValueError(f"Unsupported timestamp format: {raw!r}")


def object_contact_clusters(
    collision_rows: Sequence[dict],
    capture_start: Optional[datetime.datetime] = None,
) -> Tuple[List[dict], Counter, int]:
    """collisions CSV rows -> 객체-객체 접촉 사건 후보 목록 + 폐기 사유 + 무시한 행 수.

    kind가 ``object``인 행만 남긴다(벽 접촉·stuck 재배치는 이 실험의 사건이 아니다).
    contact report는 접촉한 두 객체 각각에 대해 행을 남기므로 CONTACT_CLUSTER_S 안의
    행들을 한 사건으로 묶으면 objid가 정확히 2개 나온다 — 이 두 개가 접촉 쌍이다.
    objid가 2개가 아닌 클러스터(3체 이상 동시 접촉, 혹은 한쪽 행 유실)는 접촉 쌍을
    단정할 수 없으므로 폐기하고 사유를 집계한다.

    반환: ([{"t_csv", "pair": (a, b), "n_rows"}...], Counter(클러스터 폐기 사유),
           kind가 object가 아니라 무시한 **행** 수)
    """
    reasons: Counter = Counter()
    ignored_rows = 0
    stamped: List[Tuple[datetime.datetime, str]] = []
    for r in collision_rows:
        if str(r.get("kind", "")).strip() != OBJECT_KIND:
            ignored_rows += 1
            continue
        stamped.append((parse_event_time(str(r["timestamp"]), capture_start), str(r["objid"])))
    stamped.sort(key=lambda item: item[0])

    groups: List[List[Tuple[datetime.datetime, str]]] = []
    for item in stamped:
        if groups and (item[0] - groups[-1][0][0]).total_seconds() <= CONTACT_CLUSTER_S:
            groups[-1].append(item)
        else:
            groups.append([item])

    events: List[dict] = []
    for g in groups:
        ids = sorted({objid for _, objid in g})
        if len(ids) != 2:
            reasons["pair_not_two_objects"] += 1
            continue
        events.append({"t_csv": g[0][0], "pair": (ids[0], ids[1]), "n_rows": len(g)})
    return events, reasons, ignored_rows


def measure_contact(
    series: Series,
    t_csv: datetime.datetime,
    threshold: float = DEFAULT_TOUCH_THRESHOLD,
    search_back_s: float = DEFAULT_SEARCH_BACK_S,
    search_fwd_s: float = DEFAULT_SEARCH_FWD_S,
    max_contact_s: float = DEFAULT_MAX_CONTACT_S,
) -> Tuple[Optional[dict], Optional[str]]:
    """접촉 쌍의 거리 시계열에서 접촉 구간 [t_touch, t_release]를 실측한다.

    t_touch는 CSV 시각 주변 탐색창 안에서 거리가 문턱을 처음 하회한 순간이다.
    t_release는 t_touch 이후 거리가 문턱 이상으로 회복한 첫 순간인데, **탐색창
    바깥까지 이어서 본다** — 실측에서 접촉이 1.4초 넘게 이어져 회복 순간이 CSV
    시각 +2.0s를 넘는 사례가 있었기 때문이다. 대신 max_contact_s로 상한을 둬,
    회복이 없는(=붙어버린) 경우는 사건으로 인정하지 않는다.

    반환: ({"t_touch", "t_release", "contact_len_s", "d_min"}, None) 또는 (None, 사유).
    """
    lo = t_csv - datetime.timedelta(seconds=search_back_s)
    hi = t_csv + datetime.timedelta(seconds=search_fwd_s)
    t_touch: Optional[datetime.datetime] = None
    for t, d in series:
        if t < lo:
            continue
        if t > hi:
            break
        if d < threshold:
            t_touch = t
            break
    if t_touch is None:
        return None, "no_touch_in_search_window"

    cap = t_touch + datetime.timedelta(seconds=max_contact_s)
    t_release: Optional[datetime.datetime] = None
    for t, d in series:
        if t <= t_touch:
            continue
        if t > cap:
            break
        if d >= threshold:
            t_release = t
            break
    if t_release is None:
        return None, "no_release_within_cap"

    inside = [d for t, d in series if t_touch <= t <= t_release]
    contact_len_s = round((t_release - t_touch).total_seconds(), 3)
    return {
        "t_touch": t_touch,
        "t_release": t_release,
        "contact_len_s": contact_len_s,
        "contact_class": contact_class(contact_len_s),
        "d_min": round(min(inside), 1) if inside else None,
    }, None


def contact_class(contact_len_s: float, boundary_s: float = CONTACT_CLASS_BOUNDARY_S) -> str:
    """접촉 구간 길이를 이봉 분포의 두 계급으로 나눈다 — "short" 또는 "long".

    경계 0.7초의 근거는 CONTACT_CLASS_BOUNDARY_S 주석에 있다(실측 분포에서 0.4~1.0초가
    빈 구간이라 경계가 어디에 놓이든 분류가 바뀌지 않는다).
    """
    return "long" if contact_len_s >= boundary_s else "short"


def mark_chained(events: List[dict], window_s: float = CHAIN_WINDOW_S) -> None:
    """사건 목록에 ``chained`` 플래그를 채운다(제자리 수정).

    한 사건과 **객체를 공유하는** 다른 객체-객체 접촉이 window_s 안에 있으면 양쪽 모두
    chained=True다. 시간 근접은 두 접촉 **구간 사이의 간격**으로 잰다(겹쳐 있으면 간격 0).
    객체를 공유하지 않는 별개 쌍의 접촉은 세 물체가 몰린 상황이 아니므로 여기서는 세지
    않는다 — 그런 경우의 창 오염은 순도 검사가 따로 본다.
    """
    for e in events:
        e["chained"] = False
    for i, a in enumerate(events):
        for b in events[i + 1:]:
            if not (set(a["pair"]) & set(b["pair"])):
                continue
            gap = max((b["t_touch"] - a["t_release"]).total_seconds(),
                      (a["t_touch"] - b["t_release"]).total_seconds(), 0.0)
            if gap <= window_s:
                a["chained"] = True
                b["chained"] = True


def contact_events(
    frames: Frames,
    collision_rows: Sequence[dict],
    capture_start: Optional[datetime.datetime] = None,
    threshold: float = DEFAULT_TOUCH_THRESHOLD,
    search_back_s: float = DEFAULT_SEARCH_BACK_S,
    search_fwd_s: float = DEFAULT_SEARCH_FWD_S,
    max_contact_s: float = DEFAULT_MAX_CONTACT_S,
) -> Tuple[List[dict], Dict[str, object]]:
    """CSV의 객체-객체 접촉 후보를 trace 기하로 실측해 확정 사건 목록을 만든다.

    반환 항목: {"pair", "t_csv", "t_touch", "t_release", "contact_len_s", "d_min", "series"}.
    ``series``는 그 쌍의 전체 거리 시계열로, 뒤이어 조건 게이트가 이 시계열만 본다.
    함께 돌려주는 통계는 {"planned"(CSV 클러스터 수), "passed"(실측 성공 수),
    "dropped"(사유별 수), "ignored_rows"(kind가 object가 아니라 건너뛴 행 수)}.
    """
    candidates, reasons, ignored_rows = object_contact_clusters(collision_rows, capture_start)
    n_clusters = len(candidates) + sum(reasons.values())
    events: List[dict] = []
    for cand in candidates:
        a, b = cand["pair"]
        series = pair_series(frames, a, b)
        if not series:
            reasons["pair_missing_in_trace"] += 1
            continue
        measured, why = measure_contact(
            series, cand["t_csv"], threshold, search_back_s, search_fwd_s, max_contact_s)
        if measured is None:
            reasons[why or "measure_failed"] += 1
            continue
        events.append({**cand, **measured, "series": series})
    events.sort(key=lambda e: e["t_touch"])
    mark_chained(events)
    stats = {"planned": n_clusters, "passed": len(events),
             "dropped": reasons, "ignored_rows": ignored_rows,
             "chained": sum(1 for e in events if e["chained"]),
             "long_contacts": sum(1 for e in events if e["contact_class"] == "long")}
    return events, stats


def near_miss_events(frames: Frames, gap: float, enter_frac: float = NEARMISS_ENTER_FRAC) -> List[dict]:
    """trace -> 쌍별 지면 거리 극소점 목록(near-miss 기준점 t*).

    거리 시계열이 진입 문턱(= gap × enter_frac) 아래로 들어온 구간마다 최소 거리
    도달 시각을 1개 뽑는다. d_min이 접촉 의심 범위인 사례도 여기서는 그대로 담고,
    폐기는 뒤의 게이트(d_min 범위)와 순도 검사가 결정한다.
    반환: [{"t", "d_min", "pair", "series"}] (t 오름차순).
    """
    enter_thr = gap * enter_frac
    open_ranges: Dict[Tuple[str, str], dict] = {}
    events: List[dict] = []
    for t, objs in frames:
        ids = sorted(objs)
        seen_pairs = set()
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                d = math.hypot(objs[a][0] - objs[b][0], objs[a][1] - objs[b][1])
                pair = (a, b)
                seen_pairs.add(pair)
                if d < enter_thr:
                    cur = open_ranges.get(pair)
                    if cur is None or d < cur["d_min"]:
                        open_ranges[pair] = {"t": t, "d_min": d, "pair": pair}
                elif pair in open_ranges:
                    events.append(open_ranges.pop(pair))
        # 트랙이 끊긴 쌍의 열린 구간도 닫는다
        for pair in [p for p in open_ranges if p not in seen_pairs]:
            events.append(open_ranges.pop(pair))
    events += list(open_ranges.values())
    for ev in events:
        a, b = ev["pair"]
        ev["series"] = pair_series(frames, a, b)
    return sorted(events, key=lambda e: e["t"])


# ---------------------------------------------------------------- 창 만들기

def _window_abs(t_ref: datetime.datetime, spec: Sequence[Tuple[float, float]]) -> Segments:
    return [(t_ref + datetime.timedelta(seconds=s), t_ref + datetime.timedelta(seconds=e))
            for s, e in spec]


def no_contact_segments(
    t_touch: datetime.datetime, t_release: datetime.datetime,
    pad_s: float = EXCISE_PAD_S, seg_s: float = NO_CONTACT_SEG_S,
    back_pad_s: float = 0.0,
) -> Segments:
    """접촉 구간만 도려낸 스플라이스 창 = 접촉 직전 1초 + 접촉 직후 1초.

    v2는 기준 시각 ±0.5초를 고정으로 도려냈다. 접촉 구간 길이가 사건마다 0.2s~1.4s로
    다르므로(모듈 독스트링 참조) 고정 절제는 짧은 접촉에서는 멀쩡한 접근·사후 프레임을
    같이 버리고, 긴 접촉에서는 접촉 프레임을 남긴다(진단 실측: v2 no_contact 클립의
    37.6%에 접촉 장면이 그대로 남아 있었다). v3는 실측한 [t_touch, t_release]에
    ±pad_s만 더해 그만큼만 제거한다.

    **긴 접촉(contact_class="long", 중앙값 1.300초)에서의 주의**: 절제 폭이 접촉 길이를
    따라가므로 이음새에서 건너뛰는 시간이 그만큼 커진다 — 짧은 접촉은 0.4초 안팎을
    건너뛰지만 긴 접촉은 1.5초 이상을 건너뛴다. 이음새의 시간 점프가 클수록 두 객체의
    위치가 갑자기 바뀌어 보여 그 불연속 자체가 부자연 신호로 작용할 수 있다. 그렇다고
    긴 접촉을 no_contact 대상에서 빼면 조건이 짧은 접촉만 대표하게 되어 편향이 생기므로,
    **배제하지 않고 포함하되 contact_class로 표시만 한다** — 채점 분석에서 두 계급을
    나눠 보면 이 왜곡이 결과를 끌고 갔는지 확인할 수 있다.

    **v3.1 — 뒤 구간을 t_release에서 바로 연다(back_pad_s=0)**: v3는 뒤 구간을
    t_release+0.1부터 열어 튕겨 나가는 첫 0.1초를 버렸는데, 이 조건이 배제해야 하는
    것은 접촉 프레임이지 충돌 직후의 반응이 아니다. t_release는 지면 거리가 문턱
    90cm를 **회복한** 시각이고 실측 접촉 거리는 70.9~87.5cm 범위이므로, t_release
    이후 프레임은 정의상 이미 접촉 거리 밖이다 — 0.1초를 되돌려도 접촉 프레임이
    유입되지 않는다. 앞 구간은 t_touch−0.1로 그대로 두는데, 접촉 시작 직전 프레임은
    두 물체가 닿기 직전이라 시각적으로 접촉과 구분이 어렵기 때문이다.
    """
    pad = datetime.timedelta(seconds=pad_s)
    seg = datetime.timedelta(seconds=seg_s)
    left_end = t_touch - pad
    right_start = t_release + datetime.timedelta(seconds=back_pad_s)
    return [(left_end - seg, left_end), (right_start, right_start + seg)]


def _in_bounds(segs: Segments, start: datetime.datetime, end: datetime.datetime) -> bool:
    return all(start <= s and e <= end for s, e in segs)


def _samples_in(series: Series, segs: Segments) -> Series:
    return [(t, d) for t, d in series if any(s <= t <= e for s, e in segs)]


# ---------------------------------------------------------------- 순도 검사

def purity_violation(
    segs: Segments,
    contact_intervals: Sequence[Tuple[Tuple[str, str], datetime.datetime, datetime.datetime]],
    allow_pair: Optional[Tuple[str, str]] = None,
    allow_touch: Optional[datetime.datetime] = None,
) -> bool:
    """창 안(±PURITY_MARGIN_S)에 기준 사건이 아닌 **객체-객체 접촉 구간**이 겹치면 True.

    벽 접촉은 애초에 contact_intervals에 들어오지 않는다(모듈 독스트링의 순도 정의).
    """
    m = datetime.timedelta(seconds=PURITY_MARGIN_S)
    for pair, c0, c1 in contact_intervals:
        if allow_pair is not None and pair == allow_pair and c0 == allow_touch:
            continue
        for s, e in segs:
            if c0 - m <= e and c1 + m >= s:
                return True
    return False


# ---------------------------------------------------------------- 조건 게이트

def gate_window(
    condition: str,
    segs: Segments,
    series: Series,
    threshold: float,
    gap: float = 0.0,
    d_min: Optional[float] = None,
) -> Optional[str]:
    """창이 조건의 의미를 실제로 만족하는지 검사한다. 통과면 None, 아니면 폐기 사유.

    **기준 쌍의 거리 시계열에 대해서만** 평가한다 — 창 안의 다른 객체들은 이 판정에
    관여하지 않는다(그들이 만드는 오염은 순도 검사가 따로 본다). 검사 내용:

      full          : 접촉 프레임(거리 < 문턱)이 창에 실제로 들어있다.
      no_approach   : 접촉 프레임이 있고, **창의 첫 프레임이 이미 접촉 중**이다
                      (v3.1: 접근 프레임이 한 장도 남지 않았다는 기계적 확인).
      no_aftermath  : 접촉 프레임이 있고, **창의 마지막 프레임이 아직 접촉 중**이다
                      (v3.1: 분리 장면이 들어오지 않았다는 기계적 확인).
      approach_only : 창 전 구간이 문턱 밖이고, 종점 거리가 시점 거리보다 작으며,
                      거리가 줄어드는 프레임이 APPROACH_DECREASE_FRAC 이상이다
                      (= "접촉 없이 접근만"이라는 주장의 기계적 확인).
      no_contact    : 스플라이스 두 구간 모두 문턱 하회 순간이 없다(절제 성공 확인).
                      v3.1에서 뒤 구간이 t_release에서 바로 시작하므로 이 검사가
                      "회복 시각 이후에는 접촉이 없다"는 전제의 실제 확인이 된다.
      near_miss     : 창이 거리 극소점을 담고 있고, d_min이 [gap−5, 문턱+30] 안이다
                      (접촉해버린 조우도, 스치지도 않은 먼 조우도 배제).
      control       : 게이트 없음 — 무관 구간이라는 성질은 이벤트 간격(buffer)과
                      순도 검사로 이미 보장된다.
    """
    if condition == "control":
        return None

    samples = _samples_in(series, segs)
    if not samples:
        return "no_samples_in_window"

    if condition in ("full", "no_approach", "no_aftermath"):
        if not any(d < threshold for _, d in samples):
            return "no_contact_frames_in_window"
        # v3.1: 창 경계가 접촉 구간 안에 있어야 한다는 것까지 확인한다.
        # 접촉 프레임이 "들어있다"만으로는 no_approach 시작부에 접근 프레임이,
        # no_aftermath 종료부에 분리 프레임이 남아도 통과해 버린다.
        if condition == "no_approach" and samples[0][1] >= threshold:
            return "approach_frames_at_window_start"
        if condition == "no_aftermath" and samples[-1][1] >= threshold:
            return "separation_frames_at_window_end"
        return None

    if condition == "approach_only":
        if len(samples) < 2:
            return "too_few_samples"
        if any(d < threshold for _, d in samples):
            return "contact_frames_in_window"
        if samples[-1][1] >= samples[0][1]:
            return "not_closing"
        diffs = [samples[i + 1][1] - samples[i][1] for i in range(len(samples) - 1)]
        decreasing = sum(1 for x in diffs if x <= 0)
        if decreasing < APPROACH_DECREASE_FRAC * len(diffs):
            return "not_monotonic_approach"
        return None

    if condition == "no_contact":
        for seg in segs:
            part = _samples_in(series, [seg])
            if not part:
                return "empty_splice_segment"
            if any(d < threshold for _, d in part):
                return "contact_frames_survived_excision"
        return None

    if condition == "near_miss":
        if d_min is None:
            return "no_d_min"
        window_min = min(d for _, d in samples)
        if window_min > d_min + 1.0:
            return "minimum_outside_window"
        if d_min < gap - NEARMISS_DMIN_LO_MARGIN:
            return "d_min_below_gap"
        if d_min > threshold + NEARMISS_DMIN_HI_MARGIN:
            return "d_min_too_far"
        return None

    raise ValueError(f"unknown condition: {condition!r}")


# ---------------------------------------------------------------- 에피소드 계획

EVENT_STAT_KEY = "_events"   # 조건이 아니라 "사건 실측" 단계의 통계 슬롯


def _bump(stats: Dict[str, dict], cond: str, key: str, reason: str = "") -> None:
    slot = stats.setdefault(cond, {"planned": 0, "passed": 0, "dropped": Counter()})
    if key == "dropped":
        slot["dropped"][reason] += 1
    else:
        slot[key] += 1


def plan_episode(
    kind: str,
    trace_rows: Sequence[dict],
    collision_rows: Sequence[dict],
    capture_start: datetime.datetime,
    duration_s: float,
    gap: float = 95.0,
    threshold: float = DEFAULT_TOUCH_THRESHOLD,
    search_back_s: float = DEFAULT_SEARCH_BACK_S,
    search_fwd_s: float = DEFAULT_SEARCH_FWD_S,
    max_contact_s: float = DEFAULT_MAX_CONTACT_S,
    n_control: int = 1,
    seed: int = 42,
) -> Tuple[List[dict], Dict[str, dict]]:
    """에피소드 1개 -> (클립 계획 목록, 조건별 게이트 통계).

    kind="collision": 실측 접촉 구간마다 5조건(WINDOW_SPECS + no_contact) + control.
    kind="nearmiss" : 거리 극소점마다 near_miss 1조건 + control.

    계획 항목: {"condition", "t_ref", "anchor", "segments", "pair",
                "t_touch"/"t_release"/"contact_len_s" (충돌 조건), "d_min" (near_miss)}
    통계 항목: {조건: {"planned", "passed", "dropped": Counter(사유)}}
    """
    end = capture_start + datetime.timedelta(seconds=duration_s)
    frames = trace_frames(trace_rows)
    events, event_stats = contact_events(
        frames, collision_rows, capture_start, threshold, search_back_s, search_fwd_s, max_contact_s)
    stats: Dict[str, dict] = {EVENT_STAT_KEY: event_stats}
    minima = near_miss_events(frames, gap)
    # 순도 검사 기준: 이 에피소드에서 실측된 모든 객체-객체 접촉 구간
    contact_intervals = [(e["pair"], e["t_touch"], e["t_release"]) for e in events]
    plans: List[dict] = []

    def _consider(cond: str, segs: Segments, base: dict,
                  allow: Optional[dict] = None, d_min: Optional[float] = None) -> None:
        _bump(stats, cond, "planned")
        if not _in_bounds(segs, capture_start, end):
            _bump(stats, cond, "dropped", "out_of_episode_bounds")
            return
        allow_pair = allow["pair"] if allow else None
        allow_touch = allow["t_touch"] if allow else None
        if purity_violation(segs, contact_intervals, allow_pair, allow_touch):
            _bump(stats, cond, "dropped", "purity_other_object_contact")
            return
        why = gate_window(cond, segs, base.get("series", []), threshold, gap, d_min)
        if why:
            _bump(stats, cond, "dropped", why)
            return
        _bump(stats, cond, "passed")
        entry = {"condition": cond, "segments": segs, **{k: v for k, v in base.items() if k != "series"}}
        if d_min is not None:
            entry["d_min"] = round(d_min, 1)
        plans.append(entry)

    if kind == "collision":
        for ev in events:
            base = {"pair": list(ev["pair"]), "t_touch": ev["t_touch"], "t_release": ev["t_release"],
                    "contact_len_s": ev["contact_len_s"], "contact_class": ev["contact_class"],
                    "chained": ev["chained"], "series": ev["series"]}
            for cond, (anchor, spec) in WINDOW_SPECS.items():
                t_ref = ev[anchor]
                _consider(cond, _window_abs(t_ref, spec),
                          {**base, "t_ref": t_ref, "anchor": anchor}, allow=ev)
            _consider("no_contact", no_contact_segments(ev["t_touch"], ev["t_release"]),
                      {**base, "t_ref": ev["t_touch"], "anchor": ANCHOR_TOUCH}, allow=ev)
    elif kind == "nearmiss":
        for nm in minima:
            _consider("near_miss", _window_abs(nm["t"], NEARMISS_SPEC),
                      {"pair": list(nm["pair"]), "t_ref": nm["t"], "anchor": "t_star",
                       "series": nm["series"]},
                      allow=None, d_min=nm["d_min"])
    else:
        raise ValueError(f"unknown kind: {kind!r}")

    # control: 모든 이벤트(접촉 구간 + near-miss 극소점)에서 buffer 이상 떨어진 2초 창
    event_times: List[datetime.datetime] = []
    for _, c0, c1 in contact_intervals:
        event_times += [c0, c1]
    event_times += [nm["t"] for nm in minima]
    rng = random.Random(seed)
    tries, made = 0, 0
    while made < n_control and tries < 200:
        tries += 1
        off = rng.uniform(0.0, max(0.0, duration_s - CLIP_S))
        s = capture_start + datetime.timedelta(seconds=off)
        e = s + datetime.timedelta(seconds=CLIP_S)
        if all(abs((t - s).total_seconds()) > CONTROL_BUFFER_S and
               abs((t - e).total_seconds()) > CONTROL_BUFFER_S for t in event_times):
            _bump(stats, "control", "planned")
            _bump(stats, "control", "passed")
            plans.append({"condition": "control", "t_ref": s, "anchor": "window_start",
                          "segments": [(s, e)]})
            made += 1
    return plans, stats


def merge_stats(total: Dict[str, dict], part: Dict[str, dict]) -> None:
    """에피소드별 게이트 통계를 run 전체 통계에 누적한다."""
    for cond, slot in part.items():
        agg = total.setdefault(cond, {"planned": 0, "passed": 0, "dropped": Counter()})
        for key, value in slot.items():
            if key == "dropped":
                agg.setdefault("dropped", Counter()).update(value)
            else:
                agg[key] = agg.get(key, 0) + value


def format_stats(total: Dict[str, dict]) -> List[str]:
    """조건별 (계획 수, 게이트 통과 수, 폐기 사유 집계) 사람이 읽을 줄 목록."""
    lines = []
    for cond in sorted(total):
        slot = total[cond]
        drops = ", ".join(f"{k}={v}" for k, v in sorted(slot["dropped"].items())) or "-"
        notes = []
        if slot.get("ignored_rows"):
            notes.append(f"kind!=object 무시 행 {slot['ignored_rows']}")
        if "long_contacts" in slot:
            notes.append(f"긴 접촉 {slot['long_contacts']}/{slot['passed']}")
        if "chained" in slot:
            notes.append(f"연쇄 {slot['chained']}/{slot['passed']}")
        extra = f" ({', '.join(notes)})" if notes else ""
        lines.append(f"  {cond:<14} planned={slot['planned']:<5} passed={slot['passed']:<5} "
                     f"dropped: {drops}{extra}")
    return lines


# ---------------------------------------------------------------- 에피소드 IO

def _episode_files(run_dir: Path) -> List[dict]:
    """run 디렉터리(중첩 포함)에서 (trace, video, meta, collisions) 묶음 수집."""
    out = []
    for meta_path in sorted(run_dir.rglob("_video_*.meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        video = meta_path.parent / meta_path.name.replace(".meta.json", ".mp4")
        idx = meta_path.name.replace("_video_", "").replace(".meta.json", "")
        trace = meta_path.parent / f"_trace_{idx}.csv"
        if not (video.exists() and trace.exists()):
            continue
        out.append({"video": video, "trace": trace, "meta": meta,
                    "collisions": _resolve_collisions(meta_path.parent, meta)})
    return out


def _resolve_collisions(ep_dir: Path, meta: dict) -> Optional[Path]:
    """에피소드의 collisions CSV 실제 위치를 찾는다.

    meta의 ``collisions_csv``는 생성 당시 작업 머신 기준 절대 경로라 다른 머신(L40 등)
    에서는 존재하지 않는다. 반면 생성기는 CSV 사본을 에피소드 디렉터리에 함께 남기므로,
    에피소드 옆의 ``collisions_*.csv``를 우선 신뢰하고 meta 경로는 보조로 쓴다.
    (이 해석이 실패해 collision_rows가 빈 채로 돌면 접촉 기준 5조건이 통째로 0건이 되고
     control·near_miss만 남는다 — 조용한 전멸이라 우선순위를 이렇게 둔다.)
    """
    siblings = sorted(ep_dir.glob("collisions_*.csv"))
    if siblings:
        return siblings[0]
    col = meta.get("collisions_csv")
    if not col:
        return None
    path = Path(col) if Path(col).is_absolute() else (ep_dir / col)
    return path if path.exists() else None


def _read_csv_rows(path: Path) -> List[dict]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------- ffmpeg 절단

def ffmpeg_cmd(video: Path, segments: Segments, capture_start: datetime.datetime,
               out_path: Path) -> List[str]:
    """창(절대 시각) -> ffmpeg 명령. 단일 구간도 스플라이스와 동일하게 필터
    그래프로 재인코딩한다 — 조건 간 인코딩 차이를 없애기 위해(설계 §5-4)."""
    parts, inputs = [], []
    for i, (s, e) in enumerate(segments):
        off = (s - capture_start).total_seconds()
        dur = (e - s).total_seconds()
        parts.append(f"[0:v]trim=start={off:.3f}:duration={dur:.3f},setpts=PTS-STARTPTS[v{i}]")
        inputs.append(f"[v{i}]")
    graph = ";".join(parts) + f";{''.join(inputs)}concat=n={len(segments)}:v=1:a=0[out]"
    return ["ffmpeg", "-y", "-i", str(video), "-filter_complex", graph,
            "-map", "[out]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-an", str(out_path)]


# ---------------------------------------------------------------- CLI

def _fmt(t: datetime.datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--collision-run", action="append", default=[],
                    help="충돌 에피소드 run 디렉터리(반복 가능)")
    ap.add_argument("--nearmiss-run", action="append", default=[],
                    help="near-miss 에피소드 run 디렉터리(반복 가능)")
    ap.add_argument("--out", required=True, help="클립 세트 출력 디렉터리")
    ap.add_argument("--gap", type=float, default=95.0, help="near-miss gap(cm)")
    ap.add_argument("--touch-threshold", type=float, default=DEFAULT_TOUCH_THRESHOLD,
                    help="접촉 판정 지면 거리 문턱(cm). 물리 접촉 정의는 2r≈72cm")
    ap.add_argument("--search-back", type=float, default=DEFAULT_SEARCH_BACK_S,
                    help="CSV 시각 기준 뒤쪽 접촉 탐색 여유(초)")
    ap.add_argument("--search-fwd", type=float, default=DEFAULT_SEARCH_FWD_S,
                    help="CSV 시각 기준 앞쪽 접촉 탐색 여유(초)")
    ap.add_argument("--max-contact", type=float, default=DEFAULT_MAX_CONTACT_S,
                    help="접촉 구간 길이 상한(초). 넘으면 사건 폐기")
    ap.add_argument("--controls-per-episode", type=int, default=1)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dry-run", action="store_true", help="ffmpeg 없이 계획만 출력")
    args = ap.parse_args(argv)

    out_root = Path(args.out)
    manifest: List[dict] = []
    counts: Dict[str, int] = defaultdict(int)
    total_stats: Dict[str, dict] = {}

    jobs = [("collision", Path(r)) for r in args.collision_run] + \
           [("nearmiss", Path(r)) for r in args.nearmiss_run]
    if not jobs:
        raise SystemExit("입력 없음 — --collision-run / --nearmiss-run 지정")

    for kind, run_dir in jobs:
        eps = _episode_files(run_dir)
        print(f"[phase_clips] {kind} run {run_dir}: {len(eps)} episodes")
        for ep in eps:
            meta = ep["meta"]
            cap0 = _parse(str(meta.get("capture_start")))
            dur = float(meta.get("duration_s") or 0.0)
            trace_rows = _read_csv_rows(ep["trace"])
            col_rows = _read_csv_rows(ep["collisions"]) if ep["collisions"] else []
            plans, stats = plan_episode(
                kind, trace_rows, col_rows, cap0, dur, gap=args.gap,
                threshold=args.touch_threshold, search_back_s=args.search_back,
                search_fwd_s=args.search_fwd, max_contact_s=args.max_contact,
                n_control=args.controls_per_episode, seed=args.seed)
            merge_stats(total_stats, stats)
            for p in plans:
                cond = p["condition"]
                counts[cond] += 1
                name = f"{cond}_{ep['video'].parent.name}_{ep['video'].stem}_{counts[cond]:04d}.mp4"
                out_path = out_root / cond / name
                entry = {
                    "condition": cond,
                    "clip": str(out_path.relative_to(out_root)) if not args.dry_run else name,
                    "source_video": str(ep["video"]),
                    "anchor": p.get("anchor"),
                    "t_ref": _fmt(p["t_ref"]),
                    "segments": [[round((s - cap0).total_seconds(), 3),
                                  round((e - cap0).total_seconds(), 3)]
                                 for s, e in p["segments"]],
                }
                for key in ("pair", "contact_len_s", "contact_class", "chained", "d_min"):
                    if key in p:
                        entry[key] = p[key]
                for key in ("t_touch", "t_release"):
                    if key in p:
                        entry[key] = _fmt(p[key])
                manifest.append(entry)
                if not args.dry_run:
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    cmd = ffmpeg_cmd(ep["video"], p["segments"], cap0, out_path)
                    res = subprocess.run(cmd, capture_output=True, text=True)
                    if res.returncode != 0:
                        print(f"[phase_clips] ffmpeg FAILED {name}: {res.stderr[-300:]}")
                        manifest[-1]["error"] = "ffmpeg failed"

    print("[phase_clips] counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print("[phase_clips] gate stats (planned / passed / dropped reasons):")
    for line in format_stats(total_stats):
        print(line)

    params = {
        "touch_threshold_cm": args.touch_threshold,
        "search_back_s": args.search_back,
        "search_fwd_s": args.search_fwd,
        "max_contact_s": args.max_contact,
        "excise_pad_s": EXCISE_PAD_S,
        "edge_guard_s": EDGE_GUARD_S,
        "contact_class_boundary_s": CONTACT_CLASS_BOUNDARY_S,
        "chain_window_s": CHAIN_WINDOW_S,
        "gap": args.gap,
        "seed": args.seed,
    }
    gate_stats = {c: {**{k: v for k, v in s.items() if k != "dropped"},
                      "dropped": dict(s["dropped"])}
                  for c, s in total_stats.items()}
    if not args.dry_run:
        out_root.mkdir(parents=True, exist_ok=True)
        (out_root / "clips_manifest.json").write_text(
            json.dumps({**params, "extractor_version": "v3.1", "gate_stats": gate_stats,
                        "clips": manifest}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[phase_clips] manifest -> {out_root / 'clips_manifest.json'}")
    else:
        for e in manifest[:10]:
            print(f"  {e['condition']}: t_ref={e['t_ref']} segs={e['segments']}")
        if len(manifest) > 10:
            print(f"  ... ({len(manifest)} total)")


if __name__ == "__main__":
    main()
