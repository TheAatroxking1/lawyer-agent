from dataclasses import replace

import pytest

from lawyer_agent.application.legal_chunk_structure import derive_hierarchical_chunks
from lawyer_agent.application.legal_corpus_import import _derive_provisions
from lawyer_agent.application.legal_index_chunks import select_index_leaves
from lawyer_agent.domain.common import new_uuid7
from tests.unit.test_non_article_import import prepare


@pytest.mark.parametrize("paragraphs,mode", [
    (["第一条 " + "正文内容。" * 170, "附段。"], "articles"),
    (["重复", "内容。" * 230, "重复"], "non_article_document"),
])
def test_precise_leaf_spans_cover_every_character(tmp_path, paragraphs, mode):
    prepared = prepare(tmp_path, paragraphs, mode)
    version_id = new_uuid7()
    provisions = _derive_provisions(version_id, prepared.command.provisions)
    chunks = derive_hierarchical_chunks(version_id=version_id, provisions=provisions,
        articles=prepared.articles, parser_version="test/hierarchical-v2")
    text = provisions[0].full_text
    covered = set()
    for leaf in select_index_leaves(chunks):
        start, end = leaf.parent_relative_char_start, leaf.parent_relative_char_end
        assert text[start:end] == leaf.content
        assert len(leaf.content) <= 600
        covered.update(range(start, end))
    assert all(i in covered or char.isspace() for i, char in enumerate(text))


@pytest.mark.parametrize("start,end", [(None, 2), (0, None), (-1, 2), (2, 2), (True, 2)])
def test_chunk_position_pair_is_strict(start, end):
    from tests.unit.test_legal_dataset_quality import fixture
    with pytest.raises(ValueError):
        replace(fixture().chunks[0], parent_relative_char_start=start, parent_relative_char_end=end)
