"""overlay_overlap 픽셀 판정기(automation/overlay_flags.py) 순수 로직 테스트.

합성 그레이스케일 배열만 쓴다 — ffmpeg도, 이미지 파일도, 실제 클립도 필요 없다.
합성 프레임은 실측한 렌더 산출물의 성질을 그대로 흉내 낸다(실측 근거는 대상 모듈의
"원 크기 실측" 절): 어두운 방(밝기 35) 위에 밝은 객체 몸통(215)이 있고, 그 위에
반지름 12의 오버레이 — 반지름 10까지 흰 원판(252) + 반지름 11~12의 검은 테두리(0) +
원판 안 어두운 ID 획 — 이 얹힌다.

고정하는 약속:
  - 원 검출: 오버레이는 찾고, 같은 밝기의 큰 가구는 찾지 않는다(CircleDetectionTest)
  - 배경 추정: 하위 백분위가 오래 머문 객체를 배경으로 흡수하지 않는다(BackgroundTest)
  - 연결 성분: 최소 면적 미만 잡음은 버린다(ComponentTest)
  - **자기 겹침 제외**: 오버레이는 항상 자기 객체 위에 있으므로 혼자 있는 객체는
    절대 겹침이 아니다 — 이 도구의 핵심 주의점(SelfOverlapTest)
  - 판정 3기전: circle_circle / circle_object / merged_blob (FrameVerdictTest)
  - 클립 집계: 20장 중 1장이라도 겹치면 클립이 겹침(ClipVerdictTest)
  - manifest 갱신: 대상 조건에만, 재절단 없이(ManifestTest)
"""
import unittest

from gist.netai.time_travel_summarization.automation.overlay_flags import (
    BACKGROUND,
    R_OVERLAY,
    TARGET_CONDITIONS,
    Circle,
    ClipVerdict,
    FrameVerdict,
    Gray,
    Mask,
    analyze_frames,
    apply_overlay_flags,
    attribute_circles,
    background_percentile,
    clip_verdict,
    detect_circles,
    ffmpeg_frames_cmd,
    foreign_pixels,
    frame_verdict,
    label_components,
    motion_mask,
    owned_components,
)

W, H = 200, 160
FLOOR = 35        # 어두운 카펫 실측치
BODY = 215        # 객체 몸통·흰 가구 실측치 — 원판 밝기와 겹쳐서 밝기만으로는 못 가린다
DISC = 252        # 오버레이 원판 실측치
RIM = 0           # 오버레이 검은 테두리 실측치


def _blank(value: int = FLOOR) -> Gray:
    return Gray.blank(W, H, value)


def _fill_disc(g: Gray, cx: int, cy: int, r: float, value: int) -> None:
    ri = int(r)
    for dy in range(-ri, ri + 1):
        for dx in range(-ri, ri + 1):
            if dx * dx + dy * dy <= r * r:
                x, y = cx + dx, cy + dy
                if 0 <= x < g.w and 0 <= y < g.h:
                    g.px[y * g.w + x] = value


def _fill_rect(g: Gray, x0: int, y0: int, x1: int, y1: int, value: int) -> None:
    for y in range(max(0, y0), min(g.h, y1)):
        for x in range(max(0, x0), min(g.w, x1)):
            g.px[y * g.w + x] = value


def _draw_body(g: Gray, cx: int, cy: int, half: int = 13) -> None:
    """객체 몸통 — 오버레이보다 조금 넓은 밝은 사각형."""
    _fill_rect(g, cx - half, cy - half, cx + half + 1, cy + half + 1, BODY)


