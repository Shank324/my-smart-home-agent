"""检索：分词、BM25、RRF 融合的排序行为。

用真实的 seed_data 子集索引一遍，避免用假数据测出「假正确」。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.services.agent.knowledge.retriever import KnowledgeRetriever

SEED = Path(__file__).resolve().parents[1] / "seed_data"
SUBSET = ("light_pro.md", "troubleshooting.md", "tv_s1.md")


@pytest.fixture(scope="module")
def retriever(tmp_path_factory) -> KnowledgeRetriever:
    data_dir = tmp_path_factory.mktemp("corpus")
    for name in SUBSET:
        shutil.copy(SEED / name, data_dir / name)
    instance = KnowledgeRetriever(data_dir=data_dir, store_path=data_dir / "index.json",
                                  rewriter=None)
    instance.rebuild()
    return instance


def test_tokenizer_keeps_chinese_bigrams():
    tokens = KnowledgeRetriever._tokenize("灯光闪烁")
    assert "灯" in tokens and "闪" in tokens
    assert "闪烁" in tokens          # 二元组保证「闪」能匹配「闪烁」
    assert "的" not in tokens


def test_tokenizer_handles_english_and_digits():
    tokens = KnowledgeRetriever._tokenize("HDMI1 接口")
    assert "hdmi1" in tokens


def test_rebuild_indexes_subset(retriever):
    assert len(retriever.chunks) > 0
    assert {chunk.source for chunk in retriever.chunks} == set(SUBSET)


def test_search_finds_flicker_section(retriever):
    results = retriever.search("灯一直闪", top_k=3, expand=False)
    assert results
    assert results[0].source_doc == "light_pro.md"
    assert "闪" in results[0].section


def test_search_prefers_offline_section_over_flicker(retriever):
    results = retriever.search("设备显示离线怎么办", top_k=3, expand=False)
    assert results
    assert "离线" in results[0].section


def test_search_returns_empty_when_nothing_matches(retriever):
    # 完全没有词面重合时不应硬凑结果：早期版本会返回任意块，看起来像乱答
    assert retriever.search("zzz qqq", top_k=3, expand=False) == []


def test_ensure_ready_reuses_index(retriever):
    """冷启动复用磁盘索引：不重复 embedding，块数保持一致。"""
    fresh = KnowledgeRetriever(data_dir=retriever.data_dir,
                               store_path=retriever.store.fallback_path, rewriter=None)
    fresh.ensure_ready()
    assert len(fresh.chunks) == len(retriever.chunks)


def test_corpus_change_invalidates_index(retriever, tmp_path):
    """语料变了指纹就变，索引必须重建。"""
    data_dir = tmp_path / "corpus2"
    data_dir.mkdir()
    for name in SUBSET:
        shutil.copy(SEED / name, data_dir / name)
    instance = KnowledgeRetriever(data_dir=data_dir, store_path=tmp_path / "i.json", rewriter=None)
    instance.rebuild()
    fingerprint = instance.fingerprint()

    (data_dir / "light_pro.md").write_text(
        (data_dir / "light_pro.md").read_text(encoding="utf-8") + "\n\n## 新增章节\n\n测试内容。\n",
        encoding="utf-8")
    assert instance.fingerprint() != fingerprint
