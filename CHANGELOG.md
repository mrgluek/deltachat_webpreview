# Changelog

All notable changes to the Delta Chat WebPreview Bot will be documented in this file.

## [2.3.18] - 2026-06-29

### Added
- **Web Archive fallback for `/keep`**: Overhauled `/keep` command, `/keep_{urlhash}` dynamic command and `/keep` quote replies so they are accessible to all users. Non-admin users, or any users if KaraKeep is not configured, will have their URLs archived to Web Archive (Wayback Machine) via a background HTTP request to `https://web.archive.org/save/` and get the snapshot URL sent back to the chat. If KaraKeep is configured, the bot administrator's requests continue to save directly to KaraKeep.

## [2.3.17] - 2026-06-26

### Fixed
- **Image Inlining responsive bypass:** Web browsers were bypassing inlined Base64 image payloads and downloading original files (e.g. from Habr's `habrastorage.org`) because responsive attributes (`srcset` and `sizes`) and lazy loading attributes (`data-src` / `data-srcset`) were left intact on `<img>` tags. Refactored image inlining into a unified `_inline_soup_images()` helper that strips these attributes.

## [2.3.16] - 2026-06-26

### Added
- **Telegram Preview Caching Support:** Extracted Telegram post content is now returned as markdown (`jina_markdown`), allowing `/preview` to generate and compile readability HTML previews directly from local cache without making redundant network requests.

### Fixed
- **Telegram Post Parser:** Switched parsing from the non-functional `t.me/oembed` endpoint (which resolves incorrectly to an `oembed` channel page on Telegram) to the official public preview feed (`t.me/s/...`) using BeautifulSoup, robustly extracting the author name, content text (with preserved line breaks), and media thumbnails.

## [2.3.15] - 2026-06-26

### Added
- **Telegram Post Parser (`_fetch_telegram_og_data`):** When a `t.me` link is sent to the bot, it now fetches post metadata via Telegram's oEmbed API (`https://t.me/oembed?url=...&format=json`) instead of trying to scrape the page HTML. Supports standard post URLs (`t.me/channel/123`), stream-prefixed URLs (`t.me/s/channel/123`), and private channel numeric IDs (`t.me/c/123456789/123`). The preview title is formed as `"channel_name: post text excerpt (up to 200 chars)"`, with thumbnail image included if the post has media. The oEmbed path is an early-return in `_get_og_preview_data` before the standard HTML fetch and Jina fallback.

## [2.3.14] - 2026-06-26

### Fixed
- **URL trailing junk stripping:** URLs extracted from natural-language text were not properly cleaned of Unicode closing punctuation. For example, `https://www.kommersant.ru/doc/8765189)»` would propagate the trailing `)»` into fetch requests, causing ASCII encoding errors. Introduced `_strip_url_trailing_junk()` which removes standard ASCII punctuation (`.,;:!?`), Unicode closing quotation marks (`»`, `"`, `'`, `›`, etc.) and unbalanced closing parentheses/brackets, while safely preserving balanced parens (e.g. Wikipedia URLs). All 8 URL extraction call-sites now use this function.
- **Unit tests:** Added `tests/test_strip_url.py` with 18 tests covering the new helper.

## [2.3.13] - 2026-06-25

### Added
- **Unit Test Suite (`tests/`):** Introduced a structured `tests/` directory with 53 unit tests across three modules:
  - `test_url_validation.py` – URL validation (`_is_internal_or_invalid_url`): valid external domains, blocked IP ranges, local TLDs.
  - `test_invidious.py` – Invidious/YouTube helpers: video ID extraction, domain cleaning, and database add/remove/list operations.
  - `test_proxy_and_jina.py` – Proxy routing, Jina AI Reader integration, SVG skip logic, octet-stream header handling, cache saving, OG fallback, and the `_do_preview` cache optimization.
- **GitHub Actions CI (`.github/workflows/tests.yml`):** Automated test runner that executes the full test suite on every push and pull request to `main`.

## [2.3.12] - 2026-06-25

### Optimized
- **Manual Preview and Download Caching optimization:** Modified `_do_preview` and `_do_download` to check `og_cache` first and skip redundant network/HTTP header queries to the remote site if a fresh preview cache is already found in the SQLite database. This fixes the issue where clicking `/preview` or `/download` triggered another request to the target website despite having a cached copy.

## [2.3.11] - 2026-06-25

### Added
- **Jina Crawler Proxy Routing (X-Proxy-Url):** If the target URL matches a domain in `PROXY_DOMAINS` (e.g. `.ru`) and `PROXY_URL` is set, the bot now forwards the proxy URL to Jina using the `X-Proxy-Url` header. This instructs Jina's remote crawler to download the target site through our proxy server, resolving geo-restrictions or IP blocks on target Russian sites, while keeping `JINA_PROXY_URL` isolated for local bot connection to the Jina API.

## [2.3.10] - 2026-06-25

### Added
- **Jina HTML Readability Fallback:** Added support for requesting raw HTML from Jina AI Reader (`X-Return-Format: html`). When standard readability extraction fails, the bot now falls back to querying raw HTML from Jina and running `readability-lxml` directly on it. This combines Jina's dynamic rendering/WAF bypass capabilities with Python's native readability/BeautifulSoup pre-cleaning logic, yielding 100% clean layouts without menus or boilerplate, while keeping Jina Markdown as a tertiary fallback.

## [2.3.9] - 2026-06-25

### Added
- **Robot Indicator for Jina Previews:** Added a robot emoji indicator `🤖🌐` next to the title when the preview is served or fetched via Jina AI Reader, giving visual feedback that Jina was used.
- **Jina Request Exclusions:** Configured Jina Reader requests with `X-Exclude-Selector` headers to exclude common layout elements (`nav`, `footer`, `header`, etc.) directly on Jina's side, reducing payload size.
- **Jina Markdown Cleaning Heuristics:** Implemented a robust markdown-cleaning algorithm (`_clean_jina_markdown`) to isolate only the main article text (starts with title heading and ends before copyright/boilerplates), greatly enhancing readability results.

### Changed
- **Reverted Group Previews to Clean Buttons:** Reverted direct rendering of Jina markdown inside group chat messages. Group previews now always show the clean card layout with `/preview`, `/archive`, and `/keep` buttons instead of displaying full-page articles directly in the chat.

## [2.3.8] - 2026-06-25

### Added
- **Direct Jina Markdown Preview rendering:** When standard OG extraction fails and Jina AI Reader is triggered, the bot now directly outputs the parsed page content (markdown) inside the group chat message instead of presenting `/preview` and `/archive` buttons.
- **Cached Jina Compilation:** When Jina markdown is fetched during link preview, it is compiled into a readability HTML file and stored in the database cache. If manual `/preview_[hash]` or private readability previews are requested later, the bot serves the compiled HTML preview instantly from cache, avoiding redundant network queries and saving Jina Reader API calls.

## [2.3.7] - 2026-06-25

### Changed
- **WAF Bypass / Octet-Stream Handling:** Improved `_check_url_headers` to allow `application/octet-stream` content-types for URLs that do not match known binary file extensions. This prevents fast-rejection of webpages protected by anti-bot/WAF systems (like QRATOR returning empty octet-streams to bots) and lets the bot proceed to the Jina AI fallback preview generator successfully.

## [2.3.6] - 2026-06-25

### Fixed
- **SVG Image Processing:** Fixed a crash/warning when processing SVG preview images. SVG vector files are now ignored during preview generation to prevent Pillow from failing to compress them and sending them as broken image attachments.

## [2.3.5] - 2026-06-25

### Added
- **Jina AI API Key & Proxy Support:** Added support for specifying a Jina AI API key via `JINA_API_KEY` to authenticate requests and increase rate limits. Isolated Jina AI Reader requests from the default `PROXY_URL` (they now execute directly by default) and added support for a dedicated `JINA_PROXY_URL` proxy configuration.
- **Domain-Specific Proxy Routing:** Added support for routing standard URL fetches and `monolith` subprocesses targeting specific domain suffixes (like `.ru`) through an optional proxy server, configured via `PROXY_URL` and `PROXY_DOMAINS` environment variables.
- **Environment Template:** Added a `.env.example` template file for local deployment convenience.

## [2.3.4] - 2026-06-25

### Added
- **Jina AI Warning Previews:** The bot now extracts and displays warnings (such as CAPTCHA notices) in group link previews if Jina AI returns a blank title and warns about page access restrictions, falling back to a clean `URL Source: <url>` title.

## [2.3.3] - 2026-06-25

### Changed
- **Bidirectional Suffix Matching:** Suffix matching is now bidirectional (e.g. `@w` or `@webpreview` will match WebPreview bot).
- **Smart Group Chat Command Filtering:** The bot now automatically ignores unaddressed general `/help` and `/stats` commands in group chats if other bots are present in the chat.

## [2.3.2] - 2026-06-25

### Added
- **Target-Specific Command Suffixes:** Added support for addressing this bot specifically in group chats using `/command@web` or `/command@wp` suffixes.

## [2.3.1] - 2026-06-25

### Changed
- **Compact Preview Format:** Simplified the layout of web and file preview messages to be more compact, utilizing Markdown links to combine the title/filename with the URL, and aligning command options side-by-side.
- **Dynamic Keep Command:** Added a dynamic `/keep_[hash]` command link alongside the `/preview_[hash]` and `/archive_[hash]` links to easily bookmark pages.

## [2.3.0] - 2026-06-25

### Added
- **KaraKeep Bookmark Integration:** Added opt-in integration with self-hosted [KaraKeep](https://karakeep.app/) instances for saving web bookmarks via the bot. Configured through `KARAKEEP_URL`, `KARAKEEP_API_KEY`, and optional `KARAKEEP_TAGS` environment variables. When enabled, the bot administrator can use `/keep <url>` or reply `/keep` to any message containing a link to save it to KaraKeep. The bookmark is saved via the KaraKeep REST API (`POST /api/v1/bookmarks`) with optional tags and the bot reports the result with a direct link to the saved bookmark.

## [2.2.7] - 2026-06-24

### Fixed
- **URL Validation Enhancements:** Improved `_is_internal_or_invalid_url` to filter out malformed URLs with hostnames containing no dots (e.g. `https://юрл`, `http://test`), unless they represent `localhost` or valid IP addresses.
- **Log Noise Reduction:** Silenced warning log messages for bracketed invalid hostname/IP syntax issues by handling `ValueError` separately and logging it as debug messages.

## [2.2.6] - 2026-06-23


### Added
- **Manual Invidious Domain Management:** Added admin commands `/invidious_add <domain/url>`, `/invidious_rm <domain/url>`, and `/invidious_list` to allow manually registering, deregistering, and listing Invidious instances. This ensures video links can still be rewritten and forwarded to `YT Bot` even if the Invidious server blocks the WebPreview Bot from auto-detecting it (e.g. returning `418 I'm a teapot` or Cloudflare checks).

## [2.2.5] - 2026-06-23


### Added
- **Invidious Detection and YT Bot Redirection:** Automatically identifies Invidious instances (alternative YouTube front-ends) by checking description metadata. When `YT Bot` is present in the chat, it intercepts these links, extracts the video ID, rewrites them to `youtu.be` links, and forwards them to the chat, completely skipping WebPreview generation.
- **Invidious Domain Learning Cache:** Implemented a self-learning database cache that remembers detected Invidious instance domains. On subsequent requests, it immediately identifies the instance and redirects video links to `YT Bot` without making any network calls.

## [2.2.4] - 2026-06-23

### Added
- **Jina.ai Fallback Support:** Integrated Jina AI Reader (`r.jina.ai`) to resolve webpage titles, preview images, and markdown text content when standard fetching or readability extraction fails or gets blocked (e.g. by anti-bot checks).
- **Markdown-to-HTML Translator:** Implemented a placeholder-based markdown-to-HTML parser that formats Jina's markdown output into styled HTML. It protects underscores inside URLs from being parsed as italic markdown.
- **Privacy & Safety Image Filtering:** In readability previews, any images that fail to download/inline (such as analytics tracking pixels, ads, or broken links) are automatically stripped from the HTML, preventing data leakage and broken images.

## [2.2.3] - 2026-06-17

### Added
- **Image Compression Logging:** Log messages for compressed images (both OG banner images and monolith HTML files) now print the original and compressed sizes in parentheses (e.g. `(120 KB -> 45 KB)`), providing better visibility into optimization efficiency.

## [2.2.2] - 2026-06-16

### Added
- **Automatic Transport Failover:** Implemented a robust, event-driven transport failover mechanism. The bot now listens to the core's `MSG_FAILED` event. When a message fails to deliver, it automatically switches `configured_addr` to the next configured backup transport, and schedules a resend of the message using exponential backoff (5s, 10s, 20s, 40s...) via an asynchronous timer thread. The failover process is limited to a maximum of 10 attempts per message to prevent infinite loops, and the administrator is alerted only on the first failure.

### Fixed
- **E2E Failover Loop & Key Fallback**:
  - Added fallback support for both `chat_id` and `chatId` keys in message snapshots to prevent `chat 'Unknown' (ID: None)` errors.
  - Downgraded permanent E2E and resend logs to `WARNING`.
  - Removed administrative failover alert messages completely, relying entirely on structured logging to prevent any potential loop risks.


## [2.2.1] - 2026-06-16

### Fixed
- **Meta Tag Parsing Bug:** Corrected regular expression patterns in Open Graph metadata extraction to use non-greedy matching confined to a single tag (`[^>]*?` instead of `.*?`), preventing false matches across multiple tag boundaries (e.g. on Yandex Translate turbopages proxy URLs).

## [2.2.0] - 2026-06-14

### Added
- **Direct File Downloads:** Added automatic detection of document file URLs (PDF, EPUB, DjVu, MS Office, LibreOffice, and plain text/data files) via extension and response Content-Type headers. Instead of generating HTML previews, the bot now shows a `[ 💾 /download_urlhash ]` button in group chat preview cards.
- **Background Downloader:** Implemented a non-blocking downloader to fetch and cache files up to 50 MB in size using stream chunking to prevent memory overload, sending them directly as Delta Chat message attachments.
- **Safety Exclusions for Local Hosts/IPs:** Integrated local host/IP filtering utilizing the standard Python `ipaddress` library to exclude local hostnames (`localhost`, `example.com`), private IPv4/IPv6 ranges (`127.0.0.1`, `10.0.0.0/8`, link-local, etc.), and local/private domains (`.local`, `.lan`), preventing unnecessary outbound connections and chat spam.
- **Manual Download Command:** Added `/download <url>` manual command trigger.

## [2.1.0] - 2026-06-05

### Added
- **DPI Bypass Hack:** Integrated a patched `deltachat-rpc-server` binary into the Docker setup to bypass SSL DPI connection blocks when communicating with chatmail.
- **Resilient Sending Mode:** Added `/resilient` admin command to configure resilient mode (accepts `on`/`off`/`1`/`0`/`true`/`false`, or no arguments to query current status). When enabled, each outgoing message is sent through all configured mail relays using resending mechanism in a non-blocking background thread to bypass chatmail blocking issues without causing UI delays, while ensuring deduplication into a single message bubble on the recipient client.

## [2.0.0] - 2026-05-31

### Added
- **Mozilla Readability Default Mode:** `/preview` now compiles pages using Mozilla's Readability algorithm into highly compressed, clean, and beautiful Reader Mode HTML documents.
- **Pure-Python Image Optimization Engine:** Automatically downloads, resizes (maximum width 800px), compresses and base64-inlines all images inside readability pages into optimized WebP (preserving transparency) or JPEG formats using Pillow, producing completely offline-readable and featherweight outputs.
- **Premium Reader Styling:** Readability documents are styled using a custom modern responsive layout that adapts seamlessly to desktop or mobile viewport dimensions and supports prefers-color-scheme light/dark modes automatically.
- **Full Interactive Archiving Command:** Added a new `/archive` command running monolith with JavaScript execution enabled.
- **Monolith Base64 Post-Compression:** Integrates BeautifulSoup and Pillow image post-processing to parse generated monolith archives and compress heavy inlined Base64 graphics, reducing monolith file sizes from dozens of megabytes down to 1-2 MB.
- **Backwards Compatibility Support:** Kept `/previewjs` as a silent alias routed directly to the new `/archive` command.


## [1.2.2] - 2026-05-22

### Fixed
- **Profile Status Description:** Restored the missing `/help` command tip in the bot's multiline profile status (`selfstatus`) description.

## [1.2.1] - 2026-05-22

### Fixed
- **Contact Fingerprint Parsing:** Fixed a parsing bug where PGP fingerprints containing newlines or extra whitespaces in `get_contact_encryption_info` failed to be extracted for contacts. This resolves admin verification failures when writing to the bot from secondary relay accounts using the same PGP keys.

## [1.2.0] - 2026-05-22

### Added
- **Content-Length Size Pre-Check:** Integrated a lightweight response header check before starting the heavy `monolith` page compilation. If the remote resource's declared size exceeds **10 MB**, the process is aborted immediately, preventing large downloads.
- **Content-Type Binary/Media Filters:** Added proactive detection for binary and media asset headers (e.g. `application/zip`, `video/mp4`, `audio/mp3`, `application/pdf`, etc.). The bot fast-rejects requests pointing to these media or non-HTML resources.
- **Compiled Output Size Limit:** Enforced a post-compilation safety limit checking the size of the compiled HTML. If it exceeds **50 MB**, the file is discarded to ensure reliability and prevent email delivery transport failures under the server's message limit.

## [1.1.0] - 2026-05-22

### Added
- Implemented Fast-Rejection Optimization for blocked websites. When a target website blocks the bot's lightweight fetches with hard HTTP status codes (such as HTTP 403 Forbidden, 401 Unauthorized, or 404 Not Found) on both standard and fallback User-Agents, the bot caches this failure in SQLite as a `__FAILED_BLOCK__` entry for 1 hour.
- Suppressed empty button auto-preview spam in group chats when lightweight fetches fail with a hard block.
- Implemented fast-rejection check in manual monolith compilation commands (`/preview` and `/previewjs`) using the cache, or running a quick pre-check if the URL is not yet cached. Rejects requests instantly with a `❌` reaction and a clean block error message, completely bypassing the heavy 35-second `monolith` subprocess and saving massive CPU and bandwidth resources.

## [1.0.9] - 2026-05-22

### Added
- Implemented robust self-healing bypass for websites protected by the Anubis Web AI Firewall. The bot detects the cryptographic Proof-of-Work challenge and automatically retries utilizing a custom non-Mozilla User-Agent to retrieve the genuine webpage content.
- Added self-healing fallback in both direct monolith page compilation and group chat Open Graph metadata auto-previews.
- Extended the non-Mozilla fallback capability to the OG banner image downloading subsystem, securing complete preview generation for protected resources.

## [1.0.5] - 2026-05-22

### Added
- Implemented lightweight, automated Open Graph (OG) banner image and title previews inside group chats. Spawns dynamic `/preview_[hash]` and `/previewjs_[hash]` command links for on-demand high-fidelity monolith offline page compilations.
- Implemented a case-insensitive URL exclusions blacklist system (`/preview_exclude <pattern>`, `/preview_unexclude <pattern>`, `/preview_exclusions`) matching by part of the URL (e.g. `/telegram/` or `https://ya.ru`).
- Integrated exclusion checks in both auto-previews and explicit monolith compilations, blocking blacklisted URLs and sending a warning to the requester.
- Added automatic media bot detection: if the YouTube downloader bot (`YT Bot`) is present in the current group chat, WebPreview Bot will automatically skip generating auto-previews for links that `YT Bot` typically handles (such as YouTube, Yandex Music, Rutube, soundcloud, etc.) to prevent duplicate bot postings and chat spam.

## [1.0.4] - 2026-05-22

### Added
- Implemented a 1-hour SQLite-based caching system for page previews. If the exact same URL (with matching JavaScript settings) is requested within 1 hour, the bot returns the cached file directly, dramatically speeding up response time and reducing bandwidth and disk usage.
- Integrated automatic background DB pruning in the hourly cache cleaner loop to purge expired SQLite cache entries alongside deleting local files older than 1 hour.

### Changed
- Converted rate-limiting notifications from an intrusive and spammy text message to an elegant `⏱` emoji reaction attached directly to the triggering message.

## [1.0.3] - 2026-05-22

### Changed
- Standardized the welcome greeting to return the exact same detailed output as the `/help` command instead of a short introductory prefix message.

## [1.0.2] - 2026-05-22

### Fixed
- Resolved `Method not found` error (`-32601`) during private chat greeting checks by migrating contact `greeted` status tracking to the local SQLite database, completely bypassing missing JSON-RPC `get_contact_config` and `set_contact_config` core methods.
- Updated the 1-on-1 private chat welcome message to include a prompt to send `/help` for more commands.
- Restored the multiline `selfstatus` description to correctly separate the general info block and the command trigger line (`Send: /preview <url>`).


## [1.0.1] - 2026-05-22

### Added
- Added custom bot icon `icon.png` which is automatically set as the bot's avatar on startup.
- Auto-detection and auto-parsing of URLs sent directly in 1-on-1 private chats without needing the `/preview` command.
- Auto-welcoming message for new users starting a 1-on-1 private chat with the bot.
- Automatic upgrading of the bot administrator's fingerprint in the SQLite configuration when it becomes available via PGP key exchange.
- Log printing of the bot's SecureJoin QR code URL at startup for easy administration onboarding.

### Fixed
- Fixed `Method not found` JSON-RPC error (`-32601`) on hosts running older versions of `deltachat-rpc-server` by implementing a robust `_is_private_chat` checker with sequential fallbacks (`get_basic_chat_info` -> `get_full_chat_by_id` -> `get_chat_contacts`).
- Restored user custom modifications for caption formatting (double newline before the link: `\n\n🔗`) and `selfstatus` description to preserve them across updates.
- Fixed private chat detection logic to correctly parse both snake_case (`chat_type`) and camelCase (`chatType`) formats returned by different JSON-RPC server releases.
- Fixed build failures in the Docker environment by adding `make` utility package to the builder stage in the `Dockerfile` (needed for compiling Rust dependency crates for `monolith`).

### Changed
- Decoupled nested volume structures in `docker-compose.yml` to store SQLite data in `./data` and Delta Chat credentials in `./webpreview` to prevent file permission and access conflicts.
- Renamed the `/code` command to `/source` to provide a much more logical and intuitive name for getting the bot source repository URLs.
- Standardized file naming for offline HTML previews using the cleaned domain name and unix timestamp (`webpreview_[domain]_[timestamp].html`).

---

## [1.0.0] - 2026-05-21

### Added
- Initial release of Delta Chat WebPreview Bot.
- Multi-transport SMTP/IMAP relays support (same as Delta Chat Bouncer and YT bots).
- HTML single-file offline compilation using Rust-based `monolith` engine.
- Interactive user commands: `/preview`, `/previewjs`, `/help`, `/source`, `/stats`, `/initadmin`, `/remove_transport`, `/add_transport`.
- Robust SQLite persistent database for tracking logs, stats, and configurations.
- Admin commands restricted to verified admin fingerprints.
- Rate-limiting rules (15-second cooldown per user) and cache pruning routines for safe disk storage management.
