import os
import time
import sqlite3
import threading

DB_PATH = os.getenv("DB_PATH", "webpreview.db")
_lock = threading.Lock()

def _connect(timeout: float = 10.0) -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=timeout)
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn

def init_db():
    with _lock:
        conn = _connect()
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA cache_size=-4000;")
        cursor = conn.cursor()
        
        # Config table for admin_dc_email, admin_dc_fingerprint, etc.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')

        # Previews history to keep statistics
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS preview_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                from_id INTEGER,
                url TEXT,
                title TEXT,
                filesize INTEGER,
                with_js INTEGER DEFAULT 0,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_preview_stats_chat_id ON preview_stats(chat_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_preview_stats_created_at ON preview_stats(created_at)')

        # Transport statistics
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transport_stats (
                addr TEXT PRIMARY KEY,
                msgs_sent INTEGER DEFAULT 0,
                msgs_received INTEGER DEFAULT 0,
                last_sent_at INTEGER,
                last_received_at INTEGER
            )
        ''')

        # URL Cache table for cached page previews
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS url_cache (
                url_key TEXT PRIMARY KEY,
                filepath TEXT,
                title TEXT,
                filesize INTEGER,
                created_at INTEGER
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_url_cache_created_at ON url_cache(created_at)')

        # URL short hash map table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS url_hashes (
                url_key TEXT PRIMARY KEY,
                url TEXT UNIQUE
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_url_hashes_url ON url_hashes(url)')

        # URL exclusions blacklist table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS url_exclusions (
                pattern TEXT PRIMARY KEY
            )
        ''')

        # OG Cache table for cached banner images and titles
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS og_cache (
                url_key TEXT PRIMARY KEY,
                title TEXT,
                image_path TEXT,
                warning TEXT,
                jina_markdown TEXT,
                created_at INTEGER
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_og_cache_created_at ON og_cache(created_at)')
        try:
            cursor.execute("ALTER TABLE og_cache ADD COLUMN warning TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE og_cache ADD COLUMN jina_markdown TEXT")
        except sqlite3.OperationalError:
            pass
        
        # TLDR Cache table for cached summaries (24h default)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tldr_cache (
                cache_key TEXT PRIMARY KEY,
                summary TEXT,
                created_at INTEGER
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tldr_cache_created_at ON tldr_cache(created_at)')

        # API requests log table for Jina and Gemini tracking
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS api_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service TEXT,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_api_log_service ON api_log(service)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_api_log_created_at ON api_log(created_at)')
        
        # Cache hit/miss log table for tracking cache performance
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cache_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cache_type TEXT,
                hit INTEGER,
                created_at INTEGER DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_cache_log_created_at ON cache_log(created_at)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_cache_log_type ON cache_log(cache_type)')

        conn.commit()
        conn.close()

def set_config(key: str, value: str):
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value)))
            conn.commit()
        finally:
            conn.close()

def get_config(key: str) -> str | None:
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

def get_admin_fingerprint() -> str | None:
    """Get the saved admin DC fingerprint."""
    return get_config("admin_dc_fingerprint")

def set_admin_fingerprint(fp: str):
    """Set the admin DC fingerprint."""
    set_config("admin_dc_fingerprint", fp)

def add_preview_log(chat_id: int, from_id: int, url: str, title: str, filesize: int, with_js: bool):
    """Record a preview generation in the history."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO preview_stats (chat_id, from_id, url, title, filesize, with_js) VALUES (?, ?, ?, ?, ?, ?)",
                (chat_id, from_id, url, title, filesize, 1 if with_js else 0)
            )
            conn.commit()
        finally:
            conn.close()

def get_stats() -> dict:
    """Get preview statistics."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            
            # Total previews
            cursor.execute("SELECT COUNT(*) FROM preview_stats")
            total = cursor.fetchone()[0]
            
            # Last 24h
            cursor.execute("SELECT COUNT(*) FROM preview_stats WHERE created_at >= CAST(strftime('%s','now') AS INTEGER) - 86400")
            last_24h = cursor.fetchone()[0]
            
            # Total size
            cursor.execute("SELECT COALESCE(SUM(filesize), 0) FROM preview_stats")
            total_size = cursor.fetchone()[0]
            
            return {
                "total": total,
                "last_24h": last_24h,
                "total_size": total_size
            }
        finally:
            conn.close()

