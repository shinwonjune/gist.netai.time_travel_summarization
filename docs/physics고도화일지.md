# Physics 고도화 일지

물리 기반 충돌 영상 생성을 위한 객체 거동(wander) 개선 기록.
문제별로 "기존 구현 → 원인 → 해결(경로·로직)" 순으로 정리.

---

## #1. 충돌 후 "발 고정·머리가 원을 그리며 회전" 버그 (2026-06-17 해결)

### 문제 정의
객체(우주비행사)가 충돌하면 넘어지고, 다시 일어나(원상복구) 이동하도록 구현 중이었음.
그러나 기립 과정에서 **발바닥은 땅에 고정된 채 머리가 원을 그리며 회전**하는 현상 발생.

### 기존 구현 (원인)
`physics/wander_controller.py`에 prim별 3-상태 머신이 있었음:
`MOVING → STUNNED → STANDING_UP`.

- 충돌 감지: `_check_fallen()`(원래 자세 대비 회전각 ≥ `fallen_angle_deg`), `_check_stuck()`(변위 부족), PhysX contact 이벤트.
- 감지 시 `STANDING_UP`으로 전이 후 매 틱:
  - `_restore_upright()` — `RotateXYZ` 오일러 op를 `_original_rotation`으로 LERP 복원.
  - `_set_world_origin_y()` — prim origin의 Y를 바닥(`ground_y=89.5`)으로 수동 고정.
  - `_set_kinematic(True)` — 복원 중 물리 비활성화.

**근본 원인 두 가지:**
1. **회전 피벗 문제** — 우주비행사 에셋의 prim origin이 *발바닥*에 있음. `_restore_upright`의 `RotateXYZ`는 이 origin을 축으로 회전하므로, 크게 기운 자세를 되돌릴 때 발은 고정되고 머리가 호(arc)를 그림.
2. **엔진과 충돌** — kinematic 토글 + 오일러 LERP + Y 수동 클램프로 PhysX의 물리 해를 사람이 매 틱 덮어씀(비표준 workaround).

### 해결: 표준 PhysX 관용구로 전환 (넘어짐 자체를 제거)
"바닥을 도는 에이전트가 넘어지지 않게" 하는 PhysX 표준 방식 = **수평축 회전 자유도 잠금**.

**(1) `physics/collision_proxy.py` — 회전 DOF 잠금**
`wrap_with_collision_proxy()`에서 이미 damping을 설정하던 `PhysxSchema.PhysxRigidBodyAPI`에 한 줄 추가:
```python
# lockedRotAxis 비트마스크: X=1, Y=2, Z=4. Y-up이면 X+Z(=5), Z-up이면 X+Y(=3).
lock_rot_mask = 5 if is_y_up else 3
physx_api.CreateLockedRotAxisAttr().Set(lock_rot_mask)
```
→ 솔버가 객체를 **물리적으로 기울이지 못함**(yaw만 허용). 넘어짐이 원천 차단되어, 기립 로직 자체가 불필요해짐.
- 근거: Omniverse `PhysxSchemaPhysxRigidBodyAPI` (104.2+). dynamic body 전용, 시뮬레이션 시작 전 설정.

**(2) `physics/wander_controller.py` — 기립 상태머신 제거 (619→약 390줄)**
- 삭제: `PrimState.STUNNED`/`STANDING_UP`, `_restore_upright`, `_set_world_origin_y`, `_standing_world_y`, `_check_fallen`, `_rotation_delta_from_original`, `_world_basis_vectors`, `_is_grounded`, `_vertical_position_history`, `_original_rotation`/`_original_basis` 및 관련 파라미터(`standup_duration_s`, `stun_duration_s`, `fallen_*`, `ground_y`, `upright_clearance`).
- 충돌(stuck/contact) 대응을 `STANDING_UP` 전이 → **`_redirect()`(방향 재설정) + 충돌 이벤트 emit**으로 변경.
- `collision_cooldown_s`(기본 0.5s)로 contact 이벤트 스팸을 디바운스.

**(3) `physics/collision_recorder.py` — 신규**
충돌을 `{timestamp, objid, x, y, z, kind}` CSV로 기록 → 학습용 ground-truth 라벨.

**(4) `app/facade.py` — 배선**
- `wrap_with_collision_proxy(..., visible=True)` → `visible=False` (디버그 프록시 비표시).
- `WanderController(..., on_collision=self._on_collision_event)` 연결.
- `start_wander`/`stop_wander`/`set_playback_mode`에 `CollisionRecorder` 생명주기 연결. 출력: `artifacts/collisions/collisions_<ts>.csv`.

