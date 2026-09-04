import asyncio
import collections
from datetime import datetime, timezone
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import contextlib
import urllib
import urllib.request
import urllib.parse
import hashlib
import ipaddress

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

VERSION = "2.9.4"
STANDARD_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
NON_MOZILLA_USER_AGENT = "AppleWebKit/605.1.15 (KHTML, like Gecko) Safari/605.1.15 deltachat-webpreview/1.0"

# KaraKeep integration (opt-in via env)
KARAKEEP_URL = os.environ.get("KARAKEEP_URL", "").rstrip("/")
KARAKEEP_API_KEY = os.environ.get("KARAKEEP_API_KEY", "")
KARAKEEP_TAGS = [t.strip() for t in os.environ.get("KARAKEEP_TAGS", "").split(",") if t.strip()]

# Archive.today mirrors & proxy (fallback/concurrent archiving)
ARCHIVE_TODAY_DEFAULT_MIRRORS = [
    "https://archive.ph",
    "https://archive.is",
    "https://archive.today",
    "https://archive.li",
    "https://archive.vn",
    "https://archive.md",
]
_raw_archive_today_mirrors = os.environ.get("ARCHIVE_TODAY_MIRRORS", "")
ARCHIVE_TODAY_MIRRORS = [
    m.strip().rstrip("/") for m in _raw_archive_today_mirrors.split(",") if m.strip()
] if _raw_archive_today_mirrors else ARCHIVE_TODAY_DEFAULT_MIRRORS
ARCHIVE_TODAY_PROXY_URL = os.environ.get("ARCHIVE_TODAY_PROXY_URL", "").strip()

# Internet Archive SPN2 API keys (opt-in via env, https://archive.org/account/s3.php)
WAYBACK_ACCESS_KEY = os.environ.get("WAYBACK_ACCESS_KEY", "").strip()
WAYBACK_SECRET_KEY = os.environ.get("WAYBACK_SECRET_KEY", "").strip()

# Jina AI key (opt-in via env)
JINA_API_KEY = os.environ.get("JINA_API_KEY", "").strip()
JINA_PROXY_URL = os.environ.get("JINA_PROXY_URL", "").strip()

# Gemini AI key & fallback models (opt-in via env)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip().strip("'\"")
_raw_models = os.environ.get("GEMINI_MODELS") or os.environ.get("GEMINI_MODEL", "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemma-4-31b-it,gemma-4-26b-a4b-it,gemini-2.5-flash-lite")
GEMINI_MODELS = [m.strip().strip("'\"").removeprefix("models/").strip("/") for m in _raw_models.split(",") if m.strip()]
if not GEMINI_MODELS:
    GEMINI_MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemma-4-31b-it", "gemma-4-26b-a4b-it", "gemini-2.5-flash-lite"]
GEMINI_MODEL = GEMINI_MODELS[0]
_GEMINI_MODEL_COOLDOWNS: dict[str, float] = {}

# OGInstagram embed proxy settings (opt-in via env, https://github.com/seirenkr/OGInstagram)
OGINSTAGRAM_HOST = os.environ.get("OGINSTAGRAM_HOST", "oginstagram.com").strip().rstrip("/")
if not OGINSTAGRAM_HOST:
    OGINSTAGRAM_HOST = "oginstagram.com"

# Proxy settings (opt-in via env)
PROXY_URL = (os.environ.get("PROXY_URL") or os.environ.get("RU_PROXY", "")).strip()
INSTAGRAM_PROXY_URL = os.environ.get("INSTAGRAM_PROXY_URL", "").strip()

def _normalize_proxy_domains(raw_domains_str: str) -> list[str]:
    """Parse comma-separated proxy domains, automatically expanding IDNA punycode and unicode variants."""
    domains = []
    for item in raw_domains_str.split(","):
        d = item.strip().lower()
        if not d:
            continue
        if d not in domains:
            domains.append(d)
        # Add punycode representation for internationalized domains (e.g. .рф -> .xn--p1ai)
        try:
            idna_d = d.encode("idna").decode("ascii")
            if idna_d not in domains:
                domains.append(idna_d)
        except Exception:
            pass
        # Add unicode representation if input was already punycode (e.g. .xn--p1ai -> .рф)
        if "xn--" in d:
            try:
                unicode_d = d.encode("ascii").decode("idna")
                if unicode_d not in domains:
                    domains.append(unicode_d)
            except Exception:
                pass
    return domains

PROXY_DOMAINS = _normalize_proxy_domains(os.environ.get("PROXY_DOMAINS", ".ru"))

# Characters that are never legitimate at the end of a URL when extracted from
# natural-language text.  We also handle unbalanced parentheses separately below.
_URL_TRAILING_JUNK = set('.,;:!?"\'\u00ab\u00bb\u2018\u2019\u201c\u201d\u2039\u203a\u300c\u300d')

def _strip_url_trailing_junk(url: str) -> str:
    """
    Remove trailing punctuation that is never part of a URL when the URL is
    embedded in natural-language text:
      - Standard ASCII junk: . , ; : ! ? " '
      - Unicode quotation marks: « » " " ' ' ‹ ›
      - Unbalanced closing parentheses/brackets ) ] }
    Balanced parentheses inside the path (e.g. Wikipedia) are preserved.
    """
    while url:
        ch = url[-1]
        if ch in _URL_TRAILING_JUNK:
            url = url[:-1]
            continue
        # Strip unbalanced closing bracket/paren/brace
        pairs = {'(': ')', '[': ']', '{': '}'}
        matched = False
        for open_ch, close_ch in pairs.items():
            if ch == close_ch and url.count(open_ch) < url.count(close_ch):
                url = url[:-1]
                matched = True
                break
        if not matched:
            break
    return url


def _clean_url_params(url: str) -> str:
    """
    Remove ad and metric parameters from URL query string.
    Removes parameters like: ysclid=, utm_*, fbclid=, gclid=, gclsrc=, dclid=, srsltid=, etc.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        if not parsed.query:
            return url
        
        # Parse query parameters
        query_params = urllib.parse.parse_qs(parsed.query)
        
        # Parse query parameters
        query_params = urllib.parse.parse_qs(parsed.query)
        
        # Parameters to remove
        params_to_remove = []
        for param in query_params.keys():
            param_lower = param.lower()
            # Remove utm_* parameters
            if param_lower.startswith('utm_'):
                params_to_remove.append(param)
            # Remove yandex metrica
            elif param_lower in ('ysclid', 'ym_retargeting', 'yz_referer'):
                params_to_remove.append(param)
            # Remove Facebook click ID
            elif param_lower in ('fbclid', 'fbc'):
                params_to_remove.append(param)
            # Remove Google ads IDs
            elif param_lower in ('gclid', 'gclsrc', 'dclid', 'gbraid', 'wbraid'):
                params_to_remove.append(param)
            # Remove Shopify referral
            elif param_lower == 'srsltid':
                params_to_remove.append(param)
            # Remove referral/referrer parameters
            elif param_lower in ('referral', 'referrer', 'source', 'src'):
                # Only remove if value looks like a tracking ID (not a legitimate source name)
                values = query_params[param]
                if any(v and ('click' in v.lower() or 'track' in v.lower() or 
                             'campaign' in v.lower() or len(v) > 20 for v in values if isinstance(v, str))):
                    params_to_remove.append(param)
            # Remove other common tracking patterns
            elif any(pattern in param_lower for pattern in ['track', 'click', 'campaign', 'medium', 'term']):
                params_to_remove.append(param)
        
        # Rebuild query string without removed parameters
        remaining_params = {k: v for k, v in query_params.items() if k not in params_to_remove}
        new_query = urllib.parse.urlencode(remaining_params, doseq=True)
        
        # Reconstruct URL
        new_parsed = parsed._replace(query=new_query if new_query else '')
        return urllib.parse.urlunparse(new_parsed)
    except Exception:
        # Fallback to original URL if parsing fails
        return url


def _should_use_proxy(url: str) -> bool:
    """Return True if the URL should be routed through the proxy."""
    if not PROXY_URL:
        return False
    try:
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc.lower()
        if ":" in domain:
            domain = domain.split(":")[0]
            
        # Jina requests are handled separately via JINA_PROXY_URL, never via PROXY_URL
        if domain == "r.jina.ai":
            return False

        domains_to_check = [domain]
        if "xn--" in domain:
            try:
                unicode_d = domain.encode("ascii").decode("idna")
                if unicode_d not in domains_to_check:
                    domains_to_check.append(unicode_d)
            except Exception:
                pass

        for proxy_domain in PROXY_DOMAINS:
            clean_pd = proxy_domain.lstrip(".")
            for d in domains_to_check:
                if d == clean_pd or d.endswith("." + clean_pd) or d.endswith(proxy_domain):
                    return True
    except Exception:
        pass
    return False

def _copy_req(req_or_url):
    """Create a fresh Request copy without any attached proxy tunneling state."""
    if isinstance(req_or_url, urllib.request.Request):
        return urllib.request.Request(
            req_or_url.full_url,
            data=req_or_url.data,
            headers=dict(req_or_url.header_items())
        )
    return req_or_url

def _urlopen(req_or_url, timeout=None):
    """
    urlopen wrapper that dynamically routes specific domains through a proxy.
    Falls back to direct connection if the configured proxy fails (e.g. 403, timeout, connection refused).
    """
    url = req_or_url.full_url if isinstance(req_or_url, urllib.request.Request) else req_or_url
    try:
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc.lower()
        if ":" in domain:
            domain = domain.split(":")[0]
    except Exception:
        domain = ""

    # Jina AI Reader requests
    if domain == "r.jina.ai":
        if JINA_PROXY_URL:
            logger.info(f"Routing Jina request for {url} through Jina proxy: {JINA_PROXY_URL}")
            proxy_handler = urllib.request.ProxyHandler({'http': JINA_PROXY_URL, 'https': JINA_PROXY_URL})
            opener = urllib.request.build_opener(proxy_handler)
            try:
                return opener.open(req_or_url, timeout=timeout)
            except Exception as e:
                logger.warning(f"Jina proxy {JINA_PROXY_URL} failed for {url}: {e}. Retrying direct connection...")
                return urllib.request.urlopen(_copy_req(req_or_url), timeout=timeout)
        else:
            return urllib.request.urlopen(req_or_url, timeout=timeout)

    # Instagram and gateway requests (opt-in via INSTAGRAM_PROXY_URL)
    if INSTAGRAM_PROXY_URL:
        ig_domains = (
            "instagram.com", "instagr.am", "cdninstagram.com", "fbcdn.net",
            "oginstagram.com", "kkinstagram.com", "vxinstagram.com", "kkclip.com"
        )
        custom_og_host = ""
        if OGINSTAGRAM_HOST:
            try:
                parsed_og = urllib.parse.urlparse(OGINSTAGRAM_HOST if "://" in OGINSTAGRAM_HOST else f"http://{OGINSTAGRAM_HOST}")
                custom_og_host = (parsed_og.netloc or parsed_og.path).split(":")[0].lower()
            except Exception:
                pass

        is_ig = any(domain == d or domain.endswith("." + d) for d in ig_domains)
        if not is_ig and custom_og_host and (domain == custom_og_host or domain.endswith("." + custom_og_host)):
            if not (custom_og_host in ("localhost", "127.0.0.1") or "." not in custom_og_host):
                is_ig = True

        if is_ig:
            logger.info(f"Routing Instagram request for {url} through Instagram proxy: {INSTAGRAM_PROXY_URL}")
            proxy_handler = urllib.request.ProxyHandler({'http': INSTAGRAM_PROXY_URL, 'https': INSTAGRAM_PROXY_URL})
            opener = urllib.request.build_opener(proxy_handler)
            try:
                return opener.open(req_or_url, timeout=timeout)
            except Exception as e:
                logger.warning(f"Instagram proxy {INSTAGRAM_PROXY_URL} failed for {url}: {e}. Retrying direct connection...")
                return urllib.request.urlopen(_copy_req(req_or_url), timeout=timeout)

    # Archive.today and Ghostarchive requests
    archive_domains = ("archive.ph", "archive.is", "archive.today", "archive.li", "archive.vn", "archive.md", "archive.fo", "ghostarchive.org")
    if any(domain == d or domain.endswith("." + d) for d in archive_domains):
        proxy = ARCHIVE_TODAY_PROXY_URL or PROXY_URL
        if proxy:
            logger.info(f"Routing archive request for {url} through proxy: {proxy}")
            proxy_handler = urllib.request.ProxyHandler({'http': proxy, 'https': proxy})
            opener = urllib.request.build_opener(proxy_handler)
            try:
                return opener.open(req_or_url, timeout=timeout)
            except Exception as e:
                logger.warning(f"Archive proxy {proxy} failed for {url}: {e}. Retrying direct connection...")
                return urllib.request.urlopen(_copy_req(req_or_url), timeout=timeout)

    # Standard requests matching PROXY_DOMAINS
    if _should_use_proxy(url):
        logger.info(f"Routing request for {url} through proxy: {PROXY_URL}")
        proxy_handler = urllib.request.ProxyHandler({'http': PROXY_URL, 'https': PROXY_URL})
        opener = urllib.request.build_opener(proxy_handler)
        try:
            return opener.open(req_or_url, timeout=timeout)
        except Exception as e:
            logger.warning(f"Proxy {PROXY_URL} failed for {url}: {e}. Retrying direct connection...")
            return urllib.request.urlopen(_copy_req(req_or_url), timeout=timeout)

    return urllib.request.urlopen(req_or_url, timeout=timeout)

def _karakeep_enabled() -> bool:
    """Return True if KaraKeep integration is configured."""
    return bool(KARAKEEP_URL and KARAKEEP_API_KEY)

def _detect_image_mime(data: bytes, filename: str = "") -> str | None:
    """Detect image MIME type from magic header bytes or file extension."""
    if not data:
        return None
    if data.startswith(b'\xff\xd8\xff'):
        return "image/jpeg"
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return "image/png"
    if data.startswith(b'RIFF') and len(data) >= 12 and data[8:12] == b'WEBP':
        return "image/webp"
    if data.startswith(b'GIF87a') or data.startswith(b'GIF89a'):
        return "image/gif"
    if data.startswith(b'BM'):
        return "image/bmp"

    if filename:
        ext = os.path.splitext(filename)[1].lower()
        if ext in ('.jpg', '.jpeg'):
            return "image/jpeg"
        if ext == '.png':
            return "image/png"
        if ext == '.webp':
            return "image/webp"
        if ext == '.gif':
            return "image/gif"
        if ext in ('.heic', '.heif'):
            return "image/heic"
        if ext == '.bmp':
            return "image/bmp"
    return None

def _get_int_field(obj, *keys) -> int | None:
    """Safely extract integer field from object or dict."""
    if not obj:
        return None
    for k in keys:
        val = getattr(obj, k, None) if not isinstance(obj, dict) else obj.get(k)
        if isinstance(val, int) and not isinstance(val, bool):
            return val
        if isinstance(val, str) and val.isdigit():
            return int(val)
    return None

def _download_and_read_msg_file(bot, accid, msg, timeout: float = 15.0) -> tuple[bytes | None, str]:
    """Ensures a message's attachment is downloaded and reads its bytes."""
    if not msg:
        return None, "image/jpeg"

    # 1. If file path is already present and exists, read it
    file_path = getattr(msg, "file", None) if not isinstance(msg, dict) else msg.get("file")
    if isinstance(file_path, (str, bytes, os.PathLike)) and os.path.exists(file_path):
        try:
            with open(file_path, "rb") as f:
                data = f.read(16 * 1024 * 1024)
            mime = _detect_image_mime(data, str(file_path))
            if mime:
                return data, mime
        except Exception as e:
            logger.warning(f"Failed to read image file from message: {e}")

    # 2. Check if we can trigger an on-demand download via RPC
    msg_id = _get_int_field(msg, "id", "message_id", "messageId")
    if not msg_id or not bot or not hasattr(bot, "rpc"):
        return None, "image/jpeg"

    raw_vt = getattr(msg, "view_type", None) if not isinstance(msg, dict) else msg.get("view_type")
    view_type = raw_vt.lower() if isinstance(raw_vt, str) else ""

    raw_fn = getattr(msg, "file_name", None) if not isinstance(msg, dict) else msg.get("file_name")
    file_name = raw_fn.lower() if isinstance(raw_fn, str) else ""

    raw_fb = getattr(msg, "file_bytes", None) if not isinstance(msg, dict) else msg.get("file_bytes")
    file_bytes = raw_fb if isinstance(raw_fb, int) and not isinstance(raw_fb, bool) else 0

    raw_ds = getattr(msg, "download_state", None) if not isinstance(msg, dict) else msg.get("download_state")
    d_state = raw_ds.lower() if isinstance(raw_ds, str) else ""

    has_potential_image = (
        any(t in view_type for t in ("image", "gif", "sticker", "file", "2", "3", "4", "5"))
        or any(file_name.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic"))
        or file_bytes > 0
        or d_state in ("available", "inprogress")
        or isinstance(file_path, (str, bytes, os.PathLike))
    )

    if not has_potential_image:
        return None, "image/jpeg"

    try:
        if hasattr(bot.rpc, "download_full_message"):
            bot.rpc.download_full_message(accid, msg_id)
        elif hasattr(bot.rpc, "download_msg"):
            bot.rpc.download_msg(accid, msg_id)
    except Exception as e:
        logger.warning(f"Could not trigger download_full_message for msg {msg_id}: {e}")

    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            curr_msg = bot.rpc.get_message(accid, msg_id)
            cur_path = getattr(curr_msg, "file", None) if not isinstance(curr_msg, dict) else curr_msg.get("file")
            if isinstance(cur_path, (str, bytes, os.PathLike)) and os.path.exists(cur_path):
                with open(cur_path, "rb") as f:
                    data = f.read(16 * 1024 * 1024)
                mime = _detect_image_mime(data, str(cur_path))
                if mime:
                    return data, mime
            raw_cur_ds = getattr(curr_msg, "download_state", None) if not isinstance(curr_msg, dict) else curr_msg.get("download_state")
            cur_d_state = raw_cur_ds.lower() if isinstance(raw_cur_ds, str) else ""
            if cur_d_state == "failure":
                logger.warning(f"Download failed for msg {msg_id}")
                break
        except Exception as e:
            logger.warning(f"Error while polling downloaded msg {msg_id}: {e}")
            break
        time.sleep(0.3)

    return None, "image/jpeg"

def _extract_image_from_msg_or_quote(bot, accid, msg) -> tuple[bytes | None, str]:
    """Extract image bytes and mime type from direct message attachment, quoted message, or parent reply."""
    # 1. Check direct message attachment (and download on-demand if needed)
    data, mime = _download_and_read_msg_file(bot, accid, msg)
    if data:
        return data, mime

    # 2. Check quote image or quoted message
    quote = getattr(msg, "quote", None) or (msg.get("quote") if isinstance(msg, dict) else None)
    if quote:
        quote_img = getattr(quote, "image", None) or (quote.get("image") if isinstance(quote, dict) else None)
        if quote_img and isinstance(quote_img, (str, bytes, os.PathLike)) and os.path.exists(quote_img):
            try:
                with open(quote_img, "rb") as f:
                    data = f.read(16 * 1024 * 1024)
                mime = _detect_image_mime(data, str(quote_img))
                if mime:
                    return data, mime
            except Exception as e:
                logger.warning(f"Failed to read image file from quote: {e}")

        # Check quoted message ID via RPC (and download on-demand if needed)
        quote_msg_id = _get_int_field(quote, "message_id", "messageId")
        if quote_msg_id and bot and hasattr(bot, "rpc"):
            try:
                q_msg = bot.rpc.get_message(accid, quote_msg_id)
                data, mime = _download_and_read_msg_file(bot, accid, q_msg)
                if data:
                    return data, mime
            except Exception as e:
                logger.warning(f"Failed to fetch quoted message for image extraction: {e}")

    # 3. Fallback: check parent message if message is a reply (and download on-demand if needed)
    parent_id = _get_int_field(msg, "parent_id", "parentId")
    if parent_id and bot and hasattr(bot, "rpc"):
        try:
            p_msg = bot.rpc.get_message(accid, parent_id)
            data, mime = _download_and_read_msg_file(bot, accid, p_msg)
            if data:
                return data, mime
        except Exception as e:
            logger.warning(f"Failed to fetch parent message for image extraction: {e}")

    return None, "image/jpeg"

def _call_gemini_api(
    prompt: str,
    max_tokens: int = 4096,
    temperature: float = 0.3,
    image_bytes: bytes | None = None,
    image_mime: str = "image/jpeg",
) -> str | None:
    """Invokes Google Gemini API with multi-model fallback, rate-limit cooldowns, and API logging."""
    if not GEMINI_API_KEY or (not prompt and not image_bytes):
        return None

    now = time.time()
    active_models = [m for m in GEMINI_MODELS if _GEMINI_MODEL_COOLDOWNS.get(m, 0) <= now]
    if not active_models:
        active_models = list(GEMINI_MODELS)

    # When image is present, only query multimodal models (skip text-only Gemma models)
    if image_bytes:
        mm_active = [m for m in active_models if not m.startswith("gemma-")]
        if mm_active:
            active_models = mm_active
        else:
            active_models = [m for m in GEMINI_MODELS if not m.startswith("gemma-")]

    for model_name in active_models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
        
        parts = []
        if prompt:
            parts.append({"text": prompt})
        if image_bytes:
            b64_img = base64.b64encode(image_bytes).decode("utf-8")
            parts.append({
                "inline_data": {
                    "mime_type": image_mime or "image/jpeg",
                    "data": b64_img
                }
            })

        payload = {
            "contents": [{
                "parts": parts
            }],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens
            }
        }
        
        try:
            database.log_api_call("gemini")
            data_bytes = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                url,
                data=data_bytes,
                headers={
                    'Content-Type': 'application/json',
                    'x-goog-api-key': GEMINI_API_KEY
                }
            )
            with _urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode('utf-8'))
                candidates = body.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts and "text" in parts[0]:
                        res_text = parts[0]["text"].strip()
                        if res_text:
                            if not res_text.rstrip().endswith(('.', '!', '?', '"', "'", ')', '»', '”')):
                                last_p = max(res_text.rfind('.'), res_text.rfind('!'), res_text.rfind('?'))
                                if last_p > 50:
                                    res_text = res_text[:last_p + 1].strip()
                            return res_text
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode('utf-8', errors='ignore')
            except Exception:
                pass
            if e.code == 429:
                logger.warning(f"Gemini API model '{model_name}' rate limited (HTTP 429). Placing on 1-hour cooldown and falling back...")
                _GEMINI_MODEL_COOLDOWNS[model_name] = time.time() + 3600
                continue
            elif e.code in (503, 500, 502, 504):
                logger.warning(f"Gemini API model '{model_name}' unavailable or high demand (HTTP {e.code}). Placing on 5-minute cooldown and falling back...")
                _GEMINI_MODEL_COOLDOWNS[model_name] = time.time() + 300
                continue
            else:
                logger.error(f"Gemini API HTTP Error {e.code}: {e.reason} for model '{model_name}'. Details: {err_body}")
                continue
        except Exception as e:
            logger.warning(f"Gemini API call timed out or failed for model '{model_name}': {e}. Placing on 5-minute cooldown and falling back...")
            _GEMINI_MODEL_COOLDOWNS[model_name] = time.time() + 300
            continue

    return None

