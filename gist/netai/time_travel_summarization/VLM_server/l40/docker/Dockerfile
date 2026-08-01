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
# 필요 apt 패키지 근거:
#   - ffmpeg: video_capture/encoder.py가 system ffmpeg로 폴백(imageio 미가용 시) —
#             베어메탈 L40엔 이미 설치돼 있지만 컨테이너 안엔 없으므로 필수.
#   - ca-certificates: minIO(HTTPS, api.minio.mobilex.kr) 업로드에 TLS 검증 필요.
#   - tini: 아래 ENTRYPOINT 주석 참고.
FROM nvcr.io/nvidia/omniverse/ov-base-ubuntu-22:2026.2.3

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      ca-certificates \
      tini \
    && rm -rf /var/lib/apt/lists/*

# tini를 PID 1로 둔다 — kit 프로세스가 그대로 컨테이너 PID 1이 되면 SIGTERM
# 처리·좀비 reap이 불완전해질 수 있다. run_job.sh/run_replay.sh의 워치독은
# `docker run`(--sig-proxy 기본 on)으로 보낸 신호가 tini를 거쳐 컨테이너 안
# kit 프로세스까지 정상 전달되는 것을 전제로 트랩·타임아웃 로직을 재사용한다.
ENTRYPOINT ["/usr/bin/tini", "--"]
