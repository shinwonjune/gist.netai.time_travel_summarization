# VLM Server 실행 가이드

이 문서는 Time Travel Summarization Extension 동작을 위한 VLM 서버(vLLM) 실행 방법을 설명함.

*   **하드웨어 요구사항**:
    *   GPU: L40 또는 A100 40GB 1대 권장 (Qwen3-VL-8B 모델 기준).

## 1. VLM Container 실행 (ex. Qwen3-VL-8B)
vLLM 기반으로 Qwen3-VL-8B 모델을 실행함. (GPU 서버에서 실행. `run_qwen3-vl-8b.sh`와 동일)

```bash
docker run -d \
  --name qwen3-vl-8b \
  --gpus '"device=2"' \
  --network host \
  --ipc=host \
  --shm-size=16g \
  -v "${TTSUM_HOME:-$HOME/ttsum}/models/Qwen3-VL-8B-Instruct:/models/Qwen3-VL-8B-Instruct:ro" \
  vllm/vllm-openai:latest \
    --model /models/Qwen3-VL-8B-Instruct \
    --served-model-name Qwen3-VL-8B-Instruct \
    --tensor-parallel-size 1 \
    --max-model-len 8192 \
    --max-num-seqs 256 \
    --max-num-batched-tokens 8192 \
    --media-io-kwargs '{"video": {"num_frames": -1}}' \
    --host 0.0.0.0 \
    --port 38011
```
*   `--gpus`: 사용할 GPU 번호 지정 (예: `device=2`).
*   `-v`: 모델 파일 경로 마운트 (`/로컬/모델/경로:/컨테이너/모델/경로:ro`).
*   `--port`: vLLM이 서빙에 사용할 포트 번호 지정 (예: `38011`).

vLLM 서버가 뜨면 확장의 `vlm_client`가 `VLM_BASE_URL`(기본 `http://localhost:38011`)로
OpenAI 호환 `/v1/chat/completions`를 직접 호출한다 — 클라이언트가 2초 청크로 직접
슬라이스해 보내므로 별도 비디오 처리 파이프라인은 필요 없다.

## 2. 데이터 생성, 학습, 서빙 잡 (L40)
에피소드 생성, 데이터셋 빌드, LoRA 학습, vLLM 서빙 기동/중지는 job API(`l40/run_api.sh` →
`job_api.py`)가 잡 타입별 러너에 위임해 오케스트레이션한다. 셋업 절차와 전제 조건은
`l40/SETUP.md` 참고.