def _summarize_text_with_gemini(text: str, title: str | None = None, target_lang: str = "EN", short_paragraph: bool = False, url_key: str | None = None) -> str | None:
    """Summarizes text using Google Gemini API with 24h caching."""
    if not GEMINI_API_KEY or not text or not text.strip():
        return None
    
    clean_text = text.strip()
    if len(clean_text) < 50:
        return None
        
    lang_str = target_lang.strip().upper()
    cache_key = None
    if url_key:
        cache_key = f"{url_key}_{lang_str.lower()}_{'short' if short_paragraph else 'full'}"
        cached = database.get_cached_tldr(cache_key)
        if cached:
            logger.info(f"Returning cached TL;DR summary for {cache_key}")
            return cached

    truncated = clean_text[:20000]
    if lang_str in ("AUTO", ""):
        lang_str = "the language of the text"
        
    title_prefix = f"Title: {title}\n\n" if title else ""
    if short_paragraph:
        prompt = (
            f"Summarize the following text in 1 concise paragraph (2-3 clear sentences) in {lang_str}. "
            "Cover the main event, key details, and why it matters. Do not include introductory words or meta-commentary, output the summary directly:\n\n"
            f"{title_prefix}{truncated}"
        )
        max_tokens = 2048
    else:
        prompt = (
            f"Summarize the key points of the following text in 2-3 informative paragraphs (or key bullet points) in {lang_str}. "
            "Provide a comprehensive overview covering what happened, key points, and context. Do not include meta-commentary, output the summary text directly:\n\n"
            f"{title_prefix}{truncated}"
        )
        max_tokens = 4096

    res_text = _call_gemini_api(prompt, max_tokens=max_tokens, temperature=0.3)
    if res_text and cache_key:
        database.add_cached_tldr(cache_key, res_text)
    return res_text

def _ask_gemini_ai(
    query: str,
    context: str | None = None,
    target_lang: str = "AUTO",
    query_key: str | None = None,
    image_bytes: bytes | None = None,
    image_mime: str = "image/jpeg",
) -> str | None:
    """Answers a question, explains a topic, or analyzes an image using Google Gemini API with 24h caching."""
    if not GEMINI_API_KEY:
        return None
    if not query and not image_bytes and not context:
        return None

    clean_query = query.strip() if query else ""
    clean_context = context.strip() if context and context.strip() else None

    lang_str = target_lang.strip().upper()
    cache_key = None
    if query_key:
        cache_key = f"ai_{query_key}_{lang_str.lower()}"
        cached = database.get_cached_tldr(cache_key)
        if cached:
            logger.info(f"Returning cached AI answer for {cache_key}")
            return cached

    if lang_str in ("AUTO", ""):
        lang_instruction = "the language of the question/topic"
    else:
        lang_instruction = lang_str

    if image_bytes:
        if not clean_query or len(clean_query) < 2:
            prompt = (
                f"Analyze what is depicted in this image in {lang_instruction}.\n"
                "Provide a clear and concise description of the main subject, extract any visible text, and highlight key visual details (from a brief answer up to 2-3 paragraphs maximum).\n"
                "Provide a direct, accurate response without meta-commentary, introductory filler, or conversational preamble. Output the answer directly."
            )
            if clean_context:
                prompt += f"\n\nAdditional Context:\n{clean_context[:8000]}"
        else:
            prompt = (
                f"Analyze the provided image and answer the following question or explain the topic in {lang_instruction}.\n"
                "Keep the answer as concise as appropriate: if the question calls for a direct, short answer (even a single word, number, or sentence), answer concisely; if it requires detailed explanation, provide an informative summary of up to 2-3 paragraphs maximum.\n"
                "Provide a direct, accurate response without meta-commentary, conversational preamble, or introductory filler. Output the answer directly:\n\n"
                f"Question / Request:\n{clean_query}"
            )
            if clean_context:
                prompt += f"\n\nContext:\n{clean_context[:8000]}"
    elif clean_context:
        truncated_ctx = clean_context[:16000]
        prompt = (
            f"Answer the following question or explain the topic based on the provided context in {lang_instruction}.\n"
            "Keep the answer as concise as appropriate: if the question calls for a direct, short answer (even a single word, number, or sentence), answer concisely; if it requires detailed explanation, provide an informative summary of up to 2-3 paragraphs maximum.\n"
            "Provide a direct, accurate response without meta-commentary, conversational preamble, or introductory filler (such as 'Here is...', 'Sure', etc.). Output the answer directly:\n\n"
            f"Context:\n{truncated_ctx}\n\n"
            f"Question / Topic:\n{clean_query}"
        )
    else:
        prompt = (
            f"Answer the following question or explain the topic in {lang_instruction}.\n"
            "Keep the answer as concise as appropriate: if the question calls for a direct, short answer (even a single word, number, or sentence), answer concisely; if it requires detailed explanation, provide an informative overview of up to 2-3 paragraphs maximum.\n"
            "Provide a direct, accurate response without meta-commentary, conversational preamble, or introductory filler (such as 'Here is...', 'Sure', etc.). Output the answer directly:\n\n"
            f"Question / Topic:\n{clean_query}"
        )

    res_text = _call_gemini_api(
        prompt,
        max_tokens=4096,
        temperature=0.4,
        image_bytes=image_bytes,
        image_mime=image_mime,
    )
    if res_text and cache_key:
        database.add_cached_tldr(cache_key, res_text)
    return res_text

def _extract_url_from_msg_or_payload(payload: str, msg) -> str | None:
    """Extract first HTTP/HTTPS URL from payload or quoted message text."""
    if payload:
        url_match = re.search(r'(https?://[^\s<>"]+)', payload)
        if url_match:
            return _clean_url_params(_strip_url_trailing_junk(url_match.group(1)))
    
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
                return _clean_url_params(_strip_url_trailing_junk(url_match.group(1)))
    return None

def _format_preview_caption(title: str, url: str, mode: str, chat_id: int, jina_markdown: str | None = None) -> str:
    """Format caption for /preview and /webxdc, including 1-paragraph TL;DR if Gemini API is available."""
    clean_title = (title or "Webpage").replace("[", "(").replace("]", ")")
    tldr = None
    if GEMINI_API_KEY:
        article_text = jina_markdown
        if not article_text or not article_text.strip():
            try:
                html_str, _ = _download_page_html(url)
                if html_str:
                    doc = Document(html_str)
                    soup = BeautifulSoup(doc.summary(), BS_PARSER)
                    article_text = soup.get_text(separator="\n", strip=True)
            except Exception:
                pass

        if article_text and article_text.strip():
            lang = database.get_chat_lang(chat_id)
            url_key = database.get_or_create_url_hash(url)
            tldr = _summarize_text_with_gemini(article_text, title=clean_title, target_lang=lang, short_paragraph=True, url_key=url_key)
        
    if tldr:
        return f"⚡ TL;DR: {tldr}\n\n🔗 [{clean_title}]({url})"
    
    if mode == "webxdc":
        return f"📱 [{clean_title}]({url})"
    return f"🔗 [{clean_title}]({url})"

def _save_jina_preview_to_cache(url: str, urlhash: str, title: str, jina_markdown: str):
    """
    Translates Jina markdown to HTML, inlines images, saves to cache directory, 
    and registers in previews cache database under readability mode.
    """
    try:
        from bs4 import BeautifulSoup
        import base64
        import urllib.parse
        import datetime

        cleaned_md = _clean_jina_markdown(jina_markdown, title)
        summary = markdown_to_html(cleaned_md)
        soup = BeautifulSoup(summary, BS_PARSER)
        
        _inline_soup_images(soup, url)
        _process_soup_links(soup)

        downloaded_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M GMT")
        domain = urllib.parse.urlparse(url).netloc or "webpage"
        final_html = READABILITY_HTML_TEMPLATE.format(
            title=title,
            url=url,
            domain=domain,
            content=str(soup),
            downloaded_at=downloaded_at
        )

        safe_fname = _safe_filename(domain, False)
        cache_path = os.path.join(CACHE_DIR, safe_fname)
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write(final_html)
            
        filesize = os.path.getsize(cache_path)
        cache_key = f"{urlhash}_readability"
        database.add_cached_preview(cache_key, cache_path, title, filesize)
        logger.info(f"Successfully cached readability preview to {cache_path} for {url} under key {cache_key}")
    except Exception as e:
        logger.error(f"Failed to save readability preview to cache for {url}: {e}")

SAFE_FILE_EXTENSIONS = {
    # PDF
    "pdf",
    # EPUB/DjVu
    "epub", "djvu",
    # Office documents (MS Office, OpenOffice, RTF)
    "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "odt", "ods", "odp",
    "rtf",
    # Text and Data documents
    "txt", "csv", "tsv", "md", "log", "json", "yaml", "yml", "xml"
}

def _get_url_file_info_by_ext(url: str) -> tuple[bool, str, str]:
    """
    Check if the URL path ends with one of the safe file extensions.
    Returns (is_file, filename, extension).
    """
    try:
        parsed = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(parsed.path)
        filename = os.path.basename(path)
        if not filename:
            return False, "", ""
        if "." in filename:
            ext = filename.rsplit(".", 1)[-1].lower()
            if ext in SAFE_FILE_EXTENSIONS:
                return True, filename, ext
    except Exception as e:
        logger.warning(f"Error parsing URL path for extension: {e}")
    return False, "", ""

# Image extensions to auto-download and convert
IMAGE_EXTENSIONS = {
    'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg'
}

IMAGE_CONTENT_TYPES = {
    'image/jpeg', 'image/png', 'image/gif', 'image/bmp', 'image/webp', 'image/svg+xml'
}

def _is_image_url(url: str) -> bool:
    """
    Check if the URL points to an image file.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(parsed.path).lower()
        
        # Check by extension
        if '.' in path:
            ext = path.rsplit('.', 1)[-1]
            if ext in IMAGE_EXTENSIONS:
                return True
        
        # Check by content-type pattern (will be verified via headers)
        if 'image/' in path or '.jpg' in path or '.jpeg' in path or '.png' in path or '.gif' in path or '.bmp' in path:
            return True
    except Exception:
        pass
    return False

def _save_image_as_webp(img, dest, max_dim: int = 800, quality: int = 80) -> tuple[int, int, bool]:
    """
    Resizes (max_dim on longer side) and saves a PIL Image as WebP to dest (filepath or BytesIO).
    Preserves animation if the source image is an animated GIF/WebP.
    Returns (new_width, new_height, is_animated).
    """
    from PIL import Image, ImageSequence

    orig_width, orig_height = img.size
    new_width, new_height = orig_width, orig_height
    if orig_width > max_dim or orig_height > max_dim:
        if orig_width > orig_height:
            new_width = max_dim
            new_height = int(orig_height * (max_dim / orig_width))
        else:
            new_height = max_dim
            new_width = int(orig_width * (max_dim / orig_height))

    is_animated = getattr(img, "is_animated", False) and getattr(img, "n_frames", 1) > 1

    if is_animated:
        frames = []
        durations = []
        for frame in ImageSequence.Iterator(img):
            dur = frame.info.get("duration", 100)
            durations.append(dur if isinstance(dur, int) and dur > 0 else 100)

            f = frame.copy()
            if new_width != orig_width or new_height != orig_height:
                f = f.resize((new_width, new_height), Image.Resampling.LANCZOS)

            has_alpha = "transparency" in f.info or f.mode in ("RGBA", "LA", "P")
            target_mode = "RGBA" if has_alpha else "RGB"
            if f.mode != target_mode:
                f = f.convert(target_mode)
            frames.append(f)

        loop = img.info.get("loop", 0)
        loop_val = loop if isinstance(loop, int) else 0

        frames[0].save(
            dest,
            format="WEBP",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=loop_val,
            quality=quality,
            method=4,
        )
    else:
        f = img.copy() if (new_width != orig_width or new_height != orig_height) else img
        if new_width != orig_width or new_height != orig_height:
            f = f.resize((new_width, new_height), Image.Resampling.LANCZOS)

        has_alpha = "transparency" in f.info or f.mode in ("RGBA", "LA", "P")
        target_mode = "RGBA" if has_alpha else "RGB"
        if f.mode != target_mode:
            f = f.convert(target_mode)

        f.save(dest, format="WEBP", quality=quality)

    return new_width, new_height, is_animated

def _download_and_compress_image(url: str, chat_id: int) -> tuple[bool, str, str]:
    """
    Download an image from URL, check size (max 10MB), compress to WebP with max 800px on longer side.
    Preserves animation for GIFs and animated WebPs.
    Returns (success, cached_filepath_or_error, compress_info_string).
    """
    max_size = 10 * 1024 * 1024  # 10 MB
    
    # Initialize fallback variables
    orig_width, orig_height = 0, 0
    orig_size_str = "0 B"
    filename = "image"
    ext_format = "JPG"
    
    try:
        req = urllib.request.Request(url, headers={'User-Agent': STANDARD_USER_AGENT})
        with _urlopen(req, timeout=10) as response:
            content_type = response.headers.get('Content-Type', '')
            
            # Check if it's an image
            is_image = any(ct in content_type.lower() for ct in IMAGE_CONTENT_TYPES)
            if not is_image:
                # Try to detect from URL extension
                parsed = urllib.parse.urlparse(url)
                path = urllib.parse.unquote(parsed.path).lower()
                if not any(path.endswith(f'.{ext}') for ext in IMAGE_EXTENSIONS):
                    return False, "Not an image", ""
            
            # Check Content-Length
            cl_str = response.headers.get('Content-Length')
            if cl_str:
                try:
                    size = int(cl_str)
                    if size > max_size:
                        return False, f"Image size exceeds 10 MB limit ({_format_size(size)})", ""
                except ValueError:
                    pass
            
            # Download image bytes
            img_bytes = response.read()
            if len(img_bytes) > max_size:
                return False, f"Image size exceeds 10 MB limit ({_format_size(len(img_bytes))})", ""
            
        # Compress and convert to WebP
        try:
            import io
            from PIL import Image
            
            img = Image.open(io.BytesIO(img_bytes))
            orig_width, orig_height = img.size
            orig_size_str = _format_size(len(img_bytes))
            
            # Determine original format
            content_type_lower = ''
            try:
                if 'Content-Type' in response.headers:
                    content_type_lower = response.headers['Content-Type'].lower()
                elif 'content-type' in response.headers:
                    content_type_lower = response.headers['content-type'].lower()
            except Exception:
                pass
            
            if 'png' in content_type_lower or url.lower().endswith('.png'):
                ext_format = 'PNG'
            elif 'gif' in content_type_lower or url.lower().endswith('.gif'):
                ext_format = 'GIF'
            elif 'webp' in content_type_lower or url.lower().endswith('.webp'):
                ext_format = 'WEBP'
            elif 'bmp' in content_type_lower or url.lower().endswith('.bmp'):
                ext_format = 'BMP'
            else:
                ext_format = 'JPG'
            
            # Get filename from URL
            parsed = urllib.parse.urlparse(url)
            path = urllib.parse.unquote(parsed.path)
            filename = os.path.basename(path) or 'image'
            
            # Save as WebP (preserving animation if animated)
            import tempfile
            tmpdir = tempfile.mkdtemp(prefix="image_webp_")
            cached_path = os.path.join(tmpdir, "compressed.webp")
            
            new_width, new_height, is_anim = _save_image_as_webp(img, cached_path, max_dim=800, quality=80)
            
            comp_size_str = _format_size(os.path.getsize(cached_path))
            anim_tag = " Animated" if is_anim else ""
            
            # Build compress info: "file.jpg preview: 952x635 (JPG, 133 KB) -> 800x533 (WebP, 56 KB)"
            compress_info = f"{filename} preview: {orig_width}x{orig_height} ({ext_format}, {orig_size_str}) -> {new_width}x{new_height} (WebP{anim_tag}, {comp_size_str})"
            
            return True, cached_path, compress_info
        except Exception as pillow_err:
            logger.warning(f"Pillow image compression failed: {pillow_err}. Falling back to original.")
            # Fallback: save original as is
            import tempfile
            tmpdir = tempfile.mkdtemp(prefix="image_orig_")
            
            cached_path = os.path.join(tmpdir, f"image.{ext_format.lower()}")
            with open(cached_path, "wb") as f:
                f.write(img_bytes)
            
            return True, cached_path, f"{filename} preview: {orig_width}x{orig_height} ({ext_format}, {orig_size_str}) -> saved as-is"
            
    except Exception as e:
        logger.error(f"Failed to download/compress image {url}: {e}")
        return False, str(e), ""


def _send_image_to_chat(bot, accid, chat_id: int, image_path: str, compress_info: str = "", original_url: str = ""):
    """
    Send an image file to chat as attachment.
    """
    try:
        if not os.path.exists(image_path):
            logger.error(f"Image file not found: {image_path}")
            return
        
        # Send as file/attachment with info
        msg = ""
        if compress_info:
            msg += f"🖼️ {compress_info}"
        
        if original_url:
            msg += f"\n\n🔗 {original_url}"
        
        _send(bot, accid, chat_id, msg, file=image_path)
        
    except Exception as e:
        logger.error(f"Failed to send image to chat: {e}")


def _is_safe_file(filename: str, content_type: str) -> bool:
    """
    Checks if the filename or Content-Type is a safe file.
    """
    if filename:
        if "." in filename:
            ext = filename.rsplit(".", 1)[-1].lower()
            if ext in SAFE_FILE_EXTENSIONS:
                return True
                
    ct = content_type.lower()
    safe_mime_keywords = [
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.oasis.opendocument",
        "application/rtf",
        "text/rtf",
        "text/plain",
        "text/csv",
        "text/tab-separated-values",
        "text/markdown",
        "application/json",
        "application/x-yaml",
        "text/yaml",
        "text/xml",
        "application/xml"
    ]
    if any(keyword in ct for keyword in safe_mime_keywords):
        return True
        
    return False

def _fetch_file_headers_info(url: str) -> tuple[str, int, str]:
    """
    Fetches the headers for the URL to get filename, size, and Content-Type.
    Returns (filename, content_length, content_type).
    """
    response = None
    for ua in [STANDARD_USER_AGENT, NON_MOZILLA_USER_AGENT]:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': ua})
            response = _urlopen(req, timeout=5)
            break
        except Exception as e:
            logger.warning(f"Failed to fetch headers for {url} with UA: {e}")
            
    if not response:
        return "", 0, ""
        
    try:
        headers = response.headers
        content_type = headers.get('Content-Type', '').lower()
        content_length = 0
        cl_str = headers.get('Content-Length')
        if cl_str:
            try:
                content_length = int(cl_str)
            except ValueError:
                pass
                
        # Try to extract filename from Content-Disposition
        filename = ""
        cd = headers.get('Content-Disposition')
        if cd:
            m = re.search(r'filename=["\']?(.*?)["\']?$', cd, re.IGNORECASE)
            if m:
                filename = m.group(1).split(';')[0].strip()
                filename = os.path.basename(filename)
                
        # Fallback to URL path
        if not filename:
            parsed = urllib.parse.urlparse(url)
            path = urllib.parse.unquote(parsed.path)
            filename = os.path.basename(path)
            
        if not filename:
            filename = "file"
            
        return filename, content_length, content_type
    except Exception as e:
        logger.warning(f"Error parsing headers for {url}: {e}")
        return "", 0, ""
    finally:
        try:
            response.close()
        except:
            pass

def _detect_and_get_file_info(url: str) -> tuple[bool, str, int]:
    """
    Determines if the URL is a safe file, and returns (is_file, filename, size_bytes).
    """
    # 1. Fast check by extension
    is_file_ext, filename, ext = _get_url_file_info_by_ext(url)
    
    # 2. Fetch headers to get/validate content-type and size
    fname, size, ctype = _fetch_file_headers_info(url)
    
    if is_file_ext or _is_safe_file(fname or filename, ctype):
        final_filename = fname or filename or "file"
        return True, final_filename, size
        
    return False, "", 0

def _clean_filename(filename: str) -> str:
    cleaned = re.sub(r'[^a-zA-Z0-9._-]', '_', filename)
    cleaned = cleaned.strip('_')
    return cleaned or "file"

def _download_file(url: str, output_path: str) -> tuple[bool, str]:
    """
    Downloads the file from URL to output_path.
    Enforces a maximum size limit of 50 MB during download.
    Returns (success, error_message).
    """
    max_size = 50 * 1024 * 1024 # 50 MB
    
    response = None
    for ua in [STANDARD_USER_AGENT, NON_MOZILLA_USER_AGENT]:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': ua})
            response = _urlopen(req, timeout=15)
            break
        except Exception as e:
            logger.warning(f"Download attempt failed for {url} with UA: {e}")
            
    if not response:
        return False, "Failed to connect to the remote server."
        
    try:
        cl = response.headers.get('Content-Length')
        if cl:
            try:
                if int(cl) > max_size:
                    return False, f"File size exceeds the 50 MB limit."
            except ValueError:
                pass
                
        downloaded = 0
        with open(output_path, 'wb') as f:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > max_size:
                    return False, f"File size exceeds the 50 MB limit."
                f.write(chunk)
                
        return True, ""
    except Exception as e:
        logger.error(f"Error downloading file {url}: {e}")
        return False, str(e)
    finally:
        try:
            response.close()
        except:
            pass

def _is_internal_or_invalid_url(url: str) -> bool:
    """
    Checks if a URL has an internal, private, reserved or invalid domain/IP address.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        if not host:
            return True
            
        host = host.lower()
        
        # 1. Check obvious invalid/test hostnames
        if host in ("localhost", "example", "example.com", "example.org", "example.net", "example.edu"):
            return True
            
        # 1b. Check if host has a dot or is localhost / valid IP
        if "." not in host and host != "localhost":
            is_ip = False
            try:
                ip_str = host.strip("[]")
                ipaddress.ip_address(ip_str)
                is_ip = True
            except ValueError:
                pass
            if not is_ip:
                return True
            
        # 2. Check local domain suffixes
        local_suffixes = (".local", ".lan", ".home", ".internal", ".onion", ".test", ".invalid", ".localhost")
        if host.endswith(local_suffixes):
            return True
            
        # 3. Check if hostname is an IP address (IPv4 or IPv6) and check if it's private/loopback/link-local/reserved
        try:
            ip_str = host.strip("[]")
            ip = ipaddress.ip_address(ip_str)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return True
        except ValueError:
            # Not an IP address, which is fine
            pass
            
    except ValueError as e:
        logger.debug(f"Invalid URL hostname syntax {url}: {e}")
        return True
    except Exception as e:
        logger.warning(f"Error checking internal/invalid URL {url}: {e}")
        return True # Treat as invalid if parsing failed
        
    return False

