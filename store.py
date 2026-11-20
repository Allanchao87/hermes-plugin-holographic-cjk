"""SQLite-backed fact store with entity resolution and trust scoring (single-user Hermes memory plugin)."""

import os
import re
import sqlite3
import threading
from pathlib import Path

from . import holographic as hrr
from .cjk_tokenize import build_search_text

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    fact_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content         TEXT NOT NULL UNIQUE,
    category        TEXT DEFAULT 'general',
    tags            TEXT DEFAULT '',
    search_text     TEXT DEFAULT '',
    trust_score     REAL DEFAULT 0.5,
    retrieval_count INTEGER DEFAULT 0,
    helpful_count   INTEGER DEFAULT 0,
    ttl_days        INTEGER,
    valid_until     TEXT,
    superseded_by   INTEGER REFERENCES facts(fact_id),
    superseded_reason TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hrr_vector      BLOB
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    entity_type TEXT DEFAULT 'unknown',
    aliases     TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id   INTEGER REFERENCES facts(fact_id),
    entity_id INTEGER REFERENCES entities(entity_id),
    PRIMARY KEY (fact_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_facts_trust    ON facts(trust_score DESC);
CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);
CREATE INDEX IF NOT EXISTS idx_entities_name  ON entities(name);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
    USING fts5(content, tags, search_text, content=facts, content_rowid=fact_id);

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, content, tags, search_text)
        VALUES (new.fact_id, new.content, new.tags, new.search_text);
END;

CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags, search_text)
        VALUES ('delete', old.fact_id, old.content, old.tags, old.search_text);
END;

CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags, search_text)
        VALUES ('delete', old.fact_id, old.content, old.tags, old.search_text);
    INSERT INTO facts_fts(rowid, content, tags, search_text)
        VALUES (new.fact_id, new.content, new.tags, new.search_text);
END;

CREATE TABLE IF NOT EXISTS memory_banks (
    bank_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_name  TEXT NOT NULL UNIQUE,
    vector     BLOB NOT NULL,
    dim        INTEGER NOT NULL,
    fact_count INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

_HELPFUL_DELTA, _UNHELPFUL_DELTA = 0.05, -0.10

# Entity extraction patterns, applied in order: capitalized multi-word phrases ("John Doe"), double-quoted terms,
# single-quoted terms, then "X aka Y" (both sides).
_RE_SINGLE_ENTITY = (re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b'), re.compile(r'"([^"]+)"'), re.compile(r"'([^']+)'"))
_RE_AKA = re.compile(r'(\w+(?:\s+\w+)*)\s+(?:aka|also known as)\s+(\w+(?:\s+\w+)*)', re.IGNORECASE)
_ENTITY_NAMES_SQL = "SELECT e.name FROM entities e JOIN fact_entities fe ON fe.entity_id = e.entity_id WHERE fe.fact_id = ?"
# Entity lookup order: exact name, then aliases (comma-separated; wrapped in commas for whole-alias matching).
_ENTITY_LOOKUPS = ("SELECT entity_id FROM entities WHERE name LIKE ?",
                   "SELECT entity_id FROM entities WHERE ',' || aliases || ',' LIKE '%,' || ? || ',%'")


def _clamp_trust(value: float) -> float:
    return max(0.0, min(1.0, value))


