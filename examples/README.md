# 예시 데이터 — 충돌 에피소드, 좌표 교란 재연, 위상분해 클립

발표 자료와 README 그림에 쓰기 위해 실험 산출물에서 뽑아 둔 소량의 예시다. 전부
현행 세대(regime3, 모델 lora_v5) 산출물이며, 수치 인용의 정본은 `experiments/README.md`다.
용량을 줄이기 위해 교란 재연 영상은 충돌 구간 10초만 잘라 재인코딩했고(원본은 30초),
나머지는 원본 그대로다. 총 약 14MB.

## 1. `collision_episode/` — 물리 시뮬레이션 충돌 에피소드 한 개 (원본 데이터 형식)

생산런 `prod-20260817-regime3` r1의 `ep_0005`. 교란 실험의 held-out 45쌍 중 하나이며,
아래 `perturbation/`의 모든 조건이 이 에피소드의 좌표를 오염시켜 재연한 것이다.

| 파일 | 내용 |
|---|---|
| `_video_0005.mp4` | physics 렌더 원본 30초, 720x480, 30fps, BEV 카메라, ID 마커와 시각 오버레이 포함 |
| `_trace_0005.csv` | 30Hz 좌표 기록 `timestamp,objid,x,y,z`(콜라이더 중심, collider-trace-v1). 재연과 교란의 입력 |
| `collisions_20260817T140354.csv` | 물리 엔진이 기록한 접촉 사건. `kind=object`가 객체 간 충돌(정답), `kind=wall`은 벽 접촉 |
| `_video_0005.meta.json` | 캡처 메타(시작 시각, 접촉거리 59.99cm 등) |
| `pair_gt.json` | 교란 실험이 쓰는 정답표. `gt_events`의 키는 하루 기준 초(예: 27888 = 07:44:48), 값은 당사자 라벨 |
| `vlm_output_physics.json` | 원본 렌더를 VLM에 넣은 결과(2초 청크 15회) |
| `vlm_output_replay.json` | 같은 좌표를 재연 렌더한 영상의 VLM 결과(clean 베이스라인) |

정답 충돌 5건(시작 기준 초, 당사자): 12초 [1,2], 13초 [3,4], 18초 [2,4], 22초 [1,3], 24초 [2,3].

## 2. `near_miss_episode/` — 접촉 없는 스침 안무 에피소드 한 개

`nm4-gap113-20260821` 런의 `ep_0000`(4객체 2쌍 swerve 안무, gap 113cm). 파일 구성은
1번과 같고 `collisions_*.csv`는 헤더만 있다(접촉 0건이 설계 조건). 위상분해의 near_miss
클립과 frozen-near-miss 암이 이런 에피소드에서 잘려 나온다.

## 3. `perturbation/<조건>/` — 같은 에피소드를 조건별로 오염시켜 재연한 영상

각 디렉토리에 `replay_9s-19s.mp4`(재연 렌더의 9~19초 구간, 정답 충돌 12초 [1,2], 13초
[3,4], 18초 [2,4]가 들어 있음), `replay.meta.json`, `vlm_output.json`(30초 전체의 VLM
결과)이 있다. `clean/`은 교란 없는 재연(베이스라인). 디렉토리 이름은 산출물의 조건 이름
그대로이며, 리포트 표기와 다음처럼 대응한다.

