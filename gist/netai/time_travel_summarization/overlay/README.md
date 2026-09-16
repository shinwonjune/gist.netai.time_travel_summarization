# overlay — 뷰포트에 3D 객체 ID 라벨과 시각 HUD를 그리는 GUI 전용 프리뷰 오버레이

## 역할과 위치

`gist/netai/time_travel_summarization/overlay/`는 Omniverse Kit 에디터의 **뷰포트**(사람이
보는 3D 창) 위에 두 가지를 얹는다. 하나는 각 우주인(astronaut) 프림 머리 위에 떠서 항상
카메라를 향하는 원형 숫자 라벨(객체 ID)이고, 다른 하나는 뷰포트 우측 하단의 현재 시각
HUD(HH:MM:SS)다. 호출부는 `extension.py`(`on_startup`) 한 곳뿐이며, 활성 뷰포트 창
(`omni.kit.viewport.utility.get_active_viewport_window()`)을 얻은 뒤 `ViewOverlay`와
`OverlayControlWindow`를 만든다.

이 두 요소와 그것을 켜고 끄는 창이 실제로 어떻게 보이는지는 그림 1에 있다.

<!-- 이미지 TODO | 파일: images/readme/overlay-01-viewport-and-control.png | 촬영: Kit 에디터에서 우주인 프림이 여럿 있는 씬을 열고 View Overlay가 켜진 상태로 뷰포트를 캡처. 뷰포트 안에 (a) 각 우주인 머리 위 흰 원+검은 숫자 ID 라벨 여러 개, (b) 우측 하단의 HH:MM:SS 시간 HUD, (c) 작은 "View Overlay" 창(제목 아래 "Display Options", 체크박스 "Object IDs"/"Timestamp", 그 아래 "Visual Complexity" 버튼 3개)이 한 화면에 함께 보이게 | 형식: png -->
![그림 1. View Overlay가 켜진 뷰포트와 Overlay Control 창](../../../../images/readme/overlay-01-viewport-and-control.png)
*그림 1. 뷰포트에 그려진 우주인별 원형 ID 라벨과 우측 하단 시간 HUD, 그리고 이를 토글하는 "View Overlay" 창. 라벨은 뷰포트를 회전해도 항상 카메라를 향한다(빌보드).*

**이 패키지는 GUI 육안 프리뷰 전용이며, VLM이 실제로 보는 학습, 추론 영상의 시각 규약과는
다른 별개의 규약을 쓴다.** 학습, 추론 영상에 굽는 오버레이(타임스탬프, ID 원)는
`video_capture/overlay_composer.py`가 캡처 프레임에 PIL로 직접 합성하는 것이고, 여기
`overlay/`는 `omni.ui.scene`으로 뷰포트에 실시간으로 그리는 것이라 완전히 다른 렌더
경로다. 두 경로의 라벨 오프셋 값이 다른 이유는 아래 "핵심 설계 결정과 규약"을 참조.

이 패키지는 상태를 거의 갖지 않는다 — 어떤 프림이 활성 객체인지는
`app.facade.TimeTravelCore._prim_map`이 갖고 있고, `overlay/`는 매 프레임 스테이지를 조회해
`/World/TimeTravel_Objects`의 자식 프림들을 스캔하는 쪽이다(구독 갱신형이 아니라 폴링형).

## 구성 파일

| 파일 | 책임 | 핵심 심볼 |
|---|---|---|
| `core.py` | 오버레이 씬(SceneView), 라벨 레지스트리, 시간 HUD의 수명주기를 관리하는 컨트롤러 | `ViewOverlay` |
| `components.py` | 개별 3D 라벨 매니퓰레이터, 라벨 컬렉션, 시간 HUD 프레임 | `ObjectIdManipulator`, `PrimLabelRegistry`, `TimeDisplayOverlay` |
| `window.py` | 라벨/시간 표시 토글 + 표시 복잡도(Full/Simplified/Abstract) 버튼을 담은 소형 제어 창 | `OverlayControlWindow` |
| `__init__.py` | 패키지 설명 docstring만(재export 없음 — 호출부는 `overlay.core`/`overlay.window`에서 직접 import) | — |

## 동작 흐름

### 생성 (`extension.py::on_startup`)

