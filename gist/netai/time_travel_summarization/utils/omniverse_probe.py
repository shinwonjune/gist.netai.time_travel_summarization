"""
Phase 0 — Omniverse API 사전 점검.

사용법: Omniverse Kit의 Window → Script Editor에서 본 파일 내용 전체를 붙여넣고 실행.
모든 결과는 print로 출력되며, 그 결과를 그대로 알려주시면 됩니다.

점검 항목 (총 5개):
  [1] omni.kit.capture.viewport (A1 Movie Capture)
  [2] omni.kit.viewport.utility (A2 viewport texture grab)
  [3] 1프레임 RGBA buffer 추출 + shape/dtype
  [4] 비동기 인코더 후보 (imageio / imageio_ffmpeg / av)
  [5] Omniverse Python 버전 / pip 가용성
"""

print("=" * 60)
print("Phase 0 Probe — Omniverse Kit API surface")
print("=" * 60)

# [1] omni.kit.capture.viewport (A1)
print("\n[1] omni.kit.capture.viewport")
try:
    import omni.kit.capture.viewport as cap
    print("    ✓ import OK")
    attrs = [a for a in dir(cap) if not a.startswith("_")][:15]
    print(f"    public attrs (head): {attrs}")
    try:
        from omni.kit.capture.viewport import CaptureOptions
        opt = CaptureOptions()
        print("    ✓ CaptureOptions() 인스턴스화 OK")
        print(f"    settable fields (head): {[a for a in dir(opt) if not a.startswith('_')][:10]}")
    except Exception as e:
        print(f"    ✗ CaptureOptions 사용 불가: {e}")
except Exception as e:
    print(f"    ✗ import FAIL: {e}")

# [2] omni.kit.viewport.utility (A2)
print("\n[2] omni.kit.viewport.utility")
try:
    from omni.kit.viewport.utility import get_active_viewport
    vp = get_active_viewport()
    print(f"    ✓ get_active_viewport(): {vp}")
    candidates = ["capture_viewport_to_buffer", "capture_viewport_to_file", "frame_info"]
    for name in candidates:
        try:
            from omni.kit.viewport import utility as vp_util
            print(f"    {name}: {'exists' if hasattr(vp_util, name) else 'NO'}")
        except Exception:
            pass
    if vp is not None:
        for name in ["frame_info", "next_frame_event", "viewport_api"]:
            print(f"    vp.{name}: {'exists' if hasattr(vp, name) else 'NO'}")
except Exception as e:
    print(f"    ✗ FAIL: {e}")

# [3] 1프레임 RGBA buffer 추출
print("\n[3] 1프레임 RGBA buffer 추출 시도")
try:
    from omni.kit.viewport.utility import get_active_viewport, capture_viewport_to_buffer

    captured = {}
    def _on_capture(buf, buf_size, width, height, fmt):
        captured["buf"] = buf
        captured["size"] = buf_size
        captured["wh"] = (width, height)
        captured["fmt"] = fmt
        print(f"    callback fired: {width}x{height}, fmt={fmt}, size={buf_size}")

    vp = get_active_viewport()
    if vp is None:
        print("    ✗ active viewport 없음 — 뷰포트 창을 띄운 뒤 재실행")
    else:
        cap_instance = capture_viewport_to_buffer(vp, _on_capture)
        print(f"    ✓ capture_viewport_to_buffer 호출 OK ({type(cap_instance).__name__})")
        print("    (콜백은 비동기 — 다음 프레임에 도달 후 위 callback fired 줄이 추가로 출력됩니다)")
except Exception as e:
    print(f"    ✗ FAIL: {e}")

# [4] 인코더 후보
print("\n[4] 인코더 후보 가용성")
for name, attr in [("imageio", "__version__"), ("imageio_ffmpeg", "get_ffmpeg_version"), ("av", "__version__")]:
    try:
        mod = __import__(name)
        v = getattr(mod, attr, None)
        v = v() if callable(v) else v
        print(f"    ✓ {name}: {v}")
    except Exception:
        print(f"    ✗ {name}: not available")

# [5] Python / pip
print("\n[5] Omniverse Python 환경")
import sys
print(f"    sys.executable: {sys.executable}")
print(f"    sys.version:    {sys.version.split()[0]}")
try:
    import pip
    print(f"    pip:            {pip.__version__}")
except Exception:
    print("    pip:            NOT available")

print("\n" + "=" * 60)
print("Phase 0 Probe 완료 — 위 출력 전체를 그대로 보고해 주세요.")
print("=" * 60)
