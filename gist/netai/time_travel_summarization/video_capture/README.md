# video_capture — 뷰포트를 mp4로 캡처하는 두 경로(A1 baseline / A2 realtime)와 그 인코딩 파이프라인

## 역할과 위치

이 디렉터리는 Omniverse Kit 뷰포트(또는 headless render product)의 프레임을 mp4로
만드는 부분을 담당한다. `gist/netai/time_travel_summarization/video_capture/`에 있고,
호출부는 `app/capture_service.py`(GUI 캡처 시작/중단, headless 배치 캡처, 사이드카
(sidecar — 캡처 영상 옆에 같은 이름으로 남기는 메타 JSON) 작성)와
`app/benchmark_service.py`(A1/A2 성능 비교) 두 곳이다. 이 패키지 자체는 상태를
갖지 않는다 — 실행 상태(`_capture_active` 등)는 `app.facade.TimeTravelCore`가 들고,
여기 클래스들은 한 번의 캡처 요청(`CaptureRequest`)을 받아 한 번의 결과
(`CaptureResult`)를 리턴하는 함수형 실행기다.

캡처 경로는 두 가지이며, 차이는 다음과 같다.

| | A1 (`movie_capture.py`) | A2 (`realtime_capture.py`) |
|---|---|---|
| 구현 | Omniverse 내장 Movie Capture 확장(`omni.kit.capture.viewport`)을 감싼 래퍼 | viewport 텍스처를 직접 읽는 이 프로젝트 자체 구현 |
| 오버레이 합성 | 없음 | `OverlayComposer`로 합성 |
| 프레임 순서 제어 | 없음 | 있음(요청 순서로 재정렬) |
| 실행 형태 | GUI 전용 | GUI(`capture()`, 활성 viewport 필요) 또는 headless(`capture_headless()`, render product로 오프스크린 렌더) |
| 용도 | A1 vs A2 성능 비교의 베이스라인 | 실제 프로덕션 경로 — 학습/추론용 영상은 전부 이 경로로 만든다 |

## 구성 파일

| 파일 | 책임 | 핵심 심볼 |
|---|---|---|
| `types.py` | 캡처 요청/결과 데이터클래스 | `CaptureRequest`(duration_s, fps=30, width=720, height=480, output_uri, label, render_fps), `CaptureResult`(success, output_uri, wall_clock_s, output_size_bytes, sim_fps_avg, dropped_frames, error, metadata) |
| `movie_capture.py` | A1 베이스라인 — Kit Movie Capture 래핑 | `MovieCaptureRunner.capture(req)` |
| `realtime_capture.py` | A2 — viewport/headless 캡처, 오버레이 프로바이더, 프레임 재정렬, 결정론 클럭 | `RealtimeCaptureRunner.capture()` / `.capture_headless()`, `_default_provider_from_core()` |
| `frame_queue.py` | 캡처 스레드 ↔ 인코더 스레드 사이의 경계 큐 | `FrameQueue.push(item, timeout=None)` / `.pop(timeout=None)` |
| `encoder.py` | 백그라운드 스레드 H.264 인코더(imageio 우선, ffmpeg 서브프로세스 폴백) | `FrameEncoder.start(queue)`, `EncoderError` |
| `overlay_composer.py` | 캡처 프레임에 PIL로 타임스탬프/ID 마커를 굽는 합성기 | `OverlayComposer.compose()`, `TextItem`, `CircleLabel`, `OverlayFrame`, 상수 `MARKER_UP_OFFSET`/`MARKER_RADIUS_PX` |
| `benchmark.py` | A1 vs A2 반복 측정 후 JSON 리포트 저장 | `run(duration_s=60.0, repeat=3, ...)` |
| `__init__.py` | 위 심볼들의 공개 재export | — |

## 동작 흐름

### A2 GUI 경로 — `RealtimeCaptureRunner.capture()`