def _decode_html(html_bytes: bytes, response_headers=None) -> str:
    """
    Decodes html_bytes by detecting the charset from response headers or
    meta tags in HTML. Fallback to utf-8.
    """
    charset = None
    
    # 1. Try to get charset from response headers
    if response_headers:
        try:
            charset = response_headers.get_content_charset()
        except Exception:
            pass

    # 2. If not found in headers, check first 8KB of html_bytes for meta tags
    if not charset:
        try:
            # Decode a prefix using latin-1 (safe for all bytes) to inspect HTML
            chunk = html_bytes[:8192].decode('latin-1', errors='ignore')
            
            # Match <meta charset="...">
            m = re.search(r'<meta\s+charset=["\']?([a-zA-Z0-9_-]+)', chunk, re.IGNORECASE)
            if m:
                charset = m.group(1)
            else:
                # Match <meta http-equiv="Content-Type" content="...; charset=...">
                m = re.search(r'http-equiv=["\']Content-Type["\'][^>]*content=["\'][^"\']*charset=([a-zA-Z0-9_-]+)', chunk, re.IGNORECASE)
                if not m:
                    m = re.search(r'content=["\'][^"\']*charset=([a-zA-Z0-9_-]+)["\'][^>]*http-equiv=["\']Content-Type["\']', chunk, re.IGNORECASE)
                if m:
                    charset = m.group(1)
        except Exception as e:
            logger.debug(f"Failed to extract charset from meta tags: {e}")

    # Fallback to utf-8 if still not found
    if not charset:
        charset = 'utf-8'

    # Try decoding
    try:
        return html_bytes.decode(charset, errors='ignore')
    except Exception as e:
        logger.debug(f"Failed decoding HTML using {charset}: {e}")
        try:
            return html_bytes.decode('utf-8', errors='ignore')
        except Exception:
            return html_bytes.decode('latin-1', errors='ignore')

def compress_image(image_bytes: bytes, max_width=800, quality=70) -> bytes:
    """Compresses an image to WebP, resizing if wider than max_width, preserving animation if present."""
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(image_bytes))
        out_io = io.BytesIO()
        _save_image_as_webp(img, out_io, max_dim=max_width, quality=quality)
        return out_io.getvalue()
    except Exception as e:
        logger.warning(f"Failed to compress image: {e}")
        return image_bytes

def _download_image_bytes(image_url: str) -> bytes | None:
    """Downloads image bytes trying standard and fallback User-Agents."""
    for ua in [STANDARD_USER_AGENT, NON_MOZILLA_USER_AGENT]:
        try:
            req = urllib.request.Request(image_url, headers={'User-Agent': ua})
            with _urlopen(req, timeout=5) as response:
                content_type = response.headers.get('Content-Type', '')
                if 'image' in content_type.lower() or response.status == 200:
                    return response.read()
        except Exception as e:
            logger.debug(f"Failed to fetch image {image_url} with UA {ua}: {e}")
    return None

def _inline_soup_images(soup, url: str):
    """
    Finds all img tags in soup, downloads, compresses, and inlines them as Base64.
    Removes responsive attributes (srcset, sizes, lazy-loading data-src) to force browser
    to render the inlined src data URL.
    """
    import base64
    import urllib.parse

    for img in list(soup.find_all('img')):
        # Support lazy loading placeholders by checking data-src
        img_src = img.get('src') or img.get('data-src')
        if not img_src:
            img.decompose()
            continue
        if img_src.startswith('data:'):
            # Clean up responsive attributes anyway
            for attr in ['srcset', 'sizes', 'data-src', 'data-srcset']:
                if attr in img.attrs:
                    del img[attr]
            continue
            
        absolute_img_url = urllib.parse.urljoin(url, img_src)
        success = False
        try:
            img_bytes = _download_image_bytes(absolute_img_url)
            if img_bytes:
                compressed = compress_image(img_bytes, max_width=800, quality=70)
                mime_type = "image/webp" if compressed.startswith(b"RIFF") else "image/jpeg"
                b64_str = base64.b64encode(compressed).decode('utf-8')
                img['src'] = f"data:{mime_type};base64,{b64_str}"
                success = True
        except Exception as img_err:
            logger.warning(f"Could not inline/compress image {absolute_img_url}: {img_err}")
            
        if success:
            # Clean up responsive attributes that can bypass/override the inlined src
            for attr in ['srcset', 'sizes', 'data-src', 'data-srcset']:
                if attr in img.attrs:
                    del img[attr]
        else:
            img.decompose()

