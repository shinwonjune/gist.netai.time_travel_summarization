# Time Travel Summarization Extension

이 문서는 **Time Travel Summarization Framework**를 구현한 확장(Extension) **Time_travel_Summarization**의 사용 설명서입니다.
본 프레임워크는 Dream-AI_Plus_Twin.usd를 기반으로 구현되었으며 시계열 궤적 데이터를 활용하여 디지털트윈의 과거 상태를 복원하고, 이를 기반으로 **Event-based Summarization** (현재 '충돌' 이벤트 지원)을 생성합니다.

## 📝 Note: 용어 및 구조 정의

*   **Time Travel의 정의**:
    *   **본 문서:** 시간의 흐름에 따라 객체의 위치 상태를 복원하고 재생하는 기능
    *   **공식적(교수님) 관점:** 단순 복원을 넘어선 통합적인 시공간 분석 기능의 통칭 (본 프레임워크는 이를 지원하는 도구)

*   **확장 구조**:
    *   확장은 다양한 모듈로 구성되며 `extension.py`를 통해 통합 초기화
    *   모듈: Time Travel, View overlay, VLM Client, Event Post-Processing
    *   확장 외부의 VLM 서버를 통해 추론 (VLM Client가 중재)

## Extension 설치 가이드 (Local Installation)

1. **Extension 다운로드**
   이 저장소를 clone 한다. 저장소 루트가 곧 확장 디렉터리(`config/extension.toml`이 있는 위치)다.
   kit-app-template로 앱을 빌드한다면 `source/extensions/gist.netai.time_travel_summarization`
   위치에 두면 빌드 시 자동으로 포함된다.

2. **Local Path 추가** (이미 빌드된 USD Composer에 붙일 때)
   *    USD Composer에서: **Developer → Extensions → ☰ → Settings → Extension Search Path**
   *    이 저장소의 **상위 디렉터리**(확장 디렉터리들이 나열되는 폴더)의 전체 경로 추가
        *   예시: `C:\Users\<username>\workspace\kit-app-template\source\extensions`
   *    Third party 에서 Extension 실행


---

## 🚀 사용 가이드 (Workflow) 및 기능 설명

프레임워크의 작동 순서에 따른 단계별 사용법

### 0. VLM 서버 실행

GPU 서버에서 vLLM 컨테이너(OpenAI 호환 API)를 먼저 실행한다. GPU는 L40 또는 A100
40GB 한 대로 충분하다.

자세한 내용은 `./VLM_server` 디렉토리의 README.md를 참고한다.

---
### 1. 궤적 데이터 생성

디지털트윈 환경에서 객체의 움직임을 표현할 시계열 궤적 데이터를 생성

```bash
python tools/generate_living_trajectory.py --duration-hours 0.02 --output data/living_trajectory_1min_0.2s.csv --objects 4 --interval 0.2
```
좌표 범위는 스크립트 상단 상수(X_RANGE, Z_RANGE)로, 객체 수와 간격은 인자로 조정

---
### 2. Config 설정

`gist/netai/time_travel_summarization/config.json` 에서 다음 항목을 설정
*   **data_path**: 생성된 궤적 데이터(.csv) 경로 지정
*   **astronaut_usd**: Time Travel 객체로 사용할 USD 파일 경로 지정 (현재는 Astronaut USD 파일 사용 중)
*   **auto_generate**: `true` 이면 Extension 초기화시 time travel 객체 자동 생성 (data_path의 objectID 수 만큼 생성)
---
### 3. Extension Initialization

USD Composer 실행 후 `Extension` 창에서 **Time Travel Summarization**을 찾아 활성화. (Extension ID: `gist.netai.time_travel_summarization`)
*   실행 시 Time Travel, View Overlay, VLM Client, Event Post Processing 등 모든 모듈의 UI Window가 초기화

아래 그림이 활성화 직후 화면이다. 이후 4~10단계는 이 창들을 하나씩 오가며 조작한다.

<!-- 이미지 TODO | 파일: images/readme/guide-01-all-windows-init.png | 촬영: USD Composer에서 Extension을 막 활성화한 직후, Time Travel, View Overlay, VLM Client, Event Post Processing 4개 창이 모두 화면에 떠 있는 상태 전체를 캡처. 창이 겹쳐 있으면 서로 안 가리게 배치 후 촬영 | 형식: png -->
![그림 1. Extension 활성화 직후 초기화된 모듈 창들](../../../images/readme/guide-01-all-windows-init.png)
*그림 1. 활성화 직후 뜨는 4개 모듈 창. Time Travel, View Overlay, VLM Client, Event
Post Processing 창이 동시에 초기화된다.*
---
### 4. Time Travel

