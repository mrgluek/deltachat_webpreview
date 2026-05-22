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
            logger.info(f"Detected bot's own fingerprints from enc_info: {[f[-8:] for f in self_fps]}")
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
                matches = re.findall(r'[0-9a-fA-F]{32,64}', enc_info.replace(' ', '').replace(':', ''))
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
    try:
        req = urllib.request.Request(
            url, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            content_type = response.headers.get('Content-Type', '')
            if 'text/html' not in content_type.lower():
                return urllib.parse.urlparse(url).netloc, None
            
            # Read first 512KB
            html_bytes = response.read(512 * 1024)
            head = html_bytes.decode('utf-8', errors='ignore')
            
            # 1. Extract title
            title = None
            title_m = re.search(r'<meta[^>]*(?:property|name)=["\']og:title["\'][^>]*content=["\'](.*?)["\']', head, re.IGNORECASE)
            if not title_m:
                title_m = re.search(r'<meta[^]*content=["\'](.*?)["\'][^>]*(?:property|name)=["\']og:title["\']', head, re.IGNORECASE)
            if title_m:
                import html
                title = html.unescape(title_m.group(1).strip())
            
            if not title:
                # Fallback to <title>
                title_m = re.search(r'<title[^>]*>(.*?)</title>', head, re.IGNORECASE | re.DOTALL)
                if title_m:
                    import html
                    title = html.unescape(title_m.group(1).strip())
            
            if not title:
                title = urllib.parse.urlparse(url).netloc or "Webpage"
                
            # 2. Extract og:image
            image_url = None
            img_m = re.search(r'<meta[^>]*(?:property|name)=["\']og:image["\'][^>]*content=["\'](.*?)["\']', head, re.IGNORECASE)
            if not img_m:
                img_m = re.search(r'<meta[^]*content=["\'](.*?)["\'][^>]*(?:property|name)=["\']og:image["\']', head, re.IGNORECASE)
            if not img_m:
                img_m = re.search(r'<meta[^>]*(?:property|name)=["\']twitter:image["\'][^>]*content=["\'](.*?)["\']', head, re.IGNORECASE)
            
            if img_m:
                image_url = img_m.group(1).strip()
                image_url = urllib.parse.urljoin(url, image_url)
                
            return title, image_url
    except Exception as e:
        logger.warning(f"Error fetching OG tags for {url}: {e}")
        return urllib.parse.urlparse(url).netloc or "Webpage", None

def _download_temp_image(image_url: str) -> str | None:
    """
    Downloads an image URL to a temporary file.
    Returns absolute path of the temporary file, or None if failed.
    """
    try:
        req = urllib.request.Request(
            image_url, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            content_type = response.headers.get('Content-Type', '')
            if 'image' not in content_type.lower():
                return None
            
            ext = ".jpg"
            if "png" in content_type.lower():
                ext = ".png"
            elif "webp" in content_type.lower():
                ext = ".webp"
            elif "gif" in content_type.lower():
                ext = ".gif"
                
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                tmp.write(response.read())
                return tmp.name
    except Exception as e:
        logger.warning(f"Failed to download OG image {image_url}: {e}")
        return None

def _do_group_link_preview(bot, accid, chat_id, from_id, url: str):
    """Fetches OG data, downloads banner image, and sends preview options to group."""
    try:
        # 1. Skip if the URL matches exclusion pattern
        if database.is_excluded(url):
            logger.info(f"URL excluded from preview: {url}")
            return

        # 2. Get or create short hash in SQLite database
        urlhash = database.get_or_create_url_hash(url)
        
        # 3. Get OG tags
        title, image_url = _get_og_preview_data(url)
        
        # 4. Format caption
        caption = (
            f"🌐 {title}\n\n"
            f"Preview page: /preview_{urlhash}\n"
            f"Preview with js: /previewjs_{urlhash}"
        )
        
        img_path = None
        if image_url:
            img_path = _download_temp_image(image_url)
            
        try:
            if img_path:
                _send(bot, accid, chat_id, caption, file=img_path)
            else:
                _send(bot, accid, chat_id, caption)
        finally:
            if img_path and os.path.exists(img_path):
                try:
                    os.remove(img_path)
                except: pass
    except Exception as e:
        logger.error(f"Error in _do_group_link_preview: {e}")

def _do_preview(bot, accid, chat_id, req_msg_id, from_id, url: str, with_js: bool):
    """Run monolith compilation in background thread."""
    # 0. Check exclusions first!
    if database.is_excluded(url):
        logger.info(f"Exclusion hit for URL: {url} in chat {chat_id}")
        _react(bot, accid, req_msg_id, "⚠️")
        _send(bot, accid, chat_id, f"⚠️ This URL is in the exclusion list.")
        return

    import hashlib
    url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
    cache_key = f"{url_hash}_{1 if with_js else 0}"

    # 0. Check cache
    cached = database.get_cached_preview(cache_key)
    if cached:
        created_at = cached.get("created_at", 0)
        filepath = cached.get("filepath", "")
        title = cached.get("title", "")
        filesize = cached.get("filesize", 0)
        
        if time.time() - created_at < CACHE_MAX_AGE and os.path.exists(filepath):
            logger.info(f"Cache hit for URL: {url} (JS={with_js}). Sending cached preview: {filepath}")
            _react(bot, accid, req_msg_id, "⏳")
            
            caption = f"{title}\n\n🔗 {url}"
            _send(bot, accid, chat_id, caption, file=filepath)
            _react(bot, accid, req_msg_id, "☑️")
            database.add_preview_log(chat_id, from_id, url, title, filesize, with_js)
            return

    logger.info(f"Starting monolith download for URL: {url} (JS={with_js}) in chat {chat_id}")
    
    # 1. React with loading icon
    _react(bot, accid, req_msg_id, "⏳")

    domain = urllib.parse.urlparse(url).netloc or "webpage"
    tmpdir = tempfile.mkdtemp(prefix="webpreview_")
    output_path = os.path.join(tmpdir, "output.html")

    # 2. Build Monolith Command
    cmd = ["monolith", "-e", "-t", "30"]
    if not with_js:
        cmd.append("-j")
    
    # User-agent to bypass primitive bots block
    cmd.extend(["-u", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"])
    cmd.extend([url, "-o", output_path])

    start_time = time.time()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        code, err = loop.run_until_complete(_run_monolith_process(cmd))
    finally:
        loop.close()
    
    duration = int(time.time() - start_time)

    # 3. Handle compilation output
    if code != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        logger.error(f"Monolith failed with code {code} for URL {url}. Error: {err}")
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id, f"❌ Failed to generate web preview.\nReason: {err or 'Empty or missing output file.'}")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return

    try:
        # 4. Extract title and clean filename
        title = _extract_title(output_path) or domain
        safe_fname = _safe_filename(domain, with_js)
        cache_path = os.path.join(CACHE_DIR, safe_fname)
        
        # 5. Move to persistent cache for transfer
        shutil.move(output_path, cache_path)
        filesize = os.path.getsize(cache_path)
        
        # Cache preview in database
        database.add_cached_preview(cache_key, cache_path, title, filesize)
        
        # 6. Format simple, clean caption like YouTube bot
        caption = f"{title}\n\n🔗 {url}"
        
        # 7. Send attachment
        _send(bot, accid, chat_id, caption, file=cache_path)
        _react(bot, accid, req_msg_id, "☑️")
        
        # 8. Store log stats
        database.add_preview_log(chat_id, from_id, url, title, filesize, with_js)
        
    except Exception as e:
        logger.error(f"Error packing preview file: {e}")
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id, f"❌ Error packing preview file: {e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ── Preview Trigger Handler ──

def _handle_preview_command(bot, accid, event, with_js: bool):
    """Processes `/preview` and `/previewjs` triggers."""
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
              "• `/preview <url>` — Save page (default safe static)\n"
              "• `/previewjs <url>` — Save page (with scripts enabled)\n"
              "• Reply `/preview` or `/previewjs` to another message containing a link.")
        return

    # Spawn thread to run monolith in background
    t = threading.Thread(
        target=_do_preview, 
        args=(bot, accid, msg.chat_id, msg.id, msg.from_id, url, with_js), 
        daemon=True
    )
    t.start()

# ── Command Listeners ──

@dc_cli.on(events.NewMessage(command="/preview"))
def preview_command(bot, accid, event):
    if accid != dc_accid:
        return
    _handle_preview_command(bot, accid, event, with_js=False)

@dc_cli.on(events.NewMessage(command="/previewjs"))
def previewjs_command(bot, accid, event):
    if accid != dc_accid:
        return
    _handle_preview_command(bot, accid, event, with_js=True)

def get_help_text(bot, accid, from_id):
    contact = bot.rpc.get_contact(accid, from_id)
    sender_email = contact.address

    help_text = (
        f"👋 Hi {sender_email}!\n\n"
        f"I save web pages as single self-contained HTML files and send them back to you.\n\n"
        f"**Commands:**\n"
        f"/preview <url> — Generate safe, static page preview (recommended)\n"
        f"/previewjs <url> — Generate full page preview (with JS enabled)\n"
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

@dc_cli.on(events.NewMessage)
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

    # 1. Intercept dynamic commands: /preview_urlhash or /previewjs_urlhash
    m = re.match(r"^/(preview|previewjs)_([0-9a-fA-F]{8})(?:@\w+)?", text)
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

        with_js = (cmd_type == "previewjs")
        t = threading.Thread(
            target=_do_preview, 
            args=(bot, accid, msg.chat_id, msg.id, msg.from_id, url, with_js), 
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

                    # Run monolith in background thread (JS disabled by default)
                    t = threading.Thread(
                        target=_do_preview, 
                        args=(bot, accid, msg.chat_id, msg.id, msg.from_id, url, False), 
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

# ── CLI Setup Hooks ──

@dc_cli.on_init
def on_init(bot, args):
    global dc_bot_instance, dc_accid
    bot.logger.info("Initializing WebPreview Bot...")
    dc_bot_instance = bot
    
    accounts = bot.rpc.get_all_account_ids()
    if accounts:
        dc_accid = accounts[0]
        bot.rpc.set_config(dc_accid, "displayname", "WebPreview Bot")
        bot.rpc.set_config(dc_accid, "selfstatus", "I generate single-file HTML web previews in chats and groups.\n\nSend: /preview <url>")
        
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
    
    logger.info(f"WebPreview bot started with accid {accid}.")
    
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
