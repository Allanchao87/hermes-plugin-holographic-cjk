from __future__ import annotations

import sqlite3

import pytest

from holographic_zh.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    value = MemoryStore(tmp_path / "facts.db")
    yield value
    value.close()


def test_tags_only_update_rebuilds_search_text_without_crashing(store):
    fact_id = store.add_fact("供应商账期为月结", tags="旧标签")

    assert store.update_fact(fact_id, tags="新标签") is True
    row = store._one("SELECT content, tags, search_text FROM facts WHERE fact_id = ?", (fact_id,))
    assert row["content"] == "供应商账期为月结"
    assert row["tags"] == "新标签"
    assert "供应" in row["search_text"]
    assert "新标" in row["search_text"]


def test_content_only_update_preserves_tags_in_search_text(store):
    fact_id = store.add_fact("旧内容", tags="供应商 账期")

    assert store.update_fact(fact_id, content="新内容") is True
    row = store._one("SELECT content, tags, search_text FROM facts WHERE fact_id = ?", (fact_id,))
    assert row["content"] == "新内容"
    assert row["tags"] == "供应商 账期"
    assert "供应" in row["search_text"]
    assert "账期" in row["search_text"]
    assert "新内" in row["search_text"]


def test_update_preserves_other_fields_when_changing_trust_only(store):
    fact_id = store.add_fact("原始内容", tags="标签", category="tool")

    assert store.update_fact(fact_id, trust_delta=0.05) is True
    row = store._one("SELECT content, tags, category, search_text FROM facts WHERE fact_id = ?", (fact_id,))
    assert tuple(row) == ("原始内容", "标签", "tool", row["search_text"])


def test_updated_fact_remains_retrievable_by_original_tag(store):
    from holographic_zh.retrieval import FactRetriever

    fact_id = store.add_fact("旧内容", tags="供应商 账期")
    store.update_fact(fact_id, content="新内容")
    hits = FactRetriever(store=store).search("账期", limit=5)
    assert any(row["fact_id"] == fact_id for row in hits)