| 디렉토리 | 리포트 표기 | 주입 내용 |
|---|---|---|
| `g10`, `g25`, `g50` | g10 / g25 / g50 | x, z 좌표에 σ = 10 / 25 / 50cm 가우시안 노이즈 |
| `switch` | switch | 첫 사건 3초 전 당사자 1명과 비당사자 1명의 ID 영구 교환 |
| `frag` | new-id | 충돌 참가 객체를 [−4초, −2초) 구간에서 소멸시킨 뒤 새 ID로 복귀 |
| `frag-sameid` | new-id-sameid | 같은 소멸, ID 유지 |
| `frag-inclip`, `frag-inclip-sameid` | new-id-inclip (v1) | 교체 장면을 충돌 청크 안에 배치(정렬 방식, v1) |
| `frag-inclip2`, `frag-inclip2-sameid` | new-id-inclip2 (v2) | 결손 [−2.5초, −0.5초), 재출현 0.5초 뒤 충돌 |
| `occ-hold`, `occ-linear`, `occ-extrap` | occ-hold / occ-linear / (제외 조건) | 사건 ±1.5초 결손을 정지 좌표 / 직선 보간 / 등속 외삽으로 채움. extrap은 화면 양상 불성립으로 리포트에서 제외 |
| `dsr20`, `dsr10`, `dsr5`, `dsr2`, `dsr1` | dsr20 ~ dsr1 | 객체별 좌표를 20 / 10 / 5 / 2 / 1Hz로만 채택 |

같은 시각의 프레임을 조건별로 나란히 놓으면(예: 3.2초 지점 = 원본 12초) 오염이 화면에
어떻게 나타나는지 비교할 수 있다.

## 4. `phase/<조건>/` — 위상분해 클립(2초)과 채점 결과

국면 절제 격자와 반경 계열은 전부 **같은 사건**(`ep_r3_0043`, obj002-obj003, 접촉
15:58:25.116)에서 잘라 조건 간 비교가 되게 골랐다. `scores.jsonl`에 각 클립의 VLM 채점
행이 있다(`spoke`가 발화 여부, `content`가 원문 답).

| 디렉토리 | 조건 | 이 예시 클립의 발화 |
|---|---|---|
| `full` | 접촉 ±1초 온전한 클립 | 발화(15:58:26 [2,3]) |
| `no_aftermath` | 접촉이 끝나기 전 2초 | 침묵 |
| `no_approach` | 접촉 시작 직후 2초 | 침묵 |
| `no_contact` | 접촉 구간(d ≤ 90cm) 절제 후 이어붙임 | 침묵 |
| `no_contact-plateau`, `no_contact-70`, `no_contact-80`, `no_contact-120` | 절제 반경 계열 | plateau, 70, 80은 발화, 120은 침묵 — 반경 절벽의 한 사례 |
| `approach_only` | 접촉 직전 2초(접촉 없음) | 발화(다른 쌍 [3,4]를 지목) |
| `aftermath_only` | 접촉 끝난 직후 2초 | 침묵 |
| `control` | 무관 구간 | 침묵 |
| `approach_only-90`, `aftermath_only-90`, `winpos-m15`, `winpos-p10` | 국면 × 거리, 창 위치 스윕(채점 행 없음) | — |
| `near_miss` | gap 113 스침, 최근접 ±1초 | 침묵 |
| `near_stop` | 정지 거리 120 저작 클립(접근 0.5 + 정지 1.0 + 이탈 0.5초) | 침묵 |
| `frozen-contact`, `frozen-near-miss`, `frozen-near-stop`, `frozen-control` | 정지 프레임 프로브 | contact만 발화 |

한 클립의 결과는 조건의 발화율이 아니다 — 조건별 발화율은 `experiments/README.md` 4절을
본다.

## 출처와 재현

- 에피소드 원본: L40 `artifacts/episodes/prod-20260817-regime3/r1/ep_0005`, `artifacts/episodes/nm4-gap113-20260821/ep_0000`
- 교란 재연과 VLM 결과: 로컬 `artifacts/perturb_eval_v3/{replays,infer}`, clean은 `artifacts/replay_fidelity_v3`
- 위상분해 클립과 채점: 로컬 `artifacts/phase_clips_{regime3,53c,nm113_r2gate,nearstop120_r3,frozen_r3}`, `artifacts/phase_score_*_v5`
- 재연 영상 자르기: `ffmpeg -ss 9 -t 10 -an -c:v libx264 -crf 26 -preset slow -pix_fmt yuv420p`
