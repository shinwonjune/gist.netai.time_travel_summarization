"""
MinIO 연결 read-only 점검.

- .env에서 자격증명을 읽음 (시크릿 하드코딩 없음)
- ListBuckets / ListObjectsV2 만 호출 (쓰기/삭제 없음)
- pure stdlib AWS Signature V4 (외부 패키지 불필요)

사용: python3 minio_probe.py [prefix]
"""
import datetime
import hashlib
import hmac
import os
import sys
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


def load_env(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def sign(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def signing_key(secret, date_stamp, region, service):
    k = sign(("AWS4" + secret).encode(), date_stamp)
    k = sign(k, region)
    k = sign(k, service)
    return sign(k, "aws4_request")


def signed_get(endpoint, access, secret, region, path, query=""):
    host = endpoint.split("://", 1)[1]
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical_headers = f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = f"GET\n{path}\n{query}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    scope = f"{date_stamp}/{region}/s3/aws4_request"
    sts = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    sig = hmac.new(signing_key(secret, date_stamp, region, "s3"), sts.encode(), hashlib.sha256).hexdigest()
    auth = f"AWS4-HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={signed_headers}, Signature={sig}"
    url = f"{endpoint}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", auth)
    req.add_header("x-amz-date", amz_date)
    req.add_header("x-amz-content-sha256", payload_hash)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main():
    env_path = Path(__file__).resolve().parent.parent / ".env"
    env = {**load_env(env_path), **os.environ}

    endpoint = env.get("MINIO_ENDPOINT", "").rstrip("/")
    access = env.get("MINIO_ACCESS_KEY")
    secret = env.get("MINIO_SECRET_KEY")
    bucket = env.get("MINIO_BUCKET")
    region = env.get("MINIO_REGION", "us-east-1")
    secure_flag = env.get("MINIO_SECURE", "false").lower() == "true"

    print(f"endpoint     : {endpoint}")
    print(f"MINIO_SECURE : {secure_flag}  (endpoint scheme: {endpoint.split('://',1)[0]})")
    if endpoint.startswith("http://") and secure_flag:
        print("  ⚠ scheme/secure 불일치 — TLS handshake 실패 예상")

    if not all([endpoint, access, secret, bucket]):
        print("✗ .env에 필수 키 누락")
        sys.exit(2)

    # 1. ListBuckets
    status, body = signed_get(endpoint, access, secret, region, "/")
    print(f"\n[1] ListBuckets       : HTTP {status}")
    if status != 200:
        print(body.decode(errors='replace')[:400])
        sys.exit(1)
    root = ET.fromstring(body)
    buckets = [b.findtext("s3:Name", namespaces=NS) for b in root.iter(f"{{{NS['s3']}}}Bucket")]
    print(f"    buckets visible   : {buckets}")
    print(f"    target '{bucket}' : {'OK' if bucket in buckets else 'MISSING'}")

    if bucket not in buckets:
        print("\n→ 버킷이 없습니다. MinIO 콘솔/mc로 먼저 생성하세요:")
        print(f"  mc mb <alias>/{bucket}")
        sys.exit(0)

    # 2. ListObjectsV2 with delimiter=/ to show "folders"
    prefix = sys.argv[1] if len(sys.argv) > 1 else ""
    # SigV4: canonical query는 알파벳 순 정렬 + RFC3986 인코딩 필수
    params = sorted([("delimiter", "/"), ("list-type", "2"), ("prefix", prefix)])
    q = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    status, body = signed_get(endpoint, access, secret, region, f"/{bucket}", query=q)
    print(f"\n[2] ListObjectsV2 prefix='{prefix}' delim='/': HTTP {status}")
    if status != 200:
        print(body.decode(errors='replace')[:400])
        sys.exit(1)
    root = ET.fromstring(body)
    prefixes = [p.text for p in root.iter(f"{{{NS['s3']}}}Prefix")]
    contents = [
        (c.findtext("s3:Key", namespaces=NS), int(c.findtext("s3:Size", default="0", namespaces=NS)))
        for c in root.iter(f"{{{NS['s3']}}}Contents")
    ]
    print(f"    'folders' (CommonPrefixes): {prefixes if prefixes else '(none)'}")
    print(f"    objects                  : {contents if contents else '(none)'}")

    print("\n✓ read-only probe complete")


if __name__ == "__main__":
    main()
