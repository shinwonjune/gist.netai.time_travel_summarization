# physics — PhysX 기반 배회 시뮬레이션과 충돌 GT 라벨링

## 역할과 위치

`physics/`는 재현(디지털 트윈 시뮬레이션) 단계에서 로드된 우주인(astronaut) 프림에
PhysX 물리를 씌워 자율적으로 배회하게 만들고, 그 과정에서 벌어지는 접촉을
학습용 정답(Ground Truth, GT) 라벨로 기록하는 모듈이다. 상위 계층인
`app/physics_service.py`가 이 패키지의 함수와 클래스를 조립해 "물리 모드
켜기/끄기"라는 하나의 동작으로 노출하고, `app/` 계층은 UI 이벤트와 Kit 확장의
생명주기(스테이지 로드, 잡 shutdown 등)만 다룬다. 즉 `physics/`는 순수하게
"우주인이 어떻게 움직이고, 부딪히면 무슨 일이 일어나는가"만 책임진다.

생성된 우주인 프림은 좌표 데이터(궤적 CSV)를 재생하는 `playback/` 경로와,
이 `physics/` 경로 중 하나로 구동된다. `physics/`가 만드는 것은 두 산출물이다.
하나는 학습용 비디오 생성을 위한 실제 물리 시뮬레이션 배회 궤적(trace CSV,
`trace_recorder.py`)이고, 다른 하나는 그 배회 중 실제로 일어난 충돌의 정답
라벨(collisions CSV, `collision_recorder.py`)이다. 이 둘은 VLM 학습 결과와
추론 결과 양쪽에 대조되는 채점 기준이 된다.

## 구성 파일

| 파일 | 책임 | 핵심 심볼 |
|---|---|---|
| `collision_proxy.py` | 시각 메시(우주인 에셋)에 보이지 않는 원통형 충돌 콜라이더를 씌우고 강체(RigidBody)로 만든다 | `wrap_with_collision_proxy`, `unwrap` |
| `collision_recorder.py` | PhysX contact report로 감지된 충돌 이벤트를 GT 라벨 CSV로 기록한다 | `CollisionRecorder` |
| `physics_scene.py` | 스테이지에 `UsdPhysics.Scene`(중력, 물리 스텝 레이트)을 생성/갱신한다 | `ensure_physics_scene` |
| `trace_recorder.py` | 물리 시뮬레이션 중 우주인의 월드 좌표를 궤적 CSV 형식으로 스트리밍 기록한다 | `TraceRecorder` |
| `walls.py` | 배회 영역을 둘러싸는 보이지 않는 정적 벽 콜라이더(바닥 포함 5면)를 생성한다 | `create_bounding_box` |
| `wander_controller.py` | 매 물리 스텝마다 각 우주인의 헤딩과 속도를 갱신하는 배회 안무 엔진(기본 배회 + near-miss 안무) | `WanderController` |
| `__init__.py` | 위 심볼들을 패키지 최상위로 재노출 | `__all__` |

## 동작 흐름

`app/physics_service.py`의 `set_physics_mode(core)`가 물리 모드 진입 시
아래 순서로 이 패키지를 호출한다(호출자 확인: `app/physics_service.py:151-314`).

```
set_physics_mode(core)
  1. ensure_physics_scene(stage)                    # 중력, 물리 스텝 레이트 설정
  2. create_bounding_box(stage, center, size)        # 아레나(배회 영역) 벽 5면 생성
  3. 우주인 prim마다: wrap_with_collision_proxy(...)  # 원통 콜라이더 부착 + RigidBody화
  4. WanderController(rigid_prims, ..., on_collision=core._on_collision_event)
     (인스턴스만 생성 — 실제 배회 시작은 사용자가 별도 Move 동작으로 트리거)
```

