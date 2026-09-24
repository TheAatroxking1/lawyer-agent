"""查询侧清理只影响BM25，尤其防止损失法律否定词、法名和期限。"""

import unittest
from unittest.mock import Mock

from legal_query.keywords import bm25_query
from legal_query.search import SearchOptions, search


class KeywordTests(unittest.TestCase):
    def test_rental_filler_and_expansion(self):
        self.assertEqual(bm25_query("北京租房的时候，有什么需要注意的"), "北京租房 住房租赁")

    def test_preserve_negation_numbers_and_substantive_words(self):
        for question in (
            "未签劳动合同超过一年，不得要求双倍工资吗？",
            "民法典第一千一百九十八条安全保障义务与注意义务",
            "收到处罚决定后60日内能否申请行政复议，罚款5000元",
            "不需要审批的建设项目如何备案？",
            "未成年人能不能签合同？",
        ):
            self.assertEqual(bm25_query(question), question)

    def test_preserve_quoted_phrases_and_compound_housing_terms(self):
        for question in (
            "解释“有什么需要注意的”",
            '查询"租房的时候"',
            "公租房和廉租房的条件",
            "出租房安全",
        ):
            self.assertEqual(bm25_query(question), question)

    def test_only_polite_prefix_and_complete_suffix(self):
        self.assertEqual(
            bm25_query("请问，用人单位违法解除劳动合同，有什么规定？"), "用人单位违法解除劳动合同"
        )
        self.assertEqual(bm25_query("需要注意义务的认定标准"), "需要注意义务的认定标准")
        self.assertEqual(bm25_query("请问"), "请问")

    def test_no_duplicate_expansion(self):
        self.assertEqual(bm25_query("租房 住房租赁"), "租房 住房租赁")

    def test_two_routes_and_raw_control(self):
        q = "北京租房的时候，有什么需要注意的"
        for raw in (False, True):
            client = Mock()
            client.hybrid_search.return_value = [[]]
            embed = Mock(return_value=[0.01] * 1024)
            result = search(client, q, SearchOptions(raw_bm25=raw), embed)
            embed.assert_called_once_with(q)
            reqs = client.hybrid_search.call_args.kwargs["reqs"]
            expected = q if raw else "北京租房 住房租赁"
            self.assertEqual(reqs[1].data, [expected])
            self.assertEqual(reqs[0].expr, reqs[1].expr)
            self.assertEqual(result["bm25_query"], expected)

    def test_pure_bm25_uses_same_cleanup_without_embedding(self):
        client, embed = Mock(), Mock()
        client.search.return_value = [[]]
        search(client, "北京租房的时候，有什么需要注意的", SearchOptions(mode="bm25"), embed)
        self.assertEqual(client.search.call_args.kwargs["data"], ["北京租房 住房租赁"])
        embed.assert_not_called()
