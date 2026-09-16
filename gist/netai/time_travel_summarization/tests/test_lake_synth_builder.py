"""객체 수 스윕 데이터셋 빌더·합성 생성기 확장 테스트 (레이크성능_실험설계.md §1-1).

stdlib만으로 돈다 — csv + file:// 어댑터. 검증:
  - generate_synthetic_rows: 축별 범위(bounds_xyz)·y 고정·y 시계열 주입·기본 경로 불변
  - build_datasets: rows/청크 수/꼬리 없음/y가 소스에서 왔는지/사이드카/manifest 존재 시 중단/dry-run
  - arena_bounds: 씬 프로파일 aigrad_building_v1 → x·z 범위, y = spawn_floor
"""
from __future__ import annotations

import datetime
import json
import tempfile
import unittest
from pathlib import Path

from gist.netai.time_travel_summarization.playback.lake_common import (
    generate_synthetic_rows, ingest_synthetic, join_uri, manifest_uri,
)
from gist.netai.time_travel_summarization.storage import from_uri
from gist.netai.time_travel_summarization.utils.build_lake_synth_dataset import (
    arena_bounds, build_datasets, load_y_series,
)

_FMT = "%Y-%m-%d %H:%M:%S.%f"


class GeneratorTest(unittest.TestCase):
    def test_default_path_unchanged_shape(self):
        rows = list(generate_synthetic_rows(3, 2.0, hz=5.0))
        self.assertEqual(len(rows), 30)
        self.assertEqual({r["objid"] for r in rows}, {"obj001", "obj002", "obj003"})
        for r in rows:
            for a in ("x", "y", "z"):
                self.assertTrue(0.0 <= r[a] <= 1000.0)
        # 같은 seed면 결정적
        self.assertEqual(rows, list(generate_synthetic_rows(3, 2.0, hz=5.0)))

    def test_default_path_pinned_values(self):
        """확장 전(2026-09-16 이전) 생성기와 바이트 동일해야 한다 — 첫 3행을 고정값으로 박아 둔다."""
        rows = list(generate_synthetic_rows(2, 1.0, hz=5.0))
        got = [(r["objid"], round(r["x"], 6), round(r["y"], 6), round(r["z"], 6)) for r in rows[:3]]
        self.assertEqual(got, [
            ("obj001", 648.839108, 15.097287, 273.155442),
            ("obj002", 211.925871, 729.718526, 676.828014),
            ("obj001", 637.475971, 7.869391, 276.752669),
        ])

    def test_empty_y_series_rejected(self):
        with self.assertRaises(ValueError):
            list(generate_synthetic_rows(2, 1.0, hz=5.0, y_series=[[1.0], []]))

    def test_bounds_xyz_and_fixed_y(self):
        rows = list(generate_synthetic_rows(
            4, 3.0, hz=10.0, bounds_xyz=((100.0, 200.0), (89.5, 89.5), (-50.0, -10.0))))
        self.assertEqual(len(rows), 4 * 30)
        for r in rows:
            self.assertTrue(100.0 <= r["x"] <= 200.0)
            self.assertEqual(r["y"], 89.5)
            self.assertTrue(-50.0 <= r["z"] <= -10.0)
        # 움직이는 객체: 연속 샘플 좌표가 실제로 바뀐다
        o1 = [r for r in rows if r["objid"] == "obj001"]
        self.assertTrue(any(o1[i]["x"] != o1[i + 1]["x"] for i in range(len(o1) - 1)))

    def test_y_series_injection(self):
        series = [[1.0, 2.0, 3.0], [7.0, 8.0]]
        rows = list(generate_synthetic_rows(3, 1.0, hz=5.0, y_series=series))
        for r in rows:
            k = int(r["objid"][3:]) - 1
            self.assertIn(r["y"], series[k % 2])
        # 객체 1의 y는 series[0]을 순환(위상만 다름)
        o1 = [r["y"] for r in rows if r["objid"] == "obj001"]
        self.assertEqual(sorted(set(o1)), [1.0, 2.0, 3.0])


