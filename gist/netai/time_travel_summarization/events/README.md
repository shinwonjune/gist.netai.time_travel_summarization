# events — VLM 추론 결과를 파싱, 시간축 인덱스, 3D 좌표로 이어붙이는 후처리 계층

## 역할과 위치

`gist/netai/time_travel_summarization/events/`는 VLM(vision-language model)이 영상을 보고 낸 자유 형식에 가까운 JSON 응답을, 이 확장의 나머지 부분(재생 헤드 점프, 데이터 레이크 — minIO에 시간 분할 청크와 manifest로 적재한 궤적 데이터셋 — 의 시간창 조회, 3D 프림 좌표)이 쓸 수 있는 고정 스키마로 바꾸는 계층이다. 역할은 세 갈래로 나뉜다.

- **파싱** — `core.py`가 VLM 응답 문자열(`chunk_responses[].content`)을 `{timestamp: [obj_ids]}` 형태로 정규화한다.
- **인덱싱** — `event_index.py`가 추론 1회의 결과를 minIO/로컬에 영상별 JSONL 오브젝트로 축적하고, 시간창으로 조회하는 표면을 제공한다. `summary_service.py`가 처리 산출물(중간 JSONL, 이벤트 리스트)을 만들고 3D 좌표를 붙인다.
- **UI** — `window.py`(Event Post Processing 창)와 `summary_window.py`(Event Summary 창, 검색+재생)가 위 로직을 Kit 창에 연결한다.

`app/facade.py`(`TimeTravelCore`)가 `EventSummaryService`를 소유하고, `extension.py`가 두 UI 창을 생성한다. `core.py`/`event_index.py`는 `carb`/`omni` 없이 순수 파이썬으로 동작하고(`core.py`의 로그만 `carb` 있으면 우선 쓰고 없으면 `print`로 폴백), `summary_service.py`는 `app/paths.py`(경로만, Kit 무의존)에 의존한다. `window.py`/`summary_window.py`는 `omni.ui`/`carb`에 의존하지만 `summary_window.py`는 순수 헬퍼(`GenerationToken`, `parse_event_time` 등)를 모듈 최상위에 두고 `omni`/`carb` 임포트는 메서드 안으로 미뤄, 그 헬퍼들만은 Kit 없이 단위 테스트가 가능하다.

## 구성 파일

| 파일 | 책임 | 핵심 심볼 |
|---|---|---|
| `__init__.py` | 패키지 설명 docstring만 있음(공개 심볼 재수출 없음) | — |
| `core.py` | VLM 응답 문자열 파싱(마크다운 펜스 제거 → JSON → 관대한 폴백) + core 포맷(`objNNN`, 날짜 포함 타임스탬프)으로 변환 | `parse_content`, `consolidate_events`, `format_timestamp_for_core`, `format_objid_for_core`, `load_json` |
| `event_index.py` | 추론 결과를 시간축으로 조회 가능한 인덱스로 적재/조회. 영상 사이드카의 캡처 시각 앵커로 `HH:MM:SS`를 절대 시각으로 복원 | `parse_events_from_vlm_result`, `resolve_event_datetime`, `sidecar_anchor`, `append_index`, `query_events`, `index_uri_for` |
| `summary_service.py` | `EventSummaryService` — `process_event_json`(VLM JSON → 중간 결과 + 이벤트 리스트 산출)과 이벤트 리스트 로드, 3D 좌표 조회 | `EventSummaryService` |
| `window.py` | Event Post Processing 창 — VLM 결과 JSON 경로/URI를 입력받아 `EventSummaryService.process_event_json` 실행 | `EventProcessingWindow` |
| `summary_window.py` | Event Summary 창 — 이벤트 인덱스 시간창 검색 + 검색 결과를 순서대로/개별로 재생 | `EventSummaryWindow`, `GenerationToken`, `parse_event_time`, `sort_events_by_time`, `event_time_span`, `playback_reached_end` |

CLI(입력 JSON을 JSONL로 일괄 변환)는 이 디렉터리가 아니라 `utils/inspect_events.py`에 있다 — `events/core.py`의 `consolidate_events`/`load_json`을 가져다 쓰는 얇은 래퍼로, `python -m utils.inspect_events <events>.json [-o out.jsonl] [--summary] [--date 2025-01-01]` 형태로 실행한다.

## 동작 흐름

### `core.py` — VLM 응답 문자열 파싱

