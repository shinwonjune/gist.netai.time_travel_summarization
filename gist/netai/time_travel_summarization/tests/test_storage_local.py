import pytest

from gist.netai.time_travel_summarization.storage import from_uri


def _uri(path) -> str:
    return f"file://{path}"


def test_put_bytes_then_open_read_returns_identical_bytes(tmp_path):
    adapter = from_uri(_uri(tmp_path / "foo.txt"))
    uri = _uri(tmp_path / "foo.txt")

    adapter.put_bytes(uri, b"hello storage")

    with adapter.open_read(uri) as stream:
        assert stream.read() == b"hello storage"


def test_put_file_then_stat_size_matches(tmp_path):
    adapter = from_uri(_uri(tmp_path / "copied.bin"))
    source = tmp_path / "source.bin"
    source.write_bytes(b"x" * 17)
    uri = _uri(tmp_path / "copied.bin")

    adapter.put_file(uri, source)

    assert adapter.stat(uri).size == 17


def test_list_prefix_non_recursive_returns_only_immediate_children(tmp_path):
    adapter = from_uri(_uri(tmp_path / "root" / "a.txt"))
    root = tmp_path / "root"
    adapter.put_bytes(_uri(root / "a.txt"), b"a")
    adapter.put_bytes(_uri(root / "b.txt"), b"b")
    adapter.put_bytes(_uri(root / "nested" / "c.txt"), b"c")

    objects = list(adapter.list_prefix(f"file://{root}/", recursive=False))

    assert [obj.uri for obj in objects] == [
        (root / "a.txt").resolve().as_uri(),
        (root / "b.txt").resolve().as_uri(),
    ]


def test_list_prefix_recursive_returns_nested_files(tmp_path):
    adapter = from_uri(_uri(tmp_path / "root" / "a.txt"))
    root = tmp_path / "root"
    adapter.put_bytes(_uri(root / "a.txt"), b"a")
    adapter.put_bytes(_uri(root / "nested" / "b.txt"), b"b")

    objects = list(adapter.list_prefix(f"file://{root}/", recursive=True))

    assert [obj.uri for obj in objects] == [
        (root / "a.txt").resolve().as_uri(),
        (root / "nested" / "b.txt").resolve().as_uri(),
    ]


def test_exists_true_after_put_and_false_for_missing(tmp_path):
    adapter = from_uri(_uri(tmp_path / "present.txt"))
    present = _uri(tmp_path / "present.txt")
    missing = _uri(tmp_path / "missing.txt")

    adapter.put_bytes(present, b"present")

    assert adapter.exists(present)
    assert not adapter.exists(missing)


def test_stat_missing_uri_raises_file_not_found(tmp_path):
    adapter = from_uri(_uri(tmp_path / "missing.txt"))

    with pytest.raises(FileNotFoundError):
        adapter.stat(_uri(tmp_path / "missing.txt"))