### 검증
- 단위 테스트 `tests/test_wander_stuck.py` 갱신: 삭제 메서드 테스트 제거, 충돌 콜백·쿨다운·recorder 테스트 추가 (총 14개 통과, WSL stdlib 환경).
- 엔진 검증(Kit): Physics 모드 + Move 시 **객체가 더 이상 넘어지지 않음** 확인 완료.

### 알려진 한계 / 후속
- 충돌 라벨은 현재 *객체 단위*(어느 객체가 부딪혔는가)만 기록. 쌍(pairwise) 정보는 미기록 — 이 Kit 버전의 contact report API 불안정 때문. 필요 시 후속 개선.
- **벽 슬라이딩**: 객체가 벽에 얕은 각도로 닿으면 벽을 따라 미끄러지며 질질 이동하는 현상 별도 관찰 → #2에서 해결.

---

## #2. 벽 슬라이딩(wall-hugging) → 중앙 redirect (2026-06-17 해결)

### 문제 정의
객체가 벽에 닿으면 벽을 따라 미끄러지며 질질 이동(+yaw 드리프트)하는 현상.

### 원인
`_check_stuck()`은 **의도한 방향으로의 전진량(dot product)** 으로 판정. 객체가 벽에 *얕은 각도*로 닿으면 속도의 벽 수직 성분만 막히고 **접선 성분은 살아 벽을 따라 계속 이동**함 → 의도 방향 전진량이 충분해 "안 갇힘"으로 오판(탐지 사각지대).

### 해결: 경계 근접도 탐지 + 중앙 방향 redirect
회전 감지(불안정)나 contact API(이 Kit 버전 불안정) 대신, **이미 알고 있는 박스 bounds로 가장 가까운 벽까지의 거리**를 재는 결정론적 방식 채택.

**(1) `physics/wander_controller.py`**
- `__init__`에 `bounds_center`, `bounds_half`, `wall_margin`, `wall_frames` 추가.
- `_check_wall_hug()` 신설: 수평면에서 가장 가까운 벽까지 거리 < `wall_margin`이 `wall_frames` 연속이면 트리거.
- `_heading_to_center()` 신설: 객체→박스 center 수평 단위벡터 + `±35°` jitter(모두 정중앙 한 점으로 수렴 방지).
- `_on_update()`: stuck이 아니면 wall-hug 체크 → `_redirect(kind="wall", new_direction=중앙heading)`.
- `_redirect()`에 `new_direction` 인자 추가(방향을 외부에서 지정 가능). redirect 시 `_wall_count` 리셋.

**(2) `app/facade.py`**
- `set_physics_mode`에서 박스 `box_center`/`box_size`로 `bounds_half` 계산, `wall_margin = 0.1 × (작은 수평 변)`(씬 스케일에 자동 적응)로 `WanderController`에 전달.

### 검증
- 단위 테스트 추가(`tests/test_wander_stuck.py`): 벽 근접 트리거, 개활지 리셋, bounds 미전달 시 비활성, 중앙 heading 방향성.
- 엔진 검증(Kit): Physics+Move 시 벽 근처 객체가 중앙으로 방향을 트는지 확인 완료(`[Wander] WALL-HUG ... -> redirect to center` 로그).

### 튜닝 (2026-06-17)
- **문제:** 벽에서 너무 일찍(멀리서) 방향을 틀어 부자연스러움.
- **해결:** `app/facade.py`의 `wall_margin` 계수 `0.1 → 0.05`(작은 수평 변의 10%→5%). 벽에 더 붙은 뒤 회전.

---

## #3. 객체-객체 충돌이 "그냥 밀고 지나감" → 자연 충돌 + 정지 + 방향 전환 (2026-06-17 해결)

### 문제 정의
객체끼리 충돌해도 서로 살짝 밀고 그대로 지나가 버림. 충돌 느낌이 없음.

### 원인
`horizontal_per_tick` 모드는 매 틱 속도를 `direction × speed`로 **덮어씀**. 두 객체가 부딪혀 PhysX가 충돌 임펄스를 줘도 다음 틱에 속도를 원래대로 되돌려 **충돌 반응이 무시**됨 → 서로 통과하듯 밀고 감.
또한 객체 간 충돌은 stuck 탐지(얕은 각도 통과 시)나 contact API(이 Kit 버전 불안정)로 안정적으로 못 잡음.

### 해결: 쌍(pairwise) 근접 탐지 + 반동·정지·재출발
**`physics/wander_controller.py`**
- `__init__`에 `collision_distance`, `collision_pause_s` 추가. 상태: `_paused_until`, `_redirect_heading`.
- `_handle_object_collisions(now)` 신설: 관리 prim들의 **중심 간 수평 거리 < `collision_distance`** 면 충돌로 간주(벽 근접과 동일한 결정론적 방식, contact API 불필요). 이미 pause 중인 쌍은 스킵.
- `_begin_object_collision()` → 두 객체 각각 `_pause_and_redirect()`:
  - `_paused_until = now + collision_pause_s`로 일시정지 윈도우 설정.
  - `_redirect_heading`에 `_away_heading()`(상대→자신 방향 단위벡터 + ±30° jitter) 저장.
  - `on_collision` 이벤트 `kind="object"` 기록.