VLM이 정상적으로 응답하면 `chunk_responses[].content`는 `'[{"00:00:03": [1, 3]}, {"00:00:06": [1, 2, 3, 4]}]'` 형태의 문자열이다(HH:MM:SS 키, 객체 번호 리스트 값). `parse_content(content_str)`은 이걸 관대하게 파싱한다.

1. 빈 문자열이거나 `"[]"`면 바로 `[]` 반환.
2. `_strip_markdown_fence`로 ` ```json\n...\n``` ` 감쌈을 벗긴다(펜스가 없으면 그대로 통과).
3. `(strict-json, candidate)`와 `(regex-extracted, _extract_list_with_regex(candidate))` 두 후보에 대해 순서대로 `json.loads` 시도 → 실패하면 `ast.literal_eval`(작은따옴표, trailing comma 등에 관대) 시도 → 그래도 실패하면 다음 후보로. `_extract_list_with_regex`는 정규식 `\[\s*\{.*?\}\s*(?:,\s*\{.*?\}\s*)*\]`로, 자연어 서두가 섞인 응답(`"Sure! [{...}] done"`)에서 리스트-of-딕트 패턴만 잘라낸다.
4. 파싱된 값이 `list`면 그대로, `dict`면 `{"events": [...]}` 형태나 단일 이벤트 딕트로 보고 정규화한다.
5. 모두 실패하면 `_log_parse_failure`가 응답 앞 200자를 `carb.log_warn`(가능하면) 또는 `print`로 남기고 `[]`를 반환 — 한 청크의 파싱 실패가 전체 처리를 죽이지 않는다.

`consolidate_events(data, base_date="2025-01-01")`는 `data["chunk_responses"]`를 순회하며 각 청크의 `parse_content` 결과를 모아, 타임스탬프별로 `[[objid, ...], ...]`(객체 그룹의 리스트) 딕셔너리로 합친다. 이 과정에서 `format_timestamp_for_core`가 `"HH:MM:SS"` + `base_date`를 core 포맷(`"YYYY-MM-DD HH:MM:SS.000"`)으로, `format_objid_for_core`가 정수 객체 번호를 `"objNNN"`(3자리 zero-pad)으로 바꾼다. 빈 객체 리스트(`obj_list`가 falsy)인 이벤트는 버린다.

### `event_index.py` — 시간축 검색 표면

오브젝트 스토리지(minIO)에는 append가 없으므로, 이 모듈은 **추론 1회 = 인덱스 오브젝트 1개** 규칙을 쓴다. `index_uri_for(index_root_uri, video_name)`이 `<root>/vlm_events/<video_stem>.jsonl` 형태의 URI를 만들고, 영상 파일명이 유일하다는 전제로 동시 쓰기 경합을 피한다.

**적재**: `vlm_client/core.py`가 데이터 레이크 모드(`output_root_uri` 지정)에서 추론이 끝날 때마다 `parse_events_from_vlm_result(response)` → `append_index(...)`를 호출한다(로컬 모드는 결과 JSON만 저장하고 인덱스에는 적재하지 않는다). `parse_events_from_vlm_result`는 `core.py`의 파싱과 별개의(더 좁은) 구현이다. `chunk_responses[].content`에서 첫 `'['`부터 마지막 `']'`까지만 잘라 `json.loads`하고, 형식이 깨진 청크는 조용히 건너뛴다. 청크 경계를 넘나들며 중복 보고된 이벤트는 `(time_str, tuple(sorted(ids)))`를 키로 삼아 dedupe하고, 청크의 `avg_logprob`이 있으면 `round(exp(avg_logprob), 4)`를 `confidence`(청크의 기하평균 토큰 확률)로 함께 기록한다.

VLM은 영상에 오버레이된 시계(`HH:MM:SS`, 날짜 없음)를 읽어 보고하므로 절대 시각으로 복원하려면 영상의 촬영 시작 시각(앵커)이 필요하다. `sidecar_anchor(video_source)`가 영상 옆의 `<stem>.meta.json` 사이드카(`app/capture_service.py`가 캡처 시점에 기록)를 `storage.from_uri`로 읽어 `capture_start` 필드를 `datetime`으로 반환한다(사이드카가 없거나 파싱 실패면 `None` — 예외를 던지지 않고 조용히 앵커 미상으로 처리). `resolve_event_datetime(hms, anchor)`가 앵커의 날짜를 `HH:MM:SS`에 붙이되, 보고 시각이 앵커 시각보다 이르면 자정을 넘은 것으로 보고 +1일 롤오버한다(영상 길이가 24시간 미만이라는 전제).

