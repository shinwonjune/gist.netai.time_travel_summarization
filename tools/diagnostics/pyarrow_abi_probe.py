"""Probe pyarrow ABI inside Omniverse Kit.

Run this file from Omniverse Script Editor first with SMOKE_LEVEL = 0.
Only raise SMOKE_LEVEL after import/path evidence looks correct.
"""

from __future__ import annotations

import importlib.machinery
import io
import json
import os
import platform
import re
import sys
import tempfile
from pathlib import Path


SMOKE_LEVEL = int(globals().get("SMOKE_LEVEL", os.environ.get("TTS_PYARROW_SMOKE_LEVEL", "0")))
# 0: import and path checks only
# 1: create Arrow arrays/tables
# 2: write/read a tiny parquet buffer and temp file
# 3: read a real project URI (manifest.json or .parquet/.csv) through storage adapters

TEST_URI = str(globals().get("TEST_URI", os.environ.get("TTS_PYARROW_TEST_URI", ""))).strip()

LOG_PATH = Path(
    globals().get(
        "PROBE_LOG_PATH",
        os.environ.get("TTS_PYARROW_PROBE_LOG", str(Path(tempfile.gettempdir()) / "tts_pyarrow_abi_probe.txt")),
    )
)
_LOG_LINES: list[str] = []


def _write_log() -> None:
    try:
        LOG_PATH.write_text("\n".join(_LOG_LINES) + "\n", encoding="utf-8")
    except Exception:
        pass


def _emit(*parts: object) -> None:
    message = " ".join(str(part) for part in parts)
    lines = message.splitlines() or [""]
    try:
        import carb
    except Exception:
        carb = None
    for line in lines:
        print(line)
        _LOG_LINES.append(line)
        if carb is not None:
            try:
                carb.log_warn(f"[PyArrowProbe] {line}")
            except Exception:
                pass
    _write_log()


def _print_header(title: str) -> None:
    _emit("")
    _emit("=" * 70)
    _emit(title)
    _emit("=" * 70)


def _native_tag(path: str) -> str:
    name = os.path.basename(path)
    match = re.search(r"\.cp(\d+)-win_amd64\.pyd$", name)
    if match:
        return f"cp{match.group(1)}"
    match = re.search(r"\.cpython-(\d+)[-.]", name)
    if match:
        return f"cpython-{match.group(1)}"
    return "unknown"


def _expected_tags() -> set[str]:
    cache_tag = getattr(sys.implementation, "cache_tag", "") or ""
    tags = {cache_tag}
    match = re.search(r"(\d+)$", cache_tag)
    if match:
        tags.add(f"cp{match.group(1)}")
        tags.add(f"cpython-{match.group(1)}")
    return tags


def _join_uri(base: str, name: str) -> str:
    return base.rstrip("/") + "/" + name


def _dataset_uri_from_manifest(uri: str) -> str:
    marker = "manifest.json"
    if uri.lower().endswith(marker):
        return uri[: -(len(marker) + 1)]
    return uri.rstrip("/")


def _read_project_uri(uri: str) -> bytes:
    from gist.netai.time_travel_summarization.storage import from_uri

    adapter = from_uri(uri)
    try:
        stat = adapter.stat(uri)
        _emit("object stat:", stat)
    except Exception as exc:
        _emit("object stat: unavailable", type(exc).__name__, repr(exc))
    with adapter.open_read(uri) as stream:
        raw = stream.read()
    _emit("download bytes:", len(raw))
    return raw


