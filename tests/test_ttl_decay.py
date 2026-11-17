"""Per-fact temporal decay (P1). Behavior contracts:

- a stale T3 fact ranks below its fresh counterpart on the same topic
- T0/T1 (timeless levels) never decay, however old
- facts without a T-level tag keep the legacy behavior: no decay unless the global
  temporal_decay_half_life fallback is set (backward compatibility)
- retrieval_count reinforcement stretches the effective half-life (frequently
  recalled facts fade slower)
- an explicit facts.ttl_days overrides the tag-derived half-life
- decay only reranks, never drops a fact from results; unparseable/future
  timestamps score 1.0
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from holographic_zh.retrieval import FactRetriever
from holographic_zh.store import MemoryStore

TTL_MAP = {"T0": 0, "T1": 0, "T2": 180, "T3": 60}


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(db_path=tmp_path / "facts.db")
    yield s
    s.close()


def _age_to(store, fact_id: int, days: int) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    store._conn.execute("UPDATE facts SET updated_at = ? WHERE fact_id = ?", (ts, fact_id))
    store._conn.commit()


def _set(store, fact_id: int, **cols) -> None:
    sets = ", ".join(f"{k} = ?" for k in cols)
    store._conn.execute(f"UPDATE facts SET {sets} WHERE fact_id = ?", (*cols.values(), fact_id))
    store._conn.commit()


def test_stale_t3_ranks_below_fresh_snapshot(store):
    old_id = store.add_fact("澳弘价格快照（2026-08）：双面板 598 元/㎡", tags="澳弘 价格 T3")
    new_id = store.add_fact("澳弘价格快照（2026-11）：双面板 720 元/㎡", tags="澳弘 价格 T3")
    _age_to(store, old_id, 120)  # 2 half-lives at T3=60d -> 0.25x
    _age_to(store, new_id, 1)
    r = FactRetriever(store=store, ttl_map=TTL_MAP)
    results = r.search("澳弘 价格", limit=5)
    assert [f["fact_id"] for f in results[:2]] == [new_id, old_id]


def test_timeless_levels_never_decay(store):
    fid = store.add_fact("金顺(P160)合作历史：2019 年起的老供应商", tags="金顺 历史 T0")
    _age_to(store, fid, 3650)  # 10 years
    r = FactRetriever(store=store, ttl_map=TTL_MAP)
    results = r.search("金顺 历史", limit=5)
    assert results and results[0]["fact_id"] == fid
    # No-decay baseline: same fact, same query, equal score regardless of map presence
    r_off = FactRetriever(store=store)  # no ttl_map, no global decay
    assert results[0]["score"] == pytest.approx(r_off.search("金顺 历史", limit=5)[0]["score"])


def test_untagged_fact_keeps_legacy_no_decay(store):
    """Default deployment (empty ttl_map, global half-life 0): behavior unchanged."""
    fid = store.add_fact("账期口径：月结 30 天", tags="账期 商务")
    _age_to(store, fid, 2000)
    r = FactRetriever(store=store, ttl_map=TTL_MAP)  # map set, but fact has no T-level tag
    results = r.search("账期", limit=5)
    assert results and results[0]["fact_id"] == fid


def test_global_half_life_fallback_still_applies(store):
    fid = store.add_fact("账期口径：月结 30 天", tags="账期 商务")  # no T-level tag
    _age_to(store, fid, 180)
    r = FactRetriever(store=store, temporal_decay_half_life=90)  # legacy global knob
    results = r.search("账期", limit=5)
    assert results and results[0]["fact_id"] == fid
    assert results[0]["score"] < 0.5 * results[0]["trust_score"] + 1e-9  # decayed below raw trust*relevance


def test_reinforcement_slows_decay(store):
    cold_id = store.add_fact("CCL 紧张口径（冷门快照）", tags="CCL 口径 T3")
    hot_id = store.add_fact("CCL 紧张口径（常用快照）", tags="CCL 口径 T3")
    _age_to(store, cold_id, 120)
    _age_to(store, hot_id, 120)
    _set(store, hot_id, retrieval_count=20)  # capped at 4 -> half-life 60 -> 300d
    r = FactRetriever(store=store, ttl_map=TTL_MAP)
    results = r.search("CCL 口径", limit=5)
    assert [f["fact_id"] for f in results[:2]] == [hot_id, cold_id]


def test_reinforcement_disabled(store):
    fid = store.add_fact("CCL 紧张口径快照", tags="CCL 口径 T3")
    _age_to(store, fid, 120)
    _set(store, fid, retrieval_count=20)  # capped at 4 -> reinforced half-life 60d * 3 = 180d
    r_on = FactRetriever(store=store, ttl_map=TTL_MAP)  # default reinforcement=True
    r_off = FactRetriever(store=store, ttl_map=TTL_MAP, temporal_reinforcement=False)
    on = r_on.search("CCL 口径", limit=5)[0]
    off = r_off.search("CCL 口径", limit=5)[0]
    assert on["fact_id"] == fid and off["fact_id"] == fid
    assert on["score"] > off["score"]  # 0.5^(120/180) vs 0.5^(120/60) on identical relevance


def test_ttl_days_overrides_tag_level(store):
    fid = store.add_fact("短线行情快照", tags="行情 快照 T3")
    _age_to(store, fid, 120)
    _set(store, fid, ttl_days=7)  # explicit override beats T3=60
    r = FactRetriever(store=store, ttl_map=TTL_MAP)
    results = r.search("行情 快照", limit=5)
    assert results[0]["fact_id"] == fid
    assert results[0]["score"] < 0.05  # 0.5^(120/7) ≈ 0.00007 times relevance*trust


def test_future_timestamp_and_garbage_score_neutral(store):
    fid = store.add_fact("未来时间戳事实", tags="未来 时间 T3")
    _set(store, fid, updated_at="2099-01-01 00:00:00")
    r = FactRetriever(store=store, ttl_map=TTL_MAP)
    results = r.search("未来 时间", limit=5)
    assert results[0]["fact_id"] == fid
    assert r._temporal_decay(None, 60) == 1.0
    assert r._temporal_decay("not-a-date", 60) == 1.0


def test_decay_reranks_but_never_drops(store):
    old_id = store.add_fact("澳弘价格快照（2026-08）：双面板 598 元/㎡", tags="澳弘 价格 T3")
    _age_to(store, old_id, 365)  # 6 half-lives
    r = FactRetriever(store=store, ttl_map=TTL_MAP)
    results = r.search("澳弘 价格", limit=5)
    assert any(f["fact_id"] == old_id for f in results)  # still retrievable, just reranked