- `_on_update()` pause 처리:
  - **전반부**(`> pause/2` 남음): 속도 미override → **PhysX 충돌 임펄스·restitution(0.8)이 그대로 반영되어 자연스러운 반동**.
  - **후반부**(`< pause/2`): `_set_all_motion_zero()`로 **정지**.
  - **pause 종료 직후**: 저장한 away heading으로 **재출발**.

**`app/facade.py`**
- `WanderController(..., collision_distance=1.2 * m_to_units)` 전달(중심 간 1.2 m, 씬 스케일 적응).

### 부수 변경
- **이동 기본 속도 240으로 변경** — `app/facade.py` `self._wander_speed = 120.0 → 240.0` (Move UI 기본값에도 반영).

### 후속 버그픽스 (2026-06-17): 충돌 후 영원히 멈춤
- **문제:** 충돌은 자연스러운데 그 뒤로 두 객체가 그냥 멈춰 있음.
- **원인:** pause가 끝나도 두 객체가 아직 `collision_distance` 안에 붙어 있어, `_handle_object_collisions`가 매 틱 재충돌로 판정 → **무한 재pause**. 재출발 속도가 적용되기 전에 다시 pause되어 영원히 정지.
- **해결:** `_handle_object_collisions`의 가드를 `_paused_until` → `_paused_until + collision_cooldown_s`로 확장. pause 종료 후 cooldown(기본 0.5s) 동안 재발동을 막아 서로 멀어질 시간을 확보. (회귀 테스트 `test_no_retrigger_during_post_pause_cooldown` 추가)

### 후속 튜닝 (2026-06-17): 닿기 전에 멈춤
- **문제:** 객체가 시각적으로 닿기 한참 전에 멈췄다 떨어짐.
- **원인:** `collision_distance`를 임의 상수(`1.2 × m_to_units`=중심 간 1.2 m)로 잡았는데, 실제 프록시 실린더 반지름(bbox 기반 ~0.2 m대)보다 훨씬 커서 한참 멀리서 충돌로 오판.
- **해결:** `physics/collision_proxy.py`의 `wrap_with_collision_proxy`가 **프록시 반지름(`proxy_radius`)을 함께 반환**하도록 변경. `app/facade.py`에서 `collision_distance = 1.8 × max(proxy_radii)`로 산정 → 두 실린더가 살짝 겹치는 거리에서만 충돌 판정(임의 상수 제거, 에셋 크기에 자동 적응).

### 후속 수정 2 (2026-06-17): 스치는 충돌 누락 + 멈춤 시간 부족
- **문제 A — 스치듯 충돌 시 그냥 밀고 지나감:** 두 실린더(반지름 r)가 닿는 중심 거리는 `2r`인데, 임계값을 `1.8r`로 둬서 `2r`보다 작음 → 깊게 관통하는 정면충돌만 잡히고, 중심 거리 ≈ `2r`인 스치는 접촉은 탐지 누락.
  - **해결:** `app/facade.py` `collision_distance` `1.8r → 2.2r`(접촉 거리 `2r` + 여유). 스치는 접촉도 포착.
- **문제 B — 멈춤이 너무 짧고 부자연스러움:** `collision_pause_s=0.5`에 전·후반 절반씩이라 실제 정지가 ~0.25s로 짧음.
  - **해결:** `physics/wander_controller.py`에 `collision_impact_s`(기본 0.2s, 물리 반동 구간) 분리 + `collision_pause_s` 기본 `0.5 → 1.0s`(완전 정지 구간). 정지 윈도우 = `impact + pause`. 앞 `impact`는 PhysX restitution 반동을 그대로 두고, 뒤 `pause`(1초)는 완전 정지 → 자연스러운 충돌감 + 1초 멈춤 후 분리.

### 검증
- 단위 테스트: 쌍 충돌 시 양쪽 pause + 반대 방향 redirect, 원거리 미발동, `collision_distance=0` 비활성, pause 중/직후 cooldown 재발동 방지 → 전체 23개 통과.
- 엔진 검증(Kit) **완료**: Physics+Move 시 ① 스치는 충돌도 잡히고 ② 자연스럽게 튕긴 뒤 ③ 약 1초 멈췄다가 ④ 서로 다른 방향으로 이동함을 확인.

---

## #4. 객체 충돌 탐지를 거리 기반 → PhysX contact report로 전환 (2026-06-18)

