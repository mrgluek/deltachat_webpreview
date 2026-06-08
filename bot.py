import asyncio
import collections
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import contextlib
import urllib.request
import urllib.parse
import hashlib

from deltachat2 import events, MsgData
from deltabot_cli import BotCli

import database
from bs4 import BeautifulSoup
from readability import Document
from PIL import Image
import io
import base64

try:
    import lxml
    BS_PARSER = "lxml"
except ImportError:
    BS_PARSER = "html.parser"


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("webpreview_bot")

dc_cli = BotCli("webpreview")

# Global references
dc_bot_instance = None
dc_accid = None

# Delta Chat constants
DC_CONTACT_ID_SELF = 1

# Rate limiting: {from_id: last_request_timestamp}
_user_rate_limits: dict[int, float] = {}
RATE_LIMIT_SECONDS = 15

# Cache settings
CACHE_DIR = os.path.join("data", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)
CACHE_MAX_AGE = 3600  # 1 hour

VERSION = "2.0.0"
STANDARD_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
NON_MOZILLA_USER_AGENT = "AppleWebKit/605.1.15 (KHTML, like Gecko) Safari/605.1.15 deltachat-webpreview/1.0"

def compress_image(image_bytes: bytes, max_width=800, quality=70) -> bytes:
    """Compresses an image to WebP or JPEG, resizing if wider than max_width."""
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(image_bytes))
        
        # Determine output format and convert if necessary
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            img_format = "WEBP"
        else:
            img_format = "JPEG"
            if img.mode != "RGB":
                img = img.convert("RGB")
        
        # Resize if width exceeds max_width
        if img.width > max_width:
            ratio = max_width / float(img.width)
            new_height = int(float(img.height) * ratio)
            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
            
        out_io = io.BytesIO()
        img.save(out_io, format=img_format, quality=quality, optimize=True)
        return out_io.getvalue()
    except Exception as e:
        logger.warning(f"Failed to compress image: {e}")
        return image_bytes

def _download_image_bytes(image_url: str) -> bytes | None:
    """Downloads image bytes trying standard and fallback User-Agents."""
    for ua in [STANDARD_USER_AGENT, NON_MOZILLA_USER_AGENT]:
        try:
            req = urllib.request.Request(image_url, headers={'User-Agent': ua})
            with urllib.request.urlopen(req, timeout=5) as response:
                content_type = response.headers.get('Content-Type', '')
                if 'image' in content_type.lower() or response.status == 200:
                    return response.read()
        except Exception as e:
            logger.debug(f"Failed to fetch image {image_url} with UA {ua}: {e}")
    return None

def _download_page_html(url: str) -> tuple[str | None, str | None]:
    """
    Downloads page HTML using standard and fallback User-Agents.
    Returns (html_str, error_msg).
    """
    import urllib.request
    import urllib.error
    
    html_str = None
    error_msg = None
    
    for ua in [STANDARD_USER_AGENT, NON_MOZILLA_USER_AGENT]:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': ua})
            with urllib.request.urlopen(req, timeout=15) as response:
                content_type = response.headers.get('Content-Type', '')
                if 'text/html' not in content_type.lower():
                    continue
                html_bytes = response.read()
                decoded = html_bytes.decode('utf-8', errors='ignore')
                
                # Check for Anubis
                if "Protected by Anubis" in decoded or "anubis.techaro.lol" in decoded:
                    logger.warning(f"Anubis challenge detected in HTML with User-Agent: {ua}")
                    error_msg = "Blocked by Anubis protection"
                    continue
                    
                html_str = decoded
                error_msg = None
                break
        except urllib.error.HTTPError as e:
            error_msg = f"HTTP Error {e.code}: {e.reason}"
        except Exception as e:
            error_msg = str(e)
            
    return html_str, error_msg

READABILITY_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{title}}</title>
    <style>
        :root {{
            --bg-color: #f7f9fa;
            --text-color: #1a1a1a;
            --card-bg: #ffffff;
            --link-color: #0076ff;
            --border-color: #e1e8ed;
            --muted-color: #657786;
            --font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }}
        @media (prefers-color-scheme: dark) {{
            :root {{
                --bg-color: #15202b;
                --text-color: #f7f9fa;
                --card-bg: #192734;
                --link-color: #1b95e0;
                --border-color: #38444d;
                --muted-color: #8899a6;
            }}
        }}
        body {{
            background-color: var(--bg-color);
            color: var(--text-color);
            font-family: var(--font-family);
            line-height: 1.6;
            margin: 0;
            padding: 0;
            display: flex;
            justify-content: center;
        }}
        .container {{
            max-width: 700px;
            width: 100%;
            margin: 40px 20px;
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 40px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.05);
            box-sizing: border-box;
        }}
        @media (max-width: 600px) {{
            .container {{
                margin: 0;
                border-radius: 0;
                border: none;
                padding: 20px;
            }}
        }}
        header {{
            margin-bottom: 30px;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 20px;
        }}
        h1 {{
            font-size: 2.2rem;
            margin-top: 0;
            margin-bottom: 10px;
            line-height: 1.25;
        }}
        .meta {{
            font-size: 0.9rem;
            color: var(--muted-color);
        }}
        .meta a {{
            color: var(--link-color);
            text-decoration: none;
        }}
        .meta a:hover {{
            text-decoration: underline;
        }}
        .content img {{
            max-width: 100%;
            height: auto;
            border-radius: 8px;
            margin: 20px 0;
            display: block;
        }}
        .content a {{
            color: var(--link-color);
            text-decoration: none;
        }}
        .content a:hover {{
            text-decoration: underline;
        }}
        .content p {{
            margin-bottom: 1.5em;
        }}
        .content blockquote {{
            border-left: 4px solid var(--link-color);
            padding-left: 20px;
            margin: 20px 0;
            color: var(--muted-color);
            font-style: italic;
        }}
        .content pre {{
            background-color: var(--bg-color);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 15px;
            overflow-x: auto;
            font-family: SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace;
            font-size: 0.9rem;
        }}
        .content code {{
            font-family: SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace;
            background-color: var(--bg-color);
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 0.9rem;
        }}
        .content pre code {{
            padding: 0;
            background-color: transparent;
            border-radius: 0;
        }}
    </style>
</head>
<body>
    <article class="container">
        <header>
            <h1>{title}</h1>
            <div class="meta">
                Original URL: <a href="{url}" target="_blank" rel="noopener noreferrer">{domain}</a>
            </div>
        </header>
        <div class="content">
            {content}
        </div>
    </article>