배회가 시작되면(`WanderController.start()`) 매 PhysX 물리 스텝마다
`WanderController._tick`이 각 프림의 헤딩과 속도를 갱신하고, 두 객체가 실제로
접촉하면 PhysX contact report 콜백이 `on_collision(prim_path, position, kind)`을
호출한다. 이 콜백은 `app/physics_service.py`의 `on_collision_event`로 연결되어
`CollisionRecorder.record(...)`가 CSV 행을 쓴다(`app/physics_service.py:45-51`).
같은 시간 동안 `TraceRecorder.tick()`이 매 프레임 별도로 호출되어(호출자는
`app/facade.py`의 `update` — `physics_service.start_trace`는 recorder의 생성과
`start()`만 담당) 우주인들의 월드 좌표를 궤적 CSV로 주기적으로 흘려 쓴다. 즉 한 번의 배회 세션에서 "무엇이 어디서 일어났는가"는
`TraceRecorder`가, "언제 누구와 부딪혔는가"는 `CollisionRecorder`가 독립적으로
기록하고, 이 둘이 나중에 재생(playback)과 VLM 채점의 두 축이 된다.

물리 모드를 끄면(`set_playback_mode`) `WanderController.stop()` → 각 프림에
`unwrap(stage, prim)`을 호출해 콜라이더, 물리 재질, 강체 API를 제거하고
`/World/PhysicsWalls`를 지운 뒤 재생(playback) 모드로 되돌아간다.

실제 배회 세션에서 두 우주인이 접촉하는 순간을 렌더링된 프레임으로 보면
이 콜백이 무엇을 포착하는지 감이 잡힌다.

<!-- 이미지 TODO | 파일: images/readme/physics-01-collision-moment.png | 촬영: generate_episodes.py로 생성한 임의 에피소드의 `_video.mp4`에서 collisions CSV에 기록된 충돌 타임스탬프의 프레임을 ffmpeg로 추출(두 우주인 메시가 화면상 맞닿아 보이는 순간) | 형식: png -->
![그림 1. 배회 중 두 우주인이 접촉하는 순간](../../../../images/readme/physics-01-collision-moment.png)
*그림 1. `CollisionRecorder`가 GT 행으로 기록하는 순간 — 이 프레임의 타임스탬프가 collisions CSV의 해당 행과 대응한다.*

이 프레임과 같은 시각의 trace CSV 행을 대조하면, 규약대로 접촉 판정 거리
(2r=60.0) 부근에서 collisions 이벤트가 기록됐는지를 사후 검증할 수 있다.

## 핵심 설계 결정과 규약

### 1. 충돌 콜라이더는 원통이고, 반지름과 높이는 에셋의 지역(local) bbox에서 유도한다

`wrap_with_collision_proxy`는 시각 메시 자체를 강체로 쓰지 않고, 자식 프림으로
보이지 않는 원통(`__phys_proxy__`)을 만들어 그 원통에만 `CollisionAPI`를 붙인다
(`collision_proxy.py:96-352`). 반지름은 상수가 아니라 계수다.

```python
proxy_radius = max(min(other_dims) * 0.4542, 0.05 * m_to_units)
```

계수 0.4542는 끈(strap) 장식을 제거한 에셋의 지역 좁은 변(66.04)을 반지름
30.0으로 맞춘 값이다(`collision_proxy.py:229-233` 주석). 반지름을 상수로
고정하지 않고 계수로 유지하는 이유는, 에셋 치수가 바뀌면 반지름이 자동으로
따라가고 그 값이 job 로그의 `radius=` 스탬프로 사후 검증되기 때문이다. 접촉
거리(두 원통이 닿았다고 보는 중심 간 거리)는 2r = 60.0 규약이다.

치수는 **월드 축 정렬 바운딩 박스(AABB)가 아니라 지역(untransformed)
bbox**에서 구한다. 월드 AABB는 객체가 회전하면 대각선만큼 부풀어 스폰 각도
추첨에 따라 반지름이 바뀌는 문제가 있었기 때문이다(`collision_proxy.py:182-233`
주석에 원인 규명 경위 포함). 지역 bbox는 세션 첫 생성(rest 자세) 시점에만 측정해
`_REST_DIMS` 캐시에 저장하고, 이후 에피소드 경계마다 프록시를 재생성할 때는
캐시 값을 재사용한다. 재측정하지 않는 이유는 배회로 누적된 요(yaw) 회전이
"다시 재는" 순간에 다시 값에 섞이기 때문이다.

원통의 높이 축(로컬 프레임에서 어느 축이 "위"인가)은 하드코딩하지 않고
월드 상축을 프림의 역회전으로 변환해 매 순간 계산한다(`local_up` 계산,
`collision_proxy.py:160-177`). 이 에셋은 눕혀서 authored된 뒤 스폰 시
`rotateXYZ(-90,0,0)`으로 세워지므로(`playback/stage_object_controller.py`의
`create_astronaut_prim`), 지역 높이 축이 월드 상축 인덱스와 다를 수 있다는
전제 때문이다.

