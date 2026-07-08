import os
import sqlite3
import threading

DB_PATH = os.getenv("DB_PATH", "webpreview.db")
_lock = threading.Lock()

def init_db():
    with _lock:
        conn = sqlite3.connect(DB_PATH)
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

        # URL short hash map table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS url_hashes (
                url_key TEXT PRIMARY KEY,
                url TEXT UNIQUE
            )
        ''')

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
        try:
            cursor.execute("ALTER TABLE og_cache ADD COLUMN warning TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE og_cache ADD COLUMN jina_markdown TEXT")
        except sqlite3.OperationalError:
            pass
        
        conn.commit()
        conn.close()

def set_config(key: str, value: str):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()
        conn.close()

def get_config(key: str) -> str:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

def get_admin_fingerprint():
    """Get the saved admin DC fingerprint."""
    return get_config("admin_dc_fingerprint")

def set_admin_fingerprint(fp):
    """Set the admin DC fingerprint."""
    set_config("admin_dc_fingerprint", fp)

def add_preview_log(chat_id: int, from_id: int, url: str, title: str, filesize: int, with_js: bool):
    """Record a preview generation in the history."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO preview_stats (chat_id, from_id, url, title, filesize, with_js) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, from_id, url, title, filesize, 1 if with_js else 0)
        )
        conn.commit()
        conn.close()

def get_stats() -> dict:
    """Get preview statistics."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
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
        
        conn.close()
        return {
            "total": total,
            "last_24h": last_24h,
            "total_size": total_size
        }

def increment_transport_sent(addr: str):
    """Increment the sent counter for a transport address."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO transport_stats (addr, msgs_sent, msgs_received, last_sent_at)
            VALUES (?, 1, 0, CAST(strftime('%s','now') AS INTEGER))
            ON CONFLICT(addr) DO UPDATE SET
                msgs_sent = msgs_sent + 1,
                last_sent_at = CAST(strftime('%s','now') AS INTEGER)
        ''', (addr,))
        conn.commit()
        conn.close()

def increment_transport_received(addr: str):
    """Increment the received counter for a transport address."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO transport_stats (addr, msgs_sent, msgs_received, last_received_at)
            VALUES (?, 0, 1, CAST(strftime('%s','now') AS INTEGER))
            ON CONFLICT(addr) DO UPDATE SET
                msgs_received = msgs_received + 1,
                last_received_at = CAST(strftime('%s','now') AS INTEGER)
        ''', (addr,))
        conn.commit()
        conn.close()

def get_all_transport_stats() -> list[dict]:
    """Get statistics for all tracked transports."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM transport_stats ORDER BY msgs_sent + msgs_received DESC")
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

def get_cached_preview(url_key: str) -> dict:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM url_cache WHERE url_key = ?", (url_key,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

def add_cached_preview(url_key: str, filepath: str, title: str, filesize: int):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO url_cache (url_key, filepath, title, filesize, created_at)
            VALUES (?, ?, ?, ?, CAST(strftime('%s','now') AS INTEGER))
        ''', (url_key, filepath, title, filesize))
        conn.commit()
        conn.close()

def clear_expired_cache(max_age_seconds: int):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM url_cache WHERE created_at < CAST(strftime('%s','now') AS INTEGER) - ?",
            (max_age_seconds,)
        )
        cursor.execute(
            "DELETE FROM og_cache WHERE created_at < CAST(strftime('%s','now') AS INTEGER) - ?",
            (max_age_seconds,)
        )
        conn.commit()
        conn.close()

def get_cached_og(url_key: str) -> dict | None:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM og_cache WHERE url_key = ?", (url_key,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

def add_cached_og(url_key: str, title: str, image_path: str | None, warning: str | None = None, jina_markdown: str | None = None):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO og_cache (url_key, title, image_path, warning, jina_markdown, created_at)
            VALUES (?, ?, ?, ?, ?, CAST(strftime('%s','now') AS INTEGER))
        ''', (url_key, title, image_path, warning, jina_markdown))
        conn.commit()
        conn.close()

def get_or_create_url_hash(url: str) -> str:
    import hashlib
    clean_url = url.strip()
    url_hash = hashlib.md5(clean_url.encode("utf-8")).hexdigest()[:8]
    
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Check if already exists
        cursor.execute("SELECT url_key FROM url_hashes WHERE url = ?", (clean_url,))
        row = cursor.fetchone()
        if row:
            conn.close()
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
        conn.close()
        return candidate_key

def get_url_by_hash(urlhash: str) -> str | None:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT url FROM url_hashes WHERE url_key = ?", (urlhash,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

def add_exclusion(pattern: str):
    clean_pat = pattern.strip()
    if not clean_pat:
        return
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO url_exclusions (pattern) VALUES (?)", (clean_pat,))
        conn.commit()
        conn.close()

def remove_exclusion(pattern: str):
    clean_pat = pattern.strip()
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM url_exclusions WHERE pattern = ?", (clean_pat,))
        conn.commit()
        conn.close()

def list_exclusions() -> list[str]:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT pattern FROM url_exclusions ORDER BY pattern ASC")
        rows = cursor.fetchall()
        conn.close()
        return [r[0] for r in rows]

def is_excluded(url: str) -> bool:
    clean_url = url.strip()
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT EXISTS(SELECT 1 FROM url_exclusions WHERE lower(?) LIKE '%' || lower(pattern) || '%')", (clean_url,))
        result = cursor.fetchone()[0]
        conn.close()
        return bool(result)

def add_invidious_domain(domain: str):
    """Save an Invidious domain to database config."""
    set_config(f"invidious_domain_{domain.strip().lower()}", "1")

def remove_invidious_domain(domain: str):
    """Remove an Invidious domain from database config."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM config WHERE key = ?", (f"invidious_domain_{domain.strip().lower()}",))
        conn.commit()
        conn.close()

def list_invidious_domains() -> list[str]:
    """List all registered Invidious domains from database config."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT key FROM config WHERE key LIKE 'invidious_domain_%'")
        rows = cursor.fetchall()
        conn.close()
        prefix = "invidious_domain_"
        return [r[0][len(prefix):] for r in rows if r[0].startswith(prefix)]

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

init_db()