</body>
</html>
"""

def _generate_readability_preview(url: str, output_path: str) -> tuple[bool, str]:
    """
    Generates a readability preview file at output_path.
    Returns (success, error_msg_or_title).
    """
    try:
        from readability import Document
        from bs4 import BeautifulSoup
        import base64
        import urllib.parse
        
        html_str, err = _download_page_html(url)
        if not html_str:
            return False, err or "Failed to download page content"
            
        doc = Document(html_str)
        title = doc.title() or "Webpage Preview"
        summary = doc.summary()
        
        if not summary or len(summary.strip()) < 50:
            return False, "Readability failed to extract meaningful content from this page"
            
        soup = BeautifulSoup(summary, BS_PARSER)
        
        # Inline images with compression
        for img in soup.find_all('img'):
            img_src = img.get('src')
            if not img_src or img_src.startswith('data:'):
                continue
                
            absolute_img_url = urllib.parse.urljoin(url, img_src)
            try:
                img_bytes = _download_image_bytes(absolute_img_url)
                if img_bytes:
                    compressed = compress_image(img_bytes, max_width=800, quality=70)
                    mime_type = "image/webp" if compressed.startswith(b"RIFF") else "image/jpeg"
                    b64_str = base64.b64encode(compressed).decode('utf-8')
                    img['src'] = f"data:{mime_type};base64,{b64_str}"
            except Exception as img_err:
                logger.warning(f"Could not inline/compress image {absolute_img_url}: {img_err}")
                
        # Format templates
        domain = urllib.parse.urlparse(url).netloc or "webpage"
        final_html = READABILITY_HTML_TEMPLATE.format(
            title=title,
            url=url,
            domain=domain,
            content=str(soup)
        )
        
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(final_html)
            
        return True, title
    except Exception as e:
        logger.error(f"Error in _generate_readability_preview: {e}")
        return False, str(e)

def compress_monolith_html(filepath: str):
    """
    Reads monolith HTML, finds all base64-encoded img tags,
    compresses the images, and writes them back.
    """
    try:
        from bs4 import BeautifulSoup
        import base64
        
        if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
            return
            
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
            
        soup = BeautifulSoup(content, BS_PARSER)
        modified = False
        
        for img in soup.find_all('img'):
            img_src = img.get('src', '')
            if img_src.startswith('data:image/'):
                try:
                    # Extract mime type and base64 string
                    if ',' in img_src:
                        header, base64_data = img_src.split(',', 1)
                        img_bytes = base64.b64decode(base64_data)
                        
                        # Only compress if it is reasonably large, e.g. > 10KB
                        if len(img_bytes) > 10 * 1024:
                            compressed = compress_image(img_bytes, max_width=800, quality=70)
                            if len(compressed) < len(img_bytes):
                                mime_type = "image/webp" if compressed.startswith(b"RIFF") else "image/jpeg"
                                b64_str = base64.b64encode(compressed).decode('utf-8')
                                img['src'] = f"data:{mime_type};base64,{b64_str}"
                                modified = True
                except Exception as e:
                    logger.warning(f"Error compressing monolith base64 image: {e}")
                    
        if modified:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(str(soup))
            logger.info(f"Successfully compressed images in monolith HTML file {filepath}")
    except Exception as e:
        logger.error(f"Error post-processing monolith file {filepath}: {e}")

def _is_anubis_blocked(filepath: str) -> bool:

    """Check if the downloaded page is an Anubis challenge/block page."""
    try:
        if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
            return False
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            # Read first 128KB to detect Anubis signature
            content = f.read(128 * 1024)
            signatures = [
                "Protected by Anubis",
                "Testing to determine if you are a bot!",
                "anubis.techaro.lol"
            ]
            for sig in signatures:
                if sig in content:
                    return True
    except Exception as e:
        logger.warning(f"Error checking Anubis status on file {filepath}: {e}")
    return False

_processed_msg_ids = set()
_processed_msg_lock = threading.Lock()

def _is_duplicate_msg(msg_id: int, handler: str) -> bool:
    with _processed_msg_lock:
        key = f"{handler}_{msg_id}"
        if key in _processed_msg_ids:
            return True
        _processed_msg_ids.add(key)
        if len(_processed_msg_ids) > 1000:
            # Simple cleanup, keep only the latest 500 to avoid memory leak
            latest = list(_processed_msg_ids)[-500:]
            _processed_msg_ids.clear()
            _processed_msg_ids.update(latest)
        return False

# ── Admin helpers (matching other bots) ──

def _get_contact_fingerprint(bot, accid, contact_id, contact=None):
    self_fps = set()
    try:
        bot_addrs = []
        bot_addr = bot.rpc.get_config(accid, "addr")
        if bot_addr: bot_addrs.append(bot_addr.lower().strip())
            
        try:
            transports = bot.rpc.list_transports(accid)
            for t in transports:
                t_addr = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
                if t_addr: bot_addrs.append(t_addr.lower().strip())
        except: pass
        
        if bot_addrs:
            for args in [(accid, contact_id), (contact_id,)]:
                try:
                    enc_info_self = bot.rpc.get_contact_encryption_info(*args)
                    if enc_info_self:
                        blocks = re.split(r'\n\s*\n', enc_info_self.strip())
                        for block in blocks:
                            if any(a in block.lower() for a in bot_addrs):
                                matches = re.findall(r'[0-9a-fA-F]{32,64}', "".join(block.split()).replace(':', ''))
                                self_fps.update(m.upper() for m in matches)
                        break
                except Exception:
                    continue
        if self_fps:
            logger.debug(f"Detected bot's own fingerprints from enc_info: {[f[-8:] for f in self_fps]}")
    except Exception as e:
        logger.error(f"Error detecting self-fingerprint: {e}")

    # Filter fingerprints from contact object
    if contact:
        get_val = getattr(contact, 'get', lambda k: getattr(contact, k, None))
        for attr in ['fingerprint', 'key_fingerprint', 'public_key']:
            val = get_val(attr)
            if val:
                matches = re.findall(r'[0-9a-fA-F]{32,64}', str(val).replace(' ', '').replace(':', ''))
                valid_matches = [m.upper() for m in matches if m.upper() not in self_fps]
                if valid_matches:
                    return ",".join(valid_matches)
    try:
        fp = bot.rpc.get_contact_config(accid, contact_id, "fp")
        if fp and fp.upper().replace(' ', '') not in self_fps:
            return fp.upper().replace(' ', '')
    except Exception:
        pass

    for args in [(accid, contact_id), (contact_id,)]:
        try:
            enc_info = bot.rpc.get_contact_encryption_info(*args)
            if enc_info:
                cleaned = "".join(enc_info.split()).replace(':', '')
                matches = re.findall(r'[0-9a-fA-F]{32,64}', cleaned)
                valid_matches = [m.upper() for m in matches if m.upper() not in self_fps]
                if valid_matches:
                    return ",".join(valid_matches)
        except Exception:
            continue
    return None

def _is_dc_admin(bot, accid, contact_id) -> bool:
    """Check if the given contact is the bot administrator (by email or fingerprint)."""
    if contact_id <= 9:  # System contacts are never admin
        return False
    try:
        contact = bot.rpc.get_contact(accid, contact_id)
        sender_email = contact.address
        
        # 1. Check fingerprint if available
        c_fp = _get_contact_fingerprint(bot, accid, contact_id, contact=contact)
        admin_fp = database.get_admin_fingerprint()
        if admin_fp:
            if c_fp:
                if admin_fp.upper() in c_fp.upper().split(','):
                    return True
        
        # 2. Fallback to email
        admin_email = database.get_config("admin_dc_email")
        if admin_email and sender_email and admin_email.lower().strip() == sender_email.lower().strip():
            # Auto-upgrade: if fingerprint became available after initial /initadmin, save it now!
            if not admin_fp and c_fp:
                first_fp = c_fp.split(',')[0]
                database.set_admin_fingerprint(first_fp)
                logger.info(f"Automatically upgraded admin {admin_email} with fingerprint {first_fp[-8:]}")
            return True
            
    except Exception as e:
        logger.error(f"Critical error in admin check: {e}")
    return False

def _is_rate_limited(bot, accid, from_id) -> bool:
    """Return True if the user is rate-limited, otherwise record request and return False."""
    if _is_dc_admin(bot, accid, from_id):
        return False
    now = time.time()
    last = _user_rate_limits.get(from_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    _user_rate_limits[from_id] = now
    return False

def _is_yt_bot_in_chat(bot, accid, chat_id) -> bool:
    """Check if the YT Bot is present in the specified chat."""
    try:
        contacts = bot.rpc.get_chat_contacts(accid, chat_id)
        if not contacts:
            return False
        for contact_id in contacts:
            if contact_id <= 9:  # Skip system contacts
                continue
            contact = bot.rpc.get_contact(accid, contact_id)
            display_name = getattr(contact, "display_name", "") or getattr(contact, "displayname", "")
            if not display_name and hasattr(contact, "get"):
                display_name = contact.get("display_name") or contact.get("displayname") or ""
            
            display_name_str = str(display_name).lower()
            if "yt bot" in display_name_str or "youtube bot" in display_name_str:
                return True
    except Exception as e:
        logger.warning(f"Error checking if YT Bot is in chat: {e}")
    return False

def _is_handled_by_yt_bot(url: str) -> bool:
    """Return True if the URL is of a media type handled by YT Bot."""
    url_lower = url.lower()
    # 1. YouTube URLs
    if "youtube.com" in url_lower or "youtu.be" in url_lower or "youtube-nocookie.com" in url_lower:
        return True
    # 2. Yandex Music URLs
    if "music.yandex." in url_lower:
        return True
    # 3. Major video/audio hosting sites supported by YT Bot
    other_media_domains = [
        "vimeo.com", "vk.com/video", "vkvideo.ru", "rutube.ru", "soundcloud.com", 
        "tiktok.com", "twitch.tv", "bilibili.com", "dzen.ru", "ok.ru", "coub.com"
    ]
    if any(domain in url_lower for domain in other_media_domains):
        return True
    return False

# ── Message sending helpers with Failover and Stats ──

def _send(bot, accid, chat_id, text, file=None):
    """Send a message and track transport stats with failover."""
    msg_data = MsgData(text=text)
    if file:
        msg_data.file = file
        
    try:
        transports = bot.rpc.list_transports(accid)
        max_attempts = max(2, len(transports))
    except Exception:
        transports = []
        max_attempts = 2

    for attempt in range(max_attempts):
        try:
            msg_id = bot.rpc.send_msg(accid, chat_id, msg_data)
            
            # Track success stats
            try:
                addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr")
                if addr:
                    database.increment_transport_sent(addr)
            except: pass
            
            return msg_id
        except Exception as e:
            error_str = str(e).lower()
            logger.warning(f"Attempt {attempt + 1} failed to send message: {e}")
            
            transport_errors = ["network", "timeout", "connection", "unreachable", "smtp", "status 0", "socket", "refused", "auth"]
            
            if attempt < max_attempts - 1 and any(err in error_str for err in transport_errors):
                try:
                    current_addr = bot.rpc.get_config(accid, "addr")
                    if not transports:
                        transports = bot.rpc.list_transports(accid)
                    
                    if len(transports) > 1:
                        for t in transports:
                            t_addr = t.get('addr') if isinstance(t, dict) else getattr(t, 'addr', None)
                            if t_addr and t_addr != current_addr:
                                logger.info(f"Switching transport from {current_addr} to backup: {t_addr}")
                                try:
                                    bot.rpc.set_config(accid, "addr", t_addr)
                                    t_pw = t.get('password') if isinstance(t, dict) else getattr(t, 'password', None)
                                    if t_pw:
                                        bot.rpc.set_config(accid, "mail_pw", t_pw)
                                    time.sleep(2)
                                    break 
                                except Exception as set_e:
                                    logger.error(f"Failed to switch transport: {set_e}")
                                    continue
                except: pass
            else:
                break

    logger.error(f"Final failure sending msg to chat {chat_id} after {max_attempts} attempts.")
    return None

def _react(bot, accid, msg_id, reaction):
    """Add a reaction to a message."""
    try:
        bot.rpc.send_reaction(accid, msg_id, [reaction] if reaction else [])
    except Exception as e:
        logger.warning(f"Failed to send reaction {reaction}: {e}")

# ── Monolith Worker Logic ──

def _extract_title(filepath: str) -> str | None:
    """Extract page title from HTML file."""
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            # Read first 256KB to find title
            head = f.read(256 * 1024)
            m = re.search(r'<title[^>]*>(.*?)</title>', head, re.IGNORECASE | re.DOTALL)
            if m:
                import html
                return html.unescape(m.group(1).strip())
    except Exception as e:
        logger.warning(f"Error reading title from file: {e}")
    return None

def _safe_filename(domain: str, with_js: bool) -> str:
    """Generate a safe, filesystem-friendly filename using domain and timestamp."""
    # Replace dots and non-alphanumeric characters with underscores
    clean_domain = re.sub(r'[^a-zA-Z0-9-]', "_", domain)
    clean_domain = clean_domain.strip('_')
    if not clean_domain:
        clean_domain = "page"
        
    js_suffix = "_js" if with_js else ""
    timestamp = int(time.time())
    return f"webpreview_{clean_domain}{js_suffix}_{timestamp}.html"

def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"

async def _run_monolith_process(cmd: list) -> tuple[int, str]:
    """Execute monolith process with timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=35)
        return proc.returncode, stderr.decode(errors='replace').strip()
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except: pass
        return -1, "Operation timed out (35 second limit exceeded)"
    except Exception as e:
        return -99, str(e)

