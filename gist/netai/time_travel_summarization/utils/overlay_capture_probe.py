"""
Overlay-inclusive capture API probe.

목적: capture_viewport_to_buffer는 3D 렌더만 잡고 UI overlay(우리의 ViewOverlay)는 빠진다.
      다른 Kit API 중 합성된 결과(=overlay 포함)를 캡처하는 게 있는지 찾는다.

사용법: Omniverse Kit Script Editor에서 viewport와 overlay가 켜진 상태로
    exec(open(r"C:\\Users\\wonjune\\workspace\\kit-app-template\\source\\extensions\\gist.netai.time_travel_summarization\\gist\\netai\\time_travel_summarization\\utils\\overlay_capture_probe.py", encoding="utf-8").read())
"""

import tempfile
from pathlib import Path

print("=" * 60)
print("Overlay-inclusive capture probe")
print("=" * 60)
print("사전 조건: TimeTravel/Overlay 창이 켜져 있고 overlay 텍스트가 viewport에 보임")
print()

# [1] capture_viewport_to_file — file 기반이 다를 가능성
print("[1] capture_viewport_to_file 시도")
try:
    from omni.kit.viewport.utility import get_active_viewport, capture_viewport_to_file
    vp = get_active_viewport()
    out_dir = Path(tempfile.gettempdir()) / "ttsum_overlay_probe"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / "via_to_file.png"
    if out_file.exists():
        out_file.unlink()
    cap = capture_viewport_to_file(vp, str(out_file))
    print(f"    ✓ 호출 OK, return: {type(cap).__name__}")
    print("    → 약 2~5초 후 다음 파일 열어보세요:")
    print(f"      {out_file}")
    print("    overlay 텍스트 보이면 '이 API가 답'")
except Exception as e:
    print(f"    ✗ FAIL: {e!r}")

print()

# [2] viewport API 메서드 — capture/snap 관련
print("[2] viewport 객체의 capture/snap/frame 관련 멤버")
try:
    from omni.kit.viewport.utility import get_active_viewport
    vp = get_active_viewport()
    candidates = [a for a in dir(vp) if any(k in a.lower() for k in ("cap", "snap", "frame", "screen"))]
    if candidates:
        for a in candidates:
            print(f"    vp.{a}")
    else:
        print("    (없음)")
except Exception as e:
    print(f"    ✗ FAIL: {e!r}")

print()

# [3] omni.kit.widget.viewport.capture 모듈
print("[3] omni.kit.widget.viewport.capture 모듈")
try:
    import omni.kit.widget.viewport.capture as cap_mod
    public = [a for a in dir(cap_mod) if not a.startswith("_")]
    print(f"    members: {public[:20]}")
except Exception as e:
    print(f"    ✗ FAIL: {e!r}")

print()

# [4] AppWindow capture (전체 윈도우 = UI 포함)
print("[4] omni.appwindow / AppWindow capture")
try:
    import omni.appwindow
    aw = omni.appwindow.get_default_app_window()
    candidates = [a for a in dir(aw) if any(k in a.lower() for k in ("cap", "snap", "frame", "screen", "swap"))]
    if candidates:
        for a in candidates:
            print(f"    aw.{a}")
    else:
        print("    (없음)")
except Exception as e:
    print(f"    ✗ FAIL: {e!r}")

print()

# [5] ViewportWindow (개별 viewport 창 단위)
print("[5] ViewportWindow API")
try:
    from omni.kit.viewport.window import get_viewport_window_instances
    instances = list(get_viewport_window_instances())
    print(f"    viewport window 개수: {len(instances)}")
    if instances:
        vw = instances[0]
        candidates = [a for a in dir(vw) if any(k in a.lower() for k in ("cap", "snap", "frame", "screen"))]
        for a in candidates:
            print(f"    vw.{a}")
except Exception as e:
    print(f"    ✗ FAIL: {e!r}")

print()
print("=" * 60)
print("Probe 완료. 위 출력 전체 + [1]번 PNG에 overlay가 잡혔는지 알려주세요.")
print("=" * 60)