class BuilderTest(unittest.TestCase):
    def _source(self, d: str) -> str:
        """y-source 역할의 소형 '실궤적' 데이터셋(2obj × 12s @5Hz, csv, 6s 청크)."""
        uri = (Path(d) / "real").resolve().as_uri()
        ingest_synthetic(uri, n_objects=2, duration_s=12.0, hz=5.0, chunk_seconds=6, fmt="csv")
        return uri

    def test_load_y_series(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._source(d)
            series = load_y_series(src)
            self.assertEqual(len(series), 2)
            self.assertEqual([len(s) for s in series], [60, 60])

    def test_build_rows_chunks_no_tail_y_from_source_sidecar(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._source(d)
            series = load_y_series(src)
            root = (Path(d) / "lake").resolve().as_uri()
            start = "2026-01-01 00:00:00.000"
            manifests = build_datasets(
                [3], root=root, name_template="synth_{n}obj_test", chunk_seconds=[6],
                hz=5.0, span_s=12.0, start=start,
                bounds_xyz=((0.0, 10.0), (1.0, 1.0), (0.0, 10.0)),
                y_series=series, y_source=src, scene_profile="test", fmt="csv")
            self.assertEqual(len(manifests), 1)
            m = manifests[0]
            self.assertEqual(m["rows"], 3 * 60)
            self.assertEqual(len(m["chunks"]), 2)                # 12s / 6s — 꼬리 없음
            self.assertEqual(m["chunks"][1]["rows"], 3 * 30)     # 마지막 청크도 온전한 30샘플
            last_end = datetime.datetime.strptime(m["chunks"][-1]["end"], _FMT)
            base = datetime.datetime.strptime(start, _FMT)
            self.assertAlmostEqual((last_end - base).total_seconds(), 11.8, places=3)
            self.assertEqual(m["objids"], ["obj001", "obj002", "obj003"])
            self.assertIn("tracks", m)
            # y는 소스 시계열 값만 쓴다
            allowed = {round(v, 6) for s in series for v in s}
            duri = f"{root}/synth_3obj_test_c6"
            ys = set()
            for ch in m["chunks"]:
                curi = join_uri(duri, ch["key"])
                with from_uri(curi).open_read(curi) as fh:
                    for line in fh.read().decode("utf-8").splitlines()[1:]:
                        ys.add(round(float(line.split(",")[3]), 6))
            self.assertTrue(ys <= allowed, ys - allowed)
            # 사이드카
            suri = join_uri(duri, "_build.json")
            with from_uri(suri).open_read(suri) as fh:
                side = json.loads(fh.read().decode("utf-8"))
            self.assertEqual(side["n_objects"], 3)
            self.assertEqual(side["y_series_objects"], 2)
            self.assertEqual(side["rows"], 180)
            # 두 번째 빌드는 manifest 존재로 중단
            with self.assertRaises(SystemExit):
                build_datasets([3], root=root, name_template="synth_{n}obj_test",
                               chunk_seconds=[6], hz=5.0, span_s=12.0, start=start, fmt="csv")

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            root = (Path(d) / "lake").resolve().as_uri()
            out = build_datasets([2, 3], root=root, name_template="s_{n}", chunk_seconds=[6, 12],
                                 hz=5.0, span_s=12.0, fmt="csv", dry_run=True)
            self.assertEqual(out, [])
            self.assertFalse((Path(d) / "lake").exists())
            self.assertFalse(from_uri(manifest_uri(f"{root}/s_2_c6")).exists(manifest_uri(f"{root}/s_2_c6")))

    def test_arena_bounds_from_profile(self):
        b = arena_bounds("aigrad_building_v1")
        self.assertEqual(b[0], (206.0, 1476.0))
        self.assertEqual(b[1], (89.5, 89.5))
        self.assertEqual(b[2], (-2935.0, -1490.0))


if __name__ == "__main__":
    unittest.main()
