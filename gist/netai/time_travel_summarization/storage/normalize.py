"""사용자 입력 경로 정규화 — minIO 콘솔 복사(`버킷/키`)에 s3:// 접두를 붙인다."""

import os
import re

# 드라이브 문자(`X:\` / `X:/`) 판정 — 윈도우 로컬 경로는 정규화 대상이 아니다.
_WIN_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def normalize_source(value: str, bucket: str | None = None) -> str:
    """minIO 콘솔에서 복사한 `버킷/키` 문자열에만 s3:// 접두를 붙인다.

    규칙(순서대로): ① "://" 포함 → 그대로 ② 윈도우 경로(드라이브 문자 `X:\\`/`X:/`
    또는 `\\\\`) → 그대로 ③ `버킷명/`으로 시작 → "s3://" + value ④ 그 외(맨 파일명 등)
    → 그대로. bucket 미지정 시 os.environ["MINIO_BUCKET"](없으면 ③ 규칙 비활성).
    """
    text = value.strip()
    if "://" in text:
        return text
    if _WIN_DRIVE.match(text) or text.startswith("\\\\"):
        return text
    if bucket is None:
        bucket = os.environ.get("MINIO_BUCKET")
    if bucket and text.startswith(bucket + "/"):
        return "s3://" + text
    return text
