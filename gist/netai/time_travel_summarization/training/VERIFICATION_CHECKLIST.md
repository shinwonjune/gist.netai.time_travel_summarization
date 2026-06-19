# BEV 충돌 LoRA PoC — 검증 체크리스트

HANDOFF §4 + 추가 검증 항목 통합. 순서대로 진행. (코드 정합은 리뷰로 확인됨 — 남은 건 Kit·하드웨어 검증.)

## A. 데이터 생성 정합 (가장 중요)
- [ ] **A1. Kit 정합 수동 대조** — physics 모드에서 오버레이 시계가 wall-clock으로 전진하는지 + 두 객체가 닿는 클립 라벨에 그 객체ID·HH:MM:SS가 정확히 들어갔는지 눈으로(수 개).
- [ ] **A2. physics 영상 wall-clock ↔ 라벨 wall-clock 일치** ⭐ — `build_dataset`는 *클립=video-time*(i·2s), *라벨=wall-clock*(capture_start 기준)으로 매핑하므로 **video 1초 == wall-clock 1초(실시간 페이싱)** 이어야 정합. 측정: 알려진 충돌 1건의 (a) 오버레이에 찍힌 시각, (b) `collisions_*.csv` 시각, (c) 그 충돌이 들어간 클립의 video-time 구간 — 셋이 일치하는지. 드리프트 시 라벨이 옆 클립으로 새는지 확인. **headless에서 특히 위험**(렌더가 실시간보다 느리면 video-time이 wall-clock보다 뒤처짐).
- [ ] **A3. objid 라벨 일치** — 오버레이 숫자 = sidecar `objid_to_label` = 평가 GT 정수. (코드상 정규식 `(\d+)$`로 동일 확인됨; 실제 캡처물로 재확인.)

## B. headless 생성·자동화
- [ ] **B1. headless 비디오 검증** — `automation/run_headless.sh`로 1 에피소드 → 오프스크린 render product 경로가 정상 프레임·오버레이·`collisions_*.csv`·trace(30Hz)를 내는지(영상 재생해 눈으로). *render product/`app.update` 렌더 타이밍은 하드웨어 첫 검증 필요.*
- [ ] **B2. 동영상 생성 자동화 동작** — `automation/generate_episodes.py`가 무인으로 객체배치→wander→capture→`ep_XXXX/` 정리까지 배치로 도는지(2–3 에피소드 스모크 → 수십 개).
- [ ] **B3. 영상 길이·개수 확정** — 아래 D 권장치 + 스모크로 "에피소드당 양성 클립 수" 측정해 총 에피소드 수 보정.

## C. 데이터셋·학습·평가
- [ ] **C1. build_dataset 출력 점검** — 클립수/경계(0,2,4..)/양음성비/split 누수0/ShareGPT 스키마/`test_gt.json`. ⚠️ **에피소드 ≥3** 이어야 test/val 생성.
- [ ] **C2. NFRAMES=20 확인** — VSS 배포의 `VLM_DEFAULT_NUM_FRAMES_PER_CHUNK` 실제값과 일치(train==infer 프레임 정합 핵심).
- [ ] **C3. observability** — 30Hz trace로 5/10Hz recall 상한 산출(샘플링 한계 vs 모델 한계 분리).
- [ ] **C4. 학습 위생** — ~50클립 overfit→~100% 도달 후 본학습 val F1 곡선(과적합 모니터).
- [ ] **C5. 평가** — held-out STRICT/RELAXED **base vs LoRA** + 실제 GT 4영상 OOD.

## D. 영상 길이·개수 권장 (PoC ~1–2K 클립)
- 클립 = 2초. 1–2K 클립 ≈ **33–67분** 분량(균형 후 기준).
- 권장: **60–90초 에피소드 30–50개**, 시드·객체수(4–6)·속도 변주로 다양성. 에피소드 단위 split이라 **최소 10개+** 필요(다양성·test/val 확보).
- 양성(충돌) 클립 수는 충돌 빈도에 의존 → **스모크 2–3개로 에피소드당 양성 수를 먼저 측정**한 뒤 목표 클립수에 맞춰 에피소드 수 조정. 다객체·고속일수록 충돌↑.

## E. L40 (외부 서버, SSH) 학습 운영
데이터 생성 = **로컬**(Omniverse Kit 필요), 학습·추론 = **원격 L40**. 로컬에서 SSH로 전 과정 제어 가능.
1. 로컬 Kit으로 에피소드 생성 → 로컬 `build_dataset`로 `clips/` + `train/val/test.jsonl` 생성.
2. **데이터 전송** — `rsync -avz dataset/ user@L40:/path/dataset/`(clips 용량 큼) 또는 minIO 경유 업/다운로드.
3. **원격 학습** — ssh 접속 후 `tmux`(또는 `nohup`)에서 `training/qwen3vl_lora_swift.sh` 실행(연결 끊겨도 유지). 체크포인트·로그는 서버.
4. **로컬 제어** — VS Code **Remote-SSH** 또는 `ssh + tmux`로 로컬에서 구동/모니터. TensorBoard는 `ssh -L 6006:localhost:6006`로 포트포워딩.
5. **평가** — 같은 L40에서 `swift infer`(base & LoRA) → 예측 json → `utils/compare_results.py`.

- [ ] **E1. 데이터 전송 경로 확정** — rsync vs minIO.
- [ ] **E2. 서버 준비** — ms-swift 설치 + Qwen3-VL-8B 가중치 + ~50클립 overfit 스모크.
- [ ] **E3. VRAM 점검** — ⚠️ 기존 vLLM 추론 서버가 같은 L40 40GB를 점유 중이면 학습과 **동시 불가** → 학습 시 추론 서버 내리거나 GPU 분리/시간 분할. LoRA bf16 OOM이면 QLoRA 4bit.

## 코드 리뷰에서 나온 리스크(데이터 생성 시 유의)
- **R2(High)**: A2와 동일 — 실시간 페이싱 의존(특히 headless 드리프트).
- **R1(Med)**: 에피소드 <3이면 test/val 없음(C1).
- **R3(Low)**: 충돌이 2초/정수초 경계에 걸리면 ±1클립 오배정 가능(초단위 라벨).
- **R4(Low)**: ms-swift per-sample `system` + `--system` 중복 가능 → 동작 확인.
