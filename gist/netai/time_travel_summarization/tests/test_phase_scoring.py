"""위상 분해 채점 드라이버(automation/phase_scoring.py) 순수 로직 테스트 — 네트워크 불필요.

검증 대상 (docs/위상분해_실험설계.md §6):
  - 엔드포인트 정규화(--endpoint에 /v1이 붙어도 base_url은 /v1 없이)
  - 응답 상태 분류: 발화 / 정상 비발화(빈 배열) / 형식 이탈 / 빈 본문
  - 실패 게이트(누적 실패율 30% 초과 시 중단)
  - 조건별 집계와 보고 순서, 실패를 발화율 분모에서 제외하는 규칙
  - manifest 로딩의 제외 처리(추출 실패 클립·파일 없음)와 이어하기(resume) 로딩
  - 추론 계약: 클립 1개가 요청 1건(청크 1개)으로 나가고, 프롬프트가 프로덕션
    프리셋(twin_view) 그대로인지 — ffmpeg/HTTP는 가짜로 대체해 네트워크 없이 검증
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gist.netai.time_travel_summarization.automation import phase_scoring
from gist.netai.time_travel_summarization.automation.phase_scoring import (
    ABORT_MIN_ATTEMPTS,
    CONDITION_ORDER,
    classify_response,
    load_done,
    load_manifest,
    normalize_base_url,
    render_summary,
    score_entry,
    should_abort,
    summarize,
)
from gist.netai.time_travel_summarization.utils.vllm_client import VLLMClient
from gist.netai.time_travel_summarization.vlm_client.prompts import PROMPTS


class NormalizeBaseUrlTest(unittest.TestCase):
    def test_strips_v1_suffix(self):
        # VLLMClient가 /v1/chat/completions를 스스로 붙이므로 base에 /v1이 남으면 안 된다
        self.assertEqual(normalize_base_url("http://localhost:38011/v1"),
                         "http://localhost:38011")
        self.assertEqual(normalize_base_url("http://localhost:38011/v1/"),
                         "http://localhost:38011")

    def test_keeps_plain_host(self):
        self.assertEqual(normalize_base_url("http://localhost:18011"),
                         "http://localhost:18011")


class ClassifyResponseTest(unittest.TestCase):
    def test_plain_json_array_is_utterance(self):
        status, events = classify_response('[{"12:00:15": [1, 2]}]')
        self.assertEqual(status, "events")
        self.assertEqual(events, {12 * 3600 + 15: [1, 2]})

    def test_code_fenced_array_is_utterance(self):
        status, events = classify_response('```json\n[{"12:00:15": [3]}]\n```')
        self.assertEqual(status, "events")
        self.assertEqual(events, {12 * 3600 + 15: [3]})

    def test_empty_array_is_silent(self):
        status, events = classify_response("[]")
        self.assertEqual((status, events), ("silent", {}))

    def test_prose_is_unparsed(self):
        # 형식을 벗어난 응답은 비발화로 세되 silent와 구분해 요약에 드러나야 한다
        status, _ = classify_response("The objects do not overlap in this clip.")
        self.assertEqual(status, "unparsed")

    def test_truncated_array_is_unparsed(self):
        status, _ = classify_response('[{"12:00:15": [1, 2}')
        self.assertEqual(status, "unparsed")

    def test_blank_is_empty(self):
        self.assertEqual(classify_response("   ")[0], "empty")
        self.assertEqual(classify_response("")[0], "empty")


class AbortGateTest(unittest.TestCase):
    def test_no_abort_before_minimum_attempts(self):
        # 초반 산발 실패로 런이 죽으면 안 된다 — 하한 미만에서는 실패율 100%라도 계속
        self.assertFalse(should_abort(1, 1))
        self.assertFalse(should_abort(ABORT_MIN_ATTEMPTS - 1, ABORT_MIN_ATTEMPTS - 1))

    def test_abort_above_threshold(self):
        self.assertTrue(should_abort(10, 4))     # 40% > 30%
        self.assertFalse(should_abort(10, 3))    # 30%는 초과가 아니다
        self.assertFalse(should_abort(100, 20))


def _row(condition, ok=True, spoke=False, status="silent"):
    return {"condition": condition, "clip": f"{condition}/x.mp4", "ok": ok,
            "spoke": spoke, "status": status}


class SummarizeTest(unittest.TestCase):
    def test_rate_excludes_failures_from_denominator(self):
        rows = [_row("full", spoke=True, status="events"),
                _row("full", spoke=True, status="events"),
                _row("full"),
                _row("full", ok=False, status="failed")]
        s = summarize(rows)
        c = s["conditions"]["full"]
        self.assertEqual((c["n"], c["spoke"], c["failed"]), (3, 2, 1))
        self.assertAlmostEqual(c["rate"], 2 / 3, places=4)
        self.assertEqual((s["total_scored"], s["total_failed"]), (3, 1))

    def test_unparsed_counted_separately(self):
        rows = [_row("control", status="unparsed"), _row("control")]
        c = summarize(rows)["conditions"]["control"]
        self.assertEqual((c["n"], c["spoke"], c["unparsed"]), (2, 0, 1))
        self.assertEqual(c["rate"], 0.0)

    def test_condition_report_order(self):
        rows = [_row(c) for c in reversed(CONDITION_ORDER)]
        self.assertEqual(list(summarize(rows)["conditions"]), CONDITION_ORDER)

    def test_unknown_condition_kept_at_end(self):
        rows = [_row("weird"), _row("full")]
        self.assertEqual(list(summarize(rows)["conditions"]), ["full", "weird"])

    def test_all_failed_condition_has_no_rate(self):
        c = summarize([_row("near_miss", ok=False, status="failed")])["conditions"]["near_miss"]
        self.assertEqual((c["n"], c["failed"], c["rate"]), (0, 1, None))


class RenderSummaryTest(unittest.TestCase):
    def test_table_rows_and_delta_vs_full(self):
        rows = ([_row("full", spoke=True, status="events")] * 4 + [_row("full")]
                + [_row("approach_only", spoke=True, status="events")]
                + [_row("approach_only")] * 3)
        md = render_summary(summarize(rows), {"model": "M"})
        self.assertIn("| full | 5 | 4 | 0.800 | - |", md)
        self.assertIn("| approach_only | 4 | 1 | 0.250 | -0.550 |", md)
        self.assertIn("`M`", md)

    def test_control_utterance_raises_warning(self):
        # 설계 §6: 무관 대조에서 발화가 나오면 표 해석을 보류해야 한다
        md = render_summary(summarize([_row("control", spoke=True, status="events")]))
        self.assertIn("WARNING", md)
        self.assertIn("control", md)

    def test_clean_control_has_no_warning(self):
        md = render_summary(summarize([_row("control"), _row("control")]))
        self.assertNotIn("WARNING", md)


class ManifestLoadTest(unittest.TestCase):
    def test_splits_usable_and_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "full").mkdir()
            (root / "full" / "a.mp4").write_bytes(b"x")
            (root / "full" / "b.mp4").write_bytes(b"x")
            manifest = root / "clips_manifest.json"
            manifest.write_text(json.dumps({"clips": [
                {"condition": "full", "clip": "full/a.mp4"},
                {"condition": "full", "clip": "full/b.mp4", "error": "ffmpeg failed"},
                {"condition": "control", "clip": "control/c.mp4"},   # 파일 없음
            ]}), encoding="utf-8")
            usable, excluded = load_manifest(manifest, root)
        self.assertEqual([u["clip"] for u in usable], ["full/a.mp4"])
        self.assertEqual(sorted(e["exclude_reason"] for e in excluded),
                         ["clip file missing", "manifest error: ffmpeg failed"])
        self.assertTrue(usable[0]["clip_path"].endswith("a.mp4"))


class ResumeLoadTest(unittest.TestCase):
    def test_keeps_success_rows_and_retries_failures(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "results.jsonl"
            path.write_text(
                json.dumps({"clip": "full/a.mp4", "ok": True, "spoke": True}) + "\n"
                + json.dumps({"clip": "full/b.mp4", "ok": False}) + "\n"
                + '{"clip": "full/c.mp4", "ok": tr',      # 중단 시점의 잘린 행
                encoding="utf-8")
            done = load_done(path)
        self.assertEqual(list(done), ["full/a.mp4"])

    def test_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(load_done(Path(td) / "none.jsonl"), {})


class _FakeClient(VLLMClient):
    """ffmpeg·HTTP를 가짜로 대체한 클라이언트 — 요청 조립 경로는 프로덕션 그대로 탄다."""

    def __init__(self, duration, replies):
        super().__init__("http://fake", PROMPTS)
        self.duration = duration
        self.replies = list(replies)      # 요청마다 하나씩 소비: dict=응답, Exception=실패
        self.payloads = []

    def probe_duration(self, video):
        return self.duration

    def _encode_chunk(self, video, start, dur):
        self.encoded = (start, dur)
        return "QUJD"

    def _post(self, payload):
        self.payloads.append(payload)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _reply(content):
    return {"choices": [{"message": {"content": content},
                         "logprobs": {"content": [{"logprob": -0.5}, {"logprob": -0.1}]}}]}


class InferenceContractTest(unittest.TestCase):
    def setUp(self):
        # analyze_video가 실재하는 파일을 요구한다 — 내용은 가짜 ffmpeg가 무시한다
        self._td = tempfile.TemporaryDirectory()
        clip = Path(self._td.name) / "a.mp4"
        clip.write_bytes(b"fake")
        self.entry = {"condition": "full", "clip": "full/a.mp4", "clip_path": str(clip),
                      "source_video": "ep.mp4", "t_ref": "2026-01-01 12:00:15.000"}
        self.addCleanup(self._td.cleanup)

    def test_one_clip_is_one_request_with_production_prompt(self):
        client = _FakeClient(2.04, [_reply('[{"12:00:15": [1, 2]}]')])
        row = score_entry(client, self.entry, model="M")

        # 클립 하나당 요청 하나 — 2.04초 클립이 0청크로 조용히 누락되지 않는다
        self.assertEqual(len(client.payloads), 1)
        self.assertEqual(client.encoded, (0.0, 2.04))    # 클립 전체가 한 청크
        payload = client.payloads[0]
        preset = PROMPTS["twin_view"]
        self.assertEqual(payload["messages"][0]["content"], preset["system_prompt"])
        self.assertEqual(payload["messages"][1]["content"][1]["text"], preset["prompt"])
        self.assertEqual(payload["messages"][1]["content"][0]["type"], "video_url")
        self.assertEqual(payload["model"], "M")

        self.assertTrue(row["ok"] and row["spoke"])
        self.assertEqual(row["status"], "events")
        self.assertEqual(row["events"], {str(12 * 3600 + 15): [1, 2]})
        self.assertEqual(row["avg_logprob"], -0.3)       # logprob이 있으면 신뢰 신호로 기록
        self.assertEqual(row["attempts"], 1)

    def test_short_clip_still_sends_one_request(self):
        """재인코딩 결과가 2.0초에 못 미쳐도(1.98s) 요청이 나가야 한다."""
        client = _FakeClient(1.98, [_reply("[]")])
        row = score_entry(client, self.entry, model="M")
        self.assertEqual(len(client.payloads), 1)
        self.assertTrue(row["ok"])
        self.assertFalse(row["spoke"])
        self.assertEqual(row["status"], "silent")

    def test_retries_once_then_succeeds(self):
        client = _FakeClient(2.0, [RuntimeError("boom"), _reply("[]")])
        with mock.patch.object(phase_scoring, "RETRY_SLEEP_S", 0.0):
            row = score_entry(client, self.entry, model="M")
        self.assertTrue(row["ok"])
        self.assertEqual(row["attempts"], 2)

    def test_records_failure_after_second_attempt(self):
        # 조용한 실패 금지 — 실패도 행으로 남고 원인 문자열이 붙는다
        client = _FakeClient(2.0, [RuntimeError("boom"), RuntimeError("boom again")])
        with mock.patch.object(phase_scoring, "RETRY_SLEEP_S", 0.0):
            row = score_entry(client, self.entry, model="M")
        self.assertFalse(row["ok"])
        self.assertEqual(row["status"], "failed")
        self.assertIn("boom again", row["error"])
        self.assertEqual(len(client.payloads), 2)


if __name__ == "__main__":
    unittest.main()
