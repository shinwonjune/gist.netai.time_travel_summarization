# Qwen3-VL-8B LoRA 파인튜닝 방법론 (BEV 충돌 탐지 PoC)

이 문서는 "어떤 자료를 근거로, 무엇을, 어떤 기술로 학습했는가"를 정리한다.
실행 스크립트는 `qwen3vl_lora_swift.sh`(+`.yaml`), 데이터 생성은
`utils/build_dataset.py`, 평가는 `utils/compare_results.py`이다.

---

## 1. 문제 정의

BEV(Bird's-Eye View) 디지털 트윈 영상에서 **두 개 이상의 객체가 겹치는 충돌 이벤트**를
탐지하고, 그 **시각(HH:MM:SS)** 과 **관련 객체ID**를 구조화 JSON으로 출력한다.

```
[{"HH:MM:SS": [obj_a, obj_b]}, ...]
```

미세조정하지 않은 오픈 VLM은 이런 도메인 특화 이벤트 탐지에서 정밀도/재현율이
낮다. 소량의 도메인 데이터로 LoRA를 학습해 이 격차를 좁히는 것이 목표(PoC)다.

제약: 단일 L40(~40GB), 도메인 데이터 ~1–2K 클립.

---

## 2. 추론 파이프라인이 학습 설계를 결정한다 (핵심 원칙)

학습과 추론의 입력 분포를 **동일**하게 만드는 것이 PoC 성패를 가른다. 본 프로젝트의
추론 경로(`vlm_client/core.py`, `utils/VSS_client.py`)는 다음과 같다.

1. 통영상을 VSS 서버에 업로드하고 `chunk_duration=2`로 요청한다.
2. VSS 서버가 영상을 **2초 청크**로 잘라(overlap 0), **청크당 고정 20프레임**을
   샘플링해 VLM에 넣는다.
3. VLM은 청크 한 개만 보고, 화면 **우하단에 구워진 HH:MM:SS 오버레이**를 읽어
   충돌을 JSON으로 보고한다. (별도 타임스탬프 메타는 모델에 주지 않는다.)

따라서 학습 표본도 이 단위에 맞춘다:

| 추론 | 학습 (본 방법) |
|---|---|
| 입력 = 2초 청크 1개 | **표본 = 2초 클립 1개** |
| 청크당 20프레임 | **클립당 20프레임 (`NFRAMES=20`)** |
| 720×480 | 720×480 유지 |
| 시각 = 오버레이에서 읽음 | 정답 시각 = 오버레이와 동일 wall-clock |
| 프롬프트 = twin_view preset | **동일 프롬프트 재사용** (`vlm_client/prompts.py`) |

> 통영상을 그대로 학습하면(모델이 추론 때 절대 보지 않는 형태) train/infer가
> 어긋나 성능이 나오지 않는다. 2초 클립 슬라이싱은 임의 선택이 아니라 **추론
> 청크의 재현**이다.

---

## 3. 데이터셋 구성 (`utils/build_dataset.py`)

### 3.1 시각 정합 (라벨↔영상이 같은 시계)
- 충돌 기록 `CollisionRecorder`는 `datetime.now()` wall-clock으로 이벤트를 찍는다
  (`physics/collision_recorder.py`).
- 오버레이도 같은 wall-clock을 화면에 굽는다. physics(wander) 모드에서는 재생 시계가
  멈춰 있으므로, 캡처 시 오버레이가 wall-clock을 전진 표시하도록 보정했다
  (`app/facade.py` `get_stage_time_string`/`get_simulation_time`).
- 결과: 충돌 라벨의 HH:MM:SS == 그 순간 영상에 찍힌 HH:MM:SS. 재생속도 매핑/프레임
  인덱스 변환이 전혀 없어 **드리프트 0**.

### 3.2 클립 슬라이싱과 라벨링
- 캡처 시 `<video>.meta.json` 사이드카에 `capture_start`(t0), fps, 충돌 CSV 경로,
  `objid_to_label`을 기록해 video↔라벨↔t0를 결정론적으로 연결한다.
- 영상을 0,2,4…초 경계로 잘라(서버 청킹과 동일 정렬) 클립 i를 만든다(ffmpeg,
  frame-accurate seek). 클립 i의 wall-clock 창은 `[t0+2i, t0+2i+2)`.
- 충돌 행 중 **객체-객체 충돌(`kind="object"`)** 만 사용한다. 객체-객체 충돌은 두
  객체에 대해 두 행이 같은 초에 찍히므로, **초 단위 HH:MM:SS로 그룹핑**하면
  `{HH:MM:SS:[id,id]}` 집합이 된다(벽 충돌 `kind="wall"`은 과제 정의상 제외).
- **단위는 2초, 라벨 해상도는 초 단위**: 한 2초 클립에 서로 다른 초의 충돌이 여러 개
  있으면 항목도 여러 개로 보존한다(예: `[{"…:00":[1,2]},{"…:01":[3,4]}]`).
- objid→정수 라벨 변환은 오버레이가 화면에 그리는 규칙(`(\d+)$`,
  `video_capture/realtime_capture.py`)과 동일하게 맞춰, 정답 ID == 화면 ID == 평가 ID.

### 3.3 양성/음성 균형
충돌이 드물어 음성(무충돌) 클립이 압도적으로 많다. 학습/검증 split에서 음성을
`--neg-ratio`(기본 1.0, ≈50:50)로 서브샘플링한다. 클래스 불균형 완화는 소량
데이터에서 재현율 붕괴를 막기 위한 표준 처치다. **테스트 split은 균형을 적용하지
않고** 자연 분포로 평가해 정직한 수치를 얻는다.

### 3.4 분할 (누수 방지)
train/val/test = 80/10/10을 **에피소드 단위**로 나눈다. 같은 에피소드에서 나온
인접 클립이 서로 다른 split에 섞이면 정보 누수로 성능이 과대평가되므로, 에피소드를
통째로 한 split에 배정한다.

### 3.5 출력 포맷 (ShareGPT)
ms-swift가 바로 읽는 ShareGPT JSONL. 표본마다 추론과 **동일한** system/instruction
프롬프트를 포함한다.
```json
{"system":"<twin_view system_prompt>",
 "videos":["/abs/clip.mp4"],
 "conversations":[{"from":"human","value":"<video>\n<twin_view prompt>"},
                  {"from":"gpt","value":"[{\"HH:MM:SS\": [1,2]}]"}]}
```

---

## 4. 모델과 적용 기술

### 4.1 베이스 모델
`Qwen/Qwen3-VL-8B-Instruct`. 추론에서 쓰는 모델과 동일 계열을 학습해야 어댑터가
그대로 서빙에 얹힌다. ms-swift가 Qwen3-VL을 네이티브 지원한다.

### 4.2 LoRA (Low-Rank Adaptation)
- **원리**: 가중치 W를 고정하고, 각 선형층에 저랭크 업데이트 ΔW = B·A(rank r)만
  학습한다. 학습 파라미터가 전체의 ~0.1–1%로 줄어 8B 모델을 40GB 단일 GPU에서
  학습 가능하게 한다. 소량 데이터에서는 full FT보다 **과적합과 파국적 망각에 강하다**.
- **본 설정**: `rank=16, alpha=32(=2r), dropout=0.05`, `target_modules=all-linear`
  (LLM + 멀티모달 merger의 선형층). 출처의 8B VLM LoRA 권장 범위와 일치.
- **비전 인코더 동결(`freeze_vit=true`)**: BEV 픽셀 통계는 일반 자연영상과 크게
  다르지 않고, 학습해야 할 것은 "오버레이/객체를 읽어 JSON으로 보고"하는 LLM 쪽
  추론이다. ViT를 얼리면 메모리·과적합·불안정이 동시에 준다.

### 4.3 메모리 기법
- `bf16` 연산, `gradient_checkpointing=true`, `per_device_batch=1` +
  `grad_accum=16`(유효 배치 ~16).
- **QLoRA 폴백**: 40GB에서도 OOM이면 베이스 가중치를 4-bit(bnb)로 로드(NF4 +
  bf16 compute). 추가 OOM 레버: `VIDEO_MAX_PIXELS`↓ → (VSS N도 함께 낮출 때만)
  `NFRAMES`↓ → `lora_rank=8`.

### 4.4 프레임 샘플링 정합 (가장 흔한 함정)
중요한 건 컨테이너 fps가 아니라 **VLM이 2초당 보는 프레임 수**이고, 이것이
train==infer여야 한다. 영상 컨테이너는 **30fps**지만 좌표 데이터가 1초 5회 갱신이라
**콘텐츠는 실효 5fps**(2초 클립=60 인코드프레임, distinct 위치는 ~10개)다. 본 배포의
VSS는 이 30fps 청크(60프레임 풀)에서 **고정 20프레임**을 샘플하므로, 학습도 클립을
**30fps 그대로 유지**(`build_dataset.slice_clip`이 `-r` 미지정 → 동일 60프레임 풀)한 뒤
`NFRAMES=20`으로 같은 풀에서 20장을 뽑는다. 두 샘플러가 동일 풀의 같은 ~10 distinct
위치를 덮으므로 정합하며, 콘텐츠 중복(각 위치 ~2회) 덕에 미세 인덱스 차이도 무의미하다.
(학습만 다운샘플(예: 클립을 5fps로 재인코드)하면 풀이 달라져 train/infer 불일치가
발생하므로 금지.)

### 4.5 샘플링 레이트(Hz)와 observability — 탐지의 물리적 상한
좌표 추출 Hz는 충돌 탐지 실효성을 좌우한다. **충돌은 렌더된 프레임에 나타나야만 탐지
가능**하고, 프레임은 콘텐츠 샘플 instants {t0+k/f}의 스냅샷이다. 참 overlap 윈도우(두
객체가 contact distance 이내인 구간, 길이 τ)가 어떤 샘플 instant도 포함하지 않으면 그
충돌은 영상에 **아예 안 나타나** 어떤 모델도 못 잡는다. 즉 rate f의 **recall 상한 ≈ τ가
샘플 간격(1/f)을 포함할 확률 = min(1, τ·f)**.

- τ=100ms면 5Hz→0.5, 10Hz→1.0. τ=66ms면 5Hz에서 다수 누락, 10Hz에서 포착(실측 확인).
- 빠른 객체(짧은 τ)일수록 높은 Hz 필요.

**현실 Hz 근거(조사)**: 카메라 기반 좌표 추출은 ~1–30Hz. **10Hz는 확립된 표준**
(NGSIM 차량궤적 데이터셋=10Hz), 저가 트래픽 카메라 ~1Hz, 모던 트래커(ByteTrack ~30fps,
BoT-SORT 30fps@Jetson Orin Nano, S-YOFEO 56fps)는 10–30Hz 달성. → **5Hz·10Hz 모두
현실적**이며 10Hz가 N=20 예산(20프레임/2초=10Hz)과도 정합.

**observability 측정**(`utils/observability.py`): 고레이트 trace(TraceRecorder 30Hz)를
참값으로, 시뮬레이터와 **동일한 contact 규칙**(수평거리 < `collision_distance`=2.2·radius)을
오프라인 복제해 객체쌍별 참 overlap 윈도우와 τ를 복원하고, rate f별 observable 비율(=상한)
및 `min(1,τ·f)` 교차검증을 산출한다. 이 상한과 실제 탐지 F1을 비교하면 성능 격차가
**샘플링 한계 탓인지 모델 한계 탓인지** 분리된다.

### 4.6 5Hz vs 10Hz 실험 설계
1. **10Hz로 캡처**(좌표 갱신 10Hz) + 30Hz trace 동시 기록(observability 참값).
2. `build_dataset --content-hz`로 10Hz(native)·5Hz(decimate) **두 데이터셋**을 동일
   에피소드/충돌에서 생성(깨끗한 A/B; VLM은 두 경우 모두 20프레임 샘플).
3. 각각 LoRA 학습 → test 클립에서 STRICT/RELAXED F1 비교(§7).
4. `observability.py`로 5/10Hz 상한을 뽑아 실측 F1과 대조.
> 주의: 현재 5Hz 데이터셋 라벨은 collisions.csv 원본을 그대로 쓴다(5Hz에서 관측
> 불가해진 충돌이 음성처럼 보일 수 있음). 그 영향은 observability로 정량화하며,
> "관측 가능 라벨만" 재생성하는 거리기반 라벨링은 후속 고도화로 이월.

---

## 5. 하이퍼파라미터 근거 요약

| 항목 | 값 | 근거 |
|---|---|---|
| lora_rank / alpha | 16 / 32 | 8B VLM LoRA 표준 범위, alpha=2·rank |
| lora_dropout | 0.05 | 소량 데이터 정규화 |
| learning_rate | 1e-4 | LoRA 어댑터 일반 권장(1e-4~2e-4) |
| epochs | 2 | 소량 데이터 과적합 방지, val로 best 선택 |
| eff. batch | ~16 | bs1×accum16, 단일40GB 안정 |
| nframes | 20 | **추론 VSS N=20과 정합** |
| scheduler | cosine, warmup 0.05 | 안정적 수렴 |
| freeze_vit | true | 메모리·과적합 완화 |

---

## 6. 학습 절차

1. **데이터 빌드**: `python -m utils.build_dataset --episodes-dir … --out-dir DATA --nframes 20`
2. **위생(overfit) 검증**: 50클립 subset을 epoch↑로 학습 → train loss≈0 / 지표≈100%.
   파이프라인(데이터·프롬프트·프레임 배선)이 옳다는 것을 먼저 확인한다.
   (`qwen3vl_lora_swift.sh` 하단 주석 참조)
3. **본 학습**: `bash training/qwen3vl_lora_swift.sh` — val F1 곡선으로 과적합 모니터,
   epoch별 best 체크포인트 선택.
4. **머지/서빙**: `swift export --adapters … --merge_lora true` 후 추론 서버에 탑재.

---

## 7. 평가 방법론 (`utils/compare_results.py --clips-gt … --clips-pred …`)

held-out 테스트 클립에 **base와 LoRA를 각각** 추론시켜 두 지표를 비교한다.

- **STRICT**: HH:MM:SS별 **객체ID 집합 완전일치**만 TP(기존 `calculate_metrics`
  재사용, 클립ID로 타임스탬프를 namespacing). 가장 엄격한 핵심 지표.
- **RELAXED**: 클립당 **충돌 유무 이진** precision/recall/f1/accuracy. ID는 틀려도
  "충돌이 있었다"를 잡았는지 보는 보조 지표.
- **OOD 점검**: 실제 GT 4영상(`compare_results.py`의 하드코딩 GT)으로 base/LoRA를
  비교해 sim→real 일반화를 본다.
- 여러 결과의 평균은 `utils/calculate_average_metrics.py`로 집계(엄격+완화 모두).

성공 기준: 테스트에서 **LoRA가 base 대비 STRICT/RELAXED F1 모두 향상**, OOD에서도
열화 없음.

**Hz 비교**: 같은 평가를 5Hz·10Hz 모델에 각각 적용(`--label 5hz|10hz`)하고,
`observability.py`의 rate별 상한과 대조한다. 10Hz가 5Hz보다 RELAXED recall에서 의미 있게
높고 그 상한도 더 높다면, 더 높은 Hz의 좌표 추출이 실효성에 필요함을 보이는 근거가 된다.

---

## 8. 한계와 향후 과제

- 소량(1–2K) → 일반화 제한. rank16·비전동결·epoch↓로 완화하나 한계 존재.
- sim→real 도메인 갭: OOD 점검으로 측정만 가능, 보장 아님.
- 시각 해상도 2초·초 단위로 제한 → 더 정밀한 시점 추정은 후속 temporal grounding
  (예: VTG-LLM) 과제.
- ms-swift 플래그/프레임 설정은 설치 버전·VSS 배포에 의존 → 스크립트 주석과
  파라미터로 명시.

---

## 9. 참고자료 (2026.6 기준)

- ms-swift (Qwen3-VL 네이티브 학습): https://github.com/modelscope/ms-swift ,
  https://qwen.readthedocs.io/en/latest/training/ms_swift.html
- Qwen3-VL 8B 파인튜닝 튜토리얼: https://www.datacamp.com/tutorial/fine-tuning-qwen3-vl-8b
- VLM LoRA 데이터 규모·하이퍼파라미터: https://huggingface.co/learn/cookbook/en/fine_tuning_vlm_trl
- LoRA 원논문: Hu et al., "LoRA: Low-Rank Adaptation of Large Language Models" (2021)
- QLoRA: Dettmers et al., "QLoRA: Efficient Finetuning of Quantized LLMs" (2023)
- LLaMA-Factory (대안 프레임워크): https://github.com/hiyouga/LLaMA-Factory
- Temporal grounding (후속): VTG-LLM https://github.com/gyxxyg/VTG-LLM
