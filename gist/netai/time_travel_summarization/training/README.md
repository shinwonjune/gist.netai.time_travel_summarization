# training — Qwen3-VL-8B LoRA 파인튜닝 (ms-swift)

## 역할과 위치

`training/`은 `utils/build_dataset.py`가 만든 ShareGPT 데이터셋으로
Qwen3-VL-8B-Instruct에 LoRA를 파인튜닝하고(ms-swift `swift sft`), 평가하는
(`swift infer` + `utils/compare_results.py`) 스크립트/설정/문서를 담는다.
학습 자체는 GPU가 필요해 보통 원격 GPU 서버에서 돌리므로, 로컬에서
원격으로 잡을 올리는 오케스트레이션 스크립트(`remote_train.sh`)도 함께
있다.

## 파이프라인 단계 다이어그램

```
[생성 → 데이터셋]                         [학습]                    [평가]
generate_episodes.py (automation/)    qwen3vl_lora_swift.sh   run_eval.sh
      │                                      ▲                      │
      ▼                                      │                      ├─ swift infer
utils/build_dataset.py ──── train.jsonl ─────┘                      ├─ utils/swift_infer_to_preds.py
      │                     val.jsonl (--eval_strategy epoch)       └─ utils/compare_results.py
      ├─ test.jsonl ─────────────────────────────────────────────────────┘ (--clips-gt test_gt.json)
      └─ system_prompt.txt (= --system, 학습/평가 공통)

remote_train.sh
(로컬 → GPU 서버: rsync 코드+에피소드 → 원격에서 build_dataset 실행 → tmux로
 qwen3vl_lora_swift.sh 실행, SSH 끊겨도 지속)
```

## 구성 파일

| 파일 | 역할 | 실행 형태 |
|---|---|---|
| `qwen3vl_lora_swift.sh` | LoRA 학습 실행 스크립트(실행 가능한 정본) | bash(`swift sft` 호출) |
| `qwen3vl_lora_swift.yaml` | 위 스크립트와 동일한 설정을 선언적으로 기록한 검토용 사본 | 문서(비실행) |
| `remote_train.sh` | 로컬 → GPU 서버 원격 학습 오케스트레이션(rsync + 원격 build_dataset + tmux 학습) | bash |
| `run_eval.sh` | 학습된 모델(베이스 또는 LoRA 어댑터)을 held-out test로 평가 | bash(`swift infer` + `utils` 호출) |
| `HF_MODEL_CARD.md` | Hugging Face 모델 카드 형식 문서 | Markdown |
| `METHODOLOGY.md` | 파인튜닝 방법론 상세 설명 | Markdown |
| `VERIFICATION_CHECKLIST.md` | 데이터 생성부터 학습까지 검증 체크리스트 | Markdown |

## qwen3vl_lora_swift.sh — 학습 실행

`build_dataset.py`가 만든 `${DATA}/{train,val}.jsonl`과
`${DATA}/system_prompt.txt`를 읽어 `swift sft`를 호출한다.

**프레임 예산 정합**(가장 중요한 제약, 스크립트 상단 주석): VSS/vLLM 추론이
2초 청크마다 정확히 20프레임을 고정 샘플링하므로, 학습도 같은 20프레임으로
맞춰야 한다. `export NFRAMES=20`(clip당 강제 샘플 프레임 수),
`export VIDEO_MAX_PIXELS=$((720 * 480))`(원본 해상도 유지, OOM 시 가장
먼저 낮출 값), `export SIZE_FACTOR=28`(Qwen 패치 정렬 기본값)로 이 정합을
env var를 통해 강제한다.

`swift sft` 주요 플래그(값은 스크립트에 그대로 하드코딩): `--model`
(env `MODEL`, 기본 `Qwen/Qwen3-VL-8B-Instruct`) `--dataset
${DATA}/train.jsonl` `--val_dataset ${DATA}/val.jsonl` `--system`(system_prompt.txt
내용) `--train_type lora` `--lora_rank 16` `--lora_alpha 32`
`--lora_dropout 0.05` `--freeze_vit true` `--target_modules all-linear`
`--torch_dtype bfloat16` `--num_train_epochs 2` `--per_device_train_batch_size 1`
`--per_device_eval_batch_size 1` `--gradient_accumulation_steps 16`
`--learning_rate 1e-4` `--lr_scheduler_type cosine` `--warmup_ratio 0.05`
`--gradient_checkpointing true` `--eval_strategy epoch` `--save_strategy epoch`
`--save_total_limit 2` `--logging_steps 5` `--dataloader_num_workers 4`
`--output_dir ${OUTPUT}`(env, 기본 `artifacts/lora_qwen3vl`) `--seed
${SEED:-42}`.

