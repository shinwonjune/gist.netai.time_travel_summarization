"""normalize_source 단위 테스트 — omni 무의존 (storage.normalize만 임포트)."""

import unittest

from gist.netai.time_travel_summarization.storage.normalize import normalize_source

BUCKET = "time-travel-summarization"


class NormalizeSourceTests(unittest.TestCase):
    def test_scheme_passthrough(self):
        uri = "s3://time-travel-summarization/replays/x.mp4"
        self.assertEqual(normalize_source(uri, bucket=BUCKET), uri)
        self.assertEqual(normalize_source("minio://b/k", bucket=BUCKET), "minio://b/k")

    def test_bucket_key_gets_s3_prefix(self):
        self.assertEqual(
            normalize_source(f"{BUCKET}/replays/j/x.mp4", bucket=BUCKET),
            f"s3://{BUCKET}/replays/j/x.mp4",
        )

    def test_windows_drive_backslash_passthrough(self):
        p = r"C:\Users\x\v.mp4"
        self.assertEqual(normalize_source(p, bucket=BUCKET), p)

    def test_windows_drive_forwardslash_passthrough(self):
        p = "C:/Users/x/v.mp4"
        self.assertEqual(normalize_source(p, bucket=BUCKET), p)

    def test_unc_passthrough(self):
        p = r"\\server\share\v.mp4"
        self.assertEqual(normalize_source(p, bucket=BUCKET), p)

    def test_bare_filename_passthrough(self):
        self.assertEqual(normalize_source("video_19.mp4", bucket=BUCKET), "video_19.mp4")

    def test_empty_string(self):
        self.assertEqual(normalize_source("", bucket=BUCKET), "")

    def test_no_bucket_disables_prefix(self):
        import os

        prev = os.environ.pop("MINIO_BUCKET", None)  # env 폴백까지 비워 ③ 규칙 비활성
        try:
            self.assertEqual(
                normalize_source(f"{BUCKET}/replays/x.mp4", bucket=None),
                f"{BUCKET}/replays/x.mp4",
            )
        finally:
            if prev is not None:
                os.environ["MINIO_BUCKET"] = prev

    def test_whitespace_trimmed(self):
        self.assertEqual(
            normalize_source(f"  {BUCKET}/x.mp4  ", bucket=BUCKET),
            f"s3://{BUCKET}/x.mp4",
        )

    def test_env_bucket_used_when_arg_none(self):
        import os

        prev = os.environ.get("MINIO_BUCKET")
        os.environ["MINIO_BUCKET"] = BUCKET
        try:
            self.assertEqual(
                normalize_source(f"{BUCKET}/x.mp4"),
                f"s3://{BUCKET}/x.mp4",
            )
        finally:
            if prev is None:
                del os.environ["MINIO_BUCKET"]
            else:
                os.environ["MINIO_BUCKET"] = prev


if __name__ == "__main__":
    unittest.main()
