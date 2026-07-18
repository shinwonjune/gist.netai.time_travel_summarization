"""원격 잡 패널 — 플랫폼 제어면의 GUI 클라이언트 (생성·학습·서빙).

automation.remote_generation(잡 스펙 + 전송 어댑터) 위의 얇은 UI 층:
파라미터 폼 → JobSpec 조립 → 제출(SSH/local/REST, 백그라운드 스레드) → 상태 폴링.
SSH는 블로킹이므로 UI 프리즈 방지를 위해 스레드에서 돌리고, 결과 반영은
UiTaskDispatcher(메인 루프)로 되돌린다.

추론(대화형)은 이 패널을 거치지 않는다 — vLLM이 자체 배칭하므로 vlm_client가
서빙 엔드포인트를 직접 호출. 여기의 Serving 섹션은 그 서빙의 기동/중지만 담당.
"""
from __future__ import annotations

import datetime
import threading
import time

from .remote_generation import (
    JobSpec, build_serve_check_command, read_status, submit_job, transport_from_host,
)

_DEFAULT_HOST = "netai@sv4000-2"
_DEFAULT_EXT_ROOT = "/home/netai/wonjune/kit-app-template/source/extensions/gist.netai.time_travel_summarization"
_DEFAULT_STAGE = ("omniverse://10.38.38.32/Projects/Dream-AI_Plus_Twin/"
                  "Workspace_Personal/swj/AI-Grad_Building/A_AI-Grad_Building.usd")
_UPLOAD_PREFIX = "s3://time-travel-summarization/episodes"
# L40 apps 디렉토리에 .kit이 여러 개라 러너 자동 발견이 불가 → 명시 지정 필수.
# prod-20260709가 실제 사용한 앱(job.log "My USD Composer" 확인).
_DEFAULT_APP_KIT = "my_company.my_usd_composer"
_DEFAULT_DATASET = "/home/netai/wonjune/ttsum-data/bev-collision-v2"
_DEFAULT_MERGED_MODEL = "/home/netai/wonjune/ttsum-data/lora_qwen3vl_v3/v0-20260710-051758/checkpoint-133-merged"
# GPU 역할 분리(서버 SERVE_GPU와 일치시킬 것): 0=서빙 전용, 1=잡(생성/학습).
# REST 경로는 job_api가 강제하지만, SSH 직결 경로는 이 값이 그대로 쓰인다.
_SERVE_GPU = 0


class RemoteGenWindow:
    """독립 창 — 데이터 생성은 재현 제어(Time Travel Control)와 관심사가 달라 분리.

    extension.py가 다른 창들과 같은 패턴으로 생성/파괴한다.
    """

    def __init__(self):
        import omni.ui as ui

        from ..ui.task_dispatcher import UiTaskDispatcher

        self._dispatcher = UiTaskDispatcher("RemoteGenWindowUiDispatcher")
        self._window = ui.Window("Remote Jobs", width=520, height=600)
        with self._window.frame:
            with ui.VStack(spacing=5):
                self._panel = RemoteGenPanel(self._dispatcher)

    def destroy(self):
        if self._dispatcher:
            self._dispatcher.shutdown()
            self._dispatcher = None
        if self._window:
            self._window.destroy()
            self._window = None


