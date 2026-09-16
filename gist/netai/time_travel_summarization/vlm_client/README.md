# vlm_client — vLLM(OpenAI 호환) 직결 VLM 추론 클라이언트와 GUI 창

## 역할과 위치

`gist/netai/time_travel_summarization/vlm_client/`는 캡처된 BEV 디지털 트윈 영상을
vLLM으로 서빙되는 Vision-Language Model(VLM)에 보내 "언제 어떤 객체들이 겹쳤는가"를
JSON으로 받아오는 추론 경로다. 호출부는 `extension.py`(`on_startup`)뿐이며,
`VLMClientCore`를 만들고 그것을 감싸는 `VLMClientWindow`를 붙인다. 캡처 완료 →
Source 필드 자동 채움, 추론 완료 → Event Post Processing 입력 자동 채움이라는 두
릴레이 콜백도 `extension.py`가 여기서 배선한다.

**백엔드는 vLLM OpenAI 호환 직접 호출(`VLM_API=openai`) 하나만 지원한다.** 과거
서버측에서 업로드→청크 분할→VLM 호출을 대신하던 VSS(VIA) 경유 백엔드는 제거됐고,
지금은 클라이언트(`utils/vllm_client.py::VLLMClient`)가 영상을 직접 2초 청크로 잘라
`POST {VLM_BASE_URL}/v1/chat/completions`로 보낸다. 청크 길이 2초, 겹침 0초는 학습
클립 길이와 같게 맞춘 값이라 **train == infer 정합**이 유지된다.

`vlm_client/`가 소유하는 상태는 "현재 선택된 비디오 하나"뿐이다(`_current_video_id`,
`_current_video_path` 등) — 결과 저장 경로나 이벤트 인덱스 갱신 같은 부수효과는
`generate_captions()` 안에서 직접 처리하고, 별도 서비스 레이어로 분리돼 있지 않다.

## 구성 파일

| 파일 | 책임 | 핵심 심볼 |
|---|---|---|
| `core.py` | 비디오 선택(스테이징), 삭제, 추론 요청, 결과 저장, 이벤트 인덱스 적재 | `VLMClientCore` |
| `window.py` | VLM Client GUI 창(Source/Upload/Delete/Generate/Model, Preset/Status) | `VLMClientWindow`, `_shorten` |
| `prompts.py` | 추론과 학습 데이터 생성이 공유하는 프롬프트 프리셋(단일 진실 공급원) | `PROMPTS` |
| `__init__.py` | 패키지 설명 docstring만(재export 없음) | — |

`artifacts/`는 `app.paths.ExtensionPaths`가 만드는 로컬 산출물 디렉터리(`video/`,
`vlm_outputs/` 등)로, 이 패키지가 직접 정의하는 구조는 아니다.

## 동작 흐름

### 초기화 (`VLMClientCore.__init__` → `_initialize_client`)

1. `VLM_API` 환경변수를 읽는다 — `openai`가 아니면(과거 VSS 값 등) `ValueError`를 던지고
   `_client`를 `None`으로 남긴다(이후 모든 호출은 "Client not initialized" 에러로 실패).
2. `VLM_BASE_URL` 환경변수를 읽는다. 미설정이면 `http://localhost:38011`로 폴백하며
   `carb.log_warn`을 남긴다(IP를 코드에 박지 않는다는 원칙 — 주소는 항상 env로).
3. `prompts.PROMPTS` 딕셔너리 원본을 그대로 `utils.vllm_client.VLLMClient`에 전달한다
   (`default_chunk_duration=2.0`). 프리셋을 복사, 가공하지 않고 원본 dict를 넘기는 이유는
   학습 데이터 빌더(`utils/build_dataset.py`)가 참조하는 문자열과 완전히 같아야
   하기 때문이다.

### Upload → Generate (GUI 경로, `VLMClientWindow`)

1. 사용자가 Source 필드에 파일명(로컬 `videos/` 기준 상대 경로) 또는 URI(`s3://`,
   `file://`)를 입력하고 **Upload**를 누르면 별도 스레드에서
   `VLMClientCore.upload_video(video_source)`가 실행된다.
   - `storage.normalize_source()`로 minIO 콘솔에서 복사한 `버킷/키` 형태 입력에
     `s3://`를 붙여 정규화한다.
   - direct(vLLM) 모드에는 서버 업로드 개념이 없으므로, 실제로는
     `_stage_video_direct()`가 **분석에 쓸 로컬 경로를 확보**하는 일만 한다. URI
     입력이면 `storage.from_uri()`로 임시 파일에 내려받고(`_staged_tmp_path`), 로컬
     파일명이면 `videos_dir` 아래에서 존재 여부만 확인한다. 결과 완료는 메인
     스레드(omni.ui가 위젯 조작을 허용하는 유일한 스레드) 디스패처
     (`ui.task_dispatcher.UiTaskDispatcher`)를 통해 UI에 반영된다.