def _draw_overlay(g: Gray, cx: int, cy: int, glyph: bool = True) -> None:
    """오버레이 원 — 검은 테두리(11~12) + 흰 원판(≤10) + ID 획.

    테두리를 원판보다 먼저가 아니라 **바깥에** 그린다: 반지름 12까지 검게 칠한 뒤
    반지름 10까지를 희게 덮으면 실측과 같은 "흰 원판 + 두께 2의 검은 링"이 된다.
    """
    _fill_disc(g, cx, cy, 12.4, RIM)
    _fill_disc(g, cx, cy, 10.0, DISC)
    if glyph:   # 원판 안의 어두운 숫자 획 — 원판이 100% 밝다고 가정하면 안 된다
        _fill_rect(g, cx - 1, cy - 6, cx + 2, cy + 6, 20)


def _object_frame(centers, body_half: int = 13) -> Gray:
    g = _blank()
    for cx, cy in centers:
        _draw_body(g, cx, cy, body_half)
    for cx, cy in centers:
        _draw_overlay(g, cx, cy)
    return g


def _mask_from(g: Gray, background: Gray, min_area: int = 20) -> Mask:
    return label_components(motion_mask(g, background), g.w, g.h, min_area)


class CircleDetectionTest(unittest.TestCase):
    """밝기 + 검은 테두리 고정 크기 매칭이 오버레이만 골라내는가."""

    def test_finds_single_overlay(self):
        g = _object_frame([(60, 60)])
        found = detect_circles(g)
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0].x, 60, delta=1)
        self.assertAlmostEqual(found[0].y, 60, delta=1)

    def test_finds_every_overlay(self):
        g = _object_frame([(50, 50), (120, 60), (80, 120)])
        self.assertEqual(len(detect_circles(g)), 3)

    def test_bright_furniture_is_not_a_circle(self):
        """같은 밝기의 큰 밝은 영역은 걸러진다 — 판별력은 검은 테두리에서 나온다.

        원판 밝기(252)와 흰 가구 밝기(215)가 실측에서 겹치므로, 밝기 문턱만으로는
        가구 안쪽 어디서나 원판 검사가 통과해 버린다. 자기 둘레가 고정 반지름으로
        새까만 영역은 오버레이뿐이라는 것이 이 도구가 쓰는 성질이다.
        """
        g = _blank()
        _fill_rect(g, 40, 40, 140, 140, DISC)
        self.assertEqual(detect_circles(g), [])

    def test_glyph_does_not_break_detection(self):
        """원판 안 어두운 ID 획이 있어도 검출된다(FILL_MIN 여유의 목적)."""
        with_glyph = _blank()
        _draw_overlay(with_glyph, 70, 70, glyph=True)
        self.assertEqual(len(detect_circles(with_glyph)), 1)

    def test_one_center_per_circle(self):
        """원 하나에서 후보가 여럿 나와도 중복 억제로 하나만 남는다."""
        g = _object_frame([(100, 80)])
        self.assertEqual(len(detect_circles(g)), 1)

    def test_matches_closer_than_overlay_radius_collapse(self):
        """오버레이 반경보다 가까운 두 매치는 같은 원 하나로 묶인다 — 회귀 방지.

        실측에서 참 중심으로부터 11px 떨어진 자리에 품질 낮은 후보 무리가 따로 서서
        한 원이 둘로 세어졌고, 그 둘의 중심 거리가 2R보다 가까워 **없는 circle_circle을
        만들어 냈다.** 이 반경으로 묶는 것이 안전한 근거는 화면 축척이다 — 실측 표본에서
        지면 거리 92.6cm인 쌍의 오버레이 중심이 37px 떨어져 있었으므로 12px는 약 30cm에
        해당하는데, 물리 접촉 거리 72cm가 하한이라 **서로 다른** 두 오버레이가 그렇게
        가까워질 수 없다. 아래 test_partially_covered_rim_still_detected가 반대편
        경계(18px 떨어진 진짜 두 원은 둘로 남는다)를 함께 고정한다.
        """
        g = _blank()
        _draw_overlay(g, 80, 80)
        _draw_overlay(g, 88, 80)      # 중심 거리 8 < r_overlay=12
        self.assertEqual(len(detect_circles(g)), 1)

    def test_partially_covered_rim_still_detected(self):
        """겹친 원은 서로 테두리를 가린다 — 그때도 놓치면 안 된다(RIM_MIN이 낮은 이유).

        겹침이 곧 판정 대상이므로 여기서 검출을 놓치면 판정 자체가 성립하지 않는다.
        """
        g = _blank()
        _draw_overlay(g, 80, 80)
        _draw_overlay(g, 98, 80)      # 중심 거리 18 < 2R=24 — 서로 테두리를 덮는다
        self.assertEqual(len(detect_circles(g)), 2)