**기능:** 시계열 데이터를 기반으로 과거 상태를 재현하고 탐색
*   **자동 객체 생성**: 데이터 내 ID 개수만큼 객체(Astronaut) 생성 및 매핑
*   **Load Time Range / Load Range**: 재생 구간의 시작과 끝 시각을 입력하고 Load Range
    버튼을 누르면 그 구간으로 좁혀 시작점으로 이동(Data Lake 모드에서는 해당 구간의
    청크만 minIO에서 로드)
*   **Go to Time / Go**: 특정 Timestamp 시점으로 즉시 이동
*   **Twin Time**: 현재 재현된 디지털트윈의 시간 표시
*   **Play / Speed**: Play 버튼으로 재생 시작과 정지, Speed 필드로 배속 조절
*   **Timeline Slider**: 타임바를 통한 선형적 시점 조절

Time Travel Control 창 전체는 아래 그림과 같이 생겼다 — 위에서부터 데이터 소스,
시간 범위, 재생 제어, 타임라인 슬라이더 순으로 배치되어 있다.

<!-- 이미지 TODO | 파일: images/readme/guide-02-time-travel-window.png | 촬영: Time Travel Control 창 전체가 보이게, 데이터 로드 후 재생 중인 상태(Play를 눌러 슬라이더가 중간쯤 이동해 있고 Twin Time 필드에 시각이 표시된 상태). 창 폭 약 700px 기준으로 텍스트가 읽히게 | 형식: png -->
![그림 2. Time Travel Control 창 — 재생 중 상태](../../../images/readme/guide-02-time-travel-window.png)
*그림 2. Time Travel Control 창. 데이터 소스, 시간 범위, 재생 제어, 타임라인
슬라이더가 이 순서로 배치되어 있으며, 그림은 재생이 진행 중이라 슬라이더가 중간
지점에 있고 Twin Time에 현재 시각이 표시된 상태다.*

> **구현 파일:** `app/facade.py`(`TimeTravelCore`), `playback/controller.py`(`PlaybackController`), `ui/main_window.py`(`TimeTravelWindow`)
---
### 5. View Overlay

VLM이 이벤트 발생 시간과 연루된 객체를 특정할 수 있도록 하기 위한 visual prompting 과정

**기능:** 복원된 디지털트윈 장면 위에 객체 정보(ID)와 현재 시간(Timestamp)을 오버레이

**사용법:**  
*   Display Options의 **Object IDs**, **Timestamp** 체크박스 선택

체크박스를 켜면 뷰포트가 아래 그림처럼 바뀐다 — 각 객체 위에 ID 라벨이, 화면
한쪽에 현재 시각이 얹힌다. 이 오버레이는 사람이 보는 프리뷰 전용이며, VLM에게
실제로 전달되는 캡처 영상에는 별도 합성 경로(`video_capture/overlay_composer.py`)가
따로 굽는 마커가 들어간다는 점에 주의한다.

<!-- 이미지 TODO | 파일: images/readme/guide-03-view-overlay-viewport.png | 촬영: View Overlay Display Options에서 Object IDs와 Timestamp를 모두 체크한 상태로 뷰포트를 캡처. 최소 2개 이상 객체가 화면에 보이고, 각 객체 위에 ID 숫자 라벨이, 화면 한쪽에 Timestamp 텍스트가 보여야 함 | 형식: png -->
![그림 3. View Overlay가 켜진 뷰포트 — Object ID와 Timestamp 표시](../../../images/readme/guide-03-view-overlay-viewport.png)
*그림 3. Object IDs와 Timestamp를 켠 뷰포트. 각 객체 위의 숫자가 ID 라벨이고,
화면 한쪽의 텍스트가 현재 재생 시각이다.*

> **구현 파일:**
> *   `overlay/core.py`, `overlay/components.py`, `overlay/window.py`
---
### 6. Visual Abstraction & Temporal Acceleration (Optional)

VLM의 추론 성능을 극대화하기 위해 디지털트윈 환경을 조정하는 단계