def _smoke_project_uri(uri: str, pq) -> None:
    _print_header("Project URI Smoke")
    _emit("test uri:", uri or "(not set)")
    if not uri:
        _emit("SMOKE_LEVEL=3 skipped. Set TEST_URI or TTS_PYARROW_TEST_URI.")
        return

    target_uri = uri
    lowered = target_uri.lower()
    if lowered.endswith("manifest.json"):
        raw_manifest = _read_project_uri(target_uri)
        manifest = json.loads(raw_manifest.decode("utf-8"))
        chunks = list(manifest.get("chunks", []))
        _emit("manifest format:", manifest.get("format"))
        _emit("manifest rows:", manifest.get("rows"))
        _emit("manifest chunks:", len(chunks))
        if not chunks:
            _emit("manifest has no chunks")
            return
        target_uri = _join_uri(_dataset_uri_from_manifest(target_uri), chunks[0]["key"])
        _emit("first chunk uri:", target_uri)
        lowered = target_uri.lower()

    raw = _read_project_uri(target_uri)
    if lowered.endswith(".parquet"):
        table = pq.read_table(io.BytesIO(raw))
        _emit("parquet schema:", table.schema)
        _emit("parquet rows:", table.num_rows)
        _emit("sample rows:", table.slice(0, min(3, table.num_rows)).to_pylist())
    elif lowered.endswith(".csv"):
        text = raw.decode("utf-8")
        lines = text.splitlines()
        _emit("csv lines:", len(lines))
        _emit("csv header:", lines[0] if lines else "")
        _emit("csv sample:", lines[1] if len(lines) > 1 else "")
    else:
        _emit("unsupported test uri extension:", target_uri)


def main() -> None:
    _emit("probe log:", LOG_PATH)
    _emit("smoke_level:", SMOKE_LEVEL)

    _print_header("Python Runtime")
    _emit("executable:", sys.executable)
    _emit("version:", sys.version)
    _emit("implementation:", sys.implementation)
    _emit("cache_tag:", getattr(sys.implementation, "cache_tag", None))
    _emit("platform:", platform.platform())
    _emit("extension_suffixes:", importlib.machinery.EXTENSION_SUFFIXES)

    _print_header("Relevant sys.path")
    for path in sys.path:
        lowered = path.lower()
        if "pip3-envs" in lowered or "site-packages" in lowered or "pyarrow" in lowered:
            _emit(path)

    _print_header("pyarrow Import")
    try:
        import pyarrow
        import pyarrow.lib
        import pyarrow.parquet as pq
    except Exception as exc:
        _emit("pyarrow import: FAIL")
        _emit(type(exc).__name__, repr(exc))
        return

    _emit("pyarrow import: OK")
    _emit("pyarrow version:", getattr(pyarrow, "__version__", None))
    _emit("pyarrow file:", getattr(pyarrow, "__file__", None))
    _emit("pyarrow.lib file:", getattr(pyarrow.lib, "__file__", None))
    _emit("pyarrow.parquet file:", getattr(pq, "__file__", None))

    lib_file = getattr(pyarrow.lib, "__file__", "") or ""
    native_tag = _native_tag(lib_file)
    expected = _expected_tags()
    _emit("native tag:", native_tag)
    _emit("expected tags:", sorted(expected))
    if native_tag != "unknown" and native_tag not in expected:
        _emit("ABI CHECK: MISMATCH")
    else:
        _emit("ABI CHECK: no obvious mismatch from filename")

    if SMOKE_LEVEL < 1:
        _emit("")
        _emit("SMOKE_LEVEL=0 complete. Set SMOKE_LEVEL=1 only after reviewing output.")
        return

    _print_header("Arrow Table Smoke")
    table = pyarrow.table(
        {
            "timestamp": ["2025-01-01 00:00:00.000"],
            "objid": ["obj001"],
            "x": [1.0],
            "y": [2.0],
            "z": [3.0],
        }
    )
    _emit("table schema:", table.schema)
    _emit("table rows:", table.num_rows)

    if SMOKE_LEVEL < 2:
        _emit("")
        _emit("SMOKE_LEVEL=1 complete. Set SMOKE_LEVEL=2 for parquet write/read.")
        return

    _print_header("Parquet Smoke")
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    _emit("buffer bytes:", len(buf.getvalue()))
    read_back = pq.read_table(io.BytesIO(buf.getvalue()))
    _emit("buffer read rows:", read_back.num_rows)

    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        pq.write_table(table, tmp_path, compression="snappy")
        read_back = pq.read_table(tmp_path)
        _emit("temp file:", tmp_path)
        _emit("temp read rows:", read_back.num_rows)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    _emit("")
    _emit("SMOKE_LEVEL=2 complete.")

    if SMOKE_LEVEL < 3:
        return

    _smoke_project_uri(TEST_URI, pq)
    _emit("")
    _emit("SMOKE_LEVEL=3 complete.")


main()