필수 사전 조건(스크립트 주석): `pip install "ms-swift" qwen-vl-utils decord`
+ 최신 transformers/accelerate, 그리고 데이터셋이 먼저 빌드돼 있어야 한다.
ms-swift 3.x 기준이며, 설치된 버전에서 `swift sft -h`로 플래그명을 재확인
하라는 경고가 스크립트에 명시돼 있다.

학습 끝에 어댑터가 `${OUTPUT}/<checkpoint>`에 저장되고, 서빙용 병합은
`swift export --adapters ${OUTPUT}/<checkpoint> --merge_lora true`로
안내한다. 스크립트 하단에는 주석 처리된 두 가지 참고 블록이 있다:
(1) 40GB GPU에서 bs=1+grad-ckpt로도 OOM이면 쓸 QLoRA(4bit) 대체 플래그와
추가 OOM 완화 순서(VIDEO_MAX_PIXELS 낮추기 → NFRAMES 낮추기(단, VSS N도
같이 낮춰야 train==infer 유지) → lora_rank 8), (2) 본 학습 전에 50개
샘플만으로 30에폭 과적합시켜 파이프라인 배선을 확인하는 sanity check 명령.

`swift sft`가 `--logging_steps 5`마다 남기는 학습/검증 손실 로그를
곡선으로 보면, 위 하이퍼파라미터(`--num_train_epochs 2`,
`--eval_strategy epoch` 등)로 학습이 실제로 수렴하는 형태인지를 확인할 수
있다.

<!-- 이미지 TODO | 파일: images/readme/training-01-loss-curve.png | 촬영: `swift sft` 학습 로그(stdout 또는 `${OUTPUT}/<checkpoint>/trainer_state.json` 등 ms-swift가 남기는 로그)를 TensorBoard 또는 직접 파싱한 손실 값으로 플롯. train loss를 step 단위로, val loss를 epoch 단위(`--eval_strategy epoch`)로 같은 그래프에 겹쳐 그릴 것 | 형식: png -->
![그림 1. LoRA 학습의 train/val 손실 곡선](../../../../images/readme/training-01-loss-curve.png)
*그림 1. `swift sft`가 기록하는 train loss(step 단위)와 val loss(epoch 단위)의 추이.*

val loss가 epoch마다(`--eval_strategy epoch`) 한 점씩만 찍히는 것과 train
loss가 훨씬 촘촘히(`--logging_steps 5`) 찍히는 것이 두 곡선의 표본 밀도
차이로 나타난다는 점, 그리고 `--save_total_limit 2`가 남기는 체크포인트
중 어느 지점이 최종 채택 대상인지가 이 곡선에서 확인해야 할 지점이다.

## qwen3vl_lora_swift.yaml — 선언적 설정 사본

`.sh`가 실행 가능한 정본이고, 이 yaml은 같은 설정을 사람이 검토하기 쉬운
형태로 옮겨 적은 문서다(주석에 "The .sh is the executable source of
truth"라고 명시). 값은 위 `.sh`와 동일하다(아래는 `dataset:` 경로 블록을 생략하고
flow-style로 압축한 발췌이며 원본은 block-style이다):

```yaml
model: Qwen/Qwen3-VL-8B-Instruct
task: collision-event detection on 2s BEV clips (output [{"HH:MM:SS": [ids]}])
video: {nframes: 20, video_max_pixels: 345600, size_factor: 28}
lora: {train_type: lora, rank: 16, alpha: 32, dropout: 0.05,
       target_modules: all-linear, freeze_vit: true}
optimization: {torch_dtype: bfloat16, num_train_epochs: 2,
                per_device_train_batch_size: 1, gradient_accumulation_steps: 16,
                learning_rate: 1.0e-4, lr_scheduler_type: cosine,
                warmup_ratio: 0.05, gradient_checkpointing: true, seed: 42}
eval: {eval_strategy: epoch, save_strategy: epoch, save_total_limit: 2}
qlora_fallback: {quant_method: bnb, quant_bits: 4, bnb_4bit_compute_dtype: bfloat16}
```