### 배경
#3에서 객체-객체 충돌을 *중심거리 O(n²)* 로 탐지했다. 이유는 관습적 방식인 PhysX **contact report**가 이 Kit 빌드에서 startup abort를 낸다고 알려져 우회했기 때문. 재검증 결과 **abort가 재현되지 않아** contact report로 전환.

### 검증: 격리 probe
`utils/contact_report_probe.py`(임시)로 격리 실험 → `PhysxContactReportAPI.Apply` 성공·**abort 없음**, 두 구 충돌 시 `CONTACT_FOUND → PERSIST → LOST` 정상 수신 확인. 진단 완료 후 probe 삭제.
- 메인 코드가 예전에 안 됐던 진짜 원인: **ContactReportAPI 미적용 + 콜백 시그니처/경로 디코딩 오류**였지 Kit 한계가 아니었음.
- 확정 사실: 콜백 시그니처 `(contact_headers, contact_data)`, 경로 `PhysicsSchemaTools.intToSdfPath(actor0/actor1)`, threshold 메서드명은 버전차 → best-effort.

### 통합
- **`physics/collision_proxy.py`**: rigid body(우주인)에 `PhysxContactReportAPI` 적용 + threshold best-effort(`CreateThresholdAttr`/`CreatePhysxContactReportThresholdAttr` 중 있는 것에 0 설정).
- **`physics/wander_controller.py`**: `use_contact_reports`(기본 **True**) 플래그.
  - ON: `subscribe_contact_report_events` 구독, 콜백에서 **두 관리 객체 쌍**의 접촉만 골라 기존 `_begin_object_collision`(멈춤+분리) 재사용. PERSIST 폭주는 cooldown으로 디바운스. 거리 기반 `_handle_object_collisions`는 OFF(중복 방지).
  - OFF: 기존 거리 기반으로 fallback.
  - 벽 충돌은 거리 기반 wall-hug(중앙 redirect)가 그대로 담당(접촉 신호로는 "중앙 방향"을 못 얻으므로).
- **반응 로직은 불변**: 탐지만 거리→contact report로 교체. 멈춤+자연반동+분리는 #3 그대로.

### 부수 변경: 콘솔 로그 정리
일상·이벤트 로그(STUCK/WALL-HUG/CONTACT/OBJECT-COLLISION/started/speed 등)를 `carb.log_info`로 강등, `facade`의 per-prim 진단 덤프 제거. **실제 경고(잘못된 입력·API 실패)만 `log_warn` 유지** → 콘솔 스팸 제거.

### 검증
- 단위 테스트(플래그 기본값, contact 경유 충돌 멈춤+분리 포함) 전체 25개 통과.
- 엔진 검증(Kit) **완료**: `[Wander] CONTACT(report) A <-> B` → `OBJECT-COLLISION` → 멈춤+분리 동작 확인. wall-hug/stuck도 병행 정상.

---

## #5. BEV 충돌 학습데이터 생성 준비 — 캡처·라벨 정합 (2026-06-19)

**목적**: 물리 충돌(contact report)로 만든 데이터를 **Qwen3-VL-8B LoRA 학습**에 쓰기 위해, 캡처 영상과 충돌 라벨의 시간·형식을 정합시킨다. (전체 학습 파이프라인 문서는 `training/METHODOLOGY.md`, 진행 체크리스트는 `training/VERIFICATION_CHECKLIST.md`.)

### 배경
원격(Ultraplan) 세션이 BEV LoRA PoC 일습(데이터셋 빌더·학습·평가·headless 자동화, 브랜치 `poc/bev-collision-lora`, +2353줄)을 생성 → 로컬에 적용. 추론이 영상을 **2초 청크**로 처리하므로 "학습 1샘플 = 2초 클립 = 추론 1청크" 정합이 품질의 핵심. 시각은 VLM이 **오버레이(픽셀에 구운 시각)** 를 읽어 보고하고, 라벨은 충돌 CSV에서 나온다 → 둘의 형식·시각이 맞아야 한다.

### 문제 → 해결
1. **오버레이가 너무 상세(`YYYY-MM-DD HH:MM:SS.mmm`)** — VLM이 읽어 보고할 형식·라벨 형식과 어긋남.
   - → `app/facade.py:get_stage_time_string`를 초단위로.
2. **충돌 CSV가 영상보다 길게 기록됨** — 기록 창이 Move(시작)~중지라 캡처 창과 불일치(1분 영상에 1분+ CSV).
   - → 충돌 recorder 소유권을 **Move → Capture**로 이동(`start_capture`/`run_capture_headless`에서 시작, 캡처 종료 시 종료; `set_playback_mode`에 안전망). **CSV 길이 ≈ 영상 길이.** (이제 Move만으론 CSV 미생성 → Capture 필요.)