class BackgroundTest(unittest.TestCase):
    """하위 백분위 배경 추정 — 오래 머문 객체를 배경으로 흡수하지 않는가."""

    def test_object_present_in_most_frames_is_not_absorbed(self):
        """20장 중 14장에 같은 자리를 차지한 객체도 배경이 되지 않는다.

        중앙값이었다면 그 픽셀의 다수값이 객체라 배경으로 흡수되고, 객체가 비켜난
        6장에서는 진짜 바닥이 거꾸로 "객체"로 뒤집혀 유령 블롭이 생긴다.
        """
        frames = []
        for i in range(20):
            g = _blank()
            if i < 14:
                _fill_rect(g, 50, 50, 80, 80, BODY)
            frames.append(g)
        bg = background_percentile(frames, 25.0)
        self.assertEqual(bg.at(60, 60), FLOOR)
        # 객체가 있던 프레임에서는 객체가, 없던 프레임에서는 아무것도 잡히지 않는다
        self.assertTrue(motion_mask(frames[0], bg)[60 * W + 60])
        self.assertFalse(motion_mask(frames[19], bg)[60 * W + 60])

    def test_median_would_have_absorbed_it(self):
        """대조 — 같은 입력에서 중앙값(50%)은 실제로 뒤집힌다. 백분위를 25로 둔 이유."""
        frames = []
        for i in range(20):
            g = _blank()
            if i < 14:
                _fill_rect(g, 50, 50, 80, 80, BODY)
            frames.append(g)
        bg = background_percentile(frames, 50.0)
        self.assertEqual(bg.at(60, 60), BODY)          # 객체가 배경이 됐다
        self.assertFalse(motion_mask(frames[0], bg)[60 * W + 60])   # 객체가 사라지고
        self.assertTrue(motion_mask(frames[19], bg)[60 * W + 60])   # 빈 바닥이 유령이 됐다

    def test_moving_object_yields_solid_blob(self):
        """움직이는 객체는 몸통 **속까지** 채워진 블롭이 된다(3프레임 차분의 구멍 없음).

        3프레임 차분이었다면 몸 너비만큼 움직이지 못한 프레임에서 몸통 중앙이 비었다.
        (정중앙 픽셀만은 예외다 — 거기 그려진 ID 획의 밝기가 어두운 바닥과 비슷해
        밝기 차 문턱을 못 넘는다. 그 픽셀은 어차피 원판 안이라 판정에 영향이 없다.)
        """
        frames = [_object_frame([(40 + 6 * i, 70)]) for i in range(20)]
        bg = background_percentile(frames)
        mask = _mask_from(frames[10], bg)
        self.assertEqual(mask.n_labels, 1)
        cx = 40 + 6 * 10
        self.assertNotEqual(mask.at(cx + 5, 70), BACKGROUND)     # 원판 안쪽
        self.assertNotEqual(mask.at(cx - 12, 70), BACKGROUND)    # 몸통 가장자리
        self.assertNotEqual(mask.at(cx, 70 - 10), BACKGROUND)    # 몸통 위쪽