def _get_og_preview_data(url: str) -> tuple[str, str | None]:
    """
    Fetches the URL and extracts og:title (or fallback title) and og:image URL.
    Returns (title, og_image_url).
    """
    def parse_html(html_content: str) -> tuple[str | None, str | None]:
        # 1. Extract title
        title = None
        title_m = re.search(r'<meta[^>]*(?:property|name)=["\']og:title["\'][^>]*content=["\'](.*?)["\']', html_content, re.IGNORECASE)
        if not title_m:
            title_m = re.search(r'<meta[^]*content=["\'](.*?)["\'][^>]*(?:property|name)=["\']og:title["\']', html_content, re.IGNORECASE)
        if title_m:
            import html
            title = html.unescape(title_m.group(1).strip())
        
        if not title:
            # Fallback to <title>
            title_m = re.search(r'<title[^>]*>(.*?)</title>', html_content, re.IGNORECASE | re.DOTALL)
            if title_m:
                import html
                title = html.unescape(title_m.group(1).strip())
        
        # 2. Extract og:image
        image_url = None
        img_m = re.search(r'<meta[^>]*(?:property|name)=["\']og:image["\'][^>]*content=["\'](.*?)["\']', html_content, re.IGNORECASE)
        if not img_m:
            img_m = re.search(r'<meta[^]*content=["\'](.*?)["\'][^>]*(?:property|name)=["\']og:image["\']', html_content, re.IGNORECASE)
        if not img_m:
            img_m = re.search(r'<meta[^>]*(?:property|name)=["\']twitter:image["\'][^>]*content=["\'](.*?)["\']', html_content, re.IGNORECASE)
        
        if img_m:
            import html
            image_url = html.unescape(img_m.group(1).strip())
            image_url = urllib.parse.urljoin(url, image_url)

            
        return title, image_url

    def is_anubis_html(html_content: str) -> bool:
        signatures = [
            "Protected by Anubis",
            "Testing to determine if you are a bot!",
            "anubis.techaro.lol"
        ]
        return any(sig in html_content for sig in signatures)

    import urllib.error
    
    hard_failure_code = None

    # First attempt with standard User-Agent
    html_head = None
    try:
        req = urllib.request.Request(
            url, 
            headers={'User-Agent': STANDARD_USER_AGENT}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            content_type = response.headers.get('Content-Type', '')
            if 'text/html' in content_type.lower():
                # Read first 512KB
                html_bytes = response.read(512 * 1024)
                html_head = html_bytes.decode('utf-8', errors='ignore')
    except urllib.error.HTTPError as e:
        logger.warning(f"Standard fetch failed/blocked for {url} in OG parse: HTTP Error {e.code}: {e.reason}. Trying non-Mozilla User-Agent fallback...")
        if e.code in (401, 403, 404):
            hard_failure_code = e.code
    except Exception as e:
        logger.warning(f"Standard fetch failed/blocked for {url} in OG parse: {e}. Trying non-Mozilla User-Agent fallback...")

    # If first fetch succeeded, check if it's an Anubis block page
    if html_head is not None and is_anubis_html(html_head):
        logger.warning(f"Anubis challenge detected in OG fetch for {url} with standard User-Agent. Triggering fallback...")
        html_head = None  # Force retry

    # Retry with non-Mozilla User-Agent if standard fetch failed or was blocked
    if html_head is None:
        try:
            req = urllib.request.Request(
                url, 
                headers={'User-Agent': NON_MOZILLA_USER_AGENT}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                content_type = response.headers.get('Content-Type', '')
                if 'text/html' in content_type.lower():
                    # Read first 512KB
                    html_bytes = response.read(512 * 1024)
                    html_head = html_bytes.decode('utf-8', errors='ignore')
                # Succeeded, clear any recorded hard failure code
                hard_failure_code = None
        except urllib.error.HTTPError as e:
            logger.warning(f"Fallback fetch failed for {url} with non-Mozilla User-Agent: HTTP Error {e.code}: {e.reason}")
            if e.code in (401, 403, 404):
                hard_failure_code = e.code
        except Exception as e:
            logger.warning(f"Fallback fetch failed for {url} with non-Mozilla User-Agent: {e}")

    if hard_failure_code is not None:
        return "__FAILED_BLOCK__", f"HTTP {hard_failure_code}"

    # Parse and extract
    if html_head is not None:
        title, image_url = parse_html(html_head)
        if title:
            return title, image_url

    return urllib.parse.urlparse(url).netloc or "Webpage", None

def _check_url_headers(url: str) -> tuple[str | None, int | str | None, str | None]:
    """
    Checks Content-Length and Content-Type using a lightweight GET request.
    Does standard + fallback User-Agent attempts if needed.
    Returns (error_type, size_or_code, content_type).
    Where error_type can be:
      - "SIZE_LIMIT": Content-Length exceeds 10 MB limit
      - "BINARY_TYPE": Content-Type is a binary/media format
      - "HTTP_ERROR": Hard HTTP failure (e.g. 401, 403, 404)
      - None: check passed
    """
    def check_response(response) -> tuple[str | None, int | None, str | None]:
        content_type = response.headers.get('Content-Type', '').lower()
        
        # 1. Content-Length check (10 MB limit)
        content_length_str = response.headers.get('Content-Length')
        content_length = None
        if content_length_str:
            try:
                content_length = int(content_length_str)
            except ValueError:
                pass
                
        if content_length is not None and content_length > 10 * 1024 * 1024:
            return "SIZE_LIMIT", content_length, content_type
            
        # 2. Content-Type check
        # Non-webpage/binary files (ZIP, MP4, PDFs, Octet-stream, audio, video, zip, rar, tar, exe, dmg, etc.)
        binary_types = [
            "application/zip", "application/x-zip-compressed", "application/octet-stream",
            "video/", "audio/", "application/pdf", "application/x-rar-compressed",
            "application/x-tar", "application/x-executable", "application/x-msdownload",
            "application/x-apple-diskimage"
        ]
        if any(bt in content_type for bt in binary_types) and 'text/html' not in content_type:
            return "BINARY_TYPE", content_length, content_type
            
        return None, content_length, content_type

    import urllib.error
    
    # Standard attempt
    try:
        req = urllib.request.Request(url, headers={'User-Agent': STANDARD_USER_AGENT})
        with urllib.request.urlopen(req, timeout=5) as response:
            return check_response(response)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            # Record hard block temporarily, will try fallback
            pass
        else:
            return "HTTP_ERROR", e.code, None
    except Exception:
        pass

    # Fallback attempt
    try:
        req = urllib.request.Request(url, headers={'User-Agent': NON_MOZILLA_USER_AGENT})
        with urllib.request.urlopen(req, timeout=5) as response:
            return check_response(response)
    except urllib.error.HTTPError as e:
        return "HTTP_ERROR", e.code, None
    except Exception as e:
        return "HTTP_ERROR", -1, str(e)

def _download_cached_image(image_url: str, urlhash: str) -> str | None:
    """
    Downloads an image URL and saves it in the persistent cache directory.
    Returns absolute path of the cached file, or None if failed.
    """
    # Try standard User-Agent first
    response_data = None
    content_type = ""
    try:
        req = urllib.request.Request(
            image_url, 
            headers={'User-Agent': STANDARD_USER_AGENT}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            content_type = response.headers.get('Content-Type', '')
            if 'image' in content_type.lower():
                response_data = response.read()
    except Exception as e:
        logger.warning(f"Standard fetch failed for image {image_url}: {e}. Retrying with non-Mozilla User-Agent...")

    # Fallback to non-Mozilla User-Agent
    if response_data is None:
        try:
            req = urllib.request.Request(
                image_url, 
                headers={'User-Agent': NON_MOZILLA_USER_AGENT}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                content_type = response.headers.get('Content-Type', '')
                if 'image' in content_type.lower():
                    response_data = response.read()
        except Exception as e:
            logger.warning(f"Fallback fetch failed for image {image_url}: {e}")

    if response_data is not None:
        try:
            ext = ".jpg"
            if "png" in content_type.lower():
                ext = ".png"
            elif "webp" in content_type.lower():
                ext = ".webp"
            elif "gif" in content_type.lower():
                ext = ".gif"
                
            cached_filename = f"og_{urlhash}{ext}"
            cached_path = os.path.join(CACHE_DIR, cached_filename)
            
            with open(cached_path, "wb") as f:
                f.write(response_data)
                
            return cached_path
        except Exception as e:
            logger.warning(f"Failed to write downloaded image to {cached_path}: {e}")
            
    return None

def _do_group_link_preview(bot, accid, chat_id, from_id, url: str):
    """Fetches OG data, downloads banner image, and sends preview options to group (with 1h cache)."""
    try:
        # 1. Skip if the URL matches exclusion pattern
        if database.is_excluded(url):
            logger.info(f"URL excluded from preview: {url}")
            return

        # 2. Get or create short hash in SQLite database
        urlhash = database.get_or_create_url_hash(url)
        
        # 3. Check Cache
        cached = database.get_cached_og(urlhash)
        if cached:
            created_at = cached.get("created_at", 0)
            if time.time() - created_at < CACHE_MAX_AGE:
                cached_title = cached.get("title", "")
                cached_image_path = cached.get("image_path")
                
                # Suppress if cached as a failed block
                if cached_title == "__FAILED_BLOCK__":
                    logger.info(f"Suppressing group preview due to cached hard failure block for {url}")
                    return
                
                # Verify that if there is a cached image path, the file still exists on disk
                if not cached_image_path or os.path.exists(cached_image_path):
                    logger.info(f"OG Cache hit for group preview: {url}")
                    caption = (
                        f"🌐 {cached_title}\n\n"
                        f"🔗 {url}\n\n"
                        f"[ 🖥️ /preview_{urlhash} ]\n\n"
                        f"[ 💾 /archive_{urlhash} ]"
                    )
                    if cached_image_path:
                        _send(bot, accid, chat_id, caption, file=cached_image_path)
                    else:
                        _send(bot, accid, chat_id, caption)
                    return
        
        # 4. Cache Miss - Fetch OG tags
        logger.info(f"OG Cache miss for group preview: {url}. Fetching from network.")
        title, image_url = _get_og_preview_data(url)
        
        if title == "__FAILED_BLOCK__":
            # Cache failure for 1 hour to prevent redundant requests
            database.add_cached_og(urlhash, "__FAILED_BLOCK__", image_url)
            logger.warning(f"Suppressing group preview for {url} due to hard failure block: {image_url}")
            return
        
        # 5. Format caption
        caption = (
            f"🌐 {title}\n\n"
            f"🔗 {url}\n\n"
            f"[ 🖥️ /preview_{urlhash} ]\n\n"
            f"[ 💾 /archive_{urlhash} ]"
        )
        
        # 6. Download image if exists, saving to persistent cache folder
        img_cache_path = None
        if image_url:
            img_cache_path = _download_cached_image(image_url, urlhash)
            
        # 7. Add to SQLite cache
        database.add_cached_og(urlhash, title, img_cache_path)
        
        # 8. Send to group
        if img_cache_path and os.path.exists(img_cache_path):
            _send(bot, accid, chat_id, caption, file=img_cache_path)
        else:
            _send(bot, accid, chat_id, caption)
            
    except Exception as e:
        logger.error(f"Error in _do_group_link_preview: {e}")


def _do_preview(bot, accid, chat_id, req_msg_id, from_id, url: str, mode: str):
    """Run preview/archive generation in background thread."""
    # 0. Check exclusions first!
    if database.is_excluded(url):
        logger.info(f"Exclusion hit for URL: {url} in chat {chat_id}")
        _react(bot, accid, req_msg_id, "⚠️")
        _send(bot, accid, chat_id, f"⚠️ This URL is in the exclusion list.")
        return

    # Check cache for hard block fast rejection
    urlhash = database.get_or_create_url_hash(url)
    cached_og = database.get_cached_og(urlhash)
    
    # If not in cache, do a fast pre-check to populate the cache and avoid network overhead on blocked sites!
    if not cached_og:
        logger.info(f"Pre-checking URL status for {url}...")
        og_title, og_image = _get_og_preview_data(url)
        if og_title == "__FAILED_BLOCK__":
            database.add_cached_og(urlhash, "__FAILED_BLOCK__", og_image)
            cached_og = {"title": "__FAILED_BLOCK__", "image_path": og_image}
        else:
            database.add_cached_og(urlhash, og_title, og_image)
            cached_og = {"title": og_title, "image_path": og_image}
            
    if cached_og and cached_og.get("title") == "__FAILED_BLOCK__":
        reason = cached_og.get("image_path") or "HTTP 403 Forbidden"
        if "HTTP 403" in reason:
            reason = "HTTP 403 Forbidden"
        elif "HTTP 401" in reason:
            reason = "HTTP 401 Unauthorized"
        elif "HTTP 404" in reason:
            reason = "HTTP 404 Not Found"
            
        logger.warning(f"Fast-rejecting manual preview request for {url} because website is blocking bot requests ({reason}).")
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id, f"❌ Failed to generate web preview.\nReason: Website is blocking bot requests ({reason}).")
        return

    import hashlib
    url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
    cache_key = f"{url_hash}_{mode}"

    # 0. Check cache
    cached = database.get_cached_preview(cache_key)
    if cached:
        created_at = cached.get("created_at", 0)
        filepath = cached.get("filepath", "")
        title = cached.get("title", "")
        filesize = cached.get("filesize", 0)
        
        if time.time() - created_at < CACHE_MAX_AGE and os.path.exists(filepath):
            logger.info(f"Cache hit for URL: {url} (mode={mode}). Sending cached preview: {filepath}")
            _react(bot, accid, req_msg_id, "⏳")
            
            caption = f"{title}\n\n🔗 {url}"
            _send(bot, accid, chat_id, caption, file=filepath)
            _react(bot, accid, req_msg_id, "☑️")
            database.add_preview_log(chat_id, from_id, url, title, filesize, 1 if mode == "archive" else 0)
            return

    # 0.5 Pre-check URL Content-Length and Content-Type to avoid downloading heavy/binary payloads
    err_type, val, ctype = _check_url_headers(url)
    if err_type == "SIZE_LIMIT":
        size_str = _format_size(val) if isinstance(val, int) else "unknown size"
        logger.warning(f"Rejecting manual preview for {url} because declared Content-Length {size_str} exceeds 10 MB limit.")
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id, f"❌ Failed to generate web preview.\nReason: The remote file size ({size_str}) exceeds the limit of 10 MB.")
        return
    elif err_type == "BINARY_TYPE":
        logger.warning(f"Rejecting manual preview for {url} because Content-Type is a binary/media format: {ctype}.")
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id, f"❌ Failed to generate web preview.\nReason: The target URL points to a binary or media file ({ctype or 'unknown type'}), not a webpage.")
        return
    elif err_type == "HTTP_ERROR" and isinstance(val, int) and val in (401, 403, 404):
        logger.warning(f"Rejecting manual preview for {url} due to HTTP {val} returned during headers check.")
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id, f"❌ Failed to generate web preview.\nReason: Website is blocking bot requests (HTTP {val}).")
        return

    logger.info(f"Starting generation for URL: {url} (mode={mode}) in chat {chat_id}")
    
    # 1. React with loading icon
    _react(bot, accid, req_msg_id, "⏳")

    domain = urllib.parse.urlparse(url).netloc or "webpage"
    tmpdir = tempfile.mkdtemp(prefix="webpreview_")
    output_path = os.path.join(tmpdir, "output.html")

    if mode == "readability":
        success, res = _generate_readability_preview(url, output_path)
        if not success:
            logger.error(f"Readability failed for URL {url}: {res}")
            _react(bot, accid, req_msg_id, "❌")
            _send(bot, accid, chat_id, f"❌ Failed to generate readability web preview.\nReason: {res}")
            shutil.rmtree(tmpdir, ignore_errors=True)
            return
        title = res
    else:
        # Monolith compilation
        cmd = ["monolith", "-e", "-t", "30"]
        # In archive mode, JS is enabled (no -j).
        cmd.extend(["-u", STANDARD_USER_AGENT])
        cmd.extend([url, "-o", output_path])

        start_time = time.time()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            code, err = loop.run_until_complete(_run_monolith_process(cmd))
        finally:
            loop.close()
        
        # Self-healing retry for Anubis block
        if code == 0 and os.path.exists(output_path) and _is_anubis_blocked(output_path):
            logger.warning(f"Anubis challenge detected for {url}. Retrying with non-Mozilla user agent...")
            try:
                os.remove(output_path)
            except Exception as remove_err:
                logger.warning(f"Error removing Anubis-blocked output: {remove_err}")
                
            cmd = ["monolith", "-e", "-t", "30"]
            cmd.extend(["-u", NON_MOZILLA_USER_AGENT])
            cmd.extend([url, "-o", output_path])
            
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                code, err = loop.run_until_complete(_run_monolith_process(cmd))
            finally:
                loop.close()
        
        if code != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            logger.error(f"Monolith failed with code {code} for URL {url}. Error: {err}")
            _react(bot, accid, req_msg_id, "❌")
            _send(bot, accid, chat_id, f"❌ Failed to generate web archive.\nReason: {err or 'Empty or missing output file.'}")
            shutil.rmtree(tmpdir, ignore_errors=True)
            return

        # Post-compress monolith inlined images to shrink the size!
        compress_monolith_html(output_path)
        title = _extract_title(output_path) or domain

    # Check compiled/preview size limit (50 MB)
    compiled_size = os.path.getsize(output_path)
    if compiled_size > 50 * 1024 * 1024:
        size_str = _format_size(compiled_size)
        logger.warning(f"Rejecting generated output for {url} because size {size_str} exceeds 50 MB limit.")
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id, f"❌ Failed to generate web preview/archive.\nReason: The generated page size ({size_str}) exceeds the maximum delivery limit of 50 MB.")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return

    try:
        # 4. Extract clean filename
        safe_fname = _safe_filename(domain, mode == "archive")
        cache_path = os.path.join(CACHE_DIR, safe_fname)
        
        # 5. Move to persistent cache for transfer
        shutil.move(output_path, cache_path)
        filesize = os.path.getsize(cache_path)
        
        # Cache preview in database
        database.add_cached_preview(cache_key, cache_path, title, filesize)
        
        # 6. Format caption
        caption = f"{title}\n\n🔗 {url}"
        
        # 7. Send attachment
        _send(bot, accid, chat_id, caption, file=cache_path)
        _react(bot, accid, req_msg_id, "☑️")
        
        # 8. Store log stats
        database.add_preview_log(chat_id, from_id, url, title, filesize, 1 if mode == "archive" else 0)
        
    except Exception as e:
        logger.error(f"Error packing preview file: {e}")
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id, f"❌ Error packing preview file: {e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── Preview Trigger Handler ──

