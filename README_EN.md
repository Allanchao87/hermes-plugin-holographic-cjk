# holographic-zh

A local memory plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) — a fix for the **zero-recall defect the Holographic fact store has for CJK** (Chinese and other unsegmented languages). It keeps the upstream data contract unchanged (same `memory_store.db` schema, same `fact_store` tool interface), so you can switch from the upstream `holographic` provider without data migration.

**[中文说明（默认）](README.md)**

## Background: why this plugin exists

### The upstream defect

The Hermes built-in `holographic` memory provider stores facts in SQLite FTS5 using the default `unicode61` tokenizer. That tokenizer **splits on whitespace** — an unsegmented CJK run is stored as **one single token**. Consequently:

- the body of a Chinese fact (e.g. `PCB供应商账期口径：月结45天…`) is indexed as **one long token**;
- a natural-language Chinese query (e.g. `云洲的账期是多少`) is also expanded to **one long token** on the query side;
- the two can never be equal → **zero hits**.

This does not only affect manual `fact_store(search)`: per-turn automatic prefetch passes the user's raw Chinese sentence and gets nothing back either. `probe` / `reason` are equally blind — their HRR vector encoding also splits on spaces, so a Chinese sentence collapses to a single effective component and every entity scores almost identically. **The net effect: on a default install, an entire language family has no long-term memory at all.** (Upstream issues: [#85524](https://github.com/NousResearch/hermes-agent/issues/85524), [#83593](https://github.com/NousResearch/hermes-agent/issues/83593), [#73868](https://github.com/NousResearch/hermes-agent/issues/73868))

