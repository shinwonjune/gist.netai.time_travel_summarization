# Time Travel Summarization Extension

이 문서는  **Time Travel Summarization Framework**를 구현한 **Time_travel_Summarization** Extension의 사용 설명서입니다.
본 프레임워크는 Dream-AI_Plus_Twin.usd를 기반으로 구현되었으며 시계열 궤적 데이터를 활용하여 디지털트윈의 과거 상태를 복원하고, 이를 기반으로 **Event-based Summarization** (현재 '충돌' 이벤트 지원)을 생성합니다.

## 📝 Note: 용어 및 구조 정의

*   **Time Travel의 정의**:
    *   **본 문서:** 시간의 흐름에 따라 객체의 위치 상태를 복원하고 재생하는 기능
    *   **공식적(교수님) 관점:** 단순 복원을 넘어선 통합적인 시공간 분석 기능의 통칭 (본 프레임워크는 이를 지원하는 도구)

*   **익스텐션 구조**:
    *   익스텐션은 다양한 모듈로 구성되며 `extension.py`를 통해 통합 초기화
    *   모듈: Time Travel, View overlay, VLM Client, Event Post-Processing
    *   익스텐션 외부의 VLM 서버를 통해 추론 (VLM Client가 중재)

## Extension 설치 가이드 (Local Installation)

1. **Extension 다운로드**
   ```bash
   git clone https://github.com/SmartX-Team/Omniverse.git
   ```

2. **Local Path 추가**
   *    USD Composer에서: **Developer → Extensions → ☰ → Settings → Extension Search Path**
   *    `[NetAI]Time_Travel_Summarization`의 `exts` 폴더의 전체 경로 추가
        *   예시: `C:\Users\<username>\workspace\Omniverse\Extension\[NetAI]Time_Travel_Summarization\exts`
   *    Third party 에서 Extension 실행


---

## 🚀 사용 가이드 (Workflow) 및 기능 설명

프레임워크의 작동 순서에 따른 단계별 사용법

### 0. VLM 서버 실행

VLM 서버를 실행합니다. 
GPU 서버에서 vLLM 컨테이너(OpenAI 호환 API)를 실행
GPU는 L40 또는 A100 40GB 한 대로 충분

자세한 내용은 `./VLM_server` 디렉토리 README.md에서 확인

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

USD Composer 실행 후 `Extension` 창에서 **Time Travel Summarization**을 찾아 활성화. (Extension ID: `netai.timetravel_dreamai`)
*   실행 시 Time Travel, View Overlay, VLM Client, Event Post Processing 등 모든 모듈의 UI Window가 초기화
---
### 4. Time Travel

**기능:** 시계열 데이터를 기반으로 과거 상태를 재현하고 탐색
*   **자동 객체 생성**: 데이터 내 ID 개수만큼 객체(Astronaut) 생성 및 매핑
*   **Dataset Range**: 시계열 데이터의 시작 timestamp와 끝 timestamp 표시
*   **Go**: 특정 Timestamp 시점으로 즉시 이동
*   **Stage Time**: 현재 재현된 디지털트윈의 시간 표시
*   **Play/Speed**: 시간 흐름에 따른 재생 및 속도 조절
*   **Timeline slider**: 타임바를 통한 선형적 시점 조절

> **구현 파일:** `core.py`, `window.py`
---
### 5. View Overlay

VLM이 이벤트 발생 시간과 연루된 객체를 특정할 수 있도록 하기 위한 visual prompting 과정

**기능:** 복원된 디지털트윈 장면 위에 객체 정보(ID)와 현재 시간(Timestamp)을 오버레이

**사용법:**  
*   timestamp, objectIDs overlay 체크박스 선택

> **구현 파일:**
> *   `overlay/core.py`, `overlay/components.py`, `overlay/window.py`
---
### 6. Visual Abstraction & Temporal Acceleration (Optional)

VLM의 추론 성능을 극대화하기 위해 디지털트윈 환경을 조정하는 단계

#### Visual Abstraction (시각적 단순화)
*   **목적:** 불필요한 시각 정보를 줄여 VLM이 객체 상호작용에 집중하도록 함
*   **방법:** Omniverse Stage 창의 '눈(Eye)' 아이콘을 통해 Prim 그룹(Furniture, Equipment 등)을 비활성화
*   **단계 예시:** Full Digital Twin → Simplified (Equipment 제거) → Abstract (Equiptment + A_Exterior 제거, View Overlay만 유지)