def _handle_preview_command(bot, accid, event, mode: str):
    """Processes `/preview`, `/previewjs` and `/archive` triggers."""
    msg = event.msg
    
    if _is_duplicate_msg(msg.id, "preview"):
        return

    # Check Rate Limit
    if _is_rate_limited(bot, accid, msg.from_id):
        _react(bot, accid, msg.id, "⏱")
        return

    # Extract target URL
    url = ""
    payload = event.payload.strip() if event.payload else ""
    
    # 1. Check if we have an explicit URL payload
    if payload:
        url_match = re.search(r'(https?://[^\s<>"]+)', payload)
        if url_match:
            url = url_match.group(1).rstrip('.,;:)!?')
            
    # 2. Check if this is a quote reply and no explicit payload is given
    else:
        quote = getattr(msg, "quote", None) or (msg.get("quote") if isinstance(msg, dict) else None)
        if quote:
            quoted_text = ""
            if isinstance(quote, dict):
                quoted_text = quote.get("text", "")
            else:
                quoted_text = getattr(quote, "text", "")
                
            if quoted_text:
                url_match = re.search(r'(https?://[^\s<>"]+)', quoted_text)
                if url_match:
                    url = url_match.group(1).rstrip('.,;:)!?')

    # If no URL resolved
    if not url:
        _send(bot, accid, msg.chat_id, 
              "Usage:\n"
              "• `/preview <url>` — Save page (compressed reader-mode, recommended)\n"
              "• `/archive <url>` — Save page (full monolith with JS enabled)\n"
              "• Reply `/preview` or `/archive` to another message containing a link.")
        return

    # Spawn thread to run in background
    t = threading.Thread(
        target=_do_preview, 
        args=(bot, accid, msg.chat_id, msg.id, msg.from_id, url, mode), 
        daemon=True
    )
    t.start()


