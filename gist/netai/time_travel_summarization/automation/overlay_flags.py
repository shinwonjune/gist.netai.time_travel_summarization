"""overlay_overlap 픽셀 판정기 — 렌더된 프레임에서 화면 겹침을 직접 잰다.

설계: docs/위상분해_실험설계.md §5-2(확정된 픽셀 판정)와 §6-1(분석 규칙 사전 등록).
대상은 **접촉이 없는 조건 전부**(near_miss / no_contact / approach_only / control)이고,
산출은 클립별 ``overlay_overlap``(bool)과 ``overlay_overlap_frames``(겹친 프레임 수)를
기존 clips_manifest.json에 **재절단 없이** 덧쓰는 것이다.

--------------------------------------------------------------------------
왜 픽셀 기반인가 — 좌표로 화면 위치를 다시 계산하는 길이 막혀 있다
--------------------------------------------------------------------------
이 플래그가 필요한 이유부터 적는다. near_miss 조건에서 두 객체가 화면상 붙어 보이는
구간이 있는데, 확대해서 확인하면 그때 쌍의 지면 거리는 92~95cm로 접촉 문턱 밖이라
물리적 접촉은 없다. 부감(위에서 내려다보는) 시점에서 오버레이 라벨이 실제
지오메트리보다 넓은 영역을 차지해 생기는 착시다. 그래서 near_miss에서 모델이
발화했을 때 그것을 "근접 기하에 속았다"로 읽을지 "오버레이가 겹쳐 보여 속았다"로
읽을지가 갈리고, 채점 **전에** 겹침 여부를 박아 두어야 사후 선별 편향 없이 층화
분석을 할 수 있다.

처음 구현안이던 **중심 거리 단일 문턱(두 오버레이 반지름의 합 102cm)** 은 철회됐다.
그 판정은 "겹쳐 보이는지가 화면 위치와 무관하게 중심 거리만의 함수"라고 가정하는데,
오버레이 원이 객체의 **발밑 지면**에 그려지고 카메라가 부감이라 객체가 화면 중앙에서
멀어질수록 몸통이 원 윤곽 밖으로 길게 삐져나온다 — 같은 중심 거리라도 화면 어디에
있느냐에 따라 겹쳐 보임이 달라진다. 게다가 A의 오버레이가 B의 **객체 픽셀**과 겹치는
종류의 겹침은 원 두 개의 관계만 보는 문턱으로는 아예 셀 수 없다.

다음 대안이던 **trace 월드 좌표를 카메라 행렬로 사후 재투영**하는 길도 막혀 있다.
렌더 시점의 오버레이는 렌더러가 자기 ``camera_params``를 그대로 쓰는 정합 경로로
그려지는 반면, 사후에 우리가 trace 좌표로 화면 위치를 재계산하는 경로는 그 정합이
보장되지 않는다. 실제로 재계산 위치가 프레임 속 실제 위치와 어긋나는 증상(인터랙티브
캡처의 overlay 투영 오프셋 한계, ``docs/플랫폼고도화_보완사항.md`` §6-1)이 헤드리스로
렌더한 프레임에서도 실측으로 재현됐다.

그래서 남는 유일한 길이 **좌표를 거치지 않고 렌더된 픽셀에서 직접 재는 것**이다.
이 모듈이 그것이고, 판정은 모델이 실제로 보는 추론 프레임 20장 전수에 대해 한다
(최근접 순간 1장으로 대표하는 근사는 기각됐다 — 겹쳐 보임이 거리뿐 아니라 화면
위치에도 좌우되므로 최근접 순간이 최악 케이스라는 보장이 없기 때문이다).

--------------------------------------------------------------------------
판정 절차 (프레임 1장)
--------------------------------------------------------------------------
1. **오버레이 원 검출** (``detect_circles``): 오버레이는 배경보다 밝고 형태가 원이라는
   두 성질을 함께 쓴다. 밝은 원판 + 그 둘레의 **검은 테두리**를 같이 요구하는데,
   테두리가 실제 판별력의 대부분을 낸다 — 방 안에는 밝은 가구가 여럿 있지만 자기
   둘레가 새까만 고정 반지름 원판은 오버레이뿐이다(상수 근거는 아래 "원 크기 실측").
2. **객체 픽셀 검출** (``background_percentile`` + ``motion_mask``): 카메라가 고정이므로
   각 픽셀의 시계열은 대부분 배경이고 객체가 지나갈 때만 다른 값이다. 클립 20장에서
   픽셀별 **하위 백분위** 값을 빈 방(정지 배경)으로 추정하고, 거기서 크게 벗어난 픽셀을
   객체로 본다. 프레임끼리 직접 차분하는 방식과 중앙값 배경을 왜 차례로 버렸는지는
   ``background_percentile`` 참조 — 전자는 **느린 객체의 텅 빈 블롭**을, 후자는 오래
   머문 객체의 **배경 흡수와 유령 블롭**을 만든다.
3. **원 ↔ 자기 블롭 연관** (``attribute_circles``): 오버레이는 자기 객체 발밑에 그려져
   **항상 자기 블롭과 겹친다.** 이 자기 겹침을 겹침으로 세면 모든 프레임이 양성이 되므로
   반드시 제외해야 하고, 그러려면 각 원이 어느 블롭의 것인지 먼저 정해야 한다. 원
   중심이 놓인 연결 성분을 그 원의 자기 블롭으로 삼는다.
4. **판정** (``frame_verdict``): 세 가지 중 하나라도 성립하면 그 프레임은 겹침이다.
   - ``circle_circle``  : 두 오버레이 원의 원판이 교차(중심 거리 < 2 × 원 반지름).
   - ``circle_object``  : 어떤 원의 원판 안에 **자기 것이 아닌** 블롭 픽셀이 들어 있다.
   - ``merged_blob``    : 두 원의 자기 블롭이 **같은 연결 성분**이다. 두 객체의 움직이는
     픽셀 영역이 하나로 이어져 있다는 뜻, 즉 화면에서 둘 사이에 배경 틈이 없다는 뜻이다.
     이 경우 연결 성분만으로는 어느 픽셀이 누구 것인지 가를 수 없어 ``circle_object``가
     원리적으로 판정 불능이 되므로, 성분이 합쳐졌다는 사실 자체를 근거로 쓴다.
     사유를 따로 세어 두므로(``reasons``) 육안 대조에서 이 기전만 과검출로 판명되면
     ``--no-merged-blob``으로 끄고 재집계할 수 있다.
5. **클립 집계** (``clip_verdict``): 추론 프레임 20장 중 **1장이라도** 겹침이면 그 클립의
   ``overlay_overlap``은 True다. 겹친 프레임 수도 ``overlay_overlap_frames``로 남긴다.

--------------------------------------------------------------------------
원 크기 실측 — 상수의 근거
--------------------------------------------------------------------------
오버레이 원은 렌더러가 ``CircleLabel(radius=12, border_width=2)``로 그린다
(``video_capture/realtime_capture.py``). 그 값을 그대로 믿지 않고 실제 산출물
프레임에서 한 번 쟀다 — 인코딩·리사이즈를 거친 뒤의 픽셀이 판정 대상이기 때문이다.

표본: ``artifacts/phase_ablation_v3/gate_samples/near_miss/
near_miss_ep_0001__video_0001_0005.mp4``의 10fps 11번째 프레임(720x480). 원 중심
(490, 245)을 가로·세로로 훑은 실측값은 다음과 같았다.

  - 밝은 원판: 가로 x=480..500, 세로 y=235..255 → **반지름 10px**, 값 241~255
    (원 채움은 알파 220의 흰색이라 아래 깔린 객체 위에서 250 언저리로 나온다)
  - 검은 테두리: |dx| = 11~12 위치에서 값 0~3 → **반지름 11~12px의 검은 링**
  - 원 안의 ID 숫자는 어두운 획이라 원판 안에 값 11~59의 픽셀을 만든다
    → 원판이 100% 밝다고 요구하면 안 되고, 여유를 둬야 한다(``FILL_MIN``)
  - 같은 프레임 밝기 분포: 값 ≥ 230인 픽셀이 전체의 0.24%뿐이고 그 대부분이 두 원이다.
    객체 몸통·흰 가구는 210~220에 몰려 있어 원판 밝기와 겹치므로, 밝기만으로는
    가릴 수 없고 검은 테두리 조건이 함께 필요하다는 것이 이 수치로 확인된다.

따라서 원판 검사 반지름은 실측 10px보다 한 픽셀 안쪽인 9px(``R_FILL``), 테두리
검사 반지름은 11.5px(``R_RIM``), 겹침 기하에 쓰는 오버레이 반경은 실제 footprint인
12px(``R_OVERLAY``)로 둔다. 해상도가 다른 산출물에서는 CLI ``--overlay-radius``로
스케일을 바꿔 준다(세 값이 함께 비례한다).

--------------------------------------------------------------------------
알려진 한계
--------------------------------------------------------------------------
- 이 판정은 **화면에 그렇게 보이는가**만 말한다. near_miss에서 "겹쳐 보이는 클립"과
  "가장 가까이 접근한 클립"이 대체로 같은 클립이라 오버레이 요인과 근접 요인은 여전히
  교락돼 있다. 교락을 실제로 가르려면 같은 구간을 **오버레이 없이 다시 렌더해 짝
  비교**해야 한다(설계 §3).
- 객체가 클립 **내내** 한 자리에 멈춰 있으면 그 픽셀의 값이 어느 백분위로도 배경과
  구별되지 않아 블롭이 생기지 않는다(충돌 후 맞물려 멈춘 경우가 그렇다). 그런 원은
  ``circle_object`` 검사에서 제외하고(자기 것과 남의 것을 가를 수 없으므로 거짓 양성이
  나기 때문이다) ``unattributed``로 센다. 그 프레임에서도 ``circle_circle`` 검사는
  그대로 유효하다.
- A의 원이 B의 원을 거의 덮어 버리면 B의 원은 테두리가 가려져 검출되지 않을 수 있다.
  그 경우에도 두 객체의 블롭은 붙어 있으므로 ``merged_blob``이 잡는다.
- 도구 신뢰도는 사람이 직접 봐야 확정된다(설계 §5-2). ``--debug-samples N``으로 판정
  근거를 그린 주석 이미지를 남기고, 표본 20~30장을 육안 대조해 오판율을 재라.
  오판율이 허용선을 넘으면 이 플래그는 층화 근거가 아니라 관찰 병기로 강등한다.

사용 (EXT_ROOT에서, ffmpeg 필요):
  python -m gist.netai.time_travel_summarization.automation.overlay_flags \\
    --manifest artifacts/phase_ablation_v3/clips_manifest.json \\
    --clip-root artifacts/phase_ablation_v3 \\
    --debug-samples 5
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import tempfile
from collections import Counter, deque
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:                                  # 있으면 마스크 계산만 벡터화한다(결과는 동일)
    import numpy as _np
except ImportError:                   # pragma: no cover - 서버 venv에는 있으나 가정하지 않는다
    _np = None

# --- 판정 대상 조건 ---------------------------------------------------------
# 접촉이 없는 조건 전부(설계 §5-2 말미). 충돌 조건은 접촉 자체가 곧 겹침이라 플래그가
# 정보를 주지 않으므로 대상이 아니다.
TARGET_CONDITIONS = ("near_miss", "no_contact", "approach_only", "control")

# --- 추론 프레임 계약 -------------------------------------------------------
# 모델이 실제로 보는 프레임과 같은 샘플링이어야 판정이 의미를 갖는다. 클립은 2초이고
# 서버는 클립당 20프레임을 본다(train==infer로 고정된 NFRAMES=20) → 10fps.
DEFAULT_FRAMES = 20
DEFAULT_FPS = 10.0

# --- 오버레이 원 상수 (모듈 독스트링 "원 크기 실측" 참조) -------------------
R_OVERLAY = 12.0     # 오버레이의 실제 footprint 반지름(검은 테두리 바깥). 겹침 기하에 쓴다
R_FILL = 9           # 밝은 원판 검사 반지름 — 실측 10px보다 1px 안쪽
R_RIM = 11.5         # 검은 테두리 검사 반지름 — 실측 링(11~12px)의 한가운데
BRIGHT_THR = 200     # 원판으로 인정할 밝기 하한. 실측 원판은 241~255, 밝은 가구가 210~220이라
                     # 이 문턱만으로는 가구가 섞이지만 테두리 조건이 그것을 걸러낸다
DARK_THR = 70        # 테두리로 인정할 밝기 상한. 실측 테두리는 0~3, 어두운 카펫이 35 근처
FILL_MIN = 0.72      # 원판 중 밝아야 하는 비율. ID 숫자 획이 원판 안에 어두운 픽셀을 만든다
RIM_MIN = 0.55       # 테두리 중 어두워야 하는 비율. 원이 서로 겹치면 테두리 일부가 가려지므로
                     # 절반 조금 넘게만 요구한다(겹침이 곧 판정 대상이라 놓치면 안 된다)

# --- 객체 픽셀(차분) 상수 ---------------------------------------------------
DIFF_THR = 25        # 배경 추정치와 이보다 크게 다른 픽셀을 객체로 본다(인코딩 잡음 여유)
BG_PERCENTILE = 25.0  # 배경 추정에 쓰는 하위 백분위. 근거는 background_percentile 참조
MIN_BLOB_PX = 60     # 이보다 작은 연결 성분은 잡음으로 버린다(객체 몸통은 수백 px)
MIN_FOREIGN_PX = 8   # 남의 블롭 픽셀이 원판 안에 이만큼 이상 들어와야 겹침으로 센다

BACKGROUND = 0       # 연결 성분 라벨에서 배경을 뜻하는 값


# ---------------------------------------------------------------- 이미지 컨테이너

class Gray:
    """8bit 그레이스케일 프레임 — 픽셀은 행 우선 1차원 목록.

    PIL Image에 직접 매달지 않고 얇은 컨테이너를 두는 이유는, 판정 로직 전체를
    합성 배열만으로 단위 테스트할 수 있게 하기 위해서다(테스트에 ffmpeg·이미지 파일이
    필요 없다).
    """

    __slots__ = ("w", "h", "px")

    def __init__(self, w: int, h: int, px: Sequence[int]):
        if len(px) != w * h:
            raise ValueError(f"픽셀 수 불일치: {len(px)} != {w}*{h}")
        self.w = w
        self.h = h
        self.px = px

    @classmethod
    def blank(cls, w: int, h: int, value: int = 0) -> "Gray":
        return cls(w, h, [value] * (w * h))

    @classmethod
    def from_image(cls, image) -> "Gray":
        """PIL Image -> Gray."""
        g = image.convert("L")
        return cls(g.width, g.height, list(g.getdata()))

    def at(self, x: int, y: int) -> int:
        return self.px[y * self.w + x]


class Mask:
    """같은 크기의 정수 라벨 격자. 0은 배경, 1 이상은 연결 성분 번호."""

    __slots__ = ("w", "h", "px", "n_labels", "areas")

    def __init__(self, w: int, h: int, px: Sequence[int], n_labels: int = 0,
                 areas: Optional[Dict[int, int]] = None):
        self.w = w
        self.h = h
        self.px = px
        self.n_labels = n_labels
        self.areas = areas or {}

    def at(self, x: int, y: int) -> int:
        return self.px[y * self.w + x]


# ---------------------------------------------------------------- 원 검출

def _disc_offsets(radius: float) -> List[Tuple[int, int]]:
    r = int(math.floor(radius))
    return [(dx, dy) for dy in range(-r, r + 1) for dx in range(-r, r + 1)
            if dx * dx + dy * dy <= radius * radius]


def _ring_offsets(radius: float, n: int = 32) -> List[Tuple[int, int]]:
    """반지름 위 n개 점의 정수 오프셋(중복 제거). 테두리 검사에 쓴다."""
    pts: List[Tuple[int, int]] = []
    seen: Set[Tuple[int, int]] = set()
    for i in range(n):
        a = 2.0 * math.pi * i / n
        p = (int(round(radius * math.cos(a))), int(round(radius * math.sin(a))))
        if p not in seen:
            seen.add(p)
            pts.append(p)
    return pts


class Circle:
    """검출된 오버레이 원 하나."""

    __slots__ = ("x", "y", "r", "fill", "rim")

    def __init__(self, x: int, y: int, r: float, fill: float = 1.0, rim: float = 1.0):
        self.x = x
        self.y = y
        self.r = r
        self.fill = fill
        self.rim = rim

    @property
    def score(self) -> float:
        return self.fill + self.rim

    def __repr__(self) -> str:      # pragma: no cover - 디버깅 편의
        return f"Circle(x={self.x}, y={self.y}, r={self.r}, fill={self.fill:.2f}, rim={self.rim:.2f})"


def _center_candidates(gray: Gray, bright: int, probe: int, margin: int) -> Iterable[Tuple[int, int]]:
    """원 중심 후보 — 상하좌우로 probe만큼 떨어진 네 점이 모두 밝은 픽셀.

    전 픽셀에 원판·테두리 검사를 돌리면 느리므로, 통과할 수 없는 픽셀을 먼저 네 번의
    비교로 쳐낸다(밝은 원판의 한가운데라면 반드시 만족하는 필요조건이다).

    **후보 자신의 밝기는 보지 않는다.** 오버레이 원 한가운데에는 ID 숫자가 그려져 있어
    정중앙 픽셀이 어두운 경우가 흔하고(실측 원판 안 획 값 11~59), 자기 밝기를 요구하면
    바로 그 참 중심이 후보에서 빠진다. 그러면 검출이 몇 px 치우친 후보에 의존하게 되는데,
    치우친 중심에서는 테두리 검사 원이 검은 링을 벗어나 주변 픽셀을 훑으므로 주변이
    밝을 때(객체 몸통 위) 검출을 통째로 놓친다 — 합성 프레임으로 재현해 고정해 두었다.

    numpy가 있으면 이 선별만 벡터화한다 — 결과 집합은 순수 파이썬 경로와 동일하다.
    """
    w, h, px = gray.w, gray.h, gray.px
    lo, hi_x, hi_y = margin, w - margin, h - margin
    if _np is not None:
        b = (_np.asarray(px, dtype=_np.uint8).reshape(h, w) >= bright)
        core = (b[lo:hi_y, lo - probe:hi_x - probe] & b[lo:hi_y, lo + probe:hi_x + probe]
                & b[lo - probe:hi_y - probe, lo:hi_x] & b[lo + probe:hi_y + probe, lo:hi_x])
        ys, xs = _np.nonzero(core)
        return zip((xs + lo).tolist(), (ys + lo).tolist())

    out: List[Tuple[int, int]] = []
    for y in range(lo, hi_y):
        base = y * w
        for x in range(lo, hi_x):
            if (px[base + x - probe] >= bright and px[base + x + probe] >= bright
                    and px[base + x - probe * w] >= bright
                    and px[base + x + probe * w] >= bright):
                out.append((x, y))
    return out


def detect_circles(
    gray: Gray,
    r_fill: int = R_FILL,
    r_rim: float = R_RIM,
    r_overlay: float = R_OVERLAY,
    bright: int = BRIGHT_THR,
    dark: int = DARK_THR,
    fill_min: float = FILL_MIN,
    rim_min: float = RIM_MIN,
) -> List[Circle]:
    """프레임에서 오버레이 원을 찾는다 — 밝은 원판 + 검은 테두리 고정 크기 매칭.

    두 성질을 함께 요구하는 이유는 실측 밝기 분포에 있다(모듈 독스트링). 밝기만
    보면 흰 가구·객체 몸통(210~220)이 원판 밝기와 겹쳐 걸러지지 않는데, **자기 둘레가
    고정 반지름으로 새까만** 영역은 오버레이뿐이다. 테두리는 다른 무엇보다 위에
    불투명 검정으로 그려지므로 뒤에 무엇이 있든 어둡게 남는다는 점도 이 조건을
    강하게 만든다.

    겹친 원은 서로의 테두리를 일부 가리므로 테두리 요구 비율(rim_min)을 절반 조금
    넘는 값으로 둔다 — 겹침이 곧 판정 대상이라 여기서 놓치면 안 된다.

    반환: 원 목록(점수 내림차순, 서로 r_fill 이내로 붙은 중복은 제거).
    """
    disc = _disc_offsets(r_fill)
    ring = _ring_offsets(r_rim)
    margin = int(math.ceil(r_rim)) + 1
    w, px = gray.w, gray.px
    probe = max(1, r_fill - 2)
    rim_need = rim_min * len(ring)
    fill_need = fill_min * len(disc)

    found: List[Circle] = []
    for x, y in _center_candidates(gray, bright, probe, margin):
        base = y * w + x
        n_dark = 0
        for dx, dy in ring:
            if px[base + dy * w + dx] <= dark:
                n_dark += 1
        if n_dark < rim_need:
            continue
        n_bright = 0
        for dx, dy in disc:
            if px[base + dy * w + dx] >= bright:
                n_bright += 1
        if n_bright < fill_need:
            continue
        found.append(Circle(x, y, r_overlay, n_bright / len(disc), n_dark / len(ring)))

    # 한 원에서 중심 후보가 여럿 통과하므로 묶어서 하나로 만들고, 무리의 **무게중심**을
    # 그 원의 중심으로 삼는다. 점수 최고 후보 하나를 고르는 것보다 치우침이 작다 —
    # 후보는 참 중심 주위에 대체로 대칭으로 분포하기 때문이다. 무리의 대표 점수는
    # 가장 좋은 후보의 것을 쓴다(무리 전체의 평균은 가장자리 후보 때문에 낮아진다).
    #
    # 묶는 반경은 원판 반경(r_fill)이 아니라 **오버레이 반경(r_overlay)** 이다. 실측에서
    # 참 중심으로부터 11px 떨어진 자리에 품질이 낮은 후보 무리(rim 0.62, fill 0.73)가
    # 따로 서서 같은 원이 둘로 세어지는 일이 있었고, 그 둘의 중심 거리가 2×r_overlay보다
    # 가까워 **없는 circle_circle을 만들어 냈다.** 이 반경이 안전한 근거는 화면 축척이다 —
    # 실측 표본에서 지면 거리 92.6cm인 쌍의 오버레이 중심이 37px 떨어져 있었으므로
    # 12px는 약 30cm에 해당하는데, 물리 접촉 거리(중심 간 2r≈72cm)가 하한이라 **서로 다른**
    # 두 오버레이의 중심이 그렇게 가까워질 수 없다. 즉 이 반경 안의 이웃은 항상 같은 원이다.
    found.sort(key=lambda c: (-c.score, c.x, c.y))
    clusters: List[List[Circle]] = []
    for c in found:
        for group in clusters:
            head = group[0]
            if (c.x - head.x) ** 2 + (c.y - head.y) ** 2 <= r_overlay * r_overlay:
                group.append(c)
                break
        else:
            clusters.append([c])
    return [Circle(int(round(sum(c.x for c in g) / len(g))),
                   int(round(sum(c.y for c in g) / len(g))),
                   r_overlay, g[0].fill, g[0].rim)
            for g in clusters]


# ---------------------------------------------------------------- 객체 픽셀

def background_percentile(grays: Sequence[Gray], q: float = BG_PERCENTILE) -> Gray:
    """클립의 프레임 전수에서 픽셀별 하위 q% 값을 뽑아 **빈 방**(정지 배경)을 추정한다.

    카메라가 고정이므로 각 픽셀의 시계열은 "대체로 배경, 객체가 지나갈 때만 다른 값"이다.

    **왜 프레임끼리 직접 차분하지 않는가 (실측으로 바꾼 결정 1).** 처음에는 시간이 떨어진
    앞뒤 프레임과의 차분을 교집합으로 묶는 3프레임 차분을 썼다. 잔상(객체가 과거에 있던
    자리가 함께 켜지는 것)은 교집합으로 지워졌지만, 실제 클립의 주석 이미지를 보니
    **객체 블롭 한가운데가 텅 비었다.** 객체가 0.3초 동안 자기 몸 너비만큼 움직이지
    못하면 몸통 중앙은 세 시각 모두에서 객체로 덮여 있어 "변하지 않은 픽셀"이 되고,
    교집합에서 탈락하기 때문이다. 그 구멍만큼 상대 객체의 픽셀 영역이 과소평가되어
    ``circle_object``가 놓치는 겹침이 생긴다. 배경 추정치와 비교하면 비교 대상이 다른
    시각의 객체가 아니라 객체가 없는 방이므로 잔상도, 구멍도 없다.

    **왜 중앙값이 아니라 하위 백분위인가 (실측으로 바꾼 결정 2).** 중앙값은 픽셀 값의
    다수를 고르므로, 객체가 그 자리에 클립의 절반 넘게 머물면 **객체가 배경이 되어
    버린다.** 그러면 두 방향으로 틀린다 — 머문 객체는 블롭이 안 생겨 사라지고, 반대로
    그 객체가 비켜난 프레임에서는 진짜 배경이 "배경과 다른 픽셀"로 뒤집혀 **유령 블롭**이
    생긴다. gate_samples 39클립 실측에서 이 증상이 프레임당 성분 수로 그대로 드러났다:
    객체 4개인 씬에서 하위 25% 배경은 성분이 프레임당 3.9~5.2개(= 객체 수)로 나온 반면,
    중앙값 배경은 접촉 조건에서 2.6~3.4개까지 내려가(객체가 배경에 흡수됨) 없는
    ``circle_object``를 만들어 냈다.

    하위 백분위가 옳게 동작하는 전제는 **객체가 자기가 가린 바닥보다 밝다**는 것이다.
    이 씬은 어두운 카펫(밝기 35 근처) 위를 흰 우주인(210~220)이 지나가므로 전제가
    성립한다. 다른 씬에 쓸 때는 이 전제를 먼저 확인하고, 어두운 객체라면 상위 백분위로
    뒤집어야 한다(CLI ``--bg-percentile``).

    남는 한계: 클립 **내내** 한 자리에 멈춰 있는 객체는 어느 백분위로도 배경과 구별되지
    않는다(충돌 후 맞물려 멈춘 경우). 그런 객체는 블롭이 생기지 않아 자기 원이 귀속
    실패로 빠지고, ``unattributed``로 집계된다.
    """
    if not grays:
        raise ValueError("프레임이 없다")
    w, h = grays[0].w, grays[0].h
    if any(g.w != w or g.h != h for g in grays):
        raise ValueError("프레임 크기가 서로 다르다")
    # 보간 없이 정렬된 표본에서 한 칸을 고른다 — numpy 경로와 순수 파이썬 경로가
    # 같은 값을 내야 테스트가 두 경로를 함께 고정할 수 있다.
    idx = int(round(q / 100.0 * (len(grays) - 1)))
    if _np is not None:
        stack = _np.array([g.px for g in grays], dtype=_np.uint8)
        picked = _np.partition(stack, idx, axis=0)[idx]
        return Gray(w, h, picked.tolist())
    return Gray(w, h, [sorted(g.px[i] for g in grays)[idx] for i in range(w * h)])


def motion_mask(frame: Gray, background: Gray, thr: int = DIFF_THR) -> List[bool]:
    """배경 추정치와 밝기가 thr보다 크게 다른 픽셀 = 지금 이 프레임의 객체 픽셀."""
    if _np is not None:
        cur = _np.asarray(frame.px, dtype=_np.int16)
        bg = _np.asarray(background.px, dtype=_np.int16)
        return (_np.abs(cur - bg) > thr).tolist()
    return [abs(v - b) > thr for v, b in zip(frame.px, background.px)]


def label_components(flags: Sequence[bool], w: int, h: int,
                     min_area: int = MIN_BLOB_PX) -> Mask:
    """움직임 마스크 -> 8연결 성분 라벨. min_area 미만 성분은 배경으로 되돌린다.

    8연결을 쓰는 이유: 판정이 묻는 것은 "두 객체 사이에 배경 틈이 있는가"이고, 대각으로
    맞닿은 두 영역은 화면에서 이미 붙어 보인다.
    """
    labels = [BACKGROUND] * (w * h)
    areas: Dict[int, int] = {}
    nxt = 0
    for start in range(w * h):
        if not flags[start] or labels[start] != BACKGROUND:
            continue
        nxt += 1
        labels[start] = nxt
        queue = deque([start])
        n = 0
        members: List[int] = []
        while queue:
            i = queue.popleft()
            n += 1
            members.append(i)
            x, y = i % w, i // w
            for dy in (-1, 0, 1):
                ny = y + dy
                if ny < 0 or ny >= h:
                    continue
                for dx in (-1, 0, 1):
                    nx = x + dx
                    if nx < 0 or nx >= w or (dx == 0 and dy == 0):
                        continue
                    j = ny * w + nx
                    if flags[j] and labels[j] == BACKGROUND:
                        labels[j] = nxt
                        queue.append(j)
        if n < min_area:
            for i in members:
                labels[i] = BACKGROUND
            nxt -= 1     # 번호를 되돌려 다음 성분이 이어서 쓰게 한다
        else:
            areas[nxt] = n
    return Mask(w, h, labels, nxt, areas)


# ---------------------------------------------------------------- 연관·판정

def attribute_circles(circles: Sequence[Circle], mask: Mask,
                      r_fill: int = R_FILL) -> List[int]:
    """각 원을 자기 객체의 블롭(연결 성분)에 귀속시킨다 — 0이면 귀속 실패.

    **이 단계가 이 도구의 핵심 주의점이다.** 오버레이 원은 자기 객체의 발밑에 그려지므로
    자기 블롭과는 항상 겹친다. 그 자기 겹침을 겹침으로 세면 모든 프레임이 양성이 되어
    플래그가 아무 정보도 주지 못한다. 그래서 원마다 "이건 내 블롭"을 먼저 정해 두고,
    겹침 판정에서는 그 성분을 빼고 본다.

    원 중심이 놓인 성분을 자기 블롭으로 삼되, 중심이 배경으로 떨어졌으면(원 테두리가
    차분에서 살짝 밀리는 경우) 원판 안에서 **가장 많이 등장한** 성분으로 보완한다.
    그래도 없으면 0 — 그 순간 객체가 거의 멈춰 있어 차분 블롭이 생기지 않은 경우다.
    """
    out: List[int] = []
    disc = _disc_offsets(r_fill)
    for c in circles:
        lab = mask.at(c.x, c.y)
        if lab == BACKGROUND:
            votes: Counter = Counter()
            for dx, dy in disc:
                x, y = c.x + dx, c.y + dy
                if 0 <= x < mask.w and 0 <= y < mask.h:
                    v = mask.at(x, y)
                    if v != BACKGROUND:
                        votes[v] += 1
            lab = votes.most_common(1)[0][0] if votes else BACKGROUND
        out.append(lab)
    return out


def owned_components(owners: Sequence[int]) -> Dict[int, int]:
    """성분 라벨 -> 그 성분을 자기 것으로 삼은 **첫** 원의 인덱스.

    이 사전이 "화면에서 무엇이 객체인가"의 정의다 — 오버레이 원이 달린 성분만 객체로
    센다. 차분 마스크는 한 객체를 여러 조각으로 쪼개기도 하고 그림자·타임스탬프 글자
    같은 것도 성분으로 만드는데, 그것들을 상대 객체의 픽셀로 세면 자기 조각을 남의
    것으로 오인하게 된다(``frame_verdict`` 독스트링의 실측 경위).
    """
    owned: Dict[int, int] = {}
    for i, lab in enumerate(owners):
        if lab != BACKGROUND:
            owned.setdefault(lab, i)
    return owned


def foreign_pixels(circle: Circle, own: int, mask: Mask, owned: Dict[int, int],
                   r_overlay: float = R_OVERLAY) -> List[Tuple[int, int]]:
    """원판 안에서 **다른 원이 소유한** 성분에 속하는 픽셀 좌표 목록.

    판정(``frame_verdict``)과 디버그 주석(``annotate_frame``)이 **같은 함수**를 쓴다 —
    주석 이미지가 실제 판정과 다른 기준으로 그려지면 오판율 육안 검증이 무의미해지므로
    이 공유는 선택이 아니라 요구사항이다.
    """
    out: List[Tuple[int, int]] = []
    for dx, dy in _disc_offsets(r_overlay):
        x, y = circle.x + dx, circle.y + dy
        if 0 <= x < mask.w and 0 <= y < mask.h:
            lab = mask.at(x, y)
            if lab != own and lab in owned:
                out.append((x, y))
    return out


class FrameVerdict:
    """프레임 1장의 판정 결과."""

    __slots__ = ("overlap", "reasons", "circles", "owners", "owned", "foreign_px",
                 "unattributed")

    def __init__(self, overlap: bool, reasons: Set[str], circles: Sequence[Circle],
                 owners: Sequence[int], owned: Dict[int, int],
                 foreign_px: Dict[int, int], unattributed: int):
        self.overlap = overlap
        self.reasons = reasons
        self.circles = list(circles)
        self.owners = list(owners)
        self.owned = owned                # 성분 라벨 -> 소유 원 인덱스
        self.foreign_px = foreign_px      # 원 인덱스 -> 원판 안 남의 블롭 픽셀 수
        self.unattributed = unattributed  # 자기 블롭을 못 찾은 원 수

    def __repr__(self) -> str:      # pragma: no cover - 디버깅 편의
        return f"FrameVerdict(overlap={self.overlap}, reasons={sorted(self.reasons)})"


def frame_verdict(
    circles: Sequence[Circle],
    mask: Mask,
    owners: Optional[Sequence[int]] = None,
    r_overlay: float = R_OVERLAY,
    min_foreign_px: int = MIN_FOREIGN_PX,
    use_merged_blob: bool = True,
) -> FrameVerdict:
    """한 프레임이 "겹쳐 보이는가"를 판정한다. 사유는 세 가지로 나눠 기록한다.

      circle_circle : 두 오버레이 원판이 교차한다(중심 거리 < 2 × r_overlay).
      circle_object : 어떤 원판 안에 **다른 원이 자기 것으로 삼은** 블롭의 픽셀이
                      min_foreign_px 이상 들어 있다 — A의 오버레이가 B의 몸통을 덮은
                      경우가 여기 잡힌다.
      merged_blob   : 두 원의 자기 블롭이 같은 성분이다. 두 객체의 움직이는 픽셀이 하나로
                      이어져 화면에 배경 틈이 없다는 뜻이고, 동시에 그 성분 안에서는 어느
                      픽셀이 누구 것인지 가를 수 없어 circle_object가 판정 불능이 되는
                      상황이기도 하다. use_merged_blob=False면 사유에서 뺀다.

    **남의 블롭을 "다른 원이 소유한 성분"으로 좁혀 정의하는 이유(실측으로 확정).**
    처음에는 "자기 성분이 아닌 모든 블롭 픽셀"을 남의 것으로 셌는데, 실제 클립에
    돌려 보니 사실상 모든 프레임이 양성으로 나왔다. 원인은 차분 마스크가 한 객체를
    **여러 조각으로 쪼갠다**는 것이다 — 그림자 경계, 팔다리처럼 대비가 낮은 부분,
    화면에 남은 잔상 꼬리가 본체와 끊긴 작은 성분이 되어(4객체 씬에서 성분이 5~8개로
    나왔다) 자기 자신의 조각을 남의 객체로 오인했다. 이 실험에서 "객체"는 오버레이가
    달린 것으로 정의되므로, 남의 블롭도 **다른 원이 소유한 성분**으로 좁히면 이 오인이
    구조적으로 사라진다. 대가는 원이 붙지 않은 조각(그림자·타임스탬프 글자·잔상)이
    판정에서 빠지는 것인데, 그것들은 애초에 "상대 객체의 픽셀"이 아니므로 옳은 배제다.

    자기 블롭을 못 찾은 원(owner=0)은 circle_object 검사에서 빠진다 — 자기 픽셀과 남의
    픽셀을 가를 근거가 없어 자기 몸통을 남의 것으로 오인할 수 있기 때문이다. 그 수는
    ``unattributed``로 남겨 두어 이 사각지대의 크기를 나중에 셀 수 있게 한다.
    """
    if owners is None:
        owners = attribute_circles(circles, mask)
    reasons: Set[str] = set()
    foreign: Dict[int, int] = {}

    for i, a in enumerate(circles):
        for b in circles[i + 1:]:
            if math.hypot(a.x - b.x, a.y - b.y) < 2.0 * r_overlay:
                reasons.add("circle_circle")

    # 어느 성분이 "객체"인가 = 어떤 원이 자기 것으로 삼은 성분인가.
    owned = owned_components(owners)
    if use_merged_blob:
        for i, lab in enumerate(owners):
            if lab != BACKGROUND and owned[lab] != i:
                reasons.add("merged_blob")

    for i, c in enumerate(circles):
        own = owners[i]
        if own == BACKGROUND:
            continue
        n = len(foreign_pixels(c, own, mask, owned, r_overlay))
        if n:
            foreign[i] = n
        if n >= min_foreign_px:
            reasons.add("circle_object")

    unattributed = sum(1 for lab in owners if lab == BACKGROUND)
    return FrameVerdict(bool(reasons), reasons, circles, owners, owned, foreign,
                        unattributed)


class ClipVerdict:
    """클립 1개의 집계 결과 — manifest에 실리는 값."""

    __slots__ = ("overlap", "n_frames", "n_overlap_frames", "reasons", "frames")

    def __init__(self, frames: Sequence[FrameVerdict]):
        self.frames = list(frames)
        self.n_frames = len(frames)
        self.n_overlap_frames = sum(1 for f in frames if f.overlap)
        self.overlap = self.n_overlap_frames > 0
        counter: Counter = Counter()
        for f in frames:
            for r in f.reasons:
                counter[r] += 1
        self.reasons = counter

    def __repr__(self) -> str:      # pragma: no cover - 디버깅 편의
        return (f"ClipVerdict(overlap={self.overlap}, "
                f"{self.n_overlap_frames}/{self.n_frames} frames)")


def clip_verdict(frames: Sequence[FrameVerdict]) -> ClipVerdict:
    """프레임 판정 목록 -> 클립 판정. 20장 중 1장이라도 겹치면 클립이 겹침이다(설계 §5-2)."""
    return ClipVerdict(frames)


def analyze_frames(
    grays: Sequence[Gray],
    diff_thr: int = DIFF_THR,
    min_blob_px: int = MIN_BLOB_PX,
    use_merged_blob: bool = True,
    bg_percentile: float = BG_PERCENTILE,
    background: Optional[Gray] = None,
    **detect_kwargs,
) -> Tuple[List[FrameVerdict], List[Mask]]:
    """클립에서 뽑은 프레임 전수를 판정한다. (프레임 판정 목록, 마스크 목록)을 준다.

    배경은 클립 자체의 프레임에서 한 번만 추정해 전 프레임이 공유한다.
    마스크를 함께 돌려주는 이유는 디버그 주석 이미지가 같은 마스크를 다시 계산하지 않고
    그대로 그릴 수 있게 하기 위해서다.
    """
    if background is None:
        background = background_percentile(grays, bg_percentile)
    verdicts: List[FrameVerdict] = []
    masks: List[Mask] = []
    for g in grays:
        flags = motion_mask(g, background, diff_thr)
        mask = label_components(flags, g.w, g.h, min_blob_px)
        circles = detect_circles(g, **detect_kwargs)
        owners = attribute_circles(circles, mask)
        verdicts.append(frame_verdict(circles, mask, owners,
                                      use_merged_blob=use_merged_blob))
        masks.append(mask)
    return verdicts, masks


# ---------------------------------------------------------------- 프레임 추출

def ffmpeg_frames_cmd(video: Path, out_pattern: str, fps: float = DEFAULT_FPS,
                      frames: int = DEFAULT_FRAMES) -> List[str]:
    """클립 -> 추론 프레임 PNG 전개 명령.

    모델이 보는 것과 같은 샘플링이어야 하므로 2초 클립에서 20장 = 10fps로 고정한다
    (train==infer로 못박힌 NFRAMES=20). 인코딩 손실을 더 얹지 않도록 PNG로 뽑는다.
    """
    return ["ffmpeg", "-y", "-v", "error", "-i", str(video),
            "-vf", f"fps={fps:g}", "-frames:v", str(frames), out_pattern]


def load_clip_frames(video: Path, fps: float = DEFAULT_FPS, frames: int = DEFAULT_FRAMES,
                     keep_rgb: bool = False):
    """클립에서 추론 프레임을 뽑아 (Gray 목록, RGB 이미지 목록 또는 None)로 돌려준다."""
    from PIL import Image

    with tempfile.TemporaryDirectory(prefix="overlay_flags_") as tmp:
        pattern = str(Path(tmp) / "f_%03d.png")
        res = subprocess.run(ffmpeg_frames_cmd(video, pattern, fps, frames),
                             capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"ffmpeg 실패 {video}: {res.stderr[-300:]}")
        paths = sorted(Path(tmp).glob("f_*.png"))
        grays, rgbs = [], []
        for p in paths:
            with Image.open(p) as im:
                im.load()
                grays.append(Gray.from_image(im))
                if keep_rgb:
                    rgbs.append(im.convert("RGB").copy())
    return grays, (rgbs if keep_rgb else None)


# ---------------------------------------------------------------- 디버그 주석

def annotate_frame(rgb, verdict: FrameVerdict, mask: Mask, r_overlay: float = R_OVERLAY):
    """판정 근거를 그린 주석 이미지 — 오판율 육안 검증용(설계 §5-2 "도구 신뢰도 검증").

    파랑 = 차분으로 잡은 객체 픽셀, 초록 원 = 검출된 오버레이(자기 블롭 있음),
    노랑 원 = 자기 블롭을 못 찾은 오버레이, 빨강 = 어떤 원판 안에 들어온 **남의**
    블롭 픽셀. 빨강이 보이면 circle_object가 왜 켜졌는지 그 자리에서 확인된다.
    """
    from PIL import ImageDraw

    out = rgb.copy()
    px = out.load()
    for y in range(mask.h):
        row = y * mask.w
        for x in range(mask.w):
            if mask.px[row + x] != BACKGROUND:
                r, g, b = px[x, y]
                px[x, y] = (r // 2, g // 2, min(255, b // 2 + 110))

    for i, c in enumerate(verdict.circles):
        own = verdict.owners[i]
        if own == BACKGROUND:
            continue
        for x, y in foreign_pixels(c, own, mask, verdict.owned, r_overlay):
            px[x, y] = (255, 40, 40)

    draw = ImageDraw.Draw(out)
    for i, c in enumerate(verdict.circles):
        color = (60, 255, 60) if verdict.owners[i] != BACKGROUND else (255, 220, 0)
        draw.ellipse((c.x - r_overlay, c.y - r_overlay, c.x + r_overlay, c.y + r_overlay),
                     outline=color, width=1)
    label = ("OVERLAP: " + ",".join(sorted(verdict.reasons))) if verdict.overlap else "clear"
    draw.text((4, 4), label, fill=(255, 255, 0))
    return out


# ---------------------------------------------------------------- manifest 갱신

def apply_overlay_flags(manifest: dict, results: Dict[str, ClipVerdict]) -> Dict[str, int]:
    """판정 결과를 manifest에 덧쓴다 — **재절단 없이** 돌아가는 후처리다.

    ``clips_manifest.json``의 각 클립 항목에 다음을 넣는다.
      overlay_overlap        : 20장 중 1장이라도 겹치면 True (분석 층화 축, 설계 §6-1)
      overlay_overlap_frames : 겹친 프레임 수
      overlay_frames_checked : 실제로 검사한 프레임 수
      overlay_reasons        : 사유별 프레임 수

    manifest 상단에는 ``overlay_flag_method="pixel"``을 박는다. 같은 이름의 필드를
    이전에 기하 문턱(철회된 중심 거리 102cm)으로 채운 적이 있어, 어느 방법으로 매긴
    값인지 파일만 보고 알 수 있어야 하기 때문이다.

    반환: {"eligible", "flagged", "missing"} 집계.
    """
    eligible = flagged = missing = 0
    for clip in manifest.get("clips", []):
        if clip.get("condition") not in TARGET_CONDITIONS:
            continue
        eligible += 1
        v = results.get(clip.get("clip"))
        if v is None:
            missing += 1
            continue
        clip["overlay_overlap"] = v.overlap
        clip["overlay_overlap_frames"] = v.n_overlap_frames
        clip["overlay_frames_checked"] = v.n_frames
        clip["overlay_reasons"] = dict(v.reasons)
        flagged += int(v.overlap)
    manifest["overlay_flag_method"] = "pixel"
    return {"eligible": eligible, "flagged": flagged, "missing": missing}


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", required=True, help="clips_manifest.json 경로")
    ap.add_argument("--clip-root", help="클립 상대 경로의 기준 디렉터리(기본: manifest가 있는 곳)")
    ap.add_argument("--conditions", default=",".join(TARGET_CONDITIONS),
                    help="판정 대상 조건(쉼표 구분)")
    ap.add_argument("--frames", type=int, default=DEFAULT_FRAMES, help="클립당 검사 프레임 수")
    ap.add_argument("--fps", type=float, default=DEFAULT_FPS, help="프레임 추출 fps")
    ap.add_argument("--overlay-radius", type=float, default=R_OVERLAY,
                    help="오버레이 footprint 반지름(px). 해상도가 다르면 함께 비례 조정된다")
    ap.add_argument("--diff-thr", type=int, default=DIFF_THR,
                    help="배경 대비 객체 픽셀 판정 문턱(밝기 차)")
    ap.add_argument("--bg-percentile", type=float, default=BG_PERCENTILE,
                    help="배경 추정 백분위. 기본 25는 '객체가 바닥보다 밝다'는 이 씬의 "
                         "성질에 기댄 값이다 — 어두운 객체 씬에서는 75 같은 상위 값으로 뒤집어라")
    ap.add_argument("--min-blob-px", type=int, default=MIN_BLOB_PX, help="블롭 최소 면적(px)")
    ap.add_argument("--no-merged-blob", action="store_true",
                    help="merged_blob 사유를 판정에서 뺀다(육안 대조에서 과검출로 판명될 때)")
    ap.add_argument("--debug-samples", type=int, default=0,
                    help="판정 근거 주석 이미지를 남길 클립 수(오판율 육안 검증용)")
    ap.add_argument("--debug-dir", help="주석 이미지 출력 디렉터리(기본: <manifest 옆>/overlay_debug)")
    ap.add_argument("--limit", type=int, default=0, help="처리할 클립 수 상한(0=전부, 시험용)")
    ap.add_argument("--dry-run", action="store_true", help="manifest를 쓰지 않고 집계만 출력")
    args = ap.parse_args(argv)

    manifest_path = Path(args.manifest)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = Path(args.clip_root) if args.clip_root else manifest_path.parent
    debug_dir = Path(args.debug_dir) if args.debug_dir else manifest_path.parent / "overlay_debug"
    wanted = {c.strip() for c in args.conditions.split(",") if c.strip()}

    # 원 상수 세 개는 실측에서 함께 나온 값이라 해상도가 바뀌면 같은 비율로 움직인다.
    scale = args.overlay_radius / R_OVERLAY
    detect_kwargs = {"r_fill": max(2, int(round(R_FILL * scale))),
                     "r_rim": R_RIM * scale,
                     "r_overlay": args.overlay_radius}

    targets = [c for c in data.get("clips", [])
               if c.get("condition") in wanted and not c.get("error")]
    if args.limit:
        targets = targets[:args.limit]
    print(f"[overlay_flags] 대상 {len(targets)}클립 (조건: {', '.join(sorted(wanted))}), "
          f"클립당 {args.frames}프레임 @{args.fps:g}fps")

    results: Dict[str, ClipVerdict] = {}
    reason_total: Counter = Counter()
    per_condition: Dict[str, List[int]] = {}
    unattributed_frames = 0
    failed: List[str] = []
    debug_left = args.debug_samples

    for i, clip in enumerate(targets, 1):
        rel = clip.get("clip")
        video = root / rel
        if not video.exists():
            failed.append(f"{rel} (파일 없음)")
            continue
        want_debug = debug_left > 0
        try:
            grays, rgbs = load_clip_frames(video, args.fps, args.frames, keep_rgb=want_debug)
        except RuntimeError as exc:
            failed.append(f"{rel} ({exc})")
            continue
        if len(grays) < 2:
            failed.append(f"{rel} (프레임 {len(grays)}장)")
            continue
        verdicts, masks = analyze_frames(
            grays, diff_thr=args.diff_thr, min_blob_px=args.min_blob_px,
            use_merged_blob=not args.no_merged_blob, bg_percentile=args.bg_percentile,
            **detect_kwargs)
        v = clip_verdict(verdicts)
        results[rel] = v
        reason_total.update(v.reasons)
        per_condition.setdefault(clip.get("condition", "?"), []).append(int(v.overlap))
        unattributed_frames += sum(1 for f in verdicts if f.unattributed)

        if want_debug and rgbs:
            debug_left -= 1
            out_dir = debug_dir / Path(rel).stem
            out_dir.mkdir(parents=True, exist_ok=True)
            for k, (rgb, fv, mk) in enumerate(zip(rgbs, verdicts, masks)):
                annotate_frame(rgb, fv, mk, args.overlay_radius).save(
                    out_dir / f"{k:02d}_{'overlap' if fv.overlap else 'clear'}.png")
            print(f"[overlay_flags] debug -> {out_dir}")
        if i % 25 == 0 or i == len(targets):
            print(f"[overlay_flags] {i}/{len(targets)} 처리")

    print("[overlay_flags] 조건별 겹침 클립 수:")
    for cond in sorted(per_condition):
        flags = per_condition[cond]
        print(f"  {cond:<14} {sum(flags)}/{len(flags)}")
    print("[overlay_flags] 사유별 겹침 프레임 수: " +
          (", ".join(f"{k}={v}" for k, v in sorted(reason_total.items())) or "-"))
    print(f"[overlay_flags] 자기 블롭 미귀속 원이 있던 프레임: {unattributed_frames} "
          f"(그 원은 circle_object 검사에서 빠진다 — 모듈 독스트링의 알려진 한계)")
    if failed:
        print(f"[overlay_flags] 실패 {len(failed)}건:")
        for f in failed[:10]:
            print(f"  {f}")

    summary = apply_overlay_flags(data, results)
    print(f"[overlay_flags] manifest 대상 {summary['eligible']}클립 중 "
          f"겹침 {summary['flagged']}, 판정 없음 {summary['missing']}")
    if args.dry_run:
        print("[overlay_flags] --dry-run — manifest를 쓰지 않았다")
        return
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[overlay_flags] manifest 갱신 -> {manifest_path}")


if __name__ == "__main__":
    main()
