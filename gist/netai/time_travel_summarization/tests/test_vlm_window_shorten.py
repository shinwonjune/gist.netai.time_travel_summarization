import sys
import types
import unittest

# window.py는 omni.ui / carb / omni.kit.app 를 import한다. 순수 헬퍼만 시험하므로
# 최소 스텁을 setUpClass에서 심고 tearDownClass에서 되돌린다 — 모듈 로드 시점에
# 심으면 sys.modules 오염이 다른 테스트(test_encoder의 omni 미로드 단언)로 샌다.
_STUBBED = ("omni", "omni.ui", "omni.kit", "omni.kit.app", "carb")


class ShortenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = {name: sys.modules.get(name) for name in _STUBBED}

        carb = types.ModuleType("carb")
        carb.log_info = carb.log_warn = carb.log_error = lambda *a, **k: None
        sys.modules["carb"] = carb

        omni = types.ModuleType("omni")
        omni.ui = types.ModuleType("omni.ui")
        omni.kit = types.ModuleType("omni.kit")
        omni.kit.app = types.ModuleType("omni.kit.app")
        sys.modules["omni"] = omni
        sys.modules["omni.ui"] = omni.ui
        sys.modules["omni.kit"] = omni.kit
        sys.modules["omni.kit.app"] = omni.kit.app

        from gist.netai.time_travel_summarization.vlm_client.window import _shorten
        cls._shorten = staticmethod(_shorten)

    @classmethod
    def tearDownClass(cls):
        for name, mod in cls._saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        # 로드된 window 모듈도 제거해 omni 참조가 남지 않게 한다.
        sys.modules.pop(
            "gist.netai.time_travel_summarization.vlm_client.window", None
        )

    def test_short_string_passes_through(self):
        self.assertEqual(self._shorten("video_19.mp4"), "video_19.mp4")

    def test_long_uri_elided_length_and_ends_preserved(self):
        uri = "s3://bev-lake/prod-20260707/videos/" + "x" * 80 + "/clip_042.mp4"
        out = self._shorten(uri, limit=48)
        self.assertLessEqual(len(out), 48)
        self.assertIn("...", out)
        self.assertTrue(out.startswith(uri[:20]))
        self.assertTrue(out.endswith(uri[-25:]))  # limit - 23 = 25 tail chars

    def test_boundary_equal_to_limit_not_elided(self):
        self.assertEqual(self._shorten("a" * 48, limit=48), "a" * 48)
        self.assertIn("...", self._shorten("a" * 49, limit=48))


if __name__ == "__main__":
    unittest.main()