class ComponentTest(unittest.TestCase):
    def test_min_area_drops_noise(self):
        flags = [False] * (W * H)
        for y in range(20, 30):          # 10x10 = 100px
            for x in range(20, 30):
                flags[y * W + x] = True
        flags[100 * W + 100] = True      # 1px 잡음
        mask = label_components(flags, W, H, min_area=50)
        self.assertEqual(mask.n_labels, 1)
        self.assertEqual(mask.at(100, 100), BACKGROUND)
        self.assertNotEqual(mask.at(25, 25), BACKGROUND)

    def test_labels_stay_consecutive_after_dropping(self):
        """작은 성분을 버려도 라벨 번호가 비지 않는다(areas 키와 n_labels가 어긋나면 안 됨)."""
        flags = [False] * (W * H)
        flags[10 * W + 10] = True                     # 버려질 1px
        for y in range(40, 50):
            for x in range(40, 50):
                flags[y * W + x] = True
        for y in range(80, 90):
            for x in range(80, 90):
                flags[y * W + x] = True
        mask = label_components(flags, W, H, min_area=50)
        self.assertEqual(mask.n_labels, 2)
        self.assertEqual(sorted(mask.areas), [1, 2])

    def test_diagonal_touch_merges(self):
        """8연결 — 대각으로 맞닿은 두 영역은 화면에서 이미 붙어 보인다."""
        flags = [False] * (W * H)
        for y in range(20, 30):
            for x in range(20, 30):
                flags[y * W + x] = True
        for y in range(30, 40):
            for x in range(30, 40):
                flags[y * W + x] = True
        self.assertEqual(label_components(flags, W, H, min_area=50).n_labels, 1)


class SelfOverlapTest(unittest.TestCase):
    """이 도구의 핵심 주의점 — 오버레이는 항상 자기 객체 위에 있다."""

    def test_lone_object_is_never_overlap(self):
        """혼자 있는 객체는 자기 원이 자기 몸통을 덮고 있어도 겹침이 아니다."""
        frames = [_object_frame([(40 + 6 * i, 70)]) for i in range(20)]
        verdicts, _ = analyze_frames(frames, min_blob_px=20)
        self.assertTrue(all(len(v.circles) == 1 for v in verdicts))
        self.assertFalse(clip_verdict(verdicts).overlap)

    def test_two_far_objects_are_not_overlap(self):
        """멀리 떨어진 두 객체도 각자 자기 원만 덮고 있으므로 겹침이 아니다."""
        frames = [_object_frame([(30 + 3 * i, 40), (150 - 3 * i, 120)]) for i in range(20)]
        verdicts, _ = analyze_frames(frames, min_blob_px=20)
        self.assertTrue(all(len(v.circles) == 2 for v in verdicts))
        self.assertFalse(clip_verdict(verdicts).overlap)

    def test_own_component_is_attributed(self):
        frames = [_object_frame([(40 + 6 * i, 70)]) for i in range(20)]
        bg = background_percentile(frames)
        mask = _mask_from(frames[10], bg)
        circles = detect_circles(frames[10])
        owners = attribute_circles(circles, mask)
        self.assertEqual(len(owners), 1)
        self.assertNotEqual(owners[0], BACKGROUND)

    def test_static_object_has_no_blob_and_is_skipped(self):
        """클립 내내 멈춰 있는 객체는 배경에 흡수돼 자기 블롭이 없다 — 알려진 한계.

        그 원은 자기 픽셀과 남의 픽셀을 가를 근거가 없으므로 circle_object 검사에서
        빠지고 unattributed로 집계된다. 거짓 양성을 내지 않는 쪽으로 실패해야 한다.
        """
        frames = [_object_frame([(70, 70)]) for _ in range(20)]
        verdicts, _ = analyze_frames(frames, min_blob_px=20)
        self.assertEqual(verdicts[0].unattributed, 1)
        self.assertFalse(clip_verdict(verdicts).overlap)