`append_index(index_root_uri, video_name, events, model="", run="", anchor=None)`은 각 이벤트에 대해 `anchor`가 있으면 `resolve_event_datetime`으로 절대 시각을 계산해 `time`에 저장하고(ISO, 초 단위), 없으면 `time=None`으로 두고 `time_hms`만 남긴다. 레코드는 `{"time", "time_hms", "ids", "kind"(기본 `"collision"`), "video", "model", "run", "indexed_at"}` 스키마로 JSONL 한 줄씩 직렬화되어 `index_uri_for`가 계산한 URI에 통째로 덮어쓰기된다 — 이벤트 0건이어도 파일은 기록한다(추론이 실행됐다는 증거를 남기기 위함). 같은 영상을 재추론하면 같은 오브젝트를 덮어써 최신 추론이 진실이 된다.

**조회**: `query_events(index_root_uri, start=None, end=None, run=None)`은 `<root>/vlm_events/` prefix 아래 모든 JSONL 오브젝트를 나열해(`recursive=True` — minIO 비재귀 목록은 이 prefix를 디렉터리 항목 하나로만 반환해 하위 오브젝트가 안 보이는 문제를 피하기 위함) 한 줄씩 읽고, `time`이 `None`인 레코드(앵커 미상)는 제외, `[start, end]`(ISO 문자열 비교, `start`/`end` 미지정 시 각각 `"0001-01-01T00:00:00"`/`"9999-12-31T23:59:59"`로 개방) 밖이면 제외, `run`이 지정됐는데 레코드의 `run`과 다르면 제외한 뒤 `(time, video)`로 정렬해 반환한다. 현재 규모(추론 결과 수백 건)에서는 매 조회마다 전체 스캔해도 충분하다는 전제이고, 병목이 되면 단일 parquet/DuckDB 컴팩션으로 승격할 여지를 남겨뒀다.

### `summary_service.py` — VLM 결과를 산출물과 3D 좌표로

`EventSummaryService(module_dir, repository, output_root_uri=None)`는 `TimeTravelCore`(`app/facade.py`)가 소유하며, 데이터 소스가 바뀔 때마다(`app/data_service.py`) `core._repository`와 `output_root_uri`(로컬 모드면 `None`, 데이터 레이크 모드면 데이터 레이크 산출물 루트)를 새로 넘겨 재생성된다.

`process_event_json(json_path)`이 핵심 흐름이다.

1. `_normalize_input_uri`가 입력을 URI로 정규화한다 — 이미 `s3://`/`minio://`/`file://` 스킴이면 그대로, 아니면 로컬 경로로 보고 존재 확인 후 `as_uri()`.
2. `_load_json_from_uri`가 `storage.from_uri(source_uri)`로 원본 VLM 결과 JSON을 읽는다.
3. `_resolve_base_date(vlm_data)`가 이벤트 타임스탬프에 붙일 날짜를 정한다 — 오버레이 시계엔 날짜가 없기 때문에 세 단계로 폴백한다. ① 결과 JSON의 `video_source` 필드가 있으면 `event_index.sidecar_anchor`로 그 영상의 촬영 앵커 날짜, ② `self._repository.data_start_time`(현재 로드된 궤적 데이터의 시작 날짜 — "지금 보고 있는 데이터를 처리한다"는 전제), ③ 위 둘 다 없으면 레거시 고정값 `"2025-01-01"`.
4. `core.consolidate_events(vlm_data, base_date=...)`로 이벤트를 core 포맷으로 정리하고, `<subdir=intermediate_results>/<stem>_intermediate.jsonl`에 타임스탬프 정렬 순으로 한 줄씩 쓴다.
5. `_generate_event_list(events)`가 각 타임스탬프의 첫 번째 오브젝트 그룹의 첫 objid를 골라(`obj_pairs[0][0]`), `self._repository.parse_timestamp`/`get_data_at_time`으로 그 시각의 3D 좌표를 조회한다. objid가 그 시각 궤적 데이터에 없으면(`carb.log_warn` 후) 그 이벤트를 건너뛴다. 결과는 `{"timestamp", "objid", "position": {"x","y","z"}}` 레코드 리스트이며, 이벤트 목록이 비면(모든 이벤트가 궤적과 매칭 실패) `process_event_json`은 `False`를 반환한다.
6. 이벤트 리스트를 `<subdir=event_list>/<stem>_eventlist.jsonl`에 쓰고, `self._event_positions`(타임스탬프 → `(x,y,z)`)를 갱신한다. `get_event_position(timestamp)`가 이 캐시를 조회하는 공개 API다.

