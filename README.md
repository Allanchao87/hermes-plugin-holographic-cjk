# holographic-zh

Standalone Hermes memory provider for the local Holographic fact store.

This plugin preserves the Hermes Holographic data contract while keeping the local CJK bigram retrieval implementation outside the Hermes core tree.

## Why this exists

The upstream built-in `holographic` provider stored facts in SQLite FTS5 with the default `unicode61` tokenizer. An unsegmented CJK run becomes **one single token**, so a Chinese fact and a natural-language Chinese query collapse to two long tokens that never match — per-turn prefetch and manual search silently return **zero hits for the entire language family** (upstream issues #85524, #83593, #73868; the same blind spot hits `probe`/`reason` HRR encoding).

The fix here indexes a shared **CJK bigram token soup** in `facts.search_text` and expands queries with the same tokenizer, so any 2+ character CJK substring matches regardless of spacing: a packed sentence query («云洲的账期是多少») retrieves the same fact as spaced keywords («账期 月结»). Latin text passes through unchanged, and old databases are migrated transparently on first open.

It also fixes two `fact_store` update defects: `update_fact(tags=...)` no longer crashes on `None.strip()`, and content-only / tags-only updates rebuild `search_text` from the effective values instead of dropping the other field's tokens. `retrieval_count` is incremented by real retrievals (#78801).

## Relation to other projects

- Upstream removed the built-in holographic provider from the Hermes core tree (2026-10); the standalone `NousResearch/hermes-plugin-holographic` repo is unmaintained and does not contain this CJK fix.
- The unrelated community project `Songokou1983/holographic-zh` (jieba-based, unmaintained since 2026-04) shares the name but no code.

## Requirements

- Hermes Agent (this is a memory provider plugin, not a standalone program), Python ≥ 3.11, SQLite with FTS5.
- `numpy` is declared in `pyproject.toml` but optional at runtime: without it the HRR vector layer disables itself and keyword/bigram retrieval keeps working (the test suite covers the numpy-less environment).
- Local-only: no embedding API, no outbound network calls.

## What this version contains

- SQLite fact store with the existing `memory_store.db` schema.
- Shared CJK bigram indexing and query expansion for Chinese and mixed Chinese/English text.
- Migration of existing `facts` / `facts_fts` databases through the provider's normal startup path.
- `retrieval_count` accounting for surfaced search results.
- Safe profile-scoped default database path: `$HERMES_HOME/memory_store.db`.
- Fact-update fixes: content-only and tags-only updates preserve the other field when rebuilding `search_text`.

It does not yet enable AND-first precision ranking, semantic embeddings, or native `cjk_unicode61`; those are separate follow-up changes.

## Install from a Git checkout

Run these commands from the plugin repository checkout:

```bash
hermes gateway stop
hermes plugins validate .
hermes plugins enable holographic-zh
hermes plugins list
```

If the checkout is not already under `$HERMES_HOME/plugins/holographic-zh`, install it into the active Hermes home first using the normal plugin install workflow. The plugin must be installed into the profile-local plugin directory, not only placed in an arbitrary source checkout.

For a Git URL pinned to an immutable commit:

```bash
hermes plugins install OWNER/REPO --ref <40-character-commit-sha>
hermes plugins enable holographic-zh
```

Do not use a moving branch for a production deployment.

## Validate before switching

Use a temporary profile/database copy first. The production database must not be opened by two providers at the same time.

```bash
hermes plugins validate "$HOME/.hermes/plugins/holographic-zh"
hermes plugins doctor holographic-zh
```

Then run the plugin test suite from the checkout (`tests/` must be the pytest rootdir — the
hyphenated plugin root has `__init__.py`, and pytest otherwise builds a Package node for it and
fails on the relative import):

```bash
cd tests && env -u PYTHONPATH uv run --with pytest --with numpy --with ruamel.yaml \
  -m pytest . -q --rootdir=. -p no:cacheprovider
```

## Backup and switch

Stop the gateway before the consistent SQLite backup:

```bash
hermes gateway stop
mkdir -p "$HOME/.hermes/backups"
backup="$HOME/.hermes/backups/memory_store-before-holographic-zh-$(date +%Y%m%d-%H%M%S).db"
sqlite3 "$HOME/.hermes/memory_store.db" ".backup '$backup'"
config_backup="$HOME/.hermes/backups/config-before-holographic-zh-$(date +%Y%m%d-%H%M%S).yaml"
cp "$HOME/.hermes/config.yaml" "$config_backup"
```

Switch the provider without moving or rewriting the database:

```bash
hermes config set memory.provider holographic-zh
hermes memory status
hermes gateway start
hermes memory status
```

The expected active provider is `holographic-zh`, and the existing database should still report the same fact count and pass SQLite integrity checks.

## Rollback

```bash
hermes gateway stop
hermes config set memory.provider holographic
hermes gateway start
hermes memory status
```

If a tested rollback requires restoring the database, stop the gateway and restore the specific consistent backup selected by timestamp, then start the gateway again. Never overwrite a live WAL database by copying only its main `.db` file.

## Scope and privacy

The provider is local-only. It does not call an embedding API or send fact content to an external service. NumPy is optional at runtime for HRR operations; SQLite/bigram retrieval remains the required path.