# ── Command Listeners ──

@dc_cli.on(events.NewMessage(command="/preview", is_bot=None))
def preview_command(bot, accid, event):
    if accid != dc_accid:
        return
    text = (event.msg.text or "").strip()
    if not re.match(r"^/preview(?:\s|$)", text):
        return
    _handle_preview_command(bot, accid, event, mode="readability")

@dc_cli.on(events.NewMessage(command="/archive", is_bot=None))
def archive_command(bot, accid, event):
    if accid != dc_accid:
        return
    text = (event.msg.text or "").strip()
    if not re.match(r"^/archive(?:\s|$)", text):
        return
    _handle_preview_command(bot, accid, event, mode="archive")

@dc_cli.on(events.NewMessage(command="/previewjs", is_bot=None))
def previewjs_command(bot, accid, event):
    if accid != dc_accid:
        return
    text = (event.msg.text or "").strip()
    if not re.match(r"^/previewjs(?:\s|$)", text):
        return
    _handle_preview_command(bot, accid, event, mode="archive")


def get_help_text(bot, accid, from_id):
    contact = bot.rpc.get_contact(accid, from_id)
    sender_email = contact.address

    help_text = (
        f"👋 Hi {sender_email}!\n\n"
        f"I save web pages as single self-contained HTML files and send them back to you.\n\n"
        f"**Commands:**\n"
        f"/preview <url> — Generate compressed reader-mode page (recommended)\n"
        f"/archive <url> — Generate full page archive (with JS enabled)\n"
        f"/stats — View bot generation statistics\n"

        f"/source — Show bot source code link 🔌\n"
        f"/donate — Support bot development ❤️\n"
        f"/help — This instruction message\n\n"
        f"💡 _Tip: You can reply `/preview` to any text message to capture its first link!_\n"
    )

    admin_email = database.get_config("admin_dc_email")
    admin_fp = database.get_admin_fingerprint()
    is_actually_admin = _is_dc_admin(bot, accid, from_id)
    
    if not admin_email:
        help_text += "\n/initadmin — Claim bot ownership\n"
    elif is_actually_admin:
        fp_suffix = f" ({admin_fp[-8:].upper()})" if admin_fp else ""
        help_text += f"\n👑 **Admin:** `{admin_email}`{fp_suffix}\n"
        help_text += "\n**Admin Commands:**\n"
        help_text += "/transports — Show configured mail relays & stats\n"
        help_text += "/addtransport — Add a backup mail relay\n"
        help_text += "/rmtransport <addr> — Remove a mail relay\n"
        help_text += "/setprimary <addr> — Switch the primary mail relay\n"
        help_text += "/resilient — Toggle resilient sending mode (all relays)\n"
        help_text += "/preview_exclude <pattern> — Blacklist a URL pattern\n"
        help_text += "/preview_unexclude <pattern> — Remove a blacklisted pattern\n"
        help_text += "/preview_exclusions — List active URL exclusions\n"

    return help_text