def _process_soup_links(soup):
    """
    Finds all a tags in soup and sets target='_blank' and rel='noopener noreferrer'
    so external links open in system browser without triggering WebXDC CSP errors.
    """
    for a in soup.find_all('a', href=True):
        a['target'] = '_blank'
        a['rel'] = 'noopener noreferrer'

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
            with _urlopen(req, timeout=15) as response:
                content_type = response.headers.get('Content-Type', '')
                if 'text/html' not in content_type.lower():
                    continue
                html_bytes = response.read()
                decoded = _decode_html(html_bytes, response.headers)
                
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
    <title>{title}</title>
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
        <hr style="border: none; border-top: 1px solid var(--border-color); margin-top: 30px; margin-bottom: 20px;">
        <footer style="font-size: 0.85rem; color: var(--muted-color); text-align: center;">
            Page downloaded at {downloaded_at} by <a href="https://git.gluek.info/gluek/deltachat_webpreview" target="_blank" rel="noopener noreferrer" style="color: var(--link-color); text-decoration: none;">Delta Chat WebPreview Bot</a>.
        </footer>
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
        
        widget_patterns = [
            re.compile(r"\bpoll\b|poll-container|article-poll", re.I),
            re.compile(r"comment|disqus|shoutbox", re.I),
            re.compile(r"social|share|recommend|related", re.I),
            re.compile(r"promo|banner|sponsor|advertising|advert", re.I),
            re.compile(r"\bfoot\b|\bheader\b|\bmenu\b|\bnav\b", re.I),
        ]
        
        html_str, err = _download_page_html(url)
        success = False
        title = "Webpage Preview"
        summary = ""

        if html_str:
            try:
                # Pre-clean the HTML of obvious boilerplate/non-content widgets to improve readability extraction
                try:
                    soup_clean = BeautifulSoup(html_str, BS_PARSER)
                    # 1. Drop structural tags
                    for tag_name in ["header", "footer", "nav", "aside"]:
                        for el in list(soup_clean.find_all(tag_name)):
                            el.decompose()
                    
                    to_decompose = []
                    for pattern in widget_patterns:
                        for el in soup_clean.find_all(class_=pattern):
                            to_decompose.append(el)
                        for el in soup_clean.find_all(id=pattern):
                            to_decompose.append(el)
                            
                    for el in to_decompose:
                        if el.parent is not None:
                            el.decompose()
                    
                    # 3. Unwrap namespace wrapper divs (e.g. <div xmlns="http://www.w3.org/1999/xhtml">)
                    # that can confuse readability scoring or cause it to delete the wrapper
                    for el in list(soup_clean.find_all("div")):
                        if el.parent is not None and el.has_attr('xmlns'):
                            el.unwrap()
                    
                    html_str = str(soup_clean)
                except Exception as clean_err:
                    logger.warning(f"Failed to pre-clean HTML: {clean_err}")

                doc = Document(html_str)
                title = doc.title() or "Webpage Preview"
                summary = doc.summary(html_partial=True)
                if summary:
                    text_content = BeautifulSoup(summary, BS_PARSER).get_text().strip()
                    if len(text_content) >= 150:
                        success = True
            except Exception as e:
                logger.warning(f"Standard readability failed: {e}")

        if not success:
            logger.info(f"Standard readability failed or was empty for {url}. Attempting Jina Reader HTML fallback...")
            _, _, jina_html, _ = _fetch_from_jina(url, return_html=True)
            if jina_html:
                try:
                    soup_clean = BeautifulSoup(jina_html, BS_PARSER)
                    # 1. Drop structural tags
                    for tag_name in ["header", "footer", "nav", "aside"]:
                        for el in list(soup_clean.find_all(tag_name)):
                            el.decompose()
                    
                    # 2. Decompose widgets matching common non-content patterns in class/id
                    to_decompose = []
                    for pattern in widget_patterns:
                        for el in soup_clean.find_all(class_=pattern):
                            to_decompose.append(el)
                        for el in soup_clean.find_all(id=pattern):
                            to_decompose.append(el)
                            
                    for el in to_decompose:
                        if el.parent is not None:
                            el.decompose()
                    
                    # 3. Unwrap namespace wrapper divs
                    for el in list(soup_clean.find_all("div")):
                        if el.parent is not None and el.has_attr('xmlns'):
                            el.unwrap()
                    
                    jina_html_cleaned = str(soup_clean)
                    doc = Document(jina_html_cleaned)
                    title = doc.title() or "Webpage Preview"
                    summary = doc.summary(html_partial=True)
                    if summary:
                        text_content = BeautifulSoup(summary, BS_PARSER).get_text().strip()
                        if len(text_content) >= 150:
                            success = True
                except Exception as jina_html_err:
                    logger.warning(f"Readability failed on Jina HTML: {jina_html_err}")

        if not success:
            logger.info(f"Jina Reader HTML fallback failed or was empty. Attempting Jina Reader Markdown fallback...")
            jina_title, _, jina_markdown, jina_warning = _fetch_from_jina(url, return_html=False)
            if jina_markdown:
                title = jina_title or "Webpage Preview"
                cleaned_md = _clean_jina_markdown(jina_markdown, title)
                summary = markdown_to_html(cleaned_md)
                success = True

        if not success:
            return False, err or "Readability failed to extract meaningful content from this page"
            
        soup = BeautifulSoup(summary, BS_PARSER)
        
        _inline_soup_images(soup, url)
        _process_soup_links(soup)
                
        # Format templates
        import datetime
        downloaded_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M GMT")
        domain = urllib.parse.urlparse(url).netloc or "webpage"
        final_html = READABILITY_HTML_TEMPLATE.format(
            title=title,
            url=url,
            domain=domain,
            content=str(soup),
            downloaded_at=downloaded_at
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
            if img_src and isinstance(img_src, str) and img_src.startswith('data:image/'):
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
            orig_size = os.path.getsize(filepath)
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(str(soup))
            comp_size = os.path.getsize(filepath)
            logger.info(f"Successfully compressed images in monolith HTML file {filepath} ({_format_size(orig_size)} -> {_format_size(comp_size)})")
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

def _is_bot_blocked(bot, accid, msg) -> bool:
    """Return True if the message is from a bot and that bot is NOT whitelisted in ALLOWED_BOT_EMAILS."""
    if not getattr(msg, 'is_bot', False):
        return False
        
    allowed_bots_env = os.environ.get("ALLOWED_BOT_EMAILS", "")
    allowed_bots = [e.strip().lower() for e in allowed_bots_env.split(",") if e.strip()]
    
    try:
        contact = bot.rpc.get_contact(accid, msg.from_id)
        sender_email = contact.address.lower().strip() if contact and contact.address else ""
    except Exception:
        sender_email = ""
        
    if sender_email and sender_email in allowed_bots:
        return False  # Allowed
        
    return True  # Blocked

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
    # 3. Instagram Reels and Videos (handled preferentially by YT Bot)
    if any(p in url_lower for p in ("/reel/", "/reels/", "/tv/")) and ("instagram.com" in url_lower or "instagr.am" in url_lower or "oginstagram.com" in url_lower):
        return True
    # 4. Major video/audio hosting sites supported by YT Bot
    other_media_domains = [
        "vimeo.com", "vk.com/video", "vkvideo.ru", "rutube.ru", "soundcloud.com", 
        "tiktok.com", "twitch.tv", "bilibili.com", "dzen.ru", "ok.ru", "coub.com"
    ]
    if any(domain in url_lower for domain in other_media_domains):
        return True
    return False

# ── Message sending helpers with Failover and Stats ──

def _send(bot, accid, chat_id, text, file=None):
    """Send a message and track transport stats."""
    msg_data = MsgData(text=text)
    if file:
        msg_data.file = file
        
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
        logger.error(f"Failed to send message to chat {chat_id}: {e}")
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

def _clean_toml_string(val: str) -> str:
    if not val:
        return ""
    val = val.replace("\r", " ").replace("\n", " ")
    return val.replace("\\", "\\\\").replace('"', '\\"')

def _make_square_icon(image_source_path: str | None, max_dim: int = 128, fmt: str = "JPEG") -> tuple[bytes | None, str]:
    """Helper to crop and resize an image file into a square icon bytes buffer (returns (bytes, filename))."""
    if not image_source_path or not os.path.exists(image_source_path):
        return None, ""
    try:
        from PIL import Image
        import io
        with Image.open(image_source_path) as img:
            ext = "jpg" if fmt == "JPEG" else "png"
            if fmt == "JPEG":
                img = img.convert("RGB")
            else:
                img = img.convert("RGBA")
            width, height = img.size
            min_dim = min(width, height)
            left = (width - min_dim) // 2
            top = (height - min_dim) // 2
            img = img.crop((left, top, left + min_dim, top + min_dim))
            img = img.resize((max_dim, max_dim), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            if fmt == "JPEG":
                img.save(buf, format="JPEG", quality=85, optimize=True)
            else:
                img.save(buf, format="PNG", optimize=True)
            return buf.getvalue(), f"icon.{ext}"
    except Exception as e:
        logger.warning(f"Failed to generate square icon from {image_source_path}: {e}")
        return None, ""

_DEFAULT_BOT_ICON_CACHE: tuple[bytes, str] | None = None

def _get_default_bot_icon_bytes() -> tuple[bytes | None, str]:
    """Returns cached (bytes, filename) of the default 128x128 bot icon PNG, generating it once if needed."""
    global _DEFAULT_BOT_ICON_CACHE
    if _DEFAULT_BOT_ICON_CACHE is not None:
        return _DEFAULT_BOT_ICON_CACHE

    base_dir = os.path.dirname(os.path.abspath(__file__))
    for icon_candidate in ["icon.png", os.path.join("data", "icon.png"), os.path.join(base_dir, "icon.png")]:
        if os.path.exists(icon_candidate):
            b, name = _make_square_icon(icon_candidate, max_dim=128, fmt="PNG")
            if b:
                _DEFAULT_BOT_ICON_CACHE = (b, name)
                return _DEFAULT_BOT_ICON_CACHE
    return None, ""

def _package_webxdc(html_filepath: str, xdc_filepath: str, title: str, source_url: str, og_image_path: str | None = None) -> bool:
    """Packages html_filepath into a .xdc webxdc ZIP package with manifest.toml and optimized icon."""
    try:
        import zipfile
        icon_bytes, icon_name = None, ""
        # 1. Try to generate square icon from OG preview image
        if og_image_path:
            icon_bytes, icon_name = _make_square_icon(og_image_path, max_dim=128, fmt="JPEG")

        # 2. Fallback to cached default bot icon (resized to 128x128 PNG to keep package small and fast)
        if not icon_bytes:
            icon_bytes, icon_name = _get_default_bot_icon_bytes()

        manifest_lines = [
            f'name = "{_clean_toml_string(title)}"',
            f'source_code_url = "{_clean_toml_string(source_url)}"',
        ]
        if icon_bytes and icon_name:
            manifest_lines.append(f'icon = "{icon_name}"')
        manifest_content = "\n".join(manifest_lines) + "\n"

        with zipfile.ZipFile(xdc_filepath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            zf.write(html_filepath, arcname="index.html")
            zf.writestr("manifest.toml", manifest_content)
            if icon_bytes and icon_name:
                zf.writestr(icon_name, icon_bytes)
        return True
    except Exception as e:
        logger.error(f"Failed to package WebXDC file: {e}")
        return False


async def _run_monolith_process(cmd: list, url: str | None = None) -> tuple[int | None, str]:
    """Execute monolith process with timeout."""
    env = os.environ.copy()
    if url:
        try:
            parsed = urllib.parse.urlparse(url)
            domain = parsed.netloc.lower()
            if ":" in domain:
                domain = domain.split(":")[0]
        except Exception:
            domain = ""

        if domain == "r.jina.ai":
            if JINA_PROXY_URL:
                env["http_proxy"] = JINA_PROXY_URL
                env["https_proxy"] = JINA_PROXY_URL
                env["HTTP_PROXY"] = JINA_PROXY_URL
                env["HTTPS_PROXY"] = JINA_PROXY_URL
                logger.info(f"Routing monolith subprocess for Jina URL {url} through Jina proxy: {JINA_PROXY_URL}")
        elif _should_use_proxy(url):
            env["http_proxy"] = PROXY_URL
            env["https_proxy"] = PROXY_URL
            env["HTTP_PROXY"] = PROXY_URL
            env["HTTPS_PROXY"] = PROXY_URL
            logger.info(f"Routing monolith subprocess for {url} through proxy: {PROXY_URL}")
    proc: asyncio.subprocess.Process | None = None
    returncode: int = -99  # Default return code for exception
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=65)
        returncode = proc.returncode if proc is not None else 0
        return returncode, stderr.decode(errors='replace').strip()
    except asyncio.TimeoutError:
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except: pass
        return -1, "Operation timed out (60 second limit exceeded)"
    except Exception as e:
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except: pass
        return returncode, str(e)

def is_likely_meta(line: str) -> bool:
    line = line.strip()
    if not line:
        return True
    
    months_rx = r"(?:янв|фев|мар|апр|май|июн|июл|авг|сен|окт|ноя|дек|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    if re.search(r'\b\d{1,2}\s+' + months_rx, line, re.I):
        return True
    if re.search(r'\b\d{2}:\d{2}\b', line):
        return True
    
    cat_match = re.match(r'^\[([^\]]{1,30})\]\(https?://[^\)]+\)$', line)
    if cat_match:
        return True
        
    return False

FOOTER_INDICATORS = [
    r'^(?:#+\s+|\*\*|)?Авторы(?:\*\*|)?$',
    r'^(?:#+\s+|\*\*|)?Теги(?:\*\*|)?$',
    r'^(?:#+\s+|\*\*|)?Персоны(?:\*\*|)?$',
    r'^(?:#+\s+|\*\*|)?Материалы по теме(?:\*\*|)?$',
    r'^(?:#+\s+|\*\*|)?Читайте также(?:\*\*|)?$',
    r'^(?:#+\s+|\*\*|)?Интересное(?:\*\*|)?$',
    r'^(?:#+\s+|\*\*|)?Новости партн(?:е|ё)ров(?:\*\*|)?$',
    r'^(?:#+\s+|\*\*|)?Рубрики(?:\*\*|)?$',
    r'^(?:#+\s+|\*\*|)?Новости регионов(?:\*\*|)?$',
    r'^(?:#+\s+|\*\*|)?Социальные сети(?:\*\*|)?$',
    r'^(?:#+\s+|\*\*|)?Подписки(?:\*\*|)?$',
    r'^(?:#+\s+|\*\*|)?Уведомления(?:\*\*|)?$',
    r'^(?:#+\s+|\*\*|)?Другие продукты РБК(?:\*\*|)?$',
    r'^(?:#+\s+|\*\*|)?РБК Новости(?:\*\*|)?$',
    r'^© ООО «БИЗНЕСПРЕСС»',
    r'^Владельцем сайта является',
    r'^Чтобы отправить редакции сообщение',
    r'^(?:#+\s+|\*\*|)?Тематические программы',
    r'^(?:#+\s+|\*\*|)?Related Articles(?:\*\*|)?$',
    r'^(?:#+\s+|\*\*|)?Share this(?:\*\*|)?$',
    r'^(?:#+\s+|\*\*|)?Recommended for you(?:\*\*|)?$',
]

def _clean_jina_markdown(markdown_text: str, title: str | None = None) -> str:
    if not markdown_text:
        return ""
    
    lines = markdown_text.split('\n')
    
    title_idx = -1
    if title:
        norm_title = re.sub(r'\W+', '', title).lower()
        # Phase 1: Look for heading/bold lines matching the title
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            is_heading = stripped.startswith('#') or (stripped.startswith('**') and stripped.endswith('**'))
            if not is_heading:
                continue
            norm_line = re.sub(r'\W+', '', stripped).lower()
            if norm_title and norm_line and (norm_title in norm_line or norm_line in norm_title):
                title_idx = idx
                break
                
        # Phase 2: Fallback to any line matching the title
        if title_idx == -1:
            for idx, line in enumerate(lines):
                stripped = line.strip()
                if not stripped:
                    continue
                norm_line = re.sub(r'\W+', '', stripped).lower()
                if norm_title and norm_line and (norm_title in norm_line or norm_line in norm_title):
                    title_idx = idx
                    break
                
    if title_idx == -1:
        for idx, line in enumerate(lines):
            if line.strip().startswith('# '):
                title_idx = idx
                break
                
    start_idx = 0
    if title_idx != -1:
        start_idx = title_idx
        for back_idx in range(title_idx - 1, max(-1, title_idx - 6), -1):
            line = lines[back_idx]
            if is_likely_meta(line):
                start_idx = back_idx
            else:
                break
                
    content_lines = lines[start_idx:]
    
    footer_idx = -1
    for idx, line in enumerate(content_lines):
        stripped = line.strip()
        for pattern in FOOTER_INDICATORS:
            if re.match(pattern, stripped, re.IGNORECASE):
                footer_idx = idx
                break
        if footer_idx != -1:
            break
            
    if footer_idx != -1:
        content_lines = content_lines[:footer_idx]
        
    return '\n'.join(content_lines).strip()

def _parse_jina_response(text: str) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Parses r.jina.ai response.
    Returns (title, image_url, markdown_content, warning).
    """
    title = None
    image_url = None
    markdown_content = None
    warning = None

    title_match = re.search(r'^Title:[ \t]*(.*)$', text, re.MULTILINE | re.IGNORECASE)
    if title_match:
        title = title_match.group(1).strip()

    warning_match = re.search(r'^Warning:[ \t]*(.*)$', text, re.MULTILINE | re.IGNORECASE)
    if warning_match:
        warning = warning_match.group(1).strip()

    md_match = re.search(r'Markdown Content:\s*(.*)', text, re.DOTALL | re.IGNORECASE)
    if md_match:
        markdown_content = md_match.group(1).strip()

    if markdown_content:
        # Match markdown image, e.g. ![alt](url)
        img_match = re.search(r'!\[[^\]]*\]\(([^)\s]+)', markdown_content)
        if img_match:
            image_url = img_match.group(1).strip().strip('"\'')

    return title, image_url, markdown_content, warning

def _fetch_from_jina(url: str, return_html: bool = False) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Queries Jina AI Reader to fetch the page metadata and content.
    Returns (title, image_url, content_str, warning).
    If return_html is True, content_str is the raw HTML and other values are None.
    """
    jina_url = f"https://r.jina.ai/{url}"
    try:
        logger.info(f"Querying Jina AI Reader for URL: {url} (return_html={return_html})")
        database.log_api_call("jina")
        headers = {
            'User-Agent': 'curl/7.88.1'
        }
        if return_html:
            headers['X-Return-Format'] = 'html'
        else:
            headers['X-Exclude-Selector'] = 'nav, footer, header, aside, .sidebar, .ads, .ad, .promo, .comments, .related, .popup, #footer, #header, #sidebar, #nav, #menu, .header, .footer, .menu, .nav'
            
        req = urllib.request.Request(
            jina_url,
            headers=headers
        )
        if JINA_API_KEY:
            req.add_header('Authorization', f'Bearer {JINA_API_KEY}')
        if _should_use_proxy(url):
            logger.info(f"Adding X-Proxy-Url header for Jina crawl request to {url}: {PROXY_URL}")
            req.add_header('X-Proxy-Url', PROXY_URL)
        with _urlopen(req, timeout=15) as response:
            text_bytes = response.read(1024 * 1024) # Read up to 1MB of content
            text = _decode_html(text_bytes, response.headers)
            if return_html:
                return None, None, text, None
            return _parse_jina_response(text)
    except Exception as e:
        logger.warning(f"Jina AI Reader fetch failed for {url}: {e}")
        return None, None, None, None

def markdown_to_html(md_text: str) -> str:
    """
    Converts a simple markdown string to HTML.
    """
    import html
    escaped = html.escape(md_text)

    # Convert images and links first, wrapping generated HTML tags in special placeholders
    # so we don't touch their contents during subsequent replacements.
    def repl_img(match):
        alt = match.group(1)
        url = match.group(2)
        return f'___HTML_TAG_START___img src="{url}" alt="{alt}" style="max-width: 100%; height: auto; display: block; margin: 10px auto; border-radius: 8px;"___HTML_TAG_END___'

    def repl_link(match):
        text = match.group(1)
        url = match.group(2)
        return f'___HTML_TAG_START___a href="{url}" style="color: var(--link-color); text-decoration: none;"___HTML_TAG_END___{text}___HTML_TAG_START___/a___HTML_TAG_END___'

    text = re.sub(
        r'!\[(.*?)\]\(([^)\s]+)(?:\s+(?:["\']|&quot;|&#x27;).*?(?:["\']|&quot;|&#x27;))?\)',
        repl_img,
        escaped
    )
    text = re.sub(
        r'\[(.*?)\]\(([^)\s]+)(?:\s+(?:["\']|&quot;|&#x27;).*?(?:["\']|&quot;|&#x27;))?\)',
        repl_link,
        text
    )

    lines = text.splitlines()
    html_lines = []
    in_list = False
    in_code = False

    for line in lines:
        stripped = line.strip()

        # Code blocks
        if stripped.startswith("```"):
            if in_code:
                html_lines.append("</pre>")
                in_code = False
            else:
                html_lines.append('<pre style="background: var(--card-bg); border: 1px solid var(--border-color); padding: 10px; border-radius: 6px; overflow-x: auto; font-family: monospace;">')
                in_code = True
            continue

        if in_code:
            html_lines.append(line)
            continue

        # Headers
        header_match = re.match(r'^(#{1,6})\s+(.*)$', line)
        if header_match:
            level = len(header_match.group(1))
            content = _process_inline_formatting(header_match.group(2))
            html_lines.append(f"<h{level}>{content}</h{level}>")
            continue

        # Lists
        list_match = re.match(r'^[-*+]\s+(.*)$', line)
        if list_match:
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            content = _process_inline_formatting(list_match.group(1))
            html_lines.append(f"<li>{content}</li>")
            continue
        else:
            if in_list:
                html_lines.append("</ul>")
                in_list = False

        # Blockquotes
        blockquote_match = re.match(r'^>\s+(.*)$', line)
        if blockquote_match:
            content = _process_inline_formatting(blockquote_match.group(1))
            html_lines.append(f'<blockquote style="border-left: 4px solid var(--border-color); padding-left: 10px; margin: 10px 0; color: var(--muted-color);">{content}</blockquote>')
            continue

        # Paragraph or empty line
        if not stripped:
            html_lines.append("<br/>")
        else:
            content = _process_inline_formatting(line)
            html_lines.append(f"<p>{content}</p>")

    if in_list:
        html_lines.append("</ul>")
    if in_code:
        html_lines.append("</pre>")

    result = "\n".join(html_lines)
    result = result.replace("___HTML_TAG_START___", "<").replace("___HTML_TAG_END___", ">")
    return result

def _process_inline_formatting(text: str) -> str:
    parts = re.split(r'(___HTML_TAG_START___.*?___HTML_TAG_END___)', text)
    processed_parts = []
    for part in parts:
        if part.startswith('___HTML_TAG_START___') and part.endswith('___HTML_TAG_END___'):
            processed_parts.append(part)
        else:
            # Bold
            part = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', part)
            part = re.sub(r'__(.*?)__', r'<strong>\1</strong>', part)
            # Italic
            part = re.sub(r'\*(.*?)\*', r'<em>\1</em>', part)
            part = re.sub(r'_(.*?)_', r'<em>\1</em>', part)
            processed_parts.append(part)
    return "".join(processed_parts)

def _extract_youtube_id_from_invidious(url: str) -> str | None:
    """Extract YouTube video ID from an Invidious URL."""
    try:
        parsed = urllib.parse.urlparse(url)
        query_params = urllib.parse.parse_qs(parsed.query)
        if 'v' in query_params:
            return query_params['v'][0]
        
        path_parts = parsed.path.strip('/').split('/')
        if len(path_parts) >= 2 and path_parts[0] in ('embed', 'v'):
            return path_parts[1]
    except Exception as e:
        logger.warning(f"Failed to extract video ID from Invidious URL {url}: {e}")
    return None

def _is_telegram_url(url: str) -> bool:
    try:
        host = url.split("//", 1)[-1].split("/")[0].lower().split(":")[0]
        return host in ("t.me", "www.t.me", "telegram.me")
    except Exception:
        return False

def _fetch_telegram_og_data(url: str) -> tuple[str | None, str | None, str | None]:
    """
    Fetch title, preview image and content for a t.me post URL from Telegram's public preview page.

    Supports:
      https://t.me/channel/123
      https://t.me/s/channel/123
      https://t.me/c/123456789/123   (private channel numeric id - will return None, None, None)

    Returns (title, thumbnail_url, markdown_content) or (None, None, None) if extraction fails.
    The title is composed as "channel_name: post_text_excerpt".
    """
    from bs4 import BeautifulSoup
    import copy

    try:
        parsed = urllib.parse.urlparse(url)
        # Normalise: strip leading slash and 's/' prefix
        path = parsed.path.lstrip("/")
        if path.startswith("s/"):
            path = path[2:]

        parts = [p for p in path.split("/") if p]
        if len(parts) < 2:
            return None, None, None

        channel = parts[0]
        post_id = parts[1]

        # Private channels (t.me/c/...) cannot be fetched via public web preview
        if channel.lower() == "c":
            return None, None, None

        # Build canonical public preview URL
        preview_url = f"https://t.me/s/{channel}/{post_id}"

        logger.info(f"Fetching Telegram public preview for {preview_url}")
        req = urllib.request.Request(
            preview_url,
            headers={"User-Agent": STANDARD_USER_AGENT}
        )
        with _urlopen(req, timeout=8) as response:
            html = response.read(512 * 1024).decode("utf-8", errors="replace")

        soup = BeautifulSoup(html, BS_PARSER)
        post_attr = f"{channel}/{post_id}".lower()

        # Find the specific message wrapper
        msg_div = None
        for div in soup.find_all(class_="tgme_widget_message"):
            data_post = div.get("data-post")
            if data_post is not None:
                if isinstance(data_post, str):
                    if data_post.lower() == post_attr:
                        msg_div = div
                        break
                else:
                    # data-post is an AttributeValueList
                    if div.get("data-post") is not None:
                        msg_div = div
                        break

        if not msg_div:
            logger.warning(f"Could not find message div for data-post: {post_attr} on page {preview_url}")
            return None, None, None

        # Extract author name
        author_span = msg_div.find(class_="tgme_widget_message_owner_name")
        author = author_span.get_text().strip() if author_span else channel

        # Extract text content (preserving line breaks)
        text_div = msg_div.find(class_="tgme_widget_message_text")
        text_content = ""
        if text_div:
            text_div_copy = copy.copy(text_div)
            for br in text_div_copy.find_all("br"):
                br.replace_with("\n")
            text_content = text_div_copy.get_text().strip()

        # Build excerpt & title
        if text_content:
            clean_title_text = re.sub(r"\s+", " ", text_content).strip()
            excerpt = clean_title_text[:200] + ("…" if len(clean_title_text) > 200 else "")
            title = f"{author}: {excerpt}"
        else:
            title = author

        # Extract media/thumbnail URL from photo/video/roundvideo thumb or link preview image
        thumb_url = None
        photo_wrap = msg_div.find(class_=["tgme_widget_message_photo_wrap", "tgme_widget_message_video_thumb", "tgme_widget_message_roundvideo_thumb", "link_preview_image"])
        if photo_wrap:
            style = photo_wrap.get("style", "")
            match = re.search(r"background-image:\s*url\(['\"]?(.*?)['\"]?\)", style)
            if match:
                thumb_url = match.group(1)

        # Construct markdown content for caching/readability preview
        md_lines = []
        md_lines.append(f"# {author}")
        md_lines.append("")
        if text_content:
            md_lines.append(text_content)
            md_lines.append("")
        if thumb_url:
            md_lines.append(f"![Media]({thumb_url})")

        jina_markdown = "\n".join(md_lines).strip()
        return title, thumb_url, jina_markdown

    except Exception as e:
        logger.warning(f"Telegram preview fetch/parse failed for {url}: {e}")
        return None, None, None

def _is_instagram_url(url: str) -> bool:
    """Return True if the URL is an Instagram or OGInstagram URL."""
    try:
        host = url.split("//", 1)[-1].split("/")[0].lower().split(":")[0]
        if host in (
            "instagram.com", "www.instagram.com", "instagr.am", "www.instagr.am",
            "oginstagram.com", "www.oginstagram.com", "d.oginstagram.com", "www.d.oginstagram.com",
            "g.oginstagram.com", "www.g.oginstagram.com"
        ):
            return True
        if OGINSTAGRAM_HOST:
            og_host = OGINSTAGRAM_HOST.lower()
            if "://" in og_host:
                og_host = og_host.split("://", 1)[1]
            og_host = og_host.split("/")[0].split(":")[0]
            if og_host and (host == og_host or host.endswith("." + og_host)):
                return True
    except Exception:
        pass
    return False

def _fetch_instagram_og_data(url: str) -> tuple[str | None, str | None, str | None]:
    """
    Fetch title, preview image and content for an Instagram URL via OGInstagram / mirror embed proxy.

    Supports:
      https://instagram.com/p/SHORTCODE
      https://instagram.com/reel/SHORTCODE or /reels/SHORTCODE
      https://instagram.com/stories/username/ID
      https://instagram.com/username
      https://d.oginstagram.com/p/SHORTCODE (and other oginstagram hosts)

    Extracts author, caption, alt-text (accessibility description), and unblocked image URL.
    Returns (title, thumbnail_url, markdown_content) or (None, None, None) if extraction fails.
    The title is composed as "author: caption_or_alt_excerpt".
    """
    from bs4 import BeautifulSoup
    import html

    try:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        if not path or path == "/":
            return None, None, None

        og_host = (OGINSTAGRAM_HOST or "oginstagram.com").strip()
        hosts_to_try = [og_host]
        for fallback_host in ("kkinstagram.com", "kkclip.com", "vxinstagram.com"):
            if fallback_host not in hosts_to_try:
                hosts_to_try.append(fallback_host)

        for host in hosts_to_try:
            if host.startswith(("http://", "https://")):
                base_url = host.rstrip("/")
            elif ":" in host or host.startswith("localhost") or "." not in host:
                base_url = f"http://{host.rstrip('/')}"
            else:
                base_url = f"https://{host.rstrip('/')}"

            target_url = f"{base_url}{path}"
            if parsed.query:
                target_url += f"?{parsed.query}"

            logger.info(f"Fetching Instagram preview from {target_url}")
            html_content = ""
            try:
                req = urllib.request.Request(
                    target_url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"}
                )
                with _urlopen(req, timeout=8) as response:
                    content_type = response.headers.get("Content-Type", "").lower()

                    # Direct image returned by proxy
                    if "image/" in content_type:
                        logger.info(f"Direct image returned by Instagram proxy {host} for {url}")
                        image_url = target_url
                        clean_path = path.strip("/")
                        parts = clean_path.split("/")
                        if len(parts) >= 2 and parts[1] in ("p", "reel", "reels"):
                            author = f"Instagram (@{parts[0]})"
                        elif len(parts) == 1 and parts[0] not in ("p", "reel", "reels", "stories"):
                            author = f"Instagram (@{parts[0]})"
                        else:
                            author = "Instagram"
                        return author, image_url, f"# {author}\n\n![Media]({image_url})"

                    html_bytes = response.read(512 * 1024)
                    html_content = _decode_html(html_bytes, response.headers)
            except Exception as host_err:
                logger.warning(f"Fetch failed for Instagram host {host}: {host_err}")
                continue

            if not html_content or "Just a moment..." in html_content or "Testing to determine if you are a bot" in html_content:
                logger.warning(f"Instagram proxy {host} returned challenge/empty content, trying next fallback...")
                continue

            soup = BeautifulSoup(html_content, BS_PARSER)

            # 1. Extract title (author name / profile)
            title = None
            for tag in soup.find_all("meta"):
                prop = tag.get("property", "") or tag.get("name", "")
                if prop.lower() in ("og:title", "twitter:title") and tag.get("content"):
                    title = html.unescape(tag["content"].strip())
                    break

            if not title and soup.title and soup.title.string:
                title = html.unescape(soup.title.string.strip())

            author = title or "Instagram"

            # 2. Extract description (post caption)
            description = None
            for tag in soup.find_all("meta"):
                prop = tag.get("property", "") or tag.get("name", "")
                if prop.lower() in ("og:description", "twitter:description") and tag.get("content"):
                    description = html.unescape(tag["content"].strip())
                    break

            # 3. Extract image alt text (accessibility caption)
            alt_text = None
            for tag in soup.find_all("meta"):
                prop = tag.get("property", "") or tag.get("name", "")
                if prop.lower() in ("og:image:alt", "twitter:image:alt") and tag.get("content"):
                    alt_text = html.unescape(tag["content"].strip())
                    break

            # 4. Extract image URL
            image_url = None
            for tag in soup.find_all("meta"):
                prop = tag.get("property", "") or tag.get("name", "")
                if prop.lower() in ("og:image", "twitter:image") and tag.get("content"):
                    image_url = html.unescape(tag["content"].strip())
                    if image_url:
                        image_url = urllib.parse.urljoin(target_url, image_url)
                    break

            # Fallback to direct media endpoint if no image meta tag found
            if not image_url and path.startswith(("/p/", "/reel/", "/reels/", "/stories/")):
                if base_url.startswith("http://") or ":" in host or "." not in host:
                    image_url = f"{base_url}/d{path}"
                else:
                    image_url = f"https://d.{host}{path}"
                if parsed.query:
                    image_url += f"?{parsed.query}"

            # 5. Build formatted title / excerpt
            content_text = description or alt_text or ""
            if content_text:
                clean_text = re.sub(r"\s+", " ", content_text).strip()
                excerpt = clean_text[:200] + ("…" if len(clean_text) > 200 else "")
                formatted_title = f"{author}: {excerpt}"
            else:
                formatted_title = author

            # 6. Build markdown representation for cache / readability / webxdc
            md_lines = []
            md_lines.append(f"# {author}")
            md_lines.append("")
            if description:
                md_lines.append(description.strip())
                md_lines.append("")
            if alt_text and alt_text.strip() != (description or "").strip():
                md_lines.append(f"*{alt_text.strip()}*")
                md_lines.append("")
            if image_url:
                md_lines.append(f"![Media]({image_url})")

            jina_markdown = "\n".join(md_lines).strip()
            return formatted_title, image_url, jina_markdown

    except Exception as e:
        logger.warning(f"Instagram preview fetch/parse failed for {url}: {e}")
        return None, None, None

    return None, None, None

def _get_og_preview_data(url: str) -> tuple[str, str | None, bool, str | None, str | None]:
    """
    Fetches the URL and extracts og:title (or fallback title) and og:image URL.
    Returns (title, og_image_url, is_invidious, warning, jina_markdown).
    """
    jina_markdown = None

    # --- Telegram posts: use oEmbed API directly -------------------------
    try:
        # Extract netloc without touching urllib (shadowed later by local import)
        _tg_host = url.split("//", 1)[-1].split("/")[0].lower().split(":")[0]
        if _tg_host in ("t.me", "www.t.me", "telegram.me"):
            tg_title, tg_thumb, tg_md = _fetch_telegram_og_data(url)
            if tg_title:
                return tg_title, tg_thumb, False, None, tg_md
    except Exception as _tg_err:
        logger.warning(f"Telegram early-return failed for {url}: {_tg_err}")
    # ---------------------------------------------------------------------

    # --- Instagram posts: use OGInstagram embed proxy --------------------
    try:
        if _is_instagram_url(url):
            ig_title, ig_image, ig_md = _fetch_instagram_og_data(url)
            if ig_title:
                return ig_title, ig_image, False, None, ig_md
    except Exception as _ig_err:
        logger.warning(f"Instagram early-return failed for {url}: {_ig_err}")
    # ---------------------------------------------------------------------




    def parse_html(html_content: str) -> tuple[str | None, str | None]:
        # 1. Extract title
        title = None
        title_m = re.search(r'<meta[^>]*(?:property|name)=["\']og:title["\'][^>]*content=(["\'])([^>]*?)\1', html_content, re.IGNORECASE)
        if not title_m:
            title_m = re.search(r'<meta[^>]*content=(["\'])([^>]*?)\1[^>]*(?:property|name)=["\']og:title["\']', html_content, re.IGNORECASE)
        if title_m:
            import html
            title = html.unescape(title_m.group(2).strip())
        
        if not title:
            # Fallback to <title>
            title_m = re.search(r'<title[^>]*>(.*?)</title>', html_content, re.IGNORECASE | re.DOTALL)
            if title_m:
                import html
                title = html.unescape(title_m.group(1).strip())
        
        # 2. Extract og:image
        image_url = None
        img_m = re.search(r'<meta[^>]*(?:property|name)=["\']og:image["\'][^>]*content=(["\'])([^>]*?)\1', html_content, re.IGNORECASE)
        if not img_m:
            img_m = re.search(r'<meta[^>]*content=(["\'])([^>]*?)\1[^>]*(?:property|name)=["\']og:image["\']', html_content, re.IGNORECASE)
        if not img_m:
            img_m = re.search(r'<meta[^>]*(?:property|name)=["\']twitter:image["\'][^>]*content=(["\'])([^>]*?)\1', html_content, re.IGNORECASE)
        
        if img_m:
            import html
            image_url = html.unescape(img_m.group(2).strip())
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
    is_invidious = False
    domain = urllib.parse.urlparse(url).netloc.lower()
    try:
        if database.get_config(f"invidious_domain_{domain}") == "1":
            is_invidious = True
    except Exception:
        pass

    # First attempt with standard User-Agent
    html_head = None
    try:
        req = urllib.request.Request(
            url, 
            headers={'User-Agent': STANDARD_USER_AGENT}
        )
        with _urlopen(req, timeout=5) as response:
            content_type = response.headers.get('Content-Type', '')
            if 'text/html' in content_type.lower():
                # Read first 512KB
                html_bytes = response.read(512 * 1024)
                html_head = _decode_html(html_bytes, response.headers)
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
            with _urlopen(req, timeout=5) as response:
                content_type = response.headers.get('Content-Type', '')
                if 'text/html' in content_type.lower():
                    # Read first 512KB
                    html_bytes = response.read(512 * 1024)
                    html_head = _decode_html(html_bytes, response.headers)
                # Succeeded, clear any recorded hard failure code
                hard_failure_code = None
        except urllib.error.HTTPError as e:
            logger.warning(f"Fallback fetch failed for {url} with non-Mozilla User-Agent: HTTP Error {e.code}: {e.reason}")
            if e.code in (401, 403, 404):
                hard_failure_code = e.code
        except Exception as e:
            logger.warning(f"Fallback fetch failed for {url} with non-Mozilla User-Agent: {e}")

    # Parse and extract
    title = None
    image_url = None
    if html_head is not None:
        title, image_url = parse_html(html_head)
        if "alternative front-end to YouTube" in html_head or "alternative frontend to YouTube" in html_head:
            is_invidious = True

    netloc = urllib.parse.urlparse(url).netloc or "Webpage"
    is_fallback_title = (not title) or (title.strip() == netloc) or (title.strip() == "Webpage")

    # If standard fetch was blocked/failed OR we got a fallback title OR we have no preview image, try Jina
    warning = None
    if is_fallback_title or not image_url or hard_failure_code is not None:
        logger.info(f"Standard OG parse didn't get complete title/image for {url}. Trying Jina.ai fallback...")
        jina_title, jina_image, jina_markdown, jina_warning = _fetch_from_jina(url)
        if jina_title and jina_title.strip():
            title = jina_title
            # Clear hard failure code if Jina fetch succeeded!
            hard_failure_code = None
        elif jina_warning:
            # Empty title but warning exists: use URL Source as fallback title
            title = f"URL Source: {url}"
            hard_failure_code = None
            
        if jina_image:
            image_url = jina_image
        if jina_markdown and ("alternative front-end to YouTube" in jina_markdown or "alternative frontend to YouTube" in jina_markdown):
            is_invidious = True
        if jina_warning:
            warning = jina_warning

    if is_invidious:
        try:
            database.set_config(f"invidious_domain_{domain}", "1")
        except Exception as db_err:
            logger.warning(f"Failed to save Invidious domain {domain} to config: {db_err}")

    if hard_failure_code is not None:
        return "__FAILED_BLOCK__", f"HTTP {hard_failure_code}", is_invidious, None, None

    if title:
        return title, image_url, is_invidious, warning, jina_markdown

    return netloc, None, is_invidious, warning, None

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
        # Non-webpage/binary files (ZIP, MP4, PDFs, audio, video, zip, rar, tar, exe, dmg, etc.)
        binary_types = [
            "application/zip", "application/x-zip-compressed",
            "video/", "audio/", "application/pdf", "application/x-rar-compressed",
            "application/x-tar", "application/x-executable", "application/x-msdownload",
            "application/x-apple-diskimage"
        ]
        if any(bt in content_type for bt in binary_types) and 'text/html' not in content_type:
            return "BINARY_TYPE", content_length, content_type
            
        # For application/octet-stream, only reject if it also looks like a binary file by extension
        if "application/octet-stream" in content_type and 'text/html' not in content_type:
            try:
                parsed_url = urllib.parse.urlparse(url)
                path = urllib.parse.unquote(parsed_url.path).lower()
                binary_extensions = (
                    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".exe", ".dmg", ".pkg", ".deb", ".rpm",
                    ".msi", ".iso", ".bin", ".pdf", ".mp4", ".mp3", ".mov", ".avi", ".mkv", ".wav", ".flac", ".ogg"
                )
                if path.endswith(binary_extensions):
                    return "BINARY_TYPE", content_length, content_type
                # Otherwise, let it pass so it can proceed to standard fetch / Jina AI fallback!
                logger.info(f"Allowing application/octet-stream for {url} because it does not match known binary extensions.")
            except Exception:
                return "BINARY_TYPE", content_length, content_type
            
        return None, content_length, content_type

    import urllib.error
    
    # Standard attempt
    try:
        req = urllib.request.Request(url, headers={'User-Agent': STANDARD_USER_AGENT})
        with _urlopen(req, timeout=5) as response:
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
        with _urlopen(req, timeout=5) as response:
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
    # Skip SVG images as they are vector format and Pillow cannot process them as raster preview thumbnails
    try:
        parsed_url = urllib.parse.urlparse(image_url)
        if parsed_url.path.lower().endswith(".svg"):
            logger.info(f"Skipping SVG image as preview: {image_url}")
            return None
    except Exception:
        pass

    # Try standard User-Agent first
    response_data = None
    content_type = ""
    try:
        req = urllib.request.Request(
            image_url, 
            headers={'User-Agent': STANDARD_USER_AGENT}
        )
        with _urlopen(req, timeout=5) as response:
            content_type = response.headers.get('Content-Type', '')
            if 'svg' in content_type.lower():
                logger.info(f"Skipping SVG content-type as preview: {image_url}")
                return None
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
            with _urlopen(req, timeout=5) as response:
                content_type = response.headers.get('Content-Type', '')
                if 'svg' in content_type.lower():
                    logger.info(f"Skipping SVG content-type as preview: {image_url}")
                    return None
                if 'image' in content_type.lower():
                    response_data = response.read()
        except Exception as e:
            logger.warning(f"Fallback fetch failed for image {image_url}: {e}")

    if not response_data:
        return None

    # Try to compress and resize the image using Pillow (max 800px on the longer side, WebP format)
    try:
        import io
        from PIL import Image
        
        img = Image.open(io.BytesIO(response_data))
        width, height = img.size
        
        cached_filename = f"og_{urlhash}.webp"
        cached_path = os.path.join(CACHE_DIR, cached_filename)
        new_w, new_h, is_anim = _save_image_as_webp(img, cached_path, max_dim=800, quality=80)
        
        orig_size_str = _format_size(len(response_data))
        comp_size_str = _format_size(os.path.getsize(cached_path))
        logger.info(f"Compressed OG image to WebP (animated={is_anim}): {width}x{height} -> {new_w}x{new_h} ({orig_size_str} -> {comp_size_str})")
        return cached_path
    except Exception as pillow_err:
        logger.warning(f"Pillow image compression failed: {pillow_err}. Falling back to original bytes.")
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
            logger.warning(f"Failed to write fallback original image to {cached_path}: {e}")
            return None
            
    return None

def _format_preview_buttons(bot, accid, from_id, urlhash: str) -> str:
    """Format action buttons shown below preview cards."""
    buttons = f"⚡\u00a0/tldr_{urlhash}   🖥️\u00a0/preview_{urlhash}   📦\u00a0/webxdc_{urlhash}"
    if _is_dc_admin(bot, accid, from_id):
        buttons += f"   🏛️\u00a0/keep_{urlhash}"
    return buttons


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
                if cached_title == "__INVIDIOUS__":
                    if _is_yt_bot_in_chat(bot, accid, chat_id):
                        video_id = cached_image_path
                        yt_link = f"https://youtu.be/{video_id}"
                        logger.info(f"Invidious cache hit. Forwarding to YT Bot: {yt_link}")
                        _send(bot, accid, chat_id, yt_link)
                        return
                elif cached_title == "__FAILED_BLOCK__":
                    logger.info(f"Suppressing group preview due to cached hard failure block for {url}")
                    return
                
                # If cached title starts with "📝 ", it's a file preview
                if cached_title and (cached_title.startswith("📝 ") or cached_title.startswith("📝")):
                    title_text = cached_title.lstrip("📝").strip()
                    caption = (
                        f"📝 [{title_text}]({url})\n\n"
                        f"💾 /download_{urlhash}"
                    )
                    _send(bot, accid, chat_id, caption)
                    return
                  # Verify that if there is a cached image path, the file still exists on disk
                if not cached_image_path or os.path.exists(cached_image_path):
                    logger.info(f"OG Cache hit for group preview: {url}")
                    cached_warning = cached.get("warning")
                    cached_jina_markdown = cached.get("jina_markdown")
                    
                    emoji_prefix = "🌐" if (_is_telegram_url(url) or _is_instagram_url(url)) else ("🤖🌐" if cached_jina_markdown else "🌐")
                    buttons = _format_preview_buttons(bot, accid, from_id, urlhash)
                    if cached_warning:
                        caption = f"{emoji_prefix} [{cached_title}]({url})\n\nWarning: {cached_warning}\n\n{buttons}"
                    else:
                        caption = f"{emoji_prefix} [{cached_title}]({url})\n\n{buttons}"

                    if cached_image_path:
                        _send(bot, accid, chat_id, caption, file=cached_image_path)
                    else:
                        _send(bot, accid, chat_id, caption)
                    return
        
        # 4. Cache Miss - Fetch OG tags or file info
        logger.info(f"OG Cache miss for group preview: {url}. Fetching from network.")
        
        is_file, filename, size = _detect_and_get_file_info(url)
        if is_file:
            if size > 0:
                title = f"{filename} ({_format_size(size)})"
            else:
                title = f"{filename}"
                
            # Cache file preview in OG cache
            database.add_cached_og(urlhash, f"📝 {title}", None)
            
            caption = (
                f"📝 [{title}]({url})\n\n"
                f"💾 /download_{urlhash}"
            )
            _send(bot, accid, chat_id, caption)
            return
 
        title, image_url, is_invidious, warning, jina_markdown = _get_og_preview_data(url)
        
        if is_invidious:
            video_id = _extract_youtube_id_from_invidious(url)
            if video_id:
                # Cache as invidious
                database.add_cached_og(urlhash, "__INVIDIOUS__", video_id)
                if _is_yt_bot_in_chat(bot, accid, chat_id):
                    yt_link = f"https://youtu.be/{video_id}"
                    logger.info(f"Invidious URL detected. Forwarding to YT Bot: {yt_link}")
                    _send(bot, accid, chat_id, yt_link)
                    return
        
        if title == "__FAILED_BLOCK__":
            # Cache failure for 1 hour to prevent redundant requests
            database.add_cached_og(urlhash, "__FAILED_BLOCK__", image_url)
            logger.warning(f"Suppressing group preview for {url} due to hard failure block: {image_url}")
            return
        
        # 5. Format caption and compile readability HTML preview if Jina is used
        if jina_markdown:
            _save_jina_preview_to_cache(url, urlhash, title, jina_markdown)
            
        emoji_prefix = "🌐" if (_is_telegram_url(url) or _is_instagram_url(url)) else ("🤖🌐" if jina_markdown else "🌐")
        buttons = _format_preview_buttons(bot, accid, from_id, urlhash)
        if warning:
            caption = f"{emoji_prefix} [{title}]({url})\n\nWarning: {warning}\n\n{buttons}"
        else:
            caption = f"{emoji_prefix} [{title}]({url})\n\n{buttons}"
        
        # 6. Download image if exists, saving to persistent cache folder
        img_cache_path = None
        if image_url:
            img_cache_path = _download_cached_image(image_url, urlhash)
            
        # 7. Add to SQLite cache
        database.add_cached_og(urlhash, title, img_cache_path, warning, jina_markdown)
        
        # 8. Send to group
        if img_cache_path and os.path.exists(img_cache_path):
            _send(bot, accid, chat_id, caption, file=img_cache_path)
        else:
            _send(bot, accid, chat_id, caption)
            
    except Exception as e:
        logger.error(f"Error in _do_group_link_preview: {e}")


def _do_download(bot, accid, chat_id, req_msg_id, from_id, url: str):
    """Downloads the file and sends it to the chat."""
    if _is_internal_or_invalid_url(url):
        logger.info(f"Local or invalid URL check hit for download: {url} in chat {chat_id}")
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id, f"❌ Failed to process URL.\nReason: Local, internal, or invalid host/IP address.")
        return

    import hashlib
    url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
    cache_key = f"{url_hash}_download"
    urlhash = database.get_or_create_url_hash(url)
    
    # 0. Check cache
    cached = database.get_cached_preview(cache_key)
    if cached:
        created_at = cached.get("created_at", 0)
        filepath = cached.get("filepath", "")
        title = cached.get("title", "")
        filesize = cached.get("filesize", 0)
        
        if time.time() - created_at < CACHE_MAX_AGE and os.path.exists(filepath):
            logger.info(f"Cache hit for URL download: {url}. Sending cached file: {filepath}")
            _react(bot, accid, req_msg_id, "⏳")
            
            caption = f"📝 {title}\n\n🔗 {url}"
            _send(bot, accid, chat_id, caption, file=filepath)
            _react(bot, accid, req_msg_id, "☑️")
            database.add_preview_log(chat_id, from_id, url, title, filesize, False)
            return
            
    # 1. React with loading icon
    _react(bot, accid, req_msg_id, "⏳")
    
    # 2. Get file info to know the filename
    cached_og = database.get_cached_og(urlhash)
    is_fresh_og = False
    if cached_og:
        created_at = cached_og.get("created_at", 0)
        if time.time() - created_at < CACHE_MAX_AGE:
            is_fresh_og = True

    filename = ""
    if is_fresh_og:
        title = cached_og.get("title", "")
        if title and (title.startswith("📝 ") or title.startswith("📝")):
            # Extract filename from title
            raw_title = title.lstrip("📝").strip()
            filename = raw_title
            if " (" in raw_title and raw_title.endswith(")"):
                filename = raw_title.rsplit(" (", 1)[0]
    
    if not filename:
        is_file, filename, size = _detect_and_get_file_info(url)

    if not filename:
        # Fallback to URL path
        parsed = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(parsed.path)
        filename = os.path.basename(path)
        if not filename:
            filename = "downloaded_file"
        
    tmpdir = tempfile.mkdtemp(prefix="webdownload_")
    temp_filepath = os.path.join(tmpdir, _clean_filename(filename))
    
    success, err = _download_file(url, temp_filepath)
    if not success:
        logger.error(f"Download failed for URL {url}: {err}")
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id, f"❌ Failed to download file.\nReason: {err}")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return
        
    try:
        filesize = os.path.getsize(temp_filepath)
        safe_fname = f"dl_{urlhash}_{_clean_filename(filename)}"
        cache_path = os.path.join(CACHE_DIR, safe_fname)
        
        # Move to persistent cache
        shutil.move(temp_filepath, cache_path)
        
        # Cache preview in database
        database.add_cached_preview(cache_key, cache_path, filename, filesize)
        
        # Format caption
        caption = f"📝 {filename}\n\n🔗 {url}"
        
        # Send attachment
        _send(bot, accid, chat_id, caption, file=cache_path)
        _react(bot, accid, req_msg_id, "☑️")
        
        # Store log stats
        database.add_preview_log(chat_id, from_id, url, filename, filesize, False)
        
    except Exception as e:
        logger.error(f"Error packing downloaded file: {e}")
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id, f"❌ Error processing downloaded file: {e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _get_og_image_filepath(urlhash: str, cached_og: dict | None) -> str | None:
    """Returns local downloaded file path of OG preview image, downloading it if necessary."""
    if not cached_og:
        return None
    img_path = cached_og.get("image_path")
    if not img_path:
        return None
    if os.path.exists(img_path):
        return img_path
    if img_path.startswith("http://") or img_path.startswith("https://"):
        downloaded = _download_cached_image(img_path, urlhash)
        if downloaded and os.path.exists(downloaded):
            title = cached_og.get("title", "")
            warning = cached_og.get("warning")
            jina_md = cached_og.get("jina_markdown")
            database.add_cached_og(urlhash, title, downloaded, warning, jina_md)
            return downloaded
    return None