## remote_train.sh — 원격(GPU 서버) 학습 오케스트레이션

로컬에서 데이터를 생성(Omniverse Kit)한 뒤, 데이터셋 빌드와 학습은 GPU
서버에서 수행한다. 이유가 스크립트 주석에 명시돼 있다(`remote_train.sh:2-8`
헤더 주석): `build_dataset.py`가 클립의 절대경로를 jsonl에 그대로 써넣기
때문에, 데이터셋은 반드시 그것을 학습에 쓸 머신에서 빌드해야 한다. 그래서
이 스크립트는 3단계를 한 번에 수행한다: (1) `rsync`로 레포 전체 + 로컬
원본 에피소드를 GPU 서버로 전송(`.git`/`__pycache__`/`artifacts` 제외),
(2) 원격에서 `python -m utils.build_dataset`로 데이터셋 빌드, (3) `tmux
new-session -d`로 학습을 분리 세션에서 실행(SSH 연결이 끊겨도 학습은
계속됨, 로그는 `artifacts/train.log`).

필수 env: `L40_HOST`(ssh 대상, `user@host` 또는 `~/.ssh/config` 별칭).
선택 env: `REMOTE_DIR`(기본 `~/ttsum`) `EPISODES_DIR`(기본
`artifacts/episodes`) `TMUX_SESSION`(기본 `train`) `PRESET`(기본
`twin_view`) `CONTENT_HZ`(샘플링레이트 A/B용, 기본 미지정).

이 스크립트는 `automation/remote_generation.py`의 `JobSpec`/`RESTTransport`
경로와는 별개의, SSH+rsync+tmux 기반 독립 오케스트레이션이다(job API
데몬을 거치지 않는다).

## run_eval.sh — 평가

학습이 끝난 뒤 GPU 서버에서 실행한다. `swift infer`로 held-out
`${DATA}/test.jsonl`에 대해 추론하고(어댑터 경로를 `ADAPTER`로 주면 LoRA,
비워두면 베이스 모델 평가), 결과를 `utils/swift_infer_to_preds.py`로
`preds.json`으로 변환한 뒤 `utils/compare_results.py`로 STRICT(HH:MM:SS별
객체 ID 집합 완전 일치)/RELAXED(클립당 충돌 유무만 보는 이진 판정) 지표를
계산한다(`compare_results.py:13`). 이 STRICT/RELAXED는 `automation/`의
채점 규약(`match_events`의 `tol=0`/`tol=1` 시각 허용 오차)과는 별개로,
`compare_results.py`가 클립 단위 평가를 위해 정의한 독립된 기준이다.

정확한 호출 체인:
```bash
swift infer --model "${MODEL}" "${ADAPTER_ARGS[@]}" --val_dataset "${DATA}/test.jsonl" \
    --system "${SYSTEM}" --result_path "${INFER}" --max_new_tokens 256 --temperature 0

python -m utils.swift_infer_to_preds \
    --test-jsonl "${DATA}/test.jsonl" --infer-result "${INFER}" --out "${PREDS}"

python -m utils.compare_results \
    --clips-gt "${DATA}/test_gt.json" --clips-pred "${PREDS}" --label "${LABEL}"
```

여기서도 프레임 예산이 학습과 정확히 맞아야 한다는 주석이 반복된다:
`export NFRAMES="${NFRAMES:-20}"`, `export VIDEO_MAX_PIXELS`(기본
720x480). env: `DATA`(`artifacts/dataset`) `MODEL`
(`Qwen/Qwen3-VL-8B-Instruct`) `ADAPTER`(비우면 베이스 모델 평가)
`LABEL`(`model`) `OUT`(`artifacts/eval`). 베이스와 LoRA를 각각 `LABEL=base`
/ `LABEL=lora ADAPTER=...`로 두 번 실행해 비교하는 것을 전제로 한다.