@dc_cli.on(events.NewMessage(command="/help"))
def help_command(bot, accid, event):
    msg = event.msg
    help_text = get_help_text(bot, accid, msg.from_id)
    _send(bot, accid, msg.chat_id, help_text)

@dc_cli.on(events.NewMessage(command="/source"))
def source_command(bot, accid, event):
    msg = event.msg
    _send(bot, accid, msg.chat_id,
          "🔌 **WebPreview Bot Source Code**\n\n"
          "The source code and docker configurations for this bot are hosted at:\n"
          "👉 https://github.com/mrgluek/deltachat_webpreview\n\n"
          "Mirror: https://git.gluek.info/gluek/deltachat_webpreview")

@dc_cli.on(events.NewMessage(command="/donate"))
def donate_command(bot, accid, event):
    msg = event.msg
    _send(bot, accid, msg.chat_id,
          "❤️ **Support Bot Development**\n\n"
          "If you find this bot useful, you can support its development:\n\n"
          "☕️ Ko-fi: https://ko-fi.com/gluek (🌍 world cards, paypal)\n"
          "🚀 Tribute: https://web.tribute.tg/d/IWb (🇷🇺 russian cards, SBP)\n\n"
          "Thank you! 🙏")

@dc_cli.on(events.NewMessage(command="/stats"))
def stats_command(bot, accid, event):
    s = database.get_stats()
    usage = shutil.disk_usage(CACHE_DIR)
    free_gb = usage.free / (1024**3)
    total_gb = usage.total / (1024**3)
    free_pct = (usage.free / usage.total) * 100

    is_admin = _is_dc_admin(bot, accid, event.msg.from_id)
    
    reply = (
        f"📊 **WebPreview Bot Statistics**\n\n"
        f"Total previews generated: {s['total']}\n"
        f"Last 24h: {s['last_24h']}\n"
        f"Total file bandwidth: {_format_size(s['total_size'])}\n"
    )

    if is_admin:
        reply += (
            f"\n💾 **Disk Space (Admin only)**\n"
            f"Free: {free_gb:.1f} GB of {total_gb:.1f} GB ({free_pct:.1f}%)\n"
        )
    _send(bot, accid, event.msg.chat_id, reply)

# ── Transports / Relays Admin Command Listeners ──

@dc_cli.on(events.NewMessage(command="/transports"))
def transports_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /transports.")
        return

    try:
        transports = bot.rpc.list_transports(accid)
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to list transports: {e}")
        return

    if not transports:
        _send(bot, accid, msg.chat_id, "No transports configured.")
        return

    # Get connectivity status
    connectivity_label = "❓ Unknown"
    try:
        connectivity = bot.rpc.get_connectivity(accid)
        if connectivity >= 4000:
            connectivity_label = "🟢 Connected"
        elif connectivity >= 3000:
            connectivity_label = "🔄 Working"
        elif connectivity >= 2000:
            connectivity_label = "🟡 Connecting"
        else:
            connectivity_label = "🔴 Not connected"
    except Exception:
        pass

    # Get per-transport statistics
    stats_map = {}
    for s in database.get_all_transport_stats():
        stats_map[s['addr']] = s

    active_addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr")
    transport_addrs = []
    for t in transports:
        addr = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
        transport_addrs.append(addr)

    reply = f"🔌 **Mail Relays (Transports)**\n\nStatus: {connectivity_label}\n\n"

    for addr in transport_addrs:
        role = "🏠 Primary" if addr == active_addr else "🔄 Backup"
        reply += f"**{role}:** `{addr}`\n"

        stats = stats_map.get(addr)
        if stats:
            reply += f"  📤 Sent: {stats['msgs_sent']}  📥 Received: {stats['msgs_received']}\n"
            if stats.get('last_sent_at'):
                import datetime
                last_sent = datetime.datetime.fromtimestamp(stats['last_sent_at']).strftime('%Y-%m-%d %H:%M')
                reply += f"  Last sent: {last_sent}\n"
            if stats.get('last_received_at'):
                import datetime
                last_recv = datetime.datetime.fromtimestamp(stats['last_received_at']).strftime('%Y-%m-%d %H:%M')
                reply += f"  Last received: {last_recv}\n"
        else:
            reply += f"  📤 Sent: 0  📥 Received: 0\n"
        reply += "\n"

    reply += f"Total transports: {len(transport_addrs)}"
    _send(bot, accid, msg.chat_id, reply)

@dc_cli.on(events.NewMessage(command="/addtransport"))
def addtransport_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /addtransport.")
        return

    payload = event.payload.strip() if event.payload else ""
    if not payload:
        _send(bot, accid, msg.chat_id, 
            "Usage:\n"
            "/addtransport DCACCOUNT:server.example\n"
            "/addtransport user@example.com password123"
        )
        return

    try:
        if payload.startswith("DCACCOUNT:"):
            bot.rpc.add_transport_from_qr(accid, payload)
            _send(bot, accid, msg.chat_id, "✅ Backup transport added via chatmail URI.")
        else:
            parts = payload.split(None, 1)
            if len(parts) < 2:
                _send(bot, accid, msg.chat_id, 
                    "❌ For email accounts, provide both address and password:\n"
                    "/addtransport user@example.com password123"
                )
                return
            addr, password = parts[0], parts[1]
            bot.rpc.add_or_update_transport(accid, {"addr": addr, "password": password})
            _send(bot, accid, msg.chat_id, f"✅ Backup transport `{addr}` added.")
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to add transport: {e}")

@dc_cli.on(events.NewMessage(command="/setprimary"))
def setprimary_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /setprimary.")
        return

    addr = event.payload.strip() if event.payload else ""
    if not addr:
        _send(bot, accid, msg.chat_id, "Usage: /setprimary user@example.com")
        return

    try:
        bot.rpc.set_config(accid, "configured_addr", addr)
        _send(bot, accid, msg.chat_id, f"✅ Primary address (`configured_addr`) is now `{addr}`.")
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to set primary address: {e}")

@dc_cli.on(events.NewMessage(command="/resilient"))
def resilient_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /resilient.")
        return

    arg = event.payload.strip().lower() if event.payload else ""

    try:
        current = database.get_config("resilient") == "1"
        if not arg:
            status = "enabled" if current else "disabled"
            _send(bot, accid, msg.chat_id, f"ℹ️ Resilient sending mode is currently {status}.")
            return

        if arg in ("on", "1", "true"):
            database.set_config("resilient", "1")
            _send(bot, accid, msg.chat_id, "✅ Resilient sending mode enabled. Each outgoing message will be sent via all connected transports.")
        elif arg in ("off", "0", "false"):
            database.set_config("resilient", "0")
            _send(bot, accid, msg.chat_id, "❌ Resilient sending mode disabled.")
        else:
            _send(bot, accid, msg.chat_id, "❌ Invalid argument. Use '/resilient on', '/resilient off', or '/resilient' to get status.")
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to update resilient mode: {e}")

@dc_cli.on(events.NewMessage(command="/rmtransport"))
def rmtransport_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /rmtransport.")
        return

    addr = event.payload.strip() if event.payload else ""
    if not addr:
        _send(bot, accid, msg.chat_id, "Usage: /rmtransport user@example.com")
        return

    try:
        transports = bot.rpc.list_transports(accid)
        transport_addrs = []
        for t in transports:
            a = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
            transport_addrs.append(a)
        if len(transport_addrs) <= 1:
            _send(bot, accid, msg.chat_id, "❌ Cannot remove the last transport.")
            return
        if addr not in transport_addrs:
            _send(bot, accid, msg.chat_id, f"❌ Transport `{addr}` not found.")
            return
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to check transports: {e}")
        return

    try:
        bot.rpc.delete_transport(accid, addr)
        _send(bot, accid, msg.chat_id, f"✅ Transport `{addr}` removed.")
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to remove transport: {e}")

@dc_cli.on(events.NewMessage(command="/initadmin"))
def initadmin_command(bot, accid, event):
    msg = event.msg
    admin_email = database.get_config("admin_dc_email")
    admin_fp = database.get_admin_fingerprint()

    if admin_email or admin_fp:
        _send(bot, accid, msg.chat_id, "❌ Admin is already set. Use `set_admin.py` on the server to change.")
        return

    contact = bot.rpc.get_contact(accid, msg.from_id)
    email = contact.address
    database.set_config("admin_dc_email", email)

    fp = _get_contact_fingerprint(bot, accid, msg.from_id, contact=contact)
    if fp:
        first_fp = fp.split(',')[0]
        database.set_admin_fingerprint(first_fp)
        _send(bot, accid, msg.chat_id,
              f"✅ You are now the admin!\n\nEmail: `{email}`\nFingerprint: `{first_fp[-8:]}`")
    else:
        _send(bot, accid, msg.chat_id,
              f"✅ You are now the admin!\n\nEmail: `{email}`\n⚠️ Fingerprint not available yet (will be used after key exchange).")