1. `app/capture_service.py`의 `start_capture()`가 `CaptureRequest`를 만들고 백그라운드
   스레드에서 `runner.capture(req, stop_event)`를 호출한다(캡처 자체는 이 스레드
   안에서 동기 실행되고, `stop_event`로 외부에서 중단 신호를 준다).
2. `capture()`는 활성 viewport를 찾아 캡처 해상도로 `set_texture_resolution`을
   바꾸고(끝나면 원래 해상도로 복원), `OverlayComposer`와 오버레이 프로바이더를
   준비한 뒤 `FrameEncoder`를 별도 스레드로 시작한다.
3. 목표 프레임 수(`duration_s * fps`)만큼 `omni.kit.viewport.utility.capture_viewport_to_buffer`를
   호출한다. 이 API는 프레임마다 콜백을 비동기로 실행하므로 완료 순서가 요청 순서와
   다를 수 있다 — `completed_frames` 딕셔너리 + `next_to_encode` 커서로 순서를
   재조립한 뒤(reorder buffer) `FrameQueue`에 넣는다.
4. 콜백이 실패하거나(`failed_frames`) 재정렬 대기가 `reorder_wait_s`를 넘기면
   직전 프레임을 복제해 채운다(빈 구간 없이 목표 프레임 수를 채우는 정책).
5. `FrameQueue.push`가 `timeout=10.0`으로 막히면(인코더 스레드가 죽어 아무도 큐를
   비우지 않는 상태) 캡처를 즉시 중단한다 — 인코더 사망으로 인한 무한 동결 방지.
6. 모든 프레임을 큐에 넣은 뒤 `queue.close()` → `encoder.join(30.0)`으로 인코딩
   완료를 기다리고, 결과 mp4를 `storage.from_uri(...).put_file()`로 `output_uri`에
   저장한다(로컬 파일이든 s3 등 원격이든 동일 인터페이스).

### A2 headless 경로 — `RealtimeCaptureRunner.capture_headless()`

viewport가 없는 자동화 잡(episode 생성, 재연 렌더)에서 쓴다. 흐름은 GUI 경로와
비슷하지만 프레임 소스와 클럭 제어가 다르다.

1. `omni.replicator.core`로 `/World/summarization_camera`(기본, `camera_path`로 override
   가능)에 render product를 만들고 `LdrColor` annotator를 붙여 프레임을 읽는다.
   가능하면 `camera_params` annotator도 붙여 렌더러가 실제 사용한 view/projection
   행렬을 매 프레임 받는다 — 오버레이 마커를 재구성 행렬이 아니라 이 실측 행렬로
   투영하면 라벨이 객체에서 밀리는 오차가 원천적으로 없다.
2. **결정론 클럭 정합**: `_configure_deterministic_clock()`이 fixed timestepping을
   켜서 `app.update()` 1회 = 1/fps sim 전진 = 프레임 1개가 되게 맞춘다(렌더 부하와
   무관하게 프레임당 동일한 물리 전진 보장). 프레임 전진은 `omni.replicator.core.orchestrator.step_async`가
   표준 경로이고(동기 `step()`은 Kit 내부에서 금지돼 있어 코루틴을 걸고
   `app.update()`로 펌프하며 완료를 기다린다), 실패하면 `app.update()` 폴백으로
   내려간다.
3. **렌더 데시메이션**: sim은 항상 `req.fps`(고정 스텝, 물리 모드에서는 60Hz)로
   전진하되, `render_fps`가 주어지면 `_dec = round(fps / render_fps)` 스텝마다
   1회만 렌더, 인코딩한다(나머지 스텝은 `_phys_advance()`로 렌더를 생략한 물리 전진).
   렌더 비용이 프레임당 병목의 대부분을 차지한다는 실측 근거로, 데이터셋 fps를
   낮추면서도 물리 정확도(60Hz substep)는 유지하는 지렛대다. 라벨 시각은 스텝
   인덱스 기준이라 데시메이션과 무관하게 정합이 깨지지 않는다.
