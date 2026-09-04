import json
import tempfile
from io import BytesIO
from pathlib import Path

from gist.netai.time_travel_summarization.tests.conftest import install_carb_stub

install_carb_stub()

from gist.netai.time_travel_summarization.vlm_client.core import VLMClientCore  # noqa: E402


class FakeVLLMClient:
    """openai/vLLM 직결 경로용 더미 클라이언트 — 업로드 개념이 없어 analyze_video만 구현."""

    def analyze_video(self, video_path, model, preset_name):
        return {
            "execution_time": 1.25,
            "chunk_responses": [
                {"content": '[{"00:00:01": [1, 2]}]'},
            ],
        }

    def save_json(self, data, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")


def test_upload_video_file_uri_stages_temp_file_and_cleans_up_on_delete():
    fake_bytes = b"\x00\x00\x00\x18ftypmp42fake-video"
    source_path = None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as source:
            source.write(fake_bytes)
            source_path = Path(source.name)

        core = VLMClientCore()
        core._api = "openai"  # direct/vLLM 경로 검증
        core._client = FakeVLLMClient()

        assert core.upload_video(source_path.as_uri()) is True
        assert core._current_video_id == source_path.name
        staged_path = core._current_video_path
        assert staged_path is not None
        assert Path(staged_path).exists()
        assert Path(staged_path).read_bytes() == fake_bytes

        # 실제 서버 업로드가 없는 direct 모드는 delete_video 시점에 스테이징
        # 임시 파일을 정리한다 (upload 직후가 아니라 — 분석에 이 파일을 쓴다).
        assert core.delete_video() is True
        assert not Path(staged_path).exists()
    finally:
        if source_path and source_path.exists():
            source_path.unlink()


def test_upload_video_s3_uri_uses_storage_adapter_temp_bridge(monkeypatch):
    fake_bytes = b"\x00\x00\x00\x18ftypmp42fake-minio-video"
    source_uri = "s3://time-travel-summarization/timetravel/video/capture.mp4"

    class FakeStorageAdapter:
        def __init__(self):
            self.exists_calls = []
            self.open_read_calls = []

        def exists(self, uri):
            self.exists_calls.append(uri)
            return uri == source_uri

        def open_read(self, uri):
            self.open_read_calls.append(uri)
            return BytesIO(fake_bytes)

    adapter = FakeStorageAdapter()

    import gist.netai.time_travel_summarization.storage as storage_module

    monkeypatch.setattr(storage_module, "from_uri", lambda uri: adapter)

    core = VLMClientCore()
    core._api = "openai"  # direct/vLLM 경로 검증
    core._client = FakeVLLMClient()

    assert core.upload_video(source_uri) is True
    assert core._current_video_id == "capture.mp4"
    assert adapter.exists_calls == [source_uri]
    assert adapter.open_read_calls == [source_uri]
    staged_path = core._current_video_path
    assert staged_path is not None
    assert Path(staged_path).suffix == ".mp4"
    assert Path(staged_path).read_bytes() == fake_bytes

    assert core.delete_video() is True
    assert not Path(staged_path).exists()


def test_upload_video_missing_file_uri_returns_false():
    with tempfile.TemporaryDirectory() as tmpdir:
        missing_uri = (Path(tmpdir) / "missing.mp4").as_uri()
        core = VLMClientCore()
        core._api = "openai"
        core._client = FakeVLLMClient()

        assert core.upload_video(missing_uri) is False


def test_upload_video_missing_local_filename_returns_false():
    core = VLMClientCore()
    core._api = "openai"
    core._client = FakeVLLMClient()

    assert core.upload_video("missing-local-video.mp4") is False


def test_generate_captions_saves_raw_result_only_to_output_root_uri(tmp_path):
    core = VLMClientCore.__new__(VLMClientCore)
    core._api = "openai"  # direct 경로 검증
    core._client = FakeVLLMClient()
    core._current_video_id = "vid-123"
    core._current_video_path = tmp_path / "capture.mp4"
    core._current_video_path.write_bytes(b"fake-video-bytes")
    core._current_video_source = None
    core._last_generation_response = None
    core._outputs_base_path = tmp_path / "local_vlm_outputs"
    remote_root = tmp_path / "lake_root"

    success, output_filename = core.generate_captions(
        model="model",
        preset_name="simple_view",
        video_filename="capture.mp4",
        output_root_uri=remote_root.as_uri(),
    )

    assert success is True
    assert output_filename is not None
    # lake 경로의 반환값은 전체 URI — Event Post Processing 자동 전달이 bare
    # 파일명으론 lake 산출물을 못 찾아서 바꾼 계약(2026-07-19).
    assert output_filename.startswith(remote_root.as_uri())
    basename = output_filename.rsplit("/", 1)[-1]
    local_output = core._outputs_base_path / basename
    lake_output = remote_root / "vlm_outputs" / basename
    assert not local_output.exists()
    assert lake_output.exists()
    assert json.loads(lake_output.read_text(encoding="utf-8"))["chunk_responses"]
    # 이벤트 인덱스도 함께 적재된다 (추론 1회 = 인덱스 오브젝트 1개).
    # 이 테스트는 사이드카가 없으므로 앵커 미상 → time=None, time_hms만 보존.
    index_file = remote_root / "vlm_events" / "capture.jsonl"
    assert index_file.exists()
    record = json.loads(index_file.read_text(encoding="utf-8").splitlines()[0])
    assert record["time"] is None
    assert record["time_hms"] == "00:00:01" and record["ids"] == [1, 2]


def _run_test(name, func):
    func()
    print(f"PASS {name}")


if __name__ == "__main__":
    _run_test(
        "test_upload_video_file_uri_stages_temp_file_and_cleans_up_on_delete",
        test_upload_video_file_uri_stages_temp_file_and_cleans_up_on_delete,
    )
    _run_test(
        "test_upload_video_missing_file_uri_returns_false",
        test_upload_video_missing_file_uri_returns_false,
    )
    _run_test(
        "test_upload_video_missing_local_filename_returns_false",
        test_upload_video_missing_local_filename_returns_false,
    )
    print("ALL PASS")
