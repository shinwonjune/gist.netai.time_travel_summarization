# perturbation — 좌표 트랙 교란기

## 역할과 위치

`perturbation/`은 파이프라인 강건성 측정(좌표 교란 주입 → 재연 → VLM 추론 →
검출/귀속 지표 분리)의 "교란 주입" 단계를 담당한다. 실제 물리 시뮬레이션으로
생성된 궤적(trace CSV, `physics/trace_recorder.py` 산출물)에, 현실의 좌표
수집 시스템(트래커/센서)이 낼 법한 오류를 사후에 주입해 새로운 trace CSV를
만든다. 이 오염된 trace는 재연(playback) 파이프라인을 그대로 통과해 비디오로
렌더링되고, 그 비디오에 대한 VLM 추론 결과를 원본 GT와 대조해 "좌표 수집
오류가 얼마나 하류(VLM 판단)까지 전파되는가"를 잰다.

이 패키지는 순수 함수만 담는다. 모든 교란 함수는 `rows`(dict 리스트)를
입력받아 새 리스트를 반환하고, GT(물리 엔진이 기록한 정답 충돌 라벨)는 절대
건드리지 않는다 — 입력 좌표만 오염시킨다는 것이 원칙이다(`perturb.py:1-15`
모듈 독스트링). 이
순수성 덕분에 `omni`(Omniverse 런타임)에 의존하지 않고 일반 Python
환경에서 단독 실행과 테스트가 가능하다.

## 구성 파일

| 파일 | 책임 | 핵심 심볼 |
|---|---|---|
| `perturb.py` | trace CSV 입출력 + 5종 교란 변환(gaussian, id_switch, fragmentation, occlusion, downsample) | `load_trace`, `dump_trace`, `gaussian`, `id_switch`, `fragmentation`, `occlusion`, `downsample` |
| `near_stop.py` | "접근하다 접촉 없이 정지 후 이탈"하는 근접-정지 조건 trace를 실측 조각 재배열로 저작하고 검증 | `author_near_stop`, `verify_near_stop`, `infer_hz`, `find_stop_frame` |
| `__init__.py` | 위 심볼들을 패키지 최상위로 재노출 | `__all__` |

## 동작 흐름 — `automation/perturb_eval.py`가 호출하는 방식

이 패키지 자체는 렌더링, 업로드, 추론을 수행하지 않는다. 이를 캠페인 단위로
자동화하는 것은 `automation/perturb_eval.py`이며, 그 안의 `plan_perturbation`
함수가 조건(`CONDITIONS`에 정의된 gaussian/switch/frag/occlusion/downsample
계열)과 한 에피소드(쌍)를 받아 "실행 계획(plan)"과 "GT 재매핑(remap)"을
만든다. 이후 `perturb_rows`가 그 plan을 보고 이 패키지의 함수를 호출한다
(`automation/perturb_eval.py:222-241`).

```
plan_perturbation(cond, pair, label_to_objid)  # 조건별 파라미터 결정(대상 객체와 시각 등)
        │
        ▼
perturb_rows(plan, rows, seed)                 # plan["kind"]에 따라 분기
        │
        ├─ "gaussian"    → perturbation.gaussian(rows, sigma, seed)
        ├─ "downsample"  → perturbation.downsample(rows, hz)
        ├─ "switch"      → perturbation.id_switch(rows, t_s, a, b)
        ├─ "occlusion"   → perturbation.occlusion(rows, obj, t0, t1, policy)  # 이벤트마다 반복
        └─ "frag"        → perturbation.fragmentation(rows, obj, t0, t1, new_id)  # 대상 객체마다 반복
```

`automation/perturb_eval.py`의 `phase_perturb`가 이 결과를 CSV로 저장하고
(`dump_trace`), 이후 단계(`phase_push` → `phase_replay` → `phase_fetch` → `phase_infer`)가
그 CSV를 GPU 서버로 올려 재연 잡을 돌리고, 렌더된 mp4를 로컬로 받아온 뒤 VLM
추론까지 수행한다. 채점은
두 기준으로 이뤄진다 — 원래 GT(교란 없는 정답)와 화면 기준 GT(교란을 GT
에도 반영한 정답, `apply_remap`이 만든다). 이 두 기준의 격차가 "데이터
유래 오류"의 크기다.

