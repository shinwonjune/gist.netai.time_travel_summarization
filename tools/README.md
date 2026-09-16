# tools — 합성 데이터 생성과 인프라 진단 스크립트

## 역할과 위치

`tools/`는 레포 루트에 있는 독립 실행 스크립트 모음이다.
`gist/netai/time_travel_summarization/automation`이나 `utils`와 달리 이
디렉터리의 스크립트는 확장 패키지 안이 아니라 레포 최상위에서 직접
실행하는 것을 전제로 한다(단 `lake_ingest.py`는 확장 패키지의 라이브러리
`playback.lake_common`을, `diagnostics/pyarrow_abi_probe.py`는 SMOKE_LEVEL=3에서
`storage.from_uri`를 임포트한다). 크게 두 갈래다.

1. **합성 궤적 생성**(`generate_living_trajectory.py`,
   `generate_overlay_calibration_grid.py`, `lake_ingest.py`) — 물리
   시뮬레이션 없이 순수 수학으로 좌표 CSV를 만들어 재연/오버레이/레이크
   적재 경로를 독립적으로 시험할 수 있게 한다.
2. **인프라 진단**(`diagnostics/minio_probe.py`,
   `diagnostics/pyarrow_abi_probe.py`) — minIO 연결과 Kit 내장 파이썬의
   pyarrow ABI 정합을 읽기 전용으로 점검한다.

## 모듈 표

| 스크립트 | 역할 | 입력 → 산출물 | 의존성 |
|---|---|---|---|
| `generate_living_trajectory.py` | 합성 궤적 생성 | CLI 인자(객체 수/시간/속도) → trajectory CSV | stdlib |
| `generate_overlay_calibration_grid.py` | 오버레이 투영 보정용 격자 생성 | CLI 인자(격자 크기/범위) → 5x5 격자 CSV | stdlib(`generate_living_trajectory`의 상수 재사용) |
| `lake_ingest.py` | 데이터 레이크 적재 CLI(합성/CSV 겸용) | 합성 데이터 또는 CSV → 시간 분할 청크 + manifest | stdlib(+ 실 minIO/parquet은 `playback.lake_common`이 `minio`/`pyarrow` 필요) |
| `diagnostics/minio_probe.py` | minIO 연결 read-only 점검 | `.env`의 자격증명 → 버킷/객체 목록 | stdlib(AWS SigV4 서명을 직접 구현, 외부 SDK 불필요) |
| `diagnostics/pyarrow_abi_probe.py` | Kit 내장 파이썬의 pyarrow ABI 점검 | 실행 환경 → 콘솔/로그 파일 진단 출력 | stdlib(+ pyarrow, Omniverse Script Editor에서 실행) |

## 각 스크립트의 동작 요약

### generate_living_trajectory.py

`living_trajectory_1min_0.2s.csv`와 같은 스키마(`timestamp,objid,x,y,z`)로
객체가 방 안을 배회하는 합성 궤적을 만든다. 좌표 범위는 고정 상수
`X_RANGE=(206.0, 1554.0)`, `Y_RANGE=(89.5, 200.0)`, `Z_RANGE=(-2879.0,
-1258.0)`(living-room 씬의 실측 범위)이다. 각 객체는 단위 방향 벡터 +
속도로 직진하다가 무작위 간격(20~100 스텝)마다 목표 방향으로 80:20
혼합 보간하며 방향을 틀고, 경계에 닿으면 그 축의 속도 부호를 반전시킨다.
매 스텝 방향 벡터에 가우시안 노이즈를 더한 뒤 정규화해 직선이 미세하게 흔들리게 한다.

CLI: `--duration-hours`(필수) `--output`(필수) `--objects`(4) `--interval`
(0.2) `--min-speed`(150.0) `--max-speed`(200.0) `--seed`(42).

생성된 CSV를 평면(x-z, BEV 시점 기준)에 그려보면 방향 전환과 경계 반사가
실제로 어떻게 나타나는지 한눈에 확인할 수 있다.

