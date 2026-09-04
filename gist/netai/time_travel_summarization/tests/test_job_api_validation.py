"""job_api.JobRequest의 자유 문자열 필드 최소 검증(공백류·"-" 시작 금지) 단위 테스트.

FastAPI TestClient/실서버 없이 pydantic 모델을 직접 생성해 검증한다. job_api.py는
import 시점에 STORE(SqliteJobStore)를 생성하므로, 리포 트리에 artifacts/jobs/jobs.db가
생기지 않도록 임시 디렉토리를 JOB_STORE_URL로 미리 지정해 둔다(다른 테스트·실서버
상태와 격리).
"""
import os
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("fastapi")

_tmp_store_dir = tempfile.mkdtemp(prefix="job_api_validation_test_")
os.environ.setdefault("JOB_STORE_URL", f"sqlite:///{Path(_tmp_store_dir) / 'jobs.db'}")

from pydantic import ValidationError  # noqa: E402

from gist.netai.time_travel_summarization.VLM_server.l40.job_api import (  # noqa: E402
    JobRequest, _STRICT_STRING_FIELDS,
)


def _minimal_kwargs(**overrides) -> dict:
    kwargs = {"job_type": "generate"}
    kwargs.update(overrides)
    return kwargs


@pytest.mark.parametrize("field", _STRICT_STRING_FIELDS)
def test_normal_values_pass(field):
    for value in ("omniverse://server/path/to/stage.usd", "s3://bucket/key/prefix", "/abs/path/name",
                  "plain_name-01", ""):
        req = JobRequest(**_minimal_kwargs(**{field: value}))
        assert getattr(req, field) == value


@pytest.mark.parametrize("field", _STRICT_STRING_FIELDS)
def test_whitespace_is_rejected(field):
    with pytest.raises(ValidationError):
        JobRequest(**_minimal_kwargs(**{field: "has space"}))
    with pytest.raises(ValidationError):
        JobRequest(**_minimal_kwargs(**{field: "line1\nline2"}))
    with pytest.raises(ValidationError):
        JobRequest(**_minimal_kwargs(**{field: "tab\there"}))


@pytest.mark.parametrize("field", _STRICT_STRING_FIELDS)
def test_leading_dash_is_rejected(field):
    with pytest.raises(ValidationError):
        JobRequest(**_minimal_kwargs(**{field: "--stage=evil"}))
