"""OpenAI 호환(vLLM) 직접 추론 클라이언트 — VSS(VIA) 대체 경로.

VSS가 서버측에서 하던 일(업로드 → 청크 분할 → VLM 호출)을 클라이언트가 직접 수행한다:
    ffmpeg로 2초 청크 슬라이스 → base64 data URI(video_url)로
    POST {base_url}/v1/chat/completions → 응답 수집.

- 출력 형식은 기존 파이프라인 호환: events/core.py가 읽는
  ``{"chunk_responses": [{"content": ...}, ...]}`` 구조를 유지한다.
- 슬라이스 규칙(재인코딩·경계)은 학습 데이터 빌더(build_dataset.slice_clip)와
  동일하게 맞춘다 — 학습 클립과 추론 클립이 같은 분포여야 LoRA 성능이 이전된다.
- 프레임 예산 주의: 학습은 클립당 20프레임(NFRAMES=20). vLLM의 프레임 샘플링은
  "서버 기동 플래그" ``--media-io-kwargs '{"video": {"num_frames": 20}}'`` 로
  맞춰야 한다(클라이언트가 요청별로 바꿀 수 없음 — 서빙 스크립트 확인 필수).
- Kit 무의존(오프라인 테스트 가능). ffmpeg는 PATH → imageio_ffmpeg 동봉 바이너리 순.

self-test:  python3 vllm_client.py
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def _find_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError("ffmpeg not found (PATH에도, imageio-ffmpeg에도 없음)") from exc


def parse_duration_s(ffmpeg_stderr: str) -> Optional[float]:
    """`ffmpeg -i` stderr의 'Duration: HH:MM:SS.xx'에서 초를 파싱."""
    m = _DURATION_RE.search(ffmpeg_stderr)
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mi * 60 + s


def chunk_spans(duration_s: float, chunk_s: float) -> List[Tuple[float, float]]:
    """(start, dur) 목록. 꽉 찬 청크만 — build_dataset의 클립 규칙과 동일(잔여 버림)."""
    n = int(duration_s // chunk_s)
    return [(i * chunk_s, chunk_s) for i in range(n)]


def _avg_logprob(out: dict) -> Optional[float]:
    """생성 토큰들의 평균 로그확률 — 이벤트 랭킹용 신뢰 신호.

    프롬프트·학습을 바꾸지 않는 추론 부산물(요청에 logprobs=True만 추가).
    응답에 logprobs가 없으면 None(구버전 서버 호환).
    """
    try:
        toks = out["choices"][0]["logprobs"]["content"]
        lps = [t["logprob"] for t in toks if t.get("logprob") is not None]
        return round(sum(lps) / len(lps), 4) if lps else None
    except (KeyError, TypeError, IndexError):
        return None


class VLLMClient:
    """vLLM OpenAI 호환 서버로 비디오 추론을 보내는 클라이언트."""

    def __init__(self, base_url: str, prompt_presets: Optional[Dict[str, Any]] = None,
                 default_chunk_duration: float = 2.0, request_timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.prompt_presets = prompt_presets or {}
        self.default_chunk_duration = float(default_chunk_duration)
        self.request_timeout = float(request_timeout)

    # ---- 프롬프트 -----------------------------------------------------------

    def _resolve_prompts(self, preset_name: Optional[str], prompt: Optional[str],
                         system_prompt: Optional[str]) -> Tuple[str, Optional[str]]:
        p_prompt = p_system = None
        if preset_name:
            preset = self.prompt_presets.get(preset_name)
            if preset is None:
                raise ValueError(f"Unknown prompt preset: {preset_name}")
            # dict("prompt"/"system_prompt") 또는 PromptPreset 객체 둘 다 수용
            p_prompt = preset["prompt"] if isinstance(preset, dict) else getattr(preset, "prompt")
            p_system = (preset.get("system_prompt") if isinstance(preset, dict)
                        else getattr(preset, "system_prompt", None))
        final_prompt = prompt if prompt is not None else p_prompt
        final_system = system_prompt if system_prompt is not None else p_system
        if final_prompt is None:
            raise ValueError("No prompt provided (prompt= 또는 preset_name= 필요)")
        return final_prompt, final_system

    # ---- ffmpeg -------------------------------------------------------------

    def probe_duration(self, video: Path) -> float:
        ff = _find_ffmpeg()
        proc = subprocess.run([ff, "-i", str(video)], capture_output=True, text=True)
        dur = parse_duration_s(proc.stderr or "")
        if dur is None:
            raise RuntimeError(f"duration parse failed for {video}")
        return dur

    def _encode_chunk(self, video: Path, start: float, dur: float) -> str:
        """[start, start+dur) 구간을 슬라이스해 base64로. 인코딩 파라미터는
        build_dataset.slice_clip과 동일(재인코딩 — 프레임 정확한 경계)."""
        ff = _find_ffmpeg()
        with tempfile.TemporaryDirectory(prefix="ttsum_vllm_") as td:
            out = Path(td) / "chunk.mp4"
            cmd = [ff, "-y", "-i", str(video), "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                   "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
                   str(out)]
            proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                  text=True)
            if proc.returncode != 0 or not out.exists():
                tail = (proc.stderr or "").strip().splitlines()[-1:]
                raise RuntimeError(f"ffmpeg slice failed @{start:.1f}s: {tail}")
            return base64.b64encode(out.read_bytes()).decode("ascii")

    # ---- HTTP ---------------------------------------------------------------

    def _post(self, payload: dict) -> dict:
        import requests
        resp = requests.post(f"{self.base_url}/v1/chat/completions",
                             data=json.dumps(payload),
                             headers={"Content-Type": "application/json"},
                             timeout=self.request_timeout)
        resp.raise_for_status()
        return resp.json()

    # ---- 메인 ----------------------------------------------------------------

    def analyze_video(self, video_path: str, model: str,
                      preset_name: Optional[str] = None, prompt: Optional[str] = None,
                      system_prompt: Optional[str] = None,
                      chunk_duration: Optional[float] = None,
                      temperature: float = 0.0, max_tokens: int = 256) -> Dict[str, Any]:
        """비디오 전체를 청크 단위로 추론. 반환 형식은 VSS 응답 호환(chunk_responses)."""
        video = Path(video_path)
        if not video.exists():
            raise FileNotFoundError(str(video))
        final_prompt, final_system = self._resolve_prompts(preset_name, prompt, system_prompt)
        cd = float(chunk_duration or self.default_chunk_duration)
        spans = chunk_spans(self.probe_duration(video), cd)

        t0 = time.time()
        chunk_responses: List[dict] = []
        errors = 0
        for idx, (start, dur) in enumerate(spans):
            try:
                b64 = self._encode_chunk(video, start, dur)
                messages: List[dict] = []
                if final_system:
                    messages.append({"role": "system", "content": final_system})
                messages.append({"role": "user", "content": [
                    {"type": "video_url",
                     "video_url": {"url": f"data:video/mp4;base64,{b64}"}},
                    {"type": "text", "text": final_prompt},
                ]})
                out = self._post({"model": model, "messages": messages,
                                  "temperature": temperature, "max_tokens": max_tokens,
                                  "logprobs": True})
                content = out["choices"][0]["message"]["content"]
                avg_lp = _avg_logprob(out)
            except Exception as exc:
                # 부분 실패는 기록하고 계속 — 조용한 누락 금지, 전체 중단도 과함
                errors += 1
                content = ""
                chunk_responses.append({"chunk_idx": idx, "start_s": start,
                                        "end_s": start + dur, "content": content,
                                        "error": repr(exc)})
                continue
            chunk_responses.append({"chunk_idx": idx, "start_s": start,
                                    "end_s": start + dur, "content": content,
                                    "avg_logprob": avg_lp})

        return {
            "api": "openai_compat",
            "base_url": self.base_url,
            "model": model,
            "video": video.name,
            "chunk_duration": cd,
            "num_chunks": len(spans),
            "num_errors": errors,
            "chunk_responses": chunk_responses,
            "execution_time": time.time() - t0,
        }

    @staticmethod
    def save_json(data: dict, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data, indent=4, ensure_ascii=False),
                              encoding="utf-8")


def _self_test() -> None:
    # duration 파싱 / 청크 스팬
    err = "Input #0 ... \n  Duration: 00:00:30.03, start: 0.000000, bitrate: 1379 kb/s"
    dur = parse_duration_s(err)
    assert dur is not None and abs(dur - 30.03) < 1e-9
    assert parse_duration_s("no duration here") is None
    assert chunk_spans(30.03, 2.0) == [(i * 2.0, 2.0) for i in range(15)]
    assert chunk_spans(1.9, 2.0) == []

    # 프롬프트 해석 (dict 프리셋 / 직접 지정 / 오류)
    c = VLLMClient("http://x", {"twin": {"prompt": "P", "system_prompt": "S"}})
    assert c._resolve_prompts("twin", None, None) == ("P", "S")
    assert c._resolve_prompts(None, "direct", "sys") == ("direct", "sys")
    for bad in (("nope", None), (None, None)):
        try:
            c._resolve_prompts(bad[0], bad[1], None)
            raise AssertionError("should raise")
        except ValueError:
            pass

    # analyze 루프 형태 (ffmpeg/HTTP는 mock)
    class Fake(VLLMClient):
        def probe_duration(self, video):
            return 6.0
        def _encode_chunk(self, video, start, dur):
            if start == 2.0:
                raise RuntimeError("slice boom")  # 부분 실패 경로
            return "QUJD"
        def _post(self, payload):
            assert payload["messages"][0]["role"] == "system"
            assert payload["messages"][1]["content"][1]["text"] == "P"
            return {"choices": [{"message": {"content": '[{"08:54:59": [1, 2]}]'}}]}

    fk = Fake("http://x/", {"twin": {"prompt": "P", "system_prompt": "S"}})
    import tempfile as _tf
    with _tf.NamedTemporaryFile(suffix=".mp4") as f:
        res = fk.analyze_video(f.name, model="m", preset_name="twin")
    assert res["num_chunks"] == 3 and res["num_errors"] == 1
    assert res["chunk_responses"][0]["content"].startswith("[{")
    assert "error" in res["chunk_responses"][1] and res["chunk_responses"][1]["content"] == ""
    assert res["chunk_responses"][2]["chunk_idx"] == 2
    print("vllm_client self-test OK")


if __name__ == "__main__":
    _self_test()
