# Time Travel Summarization

VLM과 디지털 트윈을 연계한 **폐루프 시공간 탐색·요약 프레임워크** (NVIDIA Omniverse Kit 확장).

좌표 로그를 디지털 트윈(Omniverse stage)에서 시간축으로 재현하고, 그 화면을 VLM으로
분석해 이벤트 목록을 만든 뒤, 다시 디지털 트윈을 이벤트 중심으로 재구성한다.

```
좌표 로그 → [디지털 트윈 재현] → 영상 → [VLM 추론] → 이벤트 인덱스 → [이벤트 중심 재구성/요약]
              ↑ 데이터 레이크(minIO)                   ↑ vLLM(LoRA 튜닝)        ↑ 다시 트윈으로 피드백
```

## 핵심 기능

- **시간축 재현**: 좌표를 twin time(트윈 세계의 현재 시각) 기준으로 재생. floor-lookup +
  bisect, 배속·역재생·구간 점프·데이터 공백 건너뛰기를 단일 시계 메커니즘으로 처리.
- **데이터 레이크(minIO)**: manifest 인덱싱 + 윈도우 로딩 + 프리페치로 대규모(최대 12h) 로그를
  경계 stall 없이 재연. 로컬 file://과 s3:// 동일 코드 경로(URI 스킴 어댑터).
- **VLM 추론**: vLLM OpenAI 호환 직접 호출(과거 VSS 대체). 2초 청크 = 학습·추론 정합.
  Qwen3-VL-8B를 충돌 탐지 태스크로 LoRA 튜닝(`training/`).
- **이벤트 인덱스**: 추론 결과를 시간축 검색 표면(`vlm_events/`)으로 축적, twin time 구간 조회 →
  선택 시 그 시점으로 재구성.
- **원격 제어면**: extension GUI → REST 잡 API(FastAPI) → GPU별 큐. 생성/학습/서빙 잡 타입.

## 문서 안내

| 목적 | 문서 |
|------|------|
| **연구 요약**(학술) | [연구설명.md](연구설명.md) |
| **포트폴리오**(취업) | [포트폴리오.md](포트폴리오.md) |
| **구현 원리**(엔지니어·면접) | [explanation.md](explanation.md) |
| **면접 준비 노트** | [STUDY_NOTES.md](STUDY_NOTES.md), [pytorch_수동학습_커리큘럼.md](pytorch_수동학습_커리큘럼.md) |
| **데이터 레이크 상세** | [DATA_LAKE.md](DATA_LAKE.md), [minio_details.md](minio_details.md) |
| **작업 일지** | [physics고도화일지.md](physics고도화일지.md), [minIO고도화일지.md](minIO고도화일지.md) |
| **플랫폼 로드맵** | [플랫폼고도화_보완사항.md](플랫폼고도화_보완사항.md), [agentic_rag_고도화설계.md](agentic_rag_고도화설계.md) |
| **리팩터링 기록** | [REFACTORING_EXECUTION_LOG.md](REFACTORING_EXECUTION_LOG.md), [OMNIVERSE_SDK_IMPROVEMENTS.md](OMNIVERSE_SDK_IMPROVEMENTS.md) |

과거 세션 계획·폐기 문서는 [archive/](archive/) 참조.