`near_stop.py`는 이 자동화 경로에 배선되어 있지 않다(교란 조건이 아니라
위상분해 실험의 near-stop 조건, 즉 "접근 후 무접촉 정지" 클립을 만드는 저작
도구다) — 현재는
`author_near_stop`/`verify_near_stop`를 직접 호출해 근접-정지 조건 trace를
만드는 독립 모듈이며, `perturb.py`의 5종 교란과 달리 기존 trace를 오염시키는
것이 아니라 실측 조각을 재배열해 **새로운 안무 trace를 저작**한다.

## 핵심 설계와 정확한 의미론

### perturb.py의 5종 교란

오류 모델의 근거는 MOT(Multi-Object Tracking) 표준 5대 오류 유형 대조다
(`perturb.py:1-9` 모듈 독스트링에 근거 문서 경로 명시). 각 함수는 행 단위로
정확히 다음을 바꾼다.

- **`gaussian(rows, sigma, seed)`** — 바닥 평면(x, z)에 `N(0, sigma²)`
  독립동일분포(iid) 노이즈를 더한다. y(높이)는 건드리지 않는다 — 객체가
  바닥 위에 있다는 전제 때문이다. 측위 오차(고주파 최악 케이스)를 모사한다.
  `random.Random(seed)`로 만든 전용 RNG를 쓰므로 같은 시드에서 결정적이다.

- **`id_switch(rows, t_s, a, b)`** — 시각 `t_s` 이후의 모든 행에서 objid
  `a`와 `b`를 영구적으로 맞바꾼다. 좌표 값 자체는 전혀 바뀌지 않고 라벨만
  바뀐다 — 트래커의 identity switch(연관 오류)를 모사한다.

- **`fragmentation(rows, obj, t0, t1, new_id)`** — `obj`의 `[t0, t1)`
  구간 행을 통째로 삭제하고, `t1` 이후의 `obj` 행은 `new_id`로 개명한다.
  `new_id=None`이면 개명하지 않는다(`frag-sameid` 대조군) — 결손은 같고
  번호만 유지되므로, 기본 frag와의 차이가 "새 번호(학습 분포 밖 숫자)
  효과"만으로 분리된다. 개명이 없으면 트랙 생존 창이 결손을 덮어 재연
  파이프라인의 결손 인지 despawn(`TTS_DESPAWN_GAP_S`, `app/facade.py`가
  읽는 환경변수)이 발동하지 않으므로, 화면에서 실제로 사라지게 하려면 이
  기능을 함께 켜야 한다(`perturb.py:71-96` 함수 docstring).

- **`occlusion(rows, obj, t0, t1, policy)`** — `obj`의 `[t0, t1)` 구간을
  결손시키고 정책에 따라 채운다. `policy`는 4종: `"drop"`(행 삭제만),
  `"hold"`(마지막 관측 좌표를 복사해 채움), `"linear"`(결손 양끝을 직선
  보간 — 오프라인 후처리 시나리오), `"extrap"`(직전 속도로 등속 연장 —
  온라인 트래커의 칼만 필터 coasting 시나리오, 복귀 시 점프 발생). 채울
  기준(직전 관측)이 없으면 결손을 그대로 유지한다.

- **`downsample(rows, hz)`** — 객체별로 독립적으로, 직전 채택 시각으로부터
  `1/hz`초가 지난 다음 샘플만 채택하는 **시간 기반** 다운샘플이다. 소스
  기록 주기(실측 60Hz)와 무관하게 정확히 목표 Hz를 낸다. 이전 구현은
  "N개마다 하나씩" 방식이라 소스 주기를 30Hz로 잘못 가정했을 때 실측
  주기가 2배로 어긋나는 사고가 있었다(`perturb.py:145-151` 주석).

각 함수는 파일 하단의 `_self_test()`로 자체 검증되며(`python3
perturbation/perturb.py`로 직접 실행 가능), 행 수 보존, 시드 결정성, 정책별
채움 값 등을 assert로 확인한다.

같은 원본 궤적에 세 가지 교란을 각각 적용한 결과를 위에서 본 좌표(x-z)로
나란히 그리면, 각 교란이 좌표에 남기는 흔적이 서로 얼마나 다른지 한눈에
비교할 수 있다.

