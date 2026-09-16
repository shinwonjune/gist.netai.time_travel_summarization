# app — 확장의 단일 진입 facade와 그 뒤에서 동작을 나눠 맡는 도메인 서비스

## 역할과 위치

`gist/netai/time_travel_summarization/app/`는 확장 전체의 상태를 한 곳에 모으는 facade
클래스 `TimeTravelCore`와, 그 상태를 실제로 조작하는 6개의 도메인 서비스 모듈
(데이터 소스, 재생 캡처, PhysX 물리, 객체 생성, lookup 벤치마크, 레이크 성능 계측)로
이루어진다. `extension.py`(`on_startup`)가 `TimeTravelCore()`를 한 번 생성해
`_INSTANCE_REGISTRY`에 등록하고, `ui/main_window.py`, `vlm_client/`, `overlay/`, 
`events/`, `automation/` 등 확장의 다른 모든 창, 모듈이 이 하나의 인스턴스를 통해서만
재생, 캡처, 물리, 데이터를 조작한다 — 이 확장에는 "core"가 여럿 존재하지 않는다.

**상태와 동작이 분리돼 있다.** `TimeTravelCore`가 갖는 속성(레포지토리, prim_map,
캡처/물리 플래그, near-miss 파라미터 등)이 확장의 유일한 진실 공급원이고, 실제 동작
로직은 `physics_service.py`, `capture_service.py`, `data_service.py`, `object_service.py`, 
`benchmark_service.py`의 모듈 레벨 함수들이 `core`를 첫 인자로 받아 수행한다.
`facade.py` 상단 docstring이 이 분리의 이유를 명시한다 — 서비스가 자체 상태를 들면
두 가지가 깨진다. 첫째, 테스트가 `TimeTravelCore.__new__(TimeTravelCore)` +
속성 직접 주입으로 최소 core를 구성하는 패턴을 쓸 수 없게 된다. 둘째,
`extension.py::on_shutdown`이 종료 시 `core._wander`를 직접 참조해 안전 정지시키는
코드가 있는데, `_wander`가 core 밖에 있으면 이 정리가 불가능하다. 공개 API(메서드
시그니처)는 이 서비스 분해 이전과 동일하게 유지된다.

객체 생성, 재생 헤드→USD 프림 반영, 궤적 데이터 조회 자체의 상세 로직은
`playback/README.md`(`TrajectoryRepository`, `PlaybackController`,
`StageObjectController`)를, PhysX 물리, 충돌 콜라이더, 배회 안무의 상세 로직은
`physics/README.md`(`WanderController`, `TraceRecorder`, `CollisionRecorder` 등)를,
캡처 실행기 자체는 `video_capture/README.md`(`RealtimeCaptureRunner`)를 참조한다.
`app/`는 이 모든 하위 도메인을 **조립**하는 층이며, 여기 문서는 조립 지점(누가 언제
누구를 부르는가)과 이 계층 고유의 설계 결정에 집중한다.

## 구성 파일

| 파일 | 책임 | 핵심 심볼 |
|---|---|---|
| `facade.py` | 확장 전체 상태 단일 소유, 서비스 호출로 위임하는 공개 API | `TimeTravelCore` |
| `config.py` | `config.json` + `.env` 파싱, URI 변환 | `ExtensionConfig`, `_load_dotenv` |
| `paths.py` | `artifacts/` 하위 산출물 디렉터리 정책 | `ExtensionPaths` |
| `data_service.py` | 데이터 소스(local/lake) 활성화, config 로드, URI 해석 | `load_config`, `load_data`, `set_data_source`, `activate_data_source`, `resolve_uri` |
| `capture_service.py` | 캡처 수명주기(사이드카 기록, 백그라운드 워커, headless 실행) | `start_capture`, `run_capture_headless`, `stop_capture`, `write_capture_sidecar` |
| `physics_service.py` | Physics 모드 전환, 아레나(배회 영역) 산정, 충돌/궤적 recorder | `set_physics_mode`, `set_playback_mode`, `on_collision_event` |
| `object_service.py` | 우주인 프림 생성, 활성 부분집합 관리 | `regenerate_astronauts_from_loaded_data`, `spawn_objects`, `set_active_objects` |
| `benchmark_service.py` | 궤적 조회(lookup) 알고리즘 성능 벤치마크(콘솔/CSV 전용, GUI 노출 없음) | `start_lookup_benchmark`, `run_lookup_benchmark_suite` |
| `lake_probe.py` | GUI 재생 성능 계측(링버퍼 + 조건부 덤프) | `LakeProbe`, `sanitize_scenario` |
| `__init__.py` | 패키지 설명 docstring만(재export 없음) | — |