#### Visual Abstraction (시각적 단순화)
*   **목적:** 불필요한 시각 정보를 줄여 VLM이 객체 상호작용에 집중하도록 함
*   **방법:** Omniverse Stage 창의 '눈(Eye)' 아이콘을 통해 Prim 그룹(Furniture, Equipment 등)을 비활성화
*   **단계 예시:** Full Digital Twin → Simplified (Equipment 제거) → Abstract (Equipment + A_Exterior 제거, View Overlay만 유지)

세 단계가 실제로 화면에서 어떻게 달라지는지는 문장보다 그림으로 보는 편이 빠르다.

<!-- 이미지 TODO | 파일: images/readme/guide-04-visual-abstraction-stages.png | 촬영: 같은 뷰포인트에서 Stage 창의 눈(Eye) 아이콘으로 Prim 그룹을 하나씩 끄면서 3장을 찍어 나란히 배치(3분할 비교 이미지 1장으로 합성): (1) Full Digital Twin, (2) Simplified(Equipment 제거), (3) Abstract(Equipment + A_Exterior 제거, View Overlay만 유지). 세 장 모두 같은 카메라 각도, 같은 시각으로 맞출 것 | 형식: png(3분할 비교 1장) -->
![그림 4. 시각적 단순화 3단계 비교 — Full / Simplified / Abstract](../../../images/readme/guide-04-visual-abstraction-stages.png)
*그림 4. 같은 장면을 세 단계로 단순화한 비교. 왼쪽부터 Full Digital Twin, Equipment를
제거한 Simplified, Equipment와 A_Exterior까지 제거하고 View Overlay만 남긴
Abstract다. 단계가 진행될수록 VLM이 봐야 할 배경 정보가 줄어든다.*

#### Temporal Acceleration (시간 가속)
*   **목적:** VLM에 전달되는 동영상의 재생 속도를 가속하여(영상 길이를 단축하여) VLM 처리 속도 향상
*   **경험적 성능:** '충돌' 이벤트 검출 시 **3배속** 영상까지는 추론 성능 저하가 없었음 (이벤트 특성에 따라 조절 필요)
*   시간 가속된 동영상 생성 방법은 "7. 동영상 추출" 에서 설명
---
### 7. 동영상 추출 (Capture)

메인 창의 **Capture** 버튼이 재생 중인 뷰포트를 그대로 mp4로 기록한다(내장 실시간 캡처 경로,
구현은 `video_capture/realtime_capture.py`). Kit의 Movie Capture 확장은 성능 비교용
베이스라인으로만 남아 있고 운영 흐름에서는 쓰지 않는다.
*현재 동영상 추출 단계가 파이프라인의 주요 병목구간*
*영상 전달을 스트리밍 방식으로 확장 필요 (현재는 파일 캡처 후 업로드하는 방식이라 캡처 완료까지 대기해야 함)*

#### 📸 캡쳐 가이드

*   **길이**: Capture 버튼 옆 길이 필드(초). 0이면 60초. 중간에 다시 누르면 중단
*   **해상도와 프레임레이트**: 720 x 480, 30 FPS (`video_capture/types.py`의 `CaptureRequest` 기본값)
*   **카메라**: 현재 뷰포트가 보고 있는 카메라. 객체 ID 마커와 시각 텍스트는 GUI 오버레이의
    표시 여부와 무관하게 캡처 경로가 재생 좌표에서 직접 합성해 넣는다(`video_capture/overlay_composer.py`)
*   **Output Path**: 로컬 모드는 확장 디렉터리의 `artifacts/video/video_<timestamp>.mp4`, 데이터
    레이크 모드는 `config.json`에 설정한 출력 URI 아래 같은 이름. 캡처 구간의 시작 시각과 길이는
    같은 이름의 사이드카 JSON에 함께 기록된다
*   **Output Name**: 자동 생성. 이후 VLM Client 단계에서 이 파일을 그대로 선택해 업로드

실제로 이렇게 캡처된 mp4에서 한 프레임을 꺼내 보면 아래 그림과 같다 — 화면에 보이던
View Overlay와는 별개로, 캡처 경로가 직접 구운 ID 마커와 시각 텍스트가 프레임에
박혀 있다.