아래 그림은 이 원통 프록시가 실제로 우주인 메시에 어떻게 씌워지는지를
와이어프레임으로 보여준다.

<!-- 이미지 TODO | 파일: images/readme/physics-02-proxy-wireframe.png | 촬영: USD Composer 뷰포트에서 우주인 1명을 확대하고 뷰포트 렌더 모드를 Wireframe으로 전환해 `__phys_proxy__` 원통 콜라이더가 몸통을 감싼 모습이 보이게 할 것. 스폰 직후(요 회전이 쌓이기 전) 정면 자세에서 촬영해 프록시 반지름이 몸통의 지역 좁은 변에 맞춰진 것이 보이도록 한다 | 형식: png -->
![그림 2. 우주인 메시를 감싼 원통 콜라이더 프록시](../../../../images/readme/physics-02-proxy-wireframe.png)
*그림 2. 원통 프록시(`__phys_proxy__`)가 메시의 지역 좁은 변에 계수 0.4542를 곱한 반지름으로 생성된 모습.*

이 원통에만 `CollisionAPI`가 붙어 있으며, 팔 등 부속 메시에는 별도
콜라이더가 없다는 점을 와이어프레임에서 확인할 수 있다.

### 2. 탄성(restitution)은 1.0, 수평 회전만 허용(넘어지지 않음)

물리 재질의 restitution은 1.0으로 고정한다(`_bind_physics_material`,
`collision_proxy.py:75-93`). 0은 완전 비탄성, 1은 접촉에서 에너지 손실이
없는 값이며, 1을 초과하면 충돌마다 에너지가 늘어 발산하므로 쓰지 않는다.
1.0에서도 `WanderController`가 매 틱 지시 속도를 다시 걸어주므로 속도가
누적 증가하지는 않는다. 또한 `lockedRotAxis` 비트마스크로 수평축 회전을
잠가(Y-up이면 X+Z) 우주인이 절대 넘어지지 않고 요(yaw)만 돌게 만든다
(`collision_proxy.py:326-336`).

### 3. trace 좌표는 콜라이더 중심(collider-trace-v1)

`TraceRecorder`가 기록하는 좌표는 수평 성분과 수직 성분의 출처가 다르다
(`trace_recorder.py:1-11` 모듈 독스트링). 수평(x, z)은 충돌 프록시(원통)의
월드 중심을, 수직(y)은 객체 프림의 피벗을 쓴다. 수직까지 프록시 중심으로
바꾸면 재생(playback)이 trace의 좌표를 프림 translate에 그대로 넣을 때
객체가 공중에 뜨기 때문이고(프록시 중심은 피벗 위 약 46 유닛), 접촉 거리
판정(2r)은 수평 중심 거리이므로 수평만 프록시 중심이면 "GT와 같은 점을
가리킨다"는 성질이 성립한다. 프록시가 아직 생성되지 않았으면(배회 시작 전)
피벗으로 폴백한다. 어느 소스로 기록됐는지는 job 로그의 `[Trace] source=`
스탬프로 사후 판정할 수 있다(`_log_source_stamp`, `trace_recorder.py:152-164`).

같은 이유로 `WanderController._emit_collision`이 기록하는 충돌 이벤트
좌표도 프록시 중심을 우선한다(`wander_controller.py:1053-1072`) — trace와
collisions CSV가 같은 기준점을 가리켜야 두 채널이 정합하기 때문이다.

### 4. 충돌 검출은 PhysX contact report가 기본, 거리 기반은 폴백

`WanderController(use_contact_reports=True)`가 기본값이며, 이 경우 객체-객체
충돌은 PhysX의 `subscribe_contact_report_events` 콜백(`_on_contact_event`)이
전담한다(`wander_controller.py:1383-1435`). contact report 구독이 불가능한
빌드이거나 명시적으로 끈 경우에만 중심 거리 기반 폴백
(`_handle_object_collisions`, 임계값 2.2r — 접촉 정의 2r보다 넉넉하게 잡아
스치는 접촉까지 잡는다)이 동작한다.

### 5. near-miss 안무 — "접촉 없이 접근만 하는" 대조군 데이터