## 동작 흐름

### 확장 기동부터 첫 프레임까지

```
extension.py::on_startup
  1. core = TimeTravelCore()                      # __init__: 리포지토리, 플레이백, 서비스 상태 초기화
  2. core.load_config(config_path)                 -> data_service.load_config
  3. (auto_generate 설정이면) core.auto_generate_astronauts()  -> object_service
  4. core.load_data()                               -> data_service.load_data -> activate_data_source
  5. TimeTravelWindow(core), EventProcessingWindow(core, ext_id), ... 각 창 생성
  6. core.set_to_earliest_time()                    # 데이터가 있으면 재생 헤드를 최초 시각으로
```

### 데이터 소스 활성화 (`data_service.activate_data_source`)

1. `mode`("local" | "lake")에 따라 원본 URI를 고른다. lake 모드는 먼저
   `lake.direct_data_uri`(단일 파일 테스트용)를 보고, 없으면 `lake.manifest_uri`를 쓴다.
2. **리더는 버튼이 아니라 URI의 모양이 정한다**(`_repo_factory_for`) — URI가
   `manifest.json`으로 끝나면 `LakeTrajectoryRepository`(궤적 데이터를 시간 구간별로
   나눈 청크 단위로 윈도우 로딩 + 프리페치하는 리포지토리 — `vlm_client/`가 VLM
   추론 입력을 2초 단위로 자르는 것과는 다른, 저장소 쪽 분할 단위다),
   아니면 `TrajectoryRepository`(단일 파일)를 쓴다. 이 설계 이유는 "핵심 설계 결정"
   절 참조.
3. 새 레포지토리로 데이터를 로드하고, 성공하면 이전 레포지토리를 `clear()`한 뒤
   `core._repository`를 교체한다. `core._events`(`EventSummaryService`)도 새
   레포지토리로 다시 만든다.
4. `core._playback.configure_data_range(...)`로 재생 가능 구간을 갱신한다.
5. lake 모드이거나 **청크 데이터셋이면**(단일 파일이 아니라 manifest 기반이면, 로컬
   미러를 Local 버튼으로 열었어도) `core.regenerate_astronauts_from_loaded_data()`로
   우주인 프림을 데이터의 objid 집합에 맞춰 다시 만든다.

### 캡처 시작 (`capture_service.start_capture`, GUI 경로)

1. 이미 활성 캡처가 있으면 거부.
2. `duration_s <= 0`이면 기본 60초로 대체.
3. playback 모드에서 일시정지 상태로 Capture를 누르면 **캡처 시작이 곧 재생 시작**이
   되도록 `core.toggle_playback()`을 먼저 호출한다(정지 화면만 찍히는 것 방지).
   physics 모드는 Move 버튼이 구동을 담당하므로 건드리지 않는다.
4. physics 모드면 `physics_service.start_collision_recorder(core)`를 캡처와 함께
   시작한다 — **충돌 기록 창 == 캡처 창**이라는 설계로, CSV 길이가 영상 길이와
   정확히 일치하게 한다.
5. `capture_anchor(core)`로 t0(캡처 앵커)를 정하고 `write_capture_sidecar()`로
   `<video>.meta.json`을 먼저 쓴 뒤, 백그라운드 스레드에서
   `video_capture.RealtimeCaptureRunner.capture()`를 실행한다(`_start_capture_backend`).
   워커 종료 시 `core._capture_active = False`와 함께
   `physics_service.stop_collision_recorder(core)`도 호출한다 — **캡처 종료 ==
   충돌 기록 종료**.
6. 성공 시 `core._capture_complete_cb`(있으면, `extension.py`가
   `VLMClientWindow.set_source_uri`를 배선)로 결과 URI를 전달한다.