<!-- 이미지 TODO | 파일: images/readme/guide-05-capture-output-frame.png | 촬영: Capture 버튼으로 캡처를 마친 mp4 파일을 아무 플레이어로 열어, 두 객체가 근접하거나 겹치는 순간의 프레임 1장을 스크린샷 또는 프레임 추출. 프레임 안에 ID 마커 숫자와 시각 텍스트가 보여야 함(720x480 원본 그대로) | 형식: png -->
![그림 5. 캡처된 mp4의 실제 프레임 — ID 마커와 시각 텍스트가 합성된 화면](../../../images/readme/guide-05-capture-output-frame.png)
*그림 5. 캡처 결과 mp4에서 뽑은 한 프레임. VLM에게 넘어가는 것은 이 화면이며, 사람이
보던 뷰포트의 View Overlay 설정과 무관하게 마커가 항상 합성되어 있다.*

#### 재생 속도와 시간 가속
실시간 캡처는 화면에 보이는 그대로를 기록하므로, **Time Travel 재생 배속이 곧 영상의 가속
배율**이다. 재생 배속 1.0x로 60초를 캡처하면 정속 60초 영상이, 배속 3.0x로 20초를 캡처하면
같은 구간을 3배속으로 담은 20초 영상이 생성된다.

| 목표 영상 속도 | 결과 영상 길이 | Capture 길이 | **Time Travel Play Speed** |
| :--- | :--- | :--- | :--- |
| **1배속 (정속)** | 60초 | 60 | **1.0x** |
| **3배속 (가속)** | 20초 | 20 | **3.0x** |
---
### 8. VLM Client

생성된 영상을 vLLM 서버(OpenAI 호환 API)로 전송 및 추론 결과를 수신  
VLM 서버에 동영상을 upload하고, 추론 요청(generate)하는 두 과정을 거침
VLM 서버 주소는 `VLM_BASE_URL` 환경변수로 설정(코드에 IP를 박지 않음; 세부 배선은 `vlm_client/core.py`의 `_initialize_client` 메서드 참조)

**기능:**
*   **Upload**: 생성한 영상 파일을 VLM 서버에 업로드
*   **Delete**: VLM 서버에 업로드한 영상 삭제(삭제 안하고 다른 영상 업로드해도 작동하긴 함)
*   **Generate**: VLM 모델 추론 요청
*   **Settings**:
    *   Model: VLM 서버에서 실행 중인 모델 선택
    *   Preset: Visual abtraction 정도에 따라 프롬프트 유형 선택 (`twin_view`: 입력된 동영상을 디지털트윈 BEV 영상으로 묘사, `simple_view`: 단순 도형의 움직임으로 묘사)
*   **결과**: `artifacts/vlm_outputs/` 경로에 JSON 형태로 저장됨

청크 겹침(overlap)은 GUI 설정 항목이 아니다 — direct(vLLM) 모드는 겹침 분할을 지원하지
않아 항상 0으로 고정된다(`vlm_client/README.md`의 "한계와 주의" 참조).

**사용법:**
*   Upload 버튼 (비디오 전송) -> Settings 확인 -> Generate 버튼 (추론 요청)

창 자체는 아래 그림처럼 업로드, 설정, 추론 요청 버튼이 한 화면에 모여 있다.

<!-- 이미지 TODO | 파일: images/readme/guide-06-vlm-client-window.png | 촬영: VLM Client 창 전체. 영상을 하나 Upload한 직후 상태(업로드된 비디오 ID가 표시된 상태)에서, Model과 Preset 드롭다운이 보이고 Generate 버튼이 눌리기 전인 화면 | 형식: png -->
![그림 6. VLM Client 창 — 업로드 완료, 추론 대기 상태](../../../images/readme/guide-06-vlm-client-window.png)
*그림 6. VLM Client 창. 위에서부터 Upload/Delete/Generate 버튼, Model과 Preset을
고르는 Settings가 배치되어 있다. 그림은 영상을 업로드한 직후, Generate를 누르기
전 상태다.*

> **구현 파일:** `vlm_client/core.py`, `vlm_client/window.py`, `utils/vllm_client.py`
*   vLLM 서버와의 통신(영상을 2초 청크로 슬라이스 → base64 data URI로 인코딩 → OpenAI 호환 엔드포인트 `/v1/chat/completions`에 요청)은 `utils/vllm_client.py`에 구현
*   `vlm_client/core.py`는 `utils/vllm_client.py`를 활용하여 작업을 지시하는 역할
    *   경로 설정, 프롬프트 정의, 업로드된 비디오 ID 상태관리 등
    *   VLM에 전달되는 동영상 청크의 길이는 `vlm_client/core.py`의 `default_chunk_duration` 에서 설정 (청크에 포함되는 frame 개수는 vLLM 서버 기동 시 `--media-io-kwargs`로 설정하며 클라이언트가 요청별로 바꿀 수 없음)