4. **재연(replay) 모드**: `replay_start_dt`가 주어지면 물리를 돌리는 대신 매
   프레임 `core.set_current_time(replay_start_dt + seq/vid_fps)`로 재생 헤드를
   데이터 시각에 직접 세팅해 객체를 그 시각 좌표로 배치한다. 물리 스텝도
   데시메이션도 없으므로 렌더 프레임과 sim 프레임이 1:1이다.
5. 워밍업 루프에서 annotator가 실제 픽셀을 리턴할 때까지 전진한다(렌더러 초기화가
   끝나기 전에 만든 render product는 `attach`가 안 붙어 계속 빈 배열을 준다 —
   감지되면 render product를 재생성).
6. 처음 `10 * _dec` 프레임은 프로브 구간으로, `ratio_t`(타임라인 실제 전진 /
   기대 전진)와 `ratio_d`(객체 실제 변위 / 기대 변위)를 로그에 남겨 클럭, 물리가
   정합한지 실측 검증한다.
7. 나머지는 GUI 경로와 동일하게 `FrameQueue` → `FrameEncoder` → `storage` 업로드로
   끝난다.

### 공통 — 인코더

`FrameEncoder.start(queue)`가 데몬 스레드를 띄우고, `imageio`가 있으면
`_run_imageio`(선호 경로), 없고 시스템에 `ffmpeg`가 있으면 `_run_subprocess`로
폴백한다. 둘 다 `queue.pop(timeout=2.0)`으로 프레임을 읽어 `libx264`,
`-crf 12 -preset slow -tune animation`, `yuv420p`로 인코딩한다(CRF 12는 사실상
무손실에 가까운 화질, `tune=animation`은 3D 렌더링/합성 영상에 최적화된 디블록
설정 — `encoder.py`의 인라인 주석 근거).

## 핵심 설계 결정과 규약

- **오버레이는 캡처 경로 전용 별도 시각 규약을 쓴다.** `overlay_composer.py`의
  `MARKER_UP_OFFSET=130.0`(스테이지 단위=cm, 발밑 원점 좌표를 몸 축 위로 들어올려
  BEV 투시에서 몸통이 마커 밖으로 삐져나오는 문제를 해소)과
  `MARKER_RADIUS_PX=9`는 GUI 쪽 `overlay/components.py`의 라벨 오프셋(145)과
  다르다 — ±15 이내 차이는 시점(캡처 카메라 vs 에디터 뷰)에 따른 지각 차이로
  허용한 값이다. 헤드리스로 렌더된 학습, 추론 영상은 전부 이 provider 경로를
  지나므로, 여기 상수가 곧 학습 데이터의 시각 규약이다. 어떤 규약으로 렌더됐는지는
  캡처 로그의 `marker regime: up_offset=... radius=...px` 줄로 확인할 수 있다
  (미커밋 코드로 렌더된 결과를 구분하기 위한 진단 로그). 이 규약이 실제 프레임에
  어떻게 나타나는지는 아래 그림 1과 같다.

<!-- 이미지 TODO | 파일: images/readme/capture-01-frame-markers.png | 촬영: 캡처된 mp4에서 프레임 1장 추출(720x480). 객체 ID 원형 마커 여러 개와 좌상단 시각 텍스트(HH:MM:SS)가 합성된 장면이 보여야 함 | 형식: png -->
![그림 1. 캡처 프레임 — ID 마커와 시각 텍스트가 합성된 상태](../../../../images/readme/capture-01-frame-markers.png)
*그림 1. `OverlayComposer`가 합성한 프레임. 원형 마커는 각 객체의 발밑 원점에서 `MARKER_UP_OFFSET=130.0`만큼 위로 들어올린 지점에 찍힌다.*
- **프레임 순서는 요청 순서로 보정한다.** `capture_viewport_to_buffer`의 콜백은
  비동기라 완료 순서가 요청 순서와 어긋날 수 있으므로, `realtime_capture.py`는
  `seq` 기반 재정렬 버퍼로 인코더에 넘기는 순서를 강제한다. 오버레이 스냅샷도
  콜백 완료 시점이 아니라 **요청 시점**의 상태에 묶는다(`overlay_snapshot`을 요청
  시 미리 캡처).