```
overlay_available = (overlay.core / overlay.window / omni.kit.viewport.utility import 성공?)
if overlay_available:
    viewport_window = get_active_viewport_window()
    if viewport_window:
        self._overlay = ViewOverlay(viewport_window, ext_id, self._core)
        self._overlay_control = OverlayControlWindow(self._overlay)
```

이 블록 전체가 `try/except`로 감싸여 있고 **실패해도 확장 시작을 막지 않는다**(주석에
"OPTIONAL"로 명시). 뷰포트가 없는 headless 실행이나 import 실패 시 오버레이 없이
나머지 확장 기능(재생, 캡처, VLM 등)은 정상 동작한다.

### `ViewOverlay.__init__`

1. `omni.usd.get_context().get_stage_event_stream()`을 구독해 스테이지가
   열리면(`StageEventType.OPENED`) `_build_scene_for_stage()`로 씬을 새로 짓고, 닫히면
   (`CLOSED`) `_cleanup_scene()`으로 정리한다.
2. `TimeDisplayOverlay`를 먼저 만들어 뷰포트 프레임에 HUD를 붙인다(라벨과 독립적으로
   항상 존재).
3. `omni.kit.app`의 update 이벤트 스트림을 구독해 `_on_update`가 매 프레임 호출되게 한다.
4. 이미 열린 스테이지가 있으면 곧바로 `_build_scene_for_stage()`를 호출한다.

### 매 프레임 (`_on_update`)

- 라벨이 보이는 상태(`_labels_visible`)면 `_ensure_scene_current()`로 `/World/TimeTravel_Objects`의
  자식 집합이 씬 구축 시점과 달라졌는지(`PrimLabelRegistry.matches_parent`) 확인하고,
  달라졌으면 씬을 통째로 재구축한 뒤 `PrimLabelRegistry.update_positions()`로 각 라벨의
  월드 좌표를 갱신한다.
- 시간 표시가 켜져 있으면 `core.get_simulation_time()`(`app.facade.TimeTravelCore`)을 호출해
  `HH:MM:SS`로 포맷한 뒤 HUD 라벨에 반영한다.
