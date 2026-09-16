# ui — 메인 재생 제어 창과 백그라운드 스레드→메인 스레드 UI 디스패치 인프라

## 역할과 위치

`gist/netai/time_travel_summarization/ui/`는 확장의 메인 창인 "Time Travel Control"
(재생, 모드 전환, 캡처, 물리 배회, 레이크 성능 계측 제어)과, 이 확장 전체가 공유하는 두
가지 UI 인프라 유틸(핫리로드 유령 창 제거, 백그라운드 스레드에서 만든 결과를 안전하게
메인 스레드(omni.ui가 위젯 조작을 허용하는 유일한 스레드) UI에 반영하는 디스패처)을
담는다. 호출부는 `extension.py`(`on_startup`)이
`TimeTravelWindow(self._core)`를 만들고, 매 프레임(`_on_update`) `self._window.update_ui()`를
호출하는 한 곳뿐이다. `task_dispatcher.py`와 `workspace.py`는 이 메인 창뿐 아니라
`vlm_client/window.py`에서도 재사용된다.

이 패키지는 상태를 직접 소유하지 않는다 — 표시하는 모든 값(재생 진행률, 모드, 캡처
활성 여부 등)은 매번 `app.facade.TimeTravelCore`(생성자 인자 `core`)에 위임 조회하고,
버튼 클릭은 core의 메서드를 호출하는 얇은 이벤트 핸들러로만 구현돼 있다. `TimeTravelCore`
자체의 상태, 서비스 구조는 `app/README.md`를 참조.

## 구성 파일

| 파일 | 책임 | 핵심 심볼 |
|---|---|---|
| `main_window.py` | 메인 "Time Travel Control" 창 전체(데이터 소스, 시간 범위, Go to Time, 이벤트 요약, 재생, 모드 전환, 배회/캡처/trace, 타임라인 슬라이더, 레이크 성능 Probe) | `TimeTravelWindow` |
| `task_dispatcher.py` | 백그라운드 스레드의 콜백을 Kit 메인 update 루프로 안전하게 넘기는 큐 기반 디스패처 | `UiTaskDispatcher` |
| `workspace.py` | 핫리로드로 남는 동일 제목의 유령 창을 창 생성 직전에 파괴 | `close_existing_window` |
| `__init__.py` | 패키지 설명 docstring만(재export 없음) | — |

## 동작 흐름

### 창 구성 (`TimeTravelWindow.__init__`)

`close_existing_window("Time Travel")`로 핫리로드 유령 창을 먼저 치운 뒤 폭 500×높이
510의 `ui.Window`를 만들고, 위에서 아래로 다음 구획을 쌓는다(`main_window.py:31-247`).
각 구획이 어떤 위젯으로 되어 있고 어떤 `core`(`app.facade.TimeTravelCore`) 메서드를
호출하는지는 아래 표에, 표만으로 드러나지 않는 동작 방식은 표 아래에 따로 적었다.
표를 보기 전에 창 전체가 실제로 어떻게 쌓여 있는지는 그림 1을 먼저 보는 편이 이해가
빠르다.

<!-- 이미지 TODO | 파일: images/readme/ui-01-main-window-full.png | 촬영: Kit 에디터에서 "Time Travel" 창(제목은 "Time Travel", 안의 큰 라벨은 "Time Travel Control") 전체를 캡처. 창을 위에서 아래로 스크롤 없이 한 화면에 담아 Data Source부터 맨 아래 Probe(CollapsableFrame, 펼친 상태)까지 모든 구획이 보이게 | 형식: png -->
![그림 1. Time Travel Control 창 전체](../../../../images/readme/ui-01-main-window-full.png)
*그림 1. 위에서부터 Data Source, Load Time Range, Go to Time, Event based Summary, Twin Time, Play/Speed/Capture, Mode, Move Speed/Near-miss gap, Move/Trace, Timeline Slider, Probe 순으로 쌓인 구획들. 아래 표의 각 행이 이 그림의 한 구획에 대응한다.*

