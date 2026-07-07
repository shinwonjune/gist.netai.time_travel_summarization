#!/usr/bin/env python3
"""minIO 데이터 레이크 → L40 로컬 스테이징.

에피소드 run 전체(비디오·meta·충돌CSV·trace·_run_manifest.json)를 내려받는다.
재실행 시 같은 크기의 기존 파일은 건너뛰므로 중단 후 이어받기가 된다.
전송량·처리량을 로그로 남긴다(레이크 처리량 검증 실측 자료).

사용:
    set -a; source .env.l40; set +a
    python3 stage_episodes.py --prefix episodes/prod-20260707 --out ~/ttsum-data/episodes/prod-20260707

필요 env: MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, MINIO_BUCKET
          (MINIO_SECURE=true면 https)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse


def make_client():
    from minio import Minio

    endpoint = os.environ["MINIO_ENDPOINT"]
    secure = os.environ.get("MINIO_SECURE", "true").lower() == "true"
    parsed = urlparse(endpoint if "://" in endpoint else f"//{endpoint}", scheme="")
    host = parsed.netloc or parsed.path  # "https://h" → h, "h:9000" → h:9000
    if parsed.scheme:
        secure = parsed.scheme == "https"
    return Minio(
        host,
        access_key=os.environ["MINIO_ACCESS_KEY"],
        secret_key=os.environ["MINIO_SECRET_KEY"],
        secure=secure,
        region=os.environ.get("MINIO_REGION", "us-east-1"),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prefix", required=True, help="버킷 내 접두어 (예: episodes/prod-20260707)")
    ap.add_argument("--out", required=True, help="로컬 저장 루트 (접두어 이하 구조 보존)")
    ap.add_argument("--bucket", default=os.environ.get("MINIO_BUCKET", ""))
    args = ap.parse_args()
    if not args.bucket:
        print("ERROR: --bucket 또는 MINIO_BUCKET 필요", file=sys.stderr)
        return 2

    client = make_client()
    prefix = args.prefix.strip("/") + "/"
    out_root = Path(args.out).expanduser()
    objs = list(client.list_objects(args.bucket, prefix=prefix, recursive=True))
    if not objs:
        print(f"ERROR: s3://{args.bucket}/{prefix} 아래 객체 없음", file=sys.stderr)
        return 1

    total_bytes = sum(o.size for o in objs)
    print(f"[stage] {len(objs)} objects, {total_bytes / 1e6:.1f} MB from s3://{args.bucket}/{prefix}")
    done_bytes = 0
    skipped = 0
    t0 = time.time()
    for o in objs:
        rel = o.object_name[len(prefix):]
        dst = out_root / rel
        if dst.exists() and dst.stat().st_size == o.size:
            skipped += 1
            done_bytes += o.size
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        t1 = time.time()
        client.fget_object(args.bucket, o.object_name, str(dst))
        dt = max(time.time() - t1, 1e-6)
        done_bytes += o.size
        print(f"[stage] {rel}  {o.size / 1e6:.1f} MB in {dt:.1f}s ({o.size / 1e6 / dt:.1f} MB/s)"
              f"  [{done_bytes / 1e6:.0f}/{total_bytes / 1e6:.0f} MB]")
    wall = max(time.time() - t0, 1e-6)
    print(f"[stage] DONE: {len(objs) - skipped} downloaded, {skipped} skipped, "
          f"{total_bytes / 1e6:.1f} MB total, wall {wall:.0f}s ({total_bytes / 1e6 / wall:.1f} MB/s avg)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