class MemoryStore:
    """SQLite-backed fact store with entity resolution and trust scoring.

    Process-wide shared connection registry: SQLite allows one writer at a time and several providers
    coexist per process (main agent + every delegate_task subagent), so all instances for the same database
    share ONE connection and ONE re-entrant lock — writes are fully serialized and "database is locked" is
    impossible. Refcounted: closing one instance never tears the connection out from under a sibling."""

    _shared: dict = {}
    _shared_guard = threading.Lock()

    def __init__(self, db_path: "str | Path | None" = None, default_trust: float = 0.5, hrr_dim: int = 1024) -> None:
        if db_path is None:
            from hermes_constants import get_hermes_home
            db_path = str(get_hermes_home() / "memory_store.db")
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.default_trust, self.hrr_dim, self._hrr_available = _clamp_trust(default_trust), hrr_dim, hrr._HAS_NUMPY
        try:  # resolve() so symlinked/relative paths to the same file share ONE connection
            self._key = str(self.db_path.resolve())
        except OSError:
            self._key = str(self.db_path)
        with MemoryStore._shared_guard:
            entry = MemoryStore._shared.get(self._key)
            if entry is None:
                # Autocommit: a write that raises mid-method can't leave a dangling transaction (and its
                # write lock) open; the explicit commit() calls in _write are then harmless no-ops.
                conn = sqlite3.connect(self._key, check_same_thread=False, timeout=10.0, isolation_level=None)
                conn.row_factory = sqlite3.Row
                entry = MemoryStore._shared[self._key] = {"conn": conn, "lock": threading.RLock(), "refs": 0, "ready": False}
            entry["refs"] += 1
            self._entry, self._conn, self._lock = entry, entry["conn"], entry["lock"]
        with self._lock:  # schema initialised once per shared connection
            if not entry["ready"]:
                self._init_db()
                entry["ready"] = True

    def _init_db(self) -> None:
        """Create schema, enable WAL via the shared fallback helper (NFS/SMB/FUSE degrade gracefully), add hrr_vector to pre-HRR DBs."""
        from hermes_state_wal import apply_wal_with_fallback
        apply_wal_with_fallback(self._conn, db_label="memory_store.db (holographic)")
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(facts)").fetchall()}
        if columns and "hrr_vector" not in columns:  # pre-HRR DB: table exists, column missing
            from hermes_cli.sqlite_util import add_column_if_missing
            add_column_if_missing(self._conn, "facts", "hrr_vector", "hrr_vector BLOB")
        if columns and "ttl_days" not in columns:  # pre-P1 DB: explicit per-fact decay override column
            from hermes_cli.sqlite_util import add_column_if_missing
            add_column_if_missing(self._conn, "facts", "ttl_days", "ttl_days INTEGER")
        if columns and "superseded_by" not in columns:  # pre-P2 DB: version-chain death-note columns
            from hermes_cli.sqlite_util import add_column_if_missing
            add_column_if_missing(self._conn, "facts", "valid_until", "valid_until TEXT")
            add_column_if_missing(self._conn, "facts", "superseded_by", "superseded_by INTEGER")
            add_column_if_missing(self._conn, "facts", "superseded_reason", "superseded_reason TEXT")
        self._conn.executescript(_SCHEMA)  # creates everything on a fresh DB; no-ops otherwise
        # Pre-search_text DBs: the FTS table exists with (content, tags) only. Rebuild it
        # with the extra column, backfill bigrams, and let triggers keep it current after.
        fts_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(facts_fts)").fetchall()}
        if fts_columns and "search_text" not in fts_columns:
            self._migrate_search_text("search_text" in columns)
        self._conn.commit()

    def _migrate_search_text(self, has_search_text_col: bool) -> None:
        """Upgrade a pre-CJK DB: add facts.search_text if missing, backfill bigrams for every
        existing fact, and recreate facts_fts with the search_text column (drop-and-recreate —
        FTS5 cannot ALTER a virtual table). Idempotent; safe on an empty store."""
        if not has_search_text_col:
            from hermes_cli.sqlite_util import add_column_if_missing
            add_column_if_missing(self._conn, "facts", "search_text", "search_text TEXT DEFAULT ''")
        # Drop the old FTS table + its triggers BEFORE touching facts, or the legacy
        # UPDATE/DELETE triggers fire with the old (content, tags) column list.
        self._conn.execute("DROP TRIGGER IF EXISTS facts_ai")
        self._conn.execute("DROP TRIGGER IF EXISTS facts_ad")
        self._conn.execute("DROP TRIGGER IF EXISTS facts_au")
        self._conn.execute("DROP TABLE facts_fts")
        for row in self._conn.execute("SELECT fact_id, content, tags FROM facts").fetchall():
            self._conn.execute("UPDATE facts SET search_text = ? WHERE fact_id = ?",
                               (build_search_text(row["content"], row["tags"]), row["fact_id"]))
        # Re-run just the FTS table + trigger DDL from the shared schema, then repopulate.
        self._conn.executescript(_SCHEMA)
        self._conn.execute("INSERT INTO facts_fts(facts_fts) VALUES('rebuild')")

    def _one(self, sql: str, params=()):
        return self._conn.execute(sql, params).fetchone()

    def _write(self, sql: str, params=()) -> sqlite3.Cursor:
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur

    def add_fact(self, content: str, category: str = "general", tags: str = "", ttl_days: int | None = None) -> int:
        """Insert a fact and return its fact_id; on duplicate content (UNIQUE) return the existing fact_id untouched.
        Links extracted entities and rebuilds the category bank. ttl_days: optional explicit decay
        half-life override (None = derive from a T-level tag / global config at retrieval time)."""
        with self._lock:
            content = content.strip()
            if not content:
                raise ValueError("content must not be empty")
            try:
                fact_id: int = self._write("INSERT INTO facts (content, category, tags, search_text, ttl_days, trust_score) VALUES (?, ?, ?, ?, ?, ?)",
                                           (content, category, tags, build_search_text(content, tags), ttl_days, self.default_trust)).lastrowid  # type: ignore[assignment]
            except sqlite3.IntegrityError:
                return int(self._one("SELECT fact_id FROM facts WHERE content = ?", (content,))["fact_id"])
            self._link_entities(fact_id, content)
            self._compute_hrr_vector(fact_id, content)
            self._rebuild_bank(category)
            return fact_id

    def update_fact(self, fact_id: int, content: str | None = None, trust_delta: float | None = None,
                    tags: str | None = None, category: str | None = None, ttl_days: int | None = None) -> bool:
        """Partially update a fact; omitted content/tags retain their existing values.
        ttl_days: None = leave unchanged (NOT a clear — 0 is the explicit clear, restoring
        tag-derived/global decay)."""
        with self._lock:
            row = self._one("SELECT fact_id, content, tags, category, trust_score FROM facts WHERE fact_id = ?", (fact_id,))
            if row is None:
                return False
            next_content = row["content"] if content is None else content.strip()
            next_tags = row["tags"] if tags is None else tags
            next_category = row["category"] if category is None else category
            changes = {
                "content": next_content if content is not None else None,
                "tags": next_tags if tags is not None else None,
                "category": next_category if category is not None else None,
                # Rebuild from the effective values, not from omitted None arguments.
                "search_text": build_search_text(next_content, next_tags),
                "trust_score": _clamp_trust(row["trust_score"] + trust_delta) if trust_delta is not None else None,
            }
            changes = {col: val for col, val in changes.items() if val is not None}
            if ttl_days is not None:  # added after the None-filter: 0 is a valid explicit clear
                changes["ttl_days"] = int(ttl_days)
            assignments = ", ".join(["updated_at = CURRENT_TIMESTAMP"] + [f"{col} = ?" for col in changes])
            self._write(f"UPDATE facts SET {assignments} WHERE fact_id = ?", [*changes.values(), fact_id])
            if content is not None:
                self._write("DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,))
                self._link_entities(fact_id, next_content)
                self._compute_hrr_vector(fact_id, next_content)
            self._rebuild_bank(next_category)
            return True

    def remove_fact(self, fact_id: int) -> bool:
        """Delete a fact and its entity links. Returns True if the row existed."""
        with self._lock:
            row = self._one("SELECT fact_id, category FROM facts WHERE fact_id = ?", (fact_id,))
            if row is None:
                return False
            self._conn.execute("DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,))
            self._write("DELETE FROM facts WHERE fact_id = ?", (fact_id,))
            self._rebuild_bank(row["category"])
            return True

    def supersede(self, fact_id: int, by_fact_id: int, reason: str = "", valid_until: str | None = None) -> bool:
        """Retire fact_id in favour of by_fact_id: sets valid_until (real-world end of truth,
        defaults to now UTC) + superseded_by + superseded_reason (death note). The old fact is
        NEVER deleted — retrieval stops surfacing it, timeline() still shows the full chain.
        Guards: both ids must exist, no self-reference, no re-marking (a fact already superseded
        keeps its first successor — chains stay linear), and the successor must itself be live."""
        if fact_id == by_fact_id:
            raise ValueError("a fact cannot supersede itself")
        with self._lock:
            old = self._one("SELECT fact_id, category, superseded_by FROM facts WHERE fact_id = ?", (fact_id,))
            new = self._one("SELECT fact_id, superseded_by FROM facts WHERE fact_id = ?", (by_fact_id,))
            if old is None or new is None:
                raise ValueError(f"unknown fact_id: {fact_id if old is None else by_fact_id}")
            if old["superseded_by"] is not None:
                raise ValueError(f"fact {fact_id} is already superseded by {old['superseded_by']}; chains stay linear")
            if new["superseded_by"] is not None:
                raise ValueError(f"fact {by_fact_id} is itself superseded by {new['superseded_by']}; supersede with the chain head instead")
            if valid_until is None:
                from datetime import datetime, timezone
                valid_until = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            self._write("UPDATE facts SET valid_until = ?, superseded_by = ?, superseded_reason = ? WHERE fact_id = ?",
                        (valid_until, by_fact_id, reason or "", fact_id))
            # The retired fact's vector must leave its category bank (probe ranks against it);
            # _rebuild_bank skips superseded rows, so a rebuild drops it from the bundle.
            self._rebuild_bank(old["category"])
            return True

    def timeline(self, fact_id: int) -> dict:
        """Full version chain containing fact_id, oldest → newest. Each entry is the public fact
        dict plus chain position; live head carries superseded_by=None. Cycle-safe (visited set)."""
        with self._lock:
            chain_ids: list[int] = [fact_id]
            # Walk forward to the live head
            seen = {fact_id}
            cur = self._one("SELECT fact_id, superseded_by FROM facts WHERE fact_id = ?", (fact_id,))
            if cur is None:
                raise ValueError(f"unknown fact_id: {fact_id}")
            while cur["superseded_by"] is not None:
                nxt = cur["superseded_by"]
                if nxt in seen:
                    raise ValueError("supersede cycle detected")
                seen.add(nxt)
                chain_ids.append(nxt)
                cur = self._one("SELECT fact_id, superseded_by FROM facts WHERE fact_id = ?", (nxt,))
            # Walk backward from fact_id collecting predecessors (full scan is fine at store scale)
            frontier = {fact_id}
            while frontier:
                placeholders = ",".join("?" * len(frontier))
                preds = self._conn.execute(
                    f"SELECT fact_id FROM facts WHERE superseded_by IN ({placeholders})", sorted(frontier)).fetchall()
                frontier = {r["fact_id"] for r in preds} - seen
                seen |= frontier
                chain_ids = sorted(frontier) + chain_ids
            facts = []
            for i, fid in enumerate(chain_ids):
                row = self._one("SELECT fact_id, content, category, tags, trust_score, retrieval_count, "
                                "helpful_count, ttl_days, valid_until, superseded_by, superseded_reason, "
                                "created_at, updated_at FROM facts WHERE fact_id = ?", (fid,))
                f = dict(row)
                f["chain_position"] = i + 1
                f["is_live"] = f["superseded_by"] is None
                facts.append(f)
            return {"fact_id": fact_id, "chain_length": len(facts), "chain": facts}

    def list_facts(self, category: str | None = None, min_trust: float = 0.0, limit: int = 50) -> list[dict]:
        """Browse facts ordered by trust_score descending, optionally filtered by category / min trust."""
        with self._lock:
            category_clause = "AND category = ? " if category is not None else ""
            params = [min_trust] + ([category] if category is not None else []) + [limit]
            sql = ("SELECT fact_id, content, category, tags, trust_score, retrieval_count, helpful_count, "
                   f"created_at, updated_at FROM facts WHERE trust_score >= ? {category_clause}"
                   "ORDER BY trust_score DESC LIMIT ?")
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def record_retrieval(self, fact_ids: list[int]) -> None:
        """Increment retrieval_count for the fact ids a retrieval actually surfaced (#78801:
        the column existed but no read path ever incremented it). Read paths call this with
        their final result ids; failures are non-fatal (counting must never break retrieval)."""
        if not fact_ids:
            return
        with self._lock:
            self._write(f"UPDATE facts SET retrieval_count = retrieval_count + 1 WHERE fact_id IN "
                        f"({','.join('?' * len(fact_ids))})", list(fact_ids))

    def record_feedback(self, fact_id: int, helpful: bool) -> dict:
        """Adjust trust asymmetrically: helpful -> +0.05 and helpful_count += 1; unhelpful -> -0.10.
        Returns {fact_id, old_trust, new_trust, helpful_count}. Raises KeyError if fact_id is unknown."""
        with self._lock:
            row = self._one("SELECT fact_id, trust_score, helpful_count FROM facts WHERE fact_id = ?", (fact_id,))
            if row is None:
                raise KeyError(f"fact_id {fact_id} not found")
            old_trust: float = row["trust_score"]
            new_trust = _clamp_trust(old_trust + (_HELPFUL_DELTA if helpful else _UNHELPFUL_DELTA))
            increment = 1 if helpful else 0
            self._write("UPDATE facts SET trust_score = ?, helpful_count = helpful_count + ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE fact_id = ?", (new_trust, increment, fact_id))
            return {"fact_id": fact_id, "old_trust": old_trust, "new_trust": new_trust, "helpful_count": row["helpful_count"] + increment}

    def _extract_entities(self, text: str) -> list[str]:
        """Regex entity candidates (see the pattern table), deduplicated case-insensitively in first-seen order."""
        raw = [m.group(1) for pattern in _RE_SINGLE_ENTITY for m in pattern.finditer(text)]
        for m in _RE_AKA.finditer(text):
            raw += [m.group(1), m.group(2)]
        uniq: dict[str, str] = {}  # lower-cased key -> first-seen spelling, insertion-ordered
        for name in filter(None, (n.strip() for n in raw)):
            uniq.setdefault(name.lower(), name)
        return list(uniq.values())

    def _link_entities(self, fact_id: int, content: str) -> None:
        """Extract entities from content, resolve/create them, and link each to the fact."""
        for name in self._extract_entities(content):
            self._write("INSERT OR IGNORE INTO fact_entities (fact_id, entity_id) VALUES (?, ?)",
                        (fact_id, self._resolve_entity(name)))

    def _resolve_entity(self, name: str) -> int:
        """Return the entity_id for a case-insensitive name or alias match, creating the entity if absent."""
        for sql in _ENTITY_LOOKUPS:
            row = self._one(sql, (name,))
            if row is not None:
                return int(row["entity_id"])
        return int(self._write("INSERT INTO entities (name) VALUES (?)", (name,)).lastrowid)  # type: ignore[arg-type]

    def _compute_hrr_vector(self, fact_id: int, content: str) -> None:
        """Compute and store the HRR vector for a fact (linked entities as roles). No-op without numpy."""
        if not self._hrr_available:
            return
        entities = [row["name"] for row in self._conn.execute(_ENTITY_NAMES_SQL, (fact_id,)).fetchall()]
        blob = hrr.phases_to_bytes(hrr.encode_fact(content, entities, self.hrr_dim))
        self._write("UPDATE facts SET hrr_vector = ? WHERE fact_id = ?", (blob, fact_id))

    def _rebuild_bank(self, category: str) -> None:
        """Full rebuild of a category's memory bank from all its LIVE fact vectors
        (superseded rows are excluded — a retired snapshot must not steer probe ranking)."""
        if not self._hrr_available:
            return
        bank_name = f"cat:{category}"
        rows = self._conn.execute(
            "SELECT hrr_vector FROM facts WHERE category = ? AND hrr_vector IS NOT NULL AND superseded_by IS NULL",
            (category,)).fetchall()
        if not rows:
            self._write("DELETE FROM memory_banks WHERE bank_name = ?", (bank_name,))
            return
        bank_vector = hrr.bundle(*[hrr.bytes_to_phases(row["hrr_vector"], dim=self.hrr_dim) for row in rows])
        hrr.snr_estimate(self.hrr_dim, len(rows))  # warns when near capacity
        self._write("INSERT INTO memory_banks (bank_name, vector, dim, fact_count, updated_at) "
                    "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) ON CONFLICT(bank_name) DO UPDATE SET "
                    "vector = excluded.vector, dim = excluded.dim, fact_count = excluded.fact_count, "
                    "updated_at = excluded.updated_at", (bank_name, hrr.phases_to_bytes(bank_vector), self.hrr_dim, len(rows)))

    @classmethod
    def release_all_under(cls, directory: "str | Path") -> int:
        """Force-close every shared connection whose database lives under ``directory``; returns the count.
        close() is refcount-driven, so a live holder (e.g. an agent's provider) keeps a profile's SQLite handle
        open, which on Windows makes rmtree of the profile fail. The directory is going away, so later use by a
        stale holder is expected to fail.

        That is exactly what a profile delete must break on Windows: the desktop's main ``serve`` process
        opens ``memory_store.db`` for every known profile, and ``rmtree`` of the profile directory fails
        with ``WinError 32`` while any of those handles is open (#88347). In a process that holds none (e.g.
        the CLI deleting from outside serve) this is a harmless no-op returning 0.
        """
        root = os.path.normcase(str(Path(directory).expanduser().resolve())) + os.sep
        with cls._shared_guard:
            doomed = [cls._shared.pop(key) for key in list(cls._shared) if os.path.normcase(key).startswith(root)]
            for entry in doomed:
                try:
                    with entry["lock"]:
                        entry["conn"].close()
                except Exception:
                    pass  # an already-closed/broken connection must not abort releasing siblings
        return len(doomed)

    def close(self) -> None:
        """Release this instance's reference; the connection closes with the last holder. Idempotent."""
        with MemoryStore._shared_guard:
            entry = getattr(self, "_entry", None)
            if entry is None:
                return
            entry["refs"] -= 1
            if entry["refs"] <= 0:
                try:
                    entry["conn"].close()
                finally:
                    # Pop only OUR entry: after release_all_under() a same-path store may have
                    # registered a FRESH entry under this key; a stale late close() must not evict it.
                    # See #88347.
                    if MemoryStore._shared.get(self._key) is entry:
                        MemoryStore._shared.pop(self._key, None)
            self._entry = None

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