class FrameVerdictTest(unittest.TestCase):
    """판정 3기전을 마스크·원 목록을 직접 만들어 하나씩 고정한다."""

    def _mask(self, rects):
        """rects = [(x0,y0,x1,y1)...] 각각을 따로 떨어진 성분으로 만든다."""
        flags = [False] * (W * H)
        for x0, y0, x1, y1 in rects:
            for y in range(y0, y1):
                for x in range(x0, x1):
                    flags[y * W + x] = True
        return label_components(flags, W, H, min_area=20)

    def test_circle_circle(self):
        mask = self._mask([(20, 20, 60, 60), (120, 20, 160, 60)])
        circles = [Circle(40, 40, R_OVERLAY), Circle(58, 40, R_OVERLAY)]   # 거리 18 < 24
        owners = [mask.at(40, 40), mask.at(58, 40)]
        v = frame_verdict(circles, mask, owners, use_merged_blob=False)
        self.assertIn("circle_circle", v.reasons)
        self.assertTrue(v.overlap)

    def test_circles_just_apart_are_clear(self):
        mask = self._mask([(20, 20, 60, 60), (120, 20, 160, 60)])
        circles = [Circle(40, 40, R_OVERLAY), Circle(140, 40, R_OVERLAY)]
        owners = [mask.at(40, 40), mask.at(140, 40)]
        v = frame_verdict(circles, mask, owners)
        self.assertFalse(v.overlap)

    def test_circle_object(self):
        """A의 원판이 **B의 성분** 픽셀을 덮으면 겹침 — 원끼리는 닿지 않아도 된다."""
        mask = self._mask([(20, 20, 60, 60), (66, 20, 120, 60)])
        # 원 A는 x=58에, 그 원판(반지름 12)이 x=66부터 시작하는 B의 성분을 덮는다.
        circles = [Circle(58, 40, R_OVERLAY), Circle(100, 40, R_OVERLAY)]
        owners = [mask.at(58, 40), mask.at(100, 40)]
        self.assertNotEqual(owners[0], owners[1])       # 두 성분은 분리돼 있다
        v = frame_verdict(circles, mask, owners)
        self.assertIn("circle_object", v.reasons)
        self.assertNotIn("circle_circle", v.reasons)    # 중심 거리 42 > 24

    def test_unowned_component_is_not_foreign(self):
        """원이 붙지 않은 성분(그림자·타임스탬프 글자·객체 조각)은 남의 객체가 아니다.

        이 규칙이 없으면 차분이 한 객체를 여러 조각으로 쪼갤 때 자기 조각을 남의
        객체로 오인해 사실상 모든 프레임이 양성이 된다(실측으로 확인된 증상).
        """
        mask = self._mask([(20, 20, 60, 60), (66, 20, 120, 60)])
        circles = [Circle(58, 40, R_OVERLAY)]           # 오른쪽 성분에는 원이 없다
        owners = [mask.at(58, 40)]
        v = frame_verdict(circles, mask, owners)
        self.assertFalse(v.overlap)

    def test_merged_blob(self):
        """두 원이 같은 성분을 자기 것으로 삼으면 = 두 객체 픽셀이 이어져 있다."""
        mask = self._mask([(20, 20, 160, 60)])          # 하나로 이어진 성분
        circles = [Circle(40, 40, R_OVERLAY), Circle(140, 40, R_OVERLAY)]
        owners = attribute_circles(circles, mask)
        self.assertEqual(owners[0], owners[1])
        v = frame_verdict(circles, mask, owners)
        self.assertIn("merged_blob", v.reasons)

    def test_merged_blob_can_be_disabled(self):
        mask = self._mask([(20, 20, 160, 60)])
        circles = [Circle(40, 40, R_OVERLAY), Circle(140, 40, R_OVERLAY)]
        owners = attribute_circles(circles, mask)
        v = frame_verdict(circles, mask, owners, use_merged_blob=False)
        self.assertFalse(v.overlap)

    def test_unattributed_circle_is_counted_not_flagged(self):
        mask = self._mask([(20, 20, 60, 60)])
        circles = [Circle(40, 40, R_OVERLAY), Circle(150, 130, R_OVERLAY)]   # 두 번째는 블롭 없음
        v = frame_verdict(circles, mask)
        self.assertEqual(v.unattributed, 1)
        self.assertFalse(v.overlap)

    def test_tiny_incursion_is_below_threshold(self):
        """한두 픽셀 스침은 겹침으로 세지 않는다(min_foreign_px)."""
        mask = self._mask([(20, 20, 60, 60), (69, 38, 120, 42)])
        circles = [Circle(58, 40, R_OVERLAY), Circle(100, 40, R_OVERLAY)]
        owners = [mask.at(58, 40), mask.at(100, 40)]
        v = frame_verdict(circles, mask, owners, min_foreign_px=10_000)
        self.assertNotIn("circle_object", v.reasons)