2. **Generate**를 누르면 `generate_captions(model, preset_name, video_filename,
   output_root_uri)`가 실행된다.
   - `output_root_uri`는 `extension.get_active_core().get_output_root_uri_for_active_mode()`로
     조회한다 — Data Lake(데이터 레이크, minIO에 적재된 데이터셋을 쓰는 모드)일 때만
     값이 채워지고, 로컬 모드에서는 `None`이다.
   - `VLLMClient.analyze_video(video_path, model, preset_name)`이 실제 vLLM 호출을
     수행하고 `{"chunk_responses": [...]}` 형태의 응답을 돌려준다.
   - 응답에 `video_source`(스테이징 전 원본 URI/경로)를 덧붙인다 — 결과 JSON의
     `video`는 임시 스테이징 파일명이라 그것만으로는 원본 사이드카(`.meta.json`)를
     역추적할 수 없기 때문이다. 이렇게 저장된 JSON 파일이 실제로 어떤 모양인지는
     그림 1에 있다.

<!-- 이미지 TODO | 파일: images/readme/vlm-01-inference-result-json.png | 촬영: Generate 성공 후 저장된 결과 JSON 파일(artifacts/vlm_outputs/ 아래 로컬 모드 파일, 또는 minIO 콘솔에서 연 vlm_outputs/*.json)을 텍스트 에디터로 열어 캡처. "video", "chunk_responses"(각 항목의 "chunk_idx", "start_s", "content" 또는 "error"), "video_source" 키가 다 보이게 스크롤 위치를 잡을 것 | 형식: png -->
![그림 1. 추론 결과 JSON 실물](../../../../images/readme/vlm-01-inference-result-json.png)
*그림 1. `generate_captions`가 저장한 결과 JSON. `chunk_responses` 배열의 각 원소가 청크 하나(`chunk_idx`, `start_s`, VLM 응답 `content` 또는 `error`)에 대응하고, 최상위 `video_source`가 원본 영상 URI를 보존한다.*
   - `output_root_uri`가 있으면(Data Lake) 결과를 `{root}/vlm_outputs/{filename}.json`으로
     `storage.from_uri().put_bytes()`로 저장하고, **best-effort**로
     `events.event_index.append_index()`를 호출해 이벤트 인덱스(시간축 검색용)에도
     적재한다. 이때 원본 영상 옆 사이드카에서 `capture_start`를 읽어 절대 시각
     앵커(`events.event_index.sidecar_anchor`)를 복원한다 — 사이드카가 없으면
     `anchor=None`으로 `time_hms`만 기록한다. 인덱스 적재가 실패해도 추론 자체는
     성공으로 유지한다.
   - `output_root_uri`가 없으면(로컬 모드) `_outputs_base_path`(`artifacts/vlm_outputs/`)에
     로컬 파일로 저장한다.
   - 반환하는 `output_filename`은 Data Lake 모드에서는 **전체 URI**다(단순 파일명이
     아님) — Event Post Processing이 파일명만으로는 lake 산출물을 찾을 수 없기
     때문에 바뀐 계약이다.

### 결과 릴레이 (`extension.py`가 배선)

```
capture_service 캡처 완료 --(URI)--> VLMClientWindow.set_source_uri()  → Source 필드 자동 채움
VLMClientWindow.generate_captions 성공 --(output_filename)--> EventProcessingWindow.set_source_json()
```

두 콜백 모두 사용자가 URI/파일명을 수동으로 복사, 붙여넣기 하던 과정을 없애기 위한
연결이며, `VLMClientWindow.set_generate_complete_callback()` / `set_source_uri()`가
그 등록, 적용 지점이다.

## 핵심 설계 결정과 규약

- **train == infer 프롬프트 정합.** `prompts.py`는 Omniverse/Kit 의존성이 전혀 없는
  순수 모듈이고, 추론 경로(`vlm_client/core.py`)와 학습 데이터 빌더
  (`utils/build_dataset.py`)가 이 파일의 `PROMPTS` 딕셔너리를 **같은 객체**로
  import한다 — 학습에 쓰는 프롬프트와 추론에 쓰는 프롬프트가 바이트 단위로 동일해야
  파인튜닝된 프롬프트→출력 매핑이 추론 시점에도 유효하다는 계약이 모듈 상단
  docstring(`prompts.py:1-8`)에 명시돼 있다. 프리셋은 `twin_view`(디지털 트윈 BEV
  영상 서술)와 `simple_view`(단순 흰 배경 원 서술) 두 가지이며, 각각
  `prompt`/`system_prompt` 키를 가진 딕셔너리다. 프롬프트 원문은 이 README에 옮기지
  않는다 — 정본은 `prompts.py`.
- **청크 인코딩 정합도 같은 계약의 다른 절반이다.** `PROMPTS`가 프롬프트 문자열의
  정합을 보장한다면, 영상 청크 자체의 인코딩 정합은 `utils/vllm_client.py`(추론
  청크)와 `utils/build_dataset.py`(학습 청크)가 담당한다 — 이 두 곳이 원본 영상에서
  독립적으로 재인코딩한 2초 청크의 ffmpeg 옵션이 같아야 학습, 추론 입력 분포가
  일치한다는 계약이며, 자세한 내용은 `video_capture/README.md`의 "한계와 주의"를
  참조. `vlm_client/`는 이 재인코딩을 직접 하지 않고 `VLLMClient`에 위임한다.
- **직접(vLLM) 모드에는 "업로드"가 없다.** GUI에는 Upload/Delete 버튼이 남아 있지만,
  vLLM OpenAI 호환 API는 요청마다 청크를 함께 실어 보내는 stateless 호출이라 서버에
  영상을 미리 올려 둘 개념이 없다. `upload_video`는 로컬 경로를 확보하는 것뿐이고
  `delete_video`도 서버에 아무것도 지우지 않으며 로컬 선택 상태와 스테이징 임시
  파일만 정리한다.
- **스테이징 임시 파일은 delete 시점에 정리한다(upload 직후가 아니다).** URI로 준 영상은
  `_stage_video_direct`가 임시 파일로 내려받아 `_staged_tmp_path`에 보관하고, `Generate`가
  이 로컬 경로를 분석에 쓴다. 정리(`_cleanup_staged`)는 `delete_video()` 또는 다음
  `upload_video()` 시작 시점에 일어난다 — Upload 직후 지우면 Generate가 쓸 파일이
  없어진다.
- **결과 반환값의 형태가 모드에 따라 다르다.** 로컬 모드는 파일명만, Data Lake
  모드는 전체 URI를 반환한다(2026-07-19 계약 변경, `core.py`, 테스트 주석에 근거).
  이 값을 그대로 다음 단계(Event Post Processing)에 넘기는 코드는 이 비대칭을
  알고 있어야 한다.
- **모델, 프리셋 목록은 GUI에 하드코딩돼 있다.** `window.py`의 `_on_generate_clicked`는
  `models = ["Qwen3-VL-8B-Instruct"]`, `presets = ["simple_view", "twin_view"]`
  리스트를 직접 갖고 있고, 콤보박스 인덱스로 선택한다. 모델명은 vLLM 기동 스크립트
  (`VLM_server/run_qwen3-vl-8b.sh`의 `--served-model-name`)와 반드시 일치해야
  한다 — VSS 시절의 다중 벤더 모델 목록(gpt-4o 등)은 direct 경로에서 의미가 없어
  제거됐다.

## 사용법

### GUI

VLM Client 창(폭 540)에서 Source에 파일명 또는 URI를 입력 → **Upload** → Model/Preset
선택 → **Generate**. Status 라인이 진행 상태(초록 `0xFF00AA00`=일반, 파랑 계열 `0xFFFFAA00`=처리중,
노랑 `0xFF00FFFF`=에러 — omni.ui 색상은 ABGR 순서라 `0xFF00FFFF`가 노랑이다)를 보여준다. 긴
URI는 `_shorten()`으로 중간 생략해 표시하고 전체 값은 tooltip으로 보존한다. 이 창의 실제
구획 배치와 Status 색상 전환은 그림 2로 확인한다.

<!-- 이미지 TODO | 파일: images/readme/vlm-02-client-window-flow.gif | 촬영: VLM Client 창에서 Source에 파일명/URI 입력 → Upload 클릭(Status가 처리중 색으로 바뀜) → 완료 후 Video ID가 채워짐 → Model/Preset 선택 → Generate 클릭(다시 처리중 색) → 완료 후 Status가 성공 메시지(초록)로 바뀌는 전체 흐름을 화면 녹화. 5~10초 내외 | 형식: gif -->
![그림 2. VLM Client 창의 Upload에서 Generate까지](../../../../images/readme/vlm-02-client-window-flow.gif)
*그림 2. Source 입력부터 Upload, Generate까지 이어지는 흐름과 그 사이 Status 라인의 색상 전환(대기/처리중/완료).*

### Python API

```python
from gist.netai.time_travel_summarization.vlm_client.core import VLMClientCore

core = VLMClientCore()  # VLM_BASE_URL, VLM_API=openai(기본값) 환경변수를 읽어 초기화
core.upload_video("s3://bucket/timetravel/video/clip_001.mp4")  # 또는 로컬 파일명

success, output = core.generate_captions(
    model="Qwen3-VL-8B-Instruct",
    preset_name="simple_view",       # 또는 "twin_view"
    video_filename="clip_001.mp4",
    output_root_uri="s3://bucket/timetravel",  # None이면 로컬 artifacts/vlm_outputs/
)
# output: 로컬 모드="{model}_{stem}_{timestamp}.json", Data Lake 모드=전체 URI
```

### 환경 변수

| 변수 | 기본값 | 의미 |
|---|---|---|
| `VLM_API` | `openai` | 이 값이 아니면 초기화 실패(VSS 백엔드는 제거됨) |
| `VLM_BASE_URL` | `http://localhost:38011`(경고와 함께) | vLLM OpenAI 호환 서버 주소 |

## 테스트

`tests/test_vlm_lake_upload.py`는 `carb`를 스텁으로 심고(`tests/conftest.py::install_carb_stub`)
`FakeVLLMClient`(`analyze_video`/`save_json`만 구현)로 실제 vLLM 서버 없이 아래를 검증한다.

- `file://` URI 업로드 시 임시 파일로 스테이징되고, `delete_video()`에서 정리되는지.
- `s3://` URI 업로드가 `storage.from_uri()` 어댑터(모의 객체, `monkeypatch`)를 거쳐
  스테이징되는지.
- 존재하지 않는 파일/URI 업로드가 `False`를 반환하는지.
- `output_root_uri`를 주면 결과가 **로컬이 아니라 lake 루트에만** 저장되고, 반환값이
  전체 URI이며, 이벤트 인덱스(`vlm_events/<video>.jsonl`)가 함께 적재되는지 — 사이드카가
  없는 상황이므로 `time=None`, `time_hms`만 채워지는 것까지 확인한다.

`tests/test_vlm_window_shorten.py`는 `omni.ui`/`omni.kit.app`/`carb`를 `setUpClass`에서만
스텁으로 심어(다른 테스트로 오염이 새지 않게 `tearDownClass`에서 원복) `window.py`의
순수 헬퍼 `_shorten()`만 검증한다(짧은 문자열 통과, 긴 URI의 앞/뒤 보존 축약, 경계
길이 처리).

## 한계와 주의

- **VSS(VIA) 백엔드는 완전히 제거됐다.** 코드, 주석에 이력은 남아 있지만
  `VLM_API`에 `openai` 외의 값을 주면 즉시 실패한다 — 되살릴 계획이 없는 한 다른 값을
  시도하지 않는다.
- **`generate_captions`의 `chunk_overlap_duration` 인자는 direct 모드에서 무시된다.**
  0이 아닌 값을 주면 `carb.log_warn`만 남기고 0으로 진행한다(`VLLMClient`가 겹침
  분할을 지원하지 않음).
- **모델의 실제 프레임 샘플링 수는 클라이언트가 바꿀 수 없다.** 학습 클립이 20프레임
  고정(`NFRAMES=20`)이므로 vLLM 서버도 기동 시
  `--media-io-kwargs '{"video": {"num_frames": 20}}'`로 맞춰져 있어야 하며, 이 값이
  어긋나면 `vlm_client/`에서는 감지할 방법이 없다(서빙 스크립트를 직접 확인해야 함).
- **이벤트 인덱스 적재 실패는 조용히 넘어간다.** `generate_captions`가 인덱스 쓰기를
  `try/except`로 감싸 경고만 남기므로, 추론 결과 JSON은 저장됐는데 이벤트 검색
  인덱스에는 안 잡히는 상황이 있을 수 있다 — 콘솔의
  `[VLMClient] event index write failed` 로그로 확인한다.
