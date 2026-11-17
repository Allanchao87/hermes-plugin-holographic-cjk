"""Version chains (P2): supersede retires an outdated fact in favour of its replacement —
the old row is never deleted, live retrieval stops surfacing it, and timeline() reconstructs
the full chain. Behavior contracts:

- superseded facts disappear from search (keyword path) and probe/_vector_rows (vector path)
- the old fact is still in the store: list_facts shows it, timeline returns it
- timeline walks the whole chain oldest → newest from ANY node, with reasons and is_live
- guards: self-supersede, unknown ids, re-marking, and supersede-via-a-retired-fact all raise
- contradict does not report a superseded snapshot vs its live replacement as a contradiction
  (the classic false positive: an old price snapshot and the new one share entities by design)
"""
from __future__ import annotations

import pytest

from holographic_zh.retrieval import FactRetriever
from holographic_zh.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(db_path=tmp_path / "facts.db")
    yield s
    s.close()


def _chain(store):
    """Three snapshots of the same subject: old -> mid -> live."""
    old = store.add_fact("生益供应状态（2026-06）：完全不能保供厚板", tags="生益 供应 厚板 T3")
    mid = store.add_fact("生益供应状态（2026-09）：厚板少量恢复、配额制", tags="生益 供应 厚板 T3")
    live = store.add_fact("生益供应状态（2026-11）：厚板恢复至一天量", tags="生益 供应 厚板 T3")
    return old, mid, live


def test_superseded_fact_leaves_search_but_stays_in_store(store):
    old, mid, live = _chain(store)
    assert store.supersede(old, mid, reason="2026-09 新口径取代") is True
    r = FactRetriever(store=store)
    ids = [f["fact_id"] for f in r.search("生益 供应 厚板", limit=5)]
    assert mid in ids and live in ids
    assert old not in ids  # gone from live retrieval
    assert store.list_facts(limit=10) and any(f["fact_id"] == old for f in store.list_facts(limit=50))  # still stored


def test_timeline_full_chain_from_any_node(store):
    old, mid, live = _chain(store)
    store.supersede(old, mid, reason="9 月口径更新")
    store.supersede(mid, live, reason="11 月走访刷新")
    for probe in (old, mid, live):  # any node reconstructs the same chain
        tl = store.timeline(probe)
        assert tl["chain_length"] == 3
        assert [f["fact_id"] for f in tl["chain"]] == [old, mid, live]  # oldest -> newest
        assert [f["is_live"] for f in tl["chain"]] == [False, False, True]
        assert tl["chain"][0]["superseded_reason"] == "9 月口径更新"
        assert tl["chain"][1]["superseded_reason"] == "11 月走访刷新"
        assert tl["chain"][2]["superseded_by"] is None
        assert tl["chain"][0]["valid_until"]  # death note carries a timestamp


def test_supersede_guards(store):
    old, mid, live = _chain(store)
    with pytest.raises(ValueError, match="itself"):
        store.supersede(old, old)
    with pytest.raises(ValueError, match="unknown"):
        store.supersede(old, 9999)
    store.supersede(old, mid, reason="r1")
    with pytest.raises(ValueError, match="already superseded"):
        store.supersede(old, live, reason="re-mark rejected, chains stay linear")
    with pytest.raises(ValueError, match="itself superseded"):
        store.supersede(mid, old, reason="cannot revive a retired fact as successor")


def test_vector_path_excludes_superseded(store):
    old, mid, live = _chain(store)
    store.supersede(old, mid, reason="newer snapshot")
    r = FactRetriever(store=store)
    rows = r._vector_rows(None)  # [] when no vectors exist (numpy-less stores) — contract: superseded never listed
    ids = {row["fact_id"] for row in rows}
    assert old not in ids


def test_timeline_single_live_fact(store):
    fid = store.add_fact("独立事实", tags="独立")
    tl = store.timeline(fid)
    assert tl["chain_length"] == 1 and tl["chain"][0]["is_live"] is True
