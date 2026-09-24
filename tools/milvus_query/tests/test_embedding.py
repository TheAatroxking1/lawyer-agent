"""只模拟HTTP响应，不使用真实密钥。"""

import io
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

from legal_query.config import EmbeddingConfig, NoRedirect, QueryError


class EmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.config = EmbeddingConfig("https://example.test/v1", "synthetic-key")

    def test_secret_not_in_repr(self):
        self.assertNotIn("synthetic-key", repr(self.config))

    def test_usage_is_preserved_without_inventing_missing_tokens(self):
        for usage in ({"total_tokens": 12}, None):
            body = {
                "model": self.config.model,
                "data": [{"index": 0, "embedding": [0.1] * 1024}],
                "usage": usage,
                "id": "synthetic-request",
            }
            opener = Mock()
            opener.open.return_value = io.BytesIO(json.dumps(body).encode())
            with patch("urllib.request.build_opener", return_value=opener):
                vector, info = self.config.embed_with_usage("合成问题")
            self.assertEqual(len(vector), 1024)
            self.assertEqual(info["usage"], usage)
            self.assertEqual(info["request_id"], "synthetic-request")

    def test_invalid_model_and_endpoint(self):
        configs = [
            EmbeddingConfig("http://example.test", "test"),
            EmbeddingConfig("https://user:pass@example.test", "test"),
            EmbeddingConfig("https://example.test", "test", model="wrong"),
            EmbeddingConfig("https://example.test", "test", dimensions=2048),
        ]
        for config in configs:
            with self.subTest(config=config), self.assertRaises(QueryError):
                config.validate()

    def test_no_redirect(self):
        self.assertIsNone(
            NoRedirect().redirect_request(None, None, None, None, None, None)
        )

    def test_api_sends_only_one_query_with_matching_identity(self):
        body = {
            "model": self.config.model,
            "data": [{"index": 0, "embedding": [0.1] * 1024}],
        }
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(body).encode())
        with patch("urllib.request.build_opener", return_value=opener):
            self.assertEqual(len(self.config.embed("合成问题")), 1024)
        request = opener.open.call_args.args[0]
        sent = json.loads(request.data)
        self.assertEqual(sent["input"], ["合成问题"])
        self.assertEqual(sent["model"], self.config.model)
        self.assertEqual(sent["dimensions"], 1024)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 30)

    def test_denied_response_is_redacted(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            "https://example.test", 403, "synthetic-key", {}, None
        )
        with (
            patch("urllib.request.build_opener", return_value=opener),
            self.assertRaisesRegex(QueryError, "^embedding_access_denied$"),
        ):
            self.config.embed("问题")

    def test_response_model_and_shape_checked(self):
        for body in (
            {"model": "other", "data": []},
            {
                "model": self.config.model,
                "data": [{"index": 1, "embedding": [1] * 1024}],
            },
            {"model": self.config.model, "data": [{"index": 0, "embedding": [1] * 3}]},
        ):
            opener = Mock()
            opener.open.return_value = io.BytesIO(json.dumps(body).encode())
            with (
                patch("urllib.request.build_opener", return_value=opener),
                self.assertRaises(QueryError),
            ):
                self.config.embed("问题")


if __name__ == "__main__":
    unittest.main()