- **주의**: `extension.py`의 `_on_update`는 `self._window.update_ui()`(메인 재생 창)만 직접
  호출하고 `ViewOverlay`는 부르지 않는다 — 오버레이는 위 3단계에서 스스로 구독한 프레임
  이벤트로 독립적으로 갱신된다(`extension.py`의 주석 "ViewOverlay updates itself via frame
  subscription").

### 라벨 하나의 갱신 (`ObjectIdManipulator`)

- `on_build`: 프림의 월드 변환에서 위치를 구해 `_LABEL_Y_OFFSET`(145.0)만큼 위로 띄운 뒤,
  카메라를 향하는(`sc.Transform(look_at=sc.Transform.LookAt.CAMERA)`) 흰 원(`sc.Arc`,
  radius=26)과 그 위의 검은 숫자(`sc.Label`)를 그린다.
- `update_position`: 프림이 무효화됐으면 `rebind_current_prim()`으로 다시 바인딩을 시도하고,
  실패하면 갱신을 건너뛴다. 프림이 숨김 처리됐으면(트랙 시작 전/종료 후) 라벨도 함께
  숨긴다 — 지워진 죽은 트랙 위에 ID 라벨만 남으면 화면에서 가짜 접촉으로 오인되기
  때문이다. 마지막 위치와 같으면(`_last_position` 캐시) 변환 행렬을 다시 만들지 않는다.

### 라벨 컬렉션 (`PrimLabelRegistry`)

`build_for_parent(parent_prim)`이 `/World/TimeTravel_Objects`의 모든 자식을 순회하며
이름 끝 3자리가 숫자인 프림(`_extract_id`)마다 `ObjectIdManipulator`를 하나씩 만든다. 끝
3자리가 아닌 나머지는 조용히 건너뛴다(에러 아님). `matches_parent`는 현재 자식 집합이
레지스트리 구축 시점의 집합과 같고, 등록된 모든 매니퓰레이터가 여전히 유효한 프림을
가리키는지(`has_current_prim`)까지 함께 확인한다 — 둘 중 하나라도 달라지면 `ViewOverlay`가
씬을 재구축한다.

## 핵심 설계 결정과 규약

- **GUI 라벨 오프셋(145)과 캡처 오프셋(130)은 값이 다르며, 의도적으로 동기화하지
  않는다.** `components.py`의 `ObjectIdManipulator._LABEL_Y_OFFSET = 145.0`은 GUI 육안
  확인(200=머리 위, 150=어깨 살짝 위 → 145로 확정)으로 정한 값이고,
  `video_capture/overlay_composer.py`의 `MARKER_UP_OFFSET = 130.0`은 캡처 카메라(BEV
  투시)에서 몸통이 마커 밖으로 삐져나오지 않게 정한 값이다. 두 상수는 각각 다른 코드
  주석에서 서로를 참조하며 "±15 이내 차이는 시점(캡처 카메라 vs 에디터 뷰)에 따른
  지각 차이로 허용한다"고 명시한다 — `components.py`의 145는 GUI 전용이므로 캡처 상수에
  맞춰 바꾸지 말 것.
- **GUI 원 반지름(26)도 캡처 반지름(9px)과 단위가 다르다.** GUI 쪽은 씬 공간
  단위(`sc.Arc(radius=26, ...)`, `thickness=40`)이고 캡처 쪽(`MARKER_RADIUS_PX`)은 합성된
  래스터 이미지의 픽셀 반지름이다. `components.py` 주석에 "숫자(size=30)가 원 밖으로
  삐져나옴(육안 확인) → 26으로 재확대"라는 조정 경위가 남아 있으며, 두 값을 비례로
  맞추도록 강제하지 않는다.
- **라벨은 항상 카메라를 향한다(빌보드).** `sc.Transform(look_at=sc.Transform.LookAt.CAMERA)`로
  감싸 사용자가 뷰포트를 회전해도 숫자가 항상 정면으로 보인다.
- **숨겨진 프림의 라벨은 함께 숨긴다.** `update_position`이 `UsdGeom.Imageable(prim)`의
  가시성을 매 프레임 확인해, 트랙 범위 밖이라 프림이 숨겨졌으면(`playback/visibility.py`가
  결정) 라벨도 숨긴다. 이 체크가 없으면 사라진 트랙 자리에 번호만 떠 있어 화면상
  가짜 근접/충돌 인상을 만들 수 있다.
- **씬 재구축은 감시가 아니라 매 프레임 비교로 판정한다.** `PrimLabelRegistry`는 스테이지
  이벤트를 구독하지 않고(`ViewOverlay`가 OPENED/CLOSED만 구독), 매 프레임
  `matches_parent()`로 자식 프림 집합과 각 매니퓰레이터의 유효성을 다시 비교해 필요할
  때만 재구축한다 — 객체가 런타임에 동적으로 추가/제거되는(near-miss 대조군 스폰,
  `add_synthetic_objects` 등) 상황에서도 별도 이벤트 배선 없이 따라간다.
- **오버레이 생성 전체가 optional, best-effort다.** `extension.py`가 이 패키지의 import와
  뷰포트 획득을 모두 `try/except`로 감싸고, 실패해도 확장의 나머지 기능은 정상
  동작한다. headless 실행(뷰포트 없음)에서는 애초에 이 경로를 타지 않는다.

## 사용법

`overlay/`는 `omni.ui.scene`, `omni.usd`에 의존하는 Kit 전용 코드라 독립 CLI나 오프라인
스크립트가 없다. 사용은 항상 `extension.py`를 경유한다.

```python
from gist.netai.time_travel_summarization.overlay.core import ViewOverlay
from gist.netai.time_travel_summarization.overlay.window import OverlayControlWindow
from omni.kit.viewport.utility import get_active_viewport_window

viewport_window = get_active_viewport_window()
overlay = ViewOverlay(viewport_window, ext_id, core)   # core: app.facade.TimeTravelCore
control = OverlayControlWindow(overlay)                 # "View Overlay" 창(라벨/시간 토글)

overlay.set_labels_visible(False)   # 라벨만 끄기(HUD는 유지)
overlay.set_time_visible(False)     # 시간 HUD만 끄기
overlay.shutdown()                  # 확장 종료 시 구독 해제 + 씬 정리
control.destroy()
```

`OverlayControlWindow`의 "View Overlay" 창은 체크박스 두 개(Object IDs, Timestamp)와
"Visual Complexity" 버튼 3개(Full/Simplified/Abstract — `core.set_visual_complexity(level)`
호출)를 제공한다. 창 생성 직전 `ui.workspace.close_existing_window("View Overlay")`를 호출해
확장 핫리로드로 남는 유령 창을 파괴한다.

`set_visual_complexity(level)`이 실제로 무엇을 숨기는지는 `config.json`의
`visibility_groups`(그룹명 → 프림 경로 목록)와 `complexity_levels`(레벨 → 그 레벨에서
숨길 그룹명 목록)가 정의한다 — 이 확장의 `config.json` 기준으로는 `Simplified`가
`Furniture`, `Equipment` 그룹을 숨기고, `Abstract`는 여기에 더해 `Ground`,
`A_Exterior`, 그리고 **`TimeTravel_Objects`(우주인 프림 자체)까지** 숨긴다
(`playback/stage_object_controller.py::set_visual_complexity`). 즉 `Abstract`는
"배경만 걷어내고 객체는 남기는" 단계가 아니라 객체까지 포함해 거의 빈 무대를 만드는
단계다 — 세 단계의 실제 차이는 그림 2로 확인한다.

<!-- 이미지 TODO | 파일: images/readme/overlay-02-complexity-levels.png | 촬영: 같은 씬, 같은 카메라 위치에서 View Overlay 창의 "Visual Complexity" 버튼을 Full → Simplified → Abstract 순으로 눌러 가며 3장 캡처한 뒤 가로로 이어 붙임(각 약 400px 폭). Full은 Furniture/Equipment/Ground/A_Exterior/우주인이 모두 보이고, Simplified는 Furniture와 Equipment가 사라지고, Abstract는 추가로 Ground/A_Exterior/우주인 프림까지 사라져 거의 빈 화면이 되는 것이 비교되게(config.json의 complexity_levels 값 기준) | 형식: png -->
![그림 2. 시각 복잡도 3단계 — Full, Simplified, Abstract](../../../../images/readme/overlay-02-complexity-levels.png)
*그림 2. 같은 장면을 Full, Simplified, Abstract 순으로 캡처해 나란히 놓은 비교. Simplified에서 가구, 장비 그룹이 사라지고, Abstract에서는 바닥, 외벽에 더해 우주인 프림 자체까지 사라진다(현재 `config.json`의 `complexity_levels` 기준).*

## 테스트

이 패키지 자체를 대상으로 한 헤드리스 단위 테스트는 없다 — `overlay/`의 모든 클래스가
`omni.ui.scene`, `omni.usd`(Kit 런타임 바인딩)에 직접 의존하므로 Kit 밖(WSL pytest)에서
import조차 되지 않는다. `tests/test_overlay_composer.py`와 `tests/test_overlay_flags.py`는
이름이 비슷하지만 각각 `video_capture/overlay_composer.py`(캡처 합성기)와
`automation/overlay_flags.py`(캡처된 영상의 픽셀 겹침 QC 도구)를 대상으로 하며, 이
`overlay/` 패키지와는 무관하다.

## 한계와 주의

- **Kit 에디터 안에서만 검증 가능하다.** 오프라인/헤드리스 테스트로 라벨 위치, 가시성
  로직을 고정할 수 없으므로, 변경 시 실제 뷰포트에서 육안 확인이 필요하다.
- **GUI 라벨 상수(145, 26)는 학습 데이터의 시각 규약이 아니다.** VLM이 실제로 보는
  영상의 오버레이 규약(타임스탬프 위치, 마커 오프셋 130.0, 반지름 9px 등)은
  `video_capture/README.md`의 "핵심 설계 결정과 규약"과 `video_capture/overlay_composer.py`가
  정본이다. 이 패키지의 값을 바꿔도 학습, 추론 데이터에는 아무 영향이 없다.
- **오버레이가 조용히 비활성화될 수 있다.** headless 실행이나 뷰포트 부재 시
  `carb.log_warn`만 남기고 확장은 정상 기동한다 — GUI에서 라벨이 안 보이면 먼저
  콘솔 로그의 `[TimeTravel] Overlay components not available` 또는
  `[Extension] No active viewport found`를 확인할 것.
- `PrimLabelRegistry._extract_id`는 프림 이름의 **끝 3자리가 숫자**여야 라벨을 붙인다
  (예: `obj001`). 이 규칙에 맞지 않는 프림은 에러 없이 조용히 라벨링에서 제외된다.
