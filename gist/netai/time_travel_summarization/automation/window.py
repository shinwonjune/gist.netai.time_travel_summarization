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
    DAEMON_PORT, VLLM_PORT, JobSpec, SSHTunnel, build_serve_check_command,
    connect_server, read_status, submit_job, transport_from_host,
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
        from ..ui.workspace import close_existing_window
        close_existing_window("Remote Jobs")  # 핫리로드 유령 창 방지
        self._window = ui.Window("Remote Jobs", width=520, height=540)
        with self._window.frame:
            with ui.VStack(spacing=5):
                self._panel = RemoteGenPanel(self._dispatcher)

    def destroy(self):
        if self._panel:
            self._panel.shutdown()  # SSH 터널 정리 (고아 프로세스 방지)
            self._panel = None
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
        self._tunnel = None             # Connect Server가 소유하는 SSH 터널
        self._ssh_host = ""             # REST 전환 후 Disconnect 시 되돌릴 원래 호스트

        ui.Label("Remote Data Generation", height=20, style={"font_size": 14, "font_weight": "bold"})

        with ui.HStack(height=25, spacing=8):
            ui.Label("Host:", width=85)
            self._host = ui.StringField(width=170)  # 버튼 밀림 방지 고정폭
            self._host.model.set_value(_DEFAULT_HOST)
            self._connect_btn = ui.Button("Connect Server", width=110)
            self._connect_btn.set_clicked_fn(self._on_connect_clicked)
        with ui.HStack(height=25, spacing=8):
            ui.Label("Ext root:", width=85)
            self._ext_root = ui.StringField()
            self._ext_root.model.set_value(_DEFAULT_EXT_ROOT)
        with ui.HStack(height=25, spacing=8):
            ui.Label("Scene profile:", width=85)
            # scene_profiles.json의 프로파일 이름. 지정 시 아레나 범위, 스테이지,
            # 카메라를 프로파일이 제공(데이터 로드 불필요). Stage/Camera 입력이 우선.
            self._scene_profile = ui.StringField()
            self._scene_profile.model.set_value("aigrad_building_v1")
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
        ui.Label("Training (LoRA)", height=20, style={"font_size": 14, "font_weight": "bold"})
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
        ui.Label("Serving (vLLM)", height=20, style={"font_size": 14, "font_weight": "bold"})
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
            # serve 전용 상태 — 버튼 행 인라인(별도 빈 행을 두면 섹션 사이가 벌어져 보임)
            self._serve_status_label = ui.Label("", style={"color": 0xFF888888})

        # ---- Replay Render — 좌표 구간 재연 렌더 잡 --------------------------- #
        ui.Spacer(height=8)
        ui.Label("Replay Render", height=20, style={"font_size": 14, "font_weight": "bold"})
        with ui.HStack(height=25, spacing=8):
            # "YYYY-MM-DD HH:MM:SS"(19자) 고정폭 — 창 폭 독점 방지, 두 필드 한 줄
            ui.Label("Start:", width=45)
            self._replay_start = ui.StringField(width=145)
            ui.Label("End:", width=35)
            self._replay_end = ui.StringField(width=145)
        with ui.HStack(height=25, spacing=8):
            ui.Label("Data URI:", width=85)
            self._replay_data = ui.StringField()
            ui.Label("(empty = config default)", style={"color": 0xFF888888})
        with ui.HStack(height=28, spacing=10):
            use_range_btn = ui.Button("Use current range", width=130)
            use_range_btn.set_clicked_fn(self._on_use_current_range)
            submit_replay = ui.Button("Submit Replay", width=120)
            submit_replay.set_clicked_fn(self._on_replay_clicked)
            # replay 전용 상태 — 버튼 행 인라인 (Camera/Stage/GPU/upload는 상단 값 재사용)
            self._replay_status_label = ui.Label("", style={"color": 0xFF888888})
        ui.Spacer()  # 남는 세로 공간을 맨 아래로 흡수 — 섹션 사이가 벌어지지 않게

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
            scene_profile=self._scene_profile.model.get_value_as_string().strip(),
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

    def _on_connect_clicked(self):
        """서버 연결 공용 버튼: 데몬 멱등 기동(SSH) + 터널(8800/38011) + health.

        성공 시 Host를 REST URL로 전환 → 이후 제출은 잡 큐(job_api) 경유,
        vLLM도 localhost:38011 직결. 다시 누르면 해제(Disconnect).
        """
        if self._tunnel and self._tunnel.alive():
            self._tunnel.stop()
            self._connect_btn.text = "Connect Server"
            if self._ssh_host:
                self._host.model.set_value(self._ssh_host)
            self._status_label.text = "disconnected"
            return
        host = self._host.model.get_value_as_string().strip()
        if "@" not in host:
            self._status_label.text = "connect: enter SSH host (user@host)"
            return
        ext_root = self._ext_root.model.get_value_as_string().strip()
        self._ssh_host = host
        if self._tunnel is None:
            self._tunnel = SSHTunnel(
                host, [(DAEMON_PORT, DAEMON_PORT), (VLLM_PORT, VLLM_PORT)])
        else:
            self._tunnel.host = host
        self._status_label.text = "connecting (daemon + tunnel)..."

        def work():
            try:
                ok, msg = connect_server(host, ext_root, self._tunnel)
            except Exception as exc:
                ok, msg = False, f"connect error: {exc!r}"

            def apply():
                self._status_label.text = msg
                if ok:
                    self._host.model.set_value(f"http://localhost:{DAEMON_PORT}")
                    self._connect_btn.text = "Disconnect"

            self._dispatcher.submit(apply)

        threading.Thread(target=work, daemon=True, name="ServerConnect").start()

    def shutdown(self):
        """창 파괴 시 터널 정리 — 고아 ssh 프로세스 방지."""
        if self._tunnel:
            self._tunnel.stop()

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

    def _on_use_current_range(self):
        """메인 core의 현재 재생 범위(get_start/end_time)로 Start/End를 채운다."""
        try:
            from ..extension import get_active_core
            core = get_active_core()
        except Exception as exc:
            self._replay_status_label.text = f"replay: core unavailable ({exc!r})"
            return
        if core is None:
            self._replay_status_label.text = "replay: no active core (open a scene first)"
            return
        try:
            start = core.get_start_time()
            end = core.get_end_time()
        except Exception as exc:
            self._replay_status_label.text = f"replay: cannot read range ({exc!r})"
            return
        if start is None or end is None:
            self._replay_status_label.text = "replay: no playback range loaded"
            return
        fmt = "%Y-%m-%d %H:%M:%S"
        self._replay_start.model.set_value(start.strftime(fmt))
        self._replay_end.model.set_value(end.strftime(fmt))
        self._replay_status_label.text = f"replay: filled range {start.strftime(fmt)} .. {end.strftime(fmt)}"

    def _on_replay_clicked(self):
        start = self._replay_start.model.get_value_as_string().strip()
        end = self._replay_end.model.get_value_as_string().strip()
        if not start or not end:
            self._replay_status_label.text = "replay: Start and End required (YYYY-MM-DD HH:MM:SS)"
            return
        job_id = "replay-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        upload = (f"s3://time-travel-summarization/replays/{job_id}"
                  if self._upload_checkbox.model.get_value_as_bool() else "")
        from ..storage import normalize_source
        # minIO 콘솔 복사(`버킷/키`) Data URI 입력에 s3:// 접두
        data_uri = normalize_source(self._replay_data.model.get_value_as_string())
        self._submit_spec(JobSpec(
            job_id=job_id, job_type="replay",
            replay_start=start, replay_end=end,
            data_uri=data_uri,
            camera=self._camera.model.get_value_as_string().strip(),
            stage=self._stage.model.get_value_as_string().strip(),
            app_kit=self._app_kit.model.get_value_as_string().strip(),
            gpu=self._gpu.model.get_value_as_int(),
            upload_uri=upload,
        ), status_label=self._replay_status_label)

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
                extra += f" - {note}"
            self._set_status(f"{job_id}: {state}{extra}", label)

        threading.Thread(target=work, daemon=True, name="RemoteGenStatus").start()