headless 배치(`run_capture_headless`)는 뷰포트 없이 render product로 오프스크린
렌더한다는 점과, `replay_start_dt`가 주어지면 물리 대신 좌표 데이터를 그대로 재생하는
"재연 모드"로 전환된다는 점이 다르다 — 상세 프레임 파이프라인은
`video_capture/README.md`의 "A2 headless 경로"를 참조. `app/capture_service.py`가
이 경로에서 담당하는 것은 사이드카 fps 계산(물리 모드는 실제 렌더 fps로 데시메이션 후
값, 재연 모드는 `render_fps` 그대로)과 sim 클럭 온/오프(`core._use_sim_clock`)뿐이다.

### Physics 모드 전환 (`physics_service.set_physics_mode`)

1. `physics/ensure_physics_scene(stage)`로 `UsdPhysics.Scene`을 준비하고,
   `CAPTURE_MIN_FRAME_RATE=5`로 `/persistent/simulation/minFrameRate`를 낮춘다
   (캡처 부하에서도 물리가 substep을 더 돌려 elapsed 시간을 따라잡게 하기 위함 —
   `set_playback_mode` 복귀 시 원복).
2. **아레나(배회 영역) 범위를 4단계 우선순위로 결정한다**(`_profile_for_open_stage`와
   함께, "핵심 설계 결정" 절 참조): 명시 오버라이드
   (`core.set_coord_range_override`) > 열린 스테이지에 매칭되는 씬 프로파일
   (`automation/scene_profiles.py`) > 로드된 궤적 데이터의 좌표 범위
   (`repository.get_coord_range()`) > 하드코딩 기본값. 결정된 범위로
   `physics.create_bounding_box()`가 배회 영역 벽 5면을 만든다.
3. `core._prim_map`의 각 프림에 `physics.wrap_with_collision_proxy(..., shape="cylinder",
   visible=False)`로 원통 콜라이더를 씌운다(반지름, 접촉거리 규약의 상세는
   `physics/README.md` §1 참조).
4. 접촉 거리 두 값을 따로 계산해 core에 저장한다 — `collision_distance`(거리 기반
   fallback 탐지 전용, `2.2 × max(proxy_radii)`)와 `core._collision_distance`(라벨/
   observability 기록용 접촉 정의, `2.0 × max(proxy_radii)`). 이 둘은 서로 다른
   목적의 값이며 섞어 쓰면 안 된다(주석에 명시).
5. near-miss gap이 설정돼 있으면(`core._near_miss_gap > 0`) 그 값이 접촉거리(2r)의
   1.1배보다 크지 않으면 GT 오염 위험 경고를 로그로 남긴다.
6. `physics.WanderController`를 near-miss 조향, 다양성 파라미터(전부 `None`이면
   컨트롤러가 env → 코드 기본값 순으로 해결)와 함께 생성하고
   `core._wander`에 저장한다. **이 시점에는 아직 배회를 시작하지 않는다** — 사용자가
   GUI의 Move 버튼(`core.start_wander()`)을 눌러야 실제로 움직인다.
7. `omni.timeline`을 play 상태로 만든다(물리 스텝이 진행되려면 타임라인이 돌아야 함).

이 7단계가 끝난 직후 뷰포트에는 배회 영역 벽(2단계)과 우주인마다 씌운 원통
콜라이더(3단계)가 이미 보이고, 여기에 사용자가 Move 버튼(`core.start_wander()`)을
누르면 실제로 움직이는 궤적까지 겹쳐진다 — 그림 1이 이 상태의 예시다.

<!-- 이미지 TODO | 파일: images/readme/app-01-physics-wander.png | 촬영: Kit 에디터에서 Physics 모드로 전환한 뒤 Move 버튼을 눌러 우주인들이 배회 영역 안에서 움직이는 도중의 뷰포트를 캡처. 배회 영역 벽 5면과 각 우주인을 감싼 원통 콜라이더가 함께 보이게(콜라이더는 기본 visible=False이므로 육안 확인용으로만 잠시 visible=True로 바꿔 찍거나, 벽만으로도 아레나 범위는 확인 가능) | 형식: png -->
![그림 1. Physics 모드에서 배회 중인 뷰포트](../../../../images/readme/app-01-physics-wander.png)
*그림 1. Physics 모드 전환 후 Move로 구동 중인 뷰포트. 벽으로 둘러싸인 배회 영역과 그 안에서 서로 다른 방향으로 움직이는 우주인들을 볼 수 있다.*

`set_playback_mode`는 역순으로 정리한다: `_wander.stop()` → 충돌 recorder 정지 →
minFrameRate 원복 → 모든 프림 `physics.unwrap()` → `/World/PhysicsWalls` 프림 제거 →
`core._playback.set_mode("playback")` → `core.update_stage_objects()`로 궤적 재생
좌표로 되돌림.