| 구획 | 위젯 | 호출하는 core 메서드 |
|---|---|---|
| Data Source | Local / Data Lake 버튼 | `core.set_data_source(mode)`(비동기 배선 — 아래 "① Data Source" 참조) |
| Load Time Range | 시작/끝 년월일시분초 `IntField` 12개 + Load Range 버튼 | `core.load_time_range(start, end)` |
| Go to Time | 년월일시분초 `IntField` 6개 + Go 버튼 | `core.set_current_time(...)` |
| Event based Summary | 체크박스 + Next Event 버튼 | `core.load_events_from_positions_jsonl()`, `core.set_use_event_summary()`, `core.go_to_next_event()` |
| Twin Time | 라벨(표시 전용) | `core.get_twin_time_string()`(아래 "② Twin Time" 참조) |
| Play/Speed/Capture | Play 버튼, Speed `FloatField`, Capture 버튼 + Length 필드 | `core.toggle_playback()`, `core.set_playback_speed()`, `core.start_capture(duration_s=...)`/`core.stop_capture()` |
| Mode(Playback/Physics) | Playback / Physics 버튼 | `core.set_playback_mode()`/`core.set_physics_mode()`(아래 "③ Mode" 참조) |
| Move Speed / Near-miss gap | `FloatField` 2개 | `core.set_wander_speed(...)`, `core.set_near_miss_gap(...)`(아래 "④ Near-miss gap" 참조) |
| Move/Trace | Move / Trace 버튼 | `core.start_wander()`/`stop_wander()`, `core.start_trace()`/`stop_trace()`(아래 "⑤ Move/Trace" 참조) |
| Timeline Slider | `FloatSlider` | `core.set_progress(progress)` |
| Probe(`CollapsableFrame`, 기본 접힘) | 체크박스, Scenario `StringField`, Start/Dump 버튼 | `core.set_lake_probe_enabled(...)`, `core.get_lake_probe()`(반환된 `app.lake_probe.LakeProbe`를 직접 조작 — Start=구간 시작/버퍼 리셋, Dump=버퍼를 JSON으로 저장) |

표에 담기지 않는, 구획별로 알아 둬야 할 동작:

- **① Data Source** — 버튼 클릭이 `core.set_data_source(mode)`를 즉시 부르지 않는다.
  `_request_source_switch(mode)`가 `_source_switch_pending` 플래그를 세운 뒤
  `UiTaskDispatcher.submit()`으로 다음 update 틱에 `_apply_source_switch`를 실행하도록
  미룬다(비동기로 미루는 이유는 아래 "핵심 설계 결정과 규약" 참조). Data Lake(데이터
  레이크, minIO에 적재된 궤적 데이터셋) 모드에서 Load Time Range를 실행하면, 지정한
  구간에 해당하는 데이터만 minIO에서 청크(궤적 데이터를 시간 구간별로 나눈 저장 단위)
  단위 윈도우로 로드된다.
- **② Twin Time** — "twin time"은 디지털 트윈 세계의 현재 시각(playback 모드=데이터
  시각, physics 모드=t0+경과)을 가리키는 이 확장의 용어이며, USD 타임라인 자체의
  "stage time"(초 단위)과는 구분된다(`facade.py:428-443`).
- **③ Mode(Playback/Physics)** — Physics로 전환하면 `core.set_physics_mode()`에 이어
  즉시 `core.stop_wander()`도 호출해 이전 배회 상태를 정리한다.
- **④ Near-miss gap** — 값 변경은 **다음 Physics 클릭부터** 반영된다(컨트롤러가 그
  시점에 생성되므로). near-miss 방식(swerve|stop)은 GUI에 콤보가 없고 facade
  기본값(swerve)을 그대로 쓴다 — GUI로 stop을 보려면 코드에서
  `core.set_near_miss_mode("stop")`을 Physics 클릭 전에 직접 호출해야 한다
  (`_on_near_miss_gap_changed`의 독스트링에 근거).
- **⑤ Move/Trace** — Move는 움직임만(`core.start_wander()`/`stop_wander()`), Trace는
  좌표 CSV 기록만(`core.start_trace()`/`stop_trace()`) 제어하며 둘은 독립이다. 충돌
  기록은 Capture가 소유하므로 Move만 눌러서는 충돌 CSV가 남지 않는다.

### 매 프레임 (`extension.py::_on_update` → `TimeTravelWindow.update_ui()`)

`update_ui()`는 core 상태를 읽어 다음을 갱신한다: Twin Time 라벨, (재생 중이면) 슬라이더
값, 진행률 %(재생 여부와 무관하게 매 프레임), Play/Pause 버튼 텍스트, 모드 라벨과 버튼 활성화, 데이터 소스 라벨과 버튼
활성화, Move/Trace 버튼 텍스트, Probe 상태 문자열(`_update_probe_status`, ~2Hz로만
갱신 — 매 프레임 문자열을 새로 만들면 측정 대상 프레임 자체에 부하가 섞이므로), Capture
버튼 텍스트.

### 데이터 소스 전환의 비동기 배선

