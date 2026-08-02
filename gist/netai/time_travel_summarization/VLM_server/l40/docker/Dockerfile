# Kit 헤드리스 잡(재연 렌더·생성) 컨테이너 격리용 이미지.
#
# 왜 Isaac Sim 컨테이너가 아니라 Kit 베이스인가: 이 확장은 로봇/센서 시뮬레이션 등
# Isaac Sim 전용 기능 없이 omni.kit.* + omni.replicator.core만 쓴다. Isaac Sim
# 컨테이너(수 GB, ROS·물리엔진 확장 다수 포함)는 우리 워크로드엔 과대적재라
# "Kit 런타임 필수만" 들어간 훨씬 가벼운 베이스가 맞다. 근거(NGC 카탈로그, 2026-08 확인):
#   https://catalog.ngc.nvidia.com/orgs/nvidia/teams/omniverse/containers/ov-base-ubuntu-22
#   "a base container ready for Kit Applications. It contains only the essentials
#    to run the Kit runtime." (압축 크기 ~103MB, 태그 2026.2.3 = latest)
#
# kit 빌드(_build)와 확장 소스는 이 이미지에 굽지 않는다 — L40에 이미 있는 빌드를
# 런타임에 볼륨 마운트로 재사용한다(docker/container_lib.sh 참고). 이 이미지는 그
# 위에 얹는 최소 OS 레이어(kit이 번들한 파이썬 환경 밖의 시스템 바이너리)만 담당한다.
#
FROM nvcr.io/nvidia/omniverse/ov-base-ubuntu-22:2026.2.3

# apt 설치는 하지 않는다 — 두 가지 이유:
#  1) 베이스 이미지의 apt가 NVIDIA 사내 저장소(urm.nvidia.com)를 바라보는데, 이는
#     L40에서 해석 불가한 호스트라 `apt-get install`이 "Unable to locate package"로
#     실패한다(실측). 공개 Ubuntu 저장소를 추가하지 않는 한 apt로는 아무것도 못 받는다.
#  2) 애초에 불필요하다:
#     - ffmpeg: 렌더 인코더(video_capture/encoder.py)가 imageio(kit 파이썬 환경에
#       번들된 imageio_ffmpeg 바이너리)를 우선 사용하고 system ffmpeg는 폴백일 뿐이다.
#       kit _build를 볼륨 마운트하므로 imageio는 컨테이너 안에서도 그대로 가용.
#     - tini: `docker run --init`(Docker 내장 tini, container_lib.sh 참고)로 대체.
#     - ca-certificates: 검증(로컬 mp4 렌더)엔 불필요. 향후 minIO HTTPS 업로드가
#       필요하면 호스트 인증서 마운트(-v /etc/ssl/certs:/etc/ssl/certs:ro)로 해결.
#
# 이 이미지가 베이스에 더하는 것은 단 하나: 런타임을 root로 고정(USER root)해
# container_lib.sh의 셰이더 캐시 마운트(/root/.nvidia-omniverse) 쓰기를 가능케 함.
# (베이스는 비-root 기본이라 그대로 쓰면 /root/ 쓰기가 막힌다.)
USER root
