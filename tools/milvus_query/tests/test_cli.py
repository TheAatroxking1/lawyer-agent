"""CLI资源关闭失败也必须保持错误脱敏。"""

import contextlib
import io
import unittest
from unittest.mock import Mock, patch

from legal_query.__main__ import main


class CliTests(unittest.TestCase):
    def test_argument_error_is_json_without_input(self):
        error_output = io.StringIO()
        with (
            patch("sys.argv", ["legal_query", "bm25", "问题", "--top-k", "synthetic-private"]),
            contextlib.redirect_stderr(error_output),
            self.assertRaises(SystemExit) as stopped,
        ):
            main()
        self.assertEqual(stopped.exception.code, 2)
        self.assertNotIn("synthetic-private", error_output.getvalue())
        self.assertIn('"code": "invalid_arguments"', error_output.getvalue())

    def test_close_failure_redacted(self):
        client = Mock()
        client.query.return_value = [{"count(*)": 10}]
        client.close.side_effect = RuntimeError("synthetic-secret")
        error_output = io.StringIO()
        with (
            patch("sys.argv", ["legal_query", "status"]),
            patch("legal_query.__main__.MilvusClient", return_value=client),
            patch("legal_query.__main__.preflight", return_value={}),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(error_output),
        ):
            self.assertEqual(main(), 3)
        self.assertNotIn("synthetic-secret", error_output.getvalue())
        self.assertIn("client_close_failed", error_output.getvalue())