near-miss 대조군 데이터를 만드는 목적은 "근접 신호만으로 VLM이 충돌을
오탐(hallucinate)하는지"를 시험하는 것이다(`wander_controller.py:16-71` 클래스
독스트링). 이 시험이 성립하려면 두 조건이 동시에 필요하다 — 두 객체가 화면에서
겹쳐 보일 만큼 실제로 가까이 접근해야 "충돌처럼 보이는" 근접 신호가 생기고,
동시에 접촉(따라서 GT 충돌 이벤트)은 한 건도 나오면 안 된다. 접촉이 섞이면
모델이 "충돌"이라고 답했을 때 그것이 오탐인지 실제로 맞힌 것인지 구분할 수
없기 때문이다.

이 두 조건을 만들도록 안무(choreography, `WanderController`가 매 물리 스텝마다
객체의 헤딩과 속도를 정하는 규칙) 자체가 바뀌는 것이 `near_miss_gap > 0`
설정이다. 짝끼리 서로 접근하되 중심 거리가 `gap` 아래로는 절대 내려가지
않게 하여 접촉이 0건인 클립을 만든다.

두 가지 안무 모드가 있다.

- `"stop"`(v1): 접근 중 두 객체의 "전체 속도 합"을 `(최근접 거리 - gap) /
  (2×dt)` 이하로 캡해 감속시키고, gap 근처에서 완전히 멈췄다가(hold) 다시
  출발한다. 이 감속+정지+방향전환이 실제 충돌의 운동 신호와 닮아 있어
  GUI 육안 검수에서 기각됐지만, "감속 단서"와 "근접 단서"를 분리해 보는
  대조군으로 남아 있다.
- `"swerve"`(기본): 전체 속력은 항상 유지한 채로, 이웃 방향(r̂)에 대한
  반경 성분에만 캡을 걸고 캡이 빼앗은 만큼을 접선 방향으로 돌려준다
  (`_swerve_direction`). 이렇게 하면 감속이나 정지 없이 스치듯 커브를 그리며
  지나간다.

swerve는 세 겹으로 동작한다(v3, 클래스 독스트링 참조). 회피 개시 반경
(`near_miss_avoid_frac × gap`) 안에 들어오면 목표 헤딩을 미리 "비껴가는
접선"으로 바꾸고(`_miss_angle_target`), 조향률 상한(`near_miss_turn_radius_frac`
× gap이 최소 선회 반경)으로 한 틱에 급하게 꺾지 않게 제한하며
(`_rate_limit_heading`), 마지막으로 반경 성분 하드 캡(`_swerve_direction`)이
"어떤 쌍도 gap 아래로 붙지 않는다"는 불변식을 수학적으로 보증한다. 앞의 둘은
사람 눈에 자연스러운 회피로 보이게 하는 표현 장치이고, 마지막 하나만이
GT 무오염을 지키는 안전망이다. v2(반경 캡만 있던 버전)는 캡이 gap 코앞에서야
걸려 급선회가 발생했고, 이것이 GUI 육안 검수에서 "투명한 벽에 반사"로
기각된 원인이었다 — 충돌 인상을 만드는 지배 신호는 속력이 아니라 곡률
(방향 변화율)이라는 실측 교훈이 v3 설계의 근거다.

실제 near-miss 클립 하나에서 두 객체의 중심 궤적을 위에서 내려다본 평면
(x-z)에 그리면 이 회피 곡선이 어떤 모양인지 확인할 수 있다.

<!-- 이미지 TODO | 파일: images/readme/physics-03-nearmiss-trajectory.png | 플롯: near-miss 조건으로 생성한 trace CSV 한 쌍(짝을 이루는 두 objid)의 x, z 열을 시간순으로 이어 그린 x-z 평면 궤적. 두 객체를 색으로 구분하고, 최근접 지점(중심 거리가 gap에 가장 가까워지는 프레임)에 마커를 표시 | 형식: png -->
![그림 3. near-miss 짝의 위에서 본 궤적](../../../../images/readme/physics-03-nearmiss-trajectory.png)
*그림 3. 두 객체가 서로에게 다가가다 gap 아래로 붙지 않고 곡선을 그리며 스쳐 지나가는 swerve 안무의 x-z 궤적.*

