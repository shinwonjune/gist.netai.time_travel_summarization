# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).


## [Unreleased] - 2026-05-25 데이터레이크 연동

### Added
- minIO/file 기반 데이터레이크에서 시간 분할 청크와 `manifest.json`을 이용해 대규모 좌표 로그를 윈도우 로딩으로 재연하는 경로를 추가했다.
- `tools/lake_ingest.py`로 합성 데이터 또는 CSV를 청크+manifest 데이터셋으로 적재하는 CLI를 추가했다.
- event-list JSONL 저장·로드, 캡쳐 비디오 저장, 레이크 URI 기반 VLM 업로드를 지원하는 후속 레이크 연동을 추가했다.
- Time Travel UI에 신규 Data Source 토글(`Local` / `Data Lake`)을 추가해 기존 로컬 파일 경로와 데이터레이크 경로를 선택할 수 있게 했다.
- 데이터레이크 작업 상세 기록 `docs/WORKLOG_2026-05_datalake.md`를 추가했다.

### Changed
- `TrajectoryRepository`의 행 읽기/변환 헬퍼를 청크 로딩과 기존 전체 로딩이 공유하도록 분리했다.
- `app/config.py`와 `app/facade.py`가 `lake`, `output_root_uri`, `event_list_uri`, `video_output_uri` 설정을 읽고 기존 로컬 동작과 레이크 동작을 분기하도록 확장했다.
- 레이크 재생 경로는 프리페치와 LRU 캐시로 연속 재생 중 청크 경계 동기 로드 stall을 줄이도록 변경했다.


## [0.1.0] - 2025-09-12
- Initial version of extension UI template with a window
