"""창 워크스페이스 유틸 — 핫리로드 유령 창 방지."""


def close_existing_window(title: str) -> None:
    """같은 제목의 이전 창이 남아 있으면 파괴하고 새로 만들게 한다.

    확장 핫리로드에서 shutdown이 온전히 돌지 못하면 옛 창이 새 창과 같은
    위치에 겹쳐 남는다 — 텍스트가 달라진 행만 이중으로 보이는 유령
    (실측: Video ID의 'Not uploaded'와 새 파일명 겹침). 생성 직전 호출할 것.
    """
    try:
        import omni.ui as ui

        existing = ui.Workspace.get_window(title)
        if existing is None:
            return
        destroy = getattr(existing, "destroy", None)
        if callable(destroy):
            destroy()
        else:
            existing.visible = False
    except Exception:
        pass  # 워크스페이스 조회 실패가 창 생성을 막으면 안 된다