# Transport statistics tracking (buffered in memory)
_transport_stats_buffer: dict[str, dict[str, int]] = {}
_transport_stats_lock = threading.Lock()
_last_transport_flush = time.time()
TRANSPORT_FLUSH_INTERVAL = 30.0  # seconds

def increment_transport_sent(addr: str):
    """Increment the sent counter for a transport address (buffered in memory)."""
    if not addr or not isinstance(addr, str) or "@" not in addr:
        return
    now = int(time.time())
    should_flush = False
    with _transport_stats_lock:
        if addr not in _transport_stats_buffer:
            _transport_stats_buffer[addr] = {"sent": 0, "recv": 0, "last_sent": 0, "last_recv": 0}
        _transport_stats_buffer[addr]["sent"] += 1
        _transport_stats_buffer[addr]["last_sent"] = now
        global _last_transport_flush
        if now - _last_transport_flush >= TRANSPORT_FLUSH_INTERVAL:
            should_flush = True
    if should_flush:
        flush_transport_stats()

def increment_transport_received(addr: str):
    """Increment the received counter for a transport address (buffered in memory)."""
    if not addr or not isinstance(addr, str) or "@" not in addr:
        return
    now = int(time.time())
    should_flush = False
    with _transport_stats_lock:
        if addr not in _transport_stats_buffer:
            _transport_stats_buffer[addr] = {"sent": 0, "recv": 0, "last_sent": 0, "last_recv": 0}
        _transport_stats_buffer[addr]["recv"] += 1
        _transport_stats_buffer[addr]["last_recv"] = now
        global _last_transport_flush
        if now - _last_transport_flush >= TRANSPORT_FLUSH_INTERVAL:
            should_flush = True
    if should_flush:
        flush_transport_stats()

def flush_transport_stats():
    """Flush buffered transport stats to the database in a single transaction."""
    global _last_transport_flush
    with _transport_stats_lock:
        if not _transport_stats_buffer:
            _last_transport_flush = time.time()
            return
        pending = dict(_transport_stats_buffer)
        _transport_stats_buffer.clear()
        _last_transport_flush = time.time()

    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            for addr, counts in pending.items():
                if not isinstance(addr, str) or "@" not in addr:
                    continue
                sent = int(counts.get("sent", 0))
                recv = int(counts.get("recv", 0))
                last_s = counts.get("last_sent") or None
                last_r = counts.get("last_recv") or None
                cursor.execute('''
                    INSERT INTO transport_stats (addr, msgs_sent, msgs_received, last_sent_at, last_received_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(addr) DO UPDATE SET
                        msgs_sent = msgs_sent + excluded.msgs_sent,
                        msgs_received = msgs_received + excluded.msgs_received,
                        last_sent_at = COALESCE(excluded.last_sent_at, transport_stats.last_sent_at),
                        last_received_at = COALESCE(excluded.last_received_at, transport_stats.last_received_at)
                ''', (addr, sent, recv, last_s, last_r))
            conn.commit()
        finally:
            conn.close()

def get_all_transport_stats() -> list[dict]:
    """Get statistics for all tracked transports."""
    flush_transport_stats()
    with _lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM transport_stats ORDER BY msgs_sent + msgs_received DESC")
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

def get_cached_preview(url_key: str) -> dict | None:
    with _lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM url_cache WHERE url_key = ?", (url_key,))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