3. **형식을 쉽게 바꿔 실험(초 → 추후 ms)하고 싶음.**
   - → 단일 소스 **`timefmt.py`**(`PRECISION`, `format_event_time`, `parse_event_time`) 신설. **오버레이·collisions CSV가 공유** → 한 곳에서 전환. `utils/build_dataset.py`는 날짜 없는 CSV를 `capture_start` 기준으로 복원하도록 파싱·offset 수정, 라벨 키도 공유 형식. `utils/observability.py`도 날짜 없는 시각 수용. **trace CSV는 sub-second(τ≈수십 ms) 측정에 필요해 날짜+ms 유지**(의도적). ms 전환 시 추가 필요(프롬프트 문구 + 충돌쌍 동일 스탬프)는 `timefmt.py` 주석에 명시.

### 변경 파일
- 신규: `timefmt.py`, `training/VERIFICATION_CHECKLIST.md`
- 수정: `app/facade.py`(오버레이 시계·CSV↔캡처 동기화), `physics/collision_recorder.py`(공유 형식), `utils/build_dataset.py`(날짜없는 파싱·라벨), `utils/observability.py`(날짜없는 파싱)

### 검증
- **코드(완료)**: 컴파일 OK + WSL 기능 테스트 — `timefmt` 왕복·자정 롤오버, `build_dataset` offset(클립 배정)·초단위 라벨 정합 통과.
- **미검증(Kit/하드웨어 필요)**: 실제 캡처에서 오버레이↔CSV↔클립 정합(A1/A2), headless 생성, 학습/평가 → `training/VERIFICATION_CHECKLIST.md` 참조.

---

## #6. 캡처 슬로모션·삼각형 진동 → sim-time 단일 마스터 클럭 (2026-07-02 해결)

### 맥락
Capture 중에만 ① 객체가 슬로모션 ② 제자리 삼각형 진동. 측정: 앱 루프 60(대기)→44(Move)→21fps(Capture) — GPU→CPU 리드백이 루프를 지연시키고, fixed timestepping이라 물리가 프레임당 고정량만 전진 → sim이 wall의 0.35배. 검토(Architect+Critic)로 wall-clock/sim-time 혼용이 근본 원인으로 확정.

### 문제 → 해결
| 문제 | 원인 | 해결 (경로) |
|---|---|---|
| 삼각형 진동 | `_check_stuck`이 wall-clock dt로 기대이동 계산 → progress/expected≈0.33 오탐 | wander 전체를 update 이벤트 dt 누적(`_sim_now`)으로 전환. cooldown 기본 -inf(0-시작 클럭 억제 버그) (`physics/wander_controller.py`) |
| 라벨↔영상 어긋남 | 오버레이·CSV가 `datetime.now()`(wall), 클립은 비디오 시간 | sim-time 마스터 클럭: headless 캡처가 프레임마다 `set_sim_time(seq/fps)`, 라벨=capture_start+sim경과. 사이드카 t0 단일 객체 공유 (`app/facade.py`, `physics/collision_recorder.py:when=`) |
| 재현성 없음 | wander가 전역 random 미시딩 | `random.Random(seed)` 주입, 에피소드 seed 전파 |
| 라벨 정의 불일치 | contact report(≈2r) vs observability(2.2r) | 사이드카 접촉정의 2.0r로 통일(2.2r은 거리 fallback 전용) |

검토 산출물: 원계획 REVISE(진단 정확·"결정론" 용어는 "페이싱 재현"으로 정정), B'(60fps 정합+빌더 데시메이션) 채택.

## #7. headless 데이터 생성 파이프라인 완성 — 디버그 체인 (2026-07-02~03)

### 맥락
Nucleus 씬(`A_AI-Grad_Building.usd`) + 지정 카메라(`Capture_camera`)로 physics 배회를 headless 촬영해 학습 에피소드를 자동 생산. `kit.exe`를 WSL interop으로 직접 구동(bat 따옴표 인자유실 회피), 로그 마커 기반 1분 주기 감시.

