"""
VLM 서버에 동영상을 업로드하고, VLM 분석을 요청하고, 결과를 저장하는 `VLM Client module` 의 core.
default_chunk_duration=2 초, default_chunk_overlap_duration=0 초로 설정됨.

VLM 서버 통신: _initialize_client 메서드에서 직접 IP 주소와 포트를 지정하여 통신.
"""

import os
import json
from pathlib import Path
from typing import Optional
import carb
from datetime import datetime

from ..app.paths import ExtensionPaths


class VLMClientCore:
    """Core logic for VLM Client."""
    
    def __init__(self):
        """Initialize VLM Client Core."""
        self._client = None
        self._api = "openai"                 # "openai"(vLLM 직접) | "vss"(레거시)
        self._current_video_id = None
        self._current_video_path = None      # openai 모드: 업로드 대신 로컬 경로 보관
        self._current_video_source = None    # 원본 URI/경로 (사이드카 앵커 조회용)
        self._staged_tmp_path = None         # URI 입력 시 내려받은 임시 파일 (정리용)
        self._last_upload_response = None
        self._last_generation_response = None

        self._paths = ExtensionPaths(Path(__file__).resolve().parent.parent)
        self._videos_base_path = self._paths.videos_dir
        self._outputs_base_path = self._paths.vlm_outputs_dir
        
        # Initialize client
        self._initialize_client()
    
    def _initialize_client(self):
        """VLM 백엔드 초기화.

        기본은 vLLM OpenAI 호환 직접 호출(VLM_API=openai) — 클라이언트가 청크를
        직접 슬라이스해 /v1/chat/completions로 보낸다(VSS 스택 불필요, 평가 때
        쓴 경로와 동일 → train==infer 정합). VLM_API=vss면 레거시 VSS(VIA) 경유.
        """
        try:
            from .prompts import PROMPTS

            self._api = os.environ.get("VLM_API", "openai").strip().lower()
            if self._api == "vss":
                from ..utils.VSS_client import VSSClient, PromptPreset

                base_url = os.environ.get("VIA_BACKEND")
                if not base_url:
                    base_url = "http://localhost:8100"
                    carb.log_warn(
                        "[VLMClient] VIA_BACKEND not set; falling back to "
                        f"{base_url}. Set VIA_BACKEND to your VSS server URL."
                    )
                presets = {name: PromptPreset(**spec) for name, spec in PROMPTS.items()}
                self._client = VSSClient(
                    base_url=base_url,
                    default_chunk_duration=2,
                    default_chunk_overlap_duration=0,
                    prompt_presets=presets,
                )
            else:
                from ..utils.vllm_client import VLLMClient

                # vLLM 기동 스크립트(VLM_server/run_qwen3-vl-8b.sh)의 포트가 기본값.
                base_url = os.environ.get("VLM_BASE_URL")
                if not base_url:
                    base_url = "http://localhost:38011"
                    carb.log_warn(
                        "[VLMClient] VLM_BASE_URL not set; falling back to "
                        f"{base_url}. Set VLM_BASE_URL to your vLLM server URL."
                    )
                # 프리셋 원본(dict)을 그대로 전달 — 학습 빌더와 동일 문자열 보장.
                self._client = VLLMClient(
                    base_url=base_url,
                    prompt_presets=PROMPTS,
                    default_chunk_duration=2.0,
                )

            carb.log_info(f"[VLMClient] Initialized api={self._api} base_url={base_url}")

        except Exception as e:
            carb.log_error(f"[VLMClient] Failed to initialize client: {e}")
            import traceback
            carb.log_error(traceback.format_exc())
            self._client = None
    
    def upload_video(self, video_source: str) -> bool:
        """
        Upload video to VSS server.
        
        Args:
            video_source: Video filename (relative to videos/ directory) or storage URI
            
        Returns:
            True if successful, False otherwise
        """
        if not self._client:
            carb.log_error("[VLMClient] Client not initialized")
            return False

        if self._api != "vss":
            # 직접(vLLM) 모드: 서버 업로드 개념이 없다 — 요청마다 청크를 실어 보내므로
            # 여기서는 로컬 경로 확보(URI면 임시 파일로 스테이징)만 한다.
            return self._stage_video_direct(video_source)

        if "://" in video_source:
            tmp_path = None
            try:
                import tempfile

                from ..storage import from_uri

                adapter = from_uri(video_source)
                if not adapter.exists(video_source):
                    carb.log_error(f"[VLMClient] Video URI not found: {video_source}")
                    return False

                with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp_file:
                    tmp_path = Path(tmp_file.name)

                with adapter.open_read(video_source) as stream:
                    tmp_path.write_bytes(stream.read())

                carb.log_info(f"[VLMClient] Uploading video URI via temp file: {video_source}")

                response = self._client.upload_video(str(tmp_path))
                self._last_upload_response = response
                self._current_video_id = response.get("id")

                carb.log_info(f"[VLMClient] Uploaded video ID: {self._current_video_id}")
                return True

            except Exception as e:
                carb.log_error(f"[VLMClient] Upload failed: {e}")
                import traceback
                carb.log_error(traceback.format_exc())
                return False
            finally:
                if tmp_path and tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except Exception as cleanup_error:
                        carb.log_warn(f"[VLMClient] Failed to delete temp upload file: {cleanup_error}")

        try:
            # Construct full path
            video_path = self._videos_base_path / video_source
            
            if not video_path.exists():
                carb.log_error(f"[VLMClient] Video file not found: {video_path}")
                return False
            
            carb.log_info(f"[VLMClient] Uploading video: {video_path}")
            
            # Upload video
            response = self._client.upload_video(str(video_path))
            
            # Store response and video ID
            self._last_upload_response = response
            self._current_video_id = response.get("id")
            
            carb.log_info(f"[VLMClient] Uploaded video ID: {self._current_video_id}")
            return True
            
        except Exception as e:
            carb.log_error(f"[VLMClient] Upload failed: {e}")
            import traceback
            carb.log_error(traceback.format_exc())
            return False
    
    def _cleanup_staged(self):
        if self._staged_tmp_path:
            try:
                Path(self._staged_tmp_path).unlink(missing_ok=True)
            except Exception as e:
                carb.log_warn(f"[VLMClient] staged temp cleanup failed: {e}")
            self._staged_tmp_path = None

    def _stage_video_direct(self, video_source: str) -> bool:
        """직접 모드의 '업로드': 분석할 비디오의 로컬 경로를 확보해 보관."""
        try:
            self._cleanup_staged()
            # 원본 위치 보존 — 사이드카(.meta.json)는 스테이징 사본이 아니라
            # 원본 옆에 있으므로, 이벤트 인덱스의 앵커 조회는 이 값을 쓴다.
            self._current_video_source = video_source
            if "://" in video_source:
                import tempfile

                from ..storage import from_uri

                adapter = from_uri(video_source)
                if not adapter.exists(video_source):
                    carb.log_error(f"[VLMClient] Video URI not found: {video_source}")
                    return False
                with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
                    tmp_path = Path(tmp.name)
                with adapter.open_read(video_source) as stream:
                    tmp_path.write_bytes(stream.read())
                self._staged_tmp_path = tmp_path
                self._current_video_path = tmp_path
                self._current_video_id = Path(video_source).name
            else:
                video_path = self._videos_base_path / video_source
                if not video_path.exists():
                    carb.log_error(f"[VLMClient] Video file not found: {video_path}")
                    return False
                self._current_video_path = video_path
                self._current_video_id = video_path.name
            self._last_upload_response = {"id": self._current_video_id, "mode": "direct"}
            carb.log_info(f"[VLMClient] Selected video (direct): {self._current_video_path}")
            return True
        except Exception as e:
            carb.log_error(f"[VLMClient] Video staging failed: {e}")
            import traceback
            carb.log_error(traceback.format_exc())
            return False

    def delete_video(self) -> bool:
        """
        Delete currently uploaded video.
        
        Returns:
            True if successful, False otherwise
        """
        if not self._client:
            carb.log_error("[VLMClient] Client not initialized")
            return False
        
        if not self._current_video_id:
            carb.log_error("[VLMClient] No video ID to delete")
            return False

        if self._api != "vss":
            # 직접 모드: 서버에 지울 것이 없다 — 로컬 선택 상태만 해제.
            self._cleanup_staged()
            self._current_video_id = None
            self._current_video_path = None
            self._last_upload_response = None
            carb.log_info("[VLMClient] Cleared selected video (direct mode)")
            return True

        try:
            carb.log_info(f"[VLMClient] Deleting video ID: {self._current_video_id}")
            
            response = self._client.delete_video(self._current_video_id)
            
            carb.log_info(f"[VLMClient] Video deleted: {response}")
            
            # Clear current video ID
            self._current_video_id = None
            self._last_upload_response = None
            
            return True
            
        except Exception as e:
            carb.log_error(f"[VLMClient] Delete failed: {e}")
            import traceback
            carb.log_error(traceback.format_exc())
            return False
    
    def generate_captions(
        self,
        model: str = "Qwen3-VL-8B-Instruct",
        preset_name: str = "simple_view",
        video_filename: Optional[str] = None,
        chunk_overlap_duration: int = 0,
        output_root_uri: Optional[str] = None,
    ) -> tuple[bool, Optional[str]]:
        """
        Generate VLM captions for current video.
        
        Args:
            model: VLM model name (default: "Qwen3-VL-8B-Instruct")
            preset_name: Prompt preset name (default: "simple_view")
            video_filename: Optional video filename for output naming
            chunk_overlap_duration: Chunk overlap duration in seconds (default: 0)
            output_root_uri: Optional artifact root for Data Lake mode. When provided,
                the raw VLM JSON is also written under ``<root>/vlm_outputs/``.
            
        Returns:
            Tuple of (success: bool, output_filename: Optional[str])
        """
        if not self._client:
            carb.log_error("[VLMClient] Client not initialized")
            return False, None
        
        if not self._current_video_id:
            carb.log_error("[VLMClient] No video uploaded")
            return False, None
        
        try:
            carb.log_info(f"[VLMClient] Generating captions for video ID: {self._current_video_id}")
            carb.log_info(f"[VLMClient] Model: {model}, Preset: {preset_name}")
            carb.log_info(f"[VLMClient] Chunk overlap duration: {chunk_overlap_duration}s")
            
            # Generate captions — 백엔드 분기 (응답은 둘 다 chunk_responses 형식)
            if self._api != "vss":
                if not self._current_video_path:
                    carb.log_error("[VLMClient] No video selected (direct mode)")
                    return False, None
                if chunk_overlap_duration:
                    carb.log_warn("[VLMClient] direct 모드는 chunk overlap 미지원 — 0으로 진행")
                response = self._client.analyze_video(
                    str(self._current_video_path),
                    model=model,
                    preset_name=preset_name,
                )
            else:
                response = self._client.generate_vlm_captions(
                    video_id=self._current_video_id,
                    model=model,
                    preset_name=preset_name,
                    chunk_overlap_duration=chunk_overlap_duration
                )
            
            # 원본 영상 URI 기록 — 결과 JSON의 video는 스테이징 임시명이라 사이드카를
            # 역추적할 수 없다. Process Events가 base_date(사이드카 capture_start)를
            # 복원하려면 원본 참조가 필요.
            response["video_source"] = str(getattr(self, "_current_video_source", "") or "")

            # Store response
            self._last_generation_response = response
            
            # Save to outputs directory
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            
            if video_filename:
                # Use video filename without extension
                video_stem = Path(video_filename).stem
                output_filename = f"{model}_{video_stem}_{timestamp}.json"
            else:
                output_filename = f"{model}_output_{timestamp}.json"
            
            if output_root_uri:
                output_uri = f"{output_root_uri.rstrip('/')}/vlm_outputs/{output_filename}"
                from ..storage import from_uri

                payload = json.dumps(response, indent=4, ensure_ascii=False).encode("utf-8")
                from_uri(output_uri).put_bytes(
                    output_uri,
                    payload,
                    content_type="application/json",
                )
                carb.log_info(f"[VLMClient] Results saved to: {output_uri}")
                # 이벤트 인덱스 적재(best-effort) — 추론 결과를 시간축 검색
                # 표면으로 축적. 실패해도 추론 경로는 성공으로 유지.
                try:
                    from ..events.event_index import (
                        append_index, parse_events_from_vlm_result, sidecar_anchor,
                    )

                    events = parse_events_from_vlm_result(response)
                    # 원본 영상 옆 사이드카의 capture_start로 절대 시각 복원.
                    # 사이드카 미상이면 anchor=None → time_hms만 기록.
                    anchor = (
                        sidecar_anchor(self._current_video_source)
                        if getattr(self, "_current_video_source", None) else None
                    )
                    idx_uri = append_index(
                        output_root_uri, video_filename or output_filename,
                        events, model=model, anchor=anchor,
                    )
                    carb.log_info(
                        f"[VLMClient] event index: {len(events)} events "
                        f"(anchor={'ok' if anchor else 'none'}) -> {idx_uri}")
                except Exception as exc:
                    carb.log_warn(f"[VLMClient] event index write failed: {exc!r}")
                # 반환은 전체 URI — Event Post Processing이 파일명만으론 lake 산출물을
                # 못 찾는다(_resolve_json_input은 s3://는 그대로, bare명은 로컬 탐색).
                output_filename = output_uri
            else:
                output_path = self._outputs_base_path / output_filename
                self._client.save_json(response, str(output_path))
                carb.log_info(f"[VLMClient] Results saved to: {output_path}")

            # Log execution time
            exec_time = response.get("execution_time", 0)
            carb.log_info(f"[VLMClient] Execution time: {exec_time:.2f} seconds")
            
            return True, output_filename
            
        except Exception as e:
            carb.log_error(f"[VLMClient] Generation failed: {e}")
            carb.log_error(f"[VLMClient] Video ID: {self._current_video_id}")
            carb.log_error(f"[VLMClient] Model: {model}, Preset: {preset_name}")
            import traceback
            carb.log_error(traceback.format_exc())
            return False, None
    
    def get_current_video_id(self) -> Optional[str]:
        """Get current video ID."""
        return self._current_video_id
    
    def has_video_uploaded(self) -> bool:
        """Check if video is uploaded."""
        return self._current_video_id is not None
    
    def get_videos_path(self) -> str:
        """Get videos directory path."""
        return str(self._videos_base_path)
    
    def get_outputs_path(self) -> str:
        """Get outputs directory path."""
        return str(self._outputs_base_path)
