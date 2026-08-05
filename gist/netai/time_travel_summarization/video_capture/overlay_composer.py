from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

# 오버레이 마커의 v2 시각 규약 상수(2026-08-05) — **캡처(PIL 합성) 경로 전용**.
# 헤드리스 캡처(generate/replay)가 이 provider 경로를 지나므로 여기 값이 곧 학습·추론
# 영상의 규약이다(v2 데이터 재생성 + 재학습과 묶어서만 의미 — 위상분해_리포트 한계 11).
#
# 튜닝 이력 주의(2026-08-05): 초기 헤드리스 검수 3회("엉덩이 높이", "원 크기 그대로")는
# **미커밋 변경이 없는 구코드 렌더**(L40 체크아웃 또는 _build 복사본 exts)였던 것으로
# 판정 — 근거: ① 앵커는 몸 축 위 실제 3D 점이라 몸키(206)보다 큰 250이 엉덩이에 보일
# 수 없고 ② 반지름 12→8(33% 축소)이 "그대로"로 보일 수 없다. 그 관찰로 만든 값
# (250/8px, GUI-캡처 분리)은 폐기하고 GUI 육안으로 검증된 어깨 높이로 재설정했다.
# 렌더가 어떤 규약으로 돌았는지는 캡처 로그의 "marker regime" 라인으로 확인할 것.
#
# MARKER_UP_OFFSET — 3D 앵커 상향 오프셋(스테이지 단위=cm). 발밑(객체 원점) 앵커는
# BEV 투시와 상호작용해 몸통이 마커 밖으로 삐져나오는 문제(리포트 한계 11)의 해소책.
# 어깨 높이 145는 GUI 뷰포트 육안으로 확정(150=어깨 살짝 위 → 145). 앵커는 3D 점이라
# 라이브 코드라면 캡처 렌더에서도 같은 신체 높이(어깨)에 투영된다.
MARKER_UP_OFFSET = 145.0
# MARKER_RADIUS_PX — 캡처 마커 원 반지름(픽셀). v1은 12, "조금 축소" 요청 반영해 10.
# 숫자(font_size=12)는 그대로 두고 원만 줄인다. v1 클립 분석(overlay_flags)의 12
# 가정과 구분할 것. (구코드 렌더 기반이던 9·8은 폐기 — 라이브 렌더 확인 후 재조정)
MARKER_RADIUS_PX = 10


@dataclass(frozen=True)
class TextItem:
    x: int
    y: int
    text: str
    align: str = "center"
    vertical_align: str = "center"
    font_size: int = 14
    fg: Tuple[int, int, int, int] = (255, 255, 255, 255)
    bg: Tuple[int, int, int, int] = (0, 0, 0, 180)
    padding: int = 2


@dataclass(frozen=True)
class CircleLabel:
    """원형 배경 + 중앙 정렬 짧은 텍스트(주로 ID 숫자)."""

    x: int                          # 픽셀 좌표 (중심)
    y: int                          # 픽셀 좌표 (중심)
    text: str                       # 짧은 텍스트 ("1", "42" 등)
    radius: int = 12                # 원 반지름
    font_size: int = 12
    fg: Tuple[int, int, int, int] = (0, 0, 0, 255)              # 텍스트 색
    bg: Tuple[int, int, int, int] = (255, 255, 255, 220)        # 원 채움
    border: Tuple[int, int, int, int] = (0, 0, 0, 255)          # 원 테두리
    border_width: int = 2


@dataclass(frozen=True)
class OverlayFrame:
    """Provider가 매 프레임 리턴하는 객체."""

    timestamp_text: Optional[str] = None
    object_labels: Sequence[TextItem] = ()
    misc_text: Sequence[TextItem] = ()
    circle_markers: Sequence[CircleLabel] = ()


