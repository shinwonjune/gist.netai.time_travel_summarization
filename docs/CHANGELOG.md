# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).


## [Unreleased] - 2026-07 프레임워크 고도화

### Added
- **VLM 폐루프 완성**: PhysX 충돌 시뮬로 학습 데이터 생성 → Qwen3-VL-8B LoRA 튜닝 →
  vLLM OpenAI 호환 직접 호출 추론(VSS 대체). 데이터 증량 스케일링 실증(base→v1→v2).
- **이벤트 인덱스**(`event_processing/event_index.py`): 추론 결과를 `vlm_events/`에 축적,
  사이드카 앵커로 절대 시각 복원(자정 롤오버), twin time 구간 조회 → 선택 시 재구성.
- **원격 제어면**: REST 잡 API(FastAPI, `VLM_server/l40/job_api.py`) + GPU별 순차 큐 +
  잡 타입(generate/train/serve) + 서빙 GPU 역할 분리. extension GUI "Remote Jobs" 창.
- **재생 공백 점프**: 데이터 공백 > 임계값이면 시계를 다음 데이터로 순간이동.
- **정적 게이트**: pyproject.toml(ruff+mypy) + `.github/workflows/ci.yml`, pytest kit 마커.

### Changed
- **facade.py 분해**(1,235줄 → 코디네이터 + app/{capture,physics,data,object,benchmark}_service).
  공개 API 불변, 상태는 core 소유.
- 오버레이/충돌 CSV 시각을 `timefmt.py` 초 단위로 통일(추론↔라벨 정합). "Stage Time"→"Twin Time".
- GUI 정리: 캡처 진입점 단일화(메인 Capture→VLM Source 자동 전달), VSS 잔재 제거.

### Fixed
- `list_prefix` 폴더 누출(minio dir 항목 size=None vs ==0), build_dataset 크로스플랫폼 경로 2건.


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
