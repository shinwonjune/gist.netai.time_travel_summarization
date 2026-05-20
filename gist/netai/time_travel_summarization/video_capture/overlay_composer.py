from dataclasses import dataclass
from typing import Optional, Sequence, Tuple


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

    def __init__(self, width: int, height: int):
        self._width = width
        self._height = height
        self._font_cache: dict = {}

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
        draw.ellipse(bbox, fill=marker.bg, outline=marker.border, width=marker.border_width)
        # 중앙 정렬 텍스트
        font = self._get_font(marker.font_size, image_font)
        tbbox = draw.textbbox((0, 0), marker.text, font=font)
        tw = tbbox[2] - tbbox[0]
        th = tbbox[3] - tbbox[1]
        draw.text((cx - tw // 2, cy - th // 2 - tbbox[1]), marker.text, font=font, fill=marker.fg)
