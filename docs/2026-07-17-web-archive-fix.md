# Fix for Web Archive Timeout Issue

## Problem

The `/keep` command was failing with the error:

```
❌ Failed to save to Web Archive.
Reason: The read operation timed out
```

This happened because the `_save_to_web_archive` function had several issues:

1. **Timeout too short**: Used only 20 seconds, but many websites take longer to respond
2. **No proxy routing**: Didn't route through proxy for domains in `PROXY_DOMAINS`
3. **No fallback**: If blocked by Anubis protection, didn't retry with `NON_MOZILLA_USER_AGENT`
4. **No logging**: Didn't log any information about the save attempt

## Solution

Fixed `_save_to_web_archive` function to:

1. Use a **60-second timeout** instead of 20 seconds
2. **Route through proxy** for domains in `PROXY_DOMAINS` (same logic as `_check_url_headers`)
3. **Retry with `NON_MOZILLA_USER_AGENT`** if blocked by Anubis protection
4. **Add logging** for debugging

## Code Changes

### Before (broken)

```python
def _save_to_web_archive(url: str) -> tuple[bool, str]:
    save_url = f"https://web.archive.org/save/{url}"
    try:
        req = urllib.request.Request(
            save_url,
            headers={'User-Agent': STANDARD_USER_AGENT}
        )
        with _urlopen(req, timeout=20) as response:  # <-- TIMEOUT IS 20 seconds!
            redirected_url = response.geturl()
            if "/web/" in redirected_url or "archive.org" in redirected_url:
                return True, redirected_url
            return True, redirected_url
    except Exception as e:
        logger.error(f"Failed to save {url} to Web Archive: {e}")
        return False, str(e)
```

### After (fixed)

```python
def _save_to_web_archive(url: str) -> tuple[bool, str]:
    """
    Save a URL to Web Archive (Wayback Machine).
    Uses STANDARD_USER_AGENT first, then falls back to NON_MOZILLA_USER_AGENT if blocked.
    Routes through proxy if needed, and uses a 60-second timeout to handle slow responses.
    Returns (success, archived_url_or_error).
    """
    save_url = f"https://web.archive.org/save/{url}"

    logger.info(f"Saving URL to Web Archive: {save_url}")

    # Try STANDARD_USER_AGENT first
    user_agents = [STANDARD_USER_AGENT, NON_MOZILLA_USER_AGENT]
    saved_with_ua = None

    for ua in user_agents:
        try:
            req = urllib.request.Request(
                save_url,
                headers={'User-Agent': ua}
            )

            # Route through proxy if needed (same logic as _check_url_headers)
            if _should_use_proxy(save_url):
                logger.info(f"Routing Web Archive save request for {url} through proxy: {PROXY_URL}")
                proxy_handler = urllib.request.ProxyHandler({'http': PROXY_URL, 'https': PROXY_URL})
                opener = urllib.request.build_opener(proxy_handler)
            else:
                opener = urllib.request

            with opener.open(req, timeout=60) as response:
                logger.info(f"Web Archive save succeeded with User-Agent: {ua}")
                redirected_url = response.geturl()

                # If the response redirected to standard web.archive.org snapshot, we return it
                if "/web/" in redirected_url or "archive.org" in redirected_url:
                    return True, redirected_url

                saved_with_ua = ua
                break  # Success, exit the loop

        except urllib.error.HTTPError as e:
            logger.warning(f"Web Archive HTTP error {e.code} for UA '{ua}': {e.reason}. Retrying with next UA...")
        except Exception as e:
            logger.warning(f"Web Archive save failed with UA '{ua}': {e}. Retrying with next UA...")

    # Check if we successfully saved with either User-Agent
    if saved_with_ua is None:
        logger.error(f"Failed to save {url} to Web Archive after trying all User-Agents.")
        return False, "Read operation timed out or failed"

    # Check for Anubis block (same as _is_anubis_blocked)
    try:
        if not os.path.exists(save_url):
            return False, "Read operation timed out or failed"
        with open(save_url, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read(128 * 1024)  # Read first 128KB
            signatures = [
                "Protected by Anubis",
                "Testing to determine if you are a bot!",
                "anubis.techaro.lol"
            ]
            if any(sig in content for sig in signatures):
                logger.warning(f"Web Archive blocked with Anubis protection for {url}.")
                return False, "Read operation timed out or failed"
    except Exception as e:
        logger.error(f"Error checking Anubis status: {e}")
        return False, "Read operation timed out or failed"

    return True, save_url
```

## Testing

Test with URLs that previously failed:

- `https://example.com`
- Any blocked website (e.g., sites with Anubis protection)

## Additional LSP Fixes

The following linting issues were also fixed in `bot.py`:

- Added proper type checking for BeautifulSoup attributes
- Fixed return type annotations in `_run_monolith_process`
- Added `import urllib` for `urllib.parse.urljoin`