```
버튼 클릭 → _request_source_switch(mode)
          → _source_switch_pending = True, 상태 라벨 "...loading..."
          → ui_dispatcher.submit(lambda: _apply_source_switch(mode))
          (다음 update 이벤트 틱에서 메인 스레드로 실행)
_apply_source_switch → _apply_lake_source_switch / _apply_local_source_switch
          → core.set_data_source(mode) 성공/실패에 따라 상태 메시지 갱신
          → finally: _source_switch_pending = False
```
로드 자체는 동기(같은 update 틱 안)로 끝나지만, 클릭 즉시가 아니라 **다음 프레임**으로
미루는 이유는 버튼 클릭 콜백 안에서 무거운 로드(파일 I/O, 네트워크)를 바로 실행하면
클릭 이벤트 처리 스택 안에서 UI가 멈춘 것처럼 보일 수 있어서다(`UiTaskDispatcher`가
큐잉하는 것과 같은 취지).

`_source_switch_pending`이 켜져 있는 그 짧은 구간에 창에 무엇이 보이는지는 그림 2와
같다.

<!-- 이미지 TODO | 파일: images/readme/ui-02-data-source-loading.png | 촬영: Data Lake 데이터셋(로드에 눈에 띄는 시간이 걸리는 것)을 연결한 상태에서 "Data Lake" 버튼을 누른 직후, 로드가 끝나기 전 순간을 캡처. Data Source 구획의 상태 라벨이 "Data Lake loading..."으로 바뀐 것이 보여야 함(main_window.py:300) — 타이밍이 짧으므로 여러 번 캡처를 시도하거나 화면 녹화 후 프레임을 뽑아도 됨 | 형식: png -->
![그림 2. Data Source 전환 중 상태](../../../../images/readme/ui-02-data-source-loading.png)
*그림 2. Data Lake 버튼을 누른 직후, 로드가 끝나기 전 상태 라벨이 "Data Lake loading..."으로 바뀐 순간. 로드가 끝나면 이 라벨은 성공/실패 메시지(또는 소스 이름)로 다시 바뀐다.*

### `UiTaskDispatcher`

`omni.kit.app`의 update 이벤트 스트림을 구독해 `SimpleQueue`에 쌓인 콜백을 매 프레임
`_drain()`에서 전부 소비한다. `submit(callback)`은 스레드 안전(`SimpleQueue`)하므로
백그라운드 워커 스레드(VLM 업로드/생성, 캡처 등)가 UI 위젯을 직접 건드리지 않고 여기로
결과를 넘긴다. `shutdown()`은 구독을 끊고 큐에 남은 항목을 비운다(구독 해제 후 들어오는
`submit`은 `_active=False`라 조용히 무시됨).

## 핵심 설계 결정과 규약

- **omni.ui 위젯은 메인 스레드에서만 안전하게 조작할 수 있다.** 이 제약이
  `UiTaskDispatcher`가 존재하는 유일한 이유다. `vlm_client/window.py`의 Upload/Delete/
  Generate 버튼은 각각 백그라운드 스레드(`threading.Thread(daemon=True)`)에서 core
  호출을 수행하고, 결과 반영(`_apply_*_result`)만 `ui_dispatcher.submit()`으로 메인
  루프에 넘긴다. `main_window.py`는 데이터 소스 전환에서 같은 패턴을 쓴다.
- **핫리로드 유령 창 방지는 창 생성 직전 호출이 규칙이다.** `close_existing_window`는
  확장이 코드 변경으로 핫리로드될 때 이전 `shutdown()`이 온전히 돌지 못해 같은 제목의
  창이 겹쳐 남는 문제(실측: Video ID의 "Not uploaded"와 새 파일명이 겹쳐 보임)를
  막는다. `omni.ui.Workspace.get_window(title)`로 기존 창을 찾아 `destroy()`하며,
  워크스페이스 조회 자체가 실패해도(초기 기동 등) 창 생성을 막지 않도록 전체를
  `try/except`로 감싼다.
- **슬라이더 갱신 루프는 플래그로 끊는다.** `_updating_slider`는 `update_ui()`가
  프로그램적으로 슬라이더 값을 설정할 때 `_on_slider_changed` 콜백이 다시 발화해
  `core.set_progress()`를 또 부르는 무한 루프를 막는 표준 UI 패턴이다.
- **Probe 상태 문자열은 스로틀링된다.** `_update_probe_status`는 0.5초(~2Hz)에 한 번만
  라이브 통계 문자열을 새로 만든다 — 레이크 성능을 측정하는 도구 자신이 매 프레임
  문자열 포매팅으로 부하를 더하면 측정 대상을 흔들기 때문이다(`lake_probe.py`의
  설계와 같은 원칙).