class RemoteGenPanel:
    """호출부의 ui.VStack 안에서 생성할 것. dispatcher는 창의 것을 공유."""

    def __init__(self, dispatcher):
        import omni.ui as ui

        self._dispatcher = dispatcher
        self._last_job_id = None
        self._last_status_label = None  # 잡 타입별 상태 표시 위치 (serve는 Serving 섹션)

        ui.Label("Remote Data Generation", style={"font_size": 14, "font_weight": "bold"})

        with ui.HStack(height=25, spacing=8):
            ui.Label("Host:", width=85)
            self._host = ui.StringField()
            self._host.model.set_value(_DEFAULT_HOST)
        with ui.HStack(height=25, spacing=8):
            ui.Label("Ext root:", width=85)
            self._ext_root = ui.StringField()
            self._ext_root.model.set_value(_DEFAULT_EXT_ROOT)
        with ui.HStack(height=25, spacing=8):
            ui.Label("Stage:", width=85)
            self._stage = ui.StringField()
            self._stage.model.set_value(_DEFAULT_STAGE)
        with ui.HStack(height=25, spacing=8):
            ui.Label("App kit:", width=85)
            self._app_kit = ui.StringField()
            self._app_kit.model.set_value(_DEFAULT_APP_KIT)
        with ui.HStack(height=25, spacing=8):
            ui.Label("Camera:", width=85)
            self._camera = ui.StringField(width=140)
            self._camera.model.set_value("Capture_camera")
            ui.Label("GPU:", width=35)
            self._gpu = ui.IntField(width=30)
            self._gpu.model.set_value(1)
            ui.Label("Episodes:", width=60)
            self._episodes = ui.IntField(width=40)
            self._episodes.model.set_value(5)
            ui.Label("Dur(s):", width=45)
            self._duration = ui.FloatField(width=45)
            self._duration.model.set_value(30.0)
        with ui.HStack(height=25, spacing=8):
            ui.Label("Objects:", width=85)
            self._min_objects = ui.IntField(width=30)
            self._min_objects.model.set_value(4)
            ui.Label("~", width=10)
            self._max_objects = ui.IntField(width=30)
            self._max_objects.model.set_value(4)
            ui.Label("Seed:", width=40)
            self._seed = ui.IntField(width=75)
            # run마다 달라야 중복 데이터가 안 생김(시드가 전 조건을 유도) → 초 단위
            # 유닉스 시각 기본값 + 제출 시마다 재발급(같은 날 다중 생성 대응)
            self._seed.model.set_value(self._fresh_seed())
            ui.Spacer(width=15)
            self._upload_checkbox = ui.CheckBox(width=20)
            self._upload_checkbox.model.set_value(True)
            ui.Label("upload to minIO", width=0)
        with ui.HStack(height=28, spacing=10):
            submit = ui.Button("Submit Job", width=110)
            submit.set_clicked_fn(self._on_submit_clicked)
            check = ui.Button("Check Status", width=110)
            check.set_clicked_fn(self._on_status_clicked)
            self._status_label = ui.Label("", style={"color": 0xFF888888})

        # ---- Training (LoRA) — train 잡 제출 --------------------------------- #
        ui.Spacer(height=8)
        ui.Label("Training (LoRA)", style={"font_size": 14, "font_weight": "bold"})
        with ui.HStack(height=25, spacing=8):
            ui.Label("Dataset:", width=85)
            self._train_dataset = ui.StringField()
            self._train_dataset.model.set_value(_DEFAULT_DATASET)
        with ui.HStack(height=25, spacing=8):
            ui.Label("Output:", width=85)
            self._train_output = ui.StringField()
            ui.Label("GPU:", width=35)
            self._train_gpu = ui.IntField(width=30)
            self._train_gpu.model.set_value(1)
        with ui.HStack(height=28, spacing=10):
            train_btn = ui.Button("Submit Train Job", width=140)
            train_btn.set_clicked_fn(self._on_train_clicked)
            ui.Label("(Hyperparameters fixed in training/qwen3vl_lora_swift.sh)",
                     style={"color": 0xFF888888})

        # ---- Serving (vLLM) — 전용 GPU에서 기동/중지 -------------------------- #
        ui.Spacer(height=8)
        ui.Label("Serving (vLLM)", style={"font_size": 14, "font_weight": "bold"})
        with ui.HStack(height=25, spacing=8):
            ui.Label("Model path:", width=85)
            self._serve_model = ui.StringField()
            self._serve_model.model.set_value(_DEFAULT_MERGED_MODEL)
        with ui.HStack(height=25, spacing=8):
            ui.Label("Port:", width=85)
            self._serve_port = ui.IntField(width=60)
            self._serve_port.model.set_value(38011)
            ui.Label(f"(GPU {_SERVE_GPU} for Serving)",
                     style={"color": 0xFF888888})
        with ui.HStack(height=28, spacing=10):
            start_btn = ui.Button("Start Serving", width=110)
            start_btn.set_clicked_fn(self._on_serve_start_clicked)
            stop_btn = ui.Button("Stop Serving", width=110)
            stop_btn.set_clicked_fn(self._on_serve_stop_clicked)
            check_btn = ui.Button("Check Serving", width=110)
            check_btn.set_clicked_fn(self._on_serve_check_clicked)
        with ui.HStack(height=22, spacing=8):
            # serve 전용 상태줄 — 상단 공용 라벨(생성/학습 잡)과 분리
            self._serve_status_label = ui.Label("", style={"color": 0xFF888888})

    # ---- helpers ----------------------------------------------------------- #

    @staticmethod
    def _fresh_seed() -> int:
        return int(time.time()) % 2_000_000_000  # IntField(int32) 안전 범위

    def _set_status(self, text: str, label=None):
        # 스레드에서 불려도 안전하게 디스패처 경유
        tgt = label or self._status_label
        self._dispatcher.submit(lambda: setattr(tgt, "text", text))

    def _transport(self):
        # Host 판별: "user@host"=SSH / "http://localhost:8800"=REST(잡 API, SSH 터널
        # 선행: ssh -L 8800:localhost:8800 <host>) / ""·"local"=이 머신
        return transport_from_host(self._host.model.get_value_as_string())

    def _build_spec(self) -> JobSpec:
        job_id = "gen-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        upload = f"{_UPLOAD_PREFIX}/{job_id}" if self._upload_checkbox.model.get_value_as_bool() else ""
        return JobSpec(
            job_id=job_id,
            episodes=self._episodes.model.get_value_as_int(),
            duration=self._duration.model.get_value_as_float(),
            gpu=self._gpu.model.get_value_as_int(),
            min_objects=self._min_objects.model.get_value_as_int(),
            max_objects=self._max_objects.model.get_value_as_int(),
            camera=self._camera.model.get_value_as_string().strip(),
            stage=self._stage.model.get_value_as_string().strip(),
            app_kit=self._app_kit.model.get_value_as_string().strip(),
            upload_uri=upload,
            seed=self._seed.model.get_value_as_int(),
        )

    # ---- callbacks ---------------------------------------------------------- #

    def _submit_spec(self, spec: JobSpec, status_label=None):
        """공통 제출 경로 — 모든 잡 타입이 같은 transport·status 규약을 쓴다."""
        transport = self._transport()
        ext_root = self._ext_root.model.get_value_as_string().strip()
        label = status_label or self._status_label
        self._last_job_id = spec.job_id
        self._last_status_label = label  # Check Status도 같은 자리에 표시
        label.text = f"submitting {spec.job_id} via {transport.name}..."

        def work():
            try:
                ok, out = submit_job(spec, transport, ext_root)
                msg = (f"{spec.job_id} submitted"
                       if ok else f"submit FAILED: {out[-120:]}")
            except Exception as exc:
                msg = f"submit error: {exc!r}"
            self._set_status(msg, label)

        threading.Thread(target=work, daemon=True, name="RemoteGenSubmit").start()

    def _on_submit_clicked(self):
        try:
            spec = self._build_spec()
        except Exception as exc:
            self._status_label.text = f"spec error: {exc}"
            return
        self._seed.model.set_value(self._fresh_seed())  # 다음 제출용 시드 재발급
        self._submit_spec(spec)

    def _on_train_clicked(self):
        dataset = self._train_dataset.model.get_value_as_string().strip()
        if not dataset:
            self._status_label.text = "train: Dataset path required"
            return
        job_id = "train-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self._submit_spec(JobSpec(
            job_id=job_id, job_type="train", dataset=dataset,
            train_output=self._train_output.model.get_value_as_string().strip(),
            gpu=self._train_gpu.model.get_value_as_int(),
        ))

    def _on_serve_start_clicked(self):
        model_path = self._serve_model.model.get_value_as_string().strip()
        if not model_path:
            self._status_label.text = "serve: Model path required"
            return
        job_id = "serve-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self._submit_spec(JobSpec(
            job_id=job_id, job_type="serve_start", model_path=model_path,
            port=self._serve_port.model.get_value_as_int(), gpu=_SERVE_GPU,
        ), status_label=self._serve_status_label)

    def _on_serve_stop_clicked(self):
        job_id = "serve-stop-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self._submit_spec(JobSpec(
            job_id=job_id, job_type="serve_stop",
            port=self._serve_port.model.get_value_as_int(), gpu=_SERVE_GPU,
        ), status_label=self._serve_status_label)

    def _on_serve_check_clicked(self):
        """잡 큐/status 파일이 아니라 서버의 컨테이너·API 실체를 직접 확인."""
        transport = self._transport()
        port = self._serve_port.model.get_value_as_int()
        if not hasattr(transport, "run"):  # REST 데몬엔 컨테이너 조회 API가 없음
            self._serve_status_label.text = "check: use SSH host (user@host)"
            return
        self._serve_status_label.text = "checking container/api..."

        def work():
            try:
                _, out = transport.run(build_serve_check_command(port))
                txt = "  ".join(x.strip() for x in out.splitlines() if "=" in x) or out[-100:]
            except Exception as exc:
                txt = f"check error: {exc!r}"
            self._set_status(txt, self._serve_status_label)

        threading.Thread(target=work, daemon=True, name="ServeCheck").start()

    def _on_status_clicked(self):
        if not self._last_job_id:
            self._status_label.text = "no job submitted yet"
            return
        transport = self._transport()
        ext_root = self._ext_root.model.get_value_as_string().strip()
        job_id = self._last_job_id
        label = self._last_status_label or self._status_label
        label.text = f"checking {job_id}..."

        def work():
            st = read_status(job_id, transport, ext_root)
            state = st.get("state", "?")
            extra = ""
            if "episodes_done" in st:  # generate 잡: 진행 카운트 표시
                extra = f" ({st.get('episodes_done', '?')}/{st.get('total', '?')})"
            note = st.get("note", "")  # 실패 사유·부가 정보 (모든 잡 타입)
            if note:
                extra += f" — {note}"
            self._set_status(f"{job_id}: {state}{extra}", label)

        threading.Thread(target=work, daemon=True, name="RemoteGenStatus").start()