두 산출물 모두 `_resolve_output_uri(subdir, filename)`을 거쳐 쓰인다 — `output_root_uri`가 있으면(데이터 레이크 모드) `<root>/<subdir>/<filename>` URI, 없으면(로컬 모드) `app/paths.py`의 `ExtensionPaths`가 관리하는 로컬 디렉터리(`artifacts/intermediate_results/`, `artifacts/event_list/`)의 `file://` URI로 폴백한다.

`load_events_from_event_list(event_list_uri=None)`은 UI가 아니라 재생 측(이벤트 재생 목록 로드)에서 쓰는 경로다. `event_list_uri`가 주어지면 그 prefix 아래(뒤에 `/`가 없으면 붙여서 — minIO는 trailing slash가 있어야 디렉터리 경계로 인식하고, 없으면 `recursive=False`가 폴더 내부를 못 찾아 빈 결과를 냄) `*_eventlist.jsonl` 중 `last_modified`가 있는 것 중 최신(없으면 URI 문자열 최대값으로 대체)을 골라 읽는다. `event_list_uri`가 없으면 로컬 `ExtensionPaths.event_list_dir`(레거시 `event_list/` 서브디렉터리 폴백 포함)에서 mtime 최신 파일을 찾는다. 두 경로 모두 `_parse_eventlist_text`로 파싱해 타임스탬프 리스트를 반환하고 `self._event_positions`를 채운다.

### UI — `window.py`, `summary_window.py`

`EventProcessingWindow`(Event Post Processing 창)는 입력 필드에 로컬 파일명이나 `file://`/`s3://` URI를 받아 `_resolve_json_input`(내부적으로 `storage.normalize_source`로 `버킷/키` 붙여넣기를 `s3://`로 정규화 → 스킴이 있으면 그대로, 없으면 `ExtensionPaths.resolve_input_file("vlm_outputs", ...)`로 로컬 탐색)으로 실제 경로를 만든 뒤, 데몬 스레드에서 `self._core.process_event_json(path)`을 호출하고 결과를 `UiTaskDispatcher`(`ui/task_dispatcher.py`)로 UI 스레드에 되돌린다. `set_source_json(uri)`는 VLM 추론이 끝났을 때(워커 스레드에서) 입력 필드를 자동으로 채우는 콜백이다. 이 창이 실제로 어떻게 생겼는지는 아래 그림 1을 보면 된다.

<!-- 이미지 TODO | 파일: images/readme/events-01-window-postprocessing.png | 촬영: Kit에서 Event Post Processing 창을 연 뒤, VLM 결과 JSON 경로/URI를 입력 필드에 채운 상태(처리 전 또는 처리 완료 메시지가 뜬 상태)의 창 스크린샷 | 형식: png -->
![그림 1. Event Post Processing 창 — VLM 결과 JSON 경로 입력과 처리 실행](../../../../images/readme/events-01-window-postprocessing.png)
*그림 1. 입력 필드에 채워진 경로가 `_resolve_json_input`을 거쳐 `process_event_json`으로 넘어가는 대상이다.*

`EventSummaryWindow`(Event Summary 창)는 이벤트 인덱스 검색과 재생 제어를 담당한다. `_on_search_clicked`가 입력된 `[start, end]`(`"%Y-%m-%d %H:%M:%S"` 형식)로 `event_index.query_events(index_root, start, end)`를 워커 스레드에서 호출하고(데이터 레이크 모드가 아니면 `get_output_root_uri_for_active_mode()`가 빈 값을 반환해 에러 처리), 결과를 버튼 리스트로 렌더링한다. 이벤트 버튼을 클릭하면 `_on_event_selected`가 `GenerationToken.bump()`로 새 세대를 만들고(이전 재생의 폴링 스레드를 무효화), `_seek_to_event`(로드된 재생 범위 밖이면 ±5분 창을 다시 로드)로 그 시각에 트윈을 재구축한 뒤 재생을 시작하고, 별도 스레드가 0.2초 주기로 `playback_reached_end(current, event_time, play_length)` 또는 데이터 끝 도달을 폴링해 도달하면 일시정지한다. `Play All`(`_on_play_all_clicked`/`_play_all_worker`)은 검색 결과를 `sort_events_by_time`으로 정렬한 뒤 이벤트를 순서대로 재생 길이만큼 재생하고 다음 이벤트로 넘어간다. 검색된 이벤트 목록과 재생 버튼이 실제 창에서 어떻게 배치되는지는 그림 2와 같다.