def _do_preview(bot, accid, chat_id, req_msg_id, from_id, url: str, mode: str):
    """Run preview/archive generation in background thread."""
    if _is_internal_or_invalid_url(url):
        logger.info(f"Local or invalid URL check hit for preview: {url} in chat {chat_id}")
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id, f"❌ Failed to process URL.\nReason: Local, internal, or invalid host/IP address.")
        return

    # 0. Check exclusions first!
    if database.is_excluded(url):
        logger.info(f"Exclusion hit for URL: {url} in chat {chat_id}")
        _react(bot, accid, req_msg_id, "⚠️")
        _send(bot, accid, chat_id, f"⚠️ This URL is in the exclusion list.")
        return

    # Check cache for hard block fast rejection or redirect
    urlhash = database.get_or_create_url_hash(url)
    cached_og = database.get_cached_og(urlhash)

    is_fresh_og = False
    if cached_og:
        created_at = cached_og.get("created_at", 0)
        if time.time() - created_at < CACHE_MAX_AGE:
            is_fresh_og = True

    if is_fresh_og:
        title = cached_og.get("title", "")
        if title and (title.startswith("📝 ") or title.startswith("📝")):
            logger.info(f"URL {url} is cached as a file. Redirecting from preview/archive to download.")
            _do_download(bot, accid, chat_id, req_msg_id, from_id, url)
            return

        if title == "__INVIDIOUS__":
            if _is_yt_bot_in_chat(bot, accid, chat_id):
                video_id = cached_og.get("image_path")
                yt_link = f"https://youtu.be/{video_id}"
                logger.info(f"Manual preview Invidious cache hit. Forwarding to YT Bot: {yt_link}")
                _send(bot, accid, chat_id, yt_link)
                _react(bot, accid, req_msg_id, "☑️")
                return

        if title == "__FAILED_BLOCK__":
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
    else:
        # Check if URL is a file URL (network call)
        is_file, filename, size = _detect_and_get_file_info(url)
        if is_file:
            logger.info(f"URL {url} is detected as a file. Redirecting from preview/archive to download.")
            _do_download(bot, accid, chat_id, req_msg_id, from_id, url)
            return

        # If not in cache, do a fast pre-check to populate the cache and avoid network overhead on blocked sites!
        logger.info(f"Pre-checking URL status for {url}...")
        og_title, og_image, is_invidious, warning, jina_markdown = _get_og_preview_data(url)
        if is_invidious:
            video_id = _extract_youtube_id_from_invidious(url)
            if video_id:
                database.add_cached_og(urlhash, "__INVIDIOUS__", video_id)
                if _is_yt_bot_in_chat(bot, accid, chat_id):
                    yt_link = f"https://youtu.be/{video_id}"
                    logger.info(f"Manual preview Invidious URL pre-check hit. Forwarding to YT Bot: {yt_link}")
                    _send(bot, accid, chat_id, yt_link)
                    _react(bot, accid, req_msg_id, "☑️")
                    return
                cached_og = {"title": og_title, "image_path": og_image, "warning": warning, "jina_markdown": jina_markdown}
        elif og_title == "__FAILED_BLOCK__":
            database.add_cached_og(urlhash, "__FAILED_BLOCK__", og_image)
            cached_og = {"title": "__FAILED_BLOCK__", "image_path": og_image, "warning": None, "jina_markdown": None}
        else:
            img_cache_path = _download_cached_image(og_image, urlhash) if og_image else None
            database.add_cached_og(urlhash, og_title, img_cache_path, warning, jina_markdown)
            cached_og = {"title": og_title, "image_path": img_cache_path, "warning": warning, "jina_markdown": jina_markdown}

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
            
            jina_md = cached_og.get("jina_markdown") if cached_og else None
            caption = _format_preview_caption(title, url, mode, chat_id, jina_markdown=jina_md)
            _send(bot, accid, chat_id, caption, file=filepath)
            _react(bot, accid, req_msg_id, "☑️")
            database.add_preview_log(chat_id, from_id, url, title, filesize, 1 if mode == "archive" else 0)
            return

    # 0.5 Pre-check URL Content-Length and Content-Type to avoid downloading heavy/binary payloads
    # Only check if we don't have a fresh cached OG indicating it's a valid webpage
    if not is_fresh_og:
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

    if mode in ("readability", "webxdc"):
        # Check if we already have the Jina markdown cached to avoid hitting network/Jina again
        cached_jina_markdown = cached_og.get("jina_markdown") if cached_og else None
        compiled_jina_success = False
        if cached_jina_markdown:
            try:
                from bs4 import BeautifulSoup
                import base64
                import datetime
                
                cleaned_md = _clean_jina_markdown(cached_jina_markdown, cached_og.get("title"))
                summary = markdown_to_html(cleaned_md)
                soup = BeautifulSoup(summary, BS_PARSER)
                
                _inline_soup_images(soup, url)
                _process_soup_links(soup)

                downloaded_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M GMT")
                final_html = READABILITY_HTML_TEMPLATE.format(
                    title=cached_og.get("title") or "Webpage Preview",
                    url=url,
                    domain=domain,
                    content=str(soup),
                    downloaded_at=downloaded_at
                )
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(final_html)
                compiled_jina_success = True
                res = cached_og.get("title") or "Webpage Preview"
                logger.info(f"Successfully compiled readability preview from cached Jina markdown for {url}")
            except Exception as jina_comp_err:
                logger.warning(f"Failed to compile cached Jina markdown for {url}: {jina_comp_err}")
                
        if compiled_jina_success:
            success = True
        else:
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
        cmd = ["monolith", "-e", "-t", "60"]
        # In archive mode, JS is enabled (no -j).
        cmd.extend(["-u", STANDARD_USER_AGENT])
        cmd.extend([url, "-o", output_path])

        start_time = time.time()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            code, err = loop.run_until_complete(_run_monolith_process(cmd, url))
        finally:
            loop.close()
        
        # Self-healing retry for Anubis block
        if code == 0 and os.path.exists(output_path) and _is_anubis_blocked(output_path):
            logger.warning(f"Anubis challenge detected for {url}. Retrying with non-Mozilla user agent...")
            try:
                os.remove(output_path)
            except Exception as remove_err:
                logger.warning(f"Error removing Anubis-blocked output: {remove_err}")
                
            cmd = ["monolith", "-e", "-t", "60"]
            cmd.extend(["-u", NON_MOZILLA_USER_AGENT])
            cmd.extend([url, "-o", output_path])
            
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                code, err = loop.run_until_complete(_run_monolith_process(cmd, url))
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
        # 4. Extract clean filename and package
        if mode == "webxdc":
            safe_fname = _safe_filename(domain, False).replace(".html", ".xdc")
            if not safe_fname.endswith(".xdc"):
                safe_fname += ".xdc"
            cache_path = os.path.join(CACHE_DIR, safe_fname)
            og_img_path = _get_og_image_filepath(urlhash, cached_og)
            if not _package_webxdc(output_path, cache_path, title, url, og_image_path=og_img_path):
                raise RuntimeError("WebXDC packaging failed")
        else:
            safe_fname = _safe_filename(domain, mode == "archive")
            cache_path = os.path.join(CACHE_DIR, safe_fname)
            shutil.move(output_path, cache_path)
            
        filesize = os.path.getsize(cache_path)
        
        # Cache preview in database
        database.add_cached_preview(cache_key, cache_path, title, filesize)
        
        # 6. Format caption
        jina_md = cached_og.get("jina_markdown") if cached_og else None
        caption = _format_preview_caption(title, url, mode, chat_id, jina_markdown=jina_md)
        
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
    """Processes `/preview`, `/previewjs`, `/archive`, and `/webxdc` triggers."""
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
            url = _clean_url_params(_strip_url_trailing_junk(url_match.group(1)))
            
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
                    url = _clean_url_params(_strip_url_trailing_junk(url_match.group(1)))

    # If no URL resolved
    if not url:
        _send(bot, accid, msg.chat_id, 
              "Usage:\n"
              "• `/preview <url>` — Save page (compressed reader-mode, recommended)\n"
              "• `/webxdc <url>` — Save page as a WebXDC app 📱\n"
              "• `/archive <url>` — Save page (full monolith with JS enabled)\n"
              "• Reply `/preview`, `/webxdc` or `/archive` to another message containing a link.")
        return

    # Spawn thread to run in background
    t = threading.Thread(
        target=_do_preview, 
        args=(bot, accid, msg.chat_id, msg.id, msg.from_id, url, mode), 
        daemon=True
    )
    t.start()