@dc_cli.on(events.NewMessage(command="/preview_exclude"))
def preview_exclude_command(bot, accid, event):
    msg = event.msg
    text = (msg.text or "").strip()
    if not re.match(r"^/preview_exclude(?:\s|$)", text):
        return
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /preview_exclude.")
        return

    pattern = event.payload.strip() if event.payload else ""
    if not pattern:
        _send(bot, accid, msg.chat_id, "Usage: `/preview_exclude <pattern>`")
        return

    try:
        database.add_exclusion(pattern)
        _send(bot, accid, msg.chat_id, f"✅ Added to exclusions: `{pattern}`")
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to add exclusion: {e}")

@dc_cli.on(events.NewMessage(command="/preview_unexclude"))
def preview_unexclude_command(bot, accid, event):
    msg = event.msg
    text = (msg.text or "").strip()
    if not re.match(r"^/preview_unexclude(?:\s|$)", text):
        return
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /preview_unexclude.")
        return

    pattern = event.payload.strip() if event.payload else ""
    if not pattern:
        _send(bot, accid, msg.chat_id, "Usage: `/preview_unexclude <pattern>`")
        return

    try:
        database.remove_exclusion(pattern)
        _send(bot, accid, msg.chat_id, f"✅ Removed from exclusions: `{pattern}`")
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to remove exclusion: {e}")

@dc_cli.on(events.NewMessage(command="/preview_exclusions"))
def preview_exclusions_command(bot, accid, event):
    msg = event.msg
    text = (msg.text or "").strip()
    if not re.match(r"^/preview_exclusions(?:\s|$)", text):
        return
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /preview_exclusions.")
        return

    try:
        exclusions = database.list_exclusions()
        if not exclusions:
            _send(bot, accid, msg.chat_id, "Exclusion list is empty.")
            return
        
        reply = "🚫 **URL Exclusions:**\n\n"
        for idx, pat in enumerate(exclusions, 1):
            reply += f"{idx}. `{pat}`\n"
        _send(bot, accid, msg.chat_id, reply)
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to list exclusions: {e}")

def _parse_chat_info_is_private(chat_info) -> bool:
    if isinstance(chat_info, dict):
        chat_type = chat_info.get("chatType") or chat_info.get("chat_type")
        if isinstance(chat_type, str) and chat_type.lower() == "single":
            return True
        type_val = chat_info.get("type")
        if type_val in (1, "1"):
            return True
    else:
        chat_type = getattr(chat_info, "chat_type", None) or getattr(chat_info, "chatType", None)
        if isinstance(chat_type, str) and chat_type.lower() == "single":
            return True
        type_val = getattr(chat_info, "type", None)
        if type_val in (1, "1"):
            return True
    return False

def _is_private_chat(bot, accid, chat_id) -> bool:
    # 1. Try get_basic_chat_info
    try:
        chat_info = bot.rpc.get_basic_chat_info(accid, chat_id)
        if chat_info:
            return _parse_chat_info_is_private(chat_info)
    except Exception as e:
        logger.debug(f"get_basic_chat_info failed: {e}")

    # 2. Fallback to get_full_chat_by_id
    try:
        chat_info = bot.rpc.get_full_chat_by_id(accid, chat_id)
        if chat_info:
            return _parse_chat_info_is_private(chat_info)
    except Exception as e:
        logger.debug(f"get_full_chat_by_id failed: {e}")

    # 3. Ultimate fallback: get_chat_contacts length check
    try:
        contacts = bot.rpc.get_chat_contacts(accid, chat_id)
        if isinstance(contacts, list) and len(contacts) == 1:
            return True
    except Exception as e:
        logger.error(f"get_chat_contacts failed: {e}")

    return False

# ── General Event Listener ──

@dc_cli.on(events.NewMessage(is_bot=None))
def on_new_message(bot, accid, event):
    msg = event.msg
    
    if _is_duplicate_msg(msg.id, "text"):
        return
        
    # Safety checks
    if msg.is_info or accid != dc_accid:
        return

    if msg.from_id == DC_CONTACT_ID_SELF:
        return

    # Track receiving stats
    try:
        addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr")
        if addr:
            database.increment_transport_received(addr)
    except Exception:
        pass

    text = (msg.text or "").strip()
    if not text:
        return

    # 1. Intercept dynamic commands: /preview_urlhash, /previewjs_urlhash or /archive_urlhash
    m = re.match(r"^/(preview|previewjs|archive)_([0-9a-fA-F]{8})(?:@\w+)?", text)
    if m:
        cmd_type, urlhash = m.group(1), m.group(2)
        url = database.get_url_by_hash(urlhash)
        if not url:
            _react(bot, accid, msg.id, "❌")
            _send(bot, accid, msg.chat_id, "❌ Invalid or expired preview trigger.")
            return

        # Check exclusions first!
        if database.is_excluded(url):
            logger.info(f"Exclusion hit for URL: {url} in chat {msg.chat_id} via hash command")
            _react(bot, accid, msg.id, "⚠️")
            _send(bot, accid, msg.chat_id, f"⚠️ This URL is in the exclusion list.")
            return

        # Rate limiting check
        if _is_rate_limited(bot, accid, msg.from_id):
            _react(bot, accid, msg.id, "⏱")
            return

        mode = "archive" if cmd_type in ("previewjs", "archive") else "readability"
        t = threading.Thread(
            target=_do_preview, 
            args=(bot, accid, msg.chat_id, msg.id, msg.from_id, url, mode), 
            daemon=True
        )
        t.start()
        return

    # Automatic welcoming & auto-parsing of links in chats/groups
    try:
        is_private = _is_private_chat(bot, accid, msg.chat_id)
        if is_private:
            # 1. Greet user if not greeted yet
            greeted_key = f"greeted_{msg.from_id}"
            if not database.get_config(greeted_key):
                help_text = get_help_text(bot, accid, msg.from_id)
                _send(bot, accid, msg.chat_id, help_text)
                database.set_config(greeted_key, "1")

            # 2. Automatically parse URLs sent in private chat (if not starting with a slash command)
            if not text.startswith("/"):
                url_match = re.search(r'(https?://[^\s<>"]+)', text)
                if url_match:
                    url = url_match.group(1).rstrip('.,;:)!?')
                    
                    # Rate limiting check
                    if _is_rate_limited(bot, accid, msg.from_id):
                        _react(bot, accid, msg.id, "⏱")
                        return

                    # Run readability in background thread by default
                    t = threading.Thread(
                        target=_do_preview, 
                        args=(bot, accid, msg.chat_id, msg.id, msg.from_id, url, "readability"), 
                        daemon=True
                    )
                    t.start()
        else:
            # Automatic preview of links in group chats
            if not text.startswith("/"):
                url_match = re.search(r'(https?://[^\s<>"]+)', text)
                if url_match:
                    url = url_match.group(1).rstrip('.,;:)!?')
                    
                    # Skip if the URL is in the exclusions
                    if database.is_excluded(url):
                        return

                    # Skip if YT Bot is in the chat and this is a link handled by YT Bot
                    if _is_yt_bot_in_chat(bot, accid, msg.chat_id) and _is_handled_by_yt_bot(url):
                        logger.info(f"Skipping group link auto-preview for {url} since YT Bot is present and handles it.")
                        return
                        
                    # Rate limiting check
                    if _is_rate_limited(bot, accid, msg.from_id):
                        return
                    
                    # Run OG preview generation in background thread
                    t = threading.Thread(
                        target=_do_group_link_preview, 
                        args=(bot, accid, msg.chat_id, msg.from_id, url), 
                        daemon=True
                    )
                    t.start()
    except Exception as e:
        logger.error(f"Chat processing error: {e}")


resilient_lock = threading.Lock()