- **인코더 사망은 타임아웃으로 감지한다.** `FrameQueue.push(item, timeout=10.0)`이
  실패하면(무손실 모드에서 큐가 가득 찬 채 소비자가 죽음) 캡처를 즉시 중단한다.
  timeout 없이 대기하면 인코더 스레드가 죽었을 때 캡처 전체가 무한 동결된다는
  게 실측 사고였다(`frame_queue.py`의 docstring, `realtime_capture.py`의 인라인
  주석 근거).
- **결정론적 오프라인 캡처.** headless 경로는 벽시계 페이싱이 없다 —
  `app.update()` 1회를 정확히 `1/fps` sim 전진에 대응시켜, 렌더 부하가 프레임마다
  달라도 물리 결과가 항상 같은 시퀀스로 나오게 한다(재현성 확보, 슬로모션/드리프트
  제거).
- **캡처 요청 필드는 불변(frozen dataclass)이다.** `CaptureRequest`/`CaptureResult`
  모두 `@dataclass(frozen=True)` — 캡처 도중 요청을 변형하지 않는다는 계약이다.

## 사용법

### GUI 캡처 (호출부: `app/capture_service.py`)

```python
from gist.netai.time_travel_summarization.video_capture import CaptureRequest, RealtimeCaptureRunner

req = CaptureRequest(duration_s=60.0, fps=30, width=720, height=480,
                      output_uri="file:///path/to/out.mp4", label="ui_capture")
runner = RealtimeCaptureRunner(core=core)  # core: app.facade.TimeTravelCore
result = runner.capture(req, stop_event=stop_event)  # stop_event로 중간 중단 가능
```

### Headless 배치 (호출부: `app/capture_service.py::run_capture_headless`)

```python
req = CaptureRequest(duration_s=60.0, fps=60, output_uri=output_uri,
                      label="headless_capture", render_fps=30)  # 60Hz sim, 30fps 렌더
result = RealtimeCaptureRunner(core=core).capture_headless(
    req, camera_path="/World/summarization_camera", replay_start_dt=None)  # None=물리 모드
```

### A1 vs A2 벤치마크

Kit Script Editor에서 (`exec(open(...).read())`는 relative import가 깨지므로 금지,
반드시 `import` 경로로 호출):

```python
from gist.netai.time_travel_summarization.video_capture.benchmark import run
run(duration_s=60.0, repeat=3)  # 기본 background=True, 결과는
                                 # artifacts/benchmarks/capture_<timestamp>.json
```

`run_a1=False`로 A1을 건너뛰고(수동으로 Omniverse Movie Capture UI로 측정한 경우),
`with_overlay=False`로 오버레이 없는 A2만 측정할 수도 있다. 실제로 저장되는
리포트가 어떤 형태인지는 그림 2를 참고한다.

<!-- 이미지 TODO | 파일: images/readme/capture-02-a1-a2-benchmark.png | 촬영: `artifacts/benchmarks/capture_<timestamp>.json`을 텍스트 에디터로 연 화면, 또는 그 JSON의 A1/A2 수치(wall_clock_s, sim_fps_avg, dropped_frames 등)를 표/막대그래프로 정리한 캡처 | 형식: png -->
![그림 2. A1 vs A2 벤치마크 결과 — capture_<timestamp>.json](../../../../images/readme/capture-02-a1-a2-benchmark.png)
*그림 2. `run(duration_s=60.0, repeat=3)`이 반복 측정해 저장한 A1(Movie Capture)과 A2(RealtimeCaptureRunner)의 성능 비교 수치.*