### 객체 생성 경로 3종 (`object_service.py`)

- `auto_generate_astronauts` — config의 데이터 파일에서 objid 목록을 직접 파싱해
  프림을 만든다(`load_config`의 `auto_generate: true` 옵션에서 확장 기동 시 호출).
- `regenerate_astronauts_from_loaded_data` — 이미 로드된 `core._repository`에서
  objid 목록(`get_object_ids()` 또는 최초 시각 스냅샷)을 가져와 프림을 다시 만든다.
  데이터 소스 전환(`data_service`)이 매번 이 경로를 쓴다. 프림 인덱스는 objid의
  숫자 suffix를 우선 재사용한다(`_prim_index_for`) — enumerate 순번을 쓰면 objid가
  불연속(예: obj001, obj003만 존재)일 때 화면 라벨이 다른 ID로 어긋나기 때문이다.
- `spawn_objects` / `add_synthetic_objects` — 씬 프로파일 배치 경로 전용. 데이터
  로드 없이 개수만 받아 obj001..objN 프림 풀을 만든다(`spawn_objects`가 먼저
  기존 객체를 걷어내고 `add_synthetic_objects`의 검증된 스폰 로직을 재사용). 생성
  직후 위치가 전부 원점이므로 호출부가 반드시 `random_positions`로 즉시 재배치해야
  한다(그러지 않으면 원점에서 겹친 채 물리가 켜져 서로를 튕겨내는 사고가 실측된 바 있다).

`set_active_objects(objids)`는 위 세 경로로 만든 프림 집합 중 일부만 활성화하고
나머지는 숨긴다 — 반드시 `set_physics_mode` **이전**에 호출해야 활성 부분집합만
강체, 콜라이더, 오버레이 라벨, 충돌 기록의 대상이 된다(에피소드마다 객체 수 4~6개로
바꾸는 용도).

## 핵심 설계 결정과 규약

### 1. 데이터 리더는 URI 모양이 정한다(버튼이 정하지 않는다)

`data_service._repo_factory_for`의 docstring이 이유를 명시한다: 만약 "local/lake
버튼"이 리더 종류를 고정하면, 같은 청크 데이터셋을 저장소만 바꿔서(원격 s3 ↔ 로컬
미러) 읽는 비교가 불가능해진다. 그런데 그 비교(같은 리더, 캐시, 프리페치 로직으로
경로만 다르게)가 "지연이 minIO 경유 자체 때문인가"를 가르는 유일한 대조군이다.
그래서 리더 선택은 버튼이 아니라 로드하려는 URI가 `manifest.json`으로 끝나는지로
정해진다.

### 2. Physics 아레나 결정은 "씬의 물리적 사실"을 데이터보다 우선한다

`physics_service._profile_for_open_stage`의 주석에 실측 경위가 남아 있다: GUI에는
헤드리스 생성 잡과 달리 아레나 프로파일을 명시해 주는 경로가 없어서, 한때 배회
영역의 벽이 "로드된 좌표 데이터의 범위"로만 만들어졌고, 그 결과 2026-08-17 실측에서
레이크 데이터셋 범위(South z=-2819.361)로 벽이 잘못 생성되는 사고가 있었다. 아레나는
씬 자체의 물리적 사실(방 크기)이어야 하는데 데이터가 그것을 결정해 버린 것이다.
수정된 우선순위(명시 오버라이드 > 열린 스테이지의 씬 프로파일 > 궤적 데이터 > 기본값)는
씬에 맞는 프로파일이 있으면 항상 그것을 먼저 쓰도록 한다. 어느 소스로 아레나가
결정됐는지는 headless job 로그의 `[Physics] arena source=...` 스탬프로 사후 검증할
수 있다(콜라이더, 마커, trace 규약 스탬프와 같은 취지 — CLAUDE.md의 "운영 수칙" 참조).

### 3. 충돌거리 두 값의 용도가 다르다

