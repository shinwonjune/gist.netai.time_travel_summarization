import json
import sys
import tempfile
import types
from pathlib import Path


def _install_carb_stub():
    carb = types.ModuleType("carb")
    carb.log_info = lambda *args, **kwargs: None
    carb.log_warn = lambda *args, **kwargs: None
    carb.log_error = lambda *args, **kwargs: None
    sys.modules["carb"] = carb


_install_carb_stub()

from gist.netai.time_travel_summarization.vlm_client.core import VLMClientCore  # noqa: E402


class FakeVSSClient:
    def __init__(self):
        self.uploaded_path = None
        self.observed_bytes = None

    def upload_video(self, path):
        upload_path = Path(path)
        self.uploaded_path = upload_path
        assert upload_path.exists()
        self.observed_bytes = upload_path.read_bytes()
        return {"id": "vid-123"}


class FakeGenerationClient:
    def __init__(self):
        self.saved_path = None

    def generate_vlm_captions(self, **kwargs):
        return {
            "execution_time": 1.25,
            "chunk_responses": [
                {"content": '[{"00:00:01": [1, 2]}]'},
            ],
        }

    def save_json(self, data, path):
        self.saved_path = Path(path)
        self.saved_path.parent.mkdir(parents=True, exist_ok=True)
        self.saved_path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")


def test_upload_video_file_uri_uses_temp_file_and_cleans_it_up():
    fake_bytes = b"\x00\x00\x00\x18ftypmp42fake-video"
    fake_client = FakeVSSClient()
    source_path = None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as source:
            source.write(fake_bytes)
            source_path = Path(source.name)

        core = VLMClientCore()
        core._client = fake_client

        assert core.upload_video(source_path.as_uri()) is True
        assert core._current_video_id == "vid-123"
        assert fake_client.observed_bytes == fake_bytes
        assert fake_client.uploaded_path is not None
        assert not fake_client.uploaded_path.exists()
    finally:
        if source_path and source_path.exists():
            source_path.unlink()


def test_upload_video_missing_file_uri_returns_false():
    with tempfile.TemporaryDirectory() as tmpdir:
        missing_uri = (Path(tmpdir) / "missing.mp4").as_uri()
        core = VLMClientCore()
        core._client = FakeVSSClient()

        assert core.upload_video(missing_uri) is False


def test_upload_video_missing_local_filename_returns_false():
    core = VLMClientCore()
    core._client = FakeVSSClient()

    assert core.upload_video("missing-local-video.mp4") is False


def test_generate_captions_saves_raw_result_only_to_output_root_uri(tmp_path):
    core = VLMClientCore.__new__(VLMClientCore)
    core._client = FakeGenerationClient()
    core._current_video_id = "vid-123"
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
    local_output = core._outputs_base_path / output_filename
    lake_output = remote_root / "vlm_outputs" / output_filename
    assert not local_output.exists()
    assert lake_output.exists()
    assert json.loads(lake_output.read_text(encoding="utf-8"))["chunk_responses"]


def _run_test(name, func):
    func()
    print(f"PASS {name}")


if __name__ == "__main__":
    _run_test(
        "test_upload_video_file_uri_uses_temp_file_and_cleans_it_up",
        test_upload_video_file_uri_uses_temp_file_and_cleans_it_up,
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