---
### 9. Event Post Processing

VLM의 output을 Time Travel 모듈에서 재생 가능한 형태(Event List)로 변환  
`events/summary_service.py`(EventSummaryService)가 `events/core.py`의 `consolidate_events`를 import하여 데이터를 가공(궤적 좌표는 `TrajectoryRepository`의 in-memory 데이터를 참조해야 하기 때문)

**기능:**
*   **Input**: `vlm_outputs/` 내의 JSON 파일명
*   **Process Events** (`events/summary_service.py`에서 진행됨):
    1.  JSON 파싱 및 정제 (중간단계 결과물: `artifacts/intermediate_results/*_intermediate.jsonl`)
    2.  이벤트 발생 시점의 객체 3D 좌표 추출 (`TrajectoryRepository`의 in-memory 데이터 참조)
    3.  최종 결과물 `*_eventlist.jsonl` 생성 (경로: `artifacts/event_list/`)

**사용법:**
*   Input JSON File에 파일 이름 복붙 -> Process Events 버튼

이 단계의 창은 입력 파일명을 적는 칸과 실행 버튼 하나로 단순하다 — 아래 그림 참조.

<!-- 이미지 TODO | 파일: images/readme/guide-07-event-post-processing-window.png | 촬영: Event Post Processing 창. Input JSON File 칸에 실제 파일명이 입력되어 있고, Process Events 버튼이 보이는 상태(처리 완료 후 결과 경로 메시지가 떠 있으면 더 좋음) | 형식: png -->
![그림 7. Event Post Processing 창](../../../images/readme/guide-07-event-post-processing-window.png)
*그림 7. Event Post Processing 창. Input JSON File에 VLM 산출물 파일명을 넣고
Process Events를 누르면 이벤트 리스트가 생성된다.*

> **구현 파일:** `events/core.py`, `events/summary_service.py`, `events/window.py`
---
### 10. Event-based Summarization Playback

최종 Event list를 활용하여 사건 중심 요약을 생성

**사용법:**
*  Time Travel Window의 **Event based Summary** 체크박스 선택(로드되면 라벨이
   "Event based Summary Mode (N events)"로 바뀐다)
*  Viewpoint를 생성하는 Camera를 "summarization_camera"로 변경 (Extension 초기화 시 자동 생성됨)
*  **Play**: 이벤트 리스트를 순회하며, 이벤트 발생 구간만 자동 재생
    *   재생 길이: `playback/controller.py`의 `_event_playback_duration` 설정값 (기본 1초)
    *   화면 이동: 이벤트 발생 시공간(위치+시간)으로 Viewport 자동 이동
*  **Next Event** (Pause 상태일때): 버튼 클릭 시 다음 이벤트 발생 직전 시점으로 점프

Event based Summary Mode로 재생 중인 화면은 아래 그림과 같다 — 모드 라벨에
이벤트 개수가 표시되고, 카메라는 summarization_camera로 전환되어 사건이 벌어진
위치를 비춘다.

<!-- 이미지 TODO | 파일: images/readme/guide-08-event-summary-playback.png | 촬영: Event based Summary 체크박스를 켜서 라벨이 "Event based Summary Mode (N events)"로 바뀐 Time Travel Window와, summarization_camera로 전환되어 이벤트 발생 위치를 비추는 뷰포트를 함께(또는 이어서) 캡처. Play 버튼을 눌러 재생 중인 상태 | 형식: png 또는 5초 내외 gif(Next Event로 다음 이벤트로 점프하는 순간이 보이도록) -->
![그림 8. Event-based Summarization Playback — 재생 중 화면](../../../images/readme/guide-08-event-summary-playback.png)
*그림 8. 사건 중심 요약 재생 화면. Time Travel Window의 모드 라벨(N events)과,
summarization_camera로 전환된 뷰포트가 이벤트 발생 시공간을 자동으로 비추는
모습을 함께 보여준다.*

---