def _setup_resilient_mode(bot):
    original_send_msg = bot.rpc.send_msg

    def patched_send_msg(account_id, chat_id, msg_data):
        try:
            is_resilient = database.get_config("resilient") == "1"
        except Exception:
            is_resilient = False

        if not is_resilient:
            return original_send_msg(account_id, chat_id, msg_data)

        try:
            transports = bot.rpc.list_transports(account_id)
        except Exception:
            transports = []

        if len(transports) <= 1:
            return original_send_msg(account_id, chat_id, msg_data)

        initial_addr = None
        try:
            initial_addr = bot.rpc.get_config(account_id, "configured_addr") or bot.rpc.get_config(account_id, "addr")
        except Exception:
            pass

        # 1. Send the message normally via the current primary transport (non-blocking queueing)
        try:
            msg_id = original_send_msg(account_id, chat_id, msg_data)
            bot.logger.info(f"Resilient send: initial msg queued with ID {msg_id} on transport {initial_addr}.")
        except Exception as send_err:
            bot.logger.error(f"Resilient send: failed to queue initial message: {send_err}")
            return None

        # Background worker to handle resending to other transports sequentially
        def bg_resend_worker(m_id, init_addr, t_list):
            bot.logger.info(f"Resilient send: starting background sender for msg {m_id}")
            with resilient_lock:
                bot.logger.info(f"Resilient send bg: waiting for initial delivery of msg {m_id} on {init_addr}...")
                start_time = time.time()
                delivered = False
                while time.time() - start_time < 10:
                    try:
                        msg_snapshot = bot.rpc.get_message(account_id, m_id)
                        state = msg_snapshot.get('state') if isinstance(msg_snapshot, dict) else getattr(msg_snapshot, 'state', None)
                        if state in (26, 28):
                            bot.logger.info(f"Resilient send bg: initial msg {m_id} delivered successfully on {init_addr}.")
                            delivered = True
                            break
                        if state == 24:
                            bot.logger.warning(f"Resilient send bg: initial msg {m_id} failed on {init_addr}.")
                            break
                    except Exception as poll_err:
                        bot.logger.debug(f"Resilient send bg initial poll error: {poll_err}")
                    time.sleep(0.5)

                if not delivered:
                    bot.logger.warning(f"Resilient send bg: initial msg {m_id} did not deliver on {init_addr} within timeout.")

                # 2. Resend on all other transports
                for t in t_list:
                    t_addr = t.get('addr') if isinstance(t, dict) else getattr(t, 'addr', None)
                    if not t_addr or (init_addr and t_addr.lower() == init_addr.lower()):
                        continue

                    bot.logger.info(f"Resilient send bg: switching primary transport to {t_addr}")
                    try:
                        bot.rpc.set_config(account_id, "configured_addr", t_addr)
                        time.sleep(1)
                    except Exception as switch_err:
                        bot.logger.error(f"Resilient send bg: failed to switch transport to {t_addr}: {switch_err}")
                        continue

                    try:
                        bot.logger.info(f"Resilient send bg: resending msg {m_id} on transport {t_addr}...")
                        bot.rpc.resend_messages(account_id, [m_id])

                        # Wait up to 10 seconds for the resent message to be delivered/failed
                        start_time = time.time()
                        delivered = False
                        while time.time() - start_time < 10:
                            try:
                                msg_snapshot = bot.rpc.get_message(account_id, m_id)
                                state = msg_snapshot.get('state') if isinstance(msg_snapshot, dict) else getattr(msg_snapshot, 'state', None)
                                if state in (26, 28):
                                    bot.logger.info(f"Resilient send bg: msg {m_id} delivered successfully on {t_addr}.")
                                    delivered = True
                                    break
                                if state == 24:
                                    bot.logger.warning(f"Resilient send bg: msg {m_id} failed on {t_addr}.")
                                    break
                            except Exception as poll_err:
                                bot.logger.debug(f"Resilient send bg poll error: {poll_err}")
                            time.sleep(0.5)

                        if not delivered:
                            bot.logger.warning(f"Resilient send bg: msg {m_id} did not deliver on {t_addr} within timeout.")
                    except Exception as resend_err:
                        bot.logger.error(f"Resilient send bg: failed to resend message on transport {t_addr}: {resend_err}")

                # 3. Restore the initial primary transport configuration
                if init_addr:
                    try:
                        bot.logger.info(f"Resilient send bg: restoring initial primary transport to {init_addr}")
                        bot.rpc.set_config(account_id, "configured_addr", init_addr)
                    except Exception as restore_err:
                        bot.logger.error(f"Resilient send bg: failed to restore transport to {init_addr}: {restore_err}")

        # Start the background thread for resilient sending
        threading.Thread(target=bg_resend_worker, args=(msg_id, initial_addr, transports), daemon=True).start()

        return msg_id

    bot.rpc.send_msg = patched_send_msg

@dc_cli.on_init
def on_init(bot, args):
    global dc_bot_instance, dc_accid
    bot.logger.info(f"Initializing WebPreview Bot v{VERSION}...")
    dc_bot_instance = bot
    _setup_resilient_mode(bot)
    
    accounts = bot.rpc.get_all_account_ids()
    if accounts:
        dc_accid = accounts[0]
        bot.rpc.set_config(dc_accid, "displayname", "WebPreview Bot")
        bot.rpc.set_config(dc_accid, "selfstatus", "I generate single-file HTML web previews in chats and groups.\n\nSend: /preview <url> or /archive <url>, or send /help for commands.")
        
        # Set icon if exists
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            for icon_name in ["icon.png", os.path.join("data", "icon.png")]:
                icon_path = os.path.join(base_dir, icon_name)
                if os.path.exists(icon_path):
                    bot.rpc.set_config(dc_accid, "selfavatar", icon_path)
                    break
        except Exception as e:
            bot.logger.warning(f"Could not set avatar: {e}")

@dc_cli.on_start
def on_start(bot, args):
    global dc_bot_instance, dc_accid
    dc_bot_instance = bot
    
    accounts = bot.rpc.get_all_account_ids()
    if not accounts:
        logger.error("No accounts found.")
        return
        
    accid = accounts[0]
    dc_accid = accid
    
    logger.info(f"WebPreview bot v{VERSION} started with accid {accid}.")
    
    # Show configured admin and transports
    admin_email = database.get_config("admin_dc_email")
    admin_fp = database.get_admin_fingerprint()
    if admin_email:
        fp_suffix = f" ({admin_fp[-8:].upper()})" if admin_fp else ""
        print(f"Bot Administrator: {admin_email}{fp_suffix}")
    
    try:
        transports = bot.rpc.list_transports(accid)
        print("\n" + "=" * 50)
        print("Configured Bot Transports (Relays):")
        for t in transports:
            a = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
            print(f" - {a}")
    except Exception:
        pass

    try:
        import io
        try:
            import qrcode
        except ImportError:
            qrcode = None

        qrdata = bot.rpc.get_chat_securejoin_qr_code(accid, None)
        print("\nTo add this bot, scan the QR code or copy the link:\n")
        if qrcode:
            qr = qrcode.QRCode(version=1, box_size=1, border=2)
            qr.add_data(qrdata)
            qr.make(fit=True)
            f = io.StringIO()
            qr.print_ascii(out=f)
            print(f.getvalue())
        print(qrdata)
        print("\n" + "=" * 50 + "\n")
    except Exception as e:
        bot.logger.error(f"Failed to generate QR code: {e}")

    # Start background cache cleaning task
    t = threading.Thread(target=_cache_cleaner_loop, daemon=True)
    t.start()

# ── Cache Cleanup background task ──

def _cache_cleaner_loop():
    """Background task to remove cache files and DB entries older than 1h."""
    logger.info("Cache cleaner thread started.")
    while True:
        try:
            # Clear expired cache entries from the database
            database.clear_expired_cache(CACHE_MAX_AGE)

            if not os.path.exists(CACHE_DIR):
                time.sleep(3600)
                continue

            now = time.time()
            for f in os.listdir(CACHE_DIR):
                path = os.path.join(CACHE_DIR, f)
                if not os.path.isfile(path):
                    continue
                
                mtime = os.path.getmtime(path)
                if now - mtime > CACHE_MAX_AGE:
                    logger.info(f"Removing old cache preview file: {f}")
                    try:
                        os.remove(path)
                    except: pass
        except Exception as e:
            logger.error(f"Error in cache cleaner loop: {e}")
            
        time.sleep(3600)

if __name__ == "__main__":
    import sys
    
    # Handle 'init transport' CLI command exactly like YT Bot
    if len(sys.argv) > 2 and sys.argv[1] == "init" and sys.argv[2] == "transport":
        if len(sys.argv) < 5:
            print("Usage: python bot.py init transport <email> <password>")
            sys.exit(1)
            
        addr, password = sys.argv[3], sys.argv[4]
        
        from deltachat2 import Rpc, IOTransport
        from appdirs import user_config_dir
        
        config_dir = user_config_dir("webpreview")
        accounts_dir = os.path.join(config_dir, "accounts")
        
        try:
            with IOTransport(accounts_dir=accounts_dir) as trans:
                rpc = Rpc(trans)
                accids = rpc.get_all_account_ids()
                if not accids:
                    print("Error: No accounts configured. Run 'python bot.py init addr password' first.")
                    sys.exit(1)
                    
                rpc.add_or_update_transport(accids[0], {"addr": addr, "password": password})
                print(f"Success: Backup transport {addr} added.")
        except Exception as e:
            print(f"Error adding transport: {e}")
            sys.exit(1)
        sys.exit(0)

    if len(sys.argv) == 1:
        sys.argv.append("serve")
    dc_cli.start()