In October 2026 upstream removed the `holographic` provider from the core tree (standalone repo: [NousResearch/hermes-plugin-holographic](https://github.com/NousResearch/hermes-plugin-holographic)); that repo is currently unmaintained and **does not contain this fix either**.

### The fix

This plugin stores a **shared CJK bigram token soup** in the `facts.search_text` column:

- **Index side**: CJK runs are split into overlapping 2-character bigrams (`供应商账期` → `供应 应商 商账 账期`), Latin tokens pass through unchanged, all space-joined (so unicode61 sees each bigram as its own token).
- **Query side**: queries are expanded with **the same tokenizer** — query and index produce identical bigrams, so **any CJK substring of 2+ characters matches, regardless of spacing**: a packed sentence query retrieves the same fact as spaced keywords.
- **Transparent migration**: opening a pre-CJK database automatically adds the `search_text` column, backfills bigrams, and rebuilds the FTS index — no manual steps.

It also fixes two `fact_store` update defects (found in review of the original core-tree patch):

1. `update_fact(fact_id, tags=...)` (re-tagging only) called `.strip()` on `None` and raised `AttributeError` — the documented re-tagging path was completely unusable.
2. A content-only update rebuilt `search_text` from the new content alone, **silently dropping the tags' bigrams** — after the update, querying by the original tags returned zero hits.

`update_fact` now rebuilds `search_text` from the effective values read from the existing row; tags-only / content-only / trust-only partial updates are all safe (covered by 4 regression tests in `tests/test_store_update.py`).

Additionally, the `retrieval_count` column was declared but never written upstream ([#78801](https://github.com/NousResearch/hermes-agent/issues/78801)); this plugin increments it for every surfaced fact, so it becomes a real "which facts are actually used" signal.

## Relation to other projects

- **Hermes built-in `holographic` provider**: removed from the core tree in 2026-10. This plugin keeps its data contract and interface — it is the drop-in, CJK-fixed replacement (the original core-tree patch, PR [#127339](https://github.com/NousResearch/hermes-agent/pull/127339), was closed when the code was removed; this plugin is its final form).
- **NousResearch/hermes-plugin-holographic**: the official standalone repo — unmaintained, without the CJK fix.
- **Songokou1983/holographic-zh**: an unrelated community project (jieba-based, unmaintained since 2026-04) that shares the name but no code.

## Feature list

- CJK bigram indexing + same-tokenizer query expansion: packed sentence == spaced keywords, equivalent recall
- Transparent migration of pre-CJK databases (auto add column, backfill bigrams, rebuild FTS on first open)
- Safe partial updates (`update_fact` with tags-only / content-only / trust-only)
- Real `retrieval_count` accounting
- **Tiered temporal decay (0.2.0)**: per-fact decay half-life keyed by a T-level tag (e.g. `T0` event = never, `T3` fast-moving = 60-day half-life), with an explicit `facts.ttl_days` per-row override and retrieval reinforcement that stretches the half-life of frequently recalled facts
- Graceful degradation without numpy (HRR layer disables itself, weights redistribute, CJK keyword retrieval unaffected)
- Local-only: no embedding API, zero outbound network calls

## Requirements

| Dependency | Required? | Notes |
|---|---|---|
| Hermes Agent | required | this is a memory provider plugin, not a standalone program (imports `agent.memory_provider` etc.) |
| Python ≥ 3.11 | required | |
| SQLite with FTS5 | required | bundled with official Python builds and mainstream Linux distros |
| numpy ≥1.26, <3 | optional at runtime | declared in `pyproject.toml`, auto-installed by `plugins enable`; without it the HRR layer turns off and bigram retrieval keeps working (the test suite covers the numpy-less environment) |
| pytest + ruamel.yaml | tests only | not needed at runtime |

## Installation

Prerequisite: Hermes Agent is installed. The plugin must live in the **target profile's** `plugins/` directory (default = `~/.hermes/plugins/`, other profiles = `~/.hermes/profiles/<name>/plugins/`).

```bash
git clone https://github.com/Allanchao87/hermes-plugin-holographic-cjk.git \
  ~/.hermes/plugins/holographic-zh

hermes plugins validate ~/.hermes/plugins/holographic-zh
hermes plugins enable holographic-zh          # auto-installs the numpy dependency
hermes config set memory.provider holographic-zh
hermes memory status                          # expect: Provider: holographic-zh ← active
```

For production, pin the Git URL to an immutable commit:

```bash
hermes plugins install Allanchao87/hermes-plugin-holographic-cjk --ref <40-char-commit-sha>
hermes plugins enable holographic-zh
```

Do not track a moving branch for a production deployment.

## Configuration

The `plugins.hermes-memory-store` section of `$HERMES_HOME/config.yaml`:

| Key | Default | Description |
|---|---|---|
| `db_path` | `$HERMES_HOME/memory_store.db` | SQLite path; the `$HERMES_HOME` placeholder resolves per profile |
| `auto_extract` | `false` | auto-extract facts at session end |
| `default_trust` | `0.5` | initial trust score of new facts |
| `min_trust_threshold` | `0.3` | minimum-trust retrieval filter |
| `temporal_decay_half_life` | `0` | global fallback decay half-life (days), applies only to facts with no T-level tag; 0 = disabled |
| `ttl_map` | `{}` | per-fact decay half-lives keyed by a T-level token in fact tags, JSON: `{"T0":0,"T1":0,"T2":180,"T3":60}` (0 = timeless). Example: `hermes config set plugins.hermes-memory-store.ttl_map '{"T0":0,"T1":0,"T2":180,"T3":60}' --force` |
| `temporal_reinforcement` | `true` | retrieval reinforcement: effective half-life stretches with `retrieval_count` (× (1 + 0.5·min(hits, 4))) — frequently recalled facts fade slower |
| `hrr_dim` | `1024` | HRR vector dimensions |
| `hrr_weight` | `0.3` | HRR similarity weight (redistributed automatically when numpy is absent) |

### Tiered temporal decay usage (0.2.0)

Tag facts with a temporal level when writing them: `T0` event (never expires — exits, rulings, meeting outcomes), `T1` structural (tool commands, table schemas, conventions), `T2` slow-moving (org charts, payment terms, supply landscape), `T3` fast-moving (market conditions, capacity, price snapshots). With `ttl_map` configured:

- decay only lowers ranking weight, it **never deletes** — stale facts stay retrievable, they just stop outranking fresh snapshots
- the `facts.ttl_days` column overrides the tag-derived half-life per row (e.g. a T3 status valid for exactly 7 days)
- for mixed event/state facts, split into two: the event (T0) records what happened, the state (T3) snapshots the current picture; add new snapshots instead of overwriting to build a timeline

## Running the tests

> pytest 9 builds a Package node for the hyphenated plugin root (it has `__init__.py`) and imports it by directory name, which fails the relative import — all 22 tests error out. Keep the rootdir inside `tests/`:

```bash
cd tests && env -u PYTHONPATH uv run --with pytest --with numpy --with ruamel.yaml \
  -m pytest . -q --rootdir=. -p no:cacheprovider
# expect: 22 passed
```

## Switching from the upstream holographic provider (backup / migration / rollback)

Validate on a temporary profile or database copy first. Never open the production database with two providers at the same time. Switching providers **does not move or rewrite the database**; migration happens automatically on first open.

```bash
# 1. Consistent backup (use sqlite3 .backup on a live DB; never cp the main .db file)
hermes gateway stop
mkdir -p "$HOME/.hermes/backups"
backup="$HOME/.hermes/backups/memory_store-before-holographic-zh-$(date +%Y%m%d-%H%M%S).db"
sqlite3 "$HOME/.hermes/memory_store.db" ".backup '$backup'"
cp "$HOME/.hermes/config.yaml" "$HOME/.hermes/backups/config-before-switch-$(date +%Y%m%d-%H%M%S).yaml"

# 2. Validate
hermes plugins validate "$HOME/.hermes/plugins/holographic-zh"
hermes plugins doctor holographic-zh

# 3. Switch
hermes config set memory.provider holographic-zh
hermes memory status
hermes gateway start
hermes memory status    # fact count must match the pre-switch count; PRAGMA integrity_check must pass
```

Rollback:

```bash
hermes gateway stop
hermes config set memory.provider holographic
hermes gateway start
hermes memory status
```

To restore a backup: stop the gateway first, then copy (never overwrite a live WAL database by copying only its main `.db` file).

## Known limitations and future work

- **Single-character CJK queries return nothing**: the bigram minimum is 2 characters; `search("账")` returns empty (indistinguishable from "not stored").
- **No idf / length penalty in ranking**: recall is wide, but long irrelevant entries can rank in; on large stores, use tags for precision (tags share the same bigram pipeline; write hyphenated Latin words split apart in tags, e.g. `lark-cli` → `lark cli`).
- **No semantic layer**: synonym / cross-language matching (`识图` vs `OCR`) is out of reach for bigrams; a local embedding layer is future work.
- Not implemented: AND-first precision ranking, native `cjk_unicode61` FTS5 tokenizer (requires compiling a `.so`).

## Privacy

Fully local: no embedding API, no fact content ever leaves the machine. numpy is used only for local HRR vector math.

## License

[MIT](LICENSE)