<!-- 이미지 TODO | 파일: images/readme/events-02-summary-window-playback.png | 촬영: Event Summary 창에서 [start, end] 시간창으로 검색을 실행해 이벤트 버튼 목록이 채워진 상태, 가능하면 Play All 버튼도 함께 보이는 화면 스크린샷 | 형식: png -->
![그림 2. Event Summary 창 — 검색된 이벤트 버튼 목록과 재생 제어](../../../../images/readme/events-02-summary-window-playback.png)
*그림 2. 버튼 하나하나가 검색된 이벤트 시각에 대응하며, 클릭하면 `_on_event_selected`가 그 시각으로 트윈을 재구축해 재생한다.*

## 핵심 설계 결정과 규약

- **추론 1회당 인덱스 오브젝트 1개, append 없음** — minIO 등 오브젝트 스토리지는 파일 append를 지원하지 않는다. 영상 파일명이 유일하다는 전제로 `<stem>.jsonl` 하나를 통째로 덮어쓰는 방식을 택해 동시 쓰기 경합을 원천적으로 없앴다. 규모가 커지면 이 JSONL들을 그대로 DuckDB/parquet으로 컴팩션하는 진화 경로를 열어뒀다(아직 구현 없음).
- **절대 시각 복원은 사이드카 앵커에 의존하고, 없으면 조회에서 제외된다** — VLM이 읽는 오버레이 시계는 날짜가 없는 `HH:MM:SS`뿐이라, 같은 시각의 서로 다른 날짜 이벤트를 구분하려면 영상의 촬영 시작 시각(앵커)이 반드시 필요하다. 앵커를 못 찾으면 `time=None`으로 레코드 자체는 남기되(`time_hms`는 보존, "적재는 됐다"는 증거) `query_events`의 시간창 조회에서는 제외한다 — 타임라인에 놓을 수 없는 이벤트를 억지로 끼워 넣지 않는다는 설계다.
- **`event_index.py`의 파서(`parse_events_from_vlm_result`)와 `core.py`의 파서(`parse_content`/`consolidate_events`)는 서로 다른 별개 구현이다** — 전자는 첫 `[`~마지막 `]`만 잘라 형식 위반 청크를 조용히 건너뛰고 dedupe와 confidence 계산까지 하는 인덱싱 전용 경로이고, 후자는 마크다운 펜스 제거 → strict JSON → regex 추출 → `ast.literal_eval`까지 이어지는 더 관대한 폴백 체인으로 산출물(중간 JSONL, 이벤트 리스트) 생성에 쓰인다. 두 경로 다 "형식이 깨진 일부가 전체 처리를 막지 않는다"는 원칙은 공유하지만, 코드는 통합돼 있지 않다.
- **이벤트 리스트의 3D 좌표는 그룹의 첫 objid만 대표로 쓴다** — `_generate_event_list`는 한 타임스탬프에 여러 객체 그룹이 있어도(`consolidate_events`가 `List[List[str]]`로 여러 그룹을 허용) 그 중 첫 그룹의 첫 objid 위치만 이벤트 위치로 기록한다. 나머지 그룹/객체의 좌표는 이벤트 리스트에 남지 않는다.
- **로컬/데이터 레이크 산출물 경로 분기는 `output_root_uri`의 존재 여부 하나로 결정된다** — `EventSummaryService`는 스스로 "지금 어느 모드인지"를 판단하지 않고, 생성 시점에 주입된 `output_root_uri`(데이터 레이크 모드면 URI, 로컬 모드면 `None`)만 본다. 데이터 소스가 바뀌면 `app/data_service.py`가 `EventSummaryService`를 통째로 새로 만들어 이 값을 갱신한다.

## 사용법

### VLM 결과 JSON 처리 (Python API)

```python
from gist.netai.time_travel_summarization.events.summary_service import EventSummaryService

service = EventSummaryService(module_dir, repository, output_root_uri=None)  # 로컬 모드
ok = service.process_event_json("artifacts/vlm_outputs/model_video_20260101_000000.json")
# ok == False면 파싱 실패이거나 매칭되는 궤적 좌표가 하나도 없었던 것
position = service.get_event_position("2025-01-01 00:00:28.000")  # (x, y, z) 또는 None
```

### 이벤트 인덱스 적재/조회 (`event_index.py`)

