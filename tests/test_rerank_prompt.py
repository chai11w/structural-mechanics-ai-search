import base64
import io
import json
import search
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops


class RerankPromptTest(unittest.TestCase):
    TEST_TEMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp_tests"

    @staticmethod
    def _decode_data_url(data_url):
        header, encoded = data_url.split(",", 1)
        return header, base64.b64decode(encoded)

    def test_rerank_encoder_keeps_default_orientation_bytes(self):
        self.TEST_TEMP_ROOT.mkdir(exist_ok=True)
        path = self.TEST_TEMP_ROOT / "rerank-orientation-plain.jpg"
        try:
            Image.new("RGB", (7, 5), "white").save(path, format="JPEG")

            self.assertEqual(
                search.encode_rerank_image_base64(path),
                search.encode_image_base64(path),
            )
        finally:
            path.unlink(missing_ok=True)

    def test_rerank_encoder_normalizes_all_exif_orientations(self):
        canonical = Image.new("RGB", (11, 7), "white")
        for x in range(4):
            for y in range(3):
                canonical.putpixel((x, y), (220, 20, 20))

        inverse_transpose = {
            2: Image.Transpose.FLIP_LEFT_RIGHT,
            3: Image.Transpose.ROTATE_180,
            4: Image.Transpose.FLIP_TOP_BOTTOM,
            5: Image.Transpose.TRANSPOSE,
            6: Image.Transpose.ROTATE_90,
            7: Image.Transpose.TRANSVERSE,
            8: Image.Transpose.ROTATE_270,
        }

        paths = []
        try:
            for orientation, inverse in inverse_transpose.items():
                with self.subTest(orientation=orientation):
                    path = self.TEST_TEMP_ROOT / f"rerank-orientation-{orientation}.png"
                    paths.append(path)
                    stored = canonical.transpose(inverse)
                    exif = Image.Exif()
                    exif[274] = orientation
                    stored.save(path, format="PNG", exif=exif)

                    header, payload = self._decode_data_url(
                        search.encode_rerank_image_base64(path)
                    )
                    with Image.open(io.BytesIO(payload)) as decoded:
                        decoded.load()
                        self.assertEqual(header, "data:image/png;base64")
                        self.assertEqual(decoded.size, canonical.size)
                        self.assertIsNone(decoded.getexif().get(274))
                        self.assertIsNone(ImageChops.difference(decoded, canonical).getbbox())
        finally:
            for path in paths:
                path.unlink(missing_ok=True)

    def test_score_candidate_pair_uses_orientation_aware_encoder(self):
        response = type(
            "Response",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {"message": type("Message", (), {"content": '{"score":0.95,"reason":"一致"}'})()},
                    )()
                ]
            },
        )()
        completions = type("Completions", (), {"create": lambda self, **kwargs: response})()
        client = type(
            "Client",
            (),
            {"chat": type("Chat", (), {"completions": completions})()},
        )()

        with patch(
            "search.encode_rerank_image_base64",
            side_effect=["data:image/png;base64,query", "data:image/jpeg;base64,candidate"],
        ) as encoder:
            score, reason = search.score_candidate_pair(client, "query.png", "candidate.jpg")

        self.assertEqual((score, reason), (0.95, "一致"))
        self.assertEqual(
            [call.args[0] for call in encoder.call_args_list],
            ["query.png", "candidate.jpg"],
        )

    def test_default_rerank_prompt_is_shape_only(self):
        self.assertEqual(search.RERANK_PROMPT, search.SHAPE_RERANK_PROMPT)
        self.assertIn("只看主杆件骨架", search.RERANK_PROMPT)
        self.assertIn("忽略荷载", search.RERANK_PROMPT)
        self.assertIn("支座符号细节", search.RERANK_PROMPT)
        self.assertNotIn("荷载位置和方向", search.RERANK_PROMPT)

    def test_qwen_v1_prompt_version_reuses_current_prompt(self):
        self.assertEqual(search._load_qwen_rerank_prompt("v1"), search.RERANK_PROMPT)

    def test_qwen_v4_prompt_version_is_load_aware(self):
        prompt = search._load_qwen_rerank_prompt("v4")
        self.assertIn("荷载的类型、作用位置、方向和分布范围", prompt)

    def test_legacy_rerank_prompt_kept_for_comparison(self):
        self.assertNotEqual(search.LEGACY_RERANK_PROMPT, search.SHAPE_RERANK_PROMPT)
        self.assertIn("荷载位置和方向", search.LEGACY_RERANK_PROMPT)

    def test_final_rerank_score_keeps_load_and_shape_blend(self):
        self.assertEqual(search.compute_final_rerank_score(1.0, 0.2), 0.6)
        self.assertEqual(search.compute_final_rerank_score(0.1, 0.95), 0.525)
        self.assertEqual(search.compute_final_rerank_score(0.5, 2.0), 0.75)

    def test_qwen_rerank_adapter_parses_json_and_tracks_request_shape(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "id": "qwen-test",
                        "choices": [
                            {"message": {"content": '{"score":0.83,"reason":"结构和荷载对应"}'}},
                        ],
                        "usage": {"input_tokens": 12, "output_tokens": 4},
                    }
                ).encode("utf-8")

        with (
            patch("search.DASHSCOPE_API_KEY", "test-key"),
            patch("search.encode_rerank_image_base64", side_effect=["data:query", "data:candidate"]),
            patch("search.urllib.request.urlopen", return_value=Response()) as urlopen,
        ):
            score, reason = search.score_candidate_pair(
                None,
                "query.jpg",
                "candidate.jpg",
                provider="qwen",
                model="qwen3.7-plus",
                timeout_seconds=2,
            )

        self.assertAlmostEqual(score, 0.83)
        self.assertEqual(reason, "结构和荷载对应")
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "qwen3.7-plus")
        self.assertFalse(payload["enable_thinking"])
        self.assertEqual(payload["messages"][1]["content"][2]["image_url"]["url"], "data:query")

    def test_concurrent_rerank_matches_serial_scoring_order(self):
        query = "query.jpg"
        candidates = [
            {"rank": 1, "path": "a.jpg", "name": "a.jpg", "score": 0.8},
            {"rank": 2, "path": "b.jpg", "name": "b.jpg", "score": 0.7},
            {"rank": 3, "path": "c.jpg", "name": "c.jpg", "score": 0.6},
        ]
        vision_scores = {"a.jpg": 0.2, "b.jpg": 0.9, "c.jpg": 0.4}

        def fake_score(
            client,
            query_image_path,
            candidate_path,
            prompt=search.RERANK_PROMPT,
            timeout_seconds=None,
            model=search.DEFAULT_ZHIPU_RERANK_MODEL,
            provider=None,
            endpoint=None,
            enable_thinking=None,
        ):
            del client, query_image_path, prompt, timeout_seconds, model, provider, endpoint, enable_thinking
            name = Path(candidate_path).name
            return vision_scores[name], name

        with (
            patch("search.prepare_rerank_candidates", return_value=candidates),
            patch("search.ZhipuAI", return_value=object()),
            patch("search.score_candidate_pair", side_effect=fake_score),
        ):
            serial = search.rerank_candidates(query, candidates, top_n=3)
            concurrent = search.rerank_candidates_concurrent(query, candidates, top_n=3, max_workers=3)

        self.assertEqual([item["path"] for item in concurrent], [item["path"] for item in serial])
        self.assertEqual([item["final_score"] for item in concurrent], [item["final_score"] for item in serial])

    def test_timeout_candidate_keeps_coarse_score_and_status(self):
        candidate = {"rank": 1, "path": "slow.jpg", "score": 0.9}

        with patch("search.score_candidate_pair", side_effect=TimeoutError("request timeout")):
            result = search.score_rerank_candidate(
                "query.jpg",
                candidate,
                client=object(),
                timeout_seconds=2,
            )

        self.assertIsNone(result["rerank_score"])
        self.assertEqual(result["final_score"], 0.9)
        self.assertEqual(result["rerank_status"], "timeout")

    def test_timeout_candidate_is_retried_and_ranked_when_retry_succeeds(self):
        candidates = [{"rank": 1, "path": "slow.jpg", "name": "slow.jpg", "score": 0.8}]
        responses = [TimeoutError("Request timed out."), (0.9, "补评完成")]

        with (
            patch("search.prepare_rerank_candidates", return_value=candidates),
            patch("search.ZhipuAI", return_value=object()),
            patch("search.score_candidate_pair", side_effect=responses),
        ):
            results = search.rerank_candidates_concurrent(
                "query.jpg",
                candidates,
                max_workers=1,
                candidate_timeout_seconds=1,
                retry_timeout_seconds=2,
                retry_max_candidates=1,
            )

        self.assertTrue(search.rerank_results_complete(results))
        self.assertEqual(results[0]["rerank_status"], "retried")
        self.assertEqual(results[0]["rerank_attempts"], 2)
        self.assertAlmostEqual(results[0]["final_score"], 0.85)

    def test_failed_candidate_is_retried_and_ranked_when_retry_succeeds(self):
        candidates = [{"rank": 1, "path": "limited.jpg", "name": "limited.jpg", "score": 0.8}]
        responses = [RuntimeError("API reach limit"), (0.9, "补评完成")]

        with (
            patch("search.prepare_rerank_candidates", return_value=candidates),
            patch("search.ZhipuAI", return_value=object()),
            patch("search.score_candidate_pair", side_effect=responses),
        ):
            results = search.rerank_candidates_concurrent(
                "query.jpg",
                candidates,
                max_workers=1,
                candidate_timeout_seconds=1,
                retry_timeout_seconds=2,
                retry_max_candidates=1,
                retry_failed_candidates=True,
            )

        self.assertTrue(search.rerank_results_complete(results))
        self.assertEqual(results[0]["rerank_status"], "retried")
        self.assertEqual(results[0]["rerank_attempts"], 2)
        self.assertAlmostEqual(results[0]["final_score"], 0.85)

    def test_unfinished_retry_returns_marked_coarse_fallback(self):
        candidates = [{"rank": 1, "path": "slow.jpg", "name": "slow.jpg", "score": 0.8}]

        with (
            patch("search.prepare_rerank_candidates", return_value=candidates),
            patch("search.ZhipuAI", return_value=object()),
            patch("search.score_candidate_pair", side_effect=TimeoutError("Request timed out.")),
        ):
            results = search.rerank_candidates_concurrent(
                "query.jpg",
                candidates,
                max_workers=1,
                candidate_timeout_seconds=1,
                retry_timeout_seconds=2,
                retry_max_candidates=1,
            )

        self.assertFalse(search.rerank_results_complete(results))
        self.assertEqual(results[0]["rerank_status"], "incomplete")
        self.assertNotIn("final_score", results[0])

    def test_default_rerank_uses_shared_concurrency_policy(self):
        with patch("search.rerank_candidates_concurrent", return_value=[]) as concurrent:
            search.rerank_candidates("query.jpg", [{"rank": 1, "path": "a.jpg", "score": 1.0}])

        self.assertEqual(concurrent.call_args.kwargs["max_workers"], search.RERANK_CONCURRENT_MAX_WORKERS)
        self.assertEqual(concurrent.call_args.kwargs["candidate_timeout_seconds"], search.RERANK_PRIMARY_TIMEOUT_SECONDS)
        self.assertEqual(concurrent.call_args.kwargs["retry_timeout_seconds"], search.RERANK_RETRY_TIMEOUT_SECONDS)
