"""
VLM 서버에 동영상을 업로드하고, VLM 분석을 요청하고, 결과를 저장하는 `VLM Client module` 의 core.
default_chunk_duration=2 초, default_chunk_overlap_duration=0 초로 설정됨.

VLM 서버 통신: _initialize_client 메서드에서 직접 IP 주소와 포트를 지정하여 통신.
"""

import os
import json
from pathlib import Path
from typing import Optional, Dict, Any
import carb
from datetime import datetime

from ..app.paths import ExtensionPaths


class VLMClientCore:
    """Core logic for VLM Client."""
    
    def __init__(self):
        """Initialize VLM Client Core."""
        self._client = None
        self._current_video_id = None
        self._last_upload_response = None
        self._last_generation_response = None

        self._paths = ExtensionPaths(Path(__file__).resolve().parent.parent)
        self._videos_base_path = self._paths.videos_dir
        self._outputs_base_path = self._paths.vlm_outputs_dir
        
        # Initialize client
        self._initialize_client()
    
    def _initialize_client(self):
        """Initialize VSS Client with presets."""
        try:
            from ..utils.VSS_client import VSSClient, PromptPreset
            from .prompts import PROMPTS

            # VLM 서버 base URL은 VIA_BACKEND 환경변수로 지정한다.
            # 미설정 시 localhost로 fallback하며 명시적 경고를 남긴다.
            base_url = os.environ.get("VIA_BACKEND")
            if not base_url:
                base_url = "http://localhost:8100"
                carb.log_warn(
                    "[VLMClient] VIA_BACKEND not set; falling back to "
                    f"{base_url}. Set VIA_BACKEND to your VSS server URL."
                )

            # Prompt presets are defined in vlm_client/prompts.py so the exact
            # same strings can be reused by the offline training-data builder.
            presets = {
                name: PromptPreset(**spec) for name, spec in PROMPTS.items()
            }

            self._client = VSSClient(
                base_url=base_url,
                default_chunk_duration=2,
                default_chunk_overlap_duration=0,
                prompt_presets=presets,
            )
            
            carb.log_info(f"[VLMClient] Initialized with base_url: {base_url}")
            
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
            
            # Generate captions
            response = self._client.generate_vlm_captions(
                video_id=self._current_video_id,
                model=model,
                preset_name=preset_name,
                chunk_overlap_duration=chunk_overlap_duration
            )
            
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
