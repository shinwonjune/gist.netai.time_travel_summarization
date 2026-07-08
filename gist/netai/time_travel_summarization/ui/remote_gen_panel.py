"""원격 데이터 생성 패널 — 플랫폼 제어면의 GUI 클라이언트.

automation.remote_generation(잡 스펙 + 전송 어댑터) 위의 얇은 UI 층:
파라미터 폼 → JobSpec 조립 → 제출(SSH/local, 백그라운드 스레드) → 상태 폴링.
SSH는 블로킹이므로 UI 프리즈 방지를 위해 스레드에서 돌리고, 결과 반영은
UiTaskDispatcher(메인 루프)로 되돌린다.
"""
from __future__ import annotations

import datetime
import threading

from ..automation.remote_generation import (
    JobSpec, LocalTransport, SSHTransport, read_status, submit_job,
)

_DEFAULT_HOST = "netai@sv4000-2"
_DEFAULT_EXT_ROOT = "~/wonjune/kit-app-template/source/extensions/gist.netai.time_travel_summarization"
_DEFAULT_STAGE = ("omniverse://10.38.38.32/Projects/Dream-AI_Plus_Twin/"
                  "Workspace_Personal/swj/AI-Grad_Building/A_AI-Grad_Building.usd")
_UPLOAD_PREFIX = "s3://time-travel-summarization/episodes"


class RemoteGenWindow:
    """독립 창 — 데이터 생성은 재현 제어(Time Travel Control)와 관심사가 달라 분리.

    extension.py가 다른 창들과 같은 패턴으로 생성/파괴한다.
    """

    def __init__(self):
        import omni.ui as ui

        from .task_dispatcher import UiTaskDispatcher

        self._dispatcher = UiTaskDispatcher("RemoteGenWindowUiDispatcher")
        self._window = ui.Window("Data Generation", width=520, height=290)
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

    # ---- helpers ----------------------------------------------------------- #

    def _set_status(self, text: str):
        # 스레드에서 불려도 안전하게 디스패처 경유
        self._dispatcher.submit(lambda: setattr(self._status_label, "text", text))

    def _transport(self):
        host = self._host.model.get_value_as_string().strip()
        return LocalTransport() if host in ("", "local") else SSHTransport(host)

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
            upload_uri=upload,
        )

    # ---- callbacks ---------------------------------------------------------- #

    def _on_submit_clicked(self):
        try:
            spec = self._build_spec()
        except Exception as exc:
            self._status_label.text = f"spec error: {exc}"
            return
        transport = self._transport()
        ext_root = self._ext_root.model.get_value_as_string().strip()
        self._last_job_id = spec.job_id
        self._status_label.text = f"submitting {spec.job_id} via {transport.name}..."

        def work():
            try:
                ok, out = submit_job(spec, transport, ext_root)
                msg = (f"{spec.job_id} submitted (tmux job-{spec.job_id})"
                       if ok else f"submit FAILED: {out[-120:]}")
            except Exception as exc:
                msg = f"submit error: {exc!r}"
            self._set_status(msg)

        threading.Thread(target=work, daemon=True, name="RemoteGenSubmit").start()

    def _on_status_clicked(self):
        if not self._last_job_id:
            self._status_label.text = "no job submitted yet"
            return
        transport = self._transport()
        ext_root = self._ext_root.model.get_value_as_string().strip()
        job_id = self._last_job_id
        self._status_label.text = f"checking {job_id}..."

        def work():
            st = read_status(job_id, transport, ext_root)
            done = st.get("episodes_done", "?")
            total = st.get("total", "?")
            self._set_status(f"{job_id}: {st.get('state', '?')} ({done}/{total})")

        threading.Thread(target=work, daemon=True, name="RemoteGenStatus").start()