`set_physics_mode`가 계산하는 `collision_distance`(지역 변수, `WanderController`의
거리 기반 fallback 탐지에만 쓰임, 2.2r)와 `core._collision_distance`(사이드카, 오프라인
recall 분석용 라벨 정의, 2.0r)는 이름이 비슷하지만 다른 목적이다. contact report가
기본 탐지 수단이므로 전자는 `use_contact_reports=False`일 때만 실제로 쓰이고, 후자는
항상 사이드카에 기록돼 "GT가 어떤 접촉 정의로 만들어졌는가"의 근거가 된다.

### 4. 캡처 창 == 충돌 기록 창, Move와 Trace는 서로 독립

물리 모드에서 충돌 CSV(`CollisionRecorder`)는 **Capture 버튼의 시작/종료에 결속**돼
있다(`start_capture`/`_worker` finally에서 시작, 정지) — Move 버튼만 눌러 객체를
움직여도 충돌은 기록되지 않는다. 반면 좌표 궤적 CSV(`TraceRecorder`)는 Trace
버튼으로 독립 제어된다. 이 비대칭은 의도적이다 — "영상에 담긴 시간 구간의 충돌만
GT로 남긴다"는 것이 캡처-충돌 결속의 목적이고, 궤적 기록은 영상 캡처와 무관하게
디버깅, 검증용으로 쓰일 수 있기 때문이다.

### 5. `.env`는 config.json과 같은 디렉터리에서, config 필드보다 먼저 로드된다

`ExtensionConfig.from_file`은 JSON을 파싱하기 **전에** `_load_dotenv(config_dir/.env)`를
호출해 `.env`의 `KEY=VALUE` 줄들을 `os.environ`에 먼저 채운다. 그다음 JSON의 문자열
필드(`data_path`, `lake.*` 등)에서 `${VAR}` 패턴을 `_expand_env`로 치환한다 — 즉
우선순위는 **`.env` 파일 → config.json의 `${VAR}` 참조 → 이미 설정된 OS 환경변수**
순이며, `.env`가 이미 설정된 `os.environ` 값을 덮어쓴다(`_load_dotenv`가 조건 없이
`os.environ[key] = value`). 이 파일은 config가 IP, 자격증명 같은 값을 코드/JSON에
직접 박지 않게 하기 위한 것이다.

### 6. 서비스는 상태를 갖지 않는다(테스트, shutdown 안전성의 전제)

위 "역할과 위치"에서 설명한 분해 원칙의 실질적 결과: 모든 서비스 함수는 `core`를
첫 인자로 받는 순수 함수 스타일이고, 서비스 모듈 자신은 `import` 시점 이후 아무
전역 상태도 쌓지 않는다. 이 덕분에 `tests/test_gap_skip.py`,
`tests/test_output_routing.py` 같은 테스트가 `TimeTravelCore.__new__(TimeTravelCore)`로
`__init__`을 건너뛰고 필요한 속성만 채운 최소 core로 서비스 함수를 직접 호출해
검증할 수 있다.

## 사용법

`app/`는 Kit 확장 안에서 조립되는 코드라 대부분의 서비스 함수는 `omni.usd`/`carb`에
의존하지만, `TimeTravelCore`의 공개 API 자체는 (Kit이 기동돼 있다는 전제 하에)
독립적으로 조작 가능하다.

```python
from gist.netai.time_travel_summarization.app.facade import TimeTravelCore

core = TimeTravelCore()
core.load_config("config.json")     # -> data_service.load_config
core.load_data()                    # -> data_service.load_data -> activate_data_source

core.set_current_time(some_datetime)
core.set_progress(0.5)

core.set_physics_mode()             # -> physics_service.set_physics_mode
core.start_wander()
core.start_trace()
core.start_capture(duration_s=30.0) # 물리 모드면 충돌 recorder도 함께 시작

# headless 자동화(automation/generate_episodes.py 등)가 쓰는 경로:
core.run_capture_headless(
    duration_s=30.0,
    camera_path="/World/summarization_camera",
    render_fps=30,
)
```

Lookup 벤치마크(`benchmark_service.py`)는 GUI 노출이 없는 콘솔 전용 도구다 — Kit
Script Editor에서 직접 호출한다.

```python
core.run_lookup_benchmark_suite(duration_s=5.0, fps=60)
# bisect/hybrid/invalidate × forward/backward 6개 조합을 자동 측정해
# artifacts/benchmarks/lookup_runtime_benchmark.csv에 CSV로 추가한다.
```

`LakeProbe`는 보통 GUI의 Probe 섹션(`ui/README.md` 참조)이나
`TTS_LAKE_PROBE=1` 환경변수로 켜지만, 직접 조작할 수도 있다.