class HelperTest(unittest.TestCase):
    def test_owned_components_keeps_first_circle(self):
        self.assertEqual(owned_components([3, 3, 5, BACKGROUND]), {3: 0, 5: 2})

    def test_foreign_pixels_excludes_own_and_unowned(self):
        flags = [False] * (W * H)
        for y in range(30, 50):
            for x in range(30, 50):
                flags[y * W + x] = True
        mask = label_components(flags, W, H, min_area=20)
        c = Circle(40, 40, R_OVERLAY)
        own = mask.at(40, 40)
        self.assertEqual(foreign_pixels(c, own, mask, {own: 0}), [])   # 전부 자기 것


class ClipVerdictTest(unittest.TestCase):
    """20장 중 1장이라도 겹치면 클립이 겹침 — 설계 §5-2의 확정된 의미."""

    def _frames(self, flags):
        return [FrameVerdict(f, {"circle_circle"} if f else set(), [], [], {}, {}, 0)
                for f in flags]

    def test_single_overlap_frame_flags_the_clip(self):
        v = clip_verdict(self._frames([False] * 19 + [True]))
        self.assertTrue(v.overlap)
        self.assertEqual(v.n_overlap_frames, 1)
        self.assertEqual(v.n_frames, 20)

    def test_no_overlap_frame_leaves_clip_clear(self):
        v = clip_verdict(self._frames([False] * 20))
        self.assertFalse(v.overlap)
        self.assertEqual(v.n_overlap_frames, 0)

    def test_reasons_are_counted_per_frame(self):
        v = clip_verdict(self._frames([True, False, True]))
        self.assertEqual(v.reasons["circle_circle"], 2)


class EndToEndSyntheticTest(unittest.TestCase):
    """합성 클립 20장을 통째로 돌려 검출·배경·연관·판정이 함께 동작하는지 본다."""

    def test_crossing_objects_flag_only_the_close_frames(self):
        frames = []
        for i in range(20):
            left = 30 + 5 * i             # 오른쪽으로 이동
            right = 170 - 5 * i           # 왼쪽으로 이동 — 중간(i=14 부근)에서 스친다
            frames.append(_object_frame([(left, 80), (right, 80)]))
        verdicts, masks = analyze_frames(frames, min_blob_px=20)
        v = clip_verdict(verdicts)
        self.assertTrue(v.overlap)
        self.assertGreater(v.n_overlap_frames, 0)
        self.assertLess(v.n_overlap_frames, 20)       # 멀리 있는 프레임은 깨끗해야 한다
        self.assertFalse(verdicts[0].overlap)
        self.assertEqual(len(verdicts[0].circles), 2)

    def test_parallel_objects_never_flag(self):
        """나란히 멀리 떨어져 같은 방향으로 가는 두 객체는 한 프레임도 겹치지 않는다."""
        frames = [_object_frame([(30 + 5 * i, 40), (30 + 5 * i, 120)]) for i in range(20)]
        verdicts, _ = analyze_frames(frames, min_blob_px=20)
        self.assertFalse(clip_verdict(verdicts).overlap)