<!-- 이미지 TODO | 파일: images/readme/perturbation-01-clean-vs-perturbed.png | 플롯: 임의 trace CSV 한 클립에서 객체 하나의 x, z 열을 clean(원본) / gaussian(σ=25 적용) / occlusion(policy=hold, 3초 결손) / fragmentation(new_id 부여, 결손 후 재출현) 네 계열로 각각 그린 x-z 궤적을 한 좌표축에 색으로 구분해 겹쳐 그리거나 2x2 소그림으로 배치. occlusion과 fragmentation 계열은 결손 구간을 점선 또는 빈 구간으로 표시 | 형식: png -->
![그림 1. clean 궤적과 세 교란 적용 결과 비교](../../../../images/readme/perturbation-01-clean-vs-perturbed.png)
*그림 1. 같은 원본 궤적에 gaussian(좌표 흔들림) / occlusion(구간 결손) / fragmentation(결손 + ID 개명)을 각각 적용한 x-z 궤적. gaussian은 경로 전체가 흔들리고, occlusion과 fragmentation은 결손 구간에서 궤적이 끊긴다.*

세 교란 모두 원본 경로의 대략적 형태는 유지하면서 각기 다른 방식으로
정보를 훼손한다는 점 — gaussian은 매 프레임의 좌표 값을, occlusion과
fragmentation은 특정 구간의 존재 자체를 훼손한다는 차이 — 이 플롯에서
드러난다.

### near_stop.py의 저작과 검증

`author_near_stop`은 물리 안무가 아니라 **좌표 저작**으로 근접-정지 시나리오를
만든다. 재연 렌더러는 받은 좌표를 그대로 그리므로, "접근하다 목표 거리에서
멈춰 서 있다가 다시 떠난다"는 시나리오도 물리 엔진에 새 모드를 추가할 필요
없이 **실측 trace 조각의 재배열**만으로 만들 수 있다. 이 방식은 두 가지
이점이 있다 — 좌표 수준에서 직접 저작하므로 정지 거리와 정지 시간이
정확하고(이산 물리 스텝이 만드는 오버슈트가 없다), 감속 캡이 만들던 경계
떨림도 원천적으로 생기지 않는다(`near_stop.py:1-22`).

`author_near_stop(rows, a, b, stop_distance, hold_s, ...)`의 저작 과정은
세 단계다.

1. **접근** — 실측 near-miss 에피소드에서 두 객체(`a`, `b`)가 서로
   다가가는 구간을 그대로 쓴다(운동 질감은 실측 그대로). 중심 거리가
   `stop_distance` 이하로 내려가는 첫 프레임에서 자른다
   (`find_stop_frame`).
2. **정지** — 절단 지점 좌표를 원본 표집률(`infer_hz`가 trace의 프레임
   간격 중앙값에서 유도)로 `hold_s`초만큼 반복한다. 접촉이 없으므로 반동은
   없다.
3. **이탈** — 기본값은 **각 객체 자신의 접근 구간을 역순으로 뒤집어 부호를
   반전**한 스텝을 이어 붙인다(들어온 길을 그대로 되짚어 나간다). 원본
   trace의 "정지 이후 구간"을 그대로 쓰지 않는 이유는, 그 원료가 충돌
   에피소드라서 그 구간에는 반동, pause, 방향 재추첨 같은 충돌 안무가 이미
   들어 있고, 이를 그대로 쓰면 멀어지다가 다시 서로에게 되돌아오는 현상이
   실측됐기 때문이다(121 → 125 → 117로 복귀, `near_stop.py:110-121` 주석).
   접근 스텝을 반전하면 실측 질감(요동과 속력 변화)은 유지하면서 거리가
   단조 증가함이 구조적으로 보장된다. `depart_rows`를 명시하면 외부 이탈
   조각을 우선 사용할 수 있다.

이 접근-정지-이탈 세 단계를 저작 결과의 실제 쌍 거리 시계열로 그리면
`verify_near_stop`의 검증 항목(정지 구간의 평탄함, 이탈 이후의 단조 증가)이
어떤 모양으로 나타나는지 확인할 수 있다.

<!-- 이미지 TODO | 파일: images/readme/perturbation-02-near-stop-profile.png | 플롯: author_near_stop으로 저작한 trace CSV에서 대상 쌍(a, b)의 프레임별 중심 거리(geom.center_distance_3d)를 y축, 경과 시각을 x축으로 그린 시계열. 접근 구간(거리 감소), 정지 구간(hold_s초 동안 평탄), 이탈 구간(거리 단조 증가)을 배경색이나 세로선으로 구간 구분 | 형식: png -->
![그림 2. near-stop 저작 결과의 접근-정지-이탈 거리 프로파일](../../../../images/readme/perturbation-02-near-stop-profile.png)
*그림 2. 대상 쌍의 중심 거리가 stop_distance까지 감소했다가 hold_s초 동안 평탄하게 유지되고, 이후 단조 증가로 전환되는 프로파일.*

