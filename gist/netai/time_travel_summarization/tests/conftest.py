"""테스트 공용 헬퍼.

주의: 여기 둔 헬퍼는 **호출 시점에만** sys.modules를 건드리는 일반 함수여야 한다.
autouse fixture로 전역 주입하면 안 된다 — omni/carb 스텁을 모듈 import 시점에
전역으로 심으면 그 오염이 다른 테스트로 새어나간다(test_vlm_window_shorten.py가
omni 스텁을 setUpClass/tearDownClass 범위로 한정해 두는 이유가 바로 이거다 —
그 파일 상단 주석 참조. sys.modules 오염이 test_encoder.py의 "omni 미로드"
단언으로 새는 사고를 막기 위함).
"""
import sys
import types


def install_carb_stub(with_stage_object_controller: bool = False) -> None:
    """carb 모듈 스텁을 sys.modules에 심는다. 필요하면 stage_object_controller도 같이.

    carb는 Omniverse 런타임 밖(WSL pytest)에서 import 불가능한 네이티브 바인딩이라,
    carb.log_* 만 no-op으로 흉내 내 순수 로직만 시험할 수 있게 한다.
    with_stage_object_controller=True면
    ``playback.stage_object_controller.StageObjectController``도 (Kit 없이 못 만드는
    클래스이므로) 최소 스텁 ``object``로 등록한다 — 이 심볼을 import는 하되 실제로는
    쓰지 않는 테스트 대상 모듈이 로드 시 죽지 않게 하기 위함.
    """
    carb = types.ModuleType("carb")
    carb.log_info = lambda *args, **kwargs: None
    carb.log_warn = lambda *args, **kwargs: None
    carb.log_error = lambda *args, **kwargs: None
    sys.modules["carb"] = carb

    if with_stage_object_controller:
        stage_module = types.ModuleType(
            "gist.netai.time_travel_summarization.playback.stage_object_controller"
        )
        stage_module.StageObjectController = object
        sys.modules[
            "gist.netai.time_travel_summarization.playback.stage_object_controller"
        ] = stage_module