def _save_to_karakeep(url: str) -> tuple[bool, str]:
    """Save a URL to KaraKeep via API. Returns (success, bookmark_id_or_error)."""
    endpoint = f"{KARAKEEP_URL}/api/v1/bookmarks"
    payload = {"type": "link", "url": url}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Authorization": f"Bearer {KARAKEEP_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with _urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            bookmark_id = body.get("id", "")
            if not bookmark_id:
                return False, "No ID returned in response"
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        logger.warning(f"KaraKeep API error {e.code}: {error_body}")
        return False, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        logger.warning(f"KaraKeep API request failed: {e}")
        return False, str(e)

    # Attach tags if configured
    if KARAKEEP_TAGS:
        tag_endpoint = f"{KARAKEEP_URL}/api/v1/bookmarks/{bookmark_id}/tags"
        tag_payload = {"tags": [{"tagName": tag} for tag in KARAKEEP_TAGS]}
        tag_data = json.dumps(tag_payload).encode("utf-8")
        tag_req = urllib.request.Request(
            tag_endpoint,
            data=tag_data,
            headers={
                "Authorization": f"Bearer {KARAKEEP_API_KEY}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with _urlopen(tag_req, timeout=10) as tag_resp:
                pass
        except Exception as e:
            logger.warning(f"Failed to attach tags to KaraKeep bookmark {bookmark_id}: {e}")
            # We still return True because the bookmark itself was saved successfully
            return True, bookmark_id

    return True, bookmark_id


def _check_wayback_availability(url: str, auth_header: str | None = None, max_age_seconds: int = 3600) -> str | None:
    """
    Check if Wayback Machine has a RECENT snapshot of the URL.
    Only snapshots created within `max_age_seconds` (default: 1 hour) are accepted,
    preventing stale historical snapshots (e.g. from years ago) from masquerading
    as successful fresh saves.
    """
    try:
        api_url = f"https://archive.org/wayback/available?url={urllib.parse.quote(url, safe='')}"
        headers = {
            "User-Agent": STANDARD_USER_AGENT,
            "Accept": "application/json",
        }
        if auth_header:
            headers["Authorization"] = auth_header
        req = urllib.request.Request(api_url, headers=headers)
        with _urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            snapshots = data.get("archived_snapshots", {})
            closest = snapshots.get("closest", {})
            if closest.get("available") and closest.get("url"):
                snapshot_url = closest["url"]
                ts_str = str(closest.get("timestamp", "")).strip()
                if not ts_str:
                    m = re.search(r"/web/(\d{14})", snapshot_url)
                    if m:
                        ts_str = m.group(1)

                if ts_str:
                    try:
                        snapshot_dt = datetime.strptime(ts_str[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                        now_dt = datetime.now(timezone.utc)
                        age_seconds = (now_dt - snapshot_dt).total_seconds()
                        if -300 <= age_seconds <= max_age_seconds:
                            logger.info(f"Found recent Wayback snapshot ({age_seconds:.0f}s old): {snapshot_url}")
                            return snapshot_url
                        else:
                            logger.warning(
                                f"Wayback snapshot found for {url} but is too old ({age_seconds / 86400:.1f} days old, ts={ts_str}): {snapshot_url}"
                            )
                            return None
                    except Exception as parse_err:
                        logger.warning(f"Failed to parse Wayback timestamp '{ts_str}': {parse_err}")
                        return None
                else:
                    logger.warning(f"Wayback snapshot found for {url} without valid timestamp: {snapshot_url}")
                    return None
    except Exception as e:
        logger.debug(f"Wayback availability check failed for {url}: {e}")
    return None


def _save_to_web_archive(url: str) -> tuple[bool, str]:
    """
    Save a URL to Web Archive (Wayback Machine).
    Supports authenticated SPN2 API (with WAYBACK_ACCESS_KEY / WAYBACK_SECRET_KEY),
    availability fallback (for recent snapshots only), and anonymous /save/ endpoint retry.
    Returns (success, archived_url_or_error).
    """
    if WAYBACK_ACCESS_KEY and WAYBACK_SECRET_KEY:
        logger.info(f"Saving URL to Web Archive via SPN2 API: {url}")
        post_data = urllib.parse.urlencode({
            "url": url,
            "capture_all": "1",
            "capture_outlinks": "0",
            "capture_screenshot": "0",
        }).encode("utf-8")
        auth_header = f"LOW {WAYBACK_ACCESS_KEY}:{WAYBACK_SECRET_KEY}"
        req = urllib.request.Request(
            "https://web.archive.org/save/",
            data=post_data,
            headers={
                "User-Agent": STANDARD_USER_AGENT,
                "Authorization": auth_header,
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with _urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                job_id = data.get("job_id")
                if not job_id:
                    if "url" in data:
                        return True, data["url"]
                    avail = _check_wayback_availability(url, auth_header)
                    if avail:
                        return True, avail
                    return False, f"SPN2 error: {data}"

            # Poll status for job_id
            status_url = f"https://web.archive.org/save/status/{job_id}"
            start_time = time.time()
            while time.time() - start_time < 120:
                time.sleep(3)
                status_req = urllib.request.Request(
                    status_url,
                    headers={
                        "User-Agent": STANDARD_USER_AGENT,
                        "Authorization": auth_header,
                        "Accept": "application/json",
                    },
                )
                try:
                    with _urlopen(status_req, timeout=15) as s_resp:
                        s_data = json.loads(s_resp.read().decode("utf-8"))
                        status = s_data.get("status")
                        if status == "success":
                            timestamp = s_data.get("timestamp")
                            orig_url = s_data.get("original_url", url)
                            if timestamp:
                                snapshot_url = f"https://web.archive.org/web/{timestamp}/{orig_url}"
                                logger.info(f"SPN2 save succeeded: {snapshot_url}")
                                return True, snapshot_url
                            return True, f"https://web.archive.org/web/*/{url}"
                        elif status == "error":
                            err = s_data.get("message") or s_data.get("status_ext") or "Unknown SPN2 error"
                            logger.warning(f"SPN2 job {job_id} failed: {err}")
                            avail = _check_wayback_availability(url, auth_header)
                            if avail:
                                return True, avail
                            return False, f"SPN2 error: {err}"
                except Exception as e:
                    logger.warning(f"SPN2 status check error: {e}")

            avail = _check_wayback_availability(url, auth_header)
            if avail:
                return True, avail
            return False, "SPN2 timed out waiting for snapshot"
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            logger.warning(f"SPN2 HTTP error {e.code}: {e.reason}. Detail: {error_body}")

            # If 503/429 (e.g. rate limit, already captured), check if recent snapshot exists
            avail = _check_wayback_availability(url, auth_header)
            if avail:
                logger.info(f"Found recent Wayback snapshot after SPN2 {e.code}: {avail}")
                return True, avail

            # Try standard save_url before giving up
            logger.info(f"SPN2 returned {e.code}. Attempting standard /save/ endpoint for {url}...")
        except Exception as e:
            logger.error(f"SPN2 request failed: {e}")

    # Fallback to anonymous save endpoint
    save_url = f"https://web.archive.org/save/{url}"
    logger.info(f"Saving URL to Web Archive: {save_url}")

    try:
        req = urllib.request.Request(
            save_url,
            headers={
                'User-Agent': STANDARD_USER_AGENT
            }
        )

        try:
            with _urlopen(req, timeout=60) as response:
                logger.info(f"Web Archive save succeeded with User-Agent: {STANDARD_USER_AGENT}")
                redirected_url = response.geturl()

                # The /save/ endpoint returns a redirect to the snapshot URL
                if "/web/" in redirected_url or "archive.org" in redirected_url:
                    if "/web/" in redirected_url:
                        m = re.search(r"/web/(\d{14})", redirected_url)
                        if m:
                            ts_str = m.group(1)
                            try:
                                snapshot_dt = datetime.strptime(ts_str, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                                now_dt = datetime.now(timezone.utc)
                                age_seconds = (now_dt - snapshot_dt).total_seconds()
                                if -300 <= age_seconds <= 3600:
                                    return True, redirected_url
                                else:
                                    logger.warning(f"Anonymous save redirected to old snapshot: {redirected_url}")
                            except Exception:
                                pass
                        else:
                            return True, redirected_url

                avail = _check_wayback_availability(url)
                if avail:
                    return True, avail

                return False, "No fresh snapshot generated by Web Archive"
        except urllib.error.HTTPError as e:
            logger.warning(f"Web Archive HTTP error {e.code}: {e.reason}.")
            avail = _check_wayback_availability(url)
            if avail:
                return True, avail
            return False, f"HTTP {e.code}: {e.reason}"
        except urllib.error.URLError as e:
            logger.warning(f"Web Archive save URLError: {e}")
            avail = _check_wayback_availability(url)
            if avail:
                return True, avail
            return False, str(e)
    except Exception as e:
        logger.error(f"Web Archive save failed: {e}")
        return False, str(e)


def _save_to_archive_today(url: str) -> tuple[bool, str]:
    """
    Save a URL to archive.today (or its active mirror).
    Tries configured/default mirrors sequentially until one succeeds.
    Returns (success, archived_url_or_error).
    """
    errors = []
    mirrors_to_try = ARCHIVE_TODAY_MIRRORS if ARCHIVE_TODAY_MIRRORS else ARCHIVE_TODAY_DEFAULT_MIRRORS
    for mirror in mirrors_to_try:
        mirror_clean = mirror.rstrip("/")
        logger.info(f"Attempting to archive {url} via Archive.today mirror: {mirror_clean}")
        try:
            # 1. Fetch homepage to verify mirror availability and extract submitid
            submitid = None
            try:
                get_req = urllib.request.Request(
                    f"{mirror_clean}/",
                    headers={
                        "User-Agent": STANDARD_USER_AGENT,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    }
                )
                with _urlopen(get_req, timeout=10) as resp:
                    html = resp.read().decode("utf-8", errors="replace")
                    match = re.search(r'name=["\']submitid["\']\s+value=["\']([^"\']+)["\']', html) or \
                            re.search(r'value=["\']([^"\']+)["\']\s+name=["\']submitid["\']', html)
                    if match:
                        submitid = match.group(1)
            except urllib.error.HTTPError as e:
                err_msg = f"{mirror_clean} (GET): HTTP {e.code} {e.reason}"
                logger.warning(f"Archive.today mirror unavailable: {err_msg}")
                errors.append(err_msg)
                continue
            except Exception as e:
                err_msg = f"{mirror_clean} (GET): {str(e)}"
                logger.warning(f"Archive.today mirror check failed: {err_msg}")
                errors.append(err_msg)
                continue

            # 2. Submit URL via POST
            submit_url = f"{mirror_clean}/submit/"
            form_fields = {"url": url, "anyway": "1"}
            if submitid:
                form_fields["submitid"] = submitid

            data = urllib.parse.urlencode(form_fields).encode("utf-8")
            post_req = urllib.request.Request(
                submit_url,
                data=data,
                headers={
                    "User-Agent": STANDARD_USER_AGENT,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": f"{mirror_clean}/",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                method="POST",
            )

            with _urlopen(post_req, timeout=45) as resp:
                final_url = resp.geturl()

                # Check if final redirected URL is a valid snapshot or wip page
                if mirror_clean in final_url and final_url.rstrip("/") != submit_url.rstrip("/") and final_url.rstrip("/") != mirror_clean:
                    logger.info(f"Archive.today successfully saved {url} -> {final_url} via {mirror_clean}")
                    return True, final_url

                # Check response body for location or refresh or wip links
                body = resp.read().decode("utf-8", errors="replace")

                # Look for meta refresh
                meta_match = re.search(r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^"\']*url=([^"\'>\s]+)', body, re.IGNORECASE)
                if meta_match:
                    target = meta_match.group(1).strip()
                    target_url = urllib.parse.urljoin(mirror_clean + "/", target)
                    return True, target_url

                # Look for JS redirection
                js_match = re.search(r'(?:location\.replace\s*\(\s*|location(?:\.href)?\s*=\s*)["\']([^"\']+)["\']', body, re.IGNORECASE)
                if js_match:
                    target = js_match.group(1).strip()
                    target_url = urllib.parse.urljoin(mirror_clean + "/", target)
                    return True, target_url

                # Look for wip link
                wip_match = re.search(r'href=["\']((?:https?://[^"\'>\s]+|/)?wip/[a-zA-Z0-9]+)["\']', body, re.IGNORECASE)
                if wip_match:
                    target = wip_match.group(1).strip()
                    target_url = urllib.parse.urljoin(mirror_clean + "/", target)
                    return True, target_url

                # Look for direct snapshot link
                link_match = re.search(r'href=["\']((?:https?://[^"\'>\s]+|/)[a-zA-Z0-9]{5,10})["\']', body, re.IGNORECASE)
                if link_match:
                    target = link_match.group(1).strip()
                    target_url = urllib.parse.urljoin(mirror_clean + "/", target)
                    return True, target_url

                # Fallback snapshot lookup link on this mirror
                fallback_url = f"{mirror_clean}/{url}"
                return True, fallback_url

        except urllib.error.HTTPError as e:
            # If server returned 301/302/303/307/308 redirect, check Location header
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                loc = e.headers.get("Location")
                target_url = urllib.parse.urljoin(mirror_clean + "/", loc)
                return True, target_url
            err_msg = f"{mirror_clean}: HTTP {e.code} {e.reason}"
            logger.warning(f"Archive.today mirror error: {err_msg}")
            errors.append(err_msg)
        except Exception as e:
            err_msg = f"{mirror_clean}: {str(e)}"
            logger.warning(f"Archive.today mirror request failed: {err_msg}")
            errors.append(err_msg)

    return False, "; ".join(errors) if errors else "No accessible mirrors"


def _save_to_ghostarchive(url: str) -> tuple[bool, str]:
    """
    Save a URL to Ghostarchive (ghostarchive.org).
    Returns (success, archived_url_or_error).
    """
    submit_url = "https://ghostarchive.org/archive"
    logger.info(f"Saving URL to Ghostarchive: {url}")
    data = urllib.parse.urlencode({"url": url}).encode("utf-8")
    req = urllib.request.Request(
        submit_url,
        data=data,
        headers={
            "User-Agent": STANDARD_USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://ghostarchive.org/",
        },
        method="POST",
    )
    try:
        with _urlopen(req, timeout=45) as resp:
            final_url = resp.geturl()
            if "ghostarchive.org" in final_url and final_url.rstrip("/") != submit_url.rstrip("/"):
                logger.info(f"Ghostarchive save succeeded: {final_url}")
                return True, final_url

            body = resp.read().decode("utf-8", errors="replace")

            # Look for meta refresh
            meta_match = re.search(r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^"\']*url=([^"\'>\s]+)', body, re.IGNORECASE)
            if meta_match:
                target = meta_match.group(1).strip()
                return True, urllib.parse.urljoin("https://ghostarchive.org/", target)

            # Look for JS redirect
            js_match = re.search(r'(?:location\.replace\s*\(\s*|location(?:\.href)?\s*=\s*)["\']([^"\']+)["\']', body, re.IGNORECASE)
            if js_match:
                target = js_match.group(1).strip()
                return True, urllib.parse.urljoin("https://ghostarchive.org/", target)

            # Look for archive link in HTML (e.g. /archive/xyz123 or /varchive/xyz123)
            link_match = re.search(r'href=["\']((?:https?://ghostarchive\.org)?/(?:v?archive)/[a-zA-Z0-9_-]{4,30})["\']', body, re.IGNORECASE)
            if link_match:
                target = link_match.group(1).strip()
                target_url = urllib.parse.urljoin("https://ghostarchive.org/", target)
                return True, target_url

            logger.warning(f"Ghostarchive response received but no snapshot URL found for {url}")
            return False, "No snapshot URL in Ghostarchive response"
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
            loc = e.headers.get("Location")
            if "/archive/" in loc or "/varchive/" in loc:
                target_url = urllib.parse.urljoin("https://ghostarchive.org/", loc)
                return True, target_url
        logger.warning(f"Ghostarchive HTTP error {e.code}: {e.reason}")
        return False, f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        logger.warning(f"Ghostarchive URLError: {e}")
        return False, str(e)
    except Exception as e:
        logger.error(f"Ghostarchive save failed: {e}")
        return False, str(e)


def _do_keep(bot, accid, chat_id, msg_id, from_id, url: str):
    """
    Background worker:
    1. First, save URL to KaraKeep (if configured and requester is admin).
    2. Try saving to Web Archive (Wayback Machine).
    3. If Web Archive fails or is unavailable, fall back to Archive.today and Ghostarchive.
    """
    import concurrent.futures

    # 1. If requester is admin and KaraKeep is enabled, save to KaraKeep first and send confirmation to their private chat
    is_admin = _is_dc_admin(bot, accid, from_id)
    if is_admin and _karakeep_enabled():
        kk_success, kk_result = _save_to_karakeep(url)
        try:
            private_chat_id = bot.rpc.create_chat_by_contact_id(accid, from_id)
            if kk_success:
                bookmark_url = f"{KARAKEEP_URL}/dashboard/preview/{kk_result}" if kk_result else ""
                private_reply = f"🔖 Saved to KaraKeep!\n🔗 {url}"
                if bookmark_url:
                    private_reply += f"\n📎 {bookmark_url}"
                _send(bot, accid, private_chat_id, private_reply)
            else:
                _send(bot, accid, private_chat_id, f"❌ Failed to save to KaraKeep.\nReason: {kk_result}")
        except Exception as e:
            logger.error(f"Failed to send KaraKeep notification to private chat for admin {from_id}: {e}")

    # 2. Try saving to Web Archive first
    wa_success, wa_result = _save_to_web_archive(url)
    if wa_success:
        _react(bot, accid, msg_id, "☑️")
        reply = f"🏛️ Saved to Web Archive!\n🔗 {url}"
        if wa_result:
            reply += f"\n📎 {wa_result}"
        _send(bot, accid, chat_id, reply)
        return

    # 3. Fallback: Web Archive failed or is down, try Archive.today and Ghostarchive concurrently
    logger.warning(f"Web Archive save failed ({wa_result}). Falling back to Archive.today and Ghostarchive for URL: {url}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        future_at = executor.submit(_save_to_archive_today, url)
        future_ga = executor.submit(_save_to_ghostarchive, url)

        done, not_done = concurrent.futures.wait([future_at, future_ga], timeout=15)

        at_success = False
        at_result = ""
        ga_success = False
        ga_result = ""

        if future_at in done and future_ga in done:
            at_success, at_result = future_at.result()
            ga_success, ga_result = future_ga.result()

            if at_success and ga_success:
                _react(bot, accid, msg_id, "☑️")
                reply = (
                    f"🏛️ Saved to Web Archives!\n"
                    f"🔗 {url}\n\n"
                    f"• Archive.today: {at_result}\n"
                    f"• Ghostarchive: {ga_result}"
                )
                _send(bot, accid, chat_id, reply)
                return
            elif at_success:
                _react(bot, accid, msg_id, "☑️")
                _send(bot, accid, chat_id, f"🏛️ Saved to Archive.today!\n🔗 {url}\n📎 {at_result}")
                return
            elif ga_success:
                _react(bot, accid, msg_id, "☑️")
                _send(bot, accid, chat_id, f"🏛️ Saved to Ghostarchive!\n🔗 {url}\n📎 {ga_result}")
                return
            else:
                _react(bot, accid, msg_id, "❌")
                _send(bot, accid, chat_id,
                      f"❌ Failed to archive URL.\n"
                      f"• Web Archive: {wa_result}\n"
                      f"• Archive.today: {at_result}\n"
                      f"• Ghostarchive: {ga_result}")
                return

        # If one completed in 15s and succeeded, send it right away
        sent = False
        if future_at in done:
            at_success, at_result = future_at.result()
            if at_success:
                _react(bot, accid, msg_id, "☑️")
                _send(bot, accid, chat_id, f"🏛️ Saved to Archive.today!\n🔗 {url}\n📎 {at_result}")
                sent = True

        if future_ga in done:
            ga_success, ga_result = future_ga.result()
            if ga_success:
                _react(bot, accid, msg_id, "☑️")
                _send(bot, accid, chat_id, f"🏛️ Saved to Ghostarchive!\n🔗 {url}\n📎 {ga_result}")
                sent = True

        # Wait for remaining tasks to complete
        if future_at in not_done:
            try:
                at_success, at_result = future_at.result()
                if at_success and not sent:
                    _react(bot, accid, msg_id, "☑️")
                    _send(bot, accid, chat_id, f"🏛️ Saved to Archive.today!\n🔗 {url}\n📎 {at_result}")
                    sent = True
            except Exception as e:
                at_result = str(e)

        if future_ga in not_done:
            try:
                ga_success, ga_result = future_ga.result()
                if ga_success and not sent:
                    _react(bot, accid, msg_id, "☑️")
                    _send(bot, accid, chat_id, f"🏛️ Saved to Ghostarchive!\n🔗 {url}\n📎 {ga_result}")
                    sent = True
            except Exception as e:
                ga_result = str(e)

        if not sent and not at_success and not ga_success:
            _react(bot, accid, msg_id, "❌")
            _send(bot, accid, chat_id,
                  f"❌ Failed to archive URL.\n"
                  f"• Web Archive: {wa_result}\n"
                  f"• Archive.today: {at_result or 'Timed out'}\n"
                  f"• Ghostarchive: {ga_result or 'Timed out'}")


def _handle_keep_command(bot, accid, event):
    """Processes /keep command — saves URL to KaraKeep (admin with config) or Web Archive."""
    msg = event.msg

    if _is_duplicate_msg(msg.id, "keep"):
        return

    # Extract target URL (same logic as _handle_preview_command)
    url = ""
    payload = event.payload.strip() if event.payload else ""

    if payload:
        url_match = re.search(r'(https?://[^\s<>"]+)', payload)
        if url_match:
            url = _clean_url_params(_strip_url_trailing_junk(url_match.group(1)))
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
                    url = _clean_url_params(_strip_url_trailing_junk(url_match.group(1)))

    if not url:
        is_admin = _is_dc_admin(bot, accid, msg.from_id)
        target_service = "KaraKeep" if (is_admin and _karakeep_enabled()) else "Web Archive"
        _send(bot, accid, msg.chat_id,
              f"Usage:\n"
              f"• `/keep <url>` — Save URL to {target_service}\n"
              f"• Reply `/keep` to any message containing a link.")
        return

    _react(bot, accid, msg.id, "🏛️" if not (_is_dc_admin(bot, accid, msg.from_id) and _karakeep_enabled()) else "🔖")
    t = threading.Thread(
        target=_do_keep,
        args=(bot, accid, msg.chat_id, msg.id, msg.from_id, url),
        daemon=True,
    )
    t.start()


def _handle_jina_command(bot, accid, event):
    """Processes /jina command — checks Jina AI API key remaining tokens."""
    msg = event.msg
    
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /jina.")
        return

    # Extract target API key from payload
    api_key = event.payload.strip() if event.payload else ""
    if not api_key:
        api_key = JINA_API_KEY

    if not api_key:
        _send(bot, accid, msg.chat_id,
              "❌ `JINA_API_KEY` is not configured in the bot's environment.\n"
              "Please check a specific key by passing it as an argument:\n"
              "• `/jina <your_api_key>`")
        return

    _react(bot, accid, msg.id, "⏳")

    def _do_jina_check():
        try:
            import json
            import urllib.request
            
            check_url = f"https://dash.jina.ai/api/v1/api_key/fe_user?api_key={api_key}"
            req = urllib.request.Request(
                check_url,
                headers={"User-Agent": STANDARD_USER_AGENT}
            )
            with _urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
            
            wallet = data.get("wallet", {})
            total_balance = wallet.get("total_balance", 0)
            trial_balance = wallet.get("trial_balance", 0)
            regular_balance = wallet.get("regular_balance", 0)
            
            total_str = f"{total_balance:,}"
            trial_str = f"{trial_balance:,}"
            reg_str = f"{regular_balance:,}"
            
            trial_start = wallet.get("trial_start", "")
            trial_end = wallet.get("trial_end", "")
            
            if trial_start:
                trial_start = trial_start.split("T")[0]
            if trial_end:
                trial_end = trial_end.split("T")[0]
                
            reply = (
                f"🤖 **Jina AI API Key Stats:**\n\n"
                f"• **Total Balance:** {total_str} tokens\n"
                f"• **Trial Balance:** {trial_str} tokens\n"
                f"• **Regular Balance:** {reg_str} tokens\n"
            )
            if trial_start or trial_end:
                reply += f"• **Trial Period:** {trial_start} to {trial_end}\n"
                
            _react(bot, accid, msg.id, "☑️")
            _send(bot, accid, msg.chat_id, reply)
            
        except Exception as e:
            logger.error(f"Failed to check Jina API key balance: {e}")
            _react(bot, accid, msg.id, "❌")
            _send(bot, accid, msg.chat_id, f"❌ Failed to check Jina API key balance.\nReason: {e}")

    threading.Thread(target=_do_jina_check, daemon=True).start()


def _do_tldr(bot, accid, chat_id, req_msg_id, from_id, url: str):
    """Processes TL;DR summarization for a URL in background thread."""
    if not GEMINI_API_KEY:
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id,
              "❌ `GEMINI_API_KEY` is not configured.\n"
              "Please add your Google Gemini API key to `.env` to enable summarization.")
        return

    if _is_internal_or_invalid_url(url):
        logger.info(f"Local or invalid URL check hit for tldr: {url} in chat {chat_id}")
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id, f"❌ Failed to process URL.\nReason: Local, internal, or invalid host/IP address.")
        return

    if database.is_excluded(url):
        _react(bot, accid, req_msg_id, "⚠️")
        _send(bot, accid, chat_id, "❌ Summarization skipped: URL domain is excluded by configuration.")
        return

    _react(bot, accid, req_msg_id, "⏳")

    try:
        urlhash = database.get_or_create_url_hash(url)
        cached_og = database.get_cached_og(urlhash)
        
        title = None
        jina_markdown = None
        
        if cached_og:
            title = cached_og.get("title")
            jina_markdown = cached_og.get("jina_markdown")
            
        if not jina_markdown:
            og_title, _, _, _, fetched_md = _get_og_preview_data(url)
            title = title or og_title
            jina_markdown = fetched_md

        if not jina_markdown or not jina_markdown.strip():
            try:
                html_str, _ = _download_page_html(url)
                if html_str:
                    doc = Document(html_str)
                    title = title or doc.title()
                    soup = BeautifulSoup(doc.summary(), BS_PARSER)
                    jina_markdown = soup.get_text(separator="\n", strip=True)
            except Exception as fallback_err:
                logger.warning(f"Local text extraction fallback failed for {url}: {fallback_err}")
            
        if not jina_markdown or not jina_markdown.strip():
            _react(bot, accid, req_msg_id, "❌")
            _send(bot, accid, chat_id, "❌ Failed to extract readable text content from the URL for summarization.")
            return
            
        lang = database.get_chat_lang(chat_id)
        url_key = database.get_or_create_url_hash(url)
        summary = _summarize_text_with_gemini(jina_markdown, title=title, target_lang=lang, short_paragraph=False, url_key=url_key)
        
        if not summary:
            _react(bot, accid, req_msg_id, "❌")
            _send(bot, accid, chat_id, "❌ Failed to generate summary via Gemini API.")
            return
            
        clean_title = (title or "Article").replace("[", "(").replace("]", ")")
        lang_suffix = f" ({lang})" if lang != "AUTO" else ""
        reply = f"⚡ **TL;DR**{lang_suffix}:\n\n{summary}\n\n🔗 [{clean_title}]({url})"
        
        _send(bot, accid, chat_id, reply)
        _react(bot, accid, req_msg_id, "☑️")
        database.add_preview_log(chat_id, from_id, url, title or "TL;DR", len(summary), 0)
    except Exception as e:
        logger.error(f"Error in /tldr command for {url}: {e}")
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id, f"❌ Error generating summary: {e}")


def _do_tldr_text(bot, accid, chat_id, req_msg_id, from_id, text: str):
    """Processes TL;DR summarization for plain or quoted message text in background thread."""
    if not GEMINI_API_KEY:
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id,
              "❌ `GEMINI_API_KEY` is not configured.\n"
              "Please add your Google Gemini API key to `.env` to enable summarization.")
        return

    _react(bot, accid, req_msg_id, "⏳")

    try:
        lang = database.get_chat_lang(chat_id)
        text_hash = f"msg_{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"
        summary = _summarize_text_with_gemini(text, title=None, target_lang=lang, short_paragraph=False, url_key=text_hash)

        if not summary:
            _react(bot, accid, req_msg_id, "❌")
            _send(bot, accid, chat_id, "❌ Failed to generate summary via Gemini API.")
            return

        lang_suffix = f" ({lang})" if lang != "AUTO" else ""
        reply = f"⚡ **TL;DR**{lang_suffix}:\n\n{summary}"

        _send(bot, accid, chat_id, reply)
        _react(bot, accid, req_msg_id, "☑️")
        database.add_preview_log(chat_id, from_id, "text_message", "Quoted Message", len(summary), 0)
    except Exception as e:
        logger.error(f"Error in text /tldr summarization: {e}")
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id, f"❌ Error generating summary: {e}")


def _handle_tldr_command(bot, accid, event):
    """Processes /tldr command to generate AI summary of an article or message."""
    msg = event.msg
    if _is_duplicate_msg(msg.id, "tldr"):
        return
    if _is_rate_limited(bot, accid, msg.from_id):
        _react(bot, accid, msg.id, "⏱")
        return

    payload = (event.payload or "").strip()
    if not payload and getattr(msg, "text", None):
        parts = msg.text.strip().split(maxsplit=1)
        if len(parts) > 1:
            payload = parts[1].strip()

    url = _extract_url_from_msg_or_payload(payload, msg)
    
    if url:
        t = threading.Thread(
            target=_do_tldr,
            args=(bot, accid, msg.chat_id, msg.id, msg.from_id, url),
            daemon=True
        )
        t.start()
        return

    # No URL found: check for quoted text or text payload to summarize directly
    quote = getattr(msg, "quote", None) or (msg.get("quote") if isinstance(msg, dict) else None)
    text_to_summarize = ""
    if quote:
        if isinstance(quote, dict):
            text_to_summarize = quote.get("text", "")
        else:
            text_to_summarize = getattr(quote, "text", "")

    if not text_to_summarize and payload:
        text_to_summarize = payload

    if text_to_summarize and text_to_summarize.strip():
        clean_text = text_to_summarize.strip()
        if len(clean_text) < 50:
            _react(bot, accid, msg.id, "❌")
            _send(bot, accid, msg.chat_id, "❌ Message is too short to summarize (minimum 50 characters).")
            return

        t = threading.Thread(
            target=_do_tldr_text,
            args=(bot, accid, msg.chat_id, msg.id, msg.from_id, clean_text),
            daemon=True
        )
        t.start()
        return

    _send(bot, accid, msg.chat_id,
          "Usage:\n"
          "• `/tldr <url>` — Generate article summary (TL;DR)\n"
          "• Reply `/tldr` to any message or link to summarize it.")

def _do_ai_query(
    bot,
    accid,
    chat_id,
    req_msg_id,
    from_id,
    prompt: str,
    context: str | None = None,
    url: str | None = None,
    title: str | None = None,
    image_bytes: bytes | None = None,
    image_mime: str = "image/jpeg",
):
    """Processes AI query in background thread."""
    if not GEMINI_API_KEY:
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id,
              "❌ `GEMINI_API_KEY` is not configured.\n"
              "Please add your Google Gemini API key to `.env` to enable AI queries.")
        return

    _react(bot, accid, req_msg_id, "⏳")

    try:
        lang = database.get_chat_lang(chat_id)
        img_hash = hashlib.sha256(image_bytes).hexdigest()[:16] if image_bytes else ""
        raw_key = f"{prompt}:{context or ''}:{url or ''}:{img_hash}"
        query_key = hashlib.sha256(raw_key.encode('utf-8')).hexdigest()[:16]

        answer = _ask_gemini_ai(
            prompt,
            context=context,
            target_lang=lang,
            query_key=query_key,
            image_bytes=image_bytes,
            image_mime=image_mime,
        )

        if not answer:
            _react(bot, accid, req_msg_id, "❌")
            _send(bot, accid, chat_id, "❌ Failed to generate AI response via Gemini API.")
            return

        lang_suffix = f" ({lang})" if lang != "AUTO" else ""
        if url:
            clean_title = (title or "Link").replace("[", "(").replace("]", ")")
            reply = f"🤖 **AI**{lang_suffix}:\n\n{answer}\n\n🔗 [{clean_title}]({url})"
        else:
            reply = f"🤖 **AI**{lang_suffix}:\n\n{answer}"

        _send(bot, accid, chat_id, reply)
        _react(bot, accid, req_msg_id, "☑️")
        log_url = url or ("ai_image" if image_bytes else "ai_query")
        log_title = title or ("AI Vision Query" if image_bytes else "AI Query")
        database.add_preview_log(chat_id, from_id, log_url, log_title, len(answer), len(image_bytes) if image_bytes else 0)
    except Exception as e:
        logger.error(f"Error in /ai query: {e}")
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id, f"❌ Error generating AI answer: {e}")


def _handle_ai_command(bot, accid, event):
    """Processes /ai command to answer a question, analyze topic, or inspect an image."""
    msg = event.msg
    if _is_duplicate_msg(msg.id, "ai"):
        return
    if _is_rate_limited(bot, accid, msg.from_id):
        _react(bot, accid, msg.id, "⏱")
        return

    payload = (event.payload or "").strip()
    if not payload and getattr(msg, "text", None):
        parts = msg.text.strip().split(maxsplit=1)
        if len(parts) > 1:
            payload = parts[1].strip()

    # Check for quoted text
    quote = getattr(msg, "quote", None) or (msg.get("quote") if isinstance(msg, dict) else None)
    quote_text = ""
    if quote:
        if isinstance(quote, dict):
            quote_text = quote.get("text", "")
        else:
            quote_text = getattr(quote, "text", "")
        quote_text = (quote_text or "").strip()

    url = _extract_url_from_msg_or_payload(payload, msg)

    if url:
        if _is_internal_or_invalid_url(url):
            logger.info(f"Local or invalid URL check hit for /ai: {url} in chat {msg.chat_id}")
            _react(bot, accid, msg.id, "❌")
            _send(bot, accid, msg.chat_id, "❌ Failed to process URL.\nReason: Local, internal, or invalid host/IP address.")
            return

        if database.is_excluded(url):
            _react(bot, accid, msg.id, "⚠️")
            _send(bot, accid, msg.chat_id, "❌ AI request skipped: URL domain is excluded by configuration.")
            return

        clean_payload_no_url = payload.replace(url, "").strip()
        if not clean_payload_no_url and not quote_text:
            prompt = "Provide a concise overview and key insights of this webpage/article in 2-3 paragraphs."
        elif clean_payload_no_url:
            prompt = clean_payload_no_url
        else:
            prompt = quote_text or "Provide a concise overview and key insights of this webpage/article in 2-3 paragraphs."

        def _do_ai_with_url():
            try:
                image_bytes, image_mime = _extract_image_from_msg_or_quote(bot, accid, msg)
                urlhash = database.get_or_create_url_hash(url)
                cached_og = database.get_cached_og(urlhash)
                title = cached_og.get("title") if cached_og else None
                jina_markdown = cached_og.get("jina_markdown") if cached_og else None

                if not jina_markdown:
                    og_title, _, _, _, fetched_md = _get_og_preview_data(url)
                    title = title or og_title
                    jina_markdown = fetched_md

                if not jina_markdown or not jina_markdown.strip():
                    try:
                        html_str, _ = _download_page_html(url)
                        if html_str:
                            doc = Document(html_str)
                            title = title or doc.title()
                            soup = BeautifulSoup(doc.summary(), BS_PARSER)
                            jina_markdown = soup.get_text(separator="\n", strip=True)
                    except Exception as fallback_err:
                        logger.warning(f"Local text extraction fallback failed for {url}: {fallback_err}")

                ctx = (jina_markdown or "").strip()
                if quote_text:
                    ctx = f"{quote_text}\n\n{ctx}" if ctx else quote_text

                _do_ai_query(
                    bot, accid, msg.chat_id, msg.id, msg.from_id,
                    prompt=prompt, context=ctx, url=url, title=title,
                    image_bytes=image_bytes, image_mime=image_mime
                )
            except Exception as err:
                logger.error(f"Error in URL handling for /ai: {err}")
                _do_ai_query(
                    bot, accid, msg.chat_id, msg.id, msg.from_id,
                    prompt=prompt, context=quote_text or None, url=url,
                    image_bytes=image_bytes, image_mime=image_mime
                )

        threading.Thread(target=_do_ai_with_url, daemon=True).start()
        return

    # No URL: run background thread for prompt / quote / image processing
    def _do_ai_async():
        image_bytes, image_mime = _extract_image_from_msg_or_quote(bot, accid, msg)

        if payload and quote_text:
            prompt = payload
            context = quote_text
        elif payload:
            prompt = payload
            context = None
        elif quote_text:
            prompt = quote_text
            context = None
        elif image_bytes:
            prompt = ""
            context = None
        else:
            _send(bot, accid, msg.chat_id,
                  "Usage:\n"
                  "• `/ai <question or topic>` — Ask AI a question or analyze a topic 🤖\n"
                  "• Send or reply `/ai` to a photo/image to analyze or describe it 🖼️\n"
                  "• Reply `/ai` to any message or link to answer or explain it.")
            return

        if not image_bytes and len(prompt.strip()) < 2:
            _send(bot, accid, msg.chat_id, "❌ Please provide a question or topic to ask AI.")
            return

        _do_ai_query(
            bot,
            accid,
            msg.chat_id,
            msg.id,
            msg.from_id,
            prompt,
            context,
            image_bytes=image_bytes,
            image_mime=image_mime,
        )

    threading.Thread(target=_do_ai_async, daemon=True).start()

def _handle_lang_command(bot, accid, event):
    """Processes /lang command to view or set preferred summary language for a chat."""
    msg = event.msg
    payload = (event.payload or "").strip()
    if not payload and getattr(msg, "text", None):
        parts = msg.text.strip().split(maxsplit=1)
        if len(parts) > 1:
            payload = parts[1].strip()
    
    if not payload:
        curr_lang = database.get_chat_lang(msg.chat_id)
        reply = (
            f"🌐 **Summary Language Setting**\n\n"
            f"Current summary language for this chat: **{curr_lang}**\n\n"
            f"To set a preferred language for article summaries, use:\n"
            f"• `/lang AUTO` (Automatic - match article language, default)\n"
            f"• `/lang RU` (Russian)\n"
            f"• `/lang EN` (English)\n"
            f"• `/lang DE` (German)\n"
            f"• `/lang ES` (Spanish)\n"
            f"• `/lang FR` (French)"
        )
        _send(bot, accid, msg.chat_id, reply)
        return
        
    lang_code = payload.split()[0].upper()
    if not re.match(r"^[A-Z]{2,5}(?:-[A-Z]{2,5})?$", lang_code):
        _send(bot, accid, msg.chat_id, "❌ Invalid language code format. Example: `/lang RU` or `/lang EN`.")
        return
        
    database.set_chat_lang(msg.chat_id, lang_code)
    _react(bot, accid, msg.id, "☑️")
    _send(bot, accid, msg.chat_id, f"✅ Preferred summary language for this chat set to **{lang_code}**.")


# ── Command Listeners ──

@dc_cli.on(events.NewMessage(pattern=r"(?i)^/ai(?:@\w+)?(?:\s|$)", is_bot=None))
def ai_command(bot, accid, event):
    if _is_bot_blocked(bot, accid, event.msg):
        return
    if accid != dc_accid:
        return
    text = (event.msg.text or "").strip()
    if not re.match(r"^/ai(?:@\w+)?(?:\s|$)", text, re.IGNORECASE):
        return
    _handle_ai_command(bot, accid, event)

@dc_cli.on(events.NewMessage(pattern=r"(?i)^/tldr(?:@\w+)?(?:\s|$)", is_bot=None))
def tldr_command(bot, accid, event):
    if _is_bot_blocked(bot, accid, event.msg):
        return
    if accid != dc_accid:
        return
    text = (event.msg.text or "").strip()
    if not re.match(r"^/tldr(?:@\w+)?(?:\s|$)", text, re.IGNORECASE):
        return
    _handle_tldr_command(bot, accid, event)

@dc_cli.on(events.NewMessage(pattern=r"(?i)^/lang(?:@\w+)?(?:\s|$)", is_bot=None))
def lang_command(bot, accid, event):
    if _is_bot_blocked(bot, accid, event.msg):
        return
    if accid != dc_accid:
        return
    text = (event.msg.text or "").strip()
    if not re.match(r"^/lang(?:@\w+)?(?:\s|$)", text, re.IGNORECASE):
        return
    _handle_lang_command(bot, accid, event)

@dc_cli.on(events.NewMessage(pattern=r"(?i)^/preview(?:@\w+)?(?:\s|$)", is_bot=None))
def preview_command(bot, accid, event):
    if _is_bot_blocked(bot, accid, event.msg):
        return
    if accid != dc_accid:
        return
    text = (event.msg.text or "").strip()
    if not re.match(r"^/preview(?:@\w+)?(?:\s|$)", text, re.IGNORECASE):
        return
    _handle_preview_command(bot, accid, event, mode="readability")

@dc_cli.on(events.NewMessage(pattern=r"(?i)^/archive(?:@\w+)?(?:\s|$)", is_bot=None))
def archive_command(bot, accid, event):
    if _is_bot_blocked(bot, accid, event.msg):
        return
    if accid != dc_accid:
        return
    text = (event.msg.text or "").strip()
    if not re.match(r"^/archive(?:@\w+)?(?:\s|$)", text, re.IGNORECASE):
        return
    _handle_preview_command(bot, accid, event, mode="archive")

@dc_cli.on(events.NewMessage(pattern=r"(?i)^/webxdc(?:@\w+)?(?:\s|$)", is_bot=None))
def webxdc_command(bot, accid, event):
    if _is_bot_blocked(bot, accid, event.msg):
        return
    if accid != dc_accid:
        return
    text = (event.msg.text or "").strip()
    if not re.match(r"^/webxdc(?:@\w+)?(?:\s|$)", text, re.IGNORECASE):
        return
    _handle_preview_command(bot, accid, event, mode="webxdc")

@dc_cli.on(events.NewMessage(pattern=r"(?i)^/keep(?:@\w+)?(?:\s|$)", is_bot=None))
def keep_command(bot, accid, event):
    if _is_bot_blocked(bot, accid, event.msg):
        return
    if accid != dc_accid:
        return
    text = (event.msg.text or "").strip()
    if not re.match(r"^/keep(?:@\w+)?(?:\s|$)", text, re.IGNORECASE):
        return
    _handle_keep_command(bot, accid, event)

@dc_cli.on(events.NewMessage(pattern=r"(?i)^/jina(?:@\w+)?(?:\s|$)", is_bot=None))
def jina_command(bot, accid, event):
    if _is_bot_blocked(bot, accid, event.msg):
        return
    if accid != dc_accid:
        return
    text = (event.msg.text or "").strip()
    if not re.match(r"^/jina(?:@\w+)?(?:\s|$)", text, re.IGNORECASE):
        return
    _handle_jina_command(bot, accid, event)

@dc_cli.on(events.NewMessage(pattern=r"(?i)^/previewjs(?:@\w+)?(?:\s|$)", is_bot=None))
def previewjs_command(bot, accid, event):
    if _is_bot_blocked(bot, accid, event.msg):
        return
    if accid != dc_accid:
        return
    text = (event.msg.text or "").strip()
    if not re.match(r"^/previewjs(?:@\w+)?(?:\s|$)", text, re.IGNORECASE):
        return
    _handle_preview_command(bot, accid, event, mode="archive")

@dc_cli.on(events.NewMessage(pattern=r"(?i)^/download(?:@\w+)?(?:\s|$)", is_bot=None))
def download_command(bot, accid, event):
    if _is_bot_blocked(bot, accid, event.msg):
        return
    if accid != dc_accid:
        return
    text = (event.msg.text or "").strip()
    if not re.match(r"^/download(?:@\w+)?(?:\s|$)", text, re.IGNORECASE):
        return
    
    msg = event.msg
    if _is_duplicate_msg(msg.id, "preview"):
        return
    if _is_rate_limited(bot, accid, msg.from_id):
        _react(bot, accid, msg.id, "⏱")
        return

    # Extract target URL
    url = ""
    payload = event.payload.strip() if event.payload else ""
    if not payload and msg.text:
        parts = msg.text.strip().split(maxsplit=1)
        if len(parts) > 1:
            payload = parts[1].strip()

    if payload:
        url_match = re.search(r'(https?://[^\s<>"]+)', payload)
        if url_match:
            url = _clean_url_params(_strip_url_trailing_junk(url_match.group(1)))
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
                    url = _clean_url_params(_strip_url_trailing_junk(url_match.group(1)))

    if not url:
        _send(bot, accid, msg.chat_id, 
              "Usage:\n"
              "• `/download <url>` — Download file directly and send as attachment\n"
              "• Reply `/download` to another message containing a link.")
        return

    # Spawn thread to run in background
    t = threading.Thread(
        target=_do_download, 
        args=(bot, accid, msg.chat_id, msg.id, msg.from_id, url), 
        daemon=True
    )
    t.start()

@dc_cli.on(events.NewMessage(pattern=r"(?i)^/webpreview(?:@\w+)?(?:\s|$)", is_bot=None))
def webpreview_command(bot, accid, event):
    if _is_bot_blocked(bot, accid, event.msg):
        return
    if accid != dc_accid:
        return
    text = (event.msg.text or "").strip()
    if not re.match(r"^/webpreview(?:\s|$)", text, re.IGNORECASE):
        return
    
    msg = event.msg
    payload = (event.payload or "").strip().lower()
    if not payload and msg.text:
        parts = msg.text.strip().split(maxsplit=1)
        if len(parts) > 1:
            payload = parts[1].strip().lower()
    
    if payload in ("off", "0", "false"):
        database.set_webpreview_disabled(msg.chat_id, True)
        _send(bot, accid, msg.chat_id, "🔇 WebPreview has been disabled for this chat. I will no longer preview links automatically. You can still use `/preview`, `/archive`, or `/keep` on links manually.")
    elif payload in ("on", "1", "true"):
        database.set_webpreview_disabled(msg.chat_id, False)
        _send(bot, accid, msg.chat_id, "🔊 WebPreview has been enabled for this chat. Links will be parsed automatically.")
    else:
        is_disabled = database.is_webpreview_disabled(msg.chat_id)
        status = "disabled 🔇" if is_disabled else "enabled 🔊"
        _send(bot, accid, msg.chat_id,
              f"WebPreview is currently {status} for this chat.\n\n"
              f"Usage:\n"
              f"• `/webpreview off` (or `0`, `false`) — Disable automatic link previews\n"
              f"• `/webpreview on` (or `1`, `true`) — Enable automatic link previews")


def get_help_text(bot, accid, from_id):
    contact = bot.rpc.get_contact(accid, from_id)
    sender_email = contact.address

    help_text = (
        f"👋 Hi {sender_email}!\n\n"
        f"I save web pages as single self-contained HTML files and send them back to you.\n\n"
        f"**Commands:**\n"
        f"/preview <url> — Generate compressed reader-mode page (recommended)\n"
        f"/webxdc <url> — Generate WebXDC app from webpage 📱\n"
        f"/archive <url> — Generate full page archive (with JS enabled)\n"
        f"/tldr [url] — Generate AI summary of article or quoted message ⚡\n"
        f"/ai [text] — Ask AI a question, explain topic, or analyze photo/image 🤖\n"
        f"/lang [code] — Set summary language for this chat (e.g. /lang RU) 🌐\n"
        f"/download <url> — Download file directly (PDF, office, text)\n"
        f"/stats — View bot generation statistics\n"
        f"/webpreview [on|off] — Toggle automatic link previews 🔇\n"

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
        help_text += "/invidious_add <domain/url> — Register an Invidious server domain\n"
        help_text += "/invidious_rm <domain/url> — Deregister an Invidious server domain\n"
        help_text += "/invidious_list — List registered Invidious domains\n"
        help_text += "/jina <api_key> — Check Jina AI API key token balance\n"
        if _karakeep_enabled():
            help_text += "\n**KaraKeep & Archiving:**\n"
            help_text += "/keep <url> — Save to KaraKeep (private), Web Archive & Archive.today 🔖🏛️\n"
        else:
            help_text += "/keep <url> — Save URL to Web Archive & Archive.today fallback 🏛️\n"

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
    api_s = database.get_api_stats()
    usage = shutil.disk_usage(CACHE_DIR)
    free_gb = usage.free / (1024**3)
    total_gb = usage.total / (1024**3)
    free_pct = (usage.free / usage.total) * 100

    is_admin = _is_dc_admin(bot, accid, event.msg.from_id)
    
    reply = (
        f"📊 **WebPreview Bot Statistics**\n\n"
        f"Total previews generated (last 24h): {s['total']} ({s['last_24h']})\n"
        f"JINA AI requests (last 24h): {api_s['jina_total']} ({api_s['jina_24h']})\n"
        f"Gemini AI requests (last 24h): {api_s['gemini_total']} ({api_s['gemini_24h']})\n"
        f"Total file bandwidth: {_format_size(s['total_size'])}\n"
    )

    if JINA_API_KEY:
        try:
            check_url = f"https://dash.jina.ai/api/v1/api_key/fe_user?api_key={JINA_API_KEY}"
            req = urllib.request.Request(check_url, headers={"User-Agent": STANDARD_USER_AGENT})
            with _urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
            wallet = data.get("wallet", {})
            tot_bal = wallet.get("total_balance", 0)
            reply += f"JINA AI token balance: {tot_bal:,}\n"
        except Exception:
            pass

    if GEMINI_API_KEY:
        now = time.time()
        active_model = None
        for m in GEMINI_MODELS:
            if _GEMINI_MODEL_COOLDOWNS.get(m, 0) <= now:
                active_model = m
                break
        if not active_model:
            active_model = GEMINI_MODELS[0]

        reply += f"Gemini active model: {active_model}\n"

        cooldowns = []
        for m in GEMINI_MODELS:
            cd_until = _GEMINI_MODEL_COOLDOWNS.get(m, 0)
            if cd_until > now:
                mins_left = int((cd_until - now) / 60)
                cooldowns.append(f"{m} ({mins_left}m left)")
        if cooldowns:
            reply += f"Gemini cooling down: {', '.join(cooldowns)}\n"

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

    # Get connectivity HTML to parse per-transport status
    connectivity_html = ""
    try:
        connectivity_html = bot.rpc.get_connectivity_html(accid)
    except Exception:
        pass

    # Get resilient sending mode status
    resilient_on = False
    try:
        resilient_on = database.get_config("resilient") == "1"
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

    import re
    for addr in transport_addrs:
        # Determine status label from HTML
        status_label = "❓ Unknown"
        if connectivity_html:
            domain = addr.split('@')[-1] if '@' in addr else addr
            pattern = rf'class="([^"]+)\s+dot".*?<b>{re.escape(domain)}:</b>\s*([^<]+)'
            match = re.search(pattern, connectivity_html, re.IGNORECASE)
            if match:
                color = match.group(1).lower()
                status_text = match.group(2).strip().lower()
                if "yellow" in color or "connecting" in status_text:
                    status_label = "🟡 Connecting"
                elif "green" in color:
                    status_label = "🔄 Working"
                elif "red" in color or "lost" in status_text or "error" in status_text:
                    status_label = "🔴 Not connected"

        is_used = resilient_on or (addr == active_addr)
        used_str = " ✔︎ Used for sending:" if is_used else ":"
        reply += f"**{status_label}**{used_str} `{addr}`\n"

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

def _clean_domain(domain_or_url: str) -> str:
    """Clean a domain input, extracting it from a full URL if necessary."""
    val = domain_or_url.strip()
    if "://" in val or val.startswith("//"):
        try:
            parsed = urllib.parse.urlparse(val)
            netloc = parsed.netloc or parsed.path.split('/')[0]
            if netloc:
                val = netloc
        except Exception:
            pass
    # Strip any port number if present
    if ":" in val:
        val = val.split(":")[0]
    return val.lower().strip()

@dc_cli.on(events.NewMessage(command="/invidious_add"))
def invidious_add_command(bot, accid, event):
    msg = event.msg
    text = (msg.text or "").strip()
    if not re.match(r"^/invidious_add(?:\s|$)", text):
        return
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /invidious_add.")
        return

    arg = event.payload.strip() if event.payload else ""
    if not arg:
        _send(bot, accid, msg.chat_id, "Usage: `/invidious_add <domain_or_url>`")
        return

    domain = _clean_domain(arg)
    if not domain:
        _send(bot, accid, msg.chat_id, "❌ Invalid domain or URL.")
        return

    try:
        database.add_invidious_domain(domain)
        _send(bot, accid, msg.chat_id, f"✅ Registered Invidious domain: `{domain}`")
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to register Invidious domain: {e}")

def _invidious_rm_logic(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return

    arg = event.payload.strip() if event.payload else ""
    if not arg:
        _send(bot, accid, msg.chat_id, f"Usage: `{msg.text.split()[0]} <domain_or_url>`")
        return

    domain = _clean_domain(arg)
    if not domain:
        _send(bot, accid, msg.chat_id, "❌ Invalid domain or URL.")
        return

    try:
        database.remove_invidious_domain(domain)
        _send(bot, accid, msg.chat_id, f"✅ Deregistered Invidious domain: `{domain}`")
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to deregister Invidious domain: {e}")

@dc_cli.on(events.NewMessage(command="/invidious_rm"))
def invidious_rm_command(bot, accid, event):
    msg = event.msg
    text = (msg.text or "").strip()
    if not re.match(r"^/invidious_rm(?:\s|$)", text):
        return
    _invidious_rm_logic(bot, accid, event)

@dc_cli.on(events.NewMessage(command="/invidious_remove"))
def invidious_remove_command(bot, accid, event):
    msg = event.msg
    text = (msg.text or "").strip()
    if not re.match(r"^/invidious_remove(?:\s|$)", text):
        return
    _invidious_rm_logic(bot, accid, event)

@dc_cli.on(events.NewMessage(command="/invidious_list"))
def invidious_list_command(bot, accid, event):
    msg = event.msg
    text = (msg.text or "").strip()
    if not re.match(r"^/invidious_list(?:\s|$)", text):
        return
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /invidious_list.")
        return

    try:
        domains = database.list_invidious_domains()
        if not domains:
            _send(bot, accid, msg.chat_id, "No custom Invidious domains registered.")
            return

        reply = "📺 **Registered Invidious Domains:**\n\n"
        for idx, dom in enumerate(domains, 1):
            reply += f"{idx}. `{dom}`\n"
        _send(bot, accid, msg.chat_id, reply)
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to list Invidious domains: {e}")

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
    if _is_bot_blocked(bot, accid, event.msg):
        return
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

    # 1. Intercept dynamic commands: /preview_urlhash, /previewjs_urlhash, /archive_urlhash, /webxdc_urlhash, /download_urlhash, /keep_urlhash or /tldr_urlhash
    m = re.match(r"^/(preview|previewjs|archive|webxdc|download|keep|tldr)_([0-9a-fA-F]{8})(?:@\w+)?", text, re.IGNORECASE)
    if m:
        cmd_type, urlhash = m.group(1).lower(), m.group(2).lower()
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

        if cmd_type == "keep":
            _react(bot, accid, msg.id, "🏛️" if not (_is_dc_admin(bot, accid, msg.from_id) and _karakeep_enabled()) else "🔖")
            t = threading.Thread(
                target=_do_keep,
                args=(bot, accid, msg.chat_id, msg.id, msg.from_id, url),
                daemon=True
            )
            t.start()
        elif cmd_type == "tldr":
            t = threading.Thread(
                target=_do_tldr,
                args=(bot, accid, msg.chat_id, msg.id, msg.from_id, url),
                daemon=True
            )
            t.start()
        elif cmd_type == "download":
            t = threading.Thread(
                target=_do_download, 
                args=(bot, accid, msg.chat_id, msg.id, msg.from_id, url), 
                daemon=True
            )
            t.start()
        else:
            if cmd_type == "webxdc":
                mode = "webxdc"
            elif cmd_type in ("previewjs", "archive"):
                mode = "archive"
            else:
                mode = "readability"
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

        # 2. Automatically parse URLs sent in chat (if not starting with a slash command and previews enabled)
        if not text.startswith("/") and not database.is_webpreview_disabled(msg.chat_id):
            url_match = re.search(r'(https?://[^\s<>"]+)', text)
            if url_match:
                raw_url = _strip_url_trailing_junk(url_match.group(1))
                url = _clean_url_params(raw_url)
                
                if _is_internal_or_invalid_url(url):
                    return

                # Check if it's a direct image link and process it
                if _is_image_url(url):
                    logger.info(f"Detected direct image URL in chat {msg.chat_id}: {url}")
                    success, result_path, compress_info = _download_and_compress_image(url, msg.chat_id)
                    if success:
                        t = threading.Thread(
                            target=_send_image_to_chat,
                            args=(bot, accid, msg.chat_id, result_path, compress_info, url),
                            daemon=True
                        )
                        t.start()
                    else:
                        logger.warning(f"Failed to download/compress image {url}: {result_path}")
                    return

                # Skip if the URL is in the exclusions
                if database.is_excluded(url):
                    return

                # Skip if YT Bot is in the chat and this is a link handled by YT Bot
                if _is_yt_bot_in_chat(bot, accid, msg.chat_id):
                    if _is_handled_by_yt_bot(url):
                        logger.info(f"Skipping link auto-preview for {url} since YT Bot is present and handles it.")
                        return
                    # Check if this is a known Invidious domain
                    domain = urllib.parse.urlparse(url).netloc.lower()
                    is_invidious = False
                    try:
                        if database.get_config(f"invidious_domain_{domain}") == "1":
                            is_invidious = True
                    except Exception:
                        pass
                    
                    if is_invidious:
                        video_id = _extract_youtube_id_from_invidious(url)
                        if video_id:
                            yt_link = f"https://youtu.be/{video_id}"
                            logger.info(f"Detected known Invidious URL {url} in chat with YT Bot. Forwarding: {yt_link}")
                            _send(bot, accid, msg.chat_id, yt_link)
                            return
                    
                # Rate limiting check
                if _is_rate_limited(bot, accid, msg.from_id):
                    if is_private:
                        _react(bot, accid, msg.id, "⏱")
                    return
                
                # Run link preview generation in background thread
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


_message_failover_attempts = {}

@dc_cli.on(events.RawEvent(events.EventType.MSG_FAILED))
def on_msg_failed(bot, accid, event):
    """Handle message sending failures by switching to a backup transport temporarily with backoff."""
    try:
        if database.get_config("resilient") == "1":
            return
    except Exception:
        pass

    msg_id = getattr(event, 'msg_id', None)
    if not msg_id:
        return

    try:
        global _message_failover_attempts
        if len(_message_failover_attempts) > 1000:
            _message_failover_attempts.clear()

        # Retrieve or initialize tracking state for this message
        state = _message_failover_attempts.get(msg_id)
        if state is None:
            state = {'count': 0, 'transports': set()}
            _message_failover_attempts[msg_id] = state

        # Stop retrying if we reached the maximum attempt limit (e.g. 10 attempts)
        if state['count'] >= 10:
            return

        state['count'] += 1

        # Retrieve message and verify it is indeed in failed state (state 24)
        try:
            msg_snapshot = bot.rpc.get_message(accid, msg_id)
            msg_state = msg_snapshot.get('state') if isinstance(msg_snapshot, dict) else getattr(msg_snapshot, 'state', None)
            if msg_state != 24:
                return
        except Exception:
            return

        # Fetch chat details to include in logs (checking both snake_case and camelCase key fallbacks)
        chat_id = None
        if isinstance(msg_snapshot, dict):
            chat_id = msg_snapshot.get('chat_id') or msg_snapshot.get('chatId')
        else:
            chat_id = getattr(msg_snapshot, 'chat_id', getattr(msg_snapshot, 'chatId', None))
            
        chat_name = "Unknown"

        if chat_id:
            try:
                chat_info = bot.rpc.get_full_chat_by_id(accid, chat_id)
                if isinstance(chat_info, dict):
                    chat_name = chat_info.get('name', 'Unknown')
                else:
                    chat_name = getattr(chat_info, 'name', 'Unknown')
            except Exception:
                pass

        # Check if it's a permanent E2E encryption failure
        msg_error = msg_snapshot.get('error') if isinstance(msg_snapshot, dict) else getattr(msg_snapshot, 'error', None)
        if msg_error:
            msg_error_lower = msg_error.lower()
            if "encryption" in msg_error_lower or "unencrypted" in msg_error_lower or "шифр" in msg_error_lower or "зашифр" in msg_error_lower:
                bot.logger.warning(
                    f"Permanent E2E encryption failure for message {msg_id} in chat '{chat_name}' (ID: {chat_id}): {msg_error}. "
                    f"Stopping failover attempts immediately."
                )
                return

        # List all configured transports
        try:
            transports = bot.rpc.list_transports(accid)
        except Exception:
            transports = []

        if len(transports) <= 1:
            bot.logger.info(f"Message {msg_id} failed to send, but only {len(transports)} transport(s) configured. Cannot failover.")
            return

        current_addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr")
        if not current_addr:
            return

        # Find current transport index
        current_idx = -1
        for idx, t in enumerate(transports):
            t_addr = t.get('addr') if isinstance(t, dict) else getattr(t, 'addr', None)
            if t_addr and t_addr.lower() == current_addr.lower():
                current_idx = idx
                break

        if current_idx == -1:
            bot.logger.warning(f"Current transport {current_addr} not found in transports list.")
            current_idx = 0

        # Try to find the next transport
        next_idx = (current_idx + 1) % len(transports)
        next_t = transports[next_idx]
        next_addr = next_t.get('addr') if isinstance(next_t, dict) else getattr(next_t, 'addr', None)

        if not next_addr or next_addr.lower() == current_addr.lower():
            bot.logger.info("No alternative transport available for failover.")
            return

        # Check if we have already tried this transport for this message
        if next_addr.lower() in state['transports']:
            if len(state['transports']) >= len(transports):
                bot.logger.warning(f"All available transports have been tried for message {msg_id}. Stopping failover.")
                return

        state['transports'].add(current_addr.lower())

        # Calculate exponential backoff delay: 5, 10, 20, 40, 80, 160... seconds (max 5 minutes)
        delay = min(300, 5 * (2 ** (state['count'] - 1)))
        bot.logger.warning(
            f"Resilient Failover: Message {msg_id} (Chat: {chat_name}, ID: {chat_id}) failed on {current_addr} (attempt {state['count']}/10). "
            f"Scheduling resend on transport {next_addr} in {delay}s."
        )

        init_addr = current_addr

        # Schedule the resend asynchronously using a non-blocking Timer thread
        def delayed_resend():
            try:
                bot.logger.info(f"Executing scheduled resend for message {msg_id} in chat '{chat_name}' (ID: {chat_id}) on transport {next_addr}...")
                with resilient_lock:
                    # Switch configured_addr to next transport temporarily
                    bot.rpc.set_config(accid, "configured_addr", next_addr)
                    time.sleep(1) # Give core a moment to reconfigure
                    
                    bot.rpc.resend_messages(accid, [msg_id])
                    
                    # Wait up to 10 seconds for the resent message to be delivered/failed
                    start_time = time.time()
                    delivered = False
                    while time.time() - start_time < 10:
                        try:
                            raw_msg = bot.rpc.get_message(accid, msg_id)
                            if raw_msg:
                                from deltachat2 import AttrDict
                                msg_snapshot = AttrDict(raw_msg)
                                state = msg_snapshot.get('state') if isinstance(msg_snapshot, dict) else getattr(msg_snapshot, 'state', None)
                                if state in (26, 28):
                                    bot.logger.info(f"Resilient Failover bg: msg {msg_id} delivered successfully on {next_addr}.")
                                    delivered = True
                                    break
                                if state == 24:
                                    bot.logger.warning(f"Resilient Failover bg: msg {msg_id} failed on {next_addr}.")
                                    break
                        except Exception as poll_err:
                            bot.logger.debug(f"Resilient Failover bg poll error: {poll_err}")
                        time.sleep(0.5)

                    if not delivered:
                        bot.logger.warning(f"Resilient Failover bg: msg {msg_id} did not deliver on {next_addr} within timeout.")

            except Exception as resend_err:
                bot.logger.warning(f"Error executing scheduled resend for message {msg_id} in chat '{chat_name}' (ID: {chat_id}): {resend_err}")
                err_str = str(resend_err).lower()
                if "e2e encryption" in err_str or "encryption" in err_str:
                    bot.logger.warning(f"E2E encryption error detected during resend of msg {msg_id} in chat '{chat_name}'. Stopping further failovers.")
                    try:
                        _message_failover_attempts[msg_id]['count'] = 10
                    except Exception:
                        pass
            finally:
                # Always restore the initial primary transport address!
                try:
                    bot.logger.info(f"Resilient Failover bg: restoring primary transport to {init_addr}")
                    bot.rpc.set_config(accid, "configured_addr", init_addr)
                except Exception as restore_err:
                    bot.logger.error(f"Resilient Failover bg: failed to restore transport to {init_addr}: {restore_err}")

        import threading
        threading.Timer(delay, delayed_resend).start()



    except Exception as e:
        bot.logger.error(f"Error handling message failover for message {msg_id}: {e}")


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
        
        # Configure storage and auto-cleanup
        delete_after = os.environ.get("DELETE_DEVICE_AFTER", "3600")
        download_limit = os.environ.get("DOWNLOAD_LIMIT", "1")
        bot.rpc.set_config(dc_accid, "delete_device_after", delete_after)
        bot.rpc.set_config(dc_accid, "download_limit", download_limit)
        bot.logger.info(f"Storage settings configured: delete_device_after={delete_after}s, download_limit={download_limit} bytes")
        
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

def setup_custom_command_parser(bot, allowed_prefixes):
    original_parse_command = bot._parse_command

    def custom_parse_command(accid: int, event) -> None:
        text = event.msg.text
        if not text:
            original_parse_command(accid, event)
            return

        parts = text.split(maxsplit=1)
        cmd = parts[0]
        
        if "@" in cmd:
            cmd_name, suffix = cmd.split("@", 1)
            suffix_lower = suffix.lower()
            
            if suffix_lower:
                try:
                    self_address = bot.rpc.get_contact(accid, 1).address.lower()
                except Exception:
                    self_address = ""
                
                matched = False
                for p in allowed_prefixes:
                    if suffix_lower.startswith(p.lower()) or p.lower().startswith(suffix_lower):
                        matched = True
                        break
                if not matched and self_address and suffix_lower == self_address:
                    matched = True
                
                if matched:
                    new_text = cmd_name
                    if len(parts) > 1:
                        new_text += " " + parts[1]
                    
                    original_text = event.msg.text
                    event.msg["text"] = new_text
                    try:
                        original_parse_command(accid, event)
                    finally:
                        event.msg["text"] = original_text
                else:
                    event.command = ""
                    event.payload = ""
            else:
                original_parse_command(accid, event)
        else:
            original_parse_command(accid, event)
            
            if event.command in ("/help", "/stats"):
                try:
                    chat = bot.rpc.get_chat(accid, event.msg.chat_id)
                    is_group = getattr(chat, "chat_type", "Single") != "Single"
                except Exception:
                    is_group = False
                
                if is_group:
                    try:
                        contacts = bot.rpc.get_chat_contacts(accid, event.msg.chat_id)
                        bot_count = 0
                        for contact_id in contacts:
                            if contact_id == 1:
                                bot_count += 1
                                continue
                            c = bot.rpc.get_contact(accid, contact_id)
                            if getattr(c, "is_bot", False):
                                bot_count += 1
                                if bot_count > 1:
                                    break
                        if bot_count > 1:
                            event.command = ""
                            event.payload = ""
                    except Exception:
                        pass

    bot._parse_command = custom_parse_command


@dc_cli.on_start
def on_start(bot, args):
    global dc_bot_instance, dc_accid
    setup_custom_command_parser(bot, ["web", "wp"])
    dc_bot_instance = bot
    
    accounts = bot.rpc.get_all_account_ids()
    if not accounts:
        logger.error("No accounts found.")
        return
        
    accid = accounts[0]
    dc_accid = accid
    
    logger.info(f"WebPreview bot v{VERSION} started with accid {accid}.")
    
    allowed_bots_env = os.environ.get("ALLOWED_BOT_EMAILS", "")
    allowed_bots = [e.strip().lower() for e in allowed_bots_env.split(",") if e.strip()]
    if allowed_bots:
        logger.info(f"Whitelisted bot emails: {', '.join(allowed_bots)}")
    else:
        logger.info("No whitelisted bot emails configured (other bots will be ignored).")
    
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