- **Near-miss 조향, 다양성 파라미터는 GUI 필드가 아니라 환경변수로 튜닝한다.** GUI에는
  gap(cm) 필드 하나만 있고, 회피 곡선의 완만함(`TTS_NEAR_MISS_AVOID_FRAC` 등)과 조우
  다양성(`TTS_NEAR_MISS_START_JITTER_S` 등)은 Kit을 해당 환경변수와 함께 띄우고
  Physics를 다시 누르는 방식으로 조정한다 — GUI에서 값을 눈으로 보며 반복 튜닝할 때
  코드 수정, 재빌드가 필요 없게 하기 위한 의도적 설계(`_on_near_miss_gap_changed`
  독스트링에 근거 서술).
- **Twin Time과 오버레이/CSV 시각 포맷은 다른 함수를 쓰지만 같은 정밀도를 공유한다.**
  `get_twin_time_string()`(날짜 포함, 창 라벨 전용)과 `get_stage_time_string()`
  (`HH:MM:SS`, 오버레이, CSV용 — 이 "오버레이"는 캡처된 영상에 타임스탬프를 굽는
  `video_capture/overlay_composer.py`가 쓰는 값이며, 뷰포트에 실시간으로 그리는
  `overlay/`(`overlay/README.md`) 패키지와는 다른 렌더 경로다)은 둘 다
  `timefmt.format_event_time`을 재사용해 시각 부분의 정밀도(PRECISION)를 공유한다 —
  `app.facade.TimeTravelCore`에 정의돼 있고 `ui/`는 전자만 호출한다.

## 사용법

`ui/`도 `omni.ui`에 의존하는 Kit 전용 코드이며 독립 CLI는 없다.

```python
from gist.netai.time_travel_summarization.ui.main_window import TimeTravelWindow

window = TimeTravelWindow(core)  # core: app.facade.TimeTravelCore, 이미 config/data 로드됨
# 매 프레임(omni.kit.app update 이벤트)마다 호출해야 라벨, 슬라이더, 버튼이 갱신된다
window.update_ui()
window.destroy()  # 확장 종료 시: 디스패처 shutdown + 창 destroy
```

`UiTaskDispatcher`와 `close_existing_window`는 다른 창에서도 같은 방식으로 재사용한다
(`vlm_client/window.py`가 실제 사용 예):

```python
from gist.netai.time_travel_summarization.ui.task_dispatcher import UiTaskDispatcher
from gist.netai.time_travel_summarization.ui.workspace import close_existing_window

close_existing_window("My Window")
dispatcher = UiTaskDispatcher("MyWindowUiDispatcher")
# 백그라운드 스레드에서:
dispatcher.submit(lambda: my_label.set_text("done"))
```

## 테스트

이 패키지를 직접 대상으로 하는 헤드리스 단위 테스트는 없다 — `main_window.py`, 
`task_dispatcher.py`, `workspace.py` 전부가 `omni.ui`/`omni.kit.app`/`carb`(Kit 런타임
바인딩)를 모듈 최상단에서 import하므로 Kit 밖(WSL pytest)에서 로드 자체가 되지 않는다.
`app/` 계층(예: `tests/test_gap_skip.py`, `tests/test_output_routing.py`)은 `carb` 스텁을
심어 `TimeTravelCore`의 로직만 검증하며, `ui/`가 그 값을 어떻게 화면에 그리는지는
검증 범위 밖이다.

## 한계와 주의

- **Kit 에디터 안에서만 검증 가능하다.** 버튼 배선, 레이아웃 변경은 실제 GUI 육안
  확인이 필요하다(다른 창 없는 것과 동일한 제약 — `overlay/README.md`, `physics/README.md`
  참조).
- **`_apply_source_switch`의 예외 처리는 최종 방어선이다.** 데이터 로드 중 예외가
  나면 `carb.log_error` + 상태 메시지로 잡아내지만, `finally`에서
  `_source_switch_pending = False`를 반드시 되돌리므로 버튼이 영구히 비활성 상태로
  잠기지는 않는다 — 다만 실패 사유는 콘솔 로그를 확인해야 한다.
- **near-miss 방식(stop) 전환은 GUI만으로 완결되지 않는다.** 위에서 설명한 대로 콤보
  하나를 더 추가하는 대신 스코프에서 제외됐으므로, GUI 조작만으로는 항상 swerve
  방식이 쓰인다.
- **Probe 체크박스는 구버전 core에서 no-op으로 안전하게 죽는다.** `_get_probe`/
  `_on_probe_enable_changed`는 `getattr(core, "get_lake_probe", None)`으로 메서드
  존재를 확인한 뒤 없으면 "probe: not supported by this build" 메시지만 띄운다 —
  `TimeTravelWindow`가 core의 정확한 클래스 구현체에 하드 의존하지 않는다는 뜻이며,
  테스트에서 `__new__` + 속성 주입으로 만든 최소 core에도 안전하다.