def add_cached_preview(url_key: str, filepath: str, title: str, filesize: int):
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO url_cache (url_key, filepath, title, filesize, created_at)
                VALUES (?, ?, ?, ?, CAST(strftime('%s','now') AS INTEGER))
            ''', (url_key, filepath, title, filesize))
            conn.commit()
        finally:
            conn.close()

def clear_expired_cache(max_age_seconds: int):
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM url_cache WHERE created_at < CAST(strftime('%s','now') AS INTEGER) - ?",
                (max_age_seconds,)
            )
            cursor.execute(
                "DELETE FROM og_cache WHERE created_at < CAST(strftime('%s','now') AS INTEGER) - ?",
                (max_age_seconds,)
            )
            cursor.execute(
                "DELETE FROM tldr_cache WHERE created_at < CAST(strftime('%s','now') AS INTEGER) - ?",
                (max_age_seconds,)
            )
            conn.commit()
        finally:
            conn.close()

def cleanup_old_records(retention_days: int = 30) -> dict[str, int]:
    """Prune old preview stats and API logs beyond retention window."""
    now = int(time.time())
    cutoff = now - (retention_days * 86400)
    cleaned = {}
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM preview_stats WHERE created_at < ?", (cutoff,))
            cleaned["preview_stats"] = cursor.rowcount
            cursor.execute("DELETE FROM api_log WHERE created_at < ?", (cutoff,))
            cleaned["api_log"] = cursor.rowcount
            cursor.execute("DELETE FROM cache_log WHERE created_at < ?", (cutoff,))
            cleaned["cache_log"] = cursor.rowcount
            conn.commit()
        finally:
            conn.close()
    return cleaned

def get_cached_tldr(cache_key: str, max_age_seconds: int = 86400) -> str | None:
    with _lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT summary FROM tldr_cache WHERE cache_key = ? AND created_at >= CAST(strftime('%s','now') AS INTEGER) - ?",
                (cache_key, max_age_seconds)
            )
            row = cursor.fetchone()
            return row["summary"] if row else None
        finally:
            conn.close()

def add_cached_tldr(cache_key: str, summary: str):
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO tldr_cache (cache_key, summary, created_at)
                VALUES (?, ?, CAST(strftime('%s','now') AS INTEGER))
            ''', (cache_key, summary))
            conn.commit()
        finally:
            conn.close()

def log_api_call(service: str):
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO api_log (service, created_at) VALUES (?, CAST(strftime('%s','now') AS INTEGER))",
                (service,)
            )
            conn.commit()
        finally:
            conn.close()

def get_api_stats() -> dict:
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            now = int(time.time())
            h24_ago = now - 86400

            cursor.execute("SELECT COUNT(*) FROM api_log WHERE service = 'jina'")
            jina_total = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM api_log WHERE service = 'jina' AND created_at >= ?", (h24_ago,))
            jina_24h = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM api_log WHERE service = 'gemini'")
            gemini_total = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM api_log WHERE service = 'gemini' AND created_at >= ?", (h24_ago,))
            gemini_24h = cursor.fetchone()[0]

            return {
                "jina_total": jina_total,
                "jina_24h": jina_24h,
                "gemini_total": gemini_total,
                "gemini_24h": gemini_24h,
            }
        finally:
            conn.close()

def log_cache_event(cache_type: str, hit: bool):
    """Record a cache hit or miss event."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO cache_log (cache_type, hit, created_at) VALUES (?, ?, CAST(strftime('%s','now') AS INTEGER))",
                (cache_type, 1 if hit else 0)
            )
            conn.commit()
        finally:
            conn.close()

def get_cache_stats() -> dict:
    """Get cache efficiency statistics for the last 24h."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            now = int(time.time())
            cutoff_24h = now - 86400

            cursor.execute("SELECT hit, COUNT(*) FROM cache_log WHERE created_at >= ? GROUP BY hit", (cutoff_24h,))
            rows = cursor.fetchall()
            hits_24h = 0
            misses_24h = 0
            for hit_val, cnt in rows:
                if hit_val == 1:
                    hits_24h = cnt
                else:
                    misses_24h = cnt
            total_24h = hits_24h + misses_24h
            hit_ratio_24h = (hits_24h / total_24h * 100.0) if total_24h > 0 else 0.0

            cursor.execute(
                "SELECT cache_type, hit, COUNT(*) FROM cache_log WHERE created_at >= ? GROUP BY cache_type, hit",
                (cutoff_24h,)
            )
            breakdown_rows = cursor.fetchall()
            breakdown = {}
            for c_type, hit_val, cnt in breakdown_rows:
                if c_type not in breakdown:
                    breakdown[c_type] = {"hits": 0, "misses": 0, "total": 0, "ratio": 0.0}
                if hit_val == 1:
                    breakdown[c_type]["hits"] = cnt
                else:
                    breakdown[c_type]["misses"] = cnt
                breakdown[c_type]["total"] = breakdown[c_type]["hits"] + breakdown[c_type]["misses"]
                if breakdown[c_type]["total"] > 0:
                    breakdown[c_type]["ratio"] = (breakdown[c_type]["hits"] / breakdown[c_type]["total"]) * 100.0

            cursor.execute("SELECT COUNT(*) FROM cache_log WHERE hit = 1")
            all_time_hits = cursor.fetchone()[0]

            return {
                "total_24h": total_24h,
                "hits_24h": hits_24h,
                "misses_24h": misses_24h,
                "hit_ratio_24h": hit_ratio_24h,
                "breakdown": breakdown,
                "all_time_hits": all_time_hits,
            }
        finally:
            conn.close()