#### Temporal Acceleration (시간 가속)
*   **목적:** VLM에 전달되는 동영상의 재생 속도를 가속하여(영상 길이를 단축하여) VLM 처리 속도 향상
*   **경험적 성능:** '충돌' 이벤트 검출 시 **3배속** 영상까지는 추론 성능 저하가 없었음 (이벤트 특성에 따라 조절 필요)
*   시간 가속된 동영상 생성 방법은 "7. 동영상 추출" 에서 설명
---
### 7. 동영상 추출 (Movie Capture)

**Movie Capture Extension** (내장 기능)을 사용하여 VLM 서버로 전송할 영상을 생성
*현재 동영상 추출 단계가 파이프라인의 주요 병목구간*
*영상 전달을 스트리밍 방식으로 확장 필요 (현재는 파일 캡처 후 업로드하는 방식이라 캡처 완료까지 대기해야 함)*

#### 📸 캡쳐 가이드
Movie Capture의 고정된 캡쳐 FPS 특성상, 원하는 재생 속도의 동영상을 얻기 위해 **Time Travel 재생 속도 조절**이 필요

*   **설정 값**:
    *   **Camera prim name**: 장면(Scene)을 추출할 카메라를 선택
    *   **Frame rate**: 30 FPS
    *   **Capture range(Seconds) End**: 생성할 영상의 길이(초) 선택
    *   **Resolution**: 532 x 280 (변경가능, 빠른 추론속도와 reshape 과정에서의 정보 손실 방지를 위함)
    *   **Output Path**: extension 경로 내부의 `artifacts/video` 폴더
    *   **Output Name**: 파일명 규칙 없음 — 임의로 지정 가능(예: `video_1.mp4`), 이후 VLM Client 단계에서 이 파일을 그대로 업로드
    

#### 재생 속도 설정 공식
Movie Capture는 기본적으로 10 FPS 로 캡쳐를 진행
*   Frame rate 와 Cumstom Range end (second) 를 곱한 수 만큼의 이미지를 10FPS 속도로 캡쳐한 뒤 동영상 인코딩  
예를 들어, 30FPS, 60초 Capture range 설정을 하면, 30x60 = 1800 장의 이미지를 10FPS로 촬영
따라서 1800/10 = 180초가 소요. 그러므로 Time Travel 재생 속도를 0.33x 로 설정하여 3분동안 시뮬레이션(재생)이 진행되도록 조절해야 60초 길이의 동영상을 생성 가능  
1분(60초) 분량의 데이터를 캡쳐할 때 권장 설정은 다음과 같음

| 목표 영상 속도 | 결과 영상 길이 | Custom Range End | **Time Travel Play Speed** | 설정 이유 |
| :--- | :--- | :--- | :--- | :--- |
| **1배속 (정속)** | 60초 | 60 | **0.33x** | Capture가 약 3분동안 진행되므로, 재생 속도를 1/3로 늦춰야 1배속 영상 생성됨 |
| **3배속 (가속)** | 20초 | 20 | **1.0x** | 정속 재생으로 캡쳐 시, 결과적으로 약 3배 빠른 영상(Temporal Acceleration)이 생성됨 |
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
    *   Overlap: 동영상 청크의 겹침 정도(초) 설정 (1초 단위)
*   **결과**: `artifacts/vlm_outputs/` 경로에 JSON 형태로 저장됨

**사용법:**
*   Upload 버튼 (비디오 전송) -> Settings 확인 -> Genearte 버튼 (추론 요청)

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
*   Input JSON File에 파일 이름 복붙 -> Process Evetns 버튼

> **구현 파일:** `events/core.py`, `events/summary_service.py`, `events/window.py`
---
### 10. Event-based Summarization Playback

최종 Event list를 활용하여 사건 중심 요약을 생성

**사용법:**
*  Time Travel Window의 **Event based summary mode** 체크
*  Viewpoint를 생성하는 Camera를 "summarization_camera"로 변경 (Extension 초기화 시 자동 생성됨)
*  **Play**: 이벤트 리스트를 순회하며, 이벤트 발생 구간만 자동 재생
    *   재생 길이: `core.py`의 `_event_playback_duration` 설정값 (기본 1초)
    *   화면 이동: 이벤트 발생 시공간(위치+시간)으로 Viewport 자동 이동
*  **Next Event** (Pause 상태일때): 버튼 클릭 시 다음 이벤트 발생 직전 시점으로 점프

---
