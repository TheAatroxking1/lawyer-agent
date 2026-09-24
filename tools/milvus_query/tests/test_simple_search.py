"""单文件示例的真实SDK请求契约，不连接数据库。"""

import unittest
from unittest.mock import Mock

from pymilvus import RRFRanker
from simple_search import compare


class SimpleSearchTests(unittest.TestCase):
    def test_same_inputs_for_three_queries(self):
        client = Mock()
        dense = [{"id": "dense", "distance": 0.8, "entity": {"text": "稠密正文"}}]
        bm25 = [{"id": "bm25", "distance": 5.0, "entity": {"text": "关键词正文"}}]
        client.search.side_effect = [[dense], [bm25]]
        client.hybrid_search.return_value = [[dense[0], bm25[0]]]
        vector = [0.01] * 1024
        result = compare(client, "北京租房", vector)
        self.assertEqual(result["dense"], dense)
        self.assertEqual(result["bm25"], bm25)
        self.assertEqual(len(result["rrf"]), 2)
        direct = [call.kwargs for call in client.search.call_args_list]
        hybrid = client.hybrid_search.call_args.kwargs
        requests = hybrid["reqs"]
        self.assertEqual(direct[0]["data"], requests[0].data)
        self.assertEqual(direct[1]["data"], requests[1].data)
        for i in range(2):
            self.assertEqual(direct[i]["filter"], requests[i].expr)
            self.assertEqual(direct[i]["limit"], requests[i].limit)
            self.assertEqual(direct[i]["anns_field"], requests[i].anns_field)
        self.assertIsInstance(hybrid["ranker"], RRFRanker)
        self.assertEqual(hybrid["ranker"].dict(), {"strategy": "rrf", "params": {"k": 60}})
        self.assertEqual(hybrid["limit"], 50)
