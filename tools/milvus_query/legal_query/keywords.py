"""保守的BM25查询清理：规则可审查，不调用模型、不修改用户原问题。"""

import re

# 删除完整问句套话，不把“需要/注意/不/未”等单词当停用词。
TAIL = re.compile(
    r"(?:有什么需要注意的|有哪些需要注意的|需要注意什么|应注意什么|有什么规定|有哪些规定)[？?。！!\s]*$"
)
PREFIX = re.compile(r"^(?:请问|麻烦问一下)[，,：:\s]*")
RENTAL = re.compile(r"(?<![公廉出承转合])租房(?!屋)")


def bm25_query(question: str) -> str:
    """保留否定、金额、期限、条号和引用；无有效剩余内容时回用原文。"""
    original = question.strip()
    # 引号可能表示需要精确查找的原文，整句保持原样，避免改写引用。
    if any(mark in original for mark in ('"', "'", "“", "”", "‘", "’", "《", "》")):
        return original
    cleaned = PREFIX.sub("", original)
    without_tail = TAIL.sub("", cleaned)
    if without_tail != cleaned:
        # 只在识别出上面的口语尾句后处理“的时候”，以空格避免拼词。
        cleaned = without_tail.replace("的时候", " ").strip(" ，,：:；;\t\n")
    if cleaned != original:
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return original
    # 保留口语原词并补法律文书常见表述；不扩展公租房等不同概念。
    if RENTAL.search(cleaned) and "住房租赁" not in cleaned:
        cleaned += " 住房租赁"
    return cleaned
