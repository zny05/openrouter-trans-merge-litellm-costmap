"""Offline tests: python -m unittest test_litellm_converter_gui -v"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import litellm_converter_gui as gui


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = {"data": [{"id": "vendor/model", "pricing": {"prompt": "0.001", "completion": "0"},
                              "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                              "context_length": 1000}], "total_count": 1}

    def opener(self, payload):
        return Mock(open=Mock(return_value=io.BytesIO(json.dumps(payload).encode())))

    def test_fetch_convert_merge(self):
        opener = self.opener(self.raw)
        downloaded, custom, official, merged = [self.root / name for name in
                                                ("download.json", "custom.json", "official.json", "merged.json")]
        with patch.object(gui.request, "build_opener", return_value=opener):
            result = gui.do_fetch([("limit", "1000"), ("sort", "pricing-low-to-high")],
                                  "test-secret", downloaded)
        self.assertEqual(result, self.raw)
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, gui.MODELS_URL + "?limit=1000&sort=pricing-low-to-high")
        self.assertEqual(req.get_header("Authorization"), "Bearer test-secret")
        self.assertNotIn("test-secret", downloaded.read_text())
        converted = gui.do_convert(downloaded, custom)
        official.write_text(json.dumps({"openrouter/vendor/model": {"old": True}, "official-only": {"mode": "chat"}}))
        before = official.read_bytes()
        _, _, output, conflicts = gui.do_merge(official, custom, merged)
        self.assertEqual(conflicts, ["openrouter/vendor/model"])
        self.assertEqual(list(output), ["openrouter/vendor/model", "official-only"])
        self.assertEqual(output["openrouter/vendor/model"], converted["openrouter/vendor/model"])
        self.assertEqual(gui.load_json(merged), output)
        self.assertEqual(before, official.read_bytes())

    def test_optional_auth_and_empty_result(self):
        opener = self.opener({"data": []})
        with patch.object(gui.request, "build_opener", return_value=opener):
            gui.do_fetch([], "", self.root / "empty.json")
        self.assertIsNone(opener.open.call_args.args[0].get_header("Authorization"))
        self.assertEqual(opener.open.call_args.args[0].full_url, gui.MODELS_URL)

    def test_failures_preserve_existing_output(self):
        output = self.root / "existing.json"
        original = '{"original": true}'
        for failure in [URLError("offline"), TimeoutError(), HTTPError(gui.MODELS_URL, 401, "bad", {}, None)]:
            output.write_text(original)
            with patch.object(gui.request, "build_opener", return_value=Mock(open=Mock(side_effect=failure))):
                with self.assertRaises(ValueError):
                    gui.do_fetch([], "", output, True)
            self.assertEqual(output.read_text(), original)
        for body in [b"not json", b'{"error": "bad"}', b'{"data": [{"id":"bad"}]}', b'{"data":[],"x":NaN}']:
            with patch.object(gui.request, "build_opener", return_value=Mock(open=Mock(return_value=io.BytesIO(body)))):
                with self.assertRaises(ValueError):
                    gui.do_fetch([], "", output, True)
            self.assertEqual(output.read_text(), original)

    def test_query_validation(self):
        for parameters in [[("limit", "0")], [("limit", "1.2")], [("x", "")],
                           [("x", "1"), ("x", "2")], [("api_key", "secret")]]:
            with self.assertRaises(ValueError):
                gui.build_models_url(parameters)
        self.assertEqual(gui.build_models_url([("q", "a & b")]), gui.MODELS_URL + "?q=a+%26+b")

    def test_documented_parameters(self):
        from openrouter_query_params import PARAMETERS, validate_parameters
        self.assertEqual(len(PARAMETERS), 29)
        for name, spec in PARAMETERS.items():
            value = spec[3] or "gpt-4"
            self.assertEqual(validate_parameters([(name, value)]), [(name, value)])
            if spec[2] == "enum":
                for option in spec[4]:
                    self.assertEqual(validate_parameters([(name, option)]), [(name, option)])
        for pair in [("limit", "1001"), ("offset", "-1"), ("context", "0"),
                     ("max_price", "NaN"), ("min_price", "-1"), ("min_age_days", "1.2"),
                     ("max_tool_success_rate", "1.01"), ("region", "global"),
                     ("zdr", "false"), ("input_modalities", "video"),
                     ("output_modalities", "all,text"), ("use_rss", "true"),
                     ("use_rss_chat_links", "true")]:
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                validate_parameters([pair])
        for suffix in ("price", "output_price", "age_days", "intelligence_index", "coding_index", "agentic_index", "tool_success_rate"):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                validate_parameters([("min_" + suffix, "1"), ("max_" + suffix, "0")])
        self.assertEqual(validate_parameters([("input_modalities", "text, image")]),
                         [("input_modalities", "text,image")])

    def test_real_snapshot_http_pipeline(self):
        source = gui.ROOT / "openrouter-models.json"
        official = gui.DEFAULT_OFFICIAL
        if not source.is_file() or not official.is_file():
            self.skipTest("Local OpenRouter and LiteLLM snapshots are required")
        source_bytes, official_bytes = source.read_bytes(), official.read_bytes()
        raw = gui.parse_json(source_bytes.decode("utf-8-sig"))
        self.assertIsInstance(raw, dict)
        self.assertIsInstance(raw["data"], list)
        self.assertTrue(raw["data"])
        downloaded, custom, merged = [self.root / name for name in
                                      ("real-download.json", "real-custom.json", "real-merged.json")]
        opener = Mock(open=Mock(return_value=io.BytesIO(source_bytes)))
        with patch.object(gui.request, "build_opener", return_value=opener):
            fetched = gui.do_fetch([("limit", "1000")], "test-secret", downloaded)
        opener.open.assert_called_once()
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 30)
        self.assertEqual(fetched, raw)
        self.assertEqual(gui.load_json(downloaded), raw)
        converted = gui.do_convert(downloaded, custom)
        expected = {"openrouter/" + m["id"]: gui.convert_model(m) for m in raw["data"]}
        self.assertEqual(converted, expected)
        self.assertEqual(gui.load_json(custom), expected)
        off, cus, result, conflicts = gui.do_merge(official, custom, merged)
        loaded = gui.load_json(merged)
        self.assertEqual(loaded, result)
        self.assertEqual(list(loaded), list(cus) + [key for key in off if key not in cus])
        self.assertEqual(len(loaded), len(off) + len(cus) - len(conflicts))
        for key, spec in cus.items():
            self.assertEqual(loaded[key], spec)
        for key, spec in off.items():
            if key not in cus:
                self.assertEqual(loaded[key], spec)
        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertEqual(official.read_bytes(), official_bytes)
        print("\nReal snapshot HTTP mock: top-level keys=", list(raw),
              "; model keys=", list(raw["data"][0]),
              "; fetched/converted/official/conflicts/merged=",
              (len(raw["data"]), len(converted), len(off), len(conflicts), len(loaded)))

    def test_documented_response_shapes(self):
        # The documentation example omits pagination fields even though its
        # schema lists them; both the minimal example and full response work.
        for payload in [{"data": self.raw["data"]},
                        {**self.raw, "links": {"next": None}}]:
            output = self.root / "shape.json"
            with patch.object(gui.request, "build_opener", return_value=self.opener(payload)):
                result = gui.do_fetch([], "test-secret", output, overwrite=True)
            self.assertEqual(result, payload)
            self.assertEqual(gui.load_json(output), payload)

    def test_invalid_response_shapes_do_not_create_output(self):
        for payload in [{"models": []}, {"data": {}}, [],
                        {"data": [{"id": "x", "pricing": {}, "architecture": None}]}]:
            output = self.root / "invalid.json"
            opener = self.opener(payload)
            with patch.object(gui.request, "build_opener", return_value=opener):
                with self.assertRaises(ValueError):
                    gui.do_fetch([], "test-secret", output)
            opener.open.assert_called_once()
            self.assertFalse(output.exists())

    def test_redirect_refused(self):
        self.assertIsNone(gui.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com"))


if __name__ == "__main__":
    unittest.main()
