"""Persistent SQLite-backed Local Prompt & Response Cache.

Stores exact prompt-response pairs to eliminate redundant LLM calls completely.
Supports TTL expiration, reuse tracking, and thread-safe operations with zero-failure guarantee.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterator

try:
    from .config import (
        APP_VERSION,
        BYPASS_LOCAL_CACHE,
        INSTRUCTION_VERSION,
        PARSER_VERSION,
        PROMPT_CACHE_DIR,
        PROMPT_CACHE_ENABLED,
        PROMPT_CACHE_TTL_HOURS,
        SCHEMA_VERSION,
        get_instruction_hash,
    )
    from .logger import log_local_hit
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from caching.config import (
        APP_VERSION,
        BYPASS_LOCAL_CACHE,
        INSTRUCTION_VERSION,
        PARSER_VERSION,
        PROMPT_CACHE_DIR,
        PROMPT_CACHE_ENABLED,
        PROMPT_CACHE_TTL_HOURS,
        SCHEMA_VERSION,
        get_instruction_hash,
    )
    from caching.logger import log_local_hit

log = logging.getLogger("census.caching.local")


class LocalPromptCache:
    """Thread-safe persistent SQLite cache for prompt-response pairs."""

    def __init__(self, db_dir: Path | None = None, ttl_hours: float | None = None) -> None:
        self.db_dir = db_dir or PROMPT_CACHE_DIR
        self.ttl_seconds = (ttl_hours if ttl_hours is not None else PROMPT_CACHE_TTL_HOURS) * 3600.0
        self._lock = threading.Lock()
        self._db_path = self.db_dir / "prompt_cache.sqlite3"
        self._initialized = False

    @contextlib.contextmanager
    def _get_connection(self, timeout: float = 5.0) -> Iterator[sqlite3.Connection]:
        """Context manager yielding a SQLite connection and ensuring it is closed."""
        conn = sqlite3.connect(str(self._db_path), timeout=timeout)
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _ensure_init(self) -> None:
        """Initialize the SQLite schema if not already created."""
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            try:
                self.db_dir.mkdir(parents=True, exist_ok=True)
                with self._get_connection(timeout=10.0) as conn:
                    with conn:
                        conn.execute("PRAGMA journal_mode=WAL;")
                        conn.execute("PRAGMA synchronous=NORMAL;")
                        conn.execute("""
                            CREATE TABLE IF NOT EXISTS prompt_cache (
                                prompt_hash TEXT PRIMARY KEY,
                                model TEXT,
                                step_name TEXT,
                                prompt_preview TEXT,
                                response_json TEXT NOT NULL,
                                tokens_saved_estimate INTEGER DEFAULT 0,
                                access_count INTEGER DEFAULT 1,
                                created_at REAL NOT NULL,
                                last_accessed_at REAL NOT NULL
                            );
                        """)
                        conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_created ON prompt_cache(created_at);")
                        conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_step ON prompt_cache(step_name);")
                self._initialized = True
            except Exception as exc:
                log.warning("Could not initialize local prompt cache SQLite DB: %s", exc)

    @staticmethod
    def compute_hash(
        prompt: str,
        model: str,
        app_version: str | None = None,
        schema_version: str | None = None,
        parser_version: str | None = None,
        instruction_version: str | None = None,
    ) -> str:
        """Compute deterministic SHA-256 hash incorporating prompt, model, instruction, schema, and parser versions."""
        app_v = app_version or APP_VERSION
        schema_v = schema_version or SCHEMA_VERSION
        parser_v = parser_version or PARSER_VERSION
        inst_v = instruction_version or get_instruction_hash()
        normalized = f"{model.strip().lower()}:::{inst_v}:::{schema_v}:::{parser_v}:::{app_v}:::{prompt.strip()}"
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def get(
        self,
        prompt: str,
        model: str,
        step_name: str = "",
        bypass_cache: bool = False,
    ) -> Any | None:
        """Retrieve cached response if available, not expired, and not bypassed."""
        if not PROMPT_CACHE_ENABLED or bypass_cache or BYPASS_LOCAL_CACHE:
            if bypass_cache or BYPASS_LOCAL_CACHE:
                log.info("Cache BYPASS active: step='%s' (skipping local cache)", step_name)
            return None

        t0 = time.perf_counter()
        inst_hash = get_instruction_hash()
        prompt_hash = self.compute_hash(prompt, model, instruction_version=inst_hash)

        try:
            self._ensure_init()
            if not self._initialized:
                return None

            now = time.time()
            with self._lock:
                with self._get_connection(timeout=5.0) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT response_json, access_count, tokens_saved_estimate, created_at "
                        "FROM prompt_cache WHERE prompt_hash = ?",
                        (prompt_hash,),
                    )
                    row = cursor.fetchone()
                    if not row:
                        log.info(
                            "Cache MISS: step=%-24s hash=%s app_v=%s inst_v=%s",
                            step_name or "extraction", prompt_hash[:12], APP_VERSION, inst_hash[:8],
                        )
                        return None

                    response_json, access_count, tokens_saved, created_at = row

                    # Check TTL expiration if enabled (> 0)
                    if self.ttl_seconds > 0 and (now - created_at) > self.ttl_seconds:
                        with conn:
                            cursor.execute("DELETE FROM prompt_cache WHERE prompt_hash = ?", (prompt_hash,))
                        log.info("Cache EXPIRED: step=%s hash=%s", step_name, prompt_hash[:12])
                        return None

                    # Increment reuse count and update last accessed timestamp
                    new_count = access_count + 1
                    with conn:
                        cursor.execute(
                            "UPDATE prompt_cache SET access_count = ?, last_accessed_at = ? WHERE prompt_hash = ?",
                            (new_count, now, prompt_hash),
                        )

            data = json.loads(response_json)
            latency_ms = (time.perf_counter() - t0) * 1000.0

            log.info(
                "Cache HIT:  step=%-24s hash=%s app_v=%s inst_v=%s latency=%.2fms access_count=%d",
                step_name or "extraction", prompt_hash[:12], APP_VERSION, inst_hash[:8], latency_ms, new_count,
            )

            # Log reuse with token savings
            saved_tokens = tokens_saved if tokens_saved > 0 else (len(prompt) // 4)
            log_local_hit(
                step_name=step_name or "llm_extraction",
                prompt_hash=prompt_hash,
                access_count=new_count,
                tokens_saved_estimate=saved_tokens,
                latency_ms=latency_ms,
            )
            return data

        except Exception as exc:
            log.debug("Local cache lookup failed safely (proceeding to API): %s", exc)
            return None

    def set(
        self,
        prompt: str,
        model: str,
        response_data: Any,
        step_name: str = "",
        tokens_saved_estimate: int = 0,
    ) -> None:
        """Store prompt and parsed response in persistent SQLite cache."""
        if not PROMPT_CACHE_ENABLED:
            return

        inst_hash = get_instruction_hash()
        prompt_hash = self.compute_hash(prompt, model, instruction_version=inst_hash)
        try:
            self._ensure_init()
            if not self._initialized:
                return

            now = time.time()
            serialized = json.dumps(response_data, ensure_ascii=False)
            preview = prompt.strip()[:160].replace("\n", " ")
            est_tokens = tokens_saved_estimate or (len(prompt) // 4)

            with self._lock:
                with self._get_connection(timeout=5.0) as conn:
                    with conn:
                        conn.execute(
                            """
                            INSERT INTO prompt_cache 
                                (prompt_hash, model, step_name, prompt_preview, response_json, 
                                 tokens_saved_estimate, access_count, created_at, last_accessed_at)
                            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                            ON CONFLICT(prompt_hash) DO UPDATE SET
                                response_json = excluded.response_json,
                                last_accessed_at = excluded.last_accessed_at,
                                access_count = prompt_cache.access_count + 1;
                            """,
                            (prompt_hash, model, step_name, preview, serialized, est_tokens, now, now),
                        )
        except Exception as exc:
            log.debug("Local cache write failed safely: %s", exc)

    def invalidate_step(self, step_name: str) -> int:
        """Invalidate all cached entries for a specific processing step."""
        try:
            self._ensure_init()
            with self._lock:
                with self._get_connection(timeout=5.0) as conn:
                    with conn:
                        cur = conn.cursor()
                        cur.execute("DELETE FROM prompt_cache WHERE step_name = ?", (step_name,))
                        count = cur.rowcount
            log.info("Invalidated %d cache entries for step '%s'.", count, step_name)
            return count
        except Exception as exc:
            log.warning("Failed to invalidate cache for step '%s': %s", step_name, exc)
            return 0

    def clear(self) -> None:
        """Clear all entries in the local prompt cache."""
        try:
            self._ensure_init()
            with self._lock:
                with self._get_connection(timeout=5.0) as conn:
                    with conn:
                        conn.execute("DELETE FROM prompt_cache;")
            log.info("Local prompt cache cleared successfully.")
        except Exception as exc:
            log.warning("Failed to clear local prompt cache: %s", exc)

    def get_stats(self) -> dict[str, Any]:
        """Return cache database summary metrics."""
        try:
            self._ensure_init()
            with self._lock:
                with self._get_connection(timeout=5.0) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*), SUM(access_count), SUM(tokens_saved_estimate) FROM prompt_cache")
                    row = cursor.fetchone()
                    total_items = row[0] if row else 0
                    total_reuses = row[1] if row else 0
                    total_saved = row[2] if row else 0
            return {
                "total_cached_items": total_items or 0,
                "total_reuse_count": total_reuses or 0,
                "estimated_tokens_saved": total_saved or 0,
                "db_path": str(self._db_path),
            }
        except Exception:
            return {"total_cached_items": 0, "total_reuse_count": 0, "estimated_tokens_saved": 0}


# Singleton instance
_INSTANCE: LocalPromptCache | None = None


def get_local_cache() -> LocalPromptCache:
    """Return singleton instance of LocalPromptCache."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = LocalPromptCache()
    return _INSTANCE


if __name__ == "__main__":
    cache = get_local_cache()
    stats = cache.get_stats()
    print("=" * 80)
    print("                      PROMPT CACHE DATABASE OVERVIEW                     ")
    print("=" * 80)
    print(f" Database Path:          {stats.get('db_path')}")
    print(f" Total Cached Prompts:   {stats.get('total_cached_items')}")
    print(f" Total Reuses:           {stats.get('total_reuse_count')}")
    print(f" Estimated Tokens Saved: ~{stats.get('estimated_tokens_saved', 0):,} tokens")
    print("-" * 80)
    print(f"{'STEP':<24} | {'MODEL':<18} | {'REUSE':<5} | {'PROMPT PREVIEW'}")
    print("-" * 80)
    try:
        with cache._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT step_name, model, access_count, prompt_preview FROM prompt_cache ORDER BY last_accessed_at DESC LIMIT 20")
            rows = cur.fetchall()
            for step, model, count, preview in rows:
                print(f"{(step or 'extraction'):<24} | {model:<18} | {count:<5} | {preview[:35]}...")
            if not rows:
                print(" (No prompts cached yet. Process a file through the pipeline to populate cache.)")
    except Exception as exc:
        print(f"Error querying cache: {exc}")
    print("=" * 80)