정면으로 다가오던 두 궤적이 최근접 지점 부근에서 완만하게 휘어지는 것이
v3의 조향률 제한과 반경 캡이 함께 만드는 모양이며, 급격한 꺾임이 아니라는
점이 v2에서 기각된 "투명한 벽 반사" 인상과의 차이다.

v4는 여기에 **대칭 파괴**를 더한다. v3까지는 조우가 일어나는 장소와 기하가
매번 거의 같아(짝이 동시에 같은 속도로 서로를 정면 조준) near-miss 클립들이
강하게 상관되고, 측정 관점에서 유효 표본 수가 명목 개수보다 작아지는 문제가
있었다. v4는 접근 개시 지터(`near_miss_start_jitter_s`, 객체마다 0~지터초
무작위 지연 뒤 짝을 조준), 비대칭 순항 속도(`near_miss_speed_min_frac`/
`_max_frac`, 사이클마다 독립 추첨), 이탈 방향 무작위화
(`near_miss_depart_spread_deg`, 짝의 반대 방향 ±spread 안에서 무작위)로
이 대칭을 세 군데에서 깬다. 이 세 장치는 모두 gap 불변식과 무관하게 동작한다
— 불변식의 보증은 여전히 반경 캡이 진다.

이 근접 불변식(gap 아래로 내려가지 않음)을 실제 구현은 "최근접 이웃
하나"만이 아니라 **전체 이웃**에 대해 매번 재검증한다(`_near_miss_step`의
속도 적용부 — 스침 경로의 "안전핀" 블록과 "이미 gap 안" 응급 이탈 경로.
514-536행의 독스트링이 그 증명을, 그 아래 속도 적용 루프가 구현을 담는다).
방향은 최근접 이웃 기준으로 하나만 고르되(여러 이웃을 순서대로 굽히면
뒤쪽 보정이 앞쪽 캡을 무너뜨릴 수 있어서), 그 방향의 반경 성분을 전체
이웃에 대해 다시 확인해 위반이 있으면 전체 속력을 가장 타이트한 위반
비율만큼 줄인다. 크기만 균등하게 줄이므로 이미 캡 안에 있던 다른 성분도
함께 작아져 재위반이 생기지 않는다.

### 6. 벽과 바닥은 항상 비가시(invisible)

`walls.py`가 만드는 5면(Floor, North, South, East, West)은 전부
`UsdGeom.Imageable(wall_prim).MakeInvisible()`로 캡처 영상을 오염시키지
않는다. 콜라이더는 가시성과 무관하게 동작하므로 배회, 벽 반사, contact
report는 그대로 유지된다(`walls.py:82-86`). 원통 콜라이더(`__phys_proxy__`)도
기본값 `visible=False`로 생성되며, 시각화가 필요할 때만
`wrap_with_collision_proxy(..., visible=True)`로 분홍색(1.0, 0.2, 0.8) 표시가
가능하다.

배회 시뮬레이션이 실제로 돌아가는 모습과, 평소엔 보이지 않는 이 벽 5면을
함께 확인하려면 벽을 임시로 가시화한 디버그 캡처가 필요하다.

<!-- 이미지 TODO | 파일: images/readme/physics-04-wander-arena.png | 촬영: USD Composer 뷰포트에서 물리 모드로 4개체 이상의 우주인이 배회 중인 장면을 위에서 내려다보는 각도로 캡처. 벽 5면은 기본이 비가시이므로 촬영 전 각 PhysicsWalls 프림(Floor/North/South/East/West)을 선택해 임시로 Visibility를 Inherited로 바꾸거나 뷰포트를 와이어프레임 모드로 전환해 벽의 경계가 함께 보이게 할 것 | 형식: png -->
![그림 4. 배회 중인 4개체와 아레나를 둘러싼 벽](../../../../images/readme/physics-04-wander-arena.png)
*그림 4. 평소 렌더에는 나타나지 않는 벽 5면을 임시로 가시화해 배회 아레나의 경계와 그 안에서 방향을 바꾸는 우주인들을 함께 담은 장면.*

이 벽은 캡처 영상에는 나타나지 않지만 콜라이더로는 항상 존재하므로,
배회 중인 우주인이 경계에 닿으면 반사되어 방향을 바꾸는 것을 이 그림에서
확인할 수 있다.

### 7. 결정론과 시계