### 문제 → 해결 체인 (시간순)
| # | 증상 | 원인 | 해결 (경로) |
|---|---|---|---|
| 1 | annotator 영원히 빈 데이터 | SyntheticData가 frame history<3이면 초기화 거부 | 부팅 플래그 `--/app/settings/fabricDefaultStageFrameHistoryCount=3` (`automation/run_headless.sh`) |
| 2 | `Replicator initialization aborted` | Composer 셋업 확장이 시작 ~16s 후 지연 new_stage → stage-closing에서 orchestrator reset+hydra texture 전멸 | `--/app/content/emptyStageOnStart=true`(지연 교체 자체가 예약 안 됨) |
| 3 | 동기 step 금지 에러 | Kit 내부에선 `orchestrator.step_async`만 허용 | 코루틴+`app.update()` 펌프 패턴 (`video_capture/realtime_capture.py:_advance`) |
| 4 | 영상 2배속(ratio_t=2.0) | `pause_timeline=False`면 step 전진+재생중 타임라인 자유 전진 중복 | `pause_timeline=True` → ratio_t=1.00 |
| 5 | mp4 미생성(FileNotFoundError) | RTX 준비 전 프레임 0장 → ffmpeg 파일 자체가 없음 | annotator 데이터 나올 때까지 워밍업 |
| 6 | 우주인이 화면에 없음+낙하(y −18k) | `auto_generate_astronauts`→`clear_timetravel_objects()`가 **궤적 repo까지 삭제**(facade.py:978) → 배치 no-op → 전원 (0,0,0) 겹침 → PhysX 겹침해소 폭발 → 벽(0.1) 관통 낙하 | GUI 동일 경로 `regenerate_astronauts_from_loaded_data`(repo 보존) + 배치(set_to_earliest_time)→physics 순서 (`automation/generate_episodes.py`) |
| 7 | 확장 startup 사망(headless) | pipapi PermissionError가 `except ImportError` 관통 | minio import 가드를 `except Exception`으로 (`storage/minio_adapter.py`) |

### 결과 (실측)
- 빈 스테이지: ratio_t=1.00·ratio_d≈0.8, 4s/60fps mp4 정상 속도.
- 빌딩 씬: 우주인 4명이 카메라 시야 안에서 배회(y 89~191 유지), ID 오버레이·타임스탬프·physics 벽(빨/파) 렌더 확인. 에피소드당 video+meta+collisions CSV+trace 산출.
- 잔여: `--quit` 후 프로세스 잔류(강제 종료로 대응 중).

## #8. 오버레이 투영 정확화 — 재구성 행렬 → 렌더러 실행렬 (2026-07-03 해결)

### 맥락
ID 라벨이 우주인에서 수십 px 이탈. 원인: 투영을 렌더러가 쓴 행렬이 아니라 USD 카메라에서 **재구성**(GfCamera frustum + `horizontalAperture=vertical×aspect` 손보정)했는데, 이는 렌더러 conform 정책의 "추측"이라 실제와 다르면 화면 가장자리로 갈수록 계통 오차 발생. 임시 radial 보정(`TTS_OVERLAY_RADIAL_SCALE`)은 등방 배율이라 근본 해결 불가.

### 해결
Replicator `camera_params` annotator를 render product에 병행 attach → 프레임마다 렌더러가 **실제 사용한** `cameraViewTransform`/`cameraProjection`을 받아 그대로 투영 (`video_capture/realtime_capture.py`: `_renderer_matrices` → `_default_provider_from_core(matrices_fn=)`). annotator 실패 시 기존 재구성 폴백 유지.

### 검증 (headless 실기)
빌딩 씬 run에서 로그 `overlay matrices: renderer camera_params (exact)` + 프레임 추출 비교: 라벨 ①~④가 각 우주인 몸 위에 정확히 부착(t=2, t=7 두 시점 모두). 잔여 미세 오프셋은 앵커(프림 원점=발밑)의 시차로, 투영 오차 아님. radial 보정은 기본 1.0(무동작)으로 사실상 사문화 — 제거는 인터랙티브 경로 파리티 확인 후.

## #9. 제자리 진동 랜덤워크 → wander 시계 타임라인 직독 (2026-07-06 해결)

### 맥락
투영 수정 후 영상에서 우주인들이 좁은 구역에서 진동하듯 요동. 씬에는 벽 외 콜라이더가 없으므로(사용자 확인) 충돌이 아니라 판정 문제. CSV 실측: **10초에 stuck 468회**(wall 18, object 0) — "느리다" 오판으로 초당 ~12회 방향 리셋 = 진동의 정체. 사용자가 원인 가설(false-stuck)을 정확히 제시.