## 학습 데이터 계약

이 디렉터리의 스크립트/yaml에 명시된 값만 나열한다(추정 없음).

- **2초 청크** — `utils/build_dataset.py`의 클립 길이(`--clip-sec` 기본
  2.0)와 `training/qwen3vl_lora_swift.sh`/`run_eval.sh`가 이를 전제로
  system prompt와 프레임 예산을 맞춘다.
- **20프레임/클립** — `qwen3vl_lora_swift.sh`의 `NFRAMES=20`,
  `qwen3vl_lora_swift.yaml`의 `video.nframes: 20`, `run_eval.sh`의
  `NFRAMES="${NFRAMES:-20}"`. 실제 강제는 vLLM 서빙 플래그
  (`--media-io-kwargs '{"video": {"num_frames": 20}}'`, `utils/vllm_client.py`
  문서화)가 하고, 학습 쪽은 이 값과 일치시키는 것이 계약이다.
- **twin_view 프리셋** — `remote_train.sh`의 `PRESET="${PRESET:-twin_view}"`
  가 `utils/build_dataset.py --preset`으로 전달되어 `vlm_client/prompts.py`
  의 `PROMPTS["twin_view"]`(prompt + system_prompt)를 학습/평가/추론
  전 구간에서 동일하게 쓴다.
- **VIDEO_MAX_PIXELS=345600(720x480)**, **SIZE_FACTOR=28** —
  `qwen3vl_lora_swift.sh`/`qwen3vl_lora_swift.yaml`.
- **출력 포맷** — `[{"HH:MM:SS": [ids]}]`(`qwen3vl_lora_swift.yaml`의
  `task` 필드, `utils/build_dataset.py`가 만드는 `conversations[1].value`
  와 동일 스키마).

위 계약들은 모두 "모델이 실제로 보는 입력이 무엇인가"를 규정하는
값이므로, 학습 데이터 클립 한 장을 직접 보면 이 계약이 화면에서 어떻게
구현되는지 확인할 수 있다.

<!-- 이미지 TODO | 파일: images/readme/training-02-clip-frame.png | 촬영: `artifacts/dataset/train.jsonl`의 한 클립(또는 원본 `ep_NNNN/_video.mp4`의 2초 청크)에서 프레임 1장을 추출. twin_view 프리셋이 그리는 마커(BEV 좌표를 화면에 표시하는 오버레이)가 포함된 프레임이어야 하며, 720x480 해상도를 유지한 원본 프레임을 그대로 캡처 | 형식: png -->
![그림 2. twin_view 프롬프트가 보는 학습 데이터 클립 한 프레임](../../../../images/readme/training-02-clip-frame.png)
*그림 2. NFRAMES=20, VIDEO_MAX_PIXELS=345600(720x480) 제약 아래 모델에 그대로 입력되는 프레임 — 마커가 twin_view 프리셋의 시각 정보를 담고 있다.*

이 프레임에 찍힌 마커의 위치와 개수가 같은 시각 trace CSV의 객체 좌표
개수와 일치하는지를 대조하면, `build_dataset.py`가 만드는 시각 정합이
실제로 지켜지고 있는지를 육안으로도 확인할 수 있다.

## 참고 문서

세 문서 모두 git 추적 파일이다.

- **HF_MODEL_CARD.md** — Hugging Face 모델 카드 형식. 목차: Task,
  Evaluation, Training, How to use(Adapter inference / Merge+vLLM
  serving), Limitations, License & attribution.
- **METHODOLOGY.md** — LoRA 파인튜닝 방법론 전체 설명(한국어). 목차:
  1. 문제 정의, 2. 추론 파이프라인이 학습 설계를 결정한다(핵심 원칙),
  3. 데이터셋 구성(`utils/build_dataset.py`) — 3.1 시각 정합, 3.2 클립
  슬라이싱과 라벨링, 3.3 양성/음성 균형, 3.4 분할(누수 방지), 3.5 출력
  포맷(ShareGPT), 4. 모델과 적용 기술 — 4.1 베이스 모델, 4.2 LoRA,
  4.3 메모리 기법, 4.4 프레임 샘플링 정합(가장 흔한 함정), 4.5 샘플링
  레이트(Hz)와 observability(탐지의 물리적 상한), 4.6 5Hz vs 10Hz 실험
  설계, 5. 하이퍼파라미터 근거 요약, 6. 학습 절차, 7. 평가 방법론
  (`utils/compare_results.py`), 8. 한계와 향후 과제, 9. 참고자료.