`WanderController`는 `seed`를 주면 헤딩 선택이 결정적이 되어 에피소드
재현성을 보장한다. 시간축은 wall-clock이 아니라 sim-time 마스터 클럭을 쓴다
— 1순위로 PhysX 물리 스텝 이벤트(`subscribe_physics_step_events`)를 구독해
"물리가 1스텝 돌 때마다 정확히 1회" dt를 누적하고, 이 구독이 불가능한 환경
(유닛 테스트 등)에서만 update 이벤트 스트림으로 폴백한다. 렌더 대기로 인한
헛된 update 틱에서 오탐지(false-stuck)가 발생했던 실측 사고
(`wander_controller.py:389-425` 주석)가 이 설계의 근거다.

## 사용법

`physics/`는 Kit 확장 안에서만 의미가 있는 코드(USD 스테이지와 PhysX API에
의존)이므로 독립 CLI는 없다. 사용은 항상 `app/physics_service.py`를 경유한다.

```python
from gist.netai.time_travel_summarization.physics import (
    ensure_physics_scene, wrap_with_collision_proxy, unwrap,
    create_bounding_box, WanderController, TraceRecorder, CollisionRecorder,
)

ensure_physics_scene(stage)
create_bounding_box(stage, center=(0.0, 1.5, 0.0), size=(500.0, 300.0, 500.0))
wrapped_prim, proxy_radius = wrap_with_collision_proxy(stage, prim, shape="cylinder")

wander = WanderController(
    [wrapped_prim], speed=120.0, on_collision=my_callback,
    near_miss_gap=0.0,   # 0이면 기본 배회, >0이면 near-miss 대조군 안무
)
wander.start()
...
wander.stop()
unwrap(stage, prim)
```

near-miss 조향 파라미터(`near_miss_avoid_frac` 등)는 함수 인자로 명시하지
않으면 `TTS_NEAR_MISS_AVOID_FRAC` 같은 환경변수를, 그것도 없으면 코드
기본값을 순서대로 따른다(`_resolve_tunable`, `wander_controller.py:258-276`).
헤드리스 배치는 CLI 인자로, GUI 실행은 env로 코드 수정 없이 값을 바꿀 수
있게 하기 위한 설계다.

## 테스트

`tests/test_physics_imports.py`는 `omni`(Omniverse 런타임) 없이도 이
패키지가 임포트되고 `WanderController(prims=[])`가 생성 가능한지만 확인하는
최소 스모크 테스트다.

`tests/test_trace_recorder.py`는 `TraceRecorder`를 실제 USD prim 없이
`_world_position`을 몽키패치한 가짜 프림으로 검증한다 — 시작/정지 시
헤더만 있는 CSV가 만들어지는지, tick마다 행이 쓰이는지를 본다.

`tests/test_wander_stuck.py`는 `CollisionRecorder`와 `WanderController`를
가짜 prim(`_FakePrim`)으로 구동해 stuck 감지, velocity_mode 처리 등을
검증한다.

`tests/test_near_miss.py`는 near-miss 안무를 세 층에서 검증한다.
`WanderController._near_miss_step`을 가짜 프림 + 수치 적분으로 구동해
"중심 거리가 gap 아래로 내려가지 않는다"는 불변식이 실제로 지켜지는지 보는
층, `automation/generate_episodes.py`의 `check_near_miss_trace`로 산출된
trace를 사후 검증하는 층, 그리고 `near_miss_events`/`near_miss_diversity`로
조우가 일어나는 위치의 분포(v4 대칭 파괴 효과)를 검증하는 층이다.

## 한계와 주의

- 반지름 계수(0.4542)와 접촉 거리(60.0), 탄성(1.0) 같은 값은 **데이터
  생성 규약**이다. 이 값들을 바꾸면 이미 생성된 학습 데이터의 접촉 정의가
  바뀌므로, 재생성 없이 값만 바꾸면 train 데이터와 infer 시점의 규약이
  어긋난다.
- `_REST_DIMS` 캐시는 프로세스(세션) 생존 동안만 유효한 모듈 전역 딕셔너리다.
  세션을 새로 시작하면(에셋 rest 자세가 오염되지 않은 상태에서) 다시 채워진다.
- near-miss `"stop"` 모드는 GUI 육안 검수에서 기각된 안무이며, 기본값이
  아니다. 실험적 대조군 용도로만 남아 있다.
