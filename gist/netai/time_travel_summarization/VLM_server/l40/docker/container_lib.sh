#!/usr/bin/env bash
# 컨테이너화된 kit 실행 헬퍼 — run_job.sh/run_replay.sh가 USE_CONTAINER=1일 때만
# source한다. 베어메탈 kit 직접 실행부(CUDA_VISIBLE_DEVICES=$GPU "$KIT" "$APP" ...)를
# `docker run`으로 감싸되, 완료 마커·status 파일 규약·워치독 루프는 호출부(러너)
# 그대로 재사용한다 — 이 파일은 "kit을 무엇으로 감싸 띄우는가"만 책임진다.
#
# GPU 격리: --gpus '"device=$GPU"' — 컨테이너 안에서는 지정한 GPU 하나만 인덱스 0으로
#   보인다(nvidia-container-toolkit이 remap). 따라서 컨테이너 안 kit에는 항상
#   --/renderer/activeGpu=0을 넘긴다 — 호스트 GPU 인덱스를 넘기면 안 됨(존재하지 않는
#   디바이스 지정 오류).
# 드라이버 capability: 베이스 이미지가 Vulkan/EGL(graphics)까지 기본 노출한다는 보장이
#   없어 방어적으로 NVIDIA_DRIVER_CAPABILITIES=all을 명시(누락 시 헤드리스 렌더러가
#   "ICD not found" 류로 조용히 실패하는 흔한 실패 모드 회피).
# 네트워크: --network host — minIO(HTTPS 아웃바운드)·Nucleus(LAN IP) 접근에 별도
#   포트 매핑 불필요(베어메탈과 동일 네트워크 가시성).
# 마운트: kit 빌드·확장 소스는 이미지에 굽지 않고 호스트와 동일 절대경로로 볼륨
#   마운트한다 — KIT/APP/EXT_ROOT 하위 산출물 경로(러너가 이미 해석해 둔 값)가
#   컨테이너 안에서도 별도 경로 치환 없이 그대로 유효하다.
# 셰이더 캐시: named volume(기본 ttsum-kit-shadercache)으로 잡 간 유지 — 없으면
#   `docker run --rm`이 매 잡마다 새 컨테이너 파일시스템이라 첫 렌더마다 셰이더
#   컴파일이 반복돼 느려진다.
#
# 필요 env (없으면 기본값 또는 즉시 에러):
#   KIT_CONTAINER_IMAGE   docker/Dockerfile로 빌드한 이미지 태그 (필수)
#   L40_ENV_FILE          minIO/Nucleus 자격증명 파일 (기본 $HOME/wonjune/.env.l40)
#   (셰이더 등 캐시는 호스트 홈 ~/.cache·~/.local/share·~/.nvidia-omniverse를
#    native 경로로 바인드 마운트해 재사용 — 별도 env 없음)

container_kit_launch() {
  # container_kit_launch <container_name> <kit_root> <ext_root> <gpu> <kit_bin> <app> <kit-args...>
  local name="$1" kit_root="$2" ext_root="$3" gpu="$4" kit_bin="$5" app="$6"; shift 6
  local image="${KIT_CONTAINER_IMAGE:?KIT_CONTAINER_IMAGE 필요 (docker/Dockerfile로 빌드한 이미지 태그, 예: ttsum-kit-runtime:2026.2.3)}"
  local envfile="${L40_ENV_FILE:-$HOME/wonjune/.env.l40}"
  local env_args=()
  [ -f "$envfile" ] && env_args=(--env-file "$envfile")

  # netai(호스트) 사용자로 실행(아래 --user) → 산출물이 netai 소유라 root 청소 불편이
  # 없고 kit의 allow-root도 불필요. kit이 $HOME 기준으로 쓰는 캐시를 native 절대경로로
  # 마운트해 재사용한다(실측: ~/.cache = packman 17G + 셰이더, ~/.local/share = ov 27G,
  # ~/.nvidia-omniverse = config/logs). kit_root 하위 _build의 packman 심링크가 절대경로
  # /home/netai/.cache/packman를 가리키는 것도 ~/.cache 마운트로 해석된다. blast radius를
  # 좁히려 홈 전체가 아니라 이 캐시 경로들만 마운트 — .ssh·문서 등은 노출 안 함.
  # 존재하는 것만 마운트해, 없는 경로를 docker가 root 소유로 새로 만드는 것을 피한다.
  local mounts=(-v "$kit_root:$kit_root")
  local d
  for d in "$HOME/.cache" "$HOME/.local/share" "$HOME/.nvidia-omniverse" "$HOME/.config"; do
    [ -d "$d" ] && mounts+=(-v "$d:$d")
  done

  docker rm -f "$name" >/dev/null 2>&1 || true   # 이전 이상종료로 남은 동명 컨테이너 정리

  docker run --rm --init --name "$name" \
    --gpus "\"device=$gpu\"" \
    --network host \
    --user "$(id -u):$(id -g)" \
    -e HOME="$HOME" \
    -e NVIDIA_DRIVER_CAPABILITIES=all \
    -e CUDA_VISIBLE_DEVICES=0 \
    -e ACCEPT_EULA=Y \
    -e PRIVACY_CONSENT=Y \
    "${mounts[@]}" \
    "${env_args[@]}" \
    "$image" \
    "$kit_bin" "$app" "$@" --/renderer/activeGpu=0
}

container_cleanup() {
  # container_cleanup <container_name> — 워치독/트랩/종료부에서 안전망으로 호출.
  # `docker run --rm`이 정상 종료 시 자동 정리하지만, `docker run` 클라이언트
  # 프로세스가 SIGKILL로 죽는 경로(워치독 강제종료)에선 --sig-proxy가 신호를
  # 전달할 기회가 없어 컨테이너가 고아로 남을 수 있다 — GPU를 점유한 채 남으면
  # 브리프의 "단일 GPU 상호배제" 전제가 깨지므로 항상 명시적으로 정리한다.
  local name="$1"
  docker stop -t 5 "$name" >/dev/null 2>&1 || true
  docker rm -f "$name" >/dev/null 2>&1 || true
}