<!-- 이미지 TODO | 파일: images/readme/tools-01-trajectory-xz-plot.png | 플롯: generate_living_trajectory.py로 만든 CSV의 x 열(가로축)과 z 열(세로축)을 objid별로 다른 색 선으로 그린 산점/선 플롯. X_RANGE=(206.0, 1554.0), Z_RANGE=(-2879.0, -1258.0) 경계 근처에서 궤적이 꺾이는 지점이 보이면 좋음 | 형식: png -->
![그림 1. 합성 궤적의 x-z 평면 플롯 — 객체별 경로와 경계 반사](../images/readme/tools-01-trajectory-xz-plot.png)
*그림 1. 각 객체가 무작위 간격마다 방향을 틀고, `X_RANGE`/`Z_RANGE` 경계에 닿으면 그 축의 속도 부호가 반전되는 것을 궤적의 꺾임으로 확인할 수 있다.*

### generate_overlay_calibration_grid.py

같은 CSV 스키마로, 이번엔 배회 대신 `grid_size × grid_size`(기본 5x5)
격자점에 객체를 고정 배치하고 `--duration-seconds` 동안 정지 상태로
기록한다. 오버레이가 실제 지오메트리 위에 그려지는지 좌표별로 검증하는
데 쓰는 캘리브레이션 데이터다. X/Z 범위 기본값은
`generate_living_trajectory.py`의 `X_RANGE`/`Z_RANGE`를 그대로 가져오고,
Y는 `GROUND_Y=89.5`로 고정한다.

CLI: `--output`(기본
`gist/netai/time_travel_summarization/artifacts/trajectory/
overlay_calibration_grid_5x5.csv`) `--grid-size`(5, ≥2 필수) `--duration-seconds`
(10.0) `--interval`(0.2) `--y`(89.5) `--x-min`/`--x-max`/`--z-min`/`--z-max`
(X_RANGE/Z_RANGE 기본값).

이 격자 CSV를 재연해 캡처한 화면에서 오버레이 마커가 격자점 위에 정확히
찍히는지 눈으로 확인하는 것이 이 도구의 목적이다.

<!-- 이미지 TODO | 파일: images/readme/tools-02-overlay-calibration-grid.png | 촬영: overlay_calibration_grid_5x5.csv를 재연(replay)해 캡처한 뷰포트 화면. 5x5 격자점 위치에 오버레이 ID 마커가 정확히 겹쳐 있는지(또는 어긋나 있는지) 보이는 프레임 | 형식: png -->
![그림 2. 오버레이 보정 격자 캡처 — 5x5 격자점과 마커 정렬](../images/readme/tools-02-overlay-calibration-grid.png)
*그림 2. 격자점마다 마커가 정확히 겹치면 오버레이 투영이 정확하다는 뜻이고, 어긋난 정도와 방향이 투영 보정 파라미터를 조정할 단서가 된다.*

### lake_ingest.py

합성 궤적(`playback.lake_common.generate_synthetic_rows`) 또는 기존 CSV를
시간 분할 청크(CSV 또는 parquet)로 만들어 로컬(`file://`) 또는
minIO(`s3://`)에 적재하는 CLI다. 청크 생성/manifest 작성의 실제 로직은
`playback.lake_common.ingest_rows`에 있고, 이 스크립트는 소스 선택
(합성 vs CSV 파일)과 CLI 파싱만 담당한다. 실행 후 콘솔에 `config.json`에
넣을 `"lake": {"enabled": true, "manifest_uri": ..., "cache_chunks": 4,
"prefetch_ahead": 2}` 스니펫을 그대로 출력해 준다.

CLI: `--dest`(필수, `s3://bucket/ds` | `file:///tmp/ds` | 로컬 경로)
`--source`(`synthetic`(기본) 또는 CSV 경로) `--objects`(10) `--duration`
(`60s`, 단위 접미사 `s`/`min`/`h` 파싱 지원) `--hz`(5.0) `--chunk-seconds`
(60) `--format`(`csv`|`parquet`) `--seed`(42).

### diagnostics/minio_probe.py