## 테스트

`gist/netai/time_travel_summarization/tests/`에 headless(Kit 미기동) 환경에서 도는
단위 테스트가 있다. `omni.*`를 import하지 않으므로 일반 파이썬/`unittest`로 실행된다.

- `test_video_capture_types.py` — `CaptureRequest` 기본값(해상도 720x480, fps 30),
  `CaptureResult` 실패 기본값, `MovieCaptureRunner` import 가능 여부.
- `test_frame_queue.py` — push/pop 순서, `drop_oldest=True`일 때 초과분 드롭,
  `drop_oldest=False`일 때 공간이 날 때까지 블로킹, `close()` 후 pop이 `None`으로
  풀리는 것, close 후 push가 no-op인 것.
- `test_encoder.py` — `omni`가 import되지 않은 채로 모듈이 로드되는지(headless
  임포터블리티), imageio 백엔드 선택(설치돼 있을 때만), carb 미설치 환경에서의
  폴백 동작.
- `test_overlay_composer.py` — PIL이 없는 환경에서 `compose()`가 원본 bytes를
  그대로 리턴하는지, PIL이 있을 때 텍스트를 그리면 픽셀이 실제로 바뀌는지.
- `test_realtime_projection.py` — `_apply_radial_overlay_scale()`의 순수 함수
  단위 테스트(scale=1.0이면 항등, 1.2면 중심에서 바깥으로 밀림).

## 한계와 주의

- **A1(`movie_capture.py`)은 벤치마크 베이스라인일 뿐, 오버레이 합성이나 프레임
  재정렬 로직이 없다.** 학습, 추론용 실데이터는 전부 A2 경로(`RealtimeCaptureRunner`)로
  만들어진다.
- **headless 경로(`capture_headless`)는 GPU + `omni.replicator.core`가 있는 환경
  전제다.** `realtime_capture.py`의 docstring에도 "verify on hardware"로 명시돼
  있다.
- **`_default_provider_from_core`의 카메라 행렬 재구성 경로(`_get_camera_matrices`)는
  headless의 `camera_params` annotator가 실패했을 때만 쓰이는 폴백**이며, USD
  카메라 aperture를 viewport 종횡비에 맞춰 override하는 보정을 거친다 — 그래도
  실측 카메라 행렬(annotator 경로)보다 오차 가능성이 크다.
- **`encoder.py`의 CRF 12/preset slow/tune animation은 원본 에피소드 mp4의 화질을
  위한 값이며, VLM 학습, 추론에 들어가는 2초 청크의 인코딩 옵션이 아니다.** 학습, 추론
  청크는 이 원본을 `utils/build_dataset.py::slice_clip`과 `utils/vllm_client.py::VLLMClient._encode_chunk`가
  각각 별도로 재인코딩해서 만든다(둘 다 `-an -c:v libx264 -pix_fmt yuv420p -preset veryfast`,
  프레임 정확한 경계를 위한 재인코딩). 이 두 곳의 옵션이 같아야 한다는 것이 학습, 추론
  입력 분포를 맞추는 계약이다 — `vllm_client.py`의 `_encode_chunk` docstring이
  "인코딩 파라미터는 build_dataset.slice_clip과 동일(재인코딩 — 프레임 정확한
  경계)"라고 명시하고, 모듈 상단 주석도 "슬라이스 규칙(재인코딩, 경계)은 학습 데이터
  빌더(build_dataset.slice_clip)와 동일하게 맞춘다 — 학습 클립과 추론 클립이 같은
  분포여야 LoRA 성능이 이전된다"고 근거를 밝히고 있다. 이 계약은 이 디렉터리
  (`video_capture/`) 밖의 `utils/`에 있으므로, video_capture는 원본 인코딩만 책임지고
  청크 재인코딩 정합은 `utils/`가 책임진다는 경계로 이해하면 된다.