class OverlayComposer:
    """RGBA bytes에 PIL로 텍스트를 합성."""

    def __init__(self, width: int, height: int, debug: bool = False):
        self._width = width
        self._height = height
        self._font_cache: dict = {}
        self._debug = debug

    def _get_font(self, size, image_font):
        cached = self._font_cache.get(size)
        if cached is not None:
            return cached

        for font_name in ("arial.ttf", "DejaVuSans.ttf"):
            try:
                font = image_font.truetype(font_name, size)
                break
            except OSError:
                font = None
        else:
            font = None

        if font is None:
            font = image_font.load_default()

        self._font_cache[size] = font
        return font

    def _draw_item(self, draw, item: TextItem, image_font) -> None:
        font = self._get_font(item.font_size, image_font)
        bbox = draw.textbbox((0, 0), item.text, font=font)
        bbox_w = bbox[2] - bbox[0]
        bbox_h = bbox[3] - bbox[1]

        x = item.x
        if item.align == "center":
            x -= bbox_w // 2
        elif item.align == "right":
            x -= bbox_w

        y = item.y
        if item.vertical_align == "center":
            y -= bbox_h // 2
        elif item.vertical_align == "bottom":
            y -= bbox_h

        draw.rectangle(
            (
                x - item.padding,
                y - item.padding,
                x + bbox_w + item.padding,
                y + bbox_h + item.padding,
            ),
            fill=item.bg,
        )
        draw.text((x, y), item.text, font=font, fill=item.fg)

    def compose(self, rgba_bytes: bytes, frame: OverlayFrame) -> bytes:
        """원본 rgba_bytes를 받아 overlay가 그려진 bytes를 리턴.
        PIL이 import 실패하면 원본 그대로 리턴."""
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            return rgba_bytes

        image = Image.frombytes("RGBA", (self._width, self._height), rgba_bytes)
        draw = ImageDraw.Draw(image, "RGBA")

        if frame.timestamp_text:
            font = self._get_font(14, ImageFont)
            bbox = draw.textbbox((0, 0), frame.timestamp_text, font=font)
            bbox_w = bbox[2] - bbox[0]
            bbox_h = bbox[3] - bbox[1]
            x = self._width - bbox_w - 8
            y = self._height - bbox_h - 8
            self._draw_item(
                draw,
                TextItem(
                    x=x,
                    y=y,
                    text=frame.timestamp_text,
                    align="left",
                    vertical_align="top",
                    font_size=14,
                ),
                ImageFont,
            )

        for item in frame.object_labels:
            self._draw_item(draw, item, ImageFont)

        for item in frame.misc_text:
            self._draw_item(draw, item, ImageFont)

        for marker in frame.circle_markers:
            self._draw_circle(draw, marker, ImageFont)

        return image.tobytes("raw", "RGBA")

    def _draw_circle(self, draw, marker: "CircleLabel", image_font) -> None:
        cx, cy, r = marker.x, marker.y, marker.radius
        # 원: bounding box (left, top, right, bottom)
        bbox = (cx - r, cy - r, cx + r, cy + r)
        # debug=True면 반투명 fill로 객체가 마커 뒤로 보이게 한다
        fill = (marker.bg[0], marker.bg[1], marker.bg[2], 90) if self._debug else marker.bg
        draw.ellipse(bbox, fill=fill, outline=marker.border, width=marker.border_width)
        # 중앙 정렬 텍스트
        font = self._get_font(marker.font_size, image_font)
        tbbox = draw.textbbox((0, 0), marker.text, font=font)
        tw = tbbox[2] - tbbox[0]
        th = tbbox[3] - tbbox[1]
        draw.text((cx - tw // 2, cy - th // 2 - tbbox[1]), marker.text, font=font, fill=marker.fg)
        if self._debug:
            # 정확한 투영 픽셀(cx, cy)을 외곽 4-방향 tick으로 가시화
            tick_outer = r + 6
            tick_inner = r + 1
            red = (255, 0, 0, 255)
            draw.line([(cx - tick_outer, cy), (cx - tick_inner, cy)], fill=red, width=1)
            draw.line([(cx + tick_inner, cy), (cx + tick_outer, cy)], fill=red, width=1)
            draw.line([(cx, cy - tick_outer), (cx, cy - tick_inner)], fill=red, width=1)
            draw.line([(cx, cy + tick_inner), (cx, cy + tick_outer)], fill=red, width=1)
