"""Explicit ttl_days plumbing + supersede bank hygiene.

Covers the two gaps found in the 2026-11-17 external review:
1. the explicit-override layer of the decay priority had no write path
   (add_fact / update_fact / tool schema all lacked ttl_days);
2. supersede() left the retired fact's vector inside its category bank, and
   _rebuild_bank() itself re-bundled superseded rows, so probe ranking kept
   steering by retired snapshots.
"""
from __future__ import annotations

import pytest

from holographic_zh.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(db_path=tmp_path / "facts.db")
    yield s
    s.close()


def test_add_fact_stores_explicit_ttl(store):
    fid = store.add_fact("短线行情", tags="行情", ttl_days=7)
    row = store._one("SELECT ttl_days FROM facts WHERE fact_id = ?", (fid,))
    assert row["ttl_days"] == 7


def test_add_fact_without_ttl_stores_null(store):
    fid = store.add_fact("普通事实", tags="普通")
    assert store._one("SELECT ttl_days FROM facts WHERE fact_id = ?", (fid,))["ttl_days"] is None


def test_update_sets_and_clears_ttl(store):
    fid = store.add_fact("状态事实", tags="状态 T3")
    assert store.update_fact(fid, ttl_days=3) is True
    assert store._one("SELECT ttl_days FROM facts WHERE fact_id = ?", (fid,))["ttl_days"] == 3
    # 0 clears the override (falls back to the tag-derived level) — must survive the None-filter
    assert store.update_fact(fid, ttl_days=0) is True
    assert store._one("SELECT ttl_days FROM facts WHERE fact_id = ?", (fid,))["ttl_days"] == 0
    # omitting ttl_days leaves it untouched
    assert store.update_fact(fid, tags="状态 T3 改") is True
    assert store._one("SELECT ttl_days FROM facts WHERE fact_id = ?", (fid,))["ttl_days"] == 0


def test_explicit_ttl_beats_tag_level(store):
    # Same age, same tag level: the explicit 7-day fact must decay harder than the 60-day one.
    explicit = store.add_fact("澳弘价格快照（显式 7 天）", tags="澳弘 价格 T3", ttl_days=7)
    tagged = store.add_fact("澳弘价格快照（标签 60 天）", tags="澳弘 价格 T3")
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")
    for fid in (explicit, tagged):
        store._conn.execute("UPDATE facts SET updated_at = ? WHERE fact_id = ?", (ts, fid))
    store._conn.commit()
    from holographic_zh.retrieval import FactRetriever
    r = FactRetriever(store=store, ttl_map={"T3": 60})
    assert r._fact_ttl({"ttl_days": 7, "tags": "T3", "retrieval_count": 0}) == 7
    assert r._fact_ttl({"ttl_days": None, "tags": "T3", "retrieval_count": 0}) == 60


def test_supersede_shrinks_category_bank(store):
    """A superseded fact must leave its category bank (probe ranks against the bundle)."""
    old = store.add_fact("生益供应状态（旧口径）", category="project", tags="生益 供应 T3")
    new = store.add_fact("生益供应状态（新口径）", category="project", tags="生益 供应 T3")
    if not store._hrr_available:
        pytest.skip("numpy unavailable — bank is not built")
    before = store._one("SELECT fact_count FROM memory_banks WHERE bank_name = 'cat:project'")
    assert before and before["fact_count"] == 2
    store.supersede(old, new, reason="新口径取代")
    after = store._one("SELECT fact_count FROM memory_banks WHERE bank_name = 'cat:project'")
    assert after and after["fact_count"] == 1


def test_rebuild_bank_excludes_superseded_after_later_adds(store):
    """Even a later add_fact rebuild must not re-bundle the retired row."""
    old = store.add_fact("暖通供方格局（旧）", category="project", tags="暖通 格局 T2")
    new = store.add_fact("暖通供方格局（新）", category="project", tags="暖通 格局 T2")
    if not store._hrr_available:
        pytest.skip("numpy unavailable — bank is not built")
    store.supersede(old, new, reason="格局更新")
    store.add_fact("暖通另一条事实", category="project", tags="暖通 其他 T2")  # triggers a rebuild
    row = store._one("SELECT fact_count FROM memory_banks WHERE bank_name = 'cat:project'")
    assert row["fact_count"] == 2  # new + the extra one, NOT the retired old