- **VERIFICATION_CHECKLIST.md** — 데이터 생성부터 학습 운영까지의 검증
  체크리스트. 목차: A. 데이터 생성 정합(가장 중요), B. headless
  생성/자동화, C. 데이터셋/학습/평가, D. 영상 길이/개수 권장(PoC
  ~1~2K 클립), E. GPU 서버(외부, SSH) 학습 운영, 코드 리뷰에서 나온
  리스크(데이터 생성 시 유의).

이 세 문서의 구체적 수치(정확도, 클립 수 등)는 이 README에 옮기지
않는다 — 실험 결과가 갱신되면 문서 자체를 참조할 것.

## 실행 예

```bash
# 1) 데이터셋 빌드 (레포 루트에서, 로컬 또는 GPU 서버)
python -m gist.netai.time_travel_summarization.utils.build_dataset \
    --episodes-dir artifacts/episodes --out-dir artifacts/dataset \
    --preset twin_view --nframes 20

# 2) 학습 (build_dataset과 같은 머신에서 — 클립 경로가 절대경로로 박히므로)
DATA=artifacts/dataset OUTPUT=artifacts/lora_qwen3vl \
    bash gist/netai/time_travel_summarization/training/qwen3vl_lora_swift.sh

# 2') 또는 로컬에서 원격(GPU 서버)으로 한 번에
L40_HOST=me@l40 EPISODES_DIR=artifacts/episodes CONTENT_HZ=10 \
    bash gist/netai/time_travel_summarization/training/remote_train.sh

# 3) 평가 (base와 lora 각각)
DATA=artifacts/dataset LABEL=base \
    bash gist/netai/time_travel_summarization/training/run_eval.sh
DATA=artifacts/dataset LABEL=lora ADAPTER=artifacts/lora_qwen3vl/checkpoint-XXX \
    bash gist/netai/time_travel_summarization/training/run_eval.sh
```

## 테스트

이 디렉터리 자체에는 전용 유닛 테스트가 없다. 학습 데이터가 소비하는
`utils/build_dataset.py`와 채점에 쓰이는 `utils/compare_results.py`의
정확성은 `gist/netai/time_travel_summarization/tests/`가 mypy 점진 도입
대상(`pyproject.toml`의 `[tool.mypy] files`에 `utils/build_dataset.py`
포함)과 별개로 검증한다(자세한 목록은 `tests/README.md` 참조). 학습/평가
스크립트 자체(`swift sft`/`swift infer` 호출)는 GPU와 ms-swift 설치가
필요해 유닛 테스트 대상이 아니다.

## 한계

- ms-swift CLI 플래그는 버전 간 달라질 수 있다 — `qwen3vl_lora_swift.sh`
  주석이 "ms-swift 3.x 기준, 설치된 버전에서 `swift sft -h`로 재확인"을
  명시한다.
- `remote_train.sh`/`run_eval.sh`는 `build_dataset.py`가 절대경로를 jsonl
  에 박아 넣는다는 제약 때문에 반드시 학습/평가를 실행할 그 머신에서
  데이터셋을 빌드해야 한다(다른 머신에서 빌드한 데이터셋을 복사해 오면
  클립 경로가 깨진다).
- 프레임 예산(NFRAMES=20)은 vLLM 서빙 기동 플래그와 별도로 맞춰야 하는
  값이라, 서빙 쪽 설정을 바꾸면 이 디렉터리의 스크립트도 함께 갱신해야
  한다(단일 소스가 아니라 세 곳에 중복 명시돼 있음: `qwen3vl_lora_swift.sh`,
  `run_eval.sh`, `utils/build_dataset.py --nframes`는 정보용일 뿐 강제하지
  않음).