minIO 연결을 **쓰기 없이** 점검하는 진단 스크립트. 외부 SDK(boto3/minio)
없이 AWS Signature V4 서명을 stdlib(`hashlib`, `hmac`, `urllib.request`)로
직접 구현해 `ListBuckets`/`ListObjectsV2`(delimiter=`/`)만 호출한다.
자격증명은 확장 패키지의 `.env`(`gist/netai/time_travel_summarization/.env`)
에서 읽고, endpoint가 `http://`인데 `MINIO_SECURE=true`인 조합이면 경고를
출력한다(반대 조합은 검사하지 않는다).

사용: `python3 tools/diagnostics/minio_probe.py [prefix]` — 인자가 없어도
`ListBuckets`와 `ListObjectsV2`(prefix 빈 문자열)를 항상 실행하며, `prefix`를 주면
그 접두사 아래 "폴더"(CommonPrefixes)와 객체 목록으로 조회 범위를 좁힌다.

### diagnostics/pyarrow_abi_probe.py

Omniverse Kit 내장 파이썬에서 pyarrow의 네이티브 확장 모듈(`.pyd`/`.so`)
ABI가 실행 중인 파이썬 버전과 맞는지 점검하는 진단 스크립트. **Omniverse
Script Editor에서 파일 내용을 직접 실행**하는 것을 전제로 설계됐다(파일
맨 끝에서 `main()`을 즉시 호출하며 `if __name__ == "__main__"` 가드가
없다).

`SMOKE_LEVEL`(env var `TTS_PYARROW_SMOKE_LEVEL` 또는 Script Editor에서
미리 정의한 전역 `SMOKE_LEVEL`, 기본 0)로 점검 강도를 단계적으로 올린다:
- `0` — import 여부와 `sys.path`/네이티브 태그(`.cp312-win_amd64.pyd` 등)
  검사만(쓰기 없음). `_native_tag`로 뽑은 파일명의 CPython 태그가
  `sys.implementation.cache_tag`에서 유도한 기대 태그 집합과 다르면
  "ABI CHECK: MISMATCH"를 출력한다.
- `1` — Arrow 테이블을 메모리에서 생성.
- `2` — parquet을 메모리 버퍼와 임시 파일에 write/read 왕복.
- `3` — `TEST_URI`(env var `TTS_PYARROW_TEST_URI`)로 지정한 실제 프로젝트
  URI(manifest.json 또는 .parquet/.csv)를 `storage.from_uri` 어댑터로
  읽어 검증.

진단 로그는 콘솔(`print`) + `carb.log_warn`(Kit 환경이면) +
`TTS_PYARROW_PROBE_LOG`(기본 `<tempdir>/tts_pyarrow_abi_probe.txt`)
파일에 동시에 남는다.

## 사용법

```bash
# 합성 궤적 생성
python3 tools/generate_living_trajectory.py \
    --duration-hours 1 --output artifacts/trajectory/living_1h.csv \
    --objects 6 --interval 0.2

# 오버레이 보정 격자
python3 tools/generate_overlay_calibration_grid.py --grid-size 5

# 레이크 적재(합성)
python3 tools/lake_ingest.py --dest /tmp/lake/ds1 --objects 10 --duration 1h

# 레이크 적재(기존 CSV → parquet, minio 필요)
python3 tools/lake_ingest.py --source artifacts/trajectory/living_1h.csv \
    --dest s3://mybucket/trajectory/living --format parquet --chunk-seconds 60

# minIO 연결 점검(.env 필요)
python3 tools/diagnostics/minio_probe.py

# pyarrow ABI 점검 — Omniverse Script Editor에서 tools/diagnostics/pyarrow_abi_probe.py 내용을 실행
```

## 한계

- `diagnostics/pyarrow_abi_probe.py`는 Kit 프로세스 안(Script Editor)에서
  실행하는 것을 전제로 설계됐다 — 일반 파이썬 인터프리터에서 실행해도
  import/ABI 점검(레벨 0~2)까지는 동작하지만 `carb.log_warn` 로그는
  나오지 않는다.
- `diagnostics/minio_probe.py`는 읽기 전용(ListBuckets/ListObjectsV2)만
  수행한다 — 쓰기 권한이나 버킷 정책 문제는 진단하지 못한다.