### 원인
headless 캡처는 프레임당 `orchestrator.step_async(1/60)`를 쓰는데, 렌더 완료를 기다리는 동안 `app.update()`가 7~8회 펌프된다. 그 헛바퀴에서도 update 이벤트 payload dt>0이 오므로, **dt 누적 방식의 wander 시계는 물리 1스텝 동안 7~8틱치 기대이동을 쌓아** progress/expected≈1/7 → 거의 매 프레임 false-stuck. (#6에서 wall-clock→dt누적으로 고쳤지만, step_async 구조에선 update 횟수≠물리 스텝 횟수라 재발)

### 해결
시계를 dt 누적 → **타임라인 현재시각(`omni.timeline.get_current_time()`) 직독**으로 (`physics/wander_controller.py:_on_update`). 타임라인 미전진 틱(렌더 대기 펌프)은 통째로 스킵 → 기대/실이동이 항상 같은 시간 구간. omni 없는 유닛테스트는 dt 누적 폴백.

### 검증 (run9 실측)
- stuck **468 → 0**, wall 7(정상 redirect), object 1건
- 이동 경로 2,400~2,700 units/10s = 지시속도(274) 그대로 직진 배회, 프레임에서 4명이 방 전체로 분산
- 유닛테스트 25/25 유지

### 교훈
"시계"는 한 곳(물리 진실=타임라인)에서만 읽을 것. wall-clock(#1 세대), 이벤트 dt 누적(#6 세대) 모두 특정 실행 구조에서 물리와 어긋났다.

## #10. 캡처 렌더 fps 선택 — 30fps 데시메이션 채택 (2026-07-06)

### 선택 이유
병목 실측(run10 프로브): 프레임당 ~120ms 중 orchestrator step(물리+렌더) 93% / annotator readback 4% / 오버레이·큐 3% → 렌더 횟수 축소가 유일한 유효 지렛대. sim은 60Hz 고정 스텝을 유지하고 렌더·인코딩만 N스텝당 1회로 데시메이션(라벨 시각은 스텝 인덱스 기준이라 정합 불변).

| 렌더 fps | 20s 에피소드 실측/추정 | 잃는 것 |
|----------|----------------------|---------|
| 60 (기존) | 143s | — |
| **30 (채택)** | **85s 실측 (1.7×)** | 없음에 가까움 |
| 10 | ~48s 추정 (3×) | 충돌 순간 프레임 최대 50ms 어긋남, 재슬라이스 불가, 육안 검수 곤란 |

30 채택 근거: ① 학습 데이터(10Hz 데시메이션)와 픽셀 동일 결과 유지, ② 충돌 시점 클립 위상 정렬 여지(±33ms)와 육안 검수 가능성 보존, ③ content-hz 10의 3배수라 깔끔한 데시메이션, ④ 속도가 더 급해지면 10fps 카드는 언제든 남음. `generate_episodes.py --render-fps`(기본 30), `facade.run_capture_headless(render_fps=)`, `CaptureRequest.render_fps`로 배선.

### 전제 조건이 된 수정 (검증 중 발견)
- run12: 물리-only 스텝이 타임라인을 재생 상태로 남겨 렌더 스텝에서 ratio_t=2.0 → `_phys_advance()`가 update 후 pause (`realtime_capture.py`).
- run13: 렌더 스텝의 타임라인 전진이 update 스트림에 전진 틱으로 노출되지 않아 wander velocity 재주장이 절반 누락 → damping 누적으로 실효 속도 274→90(1/3), 충돌 22→7. **wander를 update 틱 구독 → PhysX 물리 스텝 이벤트 구독으로 이관**(`wander_controller.py: _on_physics_step/_tick`; omni 없는 환경은 기존 update 경로 폴백). #6·#9에 이은 세 번째 "틱≠물리 스텝" 재발의 근본 청산.

### 검증 (run14 실측, 시드 42 — run11(60fps)과 동일 조건 비교)
- 실효 속도 240~256 units/s (평균 247) = run11 기준선 복원 (run13은 90이었음)
- 충돌 22행(object 6, wall 16) ≈ run10/11의 22행(object 8, wall 14)
- ratio_t=1.00 전 스텝, ratio_d 1.01~1.04 안정(드리프트 소멸), meta fps=30, 캡처 85s
- 유닛테스트 27/27. GUI 실기 확인 예정(물리 스텝 이벤트 경로의 인터랙티브 파리티)

## #11. 무작위 스폰을 사전 정의 구역 방식으로 전환 (2026-07-07, 0014eeb)

### 맥락 — 레이캐스트 사전계산이 만든 두 사고
무작위 시작 위치 1차 구현(레이캐스트 검증)은 startup에서 physics를 켜고 `app.update()`를
펌프해야 했는데, 이 펌프가 두 사고의 방아쇠였다:
1. **타임라인 잠금(실속)**: 캡처의 `set_capture_on_play(False)` 이전에 "재생 예약+update"가
   만나면 Replicator 자동 모드가 타임라인 자동 전진을 잠근다. 실측(run20 덤프):
   `playing=True`인데 update 무전진. 물리-only 스텝(play 의존)만 죽고 orchestrator 스텝
   (수동 전진)은 정상 → 본 생산 75ep 전부 폴백으로 생성(정합은 유지, 4.4h 소요).
   `forward_one_frame()`(상태 무관 수동 전진) 우회를 3단 방어로 추가(378bc4a).
2. **합성 객체 원점 낙하**: `--extra-objects` 우주인이 스폰 창 동안 원점(floor 밖)에서
   낙하(run19: y −18→−65,390; "실효속도 5,300+" = 낙하 속도였음).

### 해결 — 구역(spawn zones) 방식
"바닥 존재"를 런타임 레이캐스트로 검증하는 대신 **사람이 보증하는 구역을 사전 정의**하고
순수 수학으로 샘플 → startup physics 펌프 자체가 사라져 **잠금 발생 조건이 소멸**.
(`automation/generate_episodes.py: sample_zone_positions/load_spawn_zones/parse_spawn_plan`)

### 구역 정하는 법
```bash
# 기본(옵션 없음): 궤적 좌표 범위 전체가 단일 구역, 바닥 89.5(레이캐스트 실측값)
# 커스텀 구역: JSON 파일 또는 인라인 (y-up 기준 수평 사각형 + 바닥 높이)
--spawn-zones '{"lobby": {"min": [300, -2800], "max": [900, -1800], "floor": 89.5}}'
--spawn-plan "lobby:3,corridor:1"   # 구역별 객체 수 (합계=객체 수, --min==--max 필요)
--spawn-floor 89.5                  # 기본 구역의 바닥 높이 조정
```
- 샘플 규칙: 구역 가장자리 10% 마진 제외 균등, 객체 간 최소 이격(짧은 변의 15%), 바닥+5cm 스폰(중력 안착).
- **신규 구역 등록 시**: 바닥 존재를 오프라인 1회 검증 — 남겨둔 레이캐스트 도구
  (`sample_floor_positions`+`precompute_floor_positions`)로 확인 후 floor 값을 기입.
- 확장: 구역 추가 = JSON 항목 추가(코드 무변경). 구역은 physics 벽(궤적 bbox) 안쪽이어야 함.

### 검증
- run21(4객체): 실속·우회 로그 0건(잠금 미발생 — 원인 제거 실증), 10s ep 캡처 44s(fast path),
  ratio_t=1.00, 전원 스폰 좌표 안착(y 90~94), 실효속도 115~130.
- run22(6객체, `--extra-objects 2`): pos-verify 6/6 일치, 추락 없음, 전원 129~130,
  obj005/006 충돌 참여, 캡처 47s. → 합성 객체 낙하도 startup 펌프 제거로 함께 해소.

---

## 부록. 구현 방식의 PhysX 관습성(convention) 평가

현재 구현을 "PhysX 표준 관용구냐"는 관점에서 정직하게 평가. **물리 셋업은 관습적, 제어·탐지 레이어는 실용적 절충**.

### ✅ 관습적인 부분 (idiomatic)
- **Rigid body + 불가시 collider proxy**: `RigidBodyAPI` + `CollisionAPI` + `MassAPI` + 물리 머티리얼(restitution). USD Physics 표준 패턴.
- **`lockedRotAxis`로 기울기 방지**: 바닥을 도는 에이전트를 안 넘어지게 하는 PhysX의 **교과서적 방법**.
- **`PhysxRigidBodyAPI`의 linear/angular damping, restitution**: 표준.

### 🔶 실용적 절충 (관습과 다름, 의도적 선택)
- **매 틱 velocity 덮어쓰기(`horizontal_per_tick`)로 이동 구동**: PhysX dynamics를 "정석"으로 구동하는 방식이 아님. 관습적 대안은 ① **PhysX Character Controller**(kinematic 캡슐 — 안 넘어지고 벽 슬라이딩을 네이티브 처리) 또는 ② 힘/토크(`addForce`). velocity 강제는 솔버의 충돌 반응과 싸우며, 그래서 "충돌 시 잠깐 override를 멈춰 물리를 살리는" 처리가 필요했음.
- **객체-객체 충돌 탐지**: **#4에서 관습적 방식인 PhysX contact report로 전환 완료**(기본값). 처음엔 abort 우려로 거리 기반이었으나 재검증 후 표준 경로 채택. 거리 기반은 fallback으로 유지.
- **벽 근접도 탐지**: contact report/raycast 대신 알고 있는 박스 geometry로 거리 계산. "벽에 닿음"이 아니라 "중앙으로 보내기"가 목적이라 박스 center가 필요 → 의도적으로 거리 기반 유지(결정론적·견고).

### 정리 / 향후
- **셋업(rigid body·collider·DOF lock·damping·restitution)은 표준 그대로**라 이식성·신뢰성이 높음.
- **충돌 탐지는 #4에서 표준(contact report)로 정착**. 남은 비관습 요소는 *이동 방식(매 틱 velocity 덮어쓰기)*뿐. 더 관습적으로 가려면 velocity SET → **힘(addForce)** 전환이 정석(충돌 임펄스와 공존, 우회 불필요)이나 속도 유지 튜닝 비용이 있음. CCT는 객체-객체 자연 충돌을 잃어 우리 목적엔 부적합. 현재는 "표준 물리 프리미티브 + contact report 탐지 + 실용적 velocity 제어" 조합.