```python
from gist.netai.time_travel_summarization.events.event_index import (
    append_index, query_events, sidecar_anchor, parse_events_from_vlm_result,
)

anchor = sidecar_anchor("s3://bucket/artifacts/video/capture.mp4")  # None이면 앵커 미상
events = parse_events_from_vlm_result(vlm_response)  # [{"time": "00:00:28", "ids": [1, 4], "confidence": 0.83}, ...]
index_uri = append_index("s3://bucket/artifacts", "capture.mp4", events, model="qwen-vl", anchor=anchor)
# -> "s3://bucket/artifacts/vlm_events/capture.jsonl"

hits = query_events("s3://bucket/artifacts", start="2026-01-01T00:00:00", end="2026-01-01T23:59:59")
```

### CLI로 일괄 변환

```bash
python -m gist.netai.time_travel_summarization.utils.inspect_events \
    artifacts/vlm_outputs/model_video_20260101.json \
    -o artifacts/intermediate_results/model_video_20260101_intermediate.jsonl \
    --summary --date 2025-01-01
```

## 테스트

| 테스트 파일 | 검증 대상 |
|---|---|
| `tests/test_event_index.py` | `parse_events_from_vlm_result`의 잡문 허용과 dedupe, 청크 `avg_logprob` → `confidence` 계산, `resolve_event_datetime`의 자정 롤오버, `index_uri_for`의 URI 조립, `append_index`/`query_events`의 절대 시간창 조회(문자열/`datetime` 둘 다 허용), `run` 필터, 날짜가 다른 동일 `HH:MM:SS` 이벤트 구분, 앵커 없는 레코드가 파일에는 남되 조회에서는 제외되는 동작, 같은 영상 재적재 시 덮어쓰기, 이벤트 0건도 인덱스 오브젝트를 만드는 동작, `sidecar_anchor`가 `.meta.json`의 `capture_start`를 읽는 것과 사이드카 없을 때 `None` |
| `tests/test_eventlist_lake.py` | `EventSummaryService.load_events_from_event_list`가 로컬 파일 URI에서 타임스탬프 순서와 위치를 보존하는지, `_resolve_output_uri`가 `output_root_uri` 유무에 따라 데이터 레이크 URI/로컬 `file://` URI로 분기하는지, `process_event_json`이 `s3://` 입력을 받아 fake storage adapter로 중간 결과와 이벤트 리스트를 둘 다 쓰고 궤적에서 3D 좌표를 조회하는 전체 흐름 |
| `tests/test_event_summary_window.py` | `summary_window.py`의 순수 헬퍼만 — `GenerationToken`의 세대 무효화, `sort_events_by_time`(파싱 불가 레코드는 원래 순서를 지키며 뒤로), `event_time_span`(최소/최대, 전부 파싱 불가면 `None`), `parse_event_time`의 실패 케이스, `playback_reached_end`의 경계값(도달 전/정확히 도달/초과) |

`core.py`(`parse_content`, `consolidate_events` 등)를 직접 겨냥한 전용 테스트 파일은 이 확장의 `tests/`에 없다 — `utils/inspect_events.py` CLI를 통해서만 간접적으로 실행 경로를 탄다.

## 한계와 주의

- **`core.py` 파싱 실패는 로그만 남기고 이벤트를 조용히 버린다** — `parse_content`가 5단계 폴백을 모두 실패하면 그 청크의 이벤트는 그냥 사라진다(예외를 올리지 않음). 대량의 파싱 실패가 있어도 `process_event_json`은 `True`/`False`(이벤트 리스트가 비었는지)만 알려줄 뿐, 몇 개의 청크가 파싱에 실패했는지는 로그를 직접 봐야 안다.
- **이벤트-좌표 매칭은 궤적 데이터가 이미 로드돼 있어야 한다** — `_generate_event_list`가 `self._repository.get_data_at_time`을 호출하므로, VLM 결과가 가리키는 시간대의 궤적이 로드돼 있지 않으면(또는 objid가 그 시각에 없으면) 그 이벤트는 조용히 스킵된다.
- **`query_events`는 매 호출마다 인덱스 prefix 전체를 스캔한다** — 인덱스 오브젝트 수가 늘어나면(영상 수에 비례) 조회 비용이 선형으로 늘어난다. 컴팩션(단일 parquet/DuckDB)으로의 전환은 설계에 언급만 돼 있고 구현되지 않았다.
- **`sidecar_anchor`는 예외를 전부 삼킨다** — 사이드카 파일이 있어도 JSON 파싱 실패, `capture_start` 필드 누락, storage adapter 오류 등 어떤 이유로든 실패하면 원인 구분 없이 `None`을 반환한다.
