# L40 환경 구축 가이드 (데이터 스테이징 → 데이터셋 빌드 → 학습)

L40에서 이 repo를 `git pull` 받아 사용하는 전제의 셋업 절차. 데이터는 로컬 복사가 아니라
**minIO 데이터 레이크에서 스테이징**한다 (`s3://time-travel-summarization/episodes/<run>/`).

## 0. 전제 조건

| 항목 | 요구 | 확인 명령 |
|------|------|-----------|
| Python | 3.10+ | `python3 --version` |
| CUDA GPU | L40 (LoRA 학습용) | `nvidia-smi` |
| ffmpeg | build_dataset 클립 슬라이싱에 필수 | `ffmpeg -version` |
| minIO 접근 | 아웃바운드 HTTPS | `curl -sS -m 5 https://api.minio.mobilex.kr/minio/health/live -o /dev/null -w "%{http_code}\n"` → 200 |

## 1. 저장소 + 가상환경

```bash
git clone <repo-url> ttsum && cd ttsum   # 또는 기존 clone에서 git pull
cd source/extensions/gist.netai.time_travel_summarization

python3 -m venv .venv-l40
source .venv-l40/bin/activate
pip install -r gist/netai/time_travel_summarization/VLM_server/l40/requirements.txt
```

## 2. minIO 자격증명 (.env)

`gist/netai/time_travel_summarization/VLM_server/l40/env.example`을 복사해 값 기입:

```bash
cp gist/netai/time_travel_summarization/VLM_server/l40/env.example .env.l40
# MINIO_ACCESS_KEY / MINIO_SECRET_KEY 채우기 (Windows 쪽 확장 .env와 동일 값)
set -a; source .env.l40; set +a
```

## 3. 에피소드 스테이징 (레이크 → 로컬)

```bash
python3 gist/netai/time_travel_summarization/VLM_server/l40/stage_episodes.py \
  --prefix episodes/prod-20260707 --out ~/ttsum-data/episodes/prod-20260707
```

- `_run_manifest.json`까지 내려받아 생성 조건(커밋·시드·에피소드 목록)을 함께 보존한다.
- 재실행 시 크기가 같은 파일은 건너뛴다(중단 후 이어받기 가능).
- 전송량·처리량이 로그로 남는다 → minIO 처리량 검증(한계 2번) 실측 자료로 기록.

## 4. 데이터셋 빌드 (반드시 L40에서 — jsonl에 절대경로가 박힘)

```bash
python3 -m gist.netai.time_travel_summarization.utils.build_dataset \
  --episodes-dir ~/ttsum-data/episodes/prod-20260707 \
  --out-dir ~/ttsum-data/datasets/bev-collision-v1 \
  --content-hz 10
```

- 60/30fps 원본과 무관하게 `--content-hz 10`으로 2초 클립 = 20프레임(NFRAMES 일치).
- 여러 run을 합쳐 빌드하지 말 것 — 에피소드 ID가 run 간 충돌한다(ep_0000끼리 덮어씀).
  run 하나씩 별도 `--out-dir`로 빌드하거나, 합치기 전에 build_dataset의 episode_id에
  run 접두어를 붙이는 수정이 선행돼야 한다.

## 5. 학습 (training/ 스크립트 사용)

```bash
pip install "ms-swift" qwen-vl-utils decord   # + 최신 transformers/accelerate (ms-swift 3.x 기준)

# ① overfit 게이트 (본학습 전 필수 — 파이프라인 배선 검증)
#    ~50클립으로 train loss ≈ 0 확인. 명령은 training/qwen3vl_lora_swift.sh 하단 주석 참조.
# ② 본학습
bash gist/netai/time_travel_summarization/training/qwen3vl_lora_swift.sh
# ③ 평가 — precision/recall 분리 보고 (50:50 균형이 prior를 왜곡하므로)
bash gist/netai/time_travel_summarization/training/run_eval.sh
```

방법론·체크리스트: `training/METHODOLOGY.md`, `training/VERIFICATION_CHECKLIST.md`.

## 참고: 기존 rsync 흐름과의 관계

`training/remote_train.sh`는 "로컬에서 rsync로 밀어넣는" 구식 흐름이다. 이 가이드의
레이크 스테이징 흐름이 표준이며, remote_train.sh는 레이크 장애 시 폴백으로만 유지.