def get_cached_og(url_key: str) -> dict | None:
    with _lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM og_cache WHERE url_key = ?", (url_key,))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

def add_cached_og(url_key: str, title: str, image_path: str | None, warning: str | None = None, jina_markdown: str | None = None):
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO og_cache (url_key, title, image_path, warning, jina_markdown, created_at)
                VALUES (?, ?, ?, ?, ?, CAST(strftime('%s','now') AS INTEGER))
            ''', (url_key, title, image_path, warning, jina_markdown))
            conn.commit()
        finally:
            conn.close()

def get_or_create_url_hash(url: str) -> str:
    import hashlib
    clean_url = url.strip()
    url_hash = hashlib.md5(clean_url.encode("utf-8")).hexdigest()[:8]
    
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            
            # Check if already exists
            cursor.execute("SELECT url_key FROM url_hashes WHERE url = ?", (clean_url,))
            row = cursor.fetchone()
            if row:
                return row[0]
            
            candidate_key = url_hash
            attempts = 0
            while True:
                cursor.execute("SELECT url FROM url_hashes WHERE url_key = ?", (candidate_key,))
                existing_row = cursor.fetchone()
                if not existing_row:
                    break
                if existing_row[0] == clean_url:
                    break
                attempts += 1
                candidate_key = f"{url_hash[:-1]}{attempts}"[:8]
                
            cursor.execute("INSERT OR REPLACE INTO url_hashes (url_key, url) VALUES (?, ?)", (candidate_key, clean_url))
            conn.commit()
            return candidate_key
        finally:
            conn.close()

def get_url_by_hash(urlhash: str) -> str | None:
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT url FROM url_hashes WHERE url_key = ?", (urlhash,))
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

def add_exclusion(pattern: str):
    clean_pat = pattern.strip()
    if not clean_pat:
        return
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO url_exclusions (pattern) VALUES (?)", (clean_pat,))
            conn.commit()
        finally:
            conn.close()

def remove_exclusion(pattern: str):
    clean_pat = pattern.strip()
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM url_exclusions WHERE pattern = ?", (clean_pat,))
            conn.commit()
        finally:
            conn.close()

def list_exclusions() -> list[str]:
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT pattern FROM url_exclusions ORDER BY pattern ASC")
            rows = cursor.fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

def is_excluded(url: str) -> bool:
    clean_url = url.strip()
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT EXISTS(SELECT 1 FROM url_exclusions WHERE lower(?) LIKE '%' || lower(pattern) || '%')", (clean_url,))
            result = cursor.fetchone()[0]
            return bool(result)
        finally:
            conn.close()

def add_invidious_domain(domain: str):
    """Save an Invidious domain to database config."""
    set_config(f"invidious_domain_{domain.strip().lower()}", "1")

def remove_invidious_domain(domain: str):
    """Remove an Invidious domain from database config."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM config WHERE key = ?", (f"invidious_domain_{domain.strip().lower()}",))
            conn.commit()
        finally:
            conn.close()

def list_invidious_domains() -> list[str]:
    """List all registered Invidious domains from database config."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT key FROM config WHERE key LIKE 'invidious_domain_%'")
            rows = cursor.fetchall()
            prefix = "invidious_domain_"
            return [r[0][len(prefix):] for r in rows if r[0].startswith(prefix)]
        finally:
            conn.close()

def is_webpreview_disabled(chat_id: int) -> bool:
    """Check if webpreview auto-parsing is disabled in a chat."""
    try:
        val = get_config(f"webpreview_disabled_{chat_id}")
        return val == "1"
    except Exception:
        return False

def set_webpreview_disabled(chat_id: int, disabled: bool):
    """Disable or enable webpreview auto-parsing in a chat."""
    set_config(f"webpreview_disabled_{chat_id}", "1" if disabled else "0")

def get_chat_lang(chat_id: int) -> str:
    """Get preferred summary language for a chat (defaults to 'AUTO')."""
    try:
        val = get_config(f"chat_lang_{chat_id}")
        return val.strip().upper() if val and val.strip() else "AUTO"
    except Exception:
        return "AUTO"

def set_chat_lang(chat_id: int, lang: str):
    """Set preferred summary language for a chat."""
    set_config(f"chat_lang_{chat_id}", lang.strip().upper())

init_db()