정지 구간의 평탄한 구간 길이가 `hold_s`와 0.1초 이내로 일치하고, 이탈
구간에서 거리가 다시 줄어드는 굴곡 없이 단조 증가하는지가 `verify_near_stop`
5항 중 4번과 5번 항목이 실측으로 확인하는 지점이다.

대상이 아닌 다른 객체들은 정지와 이탈 구간 내내 **원본 궤적을 계속 따라간다**
— 대상 쌍만 멈추고 배경 전체가 얼어붙으면 "정지라는 형식 자체"가 VLM에게
새로운 단서가 되어 이 조건이 재려는 것(대상 쌍의 접근-정지 운동 자체에
대한 반응)을 오염시키기 때문이다(`others_at` 함수, `near_stop.py:146-154`).

`pre_s`/`post_s`를 주면 정지 시점 기준 앞뒤 창만 남기고(렌더 비용 절감 +
창 밖 제3자 조우가 GT를 오염시키는 것을 구조적으로 배제), `align_seconds=True`면
전체를 평행이동해 시작을 정확히 초 경계에 놓고 끝을 다음 초 경계까지
패딩한다 — 재연 잡이 초 단위로만 구간을 받기 때문에, 끝이 초 경계보다
이르면 내림 연산에서 마지막 1초(이탈 구간)가 통째로 잘리는 사고를 막기
위한 처리다(`near_stop.py:207-229`).

`verify_near_stop`은 저작 결과를 렌더 전에 반드시 통과해야 하는 5항으로
검증한다.

1. 전 쌍의 최소 수평 거리가 `contact_distance`(기본 60.0 — 접촉 규약과
   일치)보다 크다(GT 무접촉).
2. 결손이 없다 — 표본 간격이 중앙값보다 크게 벌어지는 구간이 없다(초 경계
   정렬이 만드는 짧은 간격은 결손이 아니므로 통과시킨다).
3. 인접 샘플 간 이동량이 `max_step`(기본 8.0) 이하다(순간이동 없음).
4. 정지 구간이 실제로 `hold_s`초 동안 정지해 있다(측정 길이와 `hold_s`의
   차이가 0.1초 이내).
5. 정지 종료 후 쌍 거리가 단조 증가한다(허용 오차 0.5 이내) — 앞서 언급한
   "이탈이 실제로 되돌아오지 않는가"를 수치로 확인하는 항목이다.

## 사용법

`perturb.py`와 `near_stop.py` 모두 각각 self-test 진입점을 갖는다.

```bash
python3 gist/netai/time_travel_summarization/perturbation/perturb.py
python3 -m gist.netai.time_travel_summarization.perturbation.near_stop --self-test
```

프로그램적으로는 패키지 최상위에서 바로 임포트한다.

```python
from gist.netai.time_travel_summarization.perturbation import (
    load_trace, dump_trace, gaussian, id_switch, fragmentation, occlusion, downsample,
    author_near_stop, verify_near_stop,
)

rows = load_trace(csv_text)
noisy = gaussian(rows, sigma=25.0, seed=42)
dump_trace(noisy)  # -> CSV 텍스트
```

캠페인 단위 실행(교란 → 업로드 → 재연 → 추론 → 채점 전체)은
`automation/perturb_eval.py`를 통해서 한다.

```bash
python3 -m gist.netai.time_travel_summarization.automation.perturb_eval \
    --conditions g25 switch frag occ-hold dsr5
```

## 테스트

전용 `tests/`의 유닛 테스트 파일은 없고, 각 모듈이 자체 `_self_test()`를
갖는다(위 사용법 참조). `automation/perturb_eval.py`의 `_self_test()`가
`plan_perturbation`을 통해 이 패키지의 함수 선택 로직(조건 → 어떤 교란을
어떤 파라미터로 호출하는가)까지 함께 검증한다.

## 한계와 주의

- `gaussian`은 y(높이)를 건드리지 않는다 — 객체가 항상 바닥 위에 있다는
  전제가 깨지는 시나리오(공중 객체 등)에는 적용되지 않는다.
- `fragmentation`을 `new_id=None`(sameid 대조군)으로 쓸 때는 재연 쪽의
  결손 인지 despawn(`TTS_DESPAWN_GAP_S`)을 함께 켜지 않으면 화면에서
  객체가 사라지지 않고 "얼어붙은 분신"으로 남는다.
- `near_stop.py`는 아직 `automation/perturb_eval.py`의 조건 매트릭스에
  자동으로 연결돼 있지 않다 — 별도 스크립트나 대화형 호출로 사용하는
  모듈이다.