class ManifestTest(unittest.TestCase):
    """재절단 없이 manifest만 갱신한다 — 기존 reflag 인프라와 같은 방식."""

    def _manifest(self):
        return {"extractor_version": "v3.3", "clips": [
            {"condition": "near_miss", "clip": "near_miss/a.mp4"},
            {"condition": "control", "clip": "control/b.mp4"},
            {"condition": "approach_only", "clip": "approach_only/c.mp4"},
            {"condition": "no_contact", "clip": "no_contact/d.mp4"},
            {"condition": "full", "clip": "full/e.mp4"},
            {"condition": "no_approach", "clip": "no_approach/f.mp4"},
        ]}

    def _verdict(self, n_overlap: int, n: int = 20) -> ClipVerdict:
        hot = FrameVerdict(True, {"circle_circle"}, [], [], {}, {}, 0)
        clear = FrameVerdict(False, set(), [], [], {}, {}, 0)
        return clip_verdict([hot] * n_overlap + [clear] * (n - n_overlap))

    def test_only_contactless_conditions_are_targets(self):
        self.assertEqual(set(TARGET_CONDITIONS),
                         {"near_miss", "no_contact", "approach_only", "control"})

    def test_flags_written_to_target_conditions_only(self):
        m = self._manifest()
        summary = apply_overlay_flags(m, {"near_miss/a.mp4": self._verdict(3)})
        by = {c["clip"]: c for c in m["clips"]}
        self.assertTrue(by["near_miss/a.mp4"]["overlay_overlap"])
        self.assertEqual(by["near_miss/a.mp4"]["overlay_overlap_frames"], 3)
        self.assertEqual(by["near_miss/a.mp4"]["overlay_frames_checked"], 20)
        self.assertNotIn("overlay_overlap", by["full/e.mp4"])      # 충돌 조건은 대상 아님
        self.assertNotIn("overlay_overlap", by["no_approach/f.mp4"])
        self.assertEqual(summary["eligible"], 4)
        self.assertEqual(summary["flagged"], 1)
        self.assertEqual(summary["missing"], 3)                    # 판정 없이 남은 대상

    def test_clear_clip_is_written_as_false_not_omitted(self):
        """겹치지 않았다는 것도 기록해야 한다 — 층화에서 '판정 없음'과 구별돼야 하므로."""
        m = self._manifest()
        apply_overlay_flags(m, {"control/b.mp4": self._verdict(0)})
        by = {c["clip"]: c for c in m["clips"]}
        self.assertIs(by["control/b.mp4"]["overlay_overlap"], False)
        self.assertEqual(by["control/b.mp4"]["overlay_overlap_frames"], 0)

    def test_method_is_recorded(self):
        """같은 필드를 철회된 기하 문턱으로 채운 적이 있어 방법을 남겨야 한다."""
        m = self._manifest()
        apply_overlay_flags(m, {})
        self.assertEqual(m["overlay_flag_method"], "pixel")

    def test_reassignment_overwrites_previous_values(self):
        m = self._manifest()
        apply_overlay_flags(m, {"near_miss/a.mp4": self._verdict(5)})
        apply_overlay_flags(m, {"near_miss/a.mp4": self._verdict(0)})
        by = {c["clip"]: c for c in m["clips"]}
        self.assertIs(by["near_miss/a.mp4"]["overlay_overlap"], False)
        self.assertEqual(by["near_miss/a.mp4"]["overlay_overlap_frames"], 0)


class FfmpegCmdTest(unittest.TestCase):
    def test_samples_twenty_frames_at_ten_fps(self):
        """모델이 보는 것과 같은 샘플링이어야 한다 — 2초 클립 20프레임 = 10fps."""
        cmd = ffmpeg_frames_cmd("clip.mp4", "out_%03d.png")
        self.assertIn("fps=10", cmd)
        self.assertEqual(cmd[cmd.index("-frames:v") + 1], "20")
        self.assertEqual(cmd[-1], "out_%03d.png")


if __name__ == "__main__":
    unittest.main()