```python
core.set_lake_probe_enabled(True)
# ... 재생 ...
probe = core.get_lake_probe()
print(probe.live_stats())    # {"frames", "stalls", "fps", "scenario"}
probe.dump(reason="manual")  # artifacts/benchmarks/gui_probe_<timestamp>[_<scenario>].json
```

## 테스트

`omni`/`carb` 없이 도는 헤드리스 단위 테스트가 `tests/`에 있다.
`tests/conftest.py::install_carb_stub()`이 `carb.log_*`를 no-op으로 스텁하고(옵션으로
`playback.stage_object_controller.StageObjectController`도 최소 스텁으로 대체), 각
테스트 파일이 이를 **호출 시점에만** 적용해(autouse 아님) 다른 테스트로 오염이 새지
않게 한다.

- `tests/test_smoke.py` — `ExtensionConfig.from_file`이 현재 config.json 형태를 읽는지,
  `data_uri`가 URI로 해석되는지(파일 종류를 못 박지 않고 확인).
- `tests/test_repository_uri.py` — `ExtensionConfig` 기반 URI 해석과
  `TrajectoryRepository`의 다양한 입력 형식 호환(pyarrow 설치 여부에 따라 조건부 skip).
- `tests/test_gap_skip.py` — `TimeTravelCore._maybe_skip_gap`(재생 중 데이터 공백을
  건너뛰는 로직)을 두 세그먼트 사이 하루 공백이 있는 합성 리포지토리로 검증.
- `tests/test_output_routing.py` — `TimeTravelCore.__new__` + 속성 주입으로 만든 최소
  core에서 `get_output_root_uri_for_active_mode` 등이 데이터 소스(local/lake)에 따라
  올바른 URI(또는 `None`)를 반환하는지 검증.
- `tests/test_object_regen_ids.py` — `object_service._prim_index_for`의 objid 숫자
  suffix 우선 재사용 규칙(중간 등장 트랙, 불연속 objid 등, frag 조건 "투명 충돌"
  회귀 방지).
- `tests/test_lake_perf_tools.py` — `LakeProbe`의 링버퍼 상한, 덤프 트리거(정지 전이/
  상한 도달), idle 전용 버퍼 억제 로직을 오프라인(파일 I/O만, 네트워크 무관)으로
  검증. `sanitize_scenario`의 파일명 안전화 규칙(선행 대시는 보존, 후행 대시만 제거 —
  역방향 재생 라벨 `-1x`의 부호를 지키기 위함)도 여기서 고정된다.

`physics_service.py`, `capture_service.py`처럼 `omni.usd`/`physics`/`video_capture`를
직접 import하는 함수들은 이 방식으로 헤드리스 검증할 수 없다 — 해당 로직의 하위
동작(콜라이더 반지름, 캡처 프레임 파이프라인 등)은 `physics/`와 `video_capture/`
자체의 테스트가 커버한다.

## 한계와 주의

- **`app/`의 서비스 함수 중 상당수는 Kit 런타임(omni.usd, physics API) 전제다.**
  `physics_service.py`와 `capture_service.py`의 핵심 경로는 활성 스테이지가 있어야
  동작하며, 헤드리스 단위 테스트로 직접 검증되지 않는다(위 "테스트" 절 참조).
- **near-miss, 아레나 프로파일 관련 파라미터는 데이터 생성 규약에 영향을 준다.**
  `_near_miss_gap`, `_collision_distance` 산출식, 아레나 우선순위는 물리 데이터
  생성의 접촉 정의를 바꾸는 값이므로, 바꾸면 이미 생성된 학습 데이터와 규약이
  어긋난다(`physics/README.md`의 "한계와 주의"와 같은 경고).
- **`benchmark_service.py`는 GUI에 노출되지 않는다.** `TimeTravelCore.start_lookup_benchmark`
  등은 공개 메서드지만 어떤 창도 버튼으로 연결하지 않았다 — 콘솔(Kit Script Editor)에서
  직접 호출하는 개발자 도구로 남아 있다.
- **`config.py`의 `_load_dotenv`는 이미 설정된 OS 환경변수를 무조건 덮어쓴다.** 셸에서
  미리 export한 값을 유지하고 싶다면 `.env` 파일에 같은 키를 넣지 않아야 한다.
